import os, logging, time
from decimal import Decimal
from utils.http import safe_json
from telegram.api import send_telegram

DEXSCREENER_API = os.getenv("DEXSCREENER_API", "https://api.dexscreener.com/latest/dex/pairs/cronos")
PRICE_MOVE_THRESHOLD = float(os.getenv("PRICE_MOVE_THRESHOLD", "10"))

_watchlist = set((os.getenv("DEX_PAIRS") or "").split(","))
_guard_prices = {}

def _get_dex_data():
    data = safe_json(DEXSCREENER_API)
    return (data or {}).get("pairs", [])

def _check_pump_dump(pair):
    if not pair: return None
    change = float(pair.get("priceChange", 0))
    if abs(change) >= PRICE_MOVE_THRESHOLD:
        return f"⚠️ {'Pump' if change > 0 else 'Dump'} {pair.get('pairAddress')[:8].upper()} {change:+.2f}% — ${pair.get('priceUsd')[:10]}\n{pair.get('url')}"
    return None

def _check_guard(pair):
    if not pair: return None
    addr = pair.get("pairAddress")
    if addr not in _watchlist:
        return None
    current_price = Decimal(pair.get("priceUsd", "0"))
    if current_price <= 0:
        return None
    last_price = _guard_prices.get(addr)
    if last_price:
        change = ((current_price - last_price) / last_price) * 100
        if abs(change) >= PRICE_MOVE_THRESHOLD:
            msg = f"⚠️ {'Pump' if change > 0 else 'Dump'} {addr[:8].upper()} {change:+.2f}% — ${current_price:.6f}\n{pair.get('url')}"
            _guard_prices[addr] = current_price
            return msg
    _guard_prices[addr] = current_price
    return None

def alert_loop():
    logging.info("Starting alerts loop...")
    while True:
        try:
            pairs = _get_dex_data()
            if not pairs:
                time.sleep(30)
                continue
            for p in pairs:
                alert_msg = _check_pump_dump(p) or _check_guard(p)
                if alert_msg:
                    send_telegram(alert_msg)
        except Exception as e:
            logging.warning("Alert loop error: %s", e)
        time.sleep(60)
