"""
Bot Telegram - Modern Space Design (roleplay Minecraft)
Python 3.10+  |  python-telegram-bot >= 21  |  google-genai
"""
import asyncio
import difflib
import functools
import io
import logging
import math
import os
import re
import sqlite3
import threading
import time
import urllib.request
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from html import escape
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from dotenv import load_dotenv
from telegram import (
    BotCommand,
    ChatPermissions,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ChatAction, ChatType, ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

load_dotenv()
logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
)
log = logging.getLogger("bot")

# ───────────────────────── CONFIG ─────────────────────────
BOT_TOKEN = os.environ["BOT_TOKEN"]
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash-image")
CEO_ID = int(os.getenv("CEO_ID", "0"))
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x}
DB_PATH = os.getenv("DB_PATH", "bot.db")
MAX_FLOORS = int(os.getenv("MAX_FLOORS", "4"))  # 1 immagine per piano
BASE_DIR = Path(__file__).parent
HIERARCHY_IMG = BASE_DIR / "gerarchia.png"

# ───────────────────────── RUOLI ─────────────────────────
ROLES = [
    "CEO",
    "Engineering Manager",
    "Sales Manager",
    "Logistic Manager",
    "Academy Manager",
    "Tecnico Cantiere",
    "Progettista Base",
    "Consulente Cliente",
    "Marketing Specialist",
    "Coordinatore Materiali",
    "Istruttore Design",
]
ENGINEERING_ROLES = {"Engineering Manager", "Tecnico Cantiere", "Progettista Base"}


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


_ROLE_MAP = {_norm(r): r for r in ROLES}
_ROLE_MAP.update({"engeneeringmanager": "Engineering Manager",
                  "marcetingspecialist": "Marketing Specialist",
                  "marketingspecialist": "Marketing Specialist"})


def match_role(text: str):
    key = _norm(text)
    if key in _ROLE_MAP:
        return _ROLE_MAP[key]
    close = difflib.get_close_matches(key, list(_ROLE_MAP), n=1, cutoff=0.78)
    return _ROLE_MAP[close[0]] if close else None


# ───────────────────────── DATABASE ─────────────────────────
def _conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def fetch(sql, params=()):
    with closing(_conn()) as c:
        return c.execute(sql, params).fetchall()


def fetch1(sql, params=()):
    rows = fetch(sql, params)
    return rows[0] if rows else None


def execute(sql, params=()):
    with closing(_conn()) as c:
        with c:
            cur = c.execute(sql, params)
            return cur.lastrowid if cur.lastrowid else cur.rowcount


def init_db():
    with closing(_conn()) as c:
        with c:
            c.executescript(
                """
                CREATE TABLE IF NOT EXISTS employees(
                    nickname TEXT PRIMARY KEY COLLATE NOCASE,
                    username TEXT COLLATE NOCASE,
                    user_id INTEGER,
                    role TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS seen_users(
                    user_id INTEGER PRIMARY KEY,
                    username TEXT COLLATE NOCASE,
                    name TEXT
                );
                CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
                CREATE TABLE IF NOT EXISTS channels(
                    name TEXT PRIMARY KEY COLLATE NOCASE, chat_id INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS personals(
                    name TEXT PRIMARY KEY COLLATE NOCASE,
                    kind TEXT NOT NULL, file_id TEXT, text TEXT);
                CREATE TABLE IF NOT EXISTS leaves(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER, nickname TEXT,
                    start TEXT, end TEXT, reason TEXT,
                    status TEXT DEFAULT 'pending');
                """
            )


def get_setting(key):
    r = fetch1("SELECT value FROM settings WHERE key=?", (key,))
    return r["value"] if r else None


def set_setting(key, value):
    execute("INSERT INTO settings(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))


# ───────────────────────── UTILS ─────────────────────────
def is_direzione(user_id: int) -> bool:
    return user_id == CEO_ID or user_id in ADMIN_IDS


def direzione_only(fn):
    @functools.wraps(fn)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not update.effective_user or not is_direzione(update.effective_user.id):
            await update.effective_message.reply_text("⛔ Comando riservato alla Direzione.")
            return
        return await fn(update, context)
    return wrapper


def mention(u) -> str:
    return f"@{u.username}" if u.username else u.full_name


async def remember_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Salva id/username di chi scrive nei gruppi (serve a risolvere @username)."""
    u = update.effective_user
    if not u or u.is_bot:
        return
    execute(
        "INSERT INTO seen_users(user_id,username,name) VALUES(?,?,?) "
        "ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, name=excluded.name",
        (u.id, u.username, u.full_name),
    )
    if u.username:
        execute("UPDATE employees SET user_id=? WHERE username=? COLLATE NOCASE "
                "AND user_id IS NULL", (u.id, u.username))


def lookup_username(username: str):
    r = fetch1("SELECT user_id FROM seen_users WHERE username=? COLLATE NOCASE", (username,))
    if r:
        return r["user_id"]
    r = fetch1("SELECT user_id FROM employees WHERE username=? COLLATE NOCASE "
               "AND user_id IS NOT NULL", (username,))
    return r["user_id"] if r else None


async def resolve_target(update: Update, args: list):
    """Ritorna (user_id, label, args_rimanenti) oppure None (e avvisa)."""
    msg = update.effective_message
    if msg.reply_to_message and msg.reply_to_message.from_user:
        u = msg.reply_to_message.from_user
        return u.id, mention(u), args
    if args and args[0].startswith("@"):
        uid = lookup_username(args[0][1:])
        if uid:
            return uid, args[0], args[1:]
        await msg.reply_text(
            f"❓ Non conosco {args[0]}: deve aver scritto almeno un messaggio in un gruppo "
            "dove sono presente (o aver avviato il bot). Oppure rispondi a un suo messaggio."
        )
        return None
    await msg.reply_text("ℹ️ Indica un @username oppure rispondi a un messaggio dell'utente.")
    return None


def get_employee_for(user):
    r = fetch1("SELECT * FROM employees WHERE user_id=?", (user.id,))
    if not r and user.username:
        r = fetch1("SELECT * FROM employees WHERE username=? COLLATE NOCASE", (user.username,))
        if r:
            execute("UPDATE employees SET user_id=? WHERE nickname=?", (user.id, r["nickname"]))
    return r


# ───────────────────────── /start (privato) ─────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != ChatType.PRIVATE:
        await update.effective_message.reply_text("📩 Scrivimi in privato per usare il pannello personale.")
        return
    u = update.effective_user
    await remember_user(update, context)
    emp = get_employee_for(u)
    nickname = emp["nickname"] if emp else u.full_name
    role = emp["role"] if emp else ("Direzione" if is_direzione(u.id) else "Nessuno (non assunto)")
    uname = f"@{u.username}" if u.username else "—"
    text = (
        "👋 <b>Il tuo profilo</b>\n\n"
        f"👤 Nickname: <b>{escape(nickname)}</b>\n"
        f"📎 Telegram: {escape(uname)}\n"
        f"🆔 ID: <code>{u.id}</code>\n"
        f"💼 Ruolo: <b>{escape(role)}</b>"
    )
    kb = None
    if emp or is_direzione(u.id):
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("📝 Richiedi Congedo", callback_data="congedo")]])
    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)


# ───────────────────────── GERARCHIA ─────────────────────────
async def cmd_gerarchia(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    cached = get_setting("hierarchy_file_id")
    try:
        if cached:
            await msg.reply_photo(cached)
            return
    except TelegramError:
        pass
    with open(HIERARCHY_IMG, "rb") as f:
        sent = await msg.reply_photo(f)
    set_setting("hierarchy_file_id", sent.photo[-1].file_id)


# ───────────────────────── ASSUNZIONI ─────────────────────────
_ASSUMI_RE = re.compile(r"^(.+?)\s+@(\w{3,})\s+(.+)$", re.S)


@direzione_only
async def cmd_assumi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    body = msg.text.split(maxsplit=1)
    m = _ASSUMI_RE.match(body[1].strip()) if len(body) > 1 else None
    if not m:
        await msg.reply_text("Uso: /assumi [Nickname] [@telegram] [Ruolo]\n"
                             "Es: /assumi Marco @marco_mc Tecnico Cantiere")
        return
    nickname, username, role_txt = m.group(1).strip(), m.group(2), m.group(3).strip()
    role = match_role(role_txt)
    if not role:
        await msg.reply_text("❌ Ruolo non valido. Ruoli disponibili:\n• " + "\n• ".join(ROLES))
        return
    if fetch1("SELECT 1 FROM employees WHERE nickname=?", (nickname,)):
        await msg.reply_text("⚠️ Esiste già un dipendente con questo nickname.")
        return
    uid = lookup_username(username)
    execute("INSERT INTO employees(nickname,username,user_id,role) VALUES(?,?,?,?)",
            (nickname, username, uid, role))
    await msg.reply_text(f"✅ <b>{escape(nickname)}</b> (@{escape(username)}) assunto come "
                         f"<b>{role}</b>.", parse_mode=ParseMode.HTML)
    dip = get_setting("dipendenti_chat")
    if dip:
        try:
            await context.bot.send_message(
                int(dip), f"🎉 Diamo il benvenuto a <b>{escape(nickname)}</b> (@{escape(username)}) "
                          f"in qualità di <b>{role}</b>!", parse_mode=ParseMode.HTML)
        except TelegramError:
            pass


@direzione_only
async def cmd_licenzia(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not context.args:
        await msg.reply_text("Uso: /licenzia [Nickname]")
        return
    nickname = " ".join(context.args)
    row = fetch1("SELECT * FROM employees WHERE nickname=?", (nickname,))
    if not row:
        await msg.reply_text("❌ Nessun dipendente con questo nickname.")
        return
    execute("DELETE FROM employees WHERE nickname=?", (nickname,))
    await msg.reply_text(f"🗑️ <b>{escape(row['nickname'])}</b> ({row['role']}) è stato licenziato.",
                         parse_mode=ParseMode.HTML)


# ───────────────────────── CHAT / CANALI ─────────────────────────
@direzione_only
async def cmd_setdipendenti(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == ChatType.PRIVATE:
        await update.effective_message.reply_text("Usalo nel gruppo dipendenti.")
        return
    set_setting("dipendenti_chat", update.effective_chat.id)
    await update.effective_message.reply_text("✅ Questa chat è ora la chat <b>Dipendenti</b>.",
                                              parse_mode=ParseMode.HTML)


@direzione_only
async def cmd_setdirezione(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == ChatType.PRIVATE:
        await update.effective_message.reply_text("Usalo nel gruppo direzione.")
        return
    set_setting("direzione_chat", update.effective_chat.id)
    await update.effective_message.reply_text("✅ Questa chat è ora la chat <b>Direzione</b>.",
                                              parse_mode=ParseMode.HTML)


@direzione_only
async def cmd_setchannel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/setchannel nome            -> salva la chat corrente
       /setchannel nome @canale    -> salva un canale pubblico (il bot deve esserne admin)
       /setchannel nome -100123..  -> salva tramite id"""
    msg = update.effective_message
    if not context.args:
        await msg.reply_text("Uso: /setchannel [nomecanale] [@canale o id (facoltativo)]")
        return
    name = context.args[0].lower()
    if name in ("dipendenti", "direzione"):
        await msg.reply_text("Nome riservato, scegline un altro.")
        return
    target = update.effective_chat.id
    if len(context.args) > 1:
        raw = context.args[1]
        try:
            chat = await context.bot.get_chat(int(raw) if re.fullmatch(r"-?\d+", raw) else raw)
            target = chat.id
        except TelegramError:
            await msg.reply_text("❌ Non riesco ad accedere a quel canale. Il bot ne è admin?")
            return
    execute("INSERT INTO channels(name,chat_id) VALUES(?,?) "
            "ON CONFLICT(name) DO UPDATE SET chat_id=excluded.chat_id", (name, target))
    await msg.reply_text(f"✅ Canale <b>{escape(name)}</b> salvato.", parse_mode=ParseMode.HTML)


def resolve_channel(name: str):
    name = name.lower()
    if name == "dipendenti":
        v = get_setting("dipendenti_chat")
        return int(v) if v else None
    if name == "direzione":
        v = get_setting("direzione_chat")
        return int(v) if v else None
    r = fetch1("SELECT chat_id FROM channels WHERE name=?", (name,))
    return r["chat_id"] if r else None


@direzione_only
async def cmd_inoltra(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not context.args or not msg.reply_to_message:
        await msg.reply_text("Uso: rispondi a un messaggio con /inoltra [canale]\n"
                             "Canali: dipendenti, direzione o uno creato con /setchannel")
        return
    target = resolve_channel(" ".join(context.args))
    if target is None:
        await msg.reply_text("❌ Canale non trovato o non settato.")
        return
    try:
        await context.bot.copy_message(target, msg.chat_id, msg.reply_to_message.message_id)
        await msg.reply_text("📨 Messaggio inviato.")
    except TelegramError as e:
        await msg.reply_text(f"❌ Invio fallito: {e.message}")


# ───────────────────────── MODERAZIONE ─────────────────────────
_DUR_RE = re.compile(r"^(\d+)([smh])$", re.I)


@direzione_only
async def cmd_ban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg, chat = update.effective_message, update.effective_chat
    if chat.type == ChatType.PRIVATE:
        await msg.reply_text("Usalo in un gruppo.")
        return
    res = await resolve_target(update, context.args)
    if not res:
        return
    uid, label, rest = res
    if is_direzione(uid):
        await msg.reply_text("⛔ Non puoi bannare un membro della Direzione.")
        return
    reason = " ".join(rest).strip()
    try:
        await context.bot.ban_chat_member(chat.id, uid)
    except TelegramError as e:
        await msg.reply_text(f"❌ Impossibile bannare: {e.message}")
        return
    await msg.reply_text(f"🔨 {label} è stato bannato." + (f"\n📄 Motivo: {reason}" if reason else ""))


@direzione_only
async def cmd_unban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg, chat = update.effective_message, update.effective_chat
    res = await resolve_target(update, context.args)
    if not res:
        return
    uid, label, _ = res
    try:
        await context.bot.unban_chat_member(chat.id, uid, only_if_banned=True)
    except TelegramError as e:
        await msg.reply_text(f"❌ Impossibile sbannare: {e.message}")
        return
    await msg.reply_text(f"✅ {label} è stato sbannato.")


@direzione_only
async def cmd_mute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg, chat = update.effective_message, update.effective_chat
    if chat.type == ChatType.PRIVATE:
        await msg.reply_text("Usalo in un gruppo.")
        return
    res = await resolve_target(update, context.args)
    if not res:
        return
    uid, label, rest = res
    if is_direzione(uid):
        await msg.reply_text("⛔ Non puoi mutare un membro della Direzione.")
        return
    until, dur_txt = None, "a tempo indeterminato"
    if rest and (m := _DUR_RE.match(rest[0])):
        n, unit = int(m.group(1)), m.group(2).lower()
        secs = max(30, n * {"s": 1, "m": 60, "h": 3600}[unit])  # <30s = per sempre su Telegram
        until = datetime.now(timezone.utc) + timedelta(seconds=secs)
        dur_txt = f"per {m.group(1)}{unit}" + (" (minimo 30s)" if n * {"s": 1, "m": 60, "h": 3600}[unit] < 30 else "")
        rest = rest[1:]
    reason = " ".join(rest).strip()
    try:
        await context.bot.restrict_chat_member(
            chat.id, uid, ChatPermissions.no_permissions(), until_date=until)
    except TelegramError as e:
        await msg.reply_text(f"❌ Impossibile mutare: {e.message}")
        return
    await msg.reply_text(f"🔇 {label} è stato mutato {dur_txt}."
                         + (f"\n📄 Motivo: {reason}" if reason else ""))


@direzione_only
async def cmd_unmute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg, chat = update.effective_message, update.effective_chat
    res = await resolve_target(update, context.args)
    if not res:
        return
    uid, label, _ = res
    try:
        c = await context.bot.get_chat(chat.id)
        perms = c.permissions or ChatPermissions.all_permissions()
        await context.bot.restrict_chat_member(chat.id, uid, perms)
    except TelegramError as e:
        await msg.reply_text(f"❌ Impossibile smutare: {e.message}")
        return
    await msg.reply_text(f"🔊 {label} può di nuovo scrivere.")


# ───────────────────────── PERSONAL ─────────────────────────
BUILTIN = {
    "start", "assumi", "licenzia", "gerarchia", "piantina", "setdipendenti", "setdirezione",
    "setchannel", "inoltra", "ban", "unban", "mute", "unmute", "personal", "unpersonal",
    "congedi", "annulla", "salta",
}


@direzione_only
async def cmd_personal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    r = msg.reply_to_message
    if not context.args or not r:
        await msg.reply_text("Uso: rispondi a un messaggio con /personal [nomecomando]")
        return
    name = context.args[0].lower().lstrip("/")
    if not re.fullmatch(r"[a-z0-9_]{1,32}", name):
        await msg.reply_text("❌ Nome non valido (solo lettere, numeri e _).")
        return
    if name in BUILTIN:
        await msg.reply_text("❌ Nome riservato a un comando del bot.")
        return
    kind, file_id = "text", None
    for k, attr in (("photo", "photo"), ("sticker", "sticker"), ("video", "video"),
                    ("animation", "animation"), ("document", "document"),
                    ("voice", "voice"), ("audio", "audio")):
        obj = getattr(r, attr)
        if obj:
            kind = k
            file_id = obj[-1].file_id if k == "photo" else obj.file_id
            break
    text = r.text_html if r.text else (r.caption_html if r.caption else None)
    if kind == "text" and not text:
        await msg.reply_text("❌ Il messaggio non ha contenuto salvabile.")
        return
    execute("INSERT INTO personals(name,kind,file_id,text) VALUES(?,?,?,?) "
            "ON CONFLICT(name) DO UPDATE SET kind=excluded.kind, file_id=excluded.file_id, "
            "text=excluded.text", (name, kind, file_id, text))
    await msg.reply_text(f"✅ Comando /{name} salvato.")


@direzione_only
async def cmd_unpersonal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not context.args:
        await msg.reply_text("Uso: /unpersonal [nomecomando]")
        return
    name = context.args[0].lower().lstrip("/")
    n = execute("DELETE FROM personals WHERE name=?", (name,))
    await msg.reply_text(f"🗑️ /{name} eliminato." if n else "❌ Comando personal non trovato.")


async def personal_dispatch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg or not msg.text:
        return
    first = msg.text.split()[0][1:]
    name, _, target_bot = first.partition("@")
    if target_bot and target_bot.lower() != (context.bot.username or "").lower():
        return
    row = fetch1("SELECT * FROM personals WHERE name=?", (name.lower(),))
    if not row:
        return
    kind, fid, text = row["kind"], row["file_id"], row["text"]
    kw = dict(caption=text, parse_mode=ParseMode.HTML) if text else {}
    if kind == "text":
        await msg.chat.send_message(text, parse_mode=ParseMode.HTML)
    elif kind == "photo":
        await msg.chat.send_photo(fid, **kw)
    elif kind == "video":
        await msg.chat.send_video(fid, **kw)
    elif kind == "animation":
        await msg.chat.send_animation(fid, **kw)
    elif kind == "document":
        await msg.chat.send_document(fid, **kw)
    elif kind == "voice":
        await msg.chat.send_voice(fid, **kw)
    elif kind == "audio":
        await msg.chat.send_audio(fid, **kw)
    elif kind == "sticker":
        await msg.chat.send_sticker(fid)


# ───────────────────────── CONGEDI ─────────────────────────
C_NICK, C_START, C_END, C_REASON = range(10, 14)


def parse_date(s: str):
    s = s.strip().replace("-", "/").replace(".", "/")
    try:
        return datetime.strptime(s, "%d/%m/%Y").date()
    except ValueError:
        return None


async def congedo_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    emp = get_employee_for(q.from_user)
    if not emp and not is_direzione(q.from_user.id):
        await q.answer("Non risulti assunto.", show_alert=True)
        return ConversationHandler.END
    await q.answer()
    context.user_data["leave"] = {}
    await q.message.reply_text("📝 <b>Richiesta congedo</b>\n(/annulla per uscire)\n\n"
                               "Qual è il tuo <b>Nickname</b>?", parse_mode=ParseMode.HTML)
    return C_NICK


async def congedo_nick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["leave"]["nick"] = update.message.text.strip()
    await update.message.reply_text("📅 Data di <b>inizio</b> congedo? (GG/MM/AAAA)", parse_mode=ParseMode.HTML)
    return C_START


async def congedo_start_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    d = parse_date(update.message.text)
    if not d:
        await update.message.reply_text("❌ Formato non valido, usa GG/MM/AAAA.")
        return C_START
    context.user_data["leave"]["start"] = d
    await update.message.reply_text("📅 Data di <b>fine</b> congedo? (GG/MM/AAAA)", parse_mode=ParseMode.HTML)
    return C_END


async def congedo_end_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    d = parse_date(update.message.text)
    if not d:
        await update.message.reply_text("❌ Formato non valido, usa GG/MM/AAAA.")
        return C_END
    if d < context.user_data["leave"]["start"]:
        await update.message.reply_text("❌ La fine non può precedere l'inizio.")
        return C_END
    context.user_data["leave"]["end"] = d
    await update.message.reply_text("✏️ <b>Motivazione</b> del congedo?", parse_mode=ParseMode.HTML)
    return C_REASON


async def congedo_reason(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = context.user_data.pop("leave")
    data["reason"] = update.message.text.strip()
    direz = get_setting("direzione_chat")
    if not direz:
        await update.message.reply_text("⚠️ Gruppo direzione non settato.")
        return ConversationHandler.END
    u = update.effective_user
    emp = get_employee_for(u)
    lid = execute(
        "INSERT INTO leaves(user_id,nickname,start,end,reason) VALUES(?,?,?,?,?)",
        (u.id, data["nick"], data["start"].isoformat(), data["end"].isoformat(), data["reason"]),
    )
    text = (
        "📝 <b>Nuova richiesta di congedo</b>\n\n"
        f"👤 {escape(data['nick'])} ({escape(mention(u))})\n"
        f"💼 {escape(emp['role']) if emp else 'Direzione'}\n"
        f"📅 Dal <b>{data['start']:%d/%m/%Y}</b> al <b>{data['end']:%d/%m/%Y}</b>\n"
        f"✏️ Motivo: {escape(data['reason'])}"
    )
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Accetta", callback_data=f"leave:ok:{lid}"),
        InlineKeyboardButton("❌ Rifiuta", callback_data=f"leave:no:{lid}"),
    ]])
    try:
        await context.bot.send_message(int(direz), text, parse_mode=ParseMode.HTML, reply_markup=kb)
    except TelegramError:
        execute("DELETE FROM leaves WHERE id=?", (lid,))
        await update.message.reply_text("⚠️ Non riesco a scrivere nel gruppo direzione. Avvisa la Direzione.")
        return ConversationHandler.END
    await update.message.reply_text("📨 Richiesta inviata alla Direzione. Riceverai l'esito qui.")
    return ConversationHandler.END


async def leave_decision(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_direzione(q.from_user.id):
        await q.answer("Solo la Direzione può decidere.", show_alert=True)
        return
    _, action, lid = q.data.split(":")
    row = fetch1("SELECT * FROM leaves WHERE id=?", (int(lid),))
    if not row or row["status"] != "pending":
        await q.answer("Richiesta già gestita.", show_alert=True)
        return
    approved = action == "ok"
    execute("UPDATE leaves SET status=? WHERE id=?", ("approved" if approved else "rejected", row["id"]))
    verdict = "✅ Approvato" if approved else "❌ Rifiutato"
    await q.answer(verdict)
    await q.edit_message_text(f"{q.message.text_html}\n\n<b>{verdict}</b> da {escape(mention(q.from_user))}",
                              parse_mode=ParseMode.HTML)
    s, e = date.fromisoformat(row["start"]), date.fromisoformat(row["end"])
    try:
        await context.bot.send_message(
            row["user_id"],
            f"{'✅ Il tuo congedo è stato <b>approvato</b>' if approved else '❌ Il tuo congedo è stato <b>rifiutato</b>'}"
            f"\n📅 {s:%d/%m/%Y} → {e:%d/%m/%Y}", parse_mode=ParseMode.HTML)
    except TelegramError:
        pass


async def cmd_congedi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    direz = get_setting("direzione_chat")
    if not (is_direzione(update.effective_user.id) or (direz and int(direz) == chat_id)):
        await update.effective_message.reply_text("⛔ Comando riservato alla Direzione.")
        return
    today = date.today().isoformat()
    active = fetch("SELECT * FROM leaves WHERE status='approved' AND end>=? ORDER BY start", (today,))
    pending = fetch("SELECT * FROM leaves WHERE status='pending' ORDER BY start")
    if not active and not pending:
        await update.effective_message.reply_text("📭 Nessun congedo attivo.")
        return

    def fmt(r):
        s, e = date.fromisoformat(r["start"]), date.fromisoformat(r["end"])
        return (f"• <b>{escape(r['nickname'])}</b>: {s:%d/%m/%Y} → {e:%d/%m/%Y}\n"
                f"   ✏️ {escape(r['reason'])}")

    parts = []
    if active:
        parts.append("🏖️ <b>Congedi attivi</b>\n" + "\n".join(fmt(r) for r in active))
    if pending:
        parts.append("⏳ <b>In attesa di risposta</b>\n" + "\n".join(fmt(r) for r in pending))
    await update.effective_message.reply_text("\n\n".join(parts), parse_mode=ParseMode.HTML)


# ───────────────────────── PIANTINA ─────────────────────────
P_PHOTO, P_SIZE, P_FLOORS, P_FURN, P_NOTES, P_POS = range(6)

EMOJI_TABLE = [
    (("letto", "branda"), "🛏️"), (("divano", "poltrona"), "🛋️"),
    (("tavolo", "tavolino"), "🍽️"), (("sedia", "sgabello"), "🪑"),
    (("cucina", "fornelli", "piano cottura", "forno"), "🍳"), (("frigo",), "🧊"),
    (("bagno", "wc", "water"), "🚽"), (("doccia",), "🚿"), (("vasca",), "🛁"),
    (("lavandino", "lavabo"), "🚰"), (("tv", "televisore"), "📺"),
    (("pc", "computer", "scrivania", "postazione"), "💻"), (("armadio", "guardaroba"), "🗄️"),
    (("libreria", "scaffal"), "📚"), (("pianta", "vaso"), "🪴"), (("lampada", "luce"), "💡"),
    (("camino",), "🔥"), (("specchio",), "🪞"), (("scala",), "🪜"), (("piscina",), "🏊"),
    (("auto", "garage", "macchina"), "🚗"), (("bancone", "cassa", "reception"), "🧾"),
    (("pianoforte",), "🎹"), (("quadro",), "🖼️"), (("jukebox", "musica"), "🎵"),
    (("baule", "chest"), "📦"),
]


def emoji_for(name: str) -> str:
    t = name.lower()
    for keys, emo in EMOJI_TABLE:
        if any(k in t for k in keys):
            return emo
    return "📦"


def parse_size(text: str):
    """-> (w, h, area, approx) oppure None"""
    t = text.lower()
    nums = [float(n.replace(",", ".")) for n in re.findall(r"\d+(?:[.,]\d+)?", t)]
    if not nums:
        return None
    if len(nums) >= 2 and re.search(r"\d\s*(?:[x×*]|per)\s*\d", t) and not re.search(r"m\s*[²2]|mq|quadr", t):
        w, h = round(nums[0]), round(nums[1])
        approx = False
    else:
        area = nums[0]
        w = max(1, round(math.sqrt(area * 4 / 3)))
        h = max(1, round(area / w))
        approx = True
    area = w * h
    if w < 3 or h < 3 or w > 200 or h > 200:
        return None
    return w, h, area, approx


ZONES = {  # sigla: (nome italiano, descrizione per l'AI, freccia)
    "NO": ("Nord-Ovest", "north-west area (top-left)", "↖️"),
    "N": ("Nord", "north area (top, middle)", "⬆️"),
    "NE": ("Nord-Est", "north-east area (top-right)", "↗️"),
    "O": ("Ovest", "west area (left, middle)", "⬅️"),
    "C": ("Centro", "center of the floor", "⏺️"),
    "E": ("Est", "east area (right, middle)", "➡️"),
    "SO": ("Sud-Ovest", "south-west area (bottom-left)", "↙️"),
    "S": ("Sud", "south area (bottom, middle)", "⬇️"),
    "SE": ("Sud-Est", "south-east area (bottom-right)", "↘️"),
}


def _zone_kb():
    rows = [["NO", "N", "NE"], ["O", "C", "E"], ["SO", "S", "SE"]]
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"{ZONES[z][2]} {ZONES[z][0]}", callback_data=f"pz:{z}") for z in r]
        for r in rows
    ])


def _furn_kb(plan):
    sel = plan["selected"]
    rows = [[InlineKeyboardButton(f"{'✅' if i in sel else '⬜'} {f['emoji']} {f['name']}",
                                  callback_data=f"pf:{i}")]
            for i, f in enumerate(plan["furn"])]
    rows.append([InlineKeyboardButton("➡️ Conferma", callback_data="pf:ok")])
    return InlineKeyboardMarkup(rows)


async def piantina_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != ChatType.PRIVATE:
        await update.effective_message.reply_text("📩 Usa /piantina in privato con me.")
        return ConversationHandler.END
    user = update.effective_user
    emp = get_employee_for(user)
    if not is_direzione(user.id) and (not emp or emp["role"] not in ENGINEERING_ROLES):
        await update.effective_message.reply_text("⛔ Comando riservato al reparto Engineering e alla Direzione.")
        return ConversationHandler.END
    context.user_data["plan"] = {"furn": [], "placements": []}
    await update.effective_message.reply_text(
        "🏗️ <b>Nuova piantina</b> (/annulla per uscire)\n\n"
        "📷 Hai una <b>foto della struttura</b> (screenshot di Minecraft)? "
        "Mandamela così capisco com'è fatta.\nSe non ce l'hai: /salta.",
        parse_mode=ParseMode.HTML)
    return P_PHOTO


async def piantina_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if msg.photo:
        tg_file, mime = await msg.photo[-1].get_file(), "image/jpeg"
    else:
        tg_file, mime = await msg.document.get_file(), (msg.document.mime_type or "image/jpeg")
    data = bytes(await tg_file.download_as_bytearray())
    context.user_data["plan"]["ref"] = (data, mime)
    await msg.reply_text("✅ Foto ricevuta.")
    return await _ask_size(update)


async def piantina_photo_skip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await _ask_size(update)


async def piantina_photo_hint(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📷 Mandami una foto della struttura oppure /salta.")
    return P_PHOTO


async def _ask_size(update: Update):
    await update.effective_chat.send_message(
        "📐 <b>Quanto è grande l'edificio?</b> (per piano, 1 blocco = 1 metro)\n"
        "Scrivi le dimensioni in blocchi, es. <code>20x15</code>, oppure i m², es. <code>300 m2</code>.",
        parse_mode=ParseMode.HTML)
    return P_SIZE


async def piantina_size(update: Update, context: ContextTypes.DEFAULT_TYPE):
    parsed = parse_size(update.message.text)
    if not parsed:
        await update.message.reply_text("❌ Non ho capito (min 3, max 200 blocchi per lato). "
                                        "Esempi: <code>20x15</code> o <code>300 m2</code>",
                                        parse_mode=ParseMode.HTML)
        return P_SIZE
    w, h, area, approx = parsed
    plan = context.user_data["plan"]
    plan.update(w=w, h=h, area=area, approx=approx)
    extra = f" (forma stimata, circa {w}x{h})" if approx else ""
    await update.message.reply_text(f"✅ {w}×{h} blocchi = {area} m² per piano{extra}.\n\n"
                                    f"🏢 <b>Quanti piani</b> ha l'edificio? (max {MAX_FLOORS})",
                                    parse_mode=ParseMode.HTML)
    return P_FLOORS


async def piantina_floors(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = update.message.text.strip()
    if not t.isdigit() or not (1 <= int(t) <= MAX_FLOORS):
        await update.message.reply_text(f"❌ Inserisci un numero da 1 a {MAX_FLOORS}.")
        return P_FLOORS
    context.user_data["plan"]["floors"] = int(t)
    await update.message.reply_text(
        "🪑 <b>Elenca le forniture</b> (separate da virgola o a capo), es: <i>letto, divano, cucina, bagno</i>\n"
        "Se una fornitura è presente più volte (es. due letti) scrivila due volte.\n"
        "Facoltativo: /salta per saltare.", parse_mode=ParseMode.HTML)
    return P_FURN


async def piantina_furn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    items = [x.strip() for x in re.split(r"[,\n;]+", update.message.text) if x.strip()][:20]
    context.user_data["plan"]["furn"] = [{"name": i, "emoji": emoji_for(i)} for i in items]
    return await _ask_notes(update)


async def piantina_furn_skip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await _ask_notes(update)


async def _ask_notes(update: Update):
    await update.effective_chat.send_message(
        "📝 <b>Note aggiuntive?</b> (stile, stanze desiderate, ecc.)\n"
        "Facoltativo: /salta per saltare.", parse_mode=ParseMode.HTML)
    return P_NOTES


async def piantina_notes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["plan"]["notes"] = update.message.text.strip()
    return await _begin_positions(update, context)


async def piantina_notes_skip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["plan"]["notes"] = ""
    return await _begin_positions(update, context)


# --- posizionamento forniture: Piano -> Forniture -> Zona del piano (tutto a pulsanti) ---
async def _begin_positions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    plan = context.user_data["plan"]
    chat = update.effective_chat
    if not plan["furn"]:
        return await _generate(chat, context)
    plan["cur"] = 1
    await _start_floor(chat, plan)
    return P_POS


async def _start_floor(chat, plan):
    plan["selected"], plan["stage"] = set(), "select"
    await chat.send_message(
        f"🏢 <b>Piano {plan['cur']}/{plan['floors']}</b>"
        f"{' (piano terra)' if plan['cur'] == 1 and plan['floors'] > 1 else ''}\n"
        "Quali forniture ci sono a questo piano? Tocca per selezionare, poi premi Conferma "
        "(puoi anche non selezionare nulla).",
        parse_mode=ParseMode.HTML, reply_markup=_furn_kb(plan))


async def _next_zone_or_floor(chat, context, plan):
    if plan["queue"]:
        f = plan["furn"][plan["queue"][0]]
        plan["stage"] = "zone"
        await chat.send_message(
            f"📍 Piano {plan['cur']} — in quale <b>zona del piano</b> si trova "
            f"{f['emoji']} <b>{escape(f['name'])}</b>?\n<i>(Nord = parte alta della piantina)</i>",
            parse_mode=ParseMode.HTML, reply_markup=_zone_kb())
        return P_POS
    if plan["cur"] < plan["floors"]:
        plan["cur"] += 1
        await _start_floor(chat, plan)
        return P_POS
    return await _generate(chat, context)


async def pos_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    plan = context.user_data.get("plan")
    if not plan:
        await q.answer("Sessione scaduta, rifai /piantina.", show_alert=True)
        return ConversationHandler.END
    if plan.get("stage") != "select":
        await q.answer()
        return P_POS
    arg = q.data.split(":")[1]
    if arg == "ok":
        await q.answer()
        plan["queue"] = sorted(plan["selected"])
        names = ", ".join(f"{plan['furn'][i]['emoji']} {plan['furn'][i]['name']}"
                          for i in plan["queue"]) or "nessuna fornitura"
        await q.edit_message_text(f"🏢 Piano {plan['cur']}: {names}")
        return await _next_zone_or_floor(update.effective_chat, context, plan)
    i = int(arg)
    plan["selected"] ^= {i}
    await q.answer()
    await q.edit_message_reply_markup(_furn_kb(plan))
    return P_POS


async def pos_zone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    plan = context.user_data.get("plan")
    if not plan:
        await q.answer("Sessione scaduta, rifai /piantina.", show_alert=True)
        return ConversationHandler.END
    if plan.get("stage") != "zone" or not plan.get("queue"):
        await q.answer()
        return P_POS
    z = q.data.split(":")[1]
    f = plan["furn"][plan["queue"].pop(0)]
    plan["placements"].append({"name": f["name"], "emoji": f["emoji"], "floor": plan["cur"], "zone": z})
    await q.answer()
    await q.edit_message_text(f"✅ {f['emoji']} {f['name']} — Piano {plan['cur']}, zona {ZONES[z][0]}")
    return await _next_zone_or_floor(update.effective_chat, context, plan)


async def pos_text_hint(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👆 Usa i pulsanti del messaggio precedente per rispondere.")
    return P_POS


def build_prompt(plan: dict, floor: int) -> str:
    w, h = plan["w"], plan["h"]
    approx = "approximately " if plan.get("approx") else ""
    lines = [
        f"A clean, professional top-down 2D architectural floor plan (blueprint style) of floor "
        f"{floor} of {plan['floors']}{' (ground floor)' if floor == 1 and plan['floors'] > 1 else ''} "
        "of a building.",
        f"Footprint: {approx}{w} m x {h} m ({w * h} m²), drawn to scale on a visible square grid "
        "where ONE grid square = 1 meter = 1 Minecraft block. Add a scale bar (0-5-10 m) and "
        "overall dimension lines.",
        "Thick black walls, doors as arcs, windows as thin double lines, room names written in Italian, "
        "white background, strictly orthographic top view, no 3D, no perspective, no people. "
        "North is the top of the image.",
    ]
    if plan.get("ref"):
        lines.append("A reference photo of the real building (a Minecraft structure) is attached: use it to "
                     "understand its shape, proportions, entrances, windows and room arrangement, and keep "
                     "the floor plan consistent with it, while respecting the exact dimensions above.")
    if plan["floors"] > 1:
        lines.append("Include a staircase in a consistent position on every floor.")
    items = [x for x in plan["placements"] if x["floor"] == floor]
    if items:
        lines.append("Mark each of these furnishings with a clear RED DOT inside the indicated zone of the "
                     "floor, with a small emoji icon next to the dot, and add a legend:")
        lines += [f"- {x['emoji']} {x['name']}: {ZONES[x['zone']][1]}" for x in items]
    if plan.get("notes"):
        lines.append(f"Additional notes: {plan['notes']}")
    return "\n".join(lines)


_client = None


def _gen_image_sync(prompt: str, ref=None) -> bytes:
    global _client
    from google import genai
    from google.genai import types
    if _client is None:
        _client = genai.Client(api_key=GEMINI_API_KEY)
    contents = [types.Part.from_bytes(data=ref[0], mime_type=ref[1]), prompt] if ref else prompt
    resp = _client.models.generate_content(model=GEMINI_MODEL, contents=contents)
    for cand in resp.candidates or []:
        for part in (cand.content.parts if cand.content else []) or []:
            if getattr(part, "inline_data", None) and part.inline_data.data:
                return part.inline_data.data
    raise RuntimeError("Gemini non ha restituito alcuna immagine.")


async def _generate(chat, context: ContextTypes.DEFAULT_TYPE):
    plan = context.user_data.pop("plan")
    if not GEMINI_API_KEY:
        await chat.send_message("⚠️ GEMINI_API_KEY non configurata.")
        return ConversationHandler.END
    await chat.send_message(f"🎨 Genero {plan['floors']} piantina/e... ci vuole qualche secondo.")
    for floor in range(1, plan["floors"] + 1):
        await chat.send_chat_action(ChatAction.UPLOAD_PHOTO)
        try:
            img = await asyncio.to_thread(_gen_image_sync, build_prompt(plan, floor), plan.get("ref"))
        except Exception as e:  # quota, safety, rete...
            log.exception("Gemini error")
            await chat.send_message(f"❌ Errore nella generazione del piano {floor}: {str(e)[:300]}")
            continue
        legend = [f"{x['emoji']} {x['name']} — zona {ZONES[x['zone']][0]}"
                  for x in plan["placements"] if x["floor"] == floor]
        caption = (f"🏢 Piano {floor}/{plan['floors']} — {plan['w']}×{plan['h']} blocchi "
                   f"({plan['area']} m²)\n📏 Scala: 1 blocco = 1 m")
        if legend:
            caption += "\n\n" + "\n".join(legend)
        await chat.send_photo(io.BytesIO(img), caption=caption[:1024])
    return ConversationHandler.END


async def conv_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("plan", None)
    context.user_data.pop("leave", None)
    await update.effective_message.reply_text("🚫 Operazione annullata.")
    return ConversationHandler.END


# ───────────────────────── KEEP-ALIVE (Render) ─────────────────────────
HEARTBEAT_FILE = BASE_DIR / "heartbeat.txt"
PORT = int(os.getenv("PORT", "10000"))


def start_keepalive():
    """Avvia 3 thread: server HTTP (richiesto da Render), heartbeat su .txt, self-ping."""
    state = {"n": 0}

    class Handler(BaseHTTPRequestHandler):
        def _reply(self, with_body):
            body = f"OK {state['n']}".encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if with_body:
                self.wfile.write(body)

        def do_GET(self):
            self._reply(True)

        def do_HEAD(self):
            self._reply(False)

        def log_message(self, *args):
            pass

    def serve():
        HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()

    def heartbeat():  # ogni 15s scrive un numero crescente in heartbeat.txt
        while True:
            state["n"] += 1
            try:
                HEARTBEAT_FILE.write_text(str(state["n"]))
            except OSError as e:
                log.warning("heartbeat: %s", e)
            time.sleep(15)

    def self_ping():  # ogni 10 min richiama il proprio URL pubblico
        url = os.getenv("RENDER_EXTERNAL_URL")
        if not url:
            return
        while True:
            time.sleep(600)
            try:
                urllib.request.urlopen(url, timeout=15).read()
            except Exception as e:
                log.warning("self-ping: %s", e)

    for fn in (serve, heartbeat, self_ping):
        threading.Thread(target=fn, daemon=True).start()
    log.info("Keep-alive attivo sulla porta %s", PORT)


# ───────────────────────── MAIN ─────────────────────────
async def post_init(app: Application):
    await app.bot.set_my_commands([
        BotCommand("start", "Il tuo profilo"),
        BotCommand("gerarchia", "Mostra l'organigramma"),
        BotCommand("piantina", "Genera una piantina 2D (Engineering)"),
    ])


def main():
    init_db()
    start_keepalive()
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    text = filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE

    # tracciamento utenti dei gruppi (gruppo -1 => non blocca gli altri handler)
    app.add_handler(MessageHandler(filters.ChatType.GROUPS & ~filters.StatusUpdate.ALL, remember_user), group=-1)

    app.add_handler(ConversationHandler(
        entry_points=[CommandHandler("piantina", piantina_start)],
        states={
            P_PHOTO: [
                CommandHandler("salta", piantina_photo_skip),
                MessageHandler((filters.PHOTO | filters.Document.IMAGE) & filters.ChatType.PRIVATE, piantina_photo),
                MessageHandler(text, piantina_photo_hint),
            ],
            P_SIZE: [MessageHandler(text, piantina_size)],
            P_FLOORS: [MessageHandler(text, piantina_floors)],
            P_FURN: [CommandHandler("salta", piantina_furn_skip), MessageHandler(text, piantina_furn)],
            P_NOTES: [CommandHandler("salta", piantina_notes_skip), MessageHandler(text, piantina_notes)],
            P_POS: [
                CallbackQueryHandler(pos_toggle, pattern=r"^pf:(\d+|ok)$"),
                CallbackQueryHandler(pos_zone, pattern=r"^pz:[A-Z]{1,2}$"),
                MessageHandler(text, pos_text_hint),
            ],
        },
        fallbacks=[CommandHandler("annulla", conv_cancel)],
    ))
    app.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(congedo_start, pattern="^congedo$")],
        states={
            C_NICK: [MessageHandler(text, congedo_nick)],
            C_START: [MessageHandler(text, congedo_start_date)],
            C_END: [MessageHandler(text, congedo_end_date)],
            C_REASON: [MessageHandler(text, congedo_reason)],
        },
        fallbacks=[CommandHandler("annulla", conv_cancel)],
    ))

    for name, fn in [
        ("start", cmd_start), ("gerarchia", cmd_gerarchia), ("assumi", cmd_assumi),
        ("licenzia", cmd_licenzia), ("setdipendenti", cmd_setdipendenti),
        ("setdirezione", cmd_setdirezione), ("setchannel", cmd_setchannel),
        ("inoltra", cmd_inoltra), ("ban", cmd_ban), ("unban", cmd_unban),
        ("mute", cmd_mute), ("unmute", cmd_unmute), ("personal", cmd_personal),
        ("unpersonal", cmd_unpersonal), ("congedi", cmd_congedi),
    ]:
        app.add_handler(CommandHandler(name, fn))

    app.add_handler(CallbackQueryHandler(leave_decision, pattern=r"^leave:(ok|no):\d+$"))
    # ultimo: qualsiasi comando non riconosciuto => cerca tra i /personal
    app.add_handler(MessageHandler(filters.COMMAND, personal_dispatch))

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
