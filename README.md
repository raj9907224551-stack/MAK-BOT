# BOT RUN V2

Professional Telegram-based Python bot hosting platform.

## Included in V2

- Paid hosting plans with admin-editable name, duration, price, enable/disable state and bot limit.
- Wallet balance for every user.
- Admin wallet management.
- FamPay Gateway settings in the Admin Panel: UPI ID + API token can be changed without editing code.
- QR payment flow using the documented FamPay create-order / verify-order endpoints.
- Automatic payment verification and subscription activation.
- Clean black-on-white QR generation with a large quiet zone and high error correction.
- Subscription expiry handling and approximately 48-hour expiry alerts to the user and admins.
- Automatic stopping of hosted bots after subscription expiry.
- Master startup recovery: only bots marked as intentionally running before the master restart are recovered.
- Direct `main.py` upload.
- Complete `.zip` upload with `main.py` anywhere in the project (including a single top-level folder).
- Per-bot virtual environments and optional `requirements.txt`.
- Start / Stop / Restart / Logs / Backup / Delete.
- Persistent projects and metadata under `data/` so code updates do not erase hosted data.
- User isolation: users see and control only their own hosted bots; admins can manage all bots.

## Files

- `main.py` - master Telegram bot and payment/subscription UI.
- `runner.py` - persistent child-bot process manager.
- `config.py` - only master Telegram token and admin IDs.
- `data/settings.json` - persistent payment settings and plans.
- `data/users.json` - wallets and subscriptions.
- `data/payments.json` - payment/order records.
- `data/state.json` - pending verification state.
- `data/projects/` - hosted bot projects.
- `data/backups/` - bot backups.
- `data/logs/` - bot logs.

## Install

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Edit `config.py` with the master bot token and Telegram admin ID, then run:

```bash
python main.py
```

## First payment setup

Open **Admin Panel -> Payment Gateway** and set:

1. UPI ID
2. FamPay API Token
3. Gateway ON

The credentials are stored in persistent `data/settings.json`; they are not hard-coded into `main.py` and the API token is never displayed to normal users.

The payment integration follows the endpoint structure shown in the supplied FamPay Gateway documentation:

- `GET /api/create_order.php?amount=...&api_key=...`
- `GET /api/verify.php?order_id=...&api_key=...`

The create response is expected to contain `data.order_id`, `data.qr_url`, `data.upi_id`, `data.amount` and `data.expires_at`; the verify response is expected to return `status=success` with payment data such as UTR.

## Upload formats

Users with an active subscription can send either:

```text
MyBot.zip
├── main.py
├── requirements.txt
└── other files...
```

or directly:

```text
main.py
```

A direct `main.py` bot can run with packages already available to its virtual environment. For additional packages, upload the complete ZIP with `requirements.txt`.

## Persistent recovery

Do not delete the `data/` directory during a BOT RUN code update. The master bot records each bot's `desired_running` state. After the master comes back online, it waits briefly and starts only those bots that were running intentionally before the master went offline, provided the owner's subscription is still active.

## Backup

The per-bot **BACKUP** action creates a ZIP in `data/backups/`. For a complete disaster-recovery backup, copy the entire `data/` directory before replacing BOT RUN code.

## Security

Uploaded Python projects execute with the permissions of the BOT RUN process. For a public paid hosting service, production deployment should use stronger isolation (Docker/containers, non-root runner, CPU/RAM/disk quotas, network controls and filesystem restrictions). Do not run untrusted projects with root privileges.
