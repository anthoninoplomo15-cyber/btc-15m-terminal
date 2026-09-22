"""Public market scan: all Kalshi 15m series + Binance/Pyth/Yahoo spots."""

from __future__ import annotations

import datetime as dt
import math
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

from omega.kalshi import BASE_URL, ForbiddenEndpoint, _assert_no_money_tokens, _assert_safe_path, optional_headers
from omega.signal import (
    KALSHI_YES_THRESHOLD,
    MAX_SPREAD,
    MIN_CONFIRMING_VOTES,
    ORDERBOOK_BID_THRESHOLD,
    TAKER_BUY_THRESHOLD,
    build_entry_plan,
    omega_signal,
)

BINANCE_BASE_URL = "https://data-api.binance.vision"
BINANCE_FUTURES_BASE_URL = "https://fapi.binance.com"
BINANCE_CACHE_SECONDS = 10
INTERVAL_SECONDS = 15 * 60
FLOW_WINDOW_SECONDS = 5 * 60
ENTRY_WINDOW_MIN = 5
ENTRY_WINDOW_MAX = 70

# Seed list; refreshed from Kalshi /series (fifteen_min) each process start / refresh.
SERIES = [
    "KXBTC15M", "KXETH15M", "KXSOL15M", "KXXRP15M",
    "KXDOGE15M", "KXBNB15M", "KXADA15M", "KXBCH15M",
    "KXHYPE15M", "KXNEAR15M", "KXTON15M", "KXZEC15M",
    "KXGOLD15M", "KXSILVER15M", "KXWTI15M", "KXCOPPER15M",
    "KXNATGAS15M", "KXPLATINUM15M", "KXPALLADIUM15M",
]

BINANCE_SYMBOLS = {
    "KXBTC15M": "BTCUSDT",
    "KXETH15M": "ETHUSDT",
    "KXSOL15M": "SOLUSDT",
    "KXXRP15M": "XRPUSDT",
    "KXDOGE15M": "DOGEUSDT",
    "KXBNB15M": "BNBUSDT",
    "KXADA15M": "ADAUSDT",
    "KXLINK15M": "LINKUSDT",
    "KXAVAX15M": "AVAXUSDT",
    "KXLTC15M": "LTCUSDT",
    "KXBCH15M": "BCHUSDT",
    "KXDOT15M": "DOTUSDT",
    "KXHYPE15M": "HYPEUSDT",
    "KXSUI15M": "SUIUSDT",
    "KXNEAR15M": "NEARUSDT",
    "KXTON15M": "TONUSDT",
    "KXZEC15M": "ZECUSDT",
}
BINANCE_FUTURES_SERIES = {"KXHYPE15M"}

# Pyth explore symbols matching Kalshi settlement_sources (preferred for metals/commodities).
PYTH_SYMBOLS = {
    "KXGOLD15M": "Metal.Index.1OZGOLD/USD",
    "KXSILVER15M": "Metal.Index.SILVER/USD",
    "KXWTI15M": "Commodities.Index.PYTHOIL/USD",
    "KXCOPPER15M": "Commodities.Index.CU/USD",
    "KXNATGAS15M": "Commodities.Index.NATGAS/USD",
    "KXPLATINUM15M": "Metal.XPT/USD",
    "KXPALLADIUM15M": "Metal.XPD/USD",
}

# Yahoo fallback / financials (free public). Used when no Binance/Pyth map.
YAHOO_SYMBOLS = {
    "KXGOLD15M": "GC=F",
    "KXSILVER15M": "SI=F",
    "KXWTI15M": "CL=F",
    "KXCOPPER15M": "HG=F",
    "KXNATGAS15M": "NG=F",
    "KXPLATINUM15M": "PL=F",
    "KXPALLADIUM15M": "PA=F",
    "KXINX15M": "^GSPC",
    "KXNDQ15M": "^NDX",
    "KXEURUSD15M": "EURUSD=X",
    "KXGBPUSD15M": "GBPUSD=X",
    "KXUSDJPY15M": "USDJPY=X",
    "KX10YRRATE15M": "^TNX",
    "KX5YRRATE15M": "^FVX",
    "KX30YRRATE15M": "^TYX",
    "KX2YRRATE15M": "2YY=F",
}

# Series we cannot map to a single spot (comparisons / tests) — always skip.
SKIP_SERIES = {
    "KXCRYPTOCOMP15M",
    "KXCRYPTOLEAD15M",
    "KXGBPUSD15MTEST",
}

BINANCE_CACHE: dict = {}
SPOT_CACHE: dict = {}
_NO_SPOT_LOGGED: set[str] = set()
_SERIES_REFRESHED_AT = 0.0
SERIES_REFRESH_SECONDS = 300


def in_entry_window(rem) -> bool:
    """Last ~1 minute of a 15-minute contract: (5, 70] (faltando un minuto)."""
    try:
        value = float(rem)
    except (TypeError, ValueError):
        return False
    return value > ENTRY_WINDOW_MIN and value <= ENTRY_WINDOW_MAX


def seconds_remaining(close_time, now=None) -> float | None:
    value = str(close_time or "").strip()
    if not value:
        return None
    try:
        close_at = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if close_at.tzinfo is None:
        close_at = close_at.replace(tzinfo=dt.timezone.utc)
    if now is None:
        now = dt.datetime.now(dt.timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=dt.timezone.utc)
    return (close_at - now).total_seconds()


def dollars(market, dollar_key, cents_key):
    value = market.get(dollar_key)
    if value not in (None, ""):
        try:
            return round(float(value), 4)
        except (TypeError, ValueError):
            pass
    value = market.get(cents_key)
    if value not in (None, ""):
        try:
            return round(float(value) / 100, 4)
        except (TypeError, ValueError):
            pass
    return None


def opposite_price(price):
    if price is None:
        return None
    return round(1 - float(price), 4)


SIGMA_15M = {
    "KXBTC15M": 0.004,
    "KXETH15M": 0.005,
    "KXSOL15M": 0.008,
    "KXXRP15M": 0.008,
    "KXDOGE15M": 0.012,
    "KXBNB15M": 0.006,
    "KXADA15M": 0.010,
    "KXBCH15M": 0.010,
    "KXHYPE15M": 0.015,
    "KXNEAR15M": 0.012,
    "KXTON15M": 0.010,
    "KXZEC15M": 0.012,
}


def _num(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def digital_fair_yes(spot, strike, rem_s, series):
    spot_f = _num(spot)
    strike_f = _num(strike)
    if spot_f is None or strike_f is None or spot_f <= 0 or strike_f <= 0:
        return None
    rem = max(_num(rem_s) or 0.0, 5.0)
    sigma_full = SIGMA_15M.get(series, 0.008)
    sigma = sigma_full * math.sqrt(rem / 900.0)
    if sigma < 1e-6:
        return 0.99 if spot_f >= strike_f else 0.01
    z = math.log(spot_f / strike_f) / (sigma * math.sqrt(2.0))
    z = max(-8.0, min(8.0, z))
    fair = 0.5 * (1.0 + math.erf(z))
    return round(max(0.01, min(0.99, fair)), 4)


def enrich_fair(market: dict) -> dict:
    rem = market.get("seconds_remaining")
    if rem is None:
        rem = seconds_remaining(market.get("close_time"))
        market["seconds_remaining"] = rem
    fair = digital_fair_yes(
        market.get("binance_price"),
        market.get("floor_strike"),
        rem,
        market.get("series"),
    )
    market["fair_yes"] = fair
    yes_ask = _num(market.get("yes_ask"))
    no_ask = _num(market.get("no_ask"))
    market["yes_edge"] = None if fair is None or yes_ask is None else round(fair - yes_ask, 4)
    market["no_edge"] = None if fair is None or no_ask is None else round((1.0 - fair) - no_ask, 4)
    return market



def mode2_crypto_series(force: bool = False) -> list[str]:
    """Kalshi crypto 15m series with Binance spot/futures map.

    Includes only tickers present on Kalshi *and* in BINANCE_SYMBOLS.
    Skips metals/commodities (Pyth), FX/indices/rates (Yahoo-only), comparisons,
    tests, and anything unmapped. Prefer majors first for status readability /
    tie-breaks; remaining crypto alphabetically.
    """
    discovered = discover_15m_series(force=force)
    crypto = []
    for ticker in discovered:
        t = str(ticker or "").upper()
        if not t or t in SKIP_SERIES or t.endswith("TEST"):
            continue
        if t not in BINANCE_SYMBOLS:
            continue  # metals/FX/indices/unmapped
        if t not in crypto:
            crypto.append(t)
    prefer = [
        "KXBTC15M", "KXETH15M", "KXSOL15M", "KXXRP15M", "KXBNB15M",
        "KXDOGE15M", "KXADA15M", "KXBCH15M", "KXHYPE15M", "KXNEAR15M",
        "KXTON15M", "KXZEC15M", "KXLINK15M", "KXAVAX15M", "KXLTC15M",
        "KXDOT15M", "KXSUI15M",
    ]
    ranked = [t for t in prefer if t in crypto]
    ranked.extend(sorted(t for t in crypto if t not in ranked))
    return ranked


def discover_15m_series(force: bool = False) -> list[str]:
    """Pull all Kalshi fifteen_min series; keep those we can price or already know."""
    global SERIES, _SERIES_REFRESHED_AT
    now = time.monotonic()
    if not force and SERIES and now - _SERIES_REFRESHED_AT < SERIES_REFRESH_SECONDS:
        return list(SERIES)
    found: list[str] = []
    cursor = None
    seen_cursors: set[str] = set()
    try:
        while True:
            params = {"limit": 200}
            if cursor:
                params["cursor"] = cursor
            payload = _kalshi_get("/series", params=params, timeout=12)
            for row in payload.get("series") or []:
                ticker = str(row.get("ticker") or "").upper()
                freq = str(row.get("frequency") or "").lower()
                if not ticker.endswith("15M") and freq not in {"fifteen_min", "fifteen_minute", "15m", "15_min"}:
                    continue
                if ticker in SKIP_SERIES or ticker.endswith("TEST"):
                    continue
                if ticker and ticker not in found:
                    found.append(ticker)
            cursor = str(payload.get("cursor") or "")
            if not cursor or cursor in seen_cursors:
                break
            seen_cursors.add(cursor)
    except (requests.exceptions.RequestException, ForbiddenEndpoint, ValueError):
        found = []
    if found:
        # Prefer known-mappable first, then the rest (unmapped will skip at spot fetch).
        known = [
            t for t in found
            if t in BINANCE_SYMBOLS or t in PYTH_SYMBOLS or t in YAHOO_SYMBOLS
        ]
        rest = [t for t in found if t not in known]
        SERIES.clear()
        SERIES.extend(known + rest)
        _SERIES_REFRESHED_AT = now
    return list(SERIES)


def _empty_spot(series_ticker, message, source=None):
    return {
        "binance_available": False,
        "binance_symbol": (
            BINANCE_SYMBOLS.get(series_ticker)
            or PYTH_SYMBOLS.get(series_ticker)
            or YAHOO_SYMBOLS.get(series_ticker)
        ),
        "binance_market": source or "none",
        "binance_price": None,
        "spot_source": source or "none",
        "momentum_1m_pct": None,
        "momentum_5m_pct": None,
        "taker_buy_share": None,
        "orderbook_bid_share": None,
        "binance_message": message,
    }


def fetch_pyth_spot(series_ticker: str):
    symbol = PYTH_SYMBOLS.get(series_ticker)
    if not symbol:
        return None
    cache_key = "pyth:" + symbol
    now = time.monotonic()
    cached = SPOT_CACHE.get(cache_key)
    if cached and now - cached["updated"] < BINANCE_CACHE_SECONDS:
        return dict(cached["value"])
    try:
        import re
        url = "https://app.pyth.com/explore/" + symbol.replace("/", "%2F")
        response = requests.get(
            url,
            headers={"User-Agent": "Mozilla/5.0 (compatible; omega-late1m/1.0)"},
            timeout=12,
        )
        response.raise_for_status()
        match = re.search(r'latestPrice\\":([0-9]+(?:\.[0-9]+)?)', response.text)
        if not match:
            match = re.search(r'"latestPrice":([0-9]+(?:\.[0-9]+)?)', response.text)
        if not match:
            raise ValueError("Pyth latestPrice missing")
        price = float(match.group(1))
        if not math.isfinite(price) or price <= 0:
            raise ValueError("Pyth price invalid")
        value = {
            "binance_available": True,
            "binance_symbol": symbol,
            "binance_market": "pyth",
            "binance_price": round(price, 8),
            "spot_source": "pyth",
            "momentum_1m_pct": None,
            "momentum_5m_pct": None,
            "taker_buy_share": None,
            "orderbook_bid_share": None,
            "binance_message": "Señal Pyth disponible",
        }
    except (requests.exceptions.RequestException, TypeError, ValueError):
        return None
    SPOT_CACHE[cache_key] = {"updated": now, "value": dict(value)}
    return value


def fetch_yahoo_spot(series_ticker: str):
    symbol = YAHOO_SYMBOLS.get(series_ticker)
    if not symbol:
        return None
    cache_key = "yahoo:" + symbol
    now = time.monotonic()
    cached = SPOT_CACHE.get(cache_key)
    if cached and now - cached["updated"] < BINANCE_CACHE_SECONDS:
        return dict(cached["value"])
    try:
        url = "https://query1.finance.yahoo.com/v8/finance/chart/" + symbol
        response = requests.get(
            url,
            params={"interval": "1m", "range": "1d"},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=10,
        )
        response.raise_for_status()
        payload = response.json()
        result = (payload.get("chart") or {}).get("result") or []
        if not result:
            raise ValueError("Yahoo chart empty")
        meta = result[0].get("meta") or {}
        price = meta.get("regularMarketPrice") or meta.get("postMarketPrice")
        if price is None:
            quotes = ((result[0].get("indicators") or {}).get("quote") or [{}])[0]
            closes = [c for c in (quotes.get("close") or []) if c is not None]
            if not closes:
                raise ValueError("Yahoo close empty")
            price = closes[-1]
        price = float(price)
        if not math.isfinite(price) or price <= 0:
            raise ValueError("Yahoo price invalid")
        value = {
            "binance_available": True,
            "binance_symbol": symbol,
            "binance_market": "yahoo",
            "binance_price": round(price, 8),
            "spot_source": "yahoo",
            "momentum_1m_pct": None,
            "momentum_5m_pct": None,
            "taker_buy_share": None,
            "orderbook_bid_share": None,
            "binance_message": "Señal Yahoo disponible",
        }
    except (requests.exceptions.RequestException, TypeError, ValueError, KeyError, IndexError):
        return None
    SPOT_CACHE[cache_key] = {"updated": now, "value": dict(value)}
    return value


def fetch_spot_signal(series_ticker: str):
    """Crypto→Binance; metals/commodities→Pyth (Kalshi settlement) then Yahoo; else skip."""
    if series_ticker in SKIP_SERIES:
        if series_ticker not in _NO_SPOT_LOGGED:
            _NO_SPOT_LOGGED.add(series_ticker)
            print(f"[spot] skip {series_ticker}: comparison/test series", flush=True)
        return _empty_spot(series_ticker, "Serie sin spot (skip)")

    if series_ticker in BINANCE_SYMBOLS:
        signal = fetch_binance_signal(series_ticker)
        if signal.get("binance_available"):
            signal["spot_source"] = "binance"
            return signal

    pyth = fetch_pyth_spot(series_ticker)
    if pyth and pyth.get("binance_available"):
        return pyth

    yahoo = fetch_yahoo_spot(series_ticker)
    if yahoo and yahoo.get("binance_available"):
        return yahoo

    # Retry binance even if also in pyth/yahoo maps failed
    if series_ticker in BINANCE_SYMBOLS:
        return fetch_binance_signal(series_ticker)

    if series_ticker not in _NO_SPOT_LOGGED:
        _NO_SPOT_LOGGED.add(series_ticker)
        print(f"[spot] skip {series_ticker}: no Binance/Pyth/Yahoo mapping or feed failed", flush=True)
    return _empty_spot(series_ticker, "Sin feed de spot — serie omitida")


def empty_binance_signal(series_ticker, message="Datos Binance no disponibles"):
    return {
        "binance_available": False,
        "binance_symbol": BINANCE_SYMBOLS.get(series_ticker),
        "binance_market": (
            "futures" if series_ticker in BINANCE_FUTURES_SERIES else "spot"
        ),
        "binance_price": None,
        "momentum_1m_pct": None,
        "momentum_5m_pct": None,
        "taker_buy_share": None,
        "orderbook_bid_share": None,
        "binance_message": message,
    }


def fetch_binance_signal(series_ticker):
    """Obtiene momentum, presion taker y libro con una cache corta."""
    symbol = BINANCE_SYMBOLS.get(series_ticker)
    if not symbol:
        return empty_binance_signal(series_ticker, "Par Binance no configurado")

    futures_market = series_ticker in BINANCE_FUTURES_SERIES
    market_name = "futures" if futures_market else "spot"
    base_url = BINANCE_FUTURES_BASE_URL if futures_market else BINANCE_BASE_URL
    klines_path = "/fapi/v1/klines" if futures_market else "/api/v3/klines"
    depth_path = "/fapi/v1/depth" if futures_market else "/api/v3/depth"
    cache_key = market_name + ":" + symbol

    now = time.monotonic()
    cached = BINANCE_CACHE.get(cache_key)
    if cached and now - cached["updated"] < BINANCE_CACHE_SECONDS:
        return dict(cached["value"])

    try:
        klines_response = requests.get(
            base_url + klines_path,
            params={"symbol": symbol, "interval": "1m", "limit": 7},
            timeout=7,
        )
        klines_response.raise_for_status()
        klines = klines_response.json()
        if not isinstance(klines, list) or len(klines) < 7:
            raise ValueError("Velas Binance incompletas")

        depth_response = requests.get(
            base_url + depth_path,
            params={"symbol": symbol, "limit": 20},
            timeout=7,
        )
        depth_response.raise_for_status()
        depth = depth_response.json()

        closes = [float(candle[4]) for candle in klines]
        current_price = closes[-1]
        momentum_1m = current_price / closes[-2] - 1
        momentum_5m = current_price / closes[-6] - 1

        recent_klines = klines[-2:]
        quote_volume = sum(float(candle[7]) for candle in recent_klines)
        taker_buy_quote = sum(float(candle[10]) for candle in recent_klines)
        taker_buy_share = (
            taker_buy_quote / quote_volume if quote_volume > 0 else None
        )

        bids = depth.get("bids", [])[:10]
        asks = depth.get("asks", [])[:10]
        bid_notional = sum(float(price) * float(size) for price, size in bids)
        ask_notional = sum(float(price) * float(size) for price, size in asks)
        book_total = bid_notional + ask_notional
        orderbook_bid_share = (
            bid_notional / book_total if book_total > 0 else None
        )

        if taker_buy_share is None or orderbook_bid_share is None:
            raise ValueError("Volumen Binance incompleto")

        value = {
            "binance_available": True,
            "binance_symbol": symbol,
            "binance_market": market_name,
            "binance_price": round(current_price, 8),
            "momentum_1m_pct": round(momentum_1m * 100, 5),
            "momentum_5m_pct": round(momentum_5m * 100, 5),
            "taker_buy_share": round(taker_buy_share, 6),
            "orderbook_bid_share": round(orderbook_bid_share, 6),
            "binance_message": "Señal Binance disponible",
        }
    except (requests.exceptions.RequestException, TypeError, ValueError, ZeroDivisionError):
        value = empty_binance_signal(series_ticker)

    BINANCE_CACHE[cache_key] = {"updated": now, "value": dict(value)}
    return value


def parse_trade_time(trade):
    """Devuelve la hora Unix mas precisa disponible para ordenar ejecuciones."""
    value = trade.get("ts_ms")
    if value not in (None, ""):
        try:
            return float(value) / 1000
        except (TypeError, ValueError):
            pass

    value = trade.get("ts")
    if value not in (None, ""):
        try:
            return float(value)
        except (TypeError, ValueError):
            pass

    value = str(trade.get("created_time") or "").strip()
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.timestamp()


def interval_times(close_time):
    value = str(close_time or "").strip()
    if not value:
        return None
    try:
        close_at = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if close_at.tzinfo is None:
        close_at = close_at.replace(tzinfo=dt.timezone.utc)
    close_ts = close_at.timestamp()
    start_ts = close_ts - INTERVAL_SECONDS
    return start_ts, start_ts + FLOW_WINDOW_SECONDS


def empty_flow(available, window_closed=False):
    starting_value = 0.0 if available and not window_closed else None
    return {
        "flow_available": available,
        "flow_window_closed": window_closed,
        "yes_flow": starting_value,
        "no_flow": starting_value,
        "flow_trade_count": 0,
        "kalshi_yes_share": None,
    }


def _kalshi_get(path, params=None, timeout=8):
    _assert_safe_path(path)
    if params:
        _assert_no_money_tokens(" ".join(str(k) for k in params.keys()))
    headers = optional_headers("GET", path)
    response = requests.get(
        BASE_URL + path,
        params=params,
        headers=headers,
        timeout=timeout,
    )
    response.raise_for_status()
    return response.json()


def fetch_trade_flow(ticker, close_time):
    """Suma el dinero ejecutado por el taker de cada lado en los primeros 5 min."""
    window = interval_times(close_time)
    if not ticker or window is None:
        return empty_flow(False)

    start_ts, deadline_ts = window
    now_ts = dt.datetime.now(dt.timezone.utc).timestamp()
    if now_ts < start_ts:
        return empty_flow(True)
    if now_ts >= deadline_ts:
        return empty_flow(True, window_closed=True)

    trades = []
    cursor = None
    seen_cursors = set()
    while True:
        params = {
            "ticker": ticker,
            "min_ts": max(0, int(start_ts) - 1),
            "max_ts": int(now_ts) + 1,
            "limit": 1000,
        }
        if cursor:
            params["cursor"] = cursor
        payload = _kalshi_get("/markets/trades", params=params, timeout=8)
        trades.extend(payload.get("trades", []))
        cursor = str(payload.get("cursor") or "")
        if not cursor:
            break
        if cursor in seen_cursors:
            raise ValueError("Cursor de trades repetido")
        seen_cursors.add(cursor)

    normalized = []
    for trade in trades:
        executed_at = parse_trade_time(trade)
        if (
            executed_at is None
            or executed_at < start_ts
            or executed_at > now_ts
            or executed_at >= deadline_ts
        ):
            continue
        side = str(
            trade.get("taker_outcome_side") or trade.get("taker_side") or ""
        ).lower()
        if side not in {"yes", "no"}:
            continue
        try:
            count = float(trade.get("count_fp", trade.get("count")))
            price = float(trade.get(f"{side}_price_dollars"))
        except (TypeError, ValueError):
            continue
        if (
            not math.isfinite(count)
            or not math.isfinite(price)
            or count <= 0
            or not 0 < price < 1
        ):
            continue
        amount = count * price
        normalized.append(
            (executed_at, str(trade.get("trade_id") or ""), side, amount)
        )

    totals = {"yes": 0.0, "no": 0.0}
    for _executed_at, _trade_id, side, amount in normalized:
        totals[side] += amount
    total_flow = totals["yes"] + totals["no"]
    yes_share = totals["yes"] / total_flow if total_flow > 0 else None

    return {
        "flow_available": True,
        "flow_window_closed": False,
        "yes_flow": round(totals["yes"], 2),
        "no_flow": round(totals["no"], 2),
        "flow_trade_count": len(normalized),
        "kalshi_yes_share": (
            None if yes_share is None else round(yes_share, 6)
        ),
    }


def fetch_market(ticker: str) -> dict | None:
    if not ticker:
        return None
    try:
        payload = _kalshi_get("/markets/" + ticker, timeout=8)
    except requests.exceptions.RequestException:
        return None
    market = payload.get("market") or payload
    yes_bid = dollars(market, "yes_bid_dollars", "yes_bid")
    yes_ask = dollars(market, "yes_ask_dollars", "yes_ask")
    return {
        "ticker": market.get("ticker") or ticker,
        "title": market.get("title") or market.get("subtitle"),
        "status": market.get("status"),
        "result": market.get("result"),
        "close_time": market.get("close_time"),
        "yes_bid": yes_bid,
        "yes_ask": yes_ask,
        "no_bid": opposite_price(yes_ask),
        "no_ask": opposite_price(yes_bid),
        "floor_strike": _num(market.get("floor_strike")),
        "series": str(market.get("ticker") or ticker).split("-")[0],
        "volume": market.get("volume_fp", market.get("volume", 0)),
    }


def scan_one_series(series_ticker):
    payload = _kalshi_get(
        "/markets",
        params={"series_ticker": series_ticker, "status": "open", "limit": 5},
        timeout=8,
    )
    markets = [
        {
            "series": series_ticker,
            "ticker": market.get("ticker"),
            "title": market.get("title") or market.get("subtitle") or series_ticker,
            "close_time": market.get("close_time"),
            "yes_bid": dollars(market, "yes_bid_dollars", "yes_bid"),
            "yes_ask": dollars(market, "yes_ask_dollars", "yes_ask"),
            "floor_strike": _num(market.get("floor_strike")),
            "volume": market.get("volume_fp", market.get("volume", 0)),
            "status": market.get("status"),
            "result": market.get("result"),
        }
        for market in payload.get("markets", [])
    ]
    markets.sort(key=lambda item: item.get("close_time") or "")
    selected = markets[:1]
    for market in selected:
        market["no_bid"] = opposite_price(market.get("yes_ask"))
        market["no_ask"] = opposite_price(market.get("yes_bid"))
        market.update(fetch_spot_signal(series_ticker))
        enrich_fair(market)
    return selected


def scan_markets():
    series = discover_15m_series()
    found = []
    with ThreadPoolExecutor(max_workers=10) as pool:
        jobs = {pool.submit(scan_one_series, ticker): ticker for ticker in series}
        for job in as_completed(jobs):
            try:
                found.extend(job.result())
            except (requests.exceptions.RequestException, ForbiddenEndpoint):
                continue
    found.sort(key=lambda item: (item.get("close_time") or "", item.get("series") or ""))
    return found


def add_strategy_plans(markets, max_total=1.00):
    enriched = []
    for market in markets:
        item = dict(market)
        yes_bid = item.get("yes_bid")
        yes_ask = item.get("yes_ask")
        no_bid = opposite_price(yes_ask)
        no_ask = opposite_price(yes_bid)
        item["no_bid"] = no_bid
        item["no_ask"] = no_ask
        rem = seconds_remaining(item.get("close_time"))
        item["seconds_remaining"] = None if rem is None else round(rem, 2)
        item["in_entry_window"] = in_entry_window(rem)
        spread = None
        if yes_bid is not None and yes_ask is not None:
            spread = max(0.0, round(float(yes_ask) - float(yes_bid), 4))
        signal = omega_signal(
            item.get("momentum_1m_pct"),
            item.get("momentum_5m_pct"),
            item.get("taker_buy_share"),
            item.get("orderbook_bid_share"),
            item.get("kalshi_yes_share"),
            spread,
        )
        item.update(signal)
        item["yes_plan"] = build_entry_plan(
            "yes", yes_ask, max_total=max_total, omega_votes=signal["omega_yes_votes"]
        )
        item["no_plan"] = build_entry_plan(
            "no", no_ask, max_total=max_total, omega_votes=signal["omega_no_votes"]
        )
        omega_side = signal["omega_side"]
        item["selected_plan"] = (
            item["yes_plan"] if omega_side == "yes"
            else item["no_plan"] if omega_side == "no"
            else None
        )
        enriched.append(item)
    return enriched


SETTINGS = {
    "mode": "LIVE_WHEN_KEYS",
    "test_version": 10,
    "max_total_cost_per_crypto": 1.00,
    "entry_mode": "omega_impulse_three_of_four",
    "minimum_confirming_votes": MIN_CONFIRMING_VOTES,
    "taker_buy_threshold": TAKER_BUY_THRESHOLD,
    "orderbook_bid_threshold": ORDERBOOK_BID_THRESHOLD,
    "kalshi_yes_threshold": KALSHI_YES_THRESHOLD,
    "maximum_spread": MAX_SPREAD,
    "flow_measure": "executed_taker_outcome_notional",
    "trail_arm_net_proceeds": 1.05,
    "trail_drop": 0.02,
    "stop_loss": None,
    "hold_to_settlement_if_never_armed": True,
    "max_open_trades": 14,
    "intervals_per_day": 96,
    "entry_window_seconds_remaining": [ENTRY_WINDOW_MIN, ENTRY_WINDOW_MAX],
    "poll_seconds": 3,
}
