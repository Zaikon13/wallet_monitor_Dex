from core.tz import ymd
from reports.ledger import append_ledger
from core.guards import mark_trade
from telegram.api import send_telegram_message
import time
class WalletMonitor:
    def __init__(self,wallet,fetch_fn,cooldown_sec=5):
        self.wallet=wallet; self.fetch_fn=fetch_fn; self.cooldown=cooldown_sec; self._seen=set(); self._last=0
    def _dedup(self,txs):
        out=[]; 
        for e in txs:
            k=str(e.get('txid') or '')
            if not k or k in self._seen: continue
            self._seen.add(k); out.append(e)
        return out
    def _record_alert(self, e):
        day=ymd()
        if (e.get('side') or '').upper()=='SWAP':
            legs=e.get('legs') or []
            for l in legs:
                entry={'wallet': self.wallet,'time': e.get('time'),
                       'side': (l.get('side') or '').upper(),'asset': (l.get('asset') or '?').upper(),
                       'qty': l.get('qty'),'price_usd': l.get('price_usd'),'usd': l.get('usd'),'realized_usd':'0'}
                append_ledger(day, entry)
                try: mark_trade(entry['asset'], entry['side'])
                except Exception: pass
            parts=[f"{(l.get('side') or '').upper()} {(l.get('asset') or '?').upper()} {l.get('qty')}" for l in legs]
            send_telegram_message('🔁 Swap: ' + ' | '.join(parts))
        else:
            entry={'wallet': self.wallet,'time': e.get('time'),
                   'side': (e.get('side') or 'IN').upper(),'asset': (e.get('asset') or '?').upper(),
                   'qty': e.get('qty'),'price_usd': e.get('price_usd'),'usd': e.get('usd'),'realized_usd':'0'}
            append_ledger(day, entry)
            try: mark_trade(entry['asset'], entry['side'])
            except Exception: pass
            send_telegram_message(f"{('➕' if entry['side']=='IN' else '➖')} {entry['side']} {entry['asset']} {entry['qty']}")
    def poll_once(self):
        raw=self.fetch_fn(self.wallet) or []
        fresh=self._dedup(raw)
        now=time.time()
        for e in fresh:
            if now - self._last < self.cooldown: continue
            self._last = now
            self._record_alert(e)
        return len(fresh)
def make_wallet_monitor(provider=None):
    import os
    wallet=os.getenv('WALLET_ADDRESS','')
    if not provider: provider=lambda _addr: []
    cd=max(3, int(os.getenv('WALLET_ALERTS_COOLDOWN','5') or 5))
    return WalletMonitor(wallet, provider, cd)
