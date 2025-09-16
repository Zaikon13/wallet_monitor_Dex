# core/holdings.py
# Snapshot όλων των holdings από Cronos RPC (CRO + ERC-20) με προαιρετικό pricing από Dexscreener.
# Κρατάμε tCRO/receipts ξεχωριστά (ΔΕΝ γίνεται alias σε CRO).

from __future__ import annotations
import os, time
from decimal import Decimal
from collections import defaultdict

# --- ENV ---
CRONOS_RPC_URL = os.getenv("CRONOS_RPC_URL") or ""
WALLET_ADDRESS = (os.getenv("WALLET_ADDRESS") or "").lower()

# --- Web3 (μικρό, τοπικό helper) ---
WEB3 = None
ERC20_ABI_MIN = [
    {"constant": True, "inputs": [], "name": "symbol", "outputs": [{"name": "", "type": "string"}], "type": "function"},
    {"constant": True, "inputs": [], "name": "decimals", "outputs": [{"name": "", "type": "uint8"}], "type": "function"},
    {"constant": True, "inputs": [{"name": "owner", "type": "address"}], "name": "balanceOf", "outputs": [{"name": "", "type": "uint256"}], "type": "function"},
]

def _w3_init():
    global WEB3
    if WEB3 is not None:
        return WEB3
    if not CRONOS_RPC_URL:
        return None
    try:
        from web3 import Web3
        WEB3 = Web3(Web3.HTTPProvider(CRONOS_RPC_URL, request_kwargs={"timeout": 15}))
        if WEB3.is_connected():
            return WEB3
        return None
    except Exception:
        return None

def _cs(addr: str) -> str:
    try:
        from web3 import Web3
        return Web3.to_checksum_address(addr)
    except Exception:
        return addr

def _erc20_symbol_decimals(addr: str) -> tuple[str, int]:
    w3 = _w3_init()
    if not w3:
        return (addr[:8].upper(), 18)
    try:
        c = w3.eth.contract(address=_cs(addr), abi=ERC20_ABI_MIN)
        sym = c.functions.symbol().call()
        dec = int(c.functions.decimals().call())
        return (sym or addr[:8].upper(), dec or 18)
    except Exception:
        return (addr[:8].upper(), 18)

def _erc20_balance(addr: str, owner: str) -> Decimal:
    w3 = _w3_init()
    if not w3:
        return Decimal("0")
    try:
        c = w3.eth.contract(address=_cs(addr), abi=ERC20_ABI_MIN)
        raw = c.functions.balanceOf(_cs(owner)).call()
        # decimals θα το εφαρμόσουμε εκτός, όπου χρειαστεί
        return Decimal(str(raw))
    except Exception:
        return Decimal("0")

def _native_balance(owner: str) -> Decimal:
    w3 = _w3_init()
    if not w3:
        return Decimal("0")
    try:
        wei = w3.eth.get_balance(_cs(owner))
        return Decimal(str(wei))
    except Exception:
        return Decimal("0")

# --- Dexscreener pricing (light) ---
from typing import Optional
import requests

DEX_BASE_TOKENS = "https://api.dexscreener.com/latest/dex/tokens"
DEX_BASE_SEARCH = "https://api.dexscreener.com/latest/dex/search"

def _pick_best_price(pairs: list[dict]) -> Optional[float]:
    if not pairs:
        return None
    best, best_liq = None, -1.0
    for p in pairs:
        try:
            if str(p.get("chainId", "")).lower() != "cronos":
                continue
            liq = float((p.get("liquidity") or {}).get("usd") or 0)
            price = float(p.get("priceUsd") or 0)
            if price <= 0:
                continue
            if liq > best_liq:
                best_liq, best = liq, price
        except Exception:
            continue
    return best

def _price_usd_for(key: str) -> Optional[float]:
    try:
        if key.lower() in ("cro", "wcro"):
            # canonical CRO μέσω WCRO/USDT (fallback: "cro usdt")
            for q in ("wcro usdt", "wcro usdc", "cro usdt"):
                r = requests.get(DEX_BASE_SEARCH, params={"q": q}, timeout=10)
                if r.ok:
                    j = r.json() or {}
                    p = _pick_best_price(j.get("pairs") or [])
                    if p and p > 0:
                        return p
            return None
        if key.startswith("0x") and len(key) == 42:
            r = requests.get(f"{DEX_BASE_TOKENS}/cronos/{key}", timeout=10)
            if r.ok:
                j = r.json() or {}
                p = _pick_best_price(j.get("pairs") or [])
                if p and p > 0:
                    return p
            # fallback: search by addr
            r = requests.get(DEX_BASE_SEARCH, params={"q": key}, timeout=10)
            if r.ok:
                j = r.json() or {}
                p = _pick_best_price(j.get("pairs") or [])
                if p and p > 0:
                    return p
            return None
        # symbol search
        r = requests.get(DEX_BASE_SEARCH, params={"q": key}, timeout=10)
        if r.ok:
            j = r.json() or {}
            p = _pick_best_price(j.get("pairs") or [])
            if p and p > 0:
                return p
        # extra attempts
        for q in (f"{key} usdt", f"{key} wcro"):
            r = requests.get(DEX_BASE_SEARCH, params={"q": q}, timeout=10)
            if r.ok:
                j = r.json() or {}
                p = _pick_best_price(j.get("pairs") or [])
                if p and p > 0:
                    return p
        return None
    except Exception:
        return None

# --- Public API ---
def get_wallet_snapshot(normalize_zero: bool = True, with_prices: bool = True) -> list[dict]:
    """
    Επιστρέφει λίστα από dicts:
      { "symbol": str, "token_addr": str|None, "amount": Decimal, "price_usd": float|None, "usd_value": float|None }
    * Native CRO με 18 decimals (Wei → CRO)
    * ERC-20 balances με πραγματικό decimals
    * ΔΕΝ γίνεται alias tCRO → CRO (receipt tokens μένουν ξεχωριστά)
    """
    if not WALLET_ADDRESS:
        return []

    out: list[dict] = []

    # Native CRO
    wei = _native_balance(WALLET_ADDRESS)
    if normalize_zero is False or wei > 0:
        cro_amt = (wei / Decimal(10 ** 18)).quantize(Decimal("0.00000001"))
        price = _price_usd_for("CRO") if with_prices else None
        usd = float(cro_amt) * float(price) if (price and cro_amt) else None
        out.append({"symbol": "CRO", "token_addr": None, "amount": cro_amt, "price_usd": price, "usd_value": usd})

    # ERC-20 discovery via recent token txs (light) + balanceOf check
    # Για απλότητα εδώ, ο χρήστης μπορεί να δώσει συμβόλαια στο env TOKENS=cronos/0x....
    tokens_env = [t.strip().lower() for t in (os.getenv("TOKENS", "")).split(",") if t.strip()]
    addrs = []
    for t in tokens_env:
        if t.startswith("cronos/"):
            addrs.append(t.split("/", 1)[1])

    # Αν θέλεις αυτόματο discovery όπως στο main.py (logs), το αφήνουμε στο main.
    # Εδώ διαβάζουμε αυτά που μας δίνεις στο env για να είναι ανεξάρτητο helper.

    seen = set()
    for addr in addrs:
        if not (addr.startswith("0x") and len(addr) == 42):
            continue
        if addr in seen:
            continue
        seen.add(addr)
        sym, dec = _erc20_symbol_decimals(addr)
        raw = _erc20_balance(addr, WALLET_ADDRESS)
        if raw <= 0:
            continue
        amt = (raw / Decimal(10 ** dec)).quantize(Decimal("0.00000001"))
        price = _price_usd_for(addr) if with_prices else None
        usd = float(amt) * float(price) if (price and amt) else None
        out.append({"symbol": sym or addr[:8].upper(), "token_addr": addr, "amount": amt, "price_usd": price, "usd_value": usd})

    # Ταξινόμηση κατά USD value (φθίνουσα), αλλιώς κατά ποσότητα
    def _key(d):
        v = d.get("usd_value")
        if v is not None:
            return (0, v)
        return (1, float(d.get("amount") or 0))
    out.sort(key=_key, reverse=True)
    return out
