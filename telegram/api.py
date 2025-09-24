import os, json, time, logging, requests
from telegram.formatters import escape_md, format_holdings
from core.holdings import get_wallet_snapshot

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
OFFSET_PATH = "/app/data/telegram_offset.json"


def send_telegram(msg: str, *, parse_mode: str = "MarkdownV2", already_escaped: bool = False):
    """
    Safe sender:
      - Αν parse_mode == 'MarkdownV2' και ΔΕΝ είναι ήδη escaped, κάνε escape_md().
      - Διαφορετικά, στείλ' το ως έχει (ή με άλλο parse_mode).
    Επιστρέφει (ok: bool, status_code: int, text: str)
    """
    if not TELEGRAM_TOKEN or not CHAT_ID:
        return False, 0, "no-credentials"

    text = msg
    if parse_mode == "MarkdownV2" and not already_escaped:
        text = escape_md(msg)

    payload = {
        "chat_id": CHAT_ID,
        "text": text,
        "disable_web_page_preview": True,
    }
    if parse_mode:
        payload["parse_mode"] = parse_mode

    try:
        r = requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                          json=payload, timeout=10)
        if r.status_code != 200:
            logging.warning("Telegram send failed %s: %s", r.status_code, r.text)
            return False, r.status_code, r.text
        return True, r.status_code, r.text
    except Exception as e:
        logging.warning("Telegram send error: %s", e)
        return False, -1, str(e)


def _load_offset():
    try:
        with open(OFFSET_PATH, "r") as f:
            data = json.load(f)
            return data.get("offset")
    except Exception:
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
            snapshot = get_wallet_snapshot()  # χωρίς παράμετρο wallet
            msg = format_holdings(snapshot)   # ΗΔΗ escaped
            send_telegram(msg, already_escaped=True)
        except Exception as e:
            send_telegram(f"❌ Error fetching holdings:\n`{str(e)}`")
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
