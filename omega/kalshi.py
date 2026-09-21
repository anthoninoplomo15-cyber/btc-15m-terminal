"""Kalshi Trade API v2 client. RSA-PSS auth. Never deposit/withdraw/transfer/bank."""

from __future__ import annotations

import base64
import datetime as dt
from typing import Any
from urllib.parse import urlparse

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from omega import config

BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
CREATE_ORDER_PATH = "/portfolio/events/orders"
CANCEL_ORDER_PATH = "/portfolio/events/orders/{order_id}"
BALANCE_PATH = "/portfolio/balance"
POSITIONS_PATH = "/portfolio/positions"
ORDERS_PATH = "/portfolio/orders"

_BLOCKED = tuple(token.lower() for token in config.BLOCKED_PATH_TOKENS)
_ALLOWED = tuple(prefix.lower() for prefix in config.ALLOWED_PATH_PREFIXES)
_PRIVATE_KEY = None


class KalshiError(RuntimeError):
    pass


class ForbiddenEndpoint(KalshiError):
    pass


def _normalize_path(path: str) -> str:
    raw = (path or "").split("?", 1)[0]
    parsed = urlparse(raw if "://" in raw else "https://kalshi.local" + (raw if raw.startswith("/") else "/" + raw))
    cleaned = parsed.path or "/"
    while "//" in cleaned:
        cleaned = cleaned.replace("//", "/")
    # reject traversal
    parts = []
    for part in cleaned.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            raise ForbiddenEndpoint("blocked path traversal")
        parts.append(part)
    return "/" + "/".join(parts)


def _assert_no_money_tokens(blob: str) -> None:
    lowered = (blob or "").lower()
    for token in _BLOCKED:
        if token in lowered:
            raise ForbiddenEndpoint(
                f"blocked Kalshi path containing {token!r}"
            )


def _assert_safe_path(path: str) -> None:
    cleaned = _normalize_path(path)
    _assert_no_money_tokens(cleaned)
    lowered = cleaned.lower()
    allowed = False
    for prefix in _ALLOWED:
        if lowered == prefix or lowered.startswith(prefix + "/"):
            allowed = True
            break
    if not allowed:
        raise ForbiddenEndpoint(f"blocked Kalshi path not on allowlist: {cleaned}")


def _load_private_key():
    global _PRIVATE_KEY
    if _PRIVATE_KEY is not None:
        return _PRIVATE_KEY
    pem = config.private_key_pem()
    if not pem:
        raise KalshiError("Kalshi private key is not available")
    _PRIVATE_KEY = serialization.load_pem_private_key(
        pem.encode("utf-8"), password=None
    )
    return _PRIVATE_KEY


def reset_key_cache() -> None:
    global _PRIVATE_KEY
    _PRIVATE_KEY = None


def has_keys() -> bool:
    return config.keys_present()


def auth_headers(method: str, endpoint: str) -> dict[str, str]:
    timestamp = str(int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000))
    full_path = urlparse(BASE_URL + endpoint.split("?")[0]).path
    message = f"{timestamp}{method.upper()}{full_path}".encode("utf-8")
    signature = _load_private_key().sign(
        message,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH,
        ),
        hashes.SHA256(),
    )
    return {
        "KALSHI-ACCESS-KEY": config.api_key_id(),
        "KALSHI-ACCESS-TIMESTAMP": timestamp,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode("utf-8"),
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def optional_headers(method: str, endpoint: str) -> dict[str, str]:
    if not has_keys():
        return {"Accept": "application/json"}
    try:
        return auth_headers(method, endpoint)
    except Exception:
        return {"Accept": "application/json"}


def request(
    method: str,
    endpoint: str,
    *,
    params: dict | None = None,
    json: dict | None = None,
    timeout: float = 12,
    signed: bool | None = None,
) -> Any:
    _assert_safe_path(endpoint)
    if params:
        _assert_no_money_tokens(" ".join(str(k) for k in params.keys()))
    if json:
        _assert_no_money_tokens(" ".join(str(k) for k in json.keys()))
    use_signed = config.keys_present() if signed is None else signed
    if use_signed:
        headers = auth_headers(method, endpoint)
    else:
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
    response = requests.request(
        method.upper(),
        BASE_URL + endpoint,
        headers=headers,
        params=params,
        json=json,
        timeout=timeout,
    )
    if not response.ok:
        body = (response.text or "")[:240].replace("\n", " ")
        raise KalshiError(f"HTTP {response.status_code} {endpoint} {body}")
    if not response.content:
        return {}
    return response.json()


def _fp_num(value) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def funded_exchange_index(payload: dict | None = None) -> int:
    """15m crypto markets live on exchange 2. Do not follow a deposit on 0."""
    return 2


def get_balance() -> dict:
    payload = request("GET", BALANCE_PATH, signed=True)
    cash = None
    for row in payload.get("balance_breakdown") or []:
        if int(row.get("exchange_index") or 0) == 2:
            cash = _fp_num(row.get("balance"))
            break
    if cash is None:
        dollars = payload.get("balance_dollars")
        if dollars not in (None, ""):
            cash = float(dollars)
        else:
            cash = float(payload.get("balance") or 0) / 100.0
    return {
        "cash": round(float(cash), 4),
        "balance_cents": payload.get("balance"),
        "portfolio_value": payload.get("portfolio_value"),
        "updated_ts": payload.get("updated_ts"),
        "exchange_index": funded_exchange_index(payload),
        "raw_keys": sorted(str(key) for key in payload.keys()),
    }


def get_positions(ticker: str | None = None) -> list[dict]:
    params: dict[str, Any] = {"limit": 200, "count_filter": "position"}
    if ticker:
        params["ticker"] = ticker
    try:
        params["exchange_index"] = funded_exchange_index()
    except Exception:
        pass
    payload = request("GET", POSITIONS_PATH, params=params, signed=True)
    return list(payload.get("market_positions") or [])


def get_orders(status: str | None = None, ticker: str | None = None) -> list[dict]:
    params: dict[str, Any] = {"limit": 200}
    if status:
        params["status"] = status
    if ticker:
        params["ticker"] = ticker
    payload = request("GET", ORDERS_PATH, params=params, signed=True)
    return list(payload.get("orders") or [])


def _fp_count(contracts: float) -> str:
    return f"{float(contracts):.2f}"


def _fp_price(price: float) -> str:
    return f"{float(price):.4f}"


def book_side_and_yes_price(
    outcome_side: str,
    outcome_price: float,
    action: str = "buy",
) -> tuple[str, float]:
    side = str(outcome_side or "").lower()
    act = str(action or "buy").lower()
    price = round(float(outcome_price), 4)
    if side not in {"yes", "no"}:
        raise KalshiError(f"invalid outcome side {outcome_side!r}")
    if act == "buy":
        if side == "yes":
            return "bid", price
        return "ask", round(1.0 - price, 4)
    if act == "sell":
        if side == "yes":
            return "ask", price
        return "bid", round(1.0 - price, 4)
    raise KalshiError(f"invalid action {action!r}")


def create_order_ioc(
    *,
    ticker: str,
    outcome_side: str,
    contracts: float,
    outcome_price: float,
    client_order_id: str,
    reduce_only: bool = False,
    action: str = "buy",
) -> dict:
    """IOC limit order. Quotes the YES book: bid=buy YES, ask=sell YES."""
    book_side, yes_price = book_side_and_yes_price(
        outcome_side, outcome_price, action=action
    )
    body = {
        "ticker": ticker,
        "client_order_id": client_order_id,
        "side": book_side,
        "count": _fp_count(contracts),
        "price": _fp_price(yes_price),
        "time_in_force": "immediate_or_cancel",
        "self_trade_prevention_type": "taker_at_cross",
        "post_only": False,
        "reduce_only": bool(reduce_only),
        "exchange_index": funded_exchange_index(),
    }
    payload = request("POST", CREATE_ORDER_PATH, json=body, signed=True, timeout=15)
    blob = payload.get("order") if isinstance(payload.get("order"), dict) else payload
    fill = _fp_num(blob.get("fill_count"))
    if fill is None:
        fill = _fp_num(blob.get("fill_count_fp"))
    if fill is None:
        fill = _fp_num(payload.get("fill_count"))
    remaining = _fp_num(blob.get("remaining_count")) or _fp_num(blob.get("remaining_count_fp"))
    initial = _fp_num(blob.get("initial_count_fp")) or _fp_num(contracts)
    if (fill is None or fill == 0) and remaining is not None and initial is not None:
        inferred = round(initial - remaining, 4)
        if inferred > 0:
            fill = inferred
    avg = blob.get("average_fill_price") or payload.get("average_fill_price")
    return {
        "order_id": blob.get("order_id") or payload.get("order_id"),
        "client_order_id": blob.get("client_order_id") or payload.get("client_order_id") or client_order_id,
        "fill_count": fill,
        "remaining_count": remaining,
        "average_fill_price": None if avg in (None, "") else float(avg),
        "ts_ms": payload.get("ts_ms"),
        "book_side": book_side,
        "yes_price": yes_price,
        "outcome_side": str(outcome_side).lower(),
        "raw": {k: payload.get(k) for k in payload.keys() if k != "error"},
    }


def cancel_order(order_id: str, market_ticker: str | None = None) -> dict:
    path = CANCEL_ORDER_PATH.format(order_id=order_id)
    params = {}
    if market_ticker:
        params["market_ticker"] = market_ticker
        params["exchange_index"] = -1
    payload = request("DELETE", path, params=params or None, signed=True)
    return payload
