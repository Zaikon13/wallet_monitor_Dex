from core.pricing import _map_from_env

def test_map_from_env():
    import os
    os.environ["TOKENS_ADDRS"]="USDT=0xabc,WETH=0xdef"
    m=_map_from_env("TOKENS_ADDRS")
    assert m["USDT"]=="0xabc" and m["WETH"]=="0xdef"
