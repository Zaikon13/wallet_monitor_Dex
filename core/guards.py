from decimal import Decimal
import os, time
from typing import Optional, Dict, Any, Set
GUARD_PUMP_PCT=Decimal(os.getenv('GUARD_PUMP_PCT','20'))
GUARD_DROP_PCT=Decimal(os.getenv('GUARD_DROP_PCT','-12'))
GUARD_TRAIL_DROP_PCT=Decimal(os.getenv('GUARD_TRAIL_DROP_PCT','-8'))
MIN_VOLUME=Decimal(os.getenv('MIN_VOLUME_FOR_ALERT','0'))
MIN_LIQ=Decimal(os.getenv('DISCOVER_MIN_LIQ_USD','0'))
SPIKE=Decimal(os.getenv('SPIKE_THRESHOLD','8'))
COOL=max(10,int(os.getenv('ALERTS_INTERVAL_MINUTES','0') or 0)*60) if os.getenv('ALERTS_INTERVAL_MINUTES') else 30
_last={}; peaks={}; traded:Set[str]=set(); holdings:Set[str]=set()
def mark_trade(symbol:str, side:str): traded.add(symbol.upper())
def set_holdings(symbols:set[str]): 
    global holdings; holdings={s.upper() for s in (symbols or set())}
def _cool_ok(sym:str)->bool:
    now=time.time(); last=_last.get(sym,0.0)
    if now-last<COOL: return False
    _last[sym]=now; return True
def should_alert(ev: Dict[str, Any]):
    sym=str(ev.get('symbol') or '').upper()
    if not sym: return None
    price=ev.get('price_usd'); chg=ev.get('change_pct'); vol=ev.get('volume24_usd'); liq=ev.get('liquidity_usd')
    is_new=bool(ev.get('is_new_pair', False)); spike=ev.get('spike_pct')
    if vol is not None and vol<MIN_VOLUME: return None
    if liq is not None and liq<MIN_LIQ: return None
    in_scope = sym in holdings or sym in traded or (is_new and spike is not None and spike>=SPIKE)
    if not in_scope: return None
    if price is not None:
        pk=peaks.get(sym); 
        if pk is None or price>pk: peaks[sym]=price
    action=None
    if chg is not None:
        if chg>=GUARD_PUMP_PCT: action='BUY_MORE'
        if chg<=GUARD_DROP_PCT: action='SELL'
    if action is None and price is not None:
        pk=peaks.get(sym)
        if pk and pk>0:
            dd=(price-pk)/pk*100
            if dd<=GUARD_TRAIL_DROP_PCT: action='SELL'
    if not action or not _cool_ok(sym): return None
    ev=dict(ev); ev['guard_action']=action; return ev
