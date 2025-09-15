# core/alerts.py
"""
Holdings spike/dump alerts with cooldown (persisted).
Scans current wallet balances (symbols), looks up Dexscreener 24h/2h change,
and sends Telegram alerts when thresholds χτυπάνε — με cooldown ανά asset.
"""
from __future__ import annotations
import os, time, json
from typing import Dict, Tuple, Optional
from utils.http import safe_get, safe_json
from core.config import settings
from telegram.api import send_telegram

DEX_BASE_SEARCH  = "https://api.dexscreener.com/latest/dex/search"
STATE_PATH = os.path.join(settings.DATA_DIR, "alerts_state.json")

def _load_state() -> dict:
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def _save_state(st: dict) -> None:
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_PATH)

def _best_pair_change(query: str) -> Tuple[Optional[float], Optional[float], Optional[str], Optional[float]]:
    """
    Returns: (ch24, ch2h, dexscreener_url, price_usd)
    Picks the highest-liquidity Cronos pair for the query (symbol or 0x).
    """
    data = safe_json(safe_get(DEX_BASE_SEARCH, params={"q": query}, timeout=12)) or {}
    pairs = data.get("pairs") or []
    best, best_liq = None, -1.0
    for p in pairs:
        try:
            if str(p.get("chainId","")).lower() != "cronos":  # only Cronos
                continue
            liq = float((p.get("liquidity") or {}).get("usd") or 0)
            if liq > best_liq:
                best_liq, best = liq, p
        except:  # noqa: E722
            continue
    if not best:
        return None, None, None, None
    ch = best.get("priceChange") or {}
    ch24 = ch.get("h24"); ch2h = ch.get("h2")
    try:
        ch24 = float(ch24) if ch24 is not None else None
    except: ch24 = None  # noqa: E722
    try:
        ch2h = float(ch2h) if ch2h is not None else None
    except: ch2h = None  # noqa: E722
    url = f"https://dexscreener.com/cronos/{best.get('pairAddress')}"
    try:
        price = float(best.get("priceUsd") or 0)
    except:
        price = None
    return ch24, ch2h, url, price

def scan_holdings_alerts(balances: Dict[str, float]) -> None:
    """
    balances: dict SYMBOL -> amount
    - Triggers on 24h change crossing ±PRICE_MOVE_THRESHOLD (fallback to 2h if 24h missing).
    - Respects cooldown (ALERTS_INTERVAL_MIN).
    """
    state = _load_state()
    now = time.time()
    cd_sec = max(60, settings.ALERTS_INTERVAL_MIN * 60)

    for sym, amt in list(balances.items()):
        try:
            if float(amt) <= 0: 
                continue
        except:
            continue
        ch24, ch2h, url, price = _best_pair_change(sym)
        ch = ch24 if ch24 is not None else ch2h
        if ch is None:
            continue

        key = f"hold:{sym.upper()}"
        last = float(state.get(key, 0))
        if now - last < cd_sec:
            continue  # cooldown

        thr = float(settings.PRICE_MOVE_THRESHOLD or 5.0)
        if abs(ch) >= thr:
            if ch > 0:
                send_telegram(f"🚀 Pump {sym} {ch:.2f}% — ${price or 0:.6f}\n{url or ''}".strip())
            else:
                send_telegram(f"⚠️ Dump {sym} {ch:.2f}% — ${price or 0:.6f}\n{url or ''}".strip())
            state[key] = now

    _save_state(state)
