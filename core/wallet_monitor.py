from __future__ import annotations

import logging
import os
import time
from typing import Dict, List

from core.tz import ymd
from reports.ledger import append_ledger, update_cost_basis
from core.guards import mark_trade
from telegram.api import send_telegram_message
from core.runtime_state import note_cost_basis_update, note_wallet_poll


class WalletMonitor:
    def __init__(self, wallet: str, fetch_fn, cooldown_sec: int = 5):
        self.wallet = wallet
        self.fetch_fn = fetch_fn
        self.cooldown = cooldown_sec
        self._seen: set[str] = set()
        self._last = 0.0

    def _dedup(self, txs: List[Dict[str, object]]):
        out: List[Dict[str, object]] = []
        for entry in txs:
            key = str(entry.get("txid") or "")
            if not key or key in self._seen:
                continue
            self._seen.add(key)
            out.append(entry)
        return out

    def _record_alert(self, entry: Dict[str, object]):
        day = ymd()
        side = (entry.get("side") or "").upper()
        if side == "SWAP":
            legs = entry.get("legs") or []
            for leg in legs:
                normalized = {
                    "wallet": self.wallet,
                    "time": entry.get("time"),
                    "side": (leg.get("side") or "").upper(),
                    "asset": (leg.get("asset") or "?").upper(),
                    "qty": leg.get("qty"),
                    "price_usd": leg.get("price_usd"),
                    "usd": leg.get("usd"),
                    "fee_usd": leg.get("fee_usd"),
                }
                append_ledger(day, normalized)
                try:
                    mark_trade(normalized["asset"], normalized["side"])
                except Exception:
                    logging.debug("mark_trade failed", exc_info=True)
            parts = [
                f"{(leg.get('side') or '').upper()} {(leg.get('asset') or '?').upper()} {leg.get('qty')}"
                for leg in legs
            ]
            if parts:
                send_telegram_message("🔁 Swap: " + " | ".join(parts))
        else:
            normalized = {
                "wallet": self.wallet,
                "time": entry.get("time"),
                "side": (entry.get("side") or "IN").upper(),
                "asset": (entry.get("asset") or "?").upper(),
                "qty": entry.get("qty"),
                "price_usd": entry.get("price_usd"),
                "usd": entry.get("usd"),
                "fee_usd": entry.get("fee_usd"),
            }
            append_ledger(day, normalized)
            try:
                mark_trade(normalized["asset"], normalized["side"])
            except Exception:
                logging.debug("mark_trade failed", exc_info=True)
            emoji = "➕" if normalized["side"] == "IN" else "➖"
            send_telegram_message(
                f"{emoji} {normalized['side']} {normalized['asset']} {normalized.get('qty')}"
            )

    def poll_once(self) -> int:
        try:
            raw = self.fetch_fn(self.wallet) or []
            note_wallet_poll(True)
        except Exception as exc:  # pragma: no cover - network failures
            logging.debug("wallet fetch failed: %s", exc)
            note_wallet_poll(False, str(exc))
            return 0

        fresh = self._dedup(raw)
        appended = 0
        now = time.time()
        for entry in fresh:
            if now - self._last < self.cooldown:
                continue
            self._last = now
            self._record_alert(entry)
            appended += 1

        if appended:
            try:
                update_cost_basis()
                note_cost_basis_update()
            except Exception:
                logging.debug("cost basis update failed", exc_info=True)
        return len(fresh)


def make_wallet_monitor(provider=None):
    wallet = os.getenv("WALLET_ADDRESS", "")
    if not provider:
        provider = lambda _addr: []
    cooldown = max(3, int(os.getenv("WALLET_ALERTS_COOLDOWN", "5") or 5))
    return WalletMonitor(wallet, provider, cooldown)
