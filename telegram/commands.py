from core.holdings import get_wallet_snapshot
from reports.day_report import build_day_report_text
from reports.weekly import build_weekly_report_text

def handle_holdings()->str:
    snap=get_wallet_snapshot()
    if not snap: return "No holdings available."
    lines=["Holdings snapshot:",""]
    for a,info in snap.items():
        qty=info.get("qty","?"); px=info.get("price_usd"); usd=info.get("usd")
        lines.append(f"  – {a}: {qty}" + (f" @ ${px} = ${usd}" if px is not None and usd is not None else ""))
    return "\n".join(lines)

def handle_show()->str: return build_day_report_text(True)
def handle_showdaily()->str: return build_day_report_text(False)
def handle_weekly(days:int=7)->str: return build_weekly_report_text(days=days)
