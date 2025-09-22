import os
from utils.http import get_json

def _base()->str:
    return os.getenv("CRONOSCAN_BASE","https://api.cronoscan.com/api")

def _key()->str:
    return os.getenv("ETHERSCAN_API","")

def account_txlist(address, startblock=0, endblock=99999999, sort="asc"):
    url=_base()
    params={"module":"account","action":"txlist","address":address,"startblock":startblock,"endblock":endblock,"sort":sort,"apikey":_key()}
    return get_json(url, params=params)

def account_tokentx(address, startblock=0, endblock=99999999, sort="asc"):
    url=_base()
    params={"module":"account","action":"tokentx","address":address,"startblock":startblock,"endblock":endblock,"sort":sort,"apikey":_key()}
    return get_json(url, params=params)

def account_balance(address):
    url=_base()
    params={"module":"account","action":"balance","address":address,"tag":"latest","apikey":_key()}
    return get_json(url, params=params)

def token_balance(contract, address):
    url=_base()
    params={"module":"account","action":"tokenbalance","contractaddress":contract,"address":address,"tag":"latest","apikey":_key()}
    return get_json(url, params=params)
