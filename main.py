import os
import time
import requests
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(message)s")

# Load environment variables
WALLET_ADDRESS = os.getenv("WALLET_ADDRESS") TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID") ETHERSCAN_API = os.getenv("ETHERSCAN_API") DEX_PAIRS = os.getenv("DEX_PAIRS", "") PRICE_MOVE_THRESHOLD = float(os.getenv("PRICE_MOVE_THRESHOLD", "5.0"))

# API endpoints
CRONOSCAN_API_URL = "https://api.cronoscan.com/api"
DEXSCREENER_URL = "https://api.dexscreener.com/latest/dex/pairs"

# Track last transactions and prices
last_seen_tx = set()
last_prices = {}

def send_telegram_message(message):
    """Send a message to Telegram bot."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message}
    try:
        requests.post(url, data=payload, timeout=10)
    except Exception as e:
        logging.error(f"Telegram send error: {e}")

def check_wallet_transactions():
    """Check new wallet transactions on Cronoscan."""
    global last_seen_tx
    try:
        url = (f"{CRONOSCAN_API_URL}?module=account&action=txlist"
               f"&address={WALLET_ADDRESS}&startblock=0&endblock=99999999"
               f"&sort=desc&apikey={ETHERSCAN_API}")
        resp = requests.get(url, timeout=10)
        data = resp.json()

        if data.get("status") != "1" or "result" not in data:
            logging.warning(f"Cronoscan API returned no txs: {data}")
            return

        for tx in data["result"]:
            tx_hash = tx["hash"]
            if tx_hash not in last_seen_tx:
                last_seen_tx.add(tx_hash)
                msg = f"New tx on {WALLET_ADDRESS}:\nHash: {tx_hash}\nValue: {int(tx['value'])/1e18} CRO"
                send_telegram_message(msg)

 # Keep last 50 txs
        if len(last_seen_tx) > 50:
            last_seen_tx = set(list(last_seen_tx)[-50:])
    except Exception as e:
        logging.error(f"Wallet check error: {e}")

def check_dex_pairs():
    """Check DEX pairs price changes."""
    global last_prices
    if not DEX_PAIRS:
        return
    pairs = [p.strip() for p in DEX_PAIRS.split(",")]
    for pair in pairs:
        try:
            resp = requests.get(f"{DEXSCREENER_URL}/{pair}", timeout=10)
            data = resp.json()

            # Dexscreener sometimes returns 'pairs' list
            if "pairs" in data and isinstance(data["pairs"], list) and data["pairs"]:
                pair_data = data["pairs"][0]
                price_usd = float(pair_data.get("priceUsd", 0))
                symbol = pair_data.get("baseToken", {}).get("symbol", "?")

                if price_usd and symbol:
                    last_price = last_prices.get(pair)
                    if last_price:
                        change_pct = ((price_usd - last_price) / last_price) * 100
                        if abs(change_pct) >= PRICE_MOVE_THRESHOLD:
                            send_telegram_message(
                                f"{symbol} price moved {change_pct:.2f}% → ${price_usd:.4f}"
                            )
                    last_prices[pair] = price_usd
            else:
                logging.warning(f"Unexpected dexscreener format for {pair}: {data}")
        except Exception as e:
            logging.error(f"DEX check error for {pair}: {e}")

if __name__ == "__main__":
    logging.info("Starting monitor with config:")
    logging.info(f"WALLET_ADDRESS: {WALLET_ADDRESS}")
    logging.info(f"TELEGRAM_BOT_TOKEN present: {bool(TELEGRAM_BOT_TOKEN)}")
    logging.info(f"TELEGRAM_CHAT_ID: {TELEGRAM_CHAT_ID}")
    logging.info(f"ETHERSCAN_API present: {bool(ETHERSCAN_API)}")
    logging.info(f"DEX_PAIRS: {DEX_PAIRS}")

send_telegram_message("📡 Cronos monitor started!")

  while True:
        check_wallet_transactions()
        check_dex_pairs()
        time.sleep(30)
