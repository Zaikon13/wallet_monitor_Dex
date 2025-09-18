# telegram/api.py
import os, json, time, logging, requests
from telegram.formatters import escape_md, format_holdings
from core.holdings import get_wallet_snapshot

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
WALLET_ADDRESS = os.getenv("WALLET_ADDRESS")
OFFSET_PATH = "/app/data/telegram_offset.json"

def send_telegram(msg: str):
    if not TELEGRAM_TOKEN or not CHAT_ID: return False, 0, "no-credentials"
    try:
        r = requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                          json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "MarkdownV2", "disable_web_page_preview": True},
                          timeout=10)
        if r.status_code != 200:
            logging.warning("Telegram send failed %s: %s", r.status_code, r.text)
            return False, r.status_code, r.text
        return True, r.status_code, r.text
    except Exception as e:
        logging.warning("Telegram send error: %s", e)
        return False, -1, str(e)

def _load_offset():
    try:
        with open(OFFSET_PATH, "r") as f: return json.load(f).get("offset")
    except: return None

def _save_offset(offset):
    try:
        os.makedirs(os.path.dirname(OFFSET_PATH), exist_ok=True)
        with open(OFFSET_PATH, "w") as f: json.dump({"offset": offset}, f)
    except Exception as e:
        logging.warning("Failed to save offset: %s", e)

def _tg_api(method: str, **params):
    try:
        r = requests.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/{method}", params=params, timeout=50)
        if r.status_code == 200: return r.json()
    except Exception as e:
        logging.debug("tg api error %s: %s", method, e)
    return None

def _handle_command(text: str):
    cmd=(text or "").strip().lower()
    if cmd.startswith("/status"): send_telegram("✅ Running. Wallet monitor, Dex monitor, Alerts & Guard active.")
    elif cmd.startswith("/diag"): send_telegram("🔧 Diagnostics available.")
    elif cmd.startswith("/rescan"): send_telegram("🔄 Rescan triggered.")
    elif cmd in ["/holdings","/show_wallet_assets","/showwalletassets","/showassets","/show"]:
        try:
            snap=get_wallet_snapshot(); msg=format_holdings(snap); send_telegram(msg)
        except Exception as e: send_telegram(f"❌ Error fetching holdings:\n`{escape_md(str(e))}`")
    else: send_telegram("❓ Unknown command")

def telegram_long_poll_loop():
    offset=_load_offset(); send_telegram("🤖 Telegram command handler online.");
    while True:
        resp=_tg_api("getUpdates", timeout=50, offset=offset, allowed_updates=json.dumps(["message"]))
        if not resp or not resp.get("ok"): time.sleep(1); continue
        for upd in resp.get("result", []):
            offset=upd["update_id"]+1; _save_offset(offset)
            msg=upd.get("message") or {}; chat_id=str(((msg.get("chat") or {}).get("id") or ""))
            if CHAT_ID and CHAT_ID!=chat_id: continue
            text=(msg.get("text") or "").strip();
            if text: _handle_command(text)
