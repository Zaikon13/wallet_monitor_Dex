from core.guards import should_alert
class PriceWatcher:
    def poll_once(self): pass
    def _alert_price_move(self, sym, price, change_pct, meta):
        evt={'symbol':sym,'price_usd':price,'change_pct':change_pct,
             'volume24_usd':meta.get('vol24_usd'),'liquidity_usd':meta.get('liq_usd'),
             'is_new_pair':meta.get('is_new_pair',False),'spike_pct':meta.get('spike_pct')}
        if not should_alert(evt): return
        # send telegram upstream if integrated
def make_from_env(): return PriceWatcher()
