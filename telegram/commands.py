# telegram/commands.py
import json, os, logging
from telegram.api import send_telegram
from core.discovery import handle_watch_command
from core.holdings import format_daily_sum_message, build_day_report_text
from core.holdings import compute_holdings_merged
from core.holdings import aggregate_per_asset, read_json, data_file_for_today, month_prefix

log = logging.getLogger("wallet-monitor")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID") or ""
WALLET_ADDRESS     = (os.getenv("WALLET_ADDRESS") or "").lower()

def _fmt_holdings_text():
    total, breakdown, unrealized, receipts = compute_holdings_merged()
    if not breakdown:
        return "📦 Κενά holdings."
    def _fmt_amount(a):
        try: a=float(a)
        except: return str(a)
        if abs(a)>=1: return f"{a:,.4f}"
        if abs(a)>=0.0001: return f"{a:.6f}"
        return f"{a:.8f}"
    def _fmt_price(p):
        try: p=float(p)
        except: return str(p)
        if p>=1: return f"{p:,.6f}"
        if p>=0.01: return f"{p:.6f}"
        if p>=1e-6: return f"{p:.8f}"
        return f"{p:.10f}"
    lines=["*📦 Holdings (merged):*"]
    for b in breakdown:
        lines.append(f"• {b['token']}: {_fmt_amount(b['amount'])}  @ ${_fmt_price(b.get('price_usd',0))}  = ${_fmt_amount(b.get('usd_value',0))}")
    if receipts:
        lines.append("\n*Receipts:*")
        for r in receipts:
            lines.append(f"• {r['token']}: {_fmt_amount(r['amount'])}")
    lines.append(f"\nΣύνολο: ${_fmt_amount(total)}")
    if abs(float(unrealized or 0.0))>1e-12:
        lines.append(f"Unrealized: ${_fmt_amount(unrealized)}")
    return "\n".join(lines)

def _iter_ledger_files_for_scope(scope:str, data_dir:str="/app/data", today:str=None):
    files=[]
    if scope=="today":
        files=[f"transactions_{today}.json" if today else None]
    elif scope=="month":
        pref=month_prefix()
        try:
            for fn in os.listdir(data_dir):
                if fn.startswith(f"transactions_{pref}") and fn.endswith(".json"): files.append(fn)
        except: pass
    else:
        try:
            for fn in os.listdir(data_dir):
                if fn.startswith("transactions_") and fn.endswith(".json"): files.append(fn)
        except: pass
    files=[os.path.join(data_dir,fn) for fn in files if fn]
    files.sort()
    return files

def _load_entries_for_totals(scope:str):
    from core.holdings import read_json  # reuse
    data_dir="/app/data"
    entries=[]
    today=None
    if scope=="today":
        from core.holdings import ymd
        today=ymd()
    for path in _iter_ledger_files_for_scope(scope, data_dir=data_dir, today=today):
        data=read_json(path, default=None)
        if not isinstance(data,dict): continue
        for e in data.get("entries",[]):
            sym=(e.get("token") or "?").upper()
            amt=float(e.get("amount") or 0.0)
            usd=float(e.get("usd_value") or 0.0)
            realized=float(e.get("realized_pnl") or 0.0)
            side="IN" if amt>0 else "OUT"
            entries.append({"asset":sym,"side":side,"qty":abs(amt),"usd":usd,"realized_usd":realized})
    return entries

def format_totals(scope:str):
    from core.holdings import _format_amount
    scope=(scope or "all").lower()
    rows=aggregate_per_asset(_load_entries_for_totals(scope))
    if not rows: return f"📊 Totals per Asset — {scope.capitalize()}: (no data)"
    lines=[f"📊 Totals per Asset — {scope.capitalize()}:"]
    for i,r in enumerate(rows,1):
        lines.append(
            f"{i}. {r['asset']}  "
            f"IN: {_format_amount(r['in_qty'])} (${_format_amount(r['in_usd'])}) | "
            f"OUT: {_format_amount(r['out_qty'])} (${_format_amount(r['out_usd'])}) | "
            f"REAL: ${_format_amount(r['realized_usd'])}"
        )
    totals_line = f"\nΣύνολο realized: ${_format_amount(sum(float(x['realized_usd']) for x in rows))}"
    lines.append(totals_line)
    return "\n".join(lines)

# ── Long poll ──────────────────────────────────────────────────────────────────
import requests

def _tg_api(method: str, **params):
    token=os.getenv("TELEGRAM_BOT_TOKEN") or ""
    url=f"https://api.telegram.org/bot{token}/{method}"
    try:
        r=requests.get(url, params=params, timeout=30)
        if r.status_code==200: return r.json()
    except Exception as e:
        log.debug("tg api error %s: %s", method, e)
    return None

def _handle_command(text: str):
    t=text.strip()
    low=t.lower()
    if low.startswith("/status"):
        send_telegram("✅ Running. Wallet monitor, Dex monitor, Alerts & Guard active.")
    elif low.startswith("/diag"):
        from core.discovery import _tracked_pairs
        from core.holdings import ymd
        send_telegram(
            "🔧 Diagnostics\n"
            f"WALLETADDRESS: {WALLET_ADDRESS}\n"
            f"TZ={os.getenv('TZ','Europe/Athens')} INTRADAYHOURS={os.getenv('INTRADAY_HOURS','3')} "
            f"EOD={int(os.getenv('EOD_HOUR','23')):02d}:{int(os.getenv('EOD_MINUTE','59')):02d}\n"
            f"Tracked pairs: {', '.join(sorted(_tracked_pairs)) or '(none)'}\n"
            f"Today: {ymd()}"
        )
    elif low.startswith("/rescan"):
        send_telegram("🔄 Rescan placeholder (RPC discovery moved to core/rpc if enabled).")
    elif low.startswith("/holdings") or low.startswith("/show_wallet_assets") or low.startswith("/showwalletassets") or low=="/show":
        send_telegram(_fmt_holdings_text())
    elif low.startswith("/dailysum") or low.startswith("/showdaily"):
        send_telegram(format_daily_sum_message())
    elif low.startswith("/report"):
        send_telegram(build_day_report_text())
    elif low.startswith("/totals"):
        parts=low.split()
        scope="all"
        if len(parts)>1 and parts[1] in ("today","month","all"):
            scope=parts[1]
        send_telegram(format_totals(scope))
    elif low.startswith("/totalstoday"):
        send_telegram(format_totals("today"))
    elif low.startswith("/totalsmonth"):
        send_telegram(format_totals("month"))
    elif low.startswith("/pnl"):
        parts=low.split()
        scope=parts[1] if len(parts)>1 and parts[1] in ("today","month","all") else "all"
        send_telegram(format_totals(scope))
    elif low.startswith("/watch "):
        try:
            _, rest = low.split(" ",1)
            if rest.startswith("add "):
                msg=handle_watch_command("add", rest.split(" ",1)[1].strip().lower())
                send_telegram(msg)
            elif rest.startswith("rm "):
                msg=handle_watch_command("rm",  rest.split(" ",1)[1].strip().lower())
                send_telegram(msg)
            elif rest.strip()=="list":
                msg=handle_watch_command("list","")
                send_telegram(msg)
            else:
                send_telegram("Usage: /watch add <cronos/pair> | /watch rm <cronos/pair> | /watch list")
        except Exception as e:
            send_telegram(f"Watch error: {e}")
    else:
        send_telegram("❓ Commands: /status /diag /rescan /holdings /show /dailysum /report /totals [today|month|all] /totalstoday /totalsmonth /pnl [scope] /watch ...")

def telegram_long_poll_loop():
    token=os.getenv("TELEGRAM_BOT_TOKEN") or ""
    if not token:
        log.warning("No TELEGRAM_BOT_TOKEN; telegram loop disabled."); return
    offset=None
    send_telegram("🤖 Telegram command handler online.")
    while True:
        try:
            resp=_tg_api("getUpdates", timeout=50, offset=offset, allowed_updates=json.dumps(["message"]))
            if not resp or not resp.get("ok"): time.sleep(2); continue
            for upd in resp.get("result",[]):
                offset = upd["update_id"] + 1
                msg=upd.get("message") or {}
                chat_id=str(((msg.get("chat") or {}).get("id") or ""))
                if TELEGRAM_CHAT_ID and str(TELEGRAM_CHAT_ID)!=chat_id:
                    continue
                text=(msg.get("text") or "").strip()
                if not text: continue
                _handle_command(text)
        except Exception as e:
            log.debug("telegram poll error: %s", e)
            time.sleep(2)

# expose small helper for external watchers if ever needed
def handle_external_watch_cmd(subcmd, arg):
    return handle_watch_command(subcmd, arg)
