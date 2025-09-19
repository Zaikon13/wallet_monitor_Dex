# telegram/commands.py
"""
Telegram command handling for Cronos DeFi Sentinel.

Supported:
  /status
  /holdings
  /show, /show_wallet_assets, /showwalletassets  (aliases to /holdings)
  /report        (EOD-style snapshot via reports.day_report)
  /totals        (optional: via reports.aggregates if present)
  /pnl [today|month|all]  (optional: formats if aggregates available)

Integration:
  - main.py long-poll loop should call dispatch_update(update_dict)
    for each incoming Telegram update with a "message" that has "text".
"""

from __future__ import annotations
import os
import logging
from typing import Optional, Tuple

from telegram.api import send_telegram_message
from core.holdings import get_wallet_snapshot, format_snapshot_lines

# Optional imports: present in repo but keep defensive guards
try:
    from telegram.formatters import format_holdings  # if you prefer a custom formatting
except Exception:
    format_holdings = None

try:
    from reports.day_report import build_day_report_text
except Exception:
    build_day_report_text = None

try:
    from reports.aggregates import (
        build_totals_text,
        build_pnl_text,
    )
except Exception:
    build_totals_text = None
    build_pnl_text = None


WALLET_ADDRESS = os.getenv("WALLET_ADDRESS", "").strip()


# ---------------- Parsing ----------------

def _parse_command(text: str) -> Tuple[str, str]:
    """
    Return (cmd, args) from a Telegram text message.
    Examples:
      "/pnl today" -> ("pnl", "today")
      "/status"    -> ("status", "")
    """
    if not text:
        return "", ""
    t = text.strip()
    if not t.startswith("/"):
        return "", ""
    # Remove leading slash and bot suffix if present ("/holdings@YourBot")
    first = t.split()[0]
    if "@" in first:
        first = first.split("@", 1)[0]
    cmd = first.lstrip("/").lower()
    args = t[len(first):].strip()
    return cmd, args


# ---------------- Commands ----------------

def _cmd_status(args: str) -> None:
    send_telegram_message("🟢 Bot is online.")


def _cmd_holdings(args: str) -> None:
    if not WALLET_ADDRESS:
        send_telegram_message("⚠️ WALLET_ADDRESS is not configured.")
        return
    try:
        snap = get_wallet_snapshot(WALLET_ADDRESS, include_usd=True)
        if format_holdings:
            # If user has custom formatter in telegram/formatters.py
            text = format_holdings(snap)
        else:
            lines = format_snapshot_lines(snap)
            text = "💰 Holdings:\n" + ("\n".join(lines) if lines else "(empty)")
        send_telegram_message(text)
    except Exception as e:
        logging.exception("Failed to build holdings")
        send_telegram_message("⚠️ Failed to build holdings.")


def _cmd_report(args: str) -> None:
    if build_day_report_text is None:
        send_telegram_message("ℹ️ Daily report is not available in this build.")
        return
    try:
        text = build_day_report_text()
        send_telegram_message(f"📒 Daily Report\n{text}")
    except Exception as e:
        logging.exception("Failed to build/send daily report")
        send_telegram_message("⚠️ Failed to generate daily report.")


def _cmd_totals(args: str) -> None:
    if build_totals_text is None:
        send_telegram_message("ℹ️ Totals are not available in this build.")
        return
    try:
        text = build_totals_text()
        send_telegram_message(f"Σύνολα (Totals)\n{text}")
    except Exception as e:
        logging.exception("Failed to build/send totals")
        send_telegram_message("⚠️ Failed to generate totals.")


def _cmd_pnl(args: str) -> None:
    if build_pnl_text is None:
        send_telegram_message("ℹ️ PnL reporting is not available in this build.")
        return
    period = (args or "").strip().lower()
    if period not in ("today", "month", "all", ""):
        send_telegram_message("ℹ️ Usage: /pnl [today|month|all]")
        return
    period = period or "today"
    try:
        text = build_pnl_text(period)
        send_telegram_message(f"📈 PnL ({period})\n{text}")
    except Exception as e:
        logging.exception("Failed to build/send PnL")
        send_telegram_message(f"⚠️ Failed to generate PnL for '{period}'.")


# ---------------- Dispatcher ----------------

ALIASES = {
    "show": "holdings",
    "show_wallet_assets": "holdings",
    "showwalletassets": "holdings",
}

HANDLERS = {
    "status": _cmd_status,
    "holdings": _cmd_holdings,
    "report": _cmd_report,
    "totals": _cmd_totals,
    "pnl": _cmd_pnl,
}


def dispatch_update(update: dict) -> None:
    """
    Entry point from main.py.
    Expects a Telegram update dict that may contain:
      - update["message"]["text"]
    If no command is found, it ignores the update silently.
    """
    try:
        msg = update.get("message") or {}
        text = (msg.get("text") or "").strip()
        if not text.startswith("/"):
            return  # not a command

        cmd, args = _parse_command(text)
        if not cmd:
            return
        cmd = ALIASES.get(cmd, cmd)

        handler = HANDLERS.get(cmd)
        if handler is None:
            send_telegram_message("ℹ️ Unknown command.")
            return

        handler(args)
    except Exception as e:
        logging.exception("dispatch_update failed")
        send_telegram_message("⚠️ Command handling error.")
