from core.guards import should_alert
from telegram.api import send_telegram_message
def ingest_signal(event):
    gated=should_alert(event)
    if not gated: return None
    sym=str(gated.get('symbol') or '?').upper(); action=gated.get('guard_action')
    price=gated.get('price_usd'); ch=gated.get('change_pct')
    emoji='🚀' if action=='BUY_MORE' else '⚠️'; suf='(momentum)' if action=='BUY_MORE' else '(risk/trailing)'
    send_telegram_message(f"{emoji} {sym} {ch}% @ ${price} → Guard: {action} {suf}")
    return gated
