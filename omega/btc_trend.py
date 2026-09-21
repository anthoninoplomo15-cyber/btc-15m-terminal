"""BTC short-horizon trend for KXBTC15M terminal advice / Mode 2 direction.

Uses Binance 1m closes (via omega.fetch when available, else public klines).
Bias: UP | DOWN | MIXED. Never places orders.

Mode 2 uses bias for DIRECTION only (not a block gate):
  UP → YES (follow), DOWN → NO (follow), MIXED → fade prior settle.
"""

from __future__ import annotations

import requests

from omega.fetch import BINANCE_BASE_URL, BINANCE_SYMBOLS, fetch_binance_signal

SERIES = "KXBTC15M"
EMA_FAST = 3
EMA_SLOW = 9
LOOKBACK = 15  # 1m closes


def _ema(values: list[float], period: int) -> float | None:
    if not values or period < 1 or len(values) < period:
        return None
    k = 2.0 / (period + 1)
    ema = sum(values[:period]) / period
    for price in values[period:]:
        ema = price * k + ema * (1 - k)
    return ema


def _fetch_closes(limit: int = LOOKBACK) -> list[float]:
    symbol = BINANCE_SYMBOLS.get(SERIES, "BTCUSDT")
    try:
        resp = requests.get(
            BINANCE_BASE_URL + "/api/v3/klines",
            params={"symbol": symbol, "interval": "1m", "limit": max(limit, 12)},
            timeout=8,
        )
        resp.raise_for_status()
        rows = resp.json()
        if not isinstance(rows, list) or len(rows) < 5:
            return []
        return [float(c[4]) for c in rows]
    except Exception:
        return []


def mode2_side_from_trend(bias: str, prior_result: str | None) -> tuple[str | None, str]:
    """Pick Mode 2 entry side + log label from trend + prior settle.

    Returns (side|'yes'|'no'|None, reason_label).
    """
    b = str(bias or "MIXED").upper()
    prior = str(prior_result or "").lower()
    if b == "UP":
        return "yes", "Mode2 FOLLOW UP→YES"
    if b == "DOWN":
        return "no", "Mode2 FOLLOW DOWN→NO"
    # MIXED / SIDEWAYS → fade prior
    if prior == "yes":
        return "no", "Mode2 FADE prior=YES→NO"
    if prior == "no":
        return "yes", "Mode2 FADE prior=NO→YES"
    return None, "Mode2 FADE waiting prior settle (MIXED)"


def detect_trend(prospective_side: str | None = None) -> dict:
    """Return bias / note / advice for status display and Mode 2 direction.

    prospective_side: 'yes' | 'no' | None — when known, advice is relative to it.
    """
    closes = _fetch_closes()
    signal = fetch_binance_signal(SERIES) or {}
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
        parts.append("insufficient BTC data → MIXED")

    # Soft conflict: last closes vs EMA bias → MIXED
    if last_n and len(last_n) >= 3 and bias in {"UP", "DOWN"}:
        slope = last_n[-1] - last_n[0]
        if bias == "UP" and slope < 0:
            bias = "MIXED"
            parts.append("last closes fading → MIXED")
        elif bias == "DOWN" and slope > 0:
            bias = "MIXED"
            parts.append("last closes bouncing → MIXED")

    if price is not None:
        parts.insert(0, f"BTC≈{float(price):.2f}")
    if last_n:
        parts.append("closes=" + ",".join(f"{c:.0f}" for c in last_n[-3:]))

    note = "; ".join(parts) if parts else "no data"

    side = str(prospective_side or "").lower() or None
    if side not in {"yes", "no"}:
        advice = "CAUTION"
        advice_note = "no prospective side"
    elif bias == "MIXED":
        advice = "CAUTION"
        advice_note = "MIXED / sideways — fade-prior path for Mode 2"
    elif (bias == "UP" and side == "yes") or (bias == "DOWN" and side == "no"):
        advice = "ALIGNED"
        advice_note = f"trend {bias} supports {side.upper()}"
    else:
        advice = "CONFLICT"
        advice_note = f"trend {bias} conflicts with {side.upper()}"

    # Mode 2 direction preview (no block gate)
    if bias == "UP":
        m2_dir = "FOLLOW → YES"
        m2_note = "Mode2 FOLLOW UP→YES (trend does not block)"
    elif bias == "DOWN":
        m2_dir = "FOLLOW → NO"
        m2_note = "Mode2 FOLLOW DOWN→NO (trend does not block)"
    else:
        m2_dir = "FADE prior settle"
        m2_note = "Mode2 FADE prior (MIXED/SIDEWAYS) — YES→NO / NO→YES"

    return {
        "bias": bias,
        "note": note,
        "advice": advice,
        "advice_note": advice_note,
        "ema3": None if ema3 is None else round(ema3, 2),
        "ema9": None if ema9 is None else round(ema9, 2),
        "momentum_1m_pct": m1,
        "momentum_5m_pct": m5,
        "price": price,
        "closes_tail": [round(c, 2) for c in last_n],
        "mode2_direction": m2_dir,
        "mode2_note": m2_note,
        "prospective_side": side,
    }


def format_trend_block(trend: dict) -> list[str]:
    """Markdown / stdout lines for status."""
    return [
        f"- Bias: **{trend.get('bias', 'MIXED')}**",
        f"- Note: {trend.get('note') or 'n/a'}",
        f"- Advice: **{trend.get('advice', 'CAUTION')}** — {trend.get('advice_note') or ''}",
        f"- Mode 2 direction: **{trend.get('mode2_direction', 'FADE prior settle')}** — "
        f"{trend.get('mode2_note') or ''}",
    ]
