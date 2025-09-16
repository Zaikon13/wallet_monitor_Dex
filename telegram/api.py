import os, json, time, logging, sys
from telegram.formatters import format_holdings
from core.holdings import get_wallet_snapshot

import requests

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
WALLET_ADDRESS = os.getenv("WALLET_ADDRESS")

OFFSET_PATH = "/app/data/telegram_offset.json"

def send_telegram(msg: str):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        payload = {
            "chat_id": CHAT_ID,
            "text": msg,
            "parse_mode": "MarkdownV2",
            "disable_web_page_preview": True,
        }
        r = requests.post(url, json=payload, timeout=10)
        if r.status_code != 200:
            logging.warning("Telegram send failed %s: %s", r.status_code, r.text)
    except Exception as e:
        logging.warning("Telegram send error: %s", e)

def _load_offset():
    try:
        with open(OFFSET_PATH, "r") as f:
            data = json.load(f)
            return data.get("offset")
    except:
        return None

def _save_offset(offset):
    try:
        os.makedirs(os.path.dirname(OFFSET_PATH), exist_ok=True)
        with open(OFFSET_PATH, "w") as f:
            json.dump({"offset": offset}, f)
    except Exception as e:
        logging.warning("Failed to save offset: %s", e)

def _tg_api(method: str, **params):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/{method}"
        r = requests.get(url, params=params, timeout=50)
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        logging.debug("tg api error %s: %s", method, e)
    return None

def _handle_command(text: str):
    cmd = (text or "").strip().lower()
    if cmd.startswith("/status"):
        send_telegram("✅ Running. Wallet monitor, Dex monitor, Alerts & Guard active.")
    elif cmd.startswith("/diag"):
        send_telegram("🔧 Diagnostics available.")
    elif cmd.startswith("/rescan"):
        send_telegram("🔄 Rescan triggered.")
    elif cmd in ["/holdings", "/show_wallet_assets", "/showwalletassets", "/showassets", "/show"]:
        try:
            snapshot = get_wallet_snapshot(WALLET_ADDRESS)
            msg = format_holdings(snapshot)
            send_telegram(msg)
        except Exception as e:
            send_telegram(f"❌ Error fetching holdings:\n`{e}`")
    else:
        send_telegram("❓ Unknown command")

def telegram_long_poll_loop():
    offset = _load_offset()
    send_telegram("🤖 Telegram command handler online.")
    while True:
        resp = _tg_api("getUpdates", timeout=50, offset=offset, allowed_updates=json.dumps(["message"]))
        if not resp or not resp.get("ok"):
            time.sleep(1)
            continue
        for upd in resp.get("result", []):
            offset = upd["update_id"] + 1
            _save_offset(offset)
            msg = upd.get("message") or {}
            chat_id = str(((msg.get("chat") or {}).get("id") or ""))
            if CHAT_ID and CHAT_ID != chat_id:
                continue
            text = (msg.get("text") or "").strip()
            if not text:
                continue
            _handle_command(text)
