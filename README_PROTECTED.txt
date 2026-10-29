RG Protected Bot Build
======================

Files:
- main.py       -> small runtime loader only
- main.enc      -> encrypted/compressed application code
- bot_config.json -> only BOT_TOKEN and BOT_ADMIN_IDS are intended to be edited
- shop_data.db  -> existing bot data, intentionally left unencrypted
- requirements.txt -> original dependencies
- existing TXT documentation -> preserved

Run:
    pip install -r requirements.txt
    python main.py

Change bot token/admin:
Edit only bot_config.json and run main.py again.

Security note:
The application code is encrypted at rest and decrypted in memory at startup.
No Python source for the bot logic is shipped. This is source protection/obfuscation,
not an absolute guarantee against reverse engineering because a running program
must ultimately decrypt its code in memory.

Important:
The Bot Token from the original archive was included in this build for immediate
compatibility. Because the token was shared during this conversation, rotate it
with BotFather before using the protected build in a public environment.
