# telegram/commands.py
# Centralized Telegram command router (extracted from main.py).
# Keeps logic modular and avoids circular imports by depending on callables
# passed in during initialization.

from typing import Callable, Iterable, Optional


class CommandRouter:
    """
    Wire this from main.py like:

    router = CommandRouter(
        send_telegram=send_telegram,
        compute_holdings_merged=compute_holdings_merged,
        build_daily_sum_text=build_daily_sum_text,
        build_day_report_text=build_day_report_text,
        format_totals=format_totals,
        rpc_discover_wallet_tokens=rpc_discover_wallet_tokens,
        ensure_tracking_pair=ensure_tracking_pair,
        remove_tracking_pair=remove_tracking_pair,
        get_tracked_pairs=get_tracked_pairs,
        build_diag_text=build_diag_text,
    )
    """
    def __init__(
        self,
        *,
        send_telegram: Callable[[str], None],
        compute_holdings_merged: Callable[[], tuple],
        build_daily_sum_text: Callable[[], str],
        build_day_report_text: Callable[[], str],
        format_totals: Callable[[str], str],
        rpc_discover_wallet_tokens: Callable[[], int],
        ensure_tracking_pair: Callable[[str, str, Optional[dict]], None],
        remove_tracking_pair: Callable[[str], bool],
        get_tracked_pairs: Callable[[], Iterable[str]],
        build_diag_text: Callable[[], str],
    ):
        self.send = send_telegram
        self.compute_holdings_merged = compute_holdings_merged
        self.build_daily_sum_text = build_daily_sum_text
        self.build_day_report_text = build_day_report_text
        self.format_totals = format_totals
        self.rpc_discover_wallet_tokens = rpc_discover_wallet_tokens
        self.ensure_tracking_pair = ensure_tracking_pair
        self.remove_tracking_pair = remove_tracking_pair
        self.get_tracked_pairs = get_tracked_pairs
        self.build_diag_text = build_diag_text

    # ---- local pretty formatters (no external deps) ----
    @staticmethod
    def _fmt_amount(a) -> str:
        try:
            a = float(a)
        except Exception:
            return str(a)
        if abs(a) >= 1:
            return f"{a:,.4f}"
        if abs(a) >= 0.0001:
            return f"{a:.6f}"
        return f"{a:.8f}"

    @staticmethod
    def _fmt_price(p) -> str:
        try:
            p = float(p)
        except Exception:
            return str(p)
        if p >= 1:
            return f"{p:,.6f}"
        if p >= 0.01:
            return f"{p:.6f}"
        if p >= 1e-6:
            return f"{p:.8f}"
        return f"{p:.10f}"

    def _fmt_holdings_text(self) -> str:
        total, breakdown, unrealized, receipts = self.compute_holdings_merged()
        if not breakdown:
            return "📦 Κενά holdings."
        lines = ["*📦 Holdings (merged):*"]
        for b in breakdown:
            lines.append(
                f"• {b['token']}: {self._fmt_amount(b['amount'])}  "
                f"@ ${self._fmt_price(b.get('price_usd', 0))}  = ${self._fmt_amount(b.get('usd_value', 0))}"
            )
        if receipts:
            lines.append("\n*Receipts:*")
            for r in receipts:
                lines.append(f"• {r['token']}: {self._fmt_amount(r['amount'])}")
        lines.append(f"\nΣύνολο: ${self._fmt_amount(total)}")
        if abs(float(unrealized or 0.0)) > 1e-12:
            lines.append(f"Unrealized: ${self._fmt_amount(unrealized)}")
        return "\n".join(lines)

    # ---- Router ----
    def handle(self, text: str):
        t = (text or "").strip()
        low = t.lower()

        if low.startswith("/status"):
            self.send("✅ Running. Wallet monitor, Dex monitor, Alerts & Guard active.")
            return

        if low.startswith("/diag"):
            self.send(self.build_diag_text())
            return

        if low.startswith("/rescan"):
            cnt = self.rpc_discover_wallet_tokens()
            self.send(f"🔄 Rescan done. Positive tokens: {cnt}")
            return

        if low.startswith("/holdings") or low.startswith("/show_wallet_assets") or low.startswith("/showwalletassets") or low == "/show":
            self.send(self._fmt_holdings_text())
            return

        if low.startswith("/dailysum") or low.startswith("/showdaily"):
            self.send(self.build_daily_sum_text())
            return

        if low.startswith("/report"):
            self.send(self.build_day_report_text())
            return

        if low.startswith("/totals"):
            parts = low.split()
            scope = "all"
            if len(parts) > 1 and parts[1] in ("today", "month", "all"):
                scope = parts[1]
            self.send(self.format_totals(scope))
            return

        if low.startswith("/totalstoday"):
            self.send(self.format_totals("today"))
            return

        if low.startswith("/totalsmonth"):
            self.send(self.format_totals("month"))
            return

        if low.startswith("/pnl"):
            parts = low.split()
            scope = parts[1] if len(parts) > 1 and parts[1] in ("today", "month", "all") else "all"
            self.send(self.format_totals(scope))
            return

        if low.startswith("/watch "):
            try:
                _, rest = low.split(" ", 1)
                if rest.startswith("add "):
                    pair = rest.split(" ", 1)[1].strip().lower()
                    if pair.startswith("cronos/"):
                        self.ensure_tracking_pair("cronos", pair.split("/", 1)[1], None)
                        self.send(f"👁 Added {pair}")
                    else:
                        self.send("Use format cronos/<pairAddress>")
                elif rest.startswith("rm "):
                    pair = rest.split(" ", 1)[1].strip().lower()
                    ok = self.remove_tracking_pair(pair)
                    self.send(f"🗑 Removed {pair}" if ok else "Pair not tracked.")
                elif rest.strip() == "list":
                    tracked = list(self.get_tracked_pairs() or [])
                    self.send("👁 Tracked:\n" + "\n".join(sorted(tracked)) if tracked else "None.")
                else:
                    self.send("Usage: /watch add <cronos/pair> | /watch rm <cronos/pair> | /watch list")
            except Exception as e:
                self.send(f"Watch error: {e}")
            return

        # Unknown
        self.send("❓ Commands: /status /diag /rescan /holdings /show /dailysum /report /totals [today|month|all] /totalstoday /totalsmonth /pnl [scope] /watch ...")
