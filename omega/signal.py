"""Motor matematico PAPER de Proyecto 2, version 10 OMEGA Impulso.

La estrategia exige tres de cuatro confirmaciones: momentum, presion taker,
libro de ordenes y flujo relativo de Kalshi. Arriesga como maximo $1,
incluyendo la tarifa de entrada, no usa stop loss y activa un trailing de 2
centavos cuando el valor de venta neto alcanza $1.05.

Este modulo no contiene endpoints para crear, cancelar o modificar ordenes.
"""

import math


FEE_RATE = 0.07
MAX_TOTAL_COST = 1.00
TRAIL_ARM_NET_PROCEEDS = 1.05
TRAIL_DROP = 0.02
CONTRACT_STEP = 0.01
PRICE_STEP = 0.01
MIN_CONFIRMING_VOTES = 3
TAKER_BUY_THRESHOLD = 0.58
ORDERBOOK_BID_THRESHOLD = 0.55
KALSHI_YES_THRESHOLD = 0.60
MAX_SPREAD = 0.05


def _number(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _share_vote(value, yes_threshold):
    share = _number(value)
    if share is None or not 0 <= share <= 1:
        return None
    if share + 1e-12 >= yes_threshold:
        return "yes"
    if share <= 1 - yes_threshold + 1e-12:
        return "no"
    return None


def omega_signal(
    momentum_1m,
    momentum_5m,
    taker_buy_share,
    orderbook_bid_share,
    kalshi_yes_share,
    spread,
):
    """Combina las cuatro confirmaciones de OMEGA sin colocar ordenes."""
    one_minute = _number(momentum_1m)
    five_minutes = _number(momentum_5m)
    market_spread = _number(spread)

    momentum_vote = None
    if one_minute is not None and five_minutes is not None:
        if one_minute > 0 and five_minutes > 0:
            momentum_vote = "yes"
        elif one_minute < 0 and five_minutes < 0:
            momentum_vote = "no"

    votes = {
        "momentum": momentum_vote,
        "taker_pressure": _share_vote(
            taker_buy_share, TAKER_BUY_THRESHOLD
        ),
        "orderbook": _share_vote(
            orderbook_bid_share, ORDERBOOK_BID_THRESHOLD
        ),
        "kalshi_flow": _share_vote(
            kalshi_yes_share, KALSHI_YES_THRESHOLD
        ),
    }
    yes_votes = sum(vote == "yes" for vote in votes.values())
    no_votes = sum(vote == "no" for vote in votes.values())

    side = None
    reason = "OMEGA sin 3 confirmaciones"
    if market_spread is None:
        reason = "Spread ejecutable no disponible"
    elif market_spread > MAX_SPREAD + 1e-12:
        reason = f"Spread mayor de {MAX_SPREAD * 100:.0f} centavos"
    elif yes_votes >= MIN_CONFIRMING_VOTES and yes_votes > no_votes:
        side = "yes"
        reason = f"OMEGA {yes_votes}/4 confirma UP"
    elif no_votes >= MIN_CONFIRMING_VOTES and no_votes > yes_votes:
        side = "no"
        reason = f"OMEGA {no_votes}/4 confirma DOWN"

    return {
        "omega_side": side,
        "omega_votes": max(yes_votes, no_votes),
        "omega_yes_votes": yes_votes,
        "omega_no_votes": no_votes,
        "omega_vote_details": votes,
        "omega_reason": reason,
        "spread": None if market_spread is None else round(market_spread, 4),
    }


def taker_fee(contracts, price):
    """Tarifa taker estimada, redondeada hacia arriba al centavo."""
    quantity = _number(contracts)
    contract_price = _number(price)
    if (
        quantity is None
        or contract_price is None
        or quantity <= 0
        or not 0 < contract_price < 1
    ):
        return 0.0
    raw_fee = FEE_RATE * quantity * contract_price * (1 - contract_price)
    return math.ceil((raw_fee - 1e-12) * 100) / 100


def entry_cost(contracts, price):
    quantity = _number(contracts)
    contract_price = _number(price)
    if quantity is None or contract_price is None:
        return None
    return round(quantity * contract_price + taker_fee(quantity, contract_price), 4)


def net_pnl(contracts, entry_price, exit_price):
    """P&L neto despues de la tarifa de entrada y la tarifa de salida."""
    quantity = _number(contracts)
    buy_price = _number(entry_price)
    sell_price = _number(exit_price)
    if (
        quantity is None
        or buy_price is None
        or sell_price is None
        or quantity <= 0
        or not 0 < buy_price < 1
        or not 0 < sell_price < 1
    ):
        return None
    cost = entry_cost(quantity, buy_price)
    proceeds = quantity * sell_price - taker_fee(quantity, sell_price)
    return round(proceeds - cost, 4)


def net_proceeds(contracts, exit_price):
    """Valor recibido si se vende ahora, despues de la tarifa de salida."""
    quantity = _number(contracts)
    sell_price = _number(exit_price)
    if (
        quantity is None
        or sell_price is None
        or quantity <= 0
        or not 0 < sell_price < 1
    ):
        return None
    return round(quantity * sell_price - taker_fee(quantity, sell_price), 4)


def affordable_contracts(entry_price, max_total=MAX_TOTAL_COST):
    """Mayor cantidad en pasos de 0.01 cuyo costo total no supera el limite."""
    price = _number(entry_price)
    limit = _number(max_total)
    if price is None or limit is None or not 0 < price < 1 or limit <= 0:
        return None

    max_units = math.floor((limit / price) / CONTRACT_STEP + 1e-9)
    for units in range(max_units, 0, -1):
        quantity = round(units * CONTRACT_STEP, 2)
        cost = entry_cost(quantity, price)
        if cost is not None and cost <= limit + 1e-9:
            return quantity
    return None


def first_arm_price(
    contracts,
    arm_net_proceeds=TRAIL_ARM_NET_PROCEEDS,
):
    """Primer bid en centavos cuyo valor neto activa el trailing."""
    quantity = _number(contracts)
    arm_value = _number(arm_net_proceeds)
    if quantity is None or arm_value is None:
        return None

    for cents in range(1, 100):
        exit_price = round(cents * PRICE_STEP, 2)
        proceeds = net_proceeds(quantity, exit_price)
        if proceeds is not None and proceeds + 1e-9 >= arm_value:
            return exit_price
    return None


def build_entry_plan(
    side,
    entry_price,
    max_total=MAX_TOTAL_COST,
    arm_net_proceeds=TRAIL_ARM_NET_PROCEEDS,
    trail_drop=TRAIL_DROP,
    omega_votes=None,
):
    """Crea un plan PAPER despues de una confirmacion OMEGA valida."""
    normalized_side = str(side or "").lower()
    price = _number(entry_price)
    vote_count = _number(omega_votes)
    if normalized_side not in {"yes", "no"}:
        return {"action": "WAIT", "reason": "Lado invalido"}
    if price is None or not 0 < price < 1:
        return {"action": "WAIT", "reason": "Ask ejecutable no disponible"}
    if vote_count is None or vote_count < MIN_CONFIRMING_VOTES:
        return {"action": "WAIT", "reason": "OMEGA sin 3 confirmaciones"}

    quantity = affordable_contracts(price, max_total=max_total)
    if quantity is None:
        return {"action": "WAIT", "reason": "No cabe dentro del limite de $1"}

    cost = entry_cost(quantity, price)
    arm_price = first_arm_price(quantity, arm_net_proceeds=arm_net_proceeds)

    return {
        "action": "PAPER_BUY_" + normalized_side.upper(),
        "side": normalized_side,
        "contracts": quantity,
        "entry_price": round(price, 4),
        "entry_fee": taker_fee(quantity, price),
        "cost": cost,
        "trail_arm_net_proceeds": round(float(arm_net_proceeds), 4),
        "trail_drop": round(float(trail_drop), 4),
        "omega_votes": int(vote_count),
        "estimated_arm_price": arm_price,
        "stop_loss": None,
        "hold_if_never_armed": True,
        "reason": (
            f"OMEGA {int(vote_count)}/4 confirma "
            + ("UP" if normalized_side == "yes" else "DOWN")
        ),
    }
