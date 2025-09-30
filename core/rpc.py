# core/rpc.py
# Minimal Cronos RPC + ERC20 helpers

from __future__ import annotations
import os, json
from typing import Any, Dict, List

try:
    from utils.http import post_json as _post_json  # type: ignore
except Exception:
    import requests
    def _post_json(url: str, payload: Dict[str, Any], timeout: int = 15) -> tuple[int, str]:
        r = requests.post(url, json=payload, timeout=timeout)
        return r.status_code, r.text

RPC_URL = os.getenv("RPC_URL") or os.getenv("CRONOS_RPC_URL") or "https://cronos-evm-rpc.publicnode.com"

def _rpc(method: str, params: List[Any]) -> Any:
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    status, text = _post_json(RPC_URL, payload, timeout=20)
    if status < 200 or status >= 300:
        raise RuntimeError(f"RPC {method} HTTP {status}: {text[:200]}")
    data = json.loads(text)
    if "error" in data:
        raise RuntimeError(f"RPC {method} error: {data['error']}")
    return data.get("result")

def eth_get_balance(address: str, block: str = "latest") -> int:
    res = _rpc("eth_getBalance", [address, block])
    return int(res, 16)

def eth_call(to: str, data: str, block: str = "latest") -> bytes:
    call = {"to": to, "data": data}
    res = _rpc("eth_call", [call, block])
    return bytes.fromhex(res[2:]) if isinstance(res, str) and res.startswith("0x") else b""

def _addr_noprefix(addr: str) -> str: return addr.lower().replace("0x", "")
def _pad32(h: str) -> str: return h.rjust(64, "0")
def _selector(sig4: str) -> str: return "0x" + sig4

def erc20_balance_of(token: str, owner: str) -> int:
    data = _selector("70a08231") + _pad32(_addr_noprefix(owner))
    raw = eth_call(token, data)
    return int.from_bytes(raw, "big") if raw else 0

def erc20_decimals(token: str) -> int:
    data = _selector("313ce567")
    raw = eth_call(token, data)
    return int.from_bytes(raw[-32:], "big") if raw else 18

def erc20_symbol(token: str) -> str:
    data = _selector("95d89b41")
    raw = eth_call(token, data)
    if not raw: return "TKN"
    try:
        if len(raw) >= 96:
            strlen = int.from_bytes(raw[64:96], "big")
            return raw[96:96+strlen].decode("utf-8", errors="ignore") or "TKN"
        return raw.rstrip(b"\x00").decode("utf-8", errors="ignore") or "TKN"
    except Exception:
        return "TKN"
