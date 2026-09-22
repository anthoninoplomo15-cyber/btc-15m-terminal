"""Short-horizon trend for Kalshi 15m crypto terminals (Binance-mapped).

Uses Binance 1m closes (via omega.fetch when available, else public klines).
Bias: UP | DOWN | MIXED. Never places orders.

Mode 2 direction (EMA3/9 primary), same rules per market:
  UP → YES (follow) only if spot > rolling VWAP
  DOWN → NO (follow) only if spot < rolling VWAP
  MIXED / SIDEWAYS → skip entirely (no fade)

VWAP: rolling typical-price VWAP of last VWAP_MINUTES (120) 1m bars.
"""

from __future__ import annotations

import requests

from omega.fetch import (
    BINANCE_BASE_URL,
    BINANCE_FUTURES_BASE_URL,
    BINANCE_FUTURES_SERIES,
    BINANCE_SYMBOLS,
    fetch_binance_signal,
)

SERIES_DEFAULT = "KXBTC15M"
# Back-compat alias used by older imports / status text
SERIES = SERIES_DEFAULT
EMA_FAST = 3
EMA_SLOW = 9
LOOKBACK = 15  # 1m closes for EMA bias
# Rolling VWAP window for Mode2 FOLLOW alignment (documented choice).
VWAP_MINUTES = 120

_ASSET_LABEL = {
    "KXBTC15M": "BTC",
    "KXETH15M": "ETH",
    "KXSOL15M": "SOL",
}


def asset_label(series: str | None = None) -> str:
    s = str(series or SERIES_DEFAULT).upper()
    if s in _ASSET_LABEL:
        return _ASSET_LABEL[s]
    # KXETH15M → ETH, etc.
    if s.startswith("KX") and s.endswith("15M") and len(s) > 5:
        return s[2:-3]
    return s


def _ema(values: list[float], period: int) -> float | None:
    if not values or period < 1 or len(values) < period:
        return None
    k = 2.0 / (period + 1)
    ema = sum(values[:period]) / period
    for price in values[period:]:
        ema = price * k + ema * (1 - k)
    return ema


def _fetch_klines(limit: int, series: str | None = None) -> list[list]:
    """Raw Binance 1m klines: [open_time, o, h, l, c, volume, ...]."""
    series = str(series or SERIES_DEFAULT).upper()
    symbol = BINANCE_SYMBOLS.get(series, "BTCUSDT")
    futures = series in BINANCE_FUTURES_SERIES
    base = BINANCE_FUTURES_BASE_URL if futures else BINANCE_BASE_URL
    path = "/fapi/v1/klines" if futures else "/api/v3/klines"
    try:
        resp = requests.get(
            base + path,
            params={"symbol": symbol, "interval": "1m", "limit": max(limit, 12)},
            timeout=8,
        )
        resp.raise_for_status()
        rows = resp.json()
        if not isinstance(rows, list) or len(rows) < 5:
            return []
        return rows
    except Exception:
        return []


def _fetch_closes(limit: int = LOOKBACK, series: str | None = None) -> list[float]:
    rows = _fetch_klines(limit, series=series)
    if not rows:
        return []
    return [float(c[4]) for c in rows]


def compute_rolling_vwap(
    minutes: int = VWAP_MINUTES, series: str | None = None
) -> tuple[float | None, float | None, str]:
    """Rolling typical-price VWAP over last `minutes` of 1m bars.

    VWAP = sum(((H+L+C)/3) * V) / sum(V). Spot = last close.
    Returns (spot, vwap, note). Either may be None on failure.
    """
    rows = _fetch_klines(minutes, series=series)
    if not rows:
        return None, None, "VWAP unavailable (no klines)"
    use = rows[-minutes:] if len(rows) >= minutes else rows
    num = 0.0
    den = 0.0
    for row in use:
        try:
            h, l, c = float(row[2]), float(row[3]), float(row[4])
            vol = float(row[5])
        except (TypeError, ValueError, IndexError):
            continue
        if vol <= 0:
            continue
        typical = (h + l + c) / 3.0
        num += typical * vol
        den += vol
    if den <= 0:
        return None, None, "VWAP unavailable (zero volume)"
    vwap = num / den
    spot = float(use[-1][4])
    return spot, vwap, f"spot={spot:.2f} VWAP{minutes}m={vwap:.2f} bars={len(use)}"


def mode2_vwap_aligned(
    side: str, bias: str, series: str | None = None
) -> tuple[bool, str]:
    """FOLLOW VWAP gate: UP/YES needs spot>VWAP; DOWN/NO needs spot<VWAP."""
    b = str(bias or "").upper()
    s = str(side or "").lower()
    spot, vwap, note = compute_rolling_vwap(VWAP_MINUTES, series=series)
    if spot is None or vwap is None:
        return False, f"skip VWAP unavailable ({note})"
    if b == "UP" and s == "yes":
        if spot > vwap:
            return True, f"VWAP aligned UP spot>VWAP ({note})"
        return False, f"skip VWAP misaligned UP spot<=VWAP ({note})"
    if b == "DOWN" and s == "no":
        if spot < vwap:
            return True, f"VWAP aligned DOWN spot<VWAP ({note})"
        return False, f"skip VWAP misaligned DOWN spot>=VWAP ({note})"
    return False, f"skip VWAP unexpected side={s} bias={b}"


def mode2_side_from_trend(bias: str, prior_result: str | None = None) -> tuple[str | None, str]:
    """Pick Mode 2 entry side + log label from trend.

    MIXED/SIDEWAYS → skip (no fade). prior_result kept for API compat, unused.
    """
    del prior_result  # no longer used for fade-prior
    b = str(bias or "MIXED").upper()
    if b == "UP":
        return "yes", "Mode2 FOLLOW UP→YES"
    if b == "DOWN":
        return "no", "Mode2 FOLLOW DOWN→NO"
    return None, "skip MIXED"


def detect_trend(
    prospective_side: str | None = None, series: str | None = None
) -> dict:
    """Return bias / note / advice for status display and Mode 2 direction.

    prospective_side: 'yes' | 'no' | None — when known, advice is relative to it.
    series: Kalshi series ticker (KXBTC15M / KXETH15M / KXSOL15M).
    """
    series = str(series or SERIES_DEFAULT).upper()
    label = asset_label(series)
    closes = _fetch_closes(series=series)
    signal = fetch_binance_signal(series) or {}
    m1 = signal.get("momentum_1m_pct")
    m5 = signal.get("momentum_5m_pct")
    price = signal.get("binance_price")

    ema3 = _ema(closes, EMA_FAST) if closes else None
    ema9 = _ema(closes, EMA_SLOW) if closes else None
    last_n = closes[-5:] if closes else []

    bias = "MIXED"
    parts: list[str] = []

    if ema3 is not None and ema9 is not None:
        if ema3 > ema9 * 1.00015:
            bias = "UP"
            parts.append(f"EMA3>EMA{EMA_SLOW} ({ema3:.1f}>{ema9:.1f})")
        elif ema3 < ema9 * 0.99985:
            bias = "DOWN"
            parts.append(f"EMA3<EMA{EMA_SLOW} ({ema3:.1f}<{ema9:.1f})")
        else:
            parts.append(f"EMA3≈EMA{EMA_SLOW}")
    elif m1 is not None and m5 is not None:
        try:
            m1f, m5f = float(m1), float(m5)
            if m1f > 0.02 and m5f > 0.05:
                bias = "UP"
                parts.append(f"mom1m={m1f:+.3f}% mom5m={m5f:+.3f}%")
            elif m1f < -0.02 and m5f < -0.05:
                bias = "DOWN"
                parts.append(f"mom1m={m1f:+.3f}% mom5m={m5f:+.3f}%")
            else:
                parts.append(f"mom1m={m1f:+.3f}% mom5m={m5f:+.3f}%")
        except (TypeError, ValueError):
            parts.append("momentum unavailable")
    else:
        parts.append(f"insufficient {label} data → MIXED")

    # Soft conflict: last closes vs EMA bias → MIXED
    if last_n and len(last_n) >= 3 and bias in {"UP", "DOWN"}:
        slope = last_n[-1] - last_n[0]
        if bias == "UP" and slope < 0:
            bias = "MIXED"
            parts.append("last closes fading → MIXED")
        elif bias == "DOWN" and slope > 0:
            bias = "MIXED"
            parts.append("last closes bouncing → MIXED")

    spot, vwap, vwap_note = compute_rolling_vwap(VWAP_MINUTES, series=series)
    if vwap is not None and spot is not None:
        parts.append(vwap_note)

    if price is not None:
        try:
            pf = float(price)
            # SOL/ETH need more decimals than BTC for readability
            if pf >= 1000:
                parts.insert(0, f"{label}≈{pf:.2f}")
            elif pf >= 10:
                parts.insert(0, f"{label}≈{pf:.3f}")
            else:
                parts.insert(0, f"{label}≈{pf:.4f}")
        except (TypeError, ValueError):
            parts.insert(0, f"{label}≈{price}")
    if last_n:
        if last_n[-1] >= 1000:
            parts.append("closes=" + ",".join(f"{c:.0f}" for c in last_n[-3:]))
        else:
            parts.append("closes=" + ",".join(f"{c:.2f}" for c in last_n[-3:]))

    note = "; ".join(parts) if parts else "no data"

    side = str(prospective_side or "").lower() or None
    if side not in {"yes", "no"}:
        advice = "CAUTION"
        advice_note = "no prospective side"
    elif bias == "MIXED":
        advice = "CAUTION"
        advice_note = "MIXED / sideways — Mode 2 skips entry"
    elif (bias == "UP" and side == "yes") or (bias == "DOWN" and side == "no"):
        advice = "ALIGNED"
        advice_note = f"trend {bias} supports {side.upper()}"
    else:
        advice = "CONFLICT"
        advice_note = f"trend {bias} conflicts with {side.upper()}"

    # Mode 2 direction preview
    if bias == "UP":
        m2_dir = "FOLLOW → YES"
        m2_note = "Mode2 FOLLOW UP→YES if spot>VWAP120m; else skip"
    elif bias == "DOWN":
        m2_dir = "FOLLOW → NO"
        m2_note = "Mode2 FOLLOW DOWN→NO if spot<VWAP120m; else skip"
    else:
        m2_dir = "SKIP MIXED"
        m2_note = "Mode2 skip MIXED/SIDEWAYS (no fade)"

    return {
        "series": series,
        "asset": label,
        "bias": bias,
        "note": note,
        "advice": advice,
        "advice_note": advice_note,
        "ema3": None if ema3 is None else round(ema3, 2),
        "ema9": None if ema9 is None else round(ema9, 2),
        "momentum_1m_pct": m1,
        "momentum_5m_pct": m5,
        "price": price,
        "vwap": None if vwap is None else round(vwap, 2),
        "vwap_spot": None if spot is None else round(spot, 2),
        "vwap_minutes": VWAP_MINUTES,
        "closes_tail": [round(c, 2) for c in last_n],
        "mode2_direction": m2_dir,
        "mode2_note": m2_note,
        "prospective_side": side,
    }


def format_trend_block(trend: dict) -> list[str]:
    """Markdown / stdout lines for status."""
    asset = trend.get("asset") or asset_label(trend.get("series"))
    series = trend.get("series") or SERIES_DEFAULT
    return [
        f"- [{asset}/{series}] Bias: **{trend.get('bias', 'MIXED')}**",
        f"- Note: {trend.get('note') or 'n/a'}",
        f"- Advice: **{trend.get('advice', 'CAUTION')}** — {trend.get('advice_note') or ''}",
        f"- Mode 2 direction: **{trend.get('mode2_direction', 'SKIP MIXED')}** — "
        f"{trend.get('mode2_note') or ''}",
    ]
