"""Short-horizon trend for Kalshi 15m crypto terminals (Binance-mapped).

Uses Binance 1m closes (via omega.fetch when available, else public klines).
Bias: UP | DOWN | MIXED. Never places orders.

Mode 2 direction (EMA3/9 primary), same rules per market:
  UP → YES (follow) only if spot > rolling VWAP
  DOWN → NO (follow) only if spot < rolling VWAP
  MIXED / SIDEWAYS → skip entirely (no fade)
  Slow filter: min |EMA3−EMA9|/EMA9 gap + EMA21/50 same side

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
EMA_MID = 21
EMA_SLOW2 = 50
LOOKBACK = 15  # legacy short lookback (status/tests)
LOOKBACK_SLOW = 80  # enough 1m closes for EMA50
# Mode2 slow-bias entry filter: require meaningful fast gap + EMA21/50 align.
MODE2_MIN_EMA_GAP = 0.0004  # |EMA3−EMA9|/|EMA9| ≥ 0.04%
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


def mode2_ema_gap_rel(trend: dict | None) -> float:
    """Relative |EMA3−EMA9|/|EMA9| from a detect_trend dict (0 if missing)."""
    if not trend:
        return 0.0
    try:
        e3 = float(trend.get("ema3"))
        e9 = float(trend.get("ema9"))
    except (TypeError, ValueError):
        return 0.0
    if abs(e9) < 1e-12:
        return 0.0
    return abs(e3 - e9) / abs(e9)


def mode2_slow_bias(trend: dict | None) -> str:
    """UP if EMA21>EMA50, DOWN if EMA21<EMA50, else MIXED."""
    if not trend:
        return "MIXED"
    try:
        e21 = float(trend.get("ema21"))
        e50 = float(trend.get("ema50"))
    except (TypeError, ValueError):
        return "MIXED"
    if e21 > e50:
        return "UP"
    if e21 < e50:
        return "DOWN"
    return "MIXED"


def mode2_slow_filter_ok(trend: dict | None, bias: str | None = None) -> tuple[bool, str]:
    """Entry gate: min EMA3/9 gap + EMA21/50 same side as fast bias.

    UP needs EMA21>EMA50; DOWN needs EMA21<EMA50. Fail-closed if EMAs missing.
    """
    b = str(bias or (trend or {}).get("bias") or "MIXED").upper()
    if b not in {"UP", "DOWN"}:
        return False, f"skip slow-filter bias={b}"
    gap = mode2_ema_gap_rel(trend)
    if gap + 1e-15 < float(MODE2_MIN_EMA_GAP):
        return False, (
            f"skip slow-filter ema_gap={gap:.6f}<{MODE2_MIN_EMA_GAP} "
            f"(need |EMA3-EMA9|/EMA9)"
        )
    slow = mode2_slow_bias(trend)
    if slow != b:
        e21 = (trend or {}).get("ema21")
        e50 = (trend or {}).get("ema50")
        return False, (
            f"skip slow-filter EMA21/50={slow} vs fast={b} "
            f"(EMA21={e21} EMA50={e50})"
        )
    return True, (
        f"slow-filter OK fast={b} gap={gap:.6f} "
        f"EMA21={((trend or {}).get('ema21'))} EMA50={((trend or {}).get('ema50'))}"
    )


def mode2_bias_adverse_to_side(bias: str | None, side: str | None) -> bool:
    """True if EMA3/9 bias flipped against an open FOLLOW side (or MIXED)."""
    b = str(bias or "MIXED").upper()
    s = str(side or "").lower()
    if s == "yes":
        return b != "UP"  # DOWN or MIXED against YES
    if s == "no":
        return b != "DOWN"
    return True


def detect_trend(
    prospective_side: str | None = None, series: str | None = None
) -> dict:
    """Return bias / note / advice for status display and Mode 2 direction.

    prospective_side: 'yes' | 'no' | None — when known, advice is relative to it.
    series: Kalshi series ticker (KXBTC15M / KXETH15M / KXSOL15M).
    """
    series = str(series or SERIES_DEFAULT).upper()
    label = asset_label(series)
    closes = _fetch_closes(limit=LOOKBACK_SLOW, series=series)
    signal = fetch_binance_signal(series) or {}
    m1 = signal.get("momentum_1m_pct")
    m5 = signal.get("momentum_5m_pct")
    price = signal.get("binance_price")

    ema3 = _ema(closes, EMA_FAST) if closes else None
    ema9 = _ema(closes, EMA_SLOW) if closes else None
    ema21 = _ema(closes, EMA_MID) if closes else None
    ema50 = _ema(closes, EMA_SLOW2) if closes else None
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

    if ema21 is not None and ema50 is not None:
        if ema21 > ema50:
            parts.append(f"EMA21>EMA50 ({ema21:.1f}>{ema50:.1f})")
        elif ema21 < ema50:
            parts.append(f"EMA21<EMA50 ({ema21:.1f}<{ema50:.1f})")
        else:
            parts.append(f"EMA21≈EMA50 ({ema21:.1f})")
    elif closes:
        parts.append("EMA21/50 unavailable (short history)")

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
        m2_note = "Mode2 FOLLOW UP→YES if spot>VWAP120m + slow EMA21>EMA50 + min gap; else skip"
    elif bias == "DOWN":
        m2_dir = "FOLLOW → NO"
        m2_note = "Mode2 FOLLOW DOWN→NO if spot<VWAP120m + slow EMA21<EMA50 + min gap; else skip"
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
        "ema21": None if ema21 is None else round(ema21, 2),
        "ema50": None if ema50 is None else round(ema50, 2),
        "ema_gap_rel": None
        if ema3 is None or ema9 is None or abs(ema9) < 1e-12
        else round(abs(ema3 - ema9) / abs(ema9), 8),
        "slow_bias": (
            "UP" if (ema21 is not None and ema50 is not None and ema21 > ema50)
            else "DOWN" if (ema21 is not None and ema50 is not None and ema21 < ema50)
            else "MIXED"
        ),
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
