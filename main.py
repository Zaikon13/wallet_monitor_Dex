# main.py - Cronos Wallet Monitor + Intraday/Daily PnL Reports
# Author: ChatGPT
# Requirements: requests, python-telegram-bot, python-dotenv

import os
import time
import requests
import threading
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from dotenv import load_dotenv
from telegram import Bot

# === Load env ===
load_dotenv()
CRONOSCAN_API_KEY = os.getenv("CRONOSCAN_API_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
WALLET_ADDRESS = os.getenv("WALLET_ADDRESS")

bot = Bot(token=TELEGRAM_TOKEN)

# === State ===
holdings = {}
transactions_today = []
pnl_month = Decimal("0.0")
today_date = datetime.now(timezone.utc).date()

# === Helpers ===
def fetch_price(symbol):
    url = f"https://api.dexscreener.com/latest/dex/tokens/{symbol}"
    try:
        r = requests.get(url).json()
        return Decimal(str(r["pairs"][0]["priceUsd"]))
    except Exception:
        return Decimal("0")

def fetch_transactions():
    """Fetches latest token transfers from CronosScan"""
    url = f"https://api.cronoscan.com/api?module=account&action=tokentx&address={WALLET_ADDRESS}&sort=desc&apikey={CRONOSCAN_API_KEY}"
    r = requests.get(url).json()
    return r.get("result", [])

def update_holdings(txns):
    global holdings, transactions_today
    for tx in txns:
        ts = datetime.utcfromtimestamp(int(tx["timeStamp"])).replace(tzinfo=timezone.utc)
        token = tx["tokenSymbol"]
        amount = Decimal(tx["value"]) / (Decimal(10) ** int(tx["tokenDecimal"]))
        direction = "IN" if tx["to"].lower() == WALLET_ADDRESS.lower() else "OUT"

        # update balances
        if token not in holdings:
            holdings[token] = Decimal("0.0")
        if direction == "IN":
            holdings[token] += amount
        else:
            holdings[token] -= amount

        # log only today's tx
        if ts.date() == datetime.now(timezone.utc).date():
            transactions_today.append({
                "time": ts.strftime("%H:%M:%S"),
                "token": token,
                "amount": amount if direction == "IN" else -amount
            })

def compute_valuations():
    values = {}
    total = Decimal("0.0")
    for token, amount in holdings.items():
        # crude mapping for test: (replace with real token addresses)
        if token.upper() == "CRO":
            price = fetch_price("0x5c7f8a570d578ed84e63fd37ee6a9c7b5a3f7d35")
        elif token.upper() == "USDT":
            price = Decimal("1.0")
        else:
            price = Decimal("0.01")  # fallback
        val = amount * price
        values[token] = (amount, price, val)
        total += val
    return values, total

# === Reporting ===
def intraday_report():
    values, total = compute_valuations()
    msg = "🟡 Intraday Update\n"
    msg += f"Time: {datetime.now().strftime('%H:%M:%S')}\n"
    msg += f"Holdings value now: ${total:.2f}\n"
    for token, (amt, price, val) in values.items():
        msg += f"  – {token}: {amt:.4f} @ ${price:.4f} = ${val:.2f}\n"
    bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=msg)

def daily_report():
    global transactions_today, pnl_month, today_date
    values, total = compute_valuations()
    msg = f"📒 Daily Report ({today_date})\n"
    if not transactions_today:
        msg += "No transactions today.\n"
    else:
        for tx in transactions_today:
            msg += f"[{tx['time']}] {tx['token']} {tx['amount']:.4f}\n"

    # daily PnL = today value – yesterday (here simplified, just show current total)
    daily_pnl = total
    pnl_month += daily_pnl

    msg += f"\nNet PnL (USDT) today: ${daily_pnl:.2f}\n"
    msg += f"Holdings value now: ${total:.2f}\n"
    for token, (amt, price, val) in values.items():
        msg += f"  – {token}: {amt:.4f} @ ${price:.4f} = ${val:.2f}\n"
    msg += f"\nMonth PnL (USDT): ${pnl_month:.2f}"

    bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=msg)
    transactions_today = []  # reset for next day
    today_date = datetime.now(timezone.utc).date()

# === Schedulers ===
def intraday_scheduler():
    while True:
        intraday_report()
        time.sleep(3 * 60 * 60)  # every 3h

def daily_scheduler():
    while True:
        now = datetime.now()
        if now.hour == 23 and now.minute == 59:
            daily_report()
            time.sleep(60)
        time.sleep(30)

# === Main ===
def main():
    # kick off schedulers
    threading.Thread(target=intraday_scheduler, daemon=True).start()
    threading.Thread(target=daily_scheduler, daemon=True).start()

    # poll transactions
    seen = set()
    while True:
        txns = fetch_transactions()
        new = [t for t in txns if t["hash"] not in seen]
        if new:
            update_holdings(new)
            for t in new:
                seen.add(t["hash"])
        time.sleep(30)

if __name__ == "__main__":
    main()
