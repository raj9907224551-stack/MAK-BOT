import asyncio
import base64
import datetime as dt
import hashlib
import html
import io
import json
import logging
import os
import secrets
import shutil
import tempfile
import time
from pathlib import Path

import httpx
import qrcode
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters
)

from config import BOT_TOKEN, ADMIN_IDS, DATA_DIR
from runner import Runner, DATA, UPLOADS, BACKUPS

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(name)s | %(message)s", level=logging.INFO)
log = logging.getLogger("bot_run")

if DATA_DIR:
    # runner.py reads BOT_RUN_DATA_DIR at import time; DATA_DIR is kept for config clarity.
    pass

runner = Runner()
SETTINGS = DATA / "settings.json"
USERS = DATA / "users.json"
PAYMENTS = DATA / "payments.json"
STATE = DATA / "state.json"

for p in (SETTINGS, USERS, PAYMENTS, STATE):
    p.parent.mkdir(parents=True, exist_ok=True)

DEFAULT_PLANS = {
    "1m": {"name": "1 Month", "days": 30, "price": 194.0, "enabled": True, "max_bots": 1},
    "2m": {"name": "2 Months", "days": 60, "price": 299.0, "enabled": True, "max_bots": 2},
    "3m": {"name": "3 Months", "days": 90, "price": 399.0, "enabled": True, "max_bots": 3},
}
DEFAULT_SETTINGS = {
    "upi_id": "",
    "fampay_api_token": "",
    "gateway_enabled": True,
    "currency_symbol": "₹",
    "payment_poll_seconds": 3,
    "plans": DEFAULT_PLANS,
}


def _load(path, default):
    if not path.exists():
        return json.loads(json.dumps(default))
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, type(default)) else json.loads(json.dumps(default))
    except Exception:
        return json.loads(json.dumps(default))


def _save(path, value):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except Exception:
        pass
    tmp.replace(path)
    if path.name == "settings.json":
        try:
            os.chmod(path, 0o600)
        except Exception:
            pass


settings = _load(SETTINGS, DEFAULT_SETTINGS)
users = _load(USERS, {})
payments = _load(PAYMENTS, {})
state = _load(STATE, {"pending": {}})


def is_admin(uid):
    return int(uid) in {int(x) for x in ADMIN_IDS}


def user_record(uid):
    uid = str(int(uid))
    rec = users.setdefault(uid, {
        "id": int(uid), "wallet": 0.0, "subscription": None,
        "created_at": int(time.time()), "last_seen": int(time.time()),
    })
    rec["last_seen"] = int(time.time())
    _save(USERS, users)
    return rec


def active_subscription(uid):
    rec = users.get(str(int(uid)))
    sub = rec.get("subscription") if rec else None
    return bool(sub and float(sub.get("expires_at", 0)) > time.time())


def subscription(uid):
    rec = users.get(str(int(uid)))
    return rec.get("subscription") if rec else None


def fmt_expiry(ts):
    if not ts:
        return "Not active"
    return dt.datetime.fromtimestamp(float(ts)).strftime("%d %b %Y, %I:%M %p")


def money(v):
    return f"{settings.get('currency_symbol', '₹')}{float(v):.2f}"


def esc(s):
    return html.escape(str(s), quote=False)


def user_menu(uid):
    rec = user_record(uid)
    rows = [
        [InlineKeyboardButton("💎 Hosting Plans", callback_data="plans")],
        [InlineKeyboardButton("🤖 My Bots", callback_data="my_bots"), InlineKeyboardButton("📅 Subscription", callback_data="subscription")],
        [InlineKeyboardButton(f"💰 Wallet · {money(rec.get('wallet', 0))}", callback_data="wallet")],
        [InlineKeyboardButton("💳 Payment Status", callback_data="payment_status")],
        [InlineKeyboardButton("🆘 Support", callback_data="support")],
    ]
    if is_admin(uid):
        rows.append([InlineKeyboardButton("🛠 Admin Panel", callback_data="admin")])
    return InlineKeyboardMarkup(rows)


def admin_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Dashboard", callback_data="admin_dashboard")],
        [InlineKeyboardButton("👥 Users & Bots", callback_data="admin_users"), InlineKeyboardButton("🤖 All Bots", callback_data="admin_bots")],
        [InlineKeyboardButton("💎 Plans & Prices", callback_data="admin_plans")],
        [InlineKeyboardButton("💳 Payment Gateway", callback_data="admin_gateway")],
        [InlineKeyboardButton("💰 Wallet Management", callback_data="admin_wallet")],
        [InlineKeyboardButton("💾 Backups", callback_data="admin_backups")],
        [InlineKeyboardButton("🖥 Server Status", callback_data="admin_status")],
        [InlineKeyboardButton("⬅️ User Panel", callback_data="home")],
    ])


def back_home():
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="home")]])


def plans_markup():
    rows=[]
    for pid,p in settings.get("plans",{}).items():
        if p.get("enabled", True):
            rows.append([InlineKeyboardButton(f"💎 {p.get('name', pid)} · {money(p.get('price',0))}", callback_data=f"plan:{pid}")])
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data="home")])
    return InlineKeyboardMarkup(rows)


def gateway_markup():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📷 Pay via QR", callback_data="pay_qr")],
        [InlineKeyboardButton("💰 Pay via Wallet", callback_data="pay_wallet")],
        [InlineKeyboardButton("⬅️ Back", callback_data="plans")],
    ])


def admin_plans_markup():
    rows=[]
    for pid,p in settings.get("plans",{}).items():
        status = "ON" if p.get("enabled", True) else "OFF"
        rows.append([InlineKeyboardButton(f"{p.get('name',pid)} · {money(p.get('price',0))} · {status}", callback_data=f"admin_plan:{pid}")])
    rows.append([InlineKeyboardButton("➕ Add Plan", callback_data="admin_plan_add")])
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data="admin")])
    return InlineKeyboardMarkup(rows)


def admin_gateway_markup():
    enabled = "ON" if settings.get("gateway_enabled") else "OFF"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ Set UPI ID", callback_data="gateway_set_upi")],
        [InlineKeyboardButton("🔐 Set API Token", callback_data="gateway_set_token")],
        [InlineKeyboardButton(f"🔌 Gateway: {enabled}", callback_data="gateway_toggle")],
        [InlineKeyboardButton("🧪 Check Connection", callback_data="gateway_check")],
        [InlineKeyboardButton("⬅️ Back", callback_data="admin")],
    ])


def payment_status_markup():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Refresh", callback_data="payment_status")],
        [InlineKeyboardButton("⬅️ Back", callback_data="home")],
    ])


def make_qr(payload: str):
    """Create a clean black-on-white fallback UPI QR."""
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_H, box_size=12, border=5)
    qr.add_data(payload)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
    bio = io.BytesIO()
    img.save(bio, "PNG", optimize=True)
    bio.seek(0)
    bio.name = "payment_qr.png"
    return bio


async def gateway_qr_image(qr_url: str, fallback_payload: str):
    """Prefer the gateway-generated order QR so verification stays tied to the order.
    If the gateway image cannot be downloaded, fall back to a clean UPI QR.
    """
    if qr_url:
        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
                response = await client.get(qr_url)
                response.raise_for_status()
                content_type = str(response.headers.get("content-type", "")).lower()
                if "image" in content_type and response.content:
                    bio = io.BytesIO(response.content)
                    bio.seek(0)
                    bio.name = "payment_qr.png"
                    return bio
        except Exception as exc:
            log.warning("Gateway QR download failed; using clean UPI fallback: %s", exc)
    return make_qr(fallback_payload)


def _gateway_token():
    return str(settings.get("fampay_api_token") or "").strip()


def _upi():
    return str(settings.get("upi_id") or "").strip()


def _gateway_url(path, **params):
    base = "https://fampaygateway.site" + path
    params["api_key"] = _gateway_token()
    return base, params


async def gateway_create_order(amount):
    if not settings.get("gateway_enabled"):
        raise RuntimeError("Payment gateway is currently disabled by the administrator.")
    if not _gateway_token() or not _upi():
        raise RuntimeError("Payment gateway is not configured yet. Please contact the administrator.")
    url, params = _gateway_url("/api/create_order.php", amount=f"{float(amount):.2f}")
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        r = await client.get(url, params=params)
        r.raise_for_status()
        data = r.json()
    if str(data.get("status", "")).lower() != "success":
        raise RuntimeError(str(data.get("message") or "Gateway rejected the order."))
    d = data.get("data") or {}
    order_id = str(d.get("order_id") or "").strip()
    qr_url = str(d.get("qr_url") or "").strip()
    upi_id = str(d.get("upi_id") or _upi()).strip()
    expires_at = str(d.get("expires_at") or "")
    if not order_id:
        raise RuntimeError("Gateway did not return an order ID.")
    return {"order_id": order_id, "qr_url": qr_url, "upi_id": upi_id, "amount": float(amount), "expires_at": expires_at}


async def gateway_verify(order_id):
    if not _gateway_token():
        return {"status": "error", "message": "Gateway token is not configured."}
    url, params = _gateway_url("/api/verify.php", order_id=order_id)
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        r = await client.get(url, params=params)
        r.raise_for_status()
        data = r.json()
    return data


def _status_from_verify(data):
    top = str(data.get("status") or "").lower()
    d = data.get("data") or {}
    if top != "success":
        return "unknown", d
    # The gateway screenshot shows successful verification as status=success.
    # Accept common status fields so minor gateway response changes do not break us.
    state_value = str(d.get("status") or d.get("payment_status") or d.get("result_status") or "credited").lower()
    if state_value in {"credited", "success", "paid", "completed", "successful"}:
        return "credited", d
    if state_value in {"pending", "processing", "created"}:
        return "pending", d
    if state_value in {"expired", "failed", "cancelled", "canceled", "duplicate"}:
        return state_value, d
    return "credited", d


def create_pending(uid, plan_id, order):
    oid = order["order_id"]
    payments[oid] = {
        "order_id": oid, "user_id": int(uid), "plan_id": plan_id,
        "amount": float(order["amount"]), "status": "pending",
        "created_at": time.time(), "expires_at": order.get("expires_at", ""),
    }
    state.setdefault("pending", {})[oid] = True
    _save(PAYMENTS, payments); _save(STATE, state)


def activate_subscription(uid, plan_id, payment_method, order_id=None):
    p = settings.get("plans", {}).get(plan_id)
    if not p or not p.get("enabled", True):
        raise RuntimeError("Selected hosting plan is no longer available.")
    rec = user_record(uid)
    now = time.time()
    old = rec.get("subscription") or {}
    base = max(now, float(old.get("expires_at", 0)))
    expires = base + int(p.get("days", 30)) * 86400
    rec["subscription"] = {
        "plan_id": plan_id, "plan_name": p.get("name", plan_id),
        "days": int(p.get("days", 30)), "price": float(p.get("price", 0)),
        "started_at": now, "expires_at": expires,
        "payment_method": payment_method, "order_id": order_id,
    }
    _save(USERS, users)
    return rec["subscription"]


def charge_wallet(uid, amount):
    rec = user_record(uid)
    if float(rec.get("wallet", 0)) + 1e-9 < float(amount):
        return False
    rec["wallet"] = round(float(rec.get("wallet", 0)) - float(amount), 2)
    _save(USERS, users)
    return True


def set_wallet(uid, amount):
    rec=user_record(uid)
    rec["wallet"] = round(max(0.0, float(amount)),2)
    _save(USERS,users)
    return rec["wallet"]


def bot_count(uid):
    return len(runner.list_bots(uid))


def plan_allows_bot(uid):
    sub=subscription(uid)
    if not sub or float(sub.get("expires_at",0)) <= time.time():
        return False, "Your hosting subscription is inactive or expired."
    p=settings.get("plans",{}).get(sub.get("plan_id"),{})
    limit=int(p.get("max_bots",1))
    if bot_count(uid) >= limit:
        return False, f"Your current plan allows up to {limit} hosted bot(s)."
    return True, ""


async def send_home(target, text=None):
    uid=target.from_user.id if hasattr(target,"from_user") else target.effective_user.id
    text=text or (
        "🚀 <b>BOT RUN</b>\n\n"
        "<b>Premium Python Bot Hosting</b>\n"
        "Run your Telegram Python bots with persistent storage, process controls, backups and automatic recovery.\n\n"
        f"💰 <b>Wallet:</b> {money(user_record(uid).get('wallet',0))}\n"
        f"📅 <b>Hosting:</b> {'Active' if active_subscription(uid) else 'Inactive'}"
    )
    if hasattr(target,"edit_message_text"):
        await target.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=user_menu(uid))
    else:
        await target.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=user_menu(uid))


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user:
        return
    user_record(update.effective_user.id)
    await update.message.reply_text(
        "🚀 <b>BOT RUN</b>\n\n<b>Premium Python Bot Hosting</b>\n\n"
        "Host your Python Telegram bots with persistent projects, automatic recovery, backups and professional management.\n\n"
        "Choose an option below to get started.",
        parse_mode=ParseMode.HTML, reply_markup=user_menu(update.effective_user.id)
    )


async def admin_command(update, context):
    if not update.effective_user or not is_admin(update.effective_user.id):
        return
    await update.message.reply_text("🛠 <b>BOT RUN ADMIN PANEL</b>\n\nSelect a management section.", parse_mode=ParseMode.HTML, reply_markup=admin_menu())


async def upload_command(update, context):
    uid=update.effective_user.id
    if not active_subscription(uid):
        await update.message.reply_text("🔒 An active hosting subscription is required before uploading a bot.", reply_markup=plans_markup())
        return
    ok,msg=plan_allows_bot(uid)
    if not ok:
        await update.message.reply_text("⚠️ "+msg)
        return
    context.user_data["awaiting_upload"] = True
    await update.message.reply_text(
        "📤 <b>Upload Your Bot</b>\n\n"
        "Send either:\n"
        "• A complete <code>.zip</code> project containing <code>main.py</code>\n"
        "• A direct <code>main.py</code> file\n\n"
        "A ZIP may also include <code>requirements.txt</code> and other project files.",
        parse_mode=ParseMode.HTML
    )


async def document_handler(update, context):
    user=update.effective_user
    if not user or not update.message or not update.message.document:
        return
    uid=user.id
    if not context.user_data.get("awaiting_upload"):
        return
    if not active_subscription(uid):
        context.user_data["awaiting_upload"]=False
        await update.message.reply_text("🔒 Your hosting subscription is inactive. Please renew before uploading.")
        return
    ok,msg=plan_allows_bot(uid)
    if not ok:
        context.user_data["awaiting_upload"]=False
        await update.message.reply_text("⚠️ "+msg)
        return
    doc=update.message.document
    filename=str(doc.file_name or "")
    lower=filename.lower()
    if not (lower.endswith(".zip") or lower == "main.py"):
        await update.message.reply_text("❌ Only a complete <code>.zip</code> project or direct <code>main.py</code> is supported.", parse_mode=ParseMode.HTML)
        return
    safe=Path(filename).name
    target=UPLOADS / f"{int(time.time())}_{secrets.token_hex(4)}_{safe}"
    tg_file=await context.bot.get_file(doc.file_id)
    await tg_file.download_to_drive(str(target))
    try:
        if lower.endswith(".zip"):
            bot_id=runner.import_zip(target, uid, display_name=Path(safe).stem)
        else:
            bot_id=runner.import_main_py(target, uid, display_name="main.py")
    except Exception as exc:
        log.exception("Bot upload failed")
        await update.message.reply_text(f"❌ <b>Upload failed</b>\n\n<code>{esc(exc)}</code>", parse_mode=ParseMode.HTML)
        return
    finally:
        try: target.unlink(missing_ok=True)
        except Exception: pass
    context.user_data["awaiting_upload"]=False
    b=runner.meta[bot_id]
    await update.message.reply_text(
        f"✅ <b>Bot Uploaded Successfully</b>\n\n🤖 <b>Name:</b> {esc(b['name'])}\n🆔 <b>Bot ID:</b> <code>{esc(bot_id)}</code>\n📦 <b>Source:</b> {esc(b['source_type'])}\n\nYour bot is ready. Press <b>START</b> to run it.",
        parse_mode=ParseMode.HTML, reply_markup=runner.bot_markup(bot_id)
    )


async def text_handler(update, context):
    uid=update.effective_user.id if update.effective_user else 0
    action=context.user_data.get("admin_action")
    if not is_admin(uid) or not action or not update.message or not update.message.text:
        return
    value=update.message.text.strip()
    context.user_data.pop("admin_action",None)
    try:
        if action=="upi":
            settings["upi_id"]=value; _save(SETTINGS,settings)
            await update.message.reply_text("✅ <b>UPI ID updated successfully.</b>", parse_mode=ParseMode.HTML, reply_markup=admin_gateway_markup())
        elif action=="token":
            settings["fampay_api_token"]=value; _save(SETTINGS,settings)
            await update.message.reply_text("✅ <b>FamPay API token updated securely.</b>\n\nThe token is stored only in the persistent settings file and is never shown to users.", parse_mode=ParseMode.HTML, reply_markup=admin_gateway_markup())
        elif action.startswith("wallet:"):
            target=int(action.split(":",1)[1]); amount=float(value)
            set_wallet(target,amount)
            await update.message.reply_text(f"✅ Wallet balance set for <code>{target}</code>.\n💰 New balance: <b>{money(amount)}</b>",parse_mode=ParseMode.HTML,reply_markup=admin_menu())
        elif action.startswith("walletadd:"):
            target=int(action.split(":",1)[1]); amount=float(value)
            rec=user_record(target); rec["wallet"]=round(float(rec.get("wallet",0))+max(0,amount),2); _save(USERS,users)
            await update.message.reply_text(f"✅ Added <b>{money(amount)}</b> to <code>{target}</code>.\n💰 New balance: <b>{money(rec['wallet'])}</b>",parse_mode=ParseMode.HTML,reply_markup=admin_menu())
        elif action.startswith("walletrem:"):
            target=int(action.split(":",1)[1]); amount=float(value)
            rec=user_record(target); rec["wallet"]=round(max(0,float(rec.get("wallet",0))-max(0,amount)),2); _save(USERS,users)
            await update.message.reply_text(f"✅ Removed <b>{money(amount)}</b> from <code>{target}</code>.\n💰 New balance: <b>{money(rec['wallet'])}</b>",parse_mode=ParseMode.HTML,reply_markup=admin_menu())
        elif action.startswith("planprice:"):
            pid=action.split(":",1)[1]; settings["plans"][pid]["price"]=float(value); _save(SETTINGS,settings)
            await update.message.reply_text("✅ Plan price updated.",reply_markup=admin_plans_markup())
        elif action.startswith("plandays:"):
            pid=action.split(":",1)[1]; settings["plans"][pid]["days"]=int(float(value)); _save(SETTINGS,settings)
            await update.message.reply_text("✅ Plan duration updated.",reply_markup=admin_plans_markup())
        elif action.startswith("planname:"):
            pid=action.split(":",1)[1]; settings["plans"][pid]["name"]=value; _save(SETTINGS,settings)
            await update.message.reply_text("✅ Plan name updated.",reply_markup=admin_plans_markup())
        elif action.startswith("planlimit:"):
            pid=action.split(":",1)[1]; settings["plans"][pid]["max_bots"]=max(1,int(float(value))); _save(SETTINGS,settings)
            await update.message.reply_text("✅ Bot limit updated.",reply_markup=admin_plans_markup())
        elif action=="addplan":
            parts=[x.strip() for x in value.split("|")]
            if len(parts)<4: raise ValueError("Use: name | days | price | max_bots")
            pid="plan_"+secrets.token_hex(3)
            settings["plans"][pid]={"name":parts[0],"days":int(parts[1]),"price":float(parts[2]),"enabled":True,"max_bots":max(1,int(parts[3]))}
            _save(SETTINGS,settings)
            await update.message.reply_text("✅ New hosting plan created.",reply_markup=admin_plans_markup())
    except Exception as exc:
        await update.message.reply_text(f"❌ Invalid value.\n<code>{esc(exc)}</code>",parse_mode=ParseMode.HTML,reply_markup=admin_menu())


async def payment_poll_job(context):
    pending=list(state.get("pending",{}).keys())
    for oid in pending:
        p=payments.get(oid)
        if not p or p.get("status")!="pending":
            state.get("pending",{}).pop(oid,None); continue
        try:
            result=await gateway_verify(oid)
            status,data=_status_from_verify(result)
            if status=="credited":
                uid=int(p["user_id"]); plan_id=p["plan_id"]
                if p.get("status")=="pending":
                    sub=activate_subscription(uid,plan_id,"QR",oid)
                    p["status"]="credited"; p["verified_at"]=time.time(); p["utr"]=data.get("utr")
                    state["pending"].pop(oid,None)
                    _save(PAYMENTS,payments); _save(STATE,state)
                    try:
                        await context.bot.send_message(uid,
                            "✅ <b>Payment Successful</b>\n\n"
                            f"💎 <b>Plan:</b> {esc(sub['plan_name'])}\n"
                            f"💰 <b>Amount:</b> {money(p['amount'])}\n"
                            "🔐 <b>Payment Method:</b> QR\n"
                            f"🧾 <b>Order ID:</b> <code>{esc(oid)}</code>\n"
                            f"📅 <b>Expires:</b> <code>{fmt_expiry(sub['expires_at'])}</code>\n\n"
                            "Your hosting subscription is now active.",parse_mode=ParseMode.HTML,reply_markup=user_menu(uid))
                    except Exception: pass
                    for aid in ADMIN_IDS:
                        try:
                            await context.bot.send_message(int(aid),f"💳 <b>Hosting Payment Credited</b>\n\n👤 User: <code>{uid}</code>\n💰 Amount: <b>{money(p['amount'])}</b>\n💎 Plan: <b>{esc(settings['plans'][plan_id]['name'])}</b>\n🧾 Order: <code>{esc(oid)}</code>",parse_mode=ParseMode.HTML)
                        except Exception: pass
            elif status in {"expired","failed","cancelled","canceled","duplicate"}:
                p["status"]=status; state["pending"].pop(oid,None); _save(PAYMENTS,payments); _save(STATE,state)
        except Exception as exc:
            log.warning("Payment verification failed for %s: %s",oid,exc)
    _save(STATE,state)


async def expiry_job(context):
    now=time.time()
    for uid,rec in list(users.items()):
        sub=rec.get("subscription") or {}
        exp=float(sub.get("expires_at",0))
        if not exp: continue
        if exp > now and not rec.get("expiry_alert_sent") and exp-now <= 48*3600:
            rec["expiry_alert_sent"]=True
            _save(USERS,users)
            msg=("⚠️ <b>Hosting Expiry Alert</b>\n\n"
                 f"Your <b>{esc(sub.get('plan_name','Hosting'))}</b> subscription expires in approximately <b>2 days</b>.\n"
                 f"📅 <b>Expiry:</b> {fmt_expiry(exp)}\n\nRenew now to keep your hosted bots running.")
            try: await context.bot.send_message(int(uid),msg,parse_mode=ParseMode.HTML,reply_markup=plans_markup())
            except Exception: pass
            for aid in ADMIN_IDS:
                try: await context.bot.send_message(int(aid),f"⚠️ <b>Hosting Expiry Alert</b>\n\nUser <code>{uid}</code> expires in approximately 2 days.\nPlan: <b>{esc(sub.get('plan_name','Hosting'))}</b>\nExpiry: <code>{fmt_expiry(exp)}</code>",parse_mode=ParseMode.HTML)
                except Exception: pass
        if exp <= now and sub.get("active",True):
            sub["active"]=False; rec["expiry_alert_sent"]=False; _save(USERS,users)
            for b in runner.list_bots(int(uid)):
                if b.get("status")=="running":
                    try: runner.stop(b["id"],mark_desired=False)
                    except Exception: pass


async def recover_job(app):
    await asyncio.sleep(1.5)
    results=runner.recover_after_master_start(active_subscription)
    if results:
        log.info("Startup recovery: %s",results)


async def buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q=update.callback_query
    await q.answer()
    uid=q.from_user.id
    data=q.data or ""
    if not is_admin(uid) and data.startswith("admin") or (data.startswith("gateway_") and not is_admin(uid)):
        await q.edit_message_text("⛔ Unauthorized.")
        return
    try:
        if data=="home": await send_home(q); return
        if data=="admin": await q.edit_message_text("🛠 <b>BOT RUN ADMIN PANEL</b>\n\nSelect a management section.",parse_mode=ParseMode.HTML,reply_markup=admin_menu()); return
        if data=="plans": await q.edit_message_text("💎 <b>Hosting Plans</b>\n\nChoose a plan to continue.",parse_mode=ParseMode.HTML,reply_markup=plans_markup()); return
        if data.startswith("plan:"):
            pid=data.split(":",1)[1]; p=settings.get("plans",{}).get(pid)
            if not p or not p.get("enabled",True): raise ValueError("Plan is unavailable.")
            context.user_data["selected_plan"]=pid
            await q.edit_message_text(f"💎 <b>{esc(p['name'])}</b>\n\n⏳ Duration: <b>{p['days']} days</b>\n💰 Price: <b>{money(p['price'])}</b>\n\nChoose your payment method.",parse_mode=ParseMode.HTML,reply_markup=gateway_markup()); return
        if data=="wallet":
            r=user_record(uid); await q.edit_message_text(f"💰 <b>Your Wallet</b>\n\nCurrent balance: <b>{money(r.get('wallet',0))}</b>\n\nWallet balance can be used for hosting plan payments.",parse_mode=ParseMode.HTML,reply_markup=back_home()); return
        if data=="subscription":
            s=subscription(uid); text="📅 <b>Your Subscription</b>\n\n"
            if not s or float(s.get('expires_at',0))<=time.time(): text+="Status: <b>Inactive</b>\n\nChoose a hosting plan to activate your service."
            else: text+=f"Status: <b>Active</b>\n💎 Plan: <b>{esc(s['plan_name'])}</b>\n⏳ Duration: <b>{s['days']} days</b>\n💳 Method: <b>{esc(s.get('payment_method',''))}</b>\n📅 Expires: <b>{fmt_expiry(s['expires_at'])}</b>"
            await q.edit_message_text(text,parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Renew",callback_data="plans")],[InlineKeyboardButton("⬅️ Back",callback_data="home")]])); return
        if data=="my_bots":
            bots=runner.list_bots(uid); rows=[]
            for b in bots:
                status="🟢" if b.get("status")=="running" else "🔴"
                rows.append([InlineKeyboardButton(f"{status} {b['name']}",callback_data=f"botview:{b['id']}")])
            rows.append([InlineKeyboardButton("📤 Upload Bot",callback_data="upload")])
            rows.append([InlineKeyboardButton("⬅️ Back",callback_data="home")])
            text="🤖 <b>My Bots</b>\n\n"+("Select a bot to manage it." if bots else "No bots uploaded yet.")
            await q.edit_message_text(text,parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup(rows)); return
        if data=="upload":
            if not active_subscription(uid): await q.edit_message_text("🔒 <b>Active hosting required</b>\n\nChoose a plan first.",parse_mode=ParseMode.HTML,reply_markup=plans_markup()); return
            ok,msg=plan_allows_bot(uid)
            if not ok: await q.edit_message_text("⚠️ "+msg,reply_markup=back_home()); return
            context.user_data["awaiting_upload"]=True
            await q.edit_message_text("📤 <b>Upload Your Bot</b>\n\nSend a complete <code>.zip</code> containing <code>main.py</code>, or send <code>main.py</code> directly.",parse_mode=ParseMode.HTML,reply_markup=back_home()); return
        if data.startswith("botview:"):
            bid=data.split(":",1)[1]; b=runner.meta.get(bid)
            if not b or int(b.get("owner_id",0))!=uid: raise ValueError("Bot not found.")
            await q.edit_message_text(f"🤖 <b>{esc(b['name'])}</b>\n\n🆔 <code>{esc(bid)}</code>\n📌 Status: <b>{esc(b['status'])}</b>\n📦 Source: <b>{esc(b.get('source_type','zip'))}</b>",parse_mode=ParseMode.HTML,reply_markup=runner.bot_markup(bid)); return
        if data.startswith("run:") or data.startswith("stop:") or data.startswith("restart:") or data.startswith("logs:") or data.startswith("backup:") or data.startswith("delete:"):
            action,bid=data.split(":",1); b=runner.meta.get(bid)
            if not b or (not is_admin(uid) and int(b.get("owner_id",0))!=uid): raise ValueError("Bot not found.")
            if action=="run":
                if not active_subscription(uid): raise ValueError("Your hosting subscription is inactive.")
                result=runner.start(bid)
            elif action=="stop": result=runner.stop(bid)
            elif action=="restart":
                if not active_subscription(uid): raise ValueError("Your hosting subscription is inactive.")
                result=runner.restart(bid)
            elif action=="logs": result=runner.logs(bid)
            elif action=="backup": result=f"💾 Backup created:\n<code>{esc(runner.backup(bid))}</code>"
            else:
                result=runner.delete(bid)
                await q.edit_message_text(result,reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ My Bots",callback_data="my_bots")]])); return
            await q.edit_message_text(result,parse_mode=ParseMode.MARKDOWN if action=="logs" else ParseMode.HTML,reply_markup=runner.bot_markup(bid)); return
        if data=="pay_wallet":
            pid=context.user_data.get("selected_plan"); p=settings.get("plans",{}).get(pid or "")
            if not p: raise ValueError("Please select a plan again.")
            if not charge_wallet(uid,p["price"]):
                await q.edit_message_text(f"💰 <b>Insufficient Wallet Balance</b>\n\nRequired: <b>{money(p['price'])}</b>\nCurrent balance: <b>{money(user_record(uid).get('wallet',0))}</b>\n\nPlease choose QR payment instead.",parse_mode=ParseMode.HTML,reply_markup=gateway_markup()); return
            sub=activate_subscription(uid,pid,"Wallet")
            await q.edit_message_text(f"✅ <b>Hosting Activated</b>\n\n💎 Plan: <b>{esc(p['name'])}</b>\n💰 Paid: <b>{money(p['price'])}</b>\n🔐 Method: <b>Wallet</b>\n📅 Expires: <b>{fmt_expiry(sub['expires_at'])}</b>\n\nYou can now upload your bot.",parse_mode=ParseMode.HTML,reply_markup=user_menu(uid)); return
        if data=="pay_qr":
            pid=context.user_data.get("selected_plan"); p=settings.get("plans",{}).get(pid or "")
            if not p: raise ValueError("Please select a plan again.")
            await q.edit_message_text("⏳ <b>Creating secure payment order...</b>",parse_mode=ParseMode.HTML)
            order=await gateway_create_order(p["price"]); create_pending(uid,pid,order)
            # Build a clean black-on-white UPI QR. If the gateway supplies a UPI ID,
            # use it; the order ID is included in the transaction note for tracking.
            upi=order.get("upi_id") or _upi()
            payload=f"upi://pay?pa={upi}&pn=BOT%20RUN&am={float(p['price']):.2f}&cu=INR&tn={order['order_id']}"
            qr=await gateway_qr_image(order.get("qr_url", ""),payload)
            await q.message.reply_photo(photo=InputFile(qr),caption=(
                "📷 <b>Complete Your Payment</b>\n\n"
                f"💎 <b>Plan:</b> {esc(p['name'])}\n"
                f"💰 <b>Amount:</b> {money(p['price'])}\n"
                f"🧾 <b>Order ID:</b> <code>{esc(order['order_id'])}</code>\n"
                f"📅 <b>Expires:</b> <code>{esc(order.get('expires_at') or 'Gateway expiry')}</code>\n\n"
                "Scan the QR and complete the payment. BOT RUN will verify it automatically."
            ),parse_mode=ParseMode.HTML)
            await q.edit_message_text("⏳ <b>Payment order created.</b>\n\nThe QR has been sent above. You do not need to send a screenshot.\n\nUse <b>Payment Status</b> to check the current order.",parse_mode=ParseMode.HTML,reply_markup=payment_status_markup()); return
        if data=="payment_status":
            mine=[p for p in payments.values() if int(p.get("user_id",0))==uid]
            mine=sorted(mine,key=lambda x:x.get("created_at",0),reverse=True)
            if not mine: text="💳 <b>Payment Status</b>\n\nNo payment orders found."
            else:
                p=mine[0]; text=f"💳 <b>Latest Payment</b>\n\n🧾 Order: <code>{esc(p['order_id'])}</code>\n💰 Amount: <b>{money(p['amount'])}</b>\n📌 Status: <b>{esc(p['status']).upper()}</b>"
            await q.edit_message_text(text,parse_mode=ParseMode.HTML,reply_markup=payment_status_markup()); return
        if data=="support": await q.edit_message_text("🆘 <b>Support</b>\n\nPlease contact the service administrator for payment or hosting assistance.",parse_mode=ParseMode.HTML,reply_markup=back_home()); return
        if data=="admin_dashboard":
            bots=runner.list_bots(); running=sum(1 for b in bots if b.get('status')=='running'); active=sum(1 for r in users.values() if active_subscription(int(r.get('id',0))))
            text=f"📊 <b>BOT RUN DASHBOARD</b>\n\n👥 Users: <b>{len(users)}</b>\n🤖 Hosted Bots: <b>{len(bots)}</b>\n🟢 Running Bots: <b>{running}</b>\n📅 Active Subscriptions: <b>{active}</b>\n💳 Payment Orders: <b>{len(payments)}</b>\n💾 Backups: <b>{len(list(BACKUPS.glob('*.zip')))}</b>"
            await q.edit_message_text(text,parse_mode=ParseMode.HTML,reply_markup=admin_menu()); return
        if data=="admin_gateway":
            token_state="Configured" if _gateway_token() else "Not configured"; upi_state=_upi() or "Not configured"; enabled="ON" if settings.get('gateway_enabled') else "OFF"
            text=f"💳 <b>Payment Gateway Settings</b>\n\n🔌 Status: <b>{enabled}</b>\n🏦 UPI ID: <code>{esc(upi_state)}</code>\n🔐 API Token: <b>{token_state}</b>\n\nCredentials can be changed here without editing the main code."
            await q.edit_message_text(text,parse_mode=ParseMode.HTML,reply_markup=admin_gateway_markup()); return
        if data=="gateway_set_upi": context.user_data["admin_action"]="upi"; await q.edit_message_text("🏦 <b>Set UPI ID</b>\n\nSend the new UPI ID.",parse_mode=ParseMode.HTML,reply_markup=back_home()); return
        if data=="gateway_set_token": context.user_data["admin_action"]="token"; await q.edit_message_text("🔐 <b>Set FamPay API Token</b>\n\nSend the new API token. It will be stored securely and hidden from users.",parse_mode=ParseMode.HTML,reply_markup=back_home()); return
        if data=="gateway_toggle": settings['gateway_enabled']=not settings.get('gateway_enabled',True); _save(SETTINGS,settings); await q.edit_message_text("💳 <b>Payment Gateway Settings</b>",parse_mode=ParseMode.HTML,reply_markup=admin_gateway_markup()); return
        if data=="gateway_check":
            if not _gateway_token() or not _upi(): await q.edit_message_text("❌ <b>Gateway not configured.</b>\n\nSet both UPI ID and API token first.",parse_mode=ParseMode.HTML,reply_markup=admin_gateway_markup()); return
            try:
                # A lightweight gateway check: create a minimum test order is not desirable,
                # so we validate credentials through the gateway endpoint only when a safe order is requested.
                await q.edit_message_text("✅ <b>Gateway configuration is present.</b>\n\nUPI ID and API token are saved and ready for the next payment order.",parse_mode=ParseMode.HTML,reply_markup=admin_gateway_markup())
            except Exception as exc: await q.edit_message_text(f"❌ <code>{esc(exc)}</code>",parse_mode=ParseMode.HTML,reply_markup=admin_gateway_markup())
            return
        if data=="admin_plans": await q.edit_message_text("💎 <b>Plans & Prices</b>\n\nSelect a plan to edit its name, duration, price, bot limit or status.",parse_mode=ParseMode.HTML,reply_markup=admin_plans_markup()); return
        if data.startswith("admin_plan:"):
            pid=data.split(":",1)[1]; p=settings['plans'].get(pid)
            if not p: raise ValueError("Plan not found.")
            m=InlineKeyboardMarkup([
                [InlineKeyboardButton("✏️ Edit Name",callback_data=f"plan_name:{pid}"),InlineKeyboardButton("💰 Edit Price",callback_data=f"plan_price:{pid}")],
                [InlineKeyboardButton("📅 Edit Duration",callback_data=f"plan_days:{pid}"),InlineKeyboardButton("🤖 Bot Limit",callback_data=f"plan_limit:{pid}")],
                [InlineKeyboardButton("🔌 Toggle Plan",callback_data=f"plan_toggle:{pid}")],
                [InlineKeyboardButton("🗑 Delete Plan",callback_data=f"plan_delete:{pid}"),InlineKeyboardButton("⬅️ Back",callback_data="admin_plans")],
            ])
            await q.edit_message_text(f"💎 <b>{esc(p['name'])}</b>\n\nDuration: <b>{p['days']} days</b>\nPrice: <b>{money(p['price'])}</b>\nBot limit: <b>{p.get('max_bots',1)}</b>\nStatus: <b>{'ON' if p.get('enabled',True) else 'OFF'}</b>",parse_mode=ParseMode.HTML,reply_markup=m); return
        if data.startswith("plan_name:"): context.user_data['admin_action']='planname:'+data.split(':',1)[1]; await q.edit_message_text("✏️ Send the new plan name.",reply_markup=back_home()); return
        if data.startswith("plan_price:"): context.user_data['admin_action']='planprice:'+data.split(':',1)[1]; await q.edit_message_text("💰 Send the new plan price.",reply_markup=back_home()); return
        if data.startswith("plan_days:"): context.user_data['admin_action']='plandays:'+data.split(':',1)[1]; await q.edit_message_text("📅 Send duration in days.",reply_markup=back_home()); return
        if data.startswith("plan_limit:"): context.user_data['admin_action']='planlimit:'+data.split(':',1)[1]; await q.edit_message_text("🤖 Send the maximum number of bots.",reply_markup=back_home()); return
        if data.startswith("plan_toggle:"):
            pid=data.split(':',1)[1]; settings['plans'][pid]['enabled']=not settings['plans'][pid].get('enabled',True); _save(SETTINGS,settings); await q.edit_message_text("✅ Plan status updated.",reply_markup=admin_plans_markup()); return
        if data.startswith("plan_delete:"):
            pid=data.split(':',1)[1]; settings['plans'].pop(pid,None); _save(SETTINGS,settings); await q.edit_message_text("🗑 Plan deleted.",reply_markup=admin_plans_markup()); return
        if data=="admin_plan_add": context.user_data['admin_action']='addplan'; await q.edit_message_text("➕ <b>Add Hosting Plan</b>\n\nSend in this format:\n<code>Name | Days | Price | Max Bots</code>\n\nExample: <code>6 Months | 180 | 699 | 5</code>",parse_mode=ParseMode.HTML,reply_markup=back_home()); return
        if data=="admin_users":
            rows=[]
            for uid2,r in sorted(users.items(),key=lambda x:x[1].get('last_seen',0),reverse=True)[:30]:
                rows.append([InlineKeyboardButton(f"👤 {uid2} · {money(r.get('wallet',0))}",callback_data=f"admin_user:{uid2}")])
            rows.append([InlineKeyboardButton("⬅️ Back",callback_data="admin")])
            await q.edit_message_text("👥 <b>Users & Resellers</b>\n\nSelect a user.",parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup(rows)); return
        if data.startswith("admin_user:"):
            target=int(data.split(':',1)[1]); r=user_record(target); sub=r.get('subscription') or {}; bots=runner.list_bots(target)
            m=InlineKeyboardMarkup([
                [InlineKeyboardButton("➕ Add Balance",callback_data=f"admin_addwallet:{target}"), InlineKeyboardButton("➖ Remove Balance",callback_data=f"admin_remwallet:{target}")],
                [InlineKeyboardButton("✏️ Set Balance",callback_data=f"admin_setwallet:{target}")],
                [InlineKeyboardButton("🤖 View Bots",callback_data=f"admin_userbots:{target}")],
                [InlineKeyboardButton("⬅️ Back",callback_data="admin_users")],
            ])
            await q.edit_message_text(f"👤 <b>User Profile</b>\n\n🆔 ID: <code>{target}</code>\n💰 Wallet: <b>{money(r.get('wallet',0))}</b>\n🤖 Hosted Bots: <b>{len(bots)}</b>\n📅 Subscription: <b>{esc(sub.get('plan_name','Inactive'))}</b>\n⏳ Expires: <b>{fmt_expiry(sub.get('expires_at'))}</b>",parse_mode=ParseMode.HTML,reply_markup=m); return
        if data.startswith("admin_addwallet:"): context.user_data['admin_action']='walletadd:'+data.split(':',1)[1]; await q.edit_message_text("➕ Send the amount to add to this wallet.",reply_markup=back_home()); return
        if data.startswith("admin_remwallet:"): context.user_data['admin_action']='walletrem:'+data.split(':',1)[1]; await q.edit_message_text("➖ Send the amount to remove from this wallet.",reply_markup=back_home()); return
        if data.startswith("admin_setwallet:"): context.user_data['admin_action']='wallet:'+data.split(':',1)[1]; await q.edit_message_text("💰 Send the user's new wallet balance.",reply_markup=back_home()); return
        if data=="admin_bots":
            rows=[]
            for b in runner.list_bots()[:40]: rows.append([InlineKeyboardButton(f"{'🟢' if b.get('status')=='running' else '🔴'} {b['name']} · {b.get('owner_id')}",callback_data=f"admin_botview:{b['id']}")])
            rows.append([InlineKeyboardButton("⬅️ Back",callback_data="admin")])
            await q.edit_message_text("🤖 <b>All Hosted Bots</b>",parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup(rows)); return
        if data.startswith("admin_botview:"):
            bid=data.split(':',1)[1]; b=runner.meta.get(bid)
            if not b: raise ValueError("Bot not found.")
            await q.edit_message_text(f"🤖 <b>{esc(b['name'])}</b>\n\nOwner: <code>{b.get('owner_id')}</code>\nStatus: <b>{b.get('status')}</b>\nSource: <b>{esc(b.get('source_type','zip'))}</b>\nPID: <code>{b.get('pid') or '-'}</code>",parse_mode=ParseMode.HTML,reply_markup=runner.bot_markup(bid)); return
        if data=="admin_wallet": await q.edit_message_text("💰 <b>Wallet Management</b>\n\nOpen Users & Bots, select a user, then choose <b>Set Wallet</b> to add/remove balance by setting the new balance.",parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("👥 Open Users",callback_data="admin_users")],[InlineKeyboardButton("⬅️ Back",callback_data="admin")]])); return
        if data=="admin_backups":
            names=runner.list_backups(); text="💾 <b>Backups</b>\n\n"+("\n".join(f"• <code>{esc(x)}</code>" for x in names[:40]) if names else "No backups created yet.")
            await q.edit_message_text(text,parse_mode=ParseMode.HTML,reply_markup=admin_menu()); return
        if data=="admin_status":
            await q.edit_message_text(f"🖥 <b>Server Status</b>\n\nBOT RUN: <b>ONLINE</b>\nHosted processes: <b>{len(runner.list_bots())}</b>\nData directory: <code>{esc(str(DATA))}</code>",parse_mode=ParseMode.HTML,reply_markup=admin_menu()); return
    except Exception as exc:
        log.exception("Callback error")
        await q.edit_message_text(f"❌ <b>Action failed</b>\n\n<code>{esc(exc)}</code>",parse_mode=ParseMode.HTML,reply_markup=back_home())


async def post_init(app):
    app.job_queue.run_repeating(payment_poll_job, interval=3, first=2, name="payment_verifier")
    app.job_queue.run_repeating(expiry_job, interval=300, first=10, name="subscription_expiry")
    app.create_task(recover_job(app))


def main():
    if not BOT_TOKEN or BOT_TOKEN.startswith("PUT_YOUR_"):
        raise RuntimeError("BOT_TOKEN is missing in config.py")
    app=Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start",start))
    app.add_handler(CommandHandler("admin",admin_command))
    app.add_handler(CommandHandler("upload",upload_command))
    app.add_handler(MessageHandler(filters.Document.ALL,document_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,text_handler))
    app.add_handler(CallbackQueryHandler(buttons))
    log.info("BOT RUN V2 started")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__=="__main__":
    main()
