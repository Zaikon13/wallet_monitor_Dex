# reports/ledger.py
from collections import defaultdict

def update_cost_basis(pos_qty: dict, pos_cost: dict, token_key: str,
                      signed_amount: float, price_usd: float,
                      eps: float = 1e-12) -> float:
    """
    Εφαρμόζει μία κίνηση (signed_amount) στο cost-basis για το token_key.
    Μεταβάλλει in-place τα pos_qty/pos_cost και επιστρέφει realized PnL της κίνησης.
    """
    qty = float(pos_qty.get(token_key, 0.0))
    cost = float(pos_cost.get(token_key, 0.0))
    realized = 0.0
    p = float(price_usd or 0.0)

    if signed_amount > eps:
        buy_qty = float(signed_amount)
        pos_qty[token_key] = qty + buy_qty
        pos_cost[token_key] = cost + buy_qty * p
    elif signed_amount < -eps:
        sell_req = -float(signed_amount)
        if qty > eps:
            sell_qty = min(sell_req, qty)
            avg_cost = (cost / qty) if qty > eps else p
            realized = (p - avg_cost) * sell_qty
            pos_qty[token_key] = qty - sell_qty
            pos_cost[token_key] = max(0.0, cost - avg_cost * sell_qty)
        else:
            realized = 0.0
    # clamp σχεδόν-μηδενικά
    if abs(pos_qty.get(token_key, 0.0)) < 1e-10:
        pos_qty[token_key] = 0.0
    if abs(pos_cost.get(token_key, 0.0)) < 1e-10:
        pos_cost[token_key] = 0.0
    return float(realized)


def replay_cost_basis_over_entries(entries: list, eps: float = 1e-12):
    """
    Ξαναπαίζει ΟΛΕΣ τις entries (όπως είναι δοσμένες) υπολογίζοντας από την αρχή:
      - pos_qty, pos_cost
      - συνολικό realized
      - ενημερωμένες entries με realized_pnl ανά κίνηση

    Δεν κάνει I/O — επιστρέφει τα πάντα στον caller.
    """
    pos_qty = defaultdict(float)
    pos_cost = defaultdict(float)
    total_realized = 0.0
    updated_entries = []

    for e in entries or []:
        # token_key: προτιμάμε το token_addr, αλλιώς 'CRO' για native, αλλιώς symbol
        sym = (e.get("token") or "").strip()
        addr = (e.get("token_addr") or "") or None
        amt = float(e.get("amount") or 0.0)
        price = float(e.get("price_usd") or 0.0)

        if addr and isinstance(addr, str) and addr.lower().startswith("0x"):
            key = addr.lower()
        elif sym.upper() == "CRO":
            key = "CRO"
        else:
            key = sym or "?"

        realized = update_cost_basis(pos_qty, pos_cost, key, amt, price, eps=eps)
        e2 = dict(e)
        e2["realized_pnl"] = float(realized)
        updated_entries.append(e2)
        total_realized += float(realized)

    return pos_qty, pos_cost, float(total_realized), updated_entries
