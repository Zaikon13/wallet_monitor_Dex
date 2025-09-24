import os, requests, logging
TOKEN=os.getenv("TELEGRAM_BOT_TOKEN","").strip()
CHAT_ID=os.getenv("TELEGRAM_CHAT_ID","").strip()
def send_telegram_message(text:str)->None:
    if not TOKEN or not CHAT_ID:
        logging.info("[telegram] %s", text); return
    url=f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    try: requests.post(url, data={"chat_id":CHAT_ID,"text":text}, timeout=10)
    except Exception as e: logging.debug("telegram send failed: %s", e)
