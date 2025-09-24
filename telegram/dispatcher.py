from telegram.commands import handle_holdings, handle_show, handle_showdaily, handle_weekly
import time
ALIASES={'/holdings':handle_holdings,'/show':handle_show,'/status':handle_show,'/report':handle_showdaily,'/showdaily':handle_showdaily,'/weekly':handle_weekly}
_last={}; COOL=5
def dispatch(text, chat_id=None):
    if not text: return None
    cmd=text.strip().split()[0].lower()
    fn=ALIASES.get(cmd); 
    if not fn: return None
    now=time.time(); k=(cmd,int(chat_id) if chat_id else None)
    if now-_last.get(k,0)<COOL: return "⌛ cooldown"
    _last[k]=now
    if fn is handle_weekly and len(text.split())>1:
        try: days=max(1,min(31,int(text.split()[1])))
        except: days=7
        return fn(days=days)
    return fn()
