# main.py - Wallet monitor + Dexscreener + Auto Discovery + Real-time PnL Reports
import os
import time
import threading
from collections import deque, defaultdict
from datetime import datetime
import requests
from dotenv import load_dotenv

load_dotenv()

# ========================= Environment =========================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID")
WALLET_ADDRESS     = os.getenv("WALLET_ADDRESS")
ETHERSCAN_API      = os.getenv("ETHERSCAN_API")
DEX_PAIRS          = os.getenv("DEX_PAIRS", "")
PRICE_MOVE_THRESHOLD = float(os.getenv("PRICE_MOVE_THRESHOLD", "5.0"))
WALLET_POLL        = int(os.getenv("WALLET_POLL", "15"))
DEX_POLL           = int(os.getenv("DEX_POLL", "60"))

PRICE_WINDOW       = int(os.getenv("PRICE_WINDOW", "3"))
SPIKE_THRESHOLD    = float(os.getenv("SPIKE_THRESHOLD", "8.0"))
MIN_VOLUME_FOR_ALERT = float(os.getenv("MIN_VOLUME_FOR_ALERT", "0"))

DISCOVER_ENABLED   = os.getenv("DISCOVER_ENABLED", "true").lower() in ("1","true","yes","on")
DISCOVER_QUERY     = os.getenv("DISCOVER_QUERY", "cronos")
DISCOVER_LIMIT     = int(os.getenv("DISCOVER_LIMIT", "10"))
DISCOVER_POLL      = int(os.getenv("DISCOVER_POLL", "120"))
TOKENS             = os.getenv("TOKENS", "")

# ========================= Constants =========================
ETHERSCAN_V2_URL   = "https://api.etherscan.io/v2/api"
CRONOS_CHAINID     = 25
DEX_BASE_PAIRS     = "https://api.dexscreener.com/latest/dex/pairs"
DEX_BASE_TOKENS    = "https://api.dexscreener.com/latest/dex/tokens"
DEX_BASE_SEARCH    = "https://api.dexscreener.com/latest/dex/search"
DEXSITE_PAIR       = "https://dexscreener.com/{chain}/{pair}"
CRONOS_TX          = "https://cronoscan.com/tx/{txhash}"
TELEGRAM_URL       = "https://api.telegram.org/bot{token}/sendMessage"

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
})

# ========================= State =========================
_seen_tx_hashes   = set()
_last_prices      = {}
_price_history    = {}
_last_pair_tx     = {}
_rate_limit_last  = 0.0
_tracked_pairs    = set()
_known_pairs_meta = {}

# ========================= PnL Tracking =========================
_wallet_day_txs = []  # Καταγραφή όλων των συναλλαγών της ημέρας
_asset_holdings = defaultdict(float)
_asset_prices   = {}
BIG_ALERT_LIMIT = 50.0  # USDT

# ========================= Utils =========================
def send_telegram(message: str) -> bool:
    global _rate_limit_last
    now = time.time()
    if now - _rate_limit_last < 0.8:
        time.sleep(0.8 - (now - _rate_limit_last))
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram not configured.")
        return False
    url = TELEGRAM_URL.format(token=TELEGRAM_BOT_TOKEN)
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
    try:
        r = SESSION.post(url, data=payload, timeout=12)
        _rate_limit_last = time.time()
        return r.status_code == 200
    except Exception as e:
        print("Exception sending telegram:", e)
        return False

def safe_json(r):
    if r is None:
        return None
    if not getattr(r, "ok", False):
        print("HTTP error:", getattr(r, "status_code", None))
        return None
    try:
        return r.json()
    except Exception:
        txt = (r.text[:800].replace("\n", " ")) if hasattr(r, "text") else "<no body>"
        print("Response not JSON (preview):", txt)
        return None

# ========================= Dexscreener Price =========================
def get_price_usdt(symbol: str) -> float:
    if symbol.upper() == "USDT":
        return 1.0
    try:
        url = f"https://api.dexscreener.com/latest/dex/search"
        r = SESSION.get(url, params={"q": symbol}, timeout=10)
        data = safe_json(r)
        if not data or "pairs" not in data:
            return 0.0
        for p in data["pairs"]:
            if str(p.get("chainId","")).lower() == "cronos":
                return float(p.get("priceUsd") or 0)
        return 0.0
    except Exception as e:
        print("Error fetching price for", symbol, e)
        return 0.0

# ========================= Wallet Monitor =========================
def fetch_latest_wallet_txs(limit=25):
    if not WALLET_ADDRESS or not ETHERSCAN_API:
        return []
    params = {
        "chainid": CRONOS_CHAINID,
        "module": "account",
        "action": "txlist",
        "address": WALLET_ADDRESS,
        "startblock": 0,
        "endblock": 99999999,
        "page": 1,
        "offset": limit,
        "sort": "desc",
        "apikey": ETHERSCAN_API,
    }
    try:
        r = SESSION.get(ETHERSCAN_V2_URL, params=params, timeout=15)
        data = safe_json(r)
        if not data:
            return []
        if str(data.get("status", "")).strip() == "1" and isinstance(data.get("result"), list):
            return data["result"]
        return []
    except Exception as e:
        print("Error fetching wallet txs:", e)
        return []

def wallet_monitor_loop():
    global _seen_tx_hashes
    initial = fetch_latest_wallet_txs(limit=50)
    _seen_tx_hashes = set(tx.get("hash") for tx in initial if isinstance(tx, dict) and tx.get("hash"))
    send_telegram(f"🚀 Wallet monitor started for `{WALLET_ADDRESS}` (Cronos).")

    while True:
        txs = fetch_latest_wallet_txs(limit=25)
        if txs:
            for tx in reversed(txs):
                if not isinstance(tx, dict):
                    continue
                h = tx.get("hash")
                if not h or h in _seen_tx_hashes:
                    continue
                _seen_tx_hashes.add(h)

                val_raw = tx.get("value", "0")
                try:
                    value = int(val_raw) / 10**18
                except Exception:
                    value = float(val_raw) if val_raw else 0.0

                frm  = tx.get("from")
                to   = tx.get("to")
                blk  = tx.get("blockNumber")
                link = CRONOS_TX.format(txhash=h)

                msg = (
                    f"*New Cronos TX*\n"
                    f"Address: `{WALLET_ADDRESS}`\n"
                    f"Hash: {link}\n"
                    f"From: `{frm}`\n"
                    f"To: `{to}`\n"
                    f"Value: {value:.6f} CRO\n"
                    f"Block: {blk}"
                )
                send_telegram(msg)

                # --- PnL Tracking ---
                symbol = "CRO"
                amount = value
                _wallet_day_txs.append({"symbol": symbol, "amount": amount, "timestamp": datetime.utcnow()})
                _asset_holdings[symbol] += amount
                _asset_prices[symbol] = get_price_usdt(symbol)
        time.sleep(WALLET_POLL)

# ========================= Daily & Intraday Reports =========================
def compute_daily_pnl_report():
    report_lines = []
    total_pnl = 0.0
    for symbol, qty in _asset_holdings.items():
        price_usdt = get_price_usdt(symbol)
        _asset_prices[symbol] = price_usdt
        pnl_usdt = qty * price_usdt
        total_pnl += pnl_usdt
        pct_change = 0.0
        report_lines.append(f"{symbol}: {pnl_usdt:.2f} USDT ({pct_change:+.2f}%)")

        if abs(pnl_usdt) >= BIG_ALERT_LIMIT:
            alert_type = "📈 Big Profit" if pnl_usdt > 0 else "📉 Big Loss"
            send_telegram(f"{alert_type} on {symbol}: {pnl_usdt:.2f} USDT ({pct_change:+.2f}%)")
    report = f"📊 PnL Report ({datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')})\n"
    report += "\n".join(report_lines)
    report += f"\n\nΣυνολικό PnL: {total_pnl:.2f} USDT"
    return report

def daily_report_loop():
    while True:
        now = datetime.utcnow()
        if now.minute == 0 and now.hour % 2 == 0:
            report = compute_daily_pnl_report()
            send_telegram(f"⏱ Intraday Report\n{report}")
            time.sleep(60)
        if now.hour == 23 and now.minute == 59:
            report = compute_daily_pnl_report()
            send_telegram(f"📅 Daily Report\n{report}")
            _wallet_day_txs.clear()
            time.sleep(60)
        time.sleep(20)

# ========================= Entrypoint =========================
def main():
    print("Starting monitor with Real-time PnL reporting...")
    t_wallet = threading.Thread(target=wallet_monitor_loop, daemon=True)
    t_daily  = threading.Thread(target=daily_report_loop, daemon=True)
    t_wallet.start()
    t_daily.start()
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        print("Stopping monitor.")

if __name__ == "__main__":
    main()
