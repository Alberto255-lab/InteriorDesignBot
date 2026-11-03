# Bot Modern Space Design

## Avvio
    pip install -r requirements.txt
    cp .env.example .env     # compila i valori
    python bot.py

## Setup Telegram
1. @BotFather → /setprivacy → **Disable** (così il bot vede gli utenti dei gruppi e può risolvere gli @username).
2. Aggiungi il bot ai gruppi come **amministratore** (permessi: bannare/limitare utenti).
3. Nel gruppo dipendenti: /setdipendenti — nel gruppo direzione: /setdirezione.
4. Per sapere un ID Telegram: @userinfobot.

## Note
- /ban /mute /unban /unmute accettano @username (se l'utente ha scritto almeno una volta nel gruppo) oppure la risposta a un messaggio.
- /setchannel nome [@canale|id]: senza secondo parametro salva la chat corrente.
- Il database è `bot.db` (SQLite), creato in automatico.

## Deploy su Render
1. Carica la cartella su un repository GitHub (privato). `.env` NON va caricato (è nel .gitignore).
2. Render → New → Web Service → collega il repo. Build: `pip install -r requirements.txt`, Start: `python bot.py`, Plan: Free.
   (oppure New → Blueprint, che legge `render.yaml`)
3. Environment: BOT_TOKEN, GEMINI_API_KEY, CEO_ID, ADMIN_IDS.
4. Dopo il deploy copia l'URL (https://nome.onrender.com) e crealo come monitor HTTP su uptimerobot.com con intervallo 5 minuti.

## Keep-alive
Il bot apre un mini server HTTP sulla porta $PORT, scrive un numero in heartbeat.txt ogni 15s e si auto-richiama ogni 10 min.
Il vero keep-alive lo fa però il ping esterno (UptimeRobot).
