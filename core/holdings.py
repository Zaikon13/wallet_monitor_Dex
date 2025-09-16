import os, requests
from decimal import Decimal
from typing import List, Dict

# Environment
CRONOS_RPC_URL = os.getenv("CRONOS_RPC_URL", "")
WALLET_ADDRESS = (os.getenv("WALLET_ADDRESS") or "").lower()
ETHERSCAN_API = os.getenv("ETHERSCAN_API", "")

# Web3 minimal
WEB3 = None
ERC20_ABI_MIN = [
    {"constant": True, "inputs": [], "name": "symbol", "outputs": [{"name": "", "type": "string"}], "type": "function"},
    {"constant": True, "inputs": [], "name": "decimals", "outputs": [{"name": "", "type": "uint8"}], "type": "function"},
    {"constant": True, "inputs": [{"name": "owner", "type": "address"}], "name": "balanceOf", "outputs": [{"name": "", "type": "uint256"}], "type": "function"},
]

def _w3_init():
    global WEB3
    if WEB3 is not None:
        return WEB3
    if not CRONOS_RPC_URL:
        return None
    try:
        from web3 import Web3
        WEB3 = Web3(Web3.HTTPProvider(CRONOS_RPC_URL, request_kwargs={"timeout": 15}))
        if WEB3.is_connected():
            return WEB3
    except Exception:
        WEB3 = None
    return WEB3

def _cs(addr: str) -> str:
    try:
        from web3 import Web3
        return Web3.to_checksum_address(addr)
    except Exception:
        return addr

def _erc20_symbol_decimals(addr: str) -> tuple[str, int]:
    w3 = _w3_init()
    if not w3:
        return (addr[:8].upper(), 18)
    try:
        contract = w3.eth.contract(address=_cs(addr), abi=ERC20_ABI_MIN)
        sym = contract.functions.symbol().call()
        dec = int(contract.functions.decimals().call())
        if isinstance(sym, bytes):
            sym = sym.decode("utf-8", "ignore").strip()
        return (sym or addr[:8].upper(), dec or 18)
    except Exception:
        return (addr[:8].upper(), 18)

def _erc20_balance(addr: str, owner: str) -> Decimal:
    w3 = _w3_init()
    if not w3:
        return Decimal("0")
    try:
        contract = w3.eth.contract(address=_cs(addr), abi=ERC20_ABI_MIN)
        raw = contract.functions.balanceOf(_cs(owner)).call()
        return Decimal(str(raw))
    except Exception:
        return Decimal("0")

def _native_balance(owner: str) -> Decimal:
    w3 = _w3_init()
    if not w3:
        return Decimal("0")
    try:
        wei = w3.eth.get_balance(_cs(owner))
        return Decimal(str(wei))
    except Exception:
        return Decimal("0")

DEX_BASE_TOKENS = "https://api.dexscreener.com/latest/dex/tokens"
DEX_BASE_SEARCH = "https://api.dexscreener.com/latest/dex/search"

def _pick_best_price(pairs: list) -> float:
    best_price = None
    best_liq = -1.0
    for p in pairs or []:
        try:
            if str(p.get("chainId", "")).lower() != "cronos":
                continue
            liq = float((p.get("liquidity") or {}).get("usd") or 0)
            price = float(p.get("priceUsd") or 0)
            if price <= 0:
                continue
            if liq > best_liq:
                best_liq = liq
                best_price = price
        except Exception:
            continue
    return best_price if best_price is not None else 0.0

def _price_usd_for(key: str) -> float:
    """
    Return current USD price for given token symbol or contract address via Dexscreener.
    """
    query = key.strip().lower()
    try:
        if query in ("cro", "wcro"):
            for q in ("wcro usdt", "wcro usdc", "cro usdt"):
                resp = requests.get(DEX_BASE_SEARCH, params={"q": q}, timeout=10)
                if resp.ok:
                    data = resp.json()
                    p = _pick_best_price(data.get("pairs") or [])
                    if p and p > 0:
                        return p
            return 0.0
        if query.startswith("0x") and len(query) == 42:
            resp = requests.get(f"{DEX_BASE_TOKENS}/cronos/{query}", timeout=10)
            if resp.ok:
                data = resp.json()
                p = _pick_best_price(data.get("pairs") or [])
                if p and p > 0:
                    return p
            resp = requests.get(DEX_BASE_SEARCH, params={"q": query}, timeout=10)
            if resp.ok:
                data = resp.json()
                p = _pick_best_price(data.get("pairs") or [])
                if p and p > 0:
                    return p
            return 0.0
        resp = requests.get(DEX_BASE_SEARCH, params={"q": query}, timeout=10)
        if resp.ok:
            data = resp.json()
            p = _pick_best_price(data.get("pairs") or [])
            if p and p > 0:
                return p
        for q in (f"{query} usdt", f"{query} wcro"):
            resp = requests.get(DEX_BASE_SEARCH, params={"q": q}, timeout=10)
            if resp.ok:
                data = resp.json()
                p = _pick_best_price(data.get("pairs") or [])
                if p and p > 0:
                    return p
        return 0.0
    except Exception:
        return 0.0

def get_wallet_snapshot(normalize_zero: bool = True, with_prices: bool = True) -> Dict[str, dict]:
    """
    Returns current wallet holdings snapshot as dict: {symbol: {"amount": Decimal, "price": float|None}}
    """
    addr = WALLET_ADDRESS
    if not addr:
        return {}
    snapshot_list = []
    # Native CRO balance
    wei = _native_balance(addr)
    if normalize_zero is False or wei > 0:
        cro_amt = (wei / Decimal(10**18)).quantize(Decimal("0.00000001"))
        price = _price_usd_for("cro") if with_prices else None
        usd_val = float(cro_amt) * float(price) if (price and cro_amt) else None
        snapshot_list.append({"symbol": "CRO", "token_addr": None, "amount": cro_amt, "price_usd": price, "usd_value": usd_val})
    # Gather token addresses from env and Etherscan
    token_addrs = set()
    tokens_env = [t.strip().lower() for t in os.getenv("TOKENS", "").split(",") if t.strip()]
    for t in tokens_env:
        if t.startswith("cronos/"):
            _, contract = t.split("/", 1)
            if contract.startswith("0x"):
                token_addrs.add(contract.lower())
        elif t.startswith("0x") and len(t) == 42:
            token_addrs.add(t.lower())
    try:
        if ETHERSCAN_API:
            url = "https://api.etherscan.io/v2/api"
            params = {
                "chainid": 25,
                "module": "account",
                "action": "tokentx",
                "address": addr,
                "startblock": 0,
                "endblock": 99999999,
                "page": 1,
                "offset": 100,
                "sort": "desc",
                "apikey": ETHERSCAN_API
            }
            resp = requests.get(url, params=params, timeout=15)
            if resp.status_code == 200:
                data = resp.json()
                if str(data.get("status", "")).strip() == "1":
                    for tx in data.get("result", []):
                        ca = (tx.get("contractAddress") or "").lower()
                        if ca.startswith("0x"):
                            token_addrs.add(ca)
    except Exception:
        pass
    # Fetch balances for each token
    for contract in sorted(token_addrs):
        if not (contract and contract.startswith("0x")):
            continue
        sym, dec = _erc20_symbol_decimals(contract)
        raw_bal = _erc20_balance(contract, addr)
        if raw_bal <= 0:
            continue
        amt = (raw_bal / Decimal(10**dec)).quantize(Decimal("0.00000001"))
        price = _price_usd_for(contract) if with_prices else None
        usd_val = float(amt) * float(price) if (price and amt) else None
        snapshot_list.append({"symbol": sym or contract[:8].upper(), "token_addr": contract, "amount": amt, "price_usd": price, "usd_value": usd_val})
    # Sort by USD value (descending)
    snapshot_list.sort(key=lambda x: float(x.get("usd_value") or 0), reverse=True)
    # Convert to dict
    snapshot: Dict[str, dict] = {}
    for item in snapshot_list:
        sym = str(item["symbol"]) if item["symbol"] else "TOKEN"
        key = sym.upper()
        if key in snapshot:
            # Differentiate duplicate symbols
            i = 2
            while f"{key}{i}" in snapshot:
                i += 1
            key = f"{key}{i}"
        snapshot[key] = {"amount": item["amount"], "price": item["price_usd"]}
    return snapshot
