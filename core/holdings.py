# core/holdings.py
from __future__ import annotations
import os
from decimal import Decimal as D
from typing import Dict, Any

from core.providers.etherscan_like import account_balance, token_balance, account_tokentx
from core.pricing import get_price_usd


def _dec(x) -> D:
    try:
        return D(str(x))
    except Exception:
        return D("0")


def _map_from_env(key: str) -> dict:
    s = os.getenv(key, "").strip()
    if not s:
        return {}
    out = {}
    for part in s.split(","):
        if not part or "=" not in part:
            continue
        k, v = part.split("=", 1)
        out[k.strip().upper()] = v.strip()
    return out


def _mk_row(qty: D, px: D | None) -> Dict[str, Any]:
    """
    Ενιαία δομή συμβατή με παλιό και νέο κώδικα:
      - amount / usd_value  (που ζητά το day_report)
      - qty / price_usd / usd (ό,τι περίμεναν άλλα formatters)
    Όλα ως Decimal.
    """
    if px is None:
        px = D("0")
    usd = qty * px if px != 0 else D("0")
    return {
        "amount": qty,
        "usd_value": usd,
        "qty": qty,
        "price_usd": px,
        "usd": usd,
    }


def get_wallet_snapshot(address: str | None = None) -> Dict[str, Dict[str, Any]]:
    """
    Επιστρέφει:
      {
        "CRO": {"amount": D, "usd_value": D, "qty": D, "price_usd": D, "usd": D},
        "SYMBOL": {...},
        ...
      }
    Προσπαθεί για CRO (native) + tokens από env maps (TOKENS_ADDRS, TOKENS_DECIMALS).
    Δεν κάνει raise — σε αποτυχία επιστρέφει ό,τι μπόρεσε.
    """
    address = (address or os.getenv("WALLET_ADDRESS", "")).strip()
    if not address:
        return {}

    snap: Dict[str, Dict[str, Any]] = {}

    # 1) CRO native balance (wei → CRO)
    try:
        bal = (account_balance(address) or {}).get("result")
        if bal is not None:
            # Συμβατότητα: αν έρθει hex → int(hex,16), αλλιώς decimal string
            try:
                wei = int(str(bal), 16) if str(bal).startswith(("0x", "0X")) else int(str(bal))
            except Exception:
                wei = int(_dec(bal))
            cro = D(wei) / (D(10) ** 18)
            px = _dec(get_price_usd("CRO") or 0)
            snap["CRO"] = _mk_row(cro, px)
    except Exception:
        # Μη ρίχνεις εξαίρεση — κρατάμε το flow ζωντανό
        pass

    # 2) Tokens via TOKENS_ADDRS (+ optional TOKENS_DECIMALS)
    addr_map = _map_from_env("TOKENS_ADDRS")
    dec_map = _map_from_env("TOKENS_DECIMALS")

    for sym, contract in addr_map.items():
        sym = (sym or "").upper()
        if not sym or not contract:
            continue
        try:
            raw = (token_balance(contract, address) or {}).get("result")
            if raw is None:
                # δείξε το σύμβολο με μηδενικά ώστε να φαίνεται
                snap.setdefault(sym, _mk_row(D("0"), D("0")))
                continue

            # raw μπορεί να είναι hex ή decimal
            try:
                base_int = int(str(raw), 16) if str(raw).startswith(("0x", "0X")) else int(str(raw))
                base = D(base_int)
            except Exception:
                base = _dec(raw)

            decimals = int(dec_map.get(sym, "18"))
            qty = base / (D(10) ** decimals)
            px = _dec(get_price_usd(sym) or 0)

            snap[sym] = _mk_row(qty, px)
        except Exception:
            snap.setdefault(sym, _mk_row(D("0"), D("0")))

    # 3) (Optional) Discover recent token symbols from tokentx (visibility only)
    try:
        toks = (account_tokentx(address) or {}).get("result") or []
        for t in toks[-50:]:
            sym = (t.get("tokenSymbol") or "?").upper()
            if sym and sym not in snap:
                snap[sym] = _mk_row(D("0"), D("0"))
    except Exception:
        pass

    return snap
