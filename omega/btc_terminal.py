"""Kalshi multi-asset 15m dual-mode terminal (BTC/ETH/SOL).

Modes:
  first_phase — BTC-only; enter last ~1 min (rem in (50,70]), side from trend + cheap ask;
                exit any ~≥1¢ net after fees, else hold to settle.
  fade (Mode2)— multi-series BTC+ETH+SOL; confirm-window entry (age 60–120s);
                  EMA3/9 direction per market:
                  UP→YES follow (spot>VWAP120m), DOWN→NO follow (spot<VWAP120m);
                  MIXED/SIDEWAYS → skip; ask>0.70 → skip;
                trailing exit (arm +0.10 peak, stop −0.08 from peak, cap 0.99);
                midcut at ~7.5m if trail never armed and bid<entry+0.05;
                max 3 TP/MIDCUT sell attempts then abandon.
                Max 1 concurrent open across BTC+ETH+SOL (still scan all three).
                Same-cycle multi-qualify → clearest EMA gap, then BTC→ETH→SOL.
                Cash-gated.

CLI (user activates manually; default is OFF / status only):
  python -m omega.btc_terminal status
  python -m omega.btc_terminal trend
  LIVE=1 python -m omega.btc_terminal start --mode first_phase
  LIVE=1 python -m omega.btc_terminal start --mode fade
  python -m omega.btc_terminal stop

HARD LOCK: never deposit/withdraw/bank. Ex2 cash only. Stake ~$1.
Without LIVE=1, start refuses. Default / no args: status+trend and EXIT (no loop).
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from omega import config
from omega.btc_trend import asset_label, detect_trend, format_trend_block, mode2_side_from_trend, mode2_vwap_aligned
from omega.fetch import INTERVAL_SECONDS, fetch_market, seconds_remaining, _kalshi_get
from omega.kalshi import (
    ForbiddenEndpoint,
    KalshiError,
    create_order_ioc,
    get_balance,
    get_positions,
    has_keys,
    reset_key_cache,
)
from omega.signal import affordable_contracts, entry_cost, net_pnl, taker_fee

SERIES = "KXBTC15M"  # primary / first_phase default
MODE2_SERIES = ("KXBTC15M", "KXETH15M", "KXSOL15M")
ALL_SERIES = MODE2_SERIES
ET = ZoneInfo("America/New_York")
# Deploy-friendly paths: package root or DATA_DIR / LOG_DIR overrides.
_PKG_ROOT = Path(__file__).resolve().parents[1]
ROOT = Path(os.environ.get("DATA_DIR", str(_PKG_ROOT))).expanduser().resolve()
LOG_DIR = Path(os.environ.get("LOG_DIR", str(ROOT))).expanduser().resolve()
LOG_PATH = LOG_DIR / "btc_terminal.log"
STATE_PATH = ROOT / "data" / "btc_terminal_state.json"
STATUS_PATH = Path(
    os.environ.get("STATUS_PATH", str(ROOT / "btc-terminal-status.md"))
).expanduser().resolve()
DOCS_PATH = Path(
    os.environ.get("DOCS_PATH", str(_PKG_ROOT / "docs" / "btc-terminal.md"))
).expanduser().resolve()

STAKE = 1.00
MAX_OPEN_PER_SERIES = 1
MAX_OPEN = 1  # Mode2: max 1 concurrent open across BTC+ETH+SOL

# Mode 1 — first_phase
FP_REM_LO = 50.0  # exclusive lower
FP_REM_HI = 70.0  # inclusive upper → rem in (50, 70]
FP_ASK_MIN = 0.05
FP_ASK_MAX = 0.96
FP_MIXED_ASK_CAP = 0.85
FP_ANY_GAIN = 0.01  # ~1¢ net after fees

# Mode 2 — fade / follow
# Confirm window: enter only when market age ∈ [60, 120] seconds (NOT exact-open ≤20s).
ENTRY_CONFIRM_MIN_AGE_SEC = 60
ENTRY_CONFIRM_MAX_AGE_SEC = 120
# Listing-lag catch-up retargeted to the same confirm window (late appear still ok if age≤120).
ENTRY_CATCHUP_MAX_AGE_SEC = ENTRY_CONFIRM_MAX_AGE_SEC
ENTRY_SETTLE_GRACE_SEC = 45  # legacy unused for Mode2 entries (MIXED skipped)
TRANSITION_CATCHUP_LOOKBACK_SEC = 90
# Back-compat alias: old "exact-open" ceiling removed; tests/docs use confirm bounds.
ENTRY_MAX_AGE_SEC = ENTRY_CONFIRM_MIN_AGE_SEC
# Legacy fixed TP (+0.20) disabled for Mode2 — trailing exit instead.
TP_ADD = 0.20  # unused for Mode2; kept for back-compat / first_phase helpers
TP_CAP = 0.99  # cap peak tracking and sell prices
MAX_TP_ATTEMPTS = 3  # reused as max trailing sell attempts
TRAIL_ARM_ADD = 0.10  # arm trailing once peak bid >= entry + 0.10
TRAIL_DRAWDOWN = 0.08  # exit when bid falls >= 0.08 from peak high
MODE2_ASK_MAX = 0.70  # skip FOLLOW if chosen ask above this
MIDCUT_AGE_SEC = 450  # ~7.5 min halfway into 15m window
MIDCUT_PROGRESS_ADD = 0.05  # MIDCUT only if trail never armed and bid < entry+0.05
MAX_MIDCUT_ATTEMPTS = 3
POLL_SEC = 2.0
OPEN_POLL_SEC = 0.25
NEAR_OPEN_SEC = 30  # also ±30s of :00/:15/:30/:45 clock boundary
FP_POLL_SEC = 1.0

# Month codes for {SERIES}-{YY}{MON}{DD}{HH}{MM}-{MM} ticker construction
_MONTH_CODES = (
    "JAN", "FEB", "MAR", "APR", "MAY", "JUN",
    "JUL", "AUG", "SEP", "OCT", "NOV", "DEC",
)

# In-memory only (cleared on restart): last time we saw empty open list or rem<=0
_last_transition_obs_mono: float | None = None

MODES = ("first_phase", "fade")

LOGGER = logging.getLogger("omega.btc_terminal")
STOP = threading.Event()


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def et_now() -> datetime:
    return datetime.now(ET)


def setup_logging() -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    LOGGER.setLevel(logging.INFO)
    if LOGGER.handlers:
        return
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    fh = logging.FileHandler(LOG_PATH, encoding="utf-8")
    fh.setFormatter(fmt)
    LOGGER.addHandler(fh)
    if sys.stdout.isatty():
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        LOGGER.addHandler(sh)


def _parse_iso(value) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def default_state() -> dict:
    return {
        "armed": False,
        "mode": None,
        "pid": None,
        "position": None,  # legacy single-slot; mirrored from positions when exactly one
        "positions": {},  # series -> open position dict
        "traded_close_times": [],  # legacy BTC close_times; prefer traded_keys
        "traded_keys": [],  # "{series}:{close_time}" per interval traded
        "last_settle": None,
        "last_entry_attempt": None,
        "last_trend": None,  # BTC primary snapshot
        "last_trends": {},  # series -> trend snapshot
        "stats": {
            "entries": 0,
            "tp": 0,
            "any_gain": 0,
            "settle_wins": 0,
            "settle_losses": 0,
            "exit_abandoned": 0,
            "mode2_follow": 0,
            "mode2_fade": 0,
            "midcut": 0,
            "trail": 0,
        },
        "updated_at": None,
    }


def _series_of_position(pos: dict | None) -> str | None:
    if not pos:
        return None
    s = pos.get("series")
    if s:
        return str(s).upper()
    ticker = str(pos.get("ticker") or "")
    for series in ALL_SERIES:
        if ticker.startswith(series):
            return series
    if ticker.startswith("KXBTC"):
        return "KXBTC15M"
    return None


def migrate_state(state: dict) -> dict:
    """Normalize single-position legacy state → positions map keyed by series."""
    base = default_state()
    merged = {**base, **(state or {})}
    positions = merged.get("positions")
    if not isinstance(positions, dict):
        positions = {}
    # Coerce keys to upper; drop empties
    clean = {}
    for k, v in positions.items():
        if v:
            clean[str(k).upper()] = v
    # Legacy single position → map
    legacy = merged.get("position")
    if legacy:
        series = _series_of_position(legacy) or SERIES
        legacy = {**legacy, "series": series}
        clean.setdefault(series, legacy)
    merged["positions"] = clean
    # Mirror legacy field: single open → that pos; else None (multi uses positions)
    if len(clean) == 1:
        merged["position"] = next(iter(clean.values()))
    elif len(clean) == 0:
        merged["position"] = None
    else:
        merged["position"] = None
    # Migrate traded_close_times → traded_keys (assume BTC for bare close_times)
    keys = list(merged.get("traded_keys") or [])
    for ct in merged.get("traded_close_times") or []:
        bare = f"{SERIES}:{ct}"
        if bare not in keys and ct not in keys:
            keys.append(bare)
    # Dedup preserve order
    seen = set()
    uniq = []
    for k in keys:
        if k not in seen:
            seen.add(k)
            uniq.append(k)
    merged["traded_keys"] = uniq[-120:]
    if not isinstance(merged.get("last_trends"), dict):
        merged["last_trends"] = {}
    return merged


def get_positions_map(state: dict) -> dict:
    return dict((state.get("positions") or {}))


def open_position_count(state: dict) -> int:
    return sum(1 for v in get_positions_map(state).values() if v)


def get_series_position(state: dict, series: str) -> dict | None:
    return get_positions_map(state).get(str(series).upper())


def set_series_position(state: dict, series: str, pos: dict | None) -> None:
    series = str(series).upper()
    positions = dict(state.get("positions") or {})
    if pos is None:
        positions.pop(series, None)
    else:
        pos = {**pos, "series": series}
        positions[series] = pos
    state["positions"] = positions
    if len(positions) == 1:
        state["position"] = next(iter(positions.values()))
    else:
        state["position"] = None


def traded_key(series: str, close_time: str) -> str:
    return f"{str(series).upper()}:{close_time}"


def already_traded(state: dict, series: str, close_time: str) -> bool:
    key = traded_key(series, close_time)
    keys = set(state.get("traded_keys") or [])
    if key in keys:
        return True
    # Legacy: bare close_time only blocks BTC
    if series == SERIES and close_time in set(state.get("traded_close_times") or []):
        return True
    return False


def mark_traded(state: dict, series: str, close_time: str) -> None:
    keys = list(state.get("traded_keys") or [])
    key = traded_key(series, close_time)
    if key not in keys:
        keys.append(key)
    state["traded_keys"] = keys[-120:]
    if series == SERIES:
        traded = list(state.get("traded_close_times") or [])
        if close_time not in traded:
            traded.append(close_time)
        state["traded_close_times"] = traded[-40:]


def load_state() -> dict:
    if STATE_PATH.is_file():
        try:
            data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            return migrate_state(data or {})
        except Exception:
            pass
    return default_state()


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = utc_now().isoformat()
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, default=str), encoding="utf-8")
    tmp.replace(STATE_PATH)


def _dollars(market, *keys):
    for k in keys:
        v = market.get(k)
        if v in (None, ""):
            continue
        try:
            if k.endswith("_dollars") or "." in str(v):
                return float(v)
            return float(v) / 100.0
        except (TypeError, ValueError):
            continue
    return None


def _opp(price):
    if price is None:
        return None
    try:
        return round(1.0 - float(price), 4)
    except (TypeError, ValueError):
        return None


def _active_interval_markets(markets: list[dict], now: datetime | None = None) -> list[dict]:
    """Return only intervals active *now*, newest open first.

    Kalshi can leave a just-closed market at status=open briefly.  Treat local
    close/open times as authoritative so that stale rows cannot hide the new
    15-minute interval.  Sorting by remaining time closest to a full interval
    selects the newly opened contract when responses overlap at the boundary.
    """
    now = now or utc_now()
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    active = []
    for market in markets:
        close_dt = _parse_iso(market.get("close_time"))
        if close_dt is None:
            continue
        rem = (close_dt - now).total_seconds()
        age = INTERVAL_SECONDS - rem
        if rem <= 0 or age < 0 or age > INTERVAL_SECONDS:
            continue
        active.append((abs(rem - INTERVAL_SECONDS), close_dt, market))
    active.sort(key=lambda item: (item[0], item[1]))
    return [item[2] for item in active]


def list_open_series(series: str | None = None) -> list[dict]:
    series = str(series or SERIES).upper()
    payload = _kalshi_get(
        "/markets",
        params={"series_ticker": series, "status": "open", "limit": 10},
        timeout=10,
    )
    markets = []
    for m in payload.get("markets") or []:
        ticker = m.get("ticker") or ""
        if not str(ticker).startswith(series):
            continue
        yes_bid = _dollars(m, "yes_bid_dollars", "yes_bid")
        yes_ask = _dollars(m, "yes_ask_dollars", "yes_ask")
        markets.append(
            {
                "ticker": ticker,
                "series": series,
                "title": m.get("title") or m.get("subtitle"),
                "close_time": m.get("close_time"),
                "status": m.get("status"),
                "result": m.get("result"),
                "yes_bid": yes_bid,
                "yes_ask": yes_ask,
                "no_bid": _opp(yes_ask),
                "no_ask": _opp(yes_bid),
                "floor_strike": m.get("floor_strike"),
            }
        )
    return _active_interval_markets(markets)


def list_open_btc() -> list[dict]:
    """Back-compat: open markets for primary (BTC) series."""
    return list_open_series(SERIES)


def list_recent_settled(limit: int = 8, series: str | None = None) -> list[dict]:
    series = str(series or SERIES).upper()
    payload = _kalshi_get(
        "/markets",
        params={"series_ticker": series, "status": "settled", "limit": limit},
        timeout=10,
    )
    rows = list(payload.get("markets") or [])
    rows.sort(key=lambda x: x.get("close_time") or "", reverse=True)
    out = []
    for m in rows:
        out.append(
            {
                "ticker": m.get("ticker"),
                "result": str(m.get("result") or "").lower(),
                "close_time": m.get("close_time"),
            }
        )
    return out


def window_age_sec(close_time) -> float | None:
    close_dt = _parse_iso(close_time)
    if close_dt is None:
        return None
    open_dt = close_dt - timedelta(seconds=INTERVAL_SECONDS)
    return (utc_now() - open_dt).total_seconds()


def mark_transition_obs() -> None:
    """Record that we saw empty open list or rem<=0 (listing rollover)."""
    global _last_transition_obs_mono
    _last_transition_obs_mono = time.monotonic()


def transition_catchup_active(lookback: float = TRANSITION_CATCHUP_LOOKBACK_SEC) -> bool:
    """True if empty/rem<=0 was observed within lookback seconds (this process)."""
    if _last_transition_obs_mono is None:
        return False
    return (time.monotonic() - _last_transition_obs_mono) <= lookback


def near_interval_boundary(
    now: datetime | None = None, window: float = NEAR_OPEN_SEC
) -> bool:
    """True within ±window seconds of a :00/:15/:30/:45 ET mark."""
    now = now or et_now()
    secs = now.minute % 15 * 60 + now.second + now.microsecond / 1_000_000
    return secs <= window or secs >= (INTERVAL_SECONDS - window)


def current_interval_close_et(now: datetime | None = None) -> datetime:
    """Close time (ET) of the 15m interval containing *now* (open+15m)."""
    now = now or et_now()
    open_min = (now.minute // 15) * 15
    open_dt = now.replace(minute=open_min, second=0, microsecond=0)
    return open_dt + timedelta(minutes=15)


def series_ticker_for_close(close_et: datetime, series: str | None = None) -> str:
    """Build {SERIES}-{YY}{MON}{DD}{HH}{MM}-{MM} from an ET close datetime."""
    series = str(series or SERIES).upper()
    if close_et.tzinfo is None:
        close_et = close_et.replace(tzinfo=ET)
    else:
        close_et = close_et.astimezone(ET)
    yy = close_et.strftime("%y")
    mon = _MONTH_CODES[close_et.month - 1]
    dd = f"{close_et.day:02d}"
    hh = f"{close_et.hour:02d}"
    mm = f"{close_et.minute:02d}"
    return f"{series}-{yy}{mon}{dd}{hh}{mm}-{mm}"


def kxbtc15m_ticker_for_close(close_et: datetime) -> str:
    """Back-compat BTC ticker builder."""
    return series_ticker_for_close(close_et, SERIES)


def fetch_market_by_clock(series: str | None = None) -> dict | None:
    """Construct current-interval ticker from clock and fetch_market (bypass open list)."""
    series = str(series or SERIES).upper()
    close_et = current_interval_close_et()
    ticker = series_ticker_for_close(close_et, series)
    try:
        raw = fetch_market(ticker)
    except Exception as exc:
        LOGGER.debug("fetch_market_by_clock %s failed: %s", ticker, exc)
        return None
    if not raw or not raw.get("ticker"):
        return None
    status = str(raw.get("status") or "").lower()
    if status not in {"open", "active", ""}:
        return None
    # Normalize to list_open_series shape
    yes_bid = raw.get("yes_bid")
    yes_ask = raw.get("yes_ask")
    return {
        "ticker": raw["ticker"],
        "series": series,
        "title": raw.get("title"),
        "close_time": raw.get("close_time"),
        "status": raw.get("status"),
        "result": raw.get("result"),
        "yes_bid": yes_bid,
        "yes_ask": yes_ask,
        "no_bid": _opp(yes_ask),
        "no_ask": _opp(yes_bid),
        "floor_strike": raw.get("floor_strike"),
    }


def resolve_fade_market(series: str | None = None) -> tuple[dict | None, str]:
    """Pick Mode2 market: open list first; on empty/rollover try clock ticker.

    Marks transition observation when list is empty or selected market has rem<=0.
    """
    series = str(series or SERIES).upper()
    opens = list_open_series(series)
    if opens:
        market = opens[0]
        rem = seconds_remaining(market.get("close_time"))
        if rem is not None and rem <= 0:
            mark_transition_obs()
            # Stale row at boundary — try clock construct for the new interval
            clock_m = fetch_market_by_clock(series)
            if clock_m:
                return clock_m, "clock_after_stale"
            return None, "stale_rem_le_0"
        return market, "open_list"

    mark_transition_obs()
    clock_m = fetch_market_by_clock(series)
    if clock_m:
        return clock_m, "clock_after_empty"
    return None, "empty"


def mode2_ema_gap_score(trend: dict | None) -> float:
    """Relative |EMA3-EMA9|/|EMA9| — larger = clearer FOLLOW. 0 if unavailable.

    Documented Mode2 same-cycle pick: highest score wins; ties break by
    series order BTC → ETH → SOL (caller sorts with MODE2_SERIES index).
    """
    if not trend:
        return 0.0
    e3, e9 = trend.get("ema3"), trend.get("ema9")
    try:
        e3f, e9f = float(e3), float(e9)
    except (TypeError, ValueError):
        return 0.0
    denom = abs(e9f) if e9f else 1.0
    return abs(e3f - e9f) / denom


def mode2_entry_allowed(age: float, *, need_prior: bool = False, prior_ok: bool = True) -> tuple[bool, float, str]:
    """Decide if Mode2 may enter given market age.

    Confirm window only: age ∈ [ENTRY_CONFIRM_MIN_AGE_SEC, ENTRY_CONFIRM_MAX_AGE_SEC]
    (60–120s). Do NOT enter at exact-open (age < 60). Listing-lag catch-up is
    retargeted to that same window — if the market appears late but age is still
    ≤120s (and ≥60s), entry is allowed. Outside the window: wait / skip.

    need_prior / prior_ok kept for API compat (MIXED settle-grace removed).
    """
    del need_prior, prior_ok  # unused — MIXED no longer enters via settle grace
    lo = float(ENTRY_CONFIRM_MIN_AGE_SEC)
    hi = float(ENTRY_CONFIRM_MAX_AGE_SEC)
    if age < lo:
        return False, lo, "too_early"
    if age <= hi:
        if transition_catchup_active():
            return True, hi, "confirm_catchup"
        return True, hi, "confirm_window"
    return False, hi, "blocked"


def prior_settle_for(close_time: str, settled: list[dict]) -> dict | None:
    close_dt = _parse_iso(close_time)
    if close_dt is None:
        return None
    prior_close = (close_dt - timedelta(seconds=INTERVAL_SECONDS)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    for s in settled:
        ct = str(s.get("close_time") or "")
        if ct.replace("+00:00", "Z") == prior_close or ct.startswith(
            prior_close.replace("Z", "")
        ):
            if s.get("result") in {"yes", "no"}:
                return s
    candidates = []
    for s in settled:
        sdt = _parse_iso(s.get("close_time"))
        if sdt and sdt < close_dt and s.get("result") in {"yes", "no"}:
            candidates.append((sdt, s))
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0], reverse=True)
    best = candidates[0]
    if abs((close_dt - best[0]).total_seconds() - INTERVAL_SECONDS) > 120:
        if (close_dt - best[0]).total_seconds() > 20 * 60:
            return None
    return best[1]


def ask_for_side(market: dict, side: str):
    return market.get("yes_ask") if side == "yes" else market.get("no_ask")


def bid_for_side(market: dict, side: str):
    return market.get("yes_bid") if side == "yes" else market.get("no_bid")


def safe_balance() -> float | None:
    try:
        info = get_balance()
        return float(info["cash"])
    except Exception as exc:
        LOGGER.error("balance failed: %s", type(exc).__name__)
        return None


def choose_first_phase_side(trend: dict, market: dict) -> tuple[str | None, str]:
    """UP→YES, DOWN→NO, MIXED→cheaper ask (under 0.85). Skip bad asks."""
    bias = str(trend.get("bias") or "MIXED").upper()
    yes_ask = market.get("yes_ask")
    no_ask = market.get("no_ask")

    def ok(ask) -> bool:
        if ask is None:
            return False
        try:
            a = float(ask)
        except (TypeError, ValueError):
            return False
        return FP_ASK_MIN <= a <= FP_ASK_MAX

    if bias == "UP":
        if ok(yes_ask):
            return "yes", f"first_phase UP→YES ask={yes_ask}"
        return None, f"skip UP YES ask bad={yes_ask}"
    if bias == "DOWN":
        if ok(no_ask):
            return "no", f"first_phase DOWN→NO ask={no_ask}"
        return None, f"skip DOWN NO ask bad={no_ask}"

    # MIXED: cheaper ask under 0.85
    candidates = []
    if ok(yes_ask) and float(yes_ask) < FP_MIXED_ASK_CAP:
        candidates.append(("yes", float(yes_ask)))
    if ok(no_ask) and float(no_ask) < FP_MIXED_ASK_CAP:
        candidates.append(("no", float(no_ask)))
    if not candidates:
        return None, f"MIXED no cheap ask under {FP_MIXED_ASK_CAP} yes={yes_ask} no={no_ask}"
    candidates.sort(key=lambda x: x[1])
    side, ask = candidates[0]
    return side, f"first_phase MIXED→cheaper {side.upper()} ask={ask}"


def place_entry(
    market: dict,
    side: str,
    cash: float,
    *,
    tactic: str,
    tp_price: float | None = None,
    extra: dict | None = None,
) -> dict | None:
    ticker = market["ticker"]
    live = fetch_market(ticker) or market
    # refresh asks on live
    if live is not market:
        ya = _dollars(live, "yes_ask_dollars", "yes_ask")
        yb = _dollars(live, "yes_bid_dollars", "yes_bid")
        if ya is not None:
            live = {
                **market,
                "yes_ask": ya,
                "yes_bid": yb,
                "no_ask": _opp(yb),
                "no_bid": _opp(ya),
                "ticker": ticker,
                "close_time": live.get("close_time") or market.get("close_time"),
            }
    ask = ask_for_side(live, side)
    if ask is None:
        LOGGER.info("no ask for %s %s", ticker, side)
        return None
    price = round(min(float(ask) + 0.01, 0.99), 2)
    if price < FP_ASK_MIN or price > 0.97:
        LOGGER.info("skip %s ask/px out of band ask=%s px=%s", ticker, ask, price)
        return None
    stake = min(STAKE, max(0.0, cash - 0.05))
    if stake < 0.50:
        LOGGER.warning("cash too low for entry: %.4f", cash)
        return None
    qty = affordable_contracts(price, max_total=stake)
    if not qty:
        LOGGER.info("no size at px=%.2f stake=%.2f", price, stake)
        return None
    qty = float(qty)
    cost = float(entry_cost(qty, price) or 0)
    if cost > cash + 1e-9:
        LOGGER.info("cost %.4f > cash %.4f", cost, cash)
        return None
    client_order_id = str(uuid.uuid4())
    LOGGER.info(
        "LIVE send %s %s %s qty=%s px=%s cost=%.4f cash=%.4f",
        tactic, ticker, side, qty, price, cost, cash,
    )
    try:
        result = create_order_ioc(
            ticker=ticker,
            outcome_side=side,
            contracts=qty,
            outcome_price=price,
            client_order_id=client_order_id,
            reduce_only=False,
            action="buy",
        )
    except ForbiddenEndpoint:
        LOGGER.error("blocked forbidden endpoint")
        return None
    except Exception as exc:
        LOGGER.error(
            "LIVE entry failed %s %s %s", ticker, type(exc).__name__, str(exc)[:240]
        )
        return None
    fill = float(result.get("fill_count") or 0.0)
    avg = result.get("average_fill_price")
    fill_price = float(avg) if avg is not None else price
    if side == "no" and avg is not None:
        fill_price = round(1.0 - float(avg), 4)
    if fill <= 0:
        LOGGER.info("LIVE IOC no fill %s %s", ticker, side)
        return None
    actual_cost = entry_cost(fill, fill_price)
    if tactic == "fade":
        # Mode2: no fixed TP — trailing exit; sentinel tp_price=None
        tp_cent = None
    elif tp_price is None:
        tp = round(min(TP_CAP, float(fill_price) + TP_ADD), 4)
        tp_cent = min(TP_CAP, math.ceil(tp * 100 - 1e-9) / 100.0)
    else:
        tp_cent = float(tp_price)
    LOGGER.info(
        "LIVE FILL %s %s %s qty=%s px=%s cost=%s tp=%s oid=%s",
        tactic, ticker, side, fill, fill_price, actual_cost, tp_cent, result.get("order_id"),
    )
    pos = {
        "ticker": ticker,
        "series": str(market.get("series") or _series_of_position({"ticker": ticker}) or SERIES).upper(),
        "side": side,
        "contracts": fill,
        "entry_price": fill_price,
        "entry_fee": taker_fee(fill, fill_price),
        "cost": actual_cost,
        "tp_price": tp_cent,
        "close_time": market.get("close_time"),
        "client_order_id": client_order_id,
        "order_id": result.get("order_id"),
        "opened_at": utc_now().isoformat(),
        "mode": "LIVE",
        "tactic": tactic,
        "tp_attempts": 0,
        "midcut_attempts": 0,
        "exit_abandoned": False,
        # Mode2 trailing state (harmless for first_phase)
        "peak_bid": round(min(TP_CAP, float(fill_price)), 4),
        "trail_armed": False,
        "trail_attempts": 0,
    }
    if extra:
        pos.update(extra)
    return pos


def close_sell(position: dict, bid: float, *, aggressive: bool = False) -> bool:
    """Sell IOC near bid (optional deeper ladder). Returns True if filled."""
    ticker = position["ticker"]
    side = position["side"]
    qty = float(position["contracts"])
    px = round(max(0.01, float(bid)), 2)
    prices = []
    ladder = [px, round(max(0.01, px - 0.01), 2), round(max(0.01, px - 0.02), 2)]
    if aggressive:
        ladder.extend(
            [
                round(max(0.01, px - 0.05), 2),
                round(max(0.01, px - 0.10), 2),
                0.01,
            ]
        )
    for p in ladder:
        if p not in prices:
            prices.append(p)
    for sell_px in prices:
        client_order_id = str(uuid.uuid4())
        try:
            result = create_order_ioc(
                ticker=ticker,
                outcome_side=side,
                contracts=qty,
                outcome_price=sell_px,
                client_order_id=client_order_id,
                reduce_only=True,
                action="sell",
            )
        except KalshiError as exc:
            LOGGER.error("exit KalshiError %s %s", ticker, str(exc)[:200])
            return False
        except Exception as exc:
            LOGGER.error("exit failed %s %s", ticker, type(exc).__name__)
            return False
        fill = float(result.get("fill_count") or 0.0)
        if fill > 0:
            avg = result.get("average_fill_price")
            exit_px = float(avg) if avg is not None else sell_px
            if side == "no" and avg is not None:
                exit_px = round(1.0 - float(avg), 4)
            pnl = net_pnl(fill, position["entry_price"], exit_px)
            LOGGER.info(
                "EXIT CLOSED %s %s qty=%s exit=%s pnl=%s oid=%s",
                ticker, side, fill, exit_px, pnl, result.get("order_id"),
            )
            return True
        LOGGER.info("exit IOC no fill %s px=%.2f", ticker, sell_px)
    return False


def check_settled(position: dict) -> str | None:
    m = fetch_market(position["ticker"])
    if not m:
        return None
    result = str(m.get("result") or "").lower()
    if result in {"yes", "no"}:
        return result
    status = str(m.get("status") or "").lower()
    if status in {"settled", "finalized"}:
        return result if result in {"yes", "no"} else None
    return None


def _position_ticker(p: dict) -> str:
    return str(
        p.get("ticker")
        or p.get("market_ticker")
        or (p.get("market") or {}).get("ticker")
        or ""
    )


def exchange_open_count(series_filter: tuple[str, ...] | None = None) -> int | None:
    """Count non-zero exchange positions, optionally limited to our series."""
    try:
        live_pos = get_positions()
        open_count = 0
        for p in live_pos or []:
            fp = p.get("position_fp", p.get("position"))
            try:
                if abs(float(fp or 0)) <= 1e-9:
                    continue
            except (TypeError, ValueError):
                continue
            if series_filter:
                ticker = _position_ticker(p)
                if not any(ticker.startswith(s) for s in series_filter):
                    continue
            open_count += 1
        return open_count
    except Exception as exc:
        LOGGER.warning("positions check failed: %s", type(exc).__name__)
        return None


def exchange_series_open(series: str) -> bool | None:
    """True if exchange already shows an open position for this series."""
    series = str(series).upper()
    try:
        live_pos = get_positions()
        for p in live_pos or []:
            fp = p.get("position_fp", p.get("position"))
            try:
                if abs(float(fp or 0)) <= 1e-9:
                    continue
            except (TypeError, ValueError):
                continue
            ticker = _position_ticker(p)
            if ticker.startswith(series):
                return True
        return False
    except Exception as exc:
        LOGGER.warning("positions check failed: %s", type(exc).__name__)
        return None


def manage_one_position(state: dict, mode: str, series: str, pos: dict) -> None:
    """Manage a single open position for `series` (trail / MIDCUT / settle)."""
    series = str(series).upper()

    # Already abandoned exit — just wait for settle, never spin TP
    if pos.get("exit_abandoned"):
        result = check_settled(pos)
        if result:
            _record_settle(state, pos, result)
        return

    result = check_settled(pos)
    if result:
        _record_settle(state, pos, result)
        return

    m = fetch_market(pos["ticker"])
    if not m:
        return
    # normalize bids
    yb = _dollars(m, "yes_bid_dollars", "yes_bid")
    ya = _dollars(m, "yes_ask_dollars", "yes_ask")
    book = {
        "yes_bid": yb,
        "yes_ask": ya,
        "no_bid": _opp(ya),
        "no_ask": _opp(yb),
    }
    bid = bid_for_side(book, pos["side"])
    if bid is None:
        return

    if mode == "first_phase" or pos.get("tactic") == "first_phase":
        pnl = net_pnl(pos["contracts"], pos["entry_price"], float(bid))
        if pnl is not None and pnl + 1e-9 >= FP_ANY_GAIN:
            LOGGER.info(
                "ANY-GAIN trigger %s bid=%.4f est_pnl=%.4f >= %.2f",
                pos["ticker"], float(bid), pnl, FP_ANY_GAIN,
            )
            if close_sell(pos, float(bid)):
                stats = state.setdefault("stats", {})
                stats["any_gain"] = int(stats.get("any_gain") or 0) + 1
                set_series_position(state, series, None)
                save_state(state)
        return

    # Mode 2: trailing exit (replaces fixed TP entry+0.20)
    # Track peak bid since entry; arm after peak >= entry+0.10; exit on -0.08 from peak.
    # Cap peak/sells at TP_CAP (0.99). MIDCUT@7.5m only if trail never armed & bid<entry+0.05.
    entry = float(pos.get("entry_price") or 0)
    bid_f = min(TP_CAP, float(bid))
    peak = float(pos.get("peak_bid") or entry)
    peak = max(peak, bid_f)
    peak = min(TP_CAP, peak)
    pos["peak_bid"] = round(peak, 4)
    # Clear legacy fixed-TP so we never fire old entry+0.20 path
    if pos.get("tp_price") is not None:
        pos["tp_price"] = None

    trail_armed = bool(pos.get("trail_armed"))
    arm_level = entry + TRAIL_ARM_ADD
    if not trail_armed and peak + 1e-9 >= arm_level:
        trail_armed = True
        pos["trail_armed"] = True
        LOGGER.info(
            "TRAIL ARM %s peak=%.4f >= entry+%.2f (entry=%.4f bid=%.4f)",
            pos["ticker"], peak, TRAIL_ARM_ADD, entry, bid_f,
        )

    if trail_armed:
        drawdown = peak - bid_f
        if drawdown + 1e-9 >= TRAIL_DRAWDOWN:
            attempts = int(pos.get("trail_attempts") or pos.get("tp_attempts") or 0)
            if attempts >= MAX_TP_ATTEMPTS:
                LOGGER.warning(
                    "TRAIL abandon %s after %s attempts — hold to settle peak=%.4f bid=%.4f",
                    pos["ticker"], attempts, peak, bid_f,
                )
                pos["exit_abandoned"] = True
                stats = state.setdefault("stats", {})
                stats["exit_abandoned"] = int(stats.get("exit_abandoned") or 0) + 1
                set_series_position(state, series, pos)
                save_state(state)
                return
            sell_bid = min(TP_CAP, bid_f)
            LOGGER.info(
                "TRAIL EXIT %s bid=%.4f peak=%.4f dd=%.4f >= %.2f attempt=%s/%s",
                pos["ticker"], sell_bid, peak, drawdown, TRAIL_DRAWDOWN,
                attempts + 1, MAX_TP_ATTEMPTS,
            )
            ok = close_sell(pos, sell_bid)
            pos["trail_attempts"] = attempts + 1
            pos["tp_attempts"] = pos["trail_attempts"]  # mirror for status
            if ok:
                stats = state.setdefault("stats", {})
                stats["trail"] = int(stats.get("trail") or 0) + 1
                set_series_position(state, series, None)
                save_state(state)
                return
            if pos["trail_attempts"] >= MAX_TP_ATTEMPTS:
                LOGGER.warning(
                    "TRAIL abandon after failed attempts on %s — hold to settle",
                    pos["ticker"],
                )
                pos["exit_abandoned"] = True
                stats = state.setdefault("stats", {})
                stats["exit_abandoned"] = int(stats.get("exit_abandoned") or 0) + 1
            set_series_position(state, series, pos)
            save_state(state)
            return
        # Armed but still within trail — hold; skip MIDCUT
        set_series_position(state, series, pos)
        save_state(state)
        return

    # Persist peak updates even when not exiting
    set_series_position(state, series, pos)
    save_state(state)

    # Mode 2 MIDCUT: ~7.5m, trailing never armed, bid not progressing (< entry+0.05)
    if pos.get("exit_abandoned"):
        return
    age = window_age_sec(pos.get("close_time"))
    if age is None or age < MIDCUT_AGE_SEC:
        return
    progress_floor = entry + MIDCUT_PROGRESS_ADD
    if bid_f + 1e-9 >= progress_floor:
        return  # some progress — hold (trail may still arm later)
    attempts = int(pos.get("midcut_attempts") or 0)
    if attempts >= MAX_MIDCUT_ATTEMPTS:
        LOGGER.warning(
            "MIDCUT abandon %s after %s attempts — hold to settle age=%.0fs bid=%.4f entry=%.4f",
            pos["ticker"], attempts, age, bid_f, entry,
        )
        pos["exit_abandoned"] = True
        stats = state.setdefault("stats", {})
        stats["exit_abandoned"] = int(stats.get("exit_abandoned") or 0) + 1
        set_series_position(state, series, pos)
        save_state(state)
        return
    LOGGER.info(
        "MIDCUT %s age=%.0fs bid=%.4f entry=%.4f peak=%.4f trail_armed=%s progress_floor=%.4f attempt=%s/%s",
        pos["ticker"], age, bid_f, entry, peak, trail_armed, progress_floor,
        attempts + 1, MAX_MIDCUT_ATTEMPTS,
    )
    ok = close_sell(pos, bid_f, aggressive=True)
    pos["midcut_attempts"] = attempts + 1
    if ok:
        stats = state.setdefault("stats", {})
        stats["midcut"] = int(stats.get("midcut") or 0) + 1
        set_series_position(state, series, None)
        save_state(state)
        return
    if pos["midcut_attempts"] >= MAX_MIDCUT_ATTEMPTS:
        LOGGER.warning(
            "MIDCUT abandon after failed attempts on %s — hold to settle",
            pos["ticker"],
        )
        pos["exit_abandoned"] = True
        stats = state.setdefault("stats", {})
        stats["exit_abandoned"] = int(stats.get("exit_abandoned") or 0) + 1
    set_series_position(state, series, pos)
    save_state(state)


def manage_position(state: dict, mode: str) -> None:
    """Manage all open positions (multi-series) or legacy single slot."""
    positions = get_positions_map(state)
    if positions:
        for series, pos in list(positions.items()):
            if not pos:
                continue
            manage_one_position(state, mode, series, pos)
            state = load_state()  # refresh after each (settle may have saved)
            state["armed"] = True  # preserve; caller re-sets pid/mode
        return
    # Legacy fallback
    pos = state.get("position")
    if not pos:
        return
    series = _series_of_position(pos) or SERIES
    manage_one_position(state, mode, series, pos)


def _record_settle(state: dict, pos: dict, result: str) -> None:
    won = result == str(pos["side"]).lower()
    pnl = round((float(pos["contracts"]) if won else 0.0) - float(pos.get("cost") or 0), 4)
    LOGGER.info(
        "SETTLED %s %s result=%s won=%s pnl=%s abandoned=%s",
        pos["ticker"], pos["side"], result, won, pnl, pos.get("exit_abandoned"),
    )
    stats = state.setdefault("stats", {})
    if won:
        stats["settle_wins"] = int(stats.get("settle_wins") or 0) + 1
    else:
        stats["settle_losses"] = int(stats.get("settle_losses") or 0) + 1
    state["last_settle"] = {
        "ticker": pos["ticker"],
        "series": _series_of_position(pos),
        "result": result,
        "close_time": pos.get("close_time"),
        "side": pos["side"],
        "won": won,
        "pnl": pnl,
    }
    series = _series_of_position(pos) or SERIES
    set_series_position(state, series, None)
    save_state(state)


def try_enter_first_phase(state: dict, cash: float, trend: dict) -> str:
    series = SERIES
    if get_series_position(state, series):
        return f"already in position ({series})"
    if cash is None or cash < 0.50:
        return f"cash too low {cash}"
    opens = list_open_series(series)
    if not opens:
        return f"no open {series} market"
    market = opens[0]
    close_time = str(market.get("close_time") or "")
    rem = seconds_remaining(close_time)
    if rem is None:
        return "bad close_time"
    if already_traded(state, series, close_time):
        return f"already traded this interval {market['ticker']}"
    if not (rem > FP_REM_LO and rem <= FP_REM_HI):
        return (
            f"waiting first_phase window rem={rem:.0f}s "
            f"(need ({FP_REM_LO:.0f},{FP_REM_HI:.0f}]) on {market['ticker']}"
        )
    side, reason = choose_first_phase_side(trend, market)
    LOGGER.info("first_phase signal: %s bias=%s", reason, trend.get("bias"))
    if not side:
        return reason
    # Cap total open across tracked series
    if open_position_count(state) >= MAX_OPEN:
        return f"already at max open positions ({MAX_OPEN})"
    ex = exchange_series_open(series)
    if ex is True:
        return f"exchange already has open {series} position"
    count = exchange_open_count(ALL_SERIES)
    if count is not None and count >= MAX_OPEN:
        return f"exchange already has {count} open position(s) in tracked series"
    # Mode 1: no fixed TP — use sentinel high so manage uses any-gain path
    pos = place_entry(
        market,
        side,
        cash,
        tactic="first_phase",
        tp_price=0.0,
        extra={"exit_rule": "any_gain_1c", "trend_bias": trend.get("bias")},
    )
    state["last_entry_attempt"] = {
        "ts": utc_now().isoformat(),
        "ticker": market["ticker"],
        "series": series,
        "side": side,
        "mode": "first_phase",
        "reason": reason,
        "filled": bool(pos),
    }
    if pos:
        set_series_position(state, series, pos)
        mark_traded(state, series, close_time)
        stats = state.setdefault("stats", {})
        stats["entries"] = int(stats.get("entries") or 0) + 1
        save_state(state)
        return (
            f"ENTERED first_phase {pos['ticker']} {side.upper()} @ {pos['entry_price']} "
            f"oid={pos['order_id']}"
        )
    save_state(state)
    return f"entry attempt no fill on {market['ticker']} {side}"


def try_enter_fade(
    state: dict, cash: float, trend: dict, series: str | None = None
) -> str:
    """Mode 2: confirm-window FOLLOW (age 60–120s); skip MIXED; VWAP+ask gates.

    Per-series evaluator/executor. Caller may rank series by EMA gap when
    multiple qualify; MAX_OPEN=1 blocks new entries while any position is open
    (existing opens are still managed elsewhere).
    """
    series = str(series or SERIES).upper()
    label_asset = asset_label(series)
    if get_series_position(state, series):
        return f"already in position ({series})"
    if cash is None or cash < 0.50:
        return f"cash too low {cash}"
    if open_position_count(state) >= MAX_OPEN:
        return f"max concurrent open ({open_position_count(state)}/{MAX_OPEN}) — skip new entries"

    market, src = resolve_fade_market(series)
    if not market:
        return f"no open {series} market"
    close_time = str(market.get("close_time") or "")
    age = window_age_sec(close_time)
    rem = seconds_remaining(close_time)
    if age is None or rem is None:
        return "bad close_time"
    if rem <= 0 or age < 0 or age > INTERVAL_SECONDS:
        mark_transition_obs()
        return (
            f"no current active interval age={age:.0f}s rem={rem:.0f}s "
            f"on {market['ticker']}"
        )
    if already_traded(state, series, close_time):
        return f"already traded this interval {market['ticker']}"

    settled = list_recent_settled(12, series=series)
    prior = prior_settle_for(close_time, settled)
    prior_ok = bool(prior and prior.get("result") in {"yes", "no"})
    bias = str(trend.get("bias") or "MIXED").upper()

    # Skip MIXED/SIDEWAYS entirely — no fade
    if bias in {"MIXED", "SIDEWAYS"}:
        LOGGER.info(
            "skip MIXED | %s age=%.0fs ticker=%s", label_asset, age, market["ticker"]
        )
        return f"skip MIXED ({series})"

    # Confirm window [60, 120]s; listing-lag catch-up retargeted to same window.
    allowed, bound_age, age_path = mode2_entry_allowed(
        age, need_prior=False, prior_ok=prior_ok
    )
    if not allowed:
        if age_path == "too_early":
            return (
                f"waiting confirm window (age={age:.0f}s < {bound_age:.0f}s, "
                f"rem={None if rem is None else round(rem)}s) on {market['ticker']}"
            )
        return (
            f"confirm window missed (age={age:.0f}s > {bound_age:.0f}s, "
            f"rem={None if rem is None else round(rem)}s) on {market['ticker']}"
        )
    if age < -2:
        return f"market not open yet age={age:.0f}s"
    if age_path == "confirm_catchup":
        LOGGER.info(
            "Mode2 confirm catch-up %s age=%.0fs src=%s ticker=%s (window %s–%ss)",
            label_asset,
            age,
            src,
            market["ticker"],
            ENTRY_CONFIRM_MIN_AGE_SEC,
            ENTRY_CONFIRM_MAX_AGE_SEC,
        )
    else:
        LOGGER.info(
            "Mode2 confirm window %s age=%.0fs src=%s ticker=%s (window %s–%ss)",
            label_asset,
            age,
            src,
            market["ticker"],
            ENTRY_CONFIRM_MIN_AGE_SEC,
            ENTRY_CONFIRM_MAX_AGE_SEC,
        )

    if prior_ok:
        state["last_settle"] = prior

    side, label = mode2_side_from_trend(bias, (prior or {}).get("result"))
    LOGGER.info(
        "%s | %s bias=%s prior=%s age=%.0fs rem=%s ticker=%s",
        label,
        label_asset,
        bias,
        (prior or {}).get("result"),
        age,
        None if rem is None else round(rem),
        market["ticker"],
    )
    if not side:
        LOGGER.info("%s", label)
        return f"{label} ({series})"

    # VWAP alignment for FOLLOW (per series)
    vwap_ok, vwap_msg = mode2_vwap_aligned(side, bias, series=series)
    if not vwap_ok:
        LOGGER.info("%s | %s | %s", label, label_asset, vwap_msg)
        return f"{vwap_msg} ({series})"
    LOGGER.info("%s | %s | %s", label, label_asset, vwap_msg)

    ex = exchange_series_open(series)
    if ex is True:
        return f"exchange already has open {series} position"
    count = exchange_open_count(ALL_SERIES)
    if count is not None and count >= MAX_OPEN:
        return f"exchange already has {count} open position(s) in tracked series"

    # Preview ask for skip bands (+ Mode2 hard ask>0.70)
    ask = ask_for_side(market, side)
    if ask is not None:
        try:
            ask_f = float(ask)
            if ask_f > MODE2_ASK_MAX:
                msg = f"skip Mode2 ask>{MODE2_ASK_MAX} {side} ask={ask}"
                LOGGER.info("%s | %s", label_asset, msg)
                return f"{msg} ({series})"
            if ask_f > FP_ASK_MAX or ask_f < FP_ASK_MIN:
                return f"skip Mode2 ask out of band {side} ask={ask} ({series})"
        except (TypeError, ValueError):
            pass

    # Re-check cash right before send (multi entries same cycle)
    live_cash = safe_balance()
    if live_cash is not None:
        cash = live_cash
    if cash < 0.50:
        return f"cash too low pre-entry {cash}"

    # TP = entry + 0.20 computed after fill inside place_entry (tp_price=None)
    pos = place_entry(
        market,
        side,
        cash,
        tactic="fade",
        tp_price=None,
        extra={
            "mode2_label": label,
            "trend_bias": bias,
            "prior_result": (prior or {}).get("result"),
        },
    )
    state["last_entry_attempt"] = {
        "ts": utc_now().isoformat(),
        "ticker": market["ticker"],
        "series": series,
        "side": side,
        "mode": "fade",
        "label": label,
        "bias": bias,
        "filled": bool(pos),
    }
    if pos:
        # Mode2 trailing: no fixed TP; ensure trail fields present
        fill_px = float(pos["entry_price"])
        pos["tp_price"] = None
        pos["peak_bid"] = round(min(TP_CAP, fill_px), 4)
        pos["trail_armed"] = False
        pos.setdefault("trail_attempts", 0)
        set_series_position(state, series, pos)
        mark_traded(state, series, close_time)
        stats = state.setdefault("stats", {})
        stats["entries"] = int(stats.get("entries") or 0) + 1
        if "FOLLOW" in label:
            stats["mode2_follow"] = int(stats.get("mode2_follow") or 0) + 1
        else:
            stats["mode2_fade"] = int(stats.get("mode2_fade") or 0) + 1
        save_state(state)
        return (
            f"ENTERED {label} {pos['ticker']} {side.upper()} @ {pos['entry_price']} "
            f"trail=arm+{TRAIL_ARM_ADD:.2f}/dd-{TRAIL_DRAWDOWN:.2f} "
            f"oid={pos['order_id']}"
        )
    save_state(state)
    return f"entry attempt no fill on {market['ticker']} {side} ({label})"


def write_status(
    state: dict,
    *,
    trend: dict | None = None,
    trends: dict | None = None,
    extra: dict | None = None,
    offline: bool = False,
) -> None:
    trends = trends or state.get("last_trends") or {}
    trend = trend or state.get("last_trend") or trends.get(SERIES) or detect_trend(series=SERIES)
    positions = get_positions_map(state)
    cash = (extra or {}).get("cash")
    armed = bool(state.get("armed")) and not offline
    mode = state.get("mode") or (extra or {}).get("mode")
    series_csv = ",".join(MODE2_SERIES)
    lines = [
        "# Multi-Asset 15m Dual-Mode Terminal Status",
        "",
        f"- Updated: {et_now().strftime('%Y-%m-%d %H:%M:%S %Z')}",
        f"- Armed: **{'ON' if armed else 'OFF'}**",
        f"- Mode: {mode or '(none — use start --mode …)'}",
        f"- Series (Mode2): **{series_csv}**",
        f"- PID: {state.get('pid') or (os.getpid() if armed else 'n/a')}",
        f"- Cash (Ex2): {cash if cash is not None else 'n/a'}",
        f"- Open positions: {open_position_count(state)} / {MAX_OPEN}",
        f"- Log: `{LOG_PATH}`",
        f"- State: `{STATE_PATH}`",
        "",
        "## Trends",
        "",
    ]
    if trends:
        for s in MODE2_SERIES:
            t = trends.get(s)
            if t:
                lines += format_trend_block(t if "bias" in t else {"series": s, **t})
                lines.append("")
    else:
        lines += format_trend_block(trend)
        lines.append("")
    lines += [
        "## Mode 2 direction rule (final, per market)",
        "",
        "- Markets: KXBTC15M/BTCUSDT · KXETH15M/ETHUSDT · KXSOL15M/SOLUSDT",
        "- UP → FOLLOW YES (spot>VWAP120m) · DOWN → FOLLOW NO (spot<VWAP120m) · MIXED → skip",
        "- EMA3/9 picks FOLLOW side; VWAP + ask≤0.70 gate entries; MIXED skipped",
        "- Confirm window age∈[60,120]s (no exact-open ≤20s); listing-lag ok inside window",
        "- Trailing exit: arm when peak≥entry+0.10; exit when bid≤peak−0.08; sell cap 0.99; no fixed TP+0.20",
        "- Max 1 concurrent open across BTC+ETH+SOL; same-cycle pick=clearest EMA gap then BTC→ETH→SOL",
        "",
        "## Last settle",
        "",
    ]
    ls = state.get("last_settle") or {}
    if ls:
        lines.append(
            f"- `{ls.get('ticker')}` result=**{str(ls.get('result') or '').upper()}** "
            f"close={ls.get('close_time')}"
        )
    else:
        lines.append("- (none yet)")
    lines += ["", "## Positions", ""]
    if positions:
        for s, pos in positions.items():
            if not pos:
                continue
            abandoned = " exit_abandoned=YES" if pos.get("exit_abandoned") else ""
            trail_txt = (
                f"peak={pos.get('peak_bid')} armed={pos.get('trail_armed')}"
                if pos.get("tactic") == "fade" or pos.get("peak_bid") is not None
                else f"tp={pos.get('tp_price')}"
            )
            lines.append(
                f"- OPEN `{pos.get('ticker')}` side=**{str(pos.get('side') or '').upper()}** "
                f"qty={pos.get('contracts')} entry={pos.get('entry_price')} "
                f"{trail_txt} tactic={pos.get('tactic')} "
                f"label={pos.get('mode2_label') or pos.get('exit_rule') or ''} "
                f"oid={pos.get('order_id')}{abandoned}"
            )
    else:
        wait = (extra or {}).get("wait_note") or ("OFF / flat" if not armed else "flat / waiting")
        lines.append(f"- {wait}")
    lines += [
        "",
        "## Rules (short)",
        "",
        "- Mode 1 first_phase (BTC): rem∈(50,70], side from trend+cheap ask, any ≥1¢ net exit",
        "- Mode 2 FOLLOW (BTC+ETH+SOL): confirm age∈[60,120]s; VWAP align; ask≤0.70; trail arm+10¢ stop−8¢ from peak (cap 0.99); MIDCUT@7.5m if trail never armed & bid<entry+0.05; max 3 tries",
        "- Stake ~$1 IOC; max 1 concurrent across series; Ex2 cash; NEVER deposit/withdraw/bank",
        "",
        "## Stats",
        "",
        f"- {json.dumps(state.get('stats') or {})}",
        "",
    ]
    STATUS_PATH.write_text("\n".join(lines), encoding="utf-8")


def print_status_stdout(state: dict, trend: dict, trends: dict | None = None) -> None:
    armed = "ON" if state.get("armed") else "OFF"
    print(
        f"btc_terminal armed={armed} mode={state.get('mode') or 'n/a'} "
        f"pid={state.get('pid') or 'n/a'} series={','.join(MODE2_SERIES)}"
    )
    trends = trends or state.get("last_trends") or {}
    if trends:
        for s in MODE2_SERIES:
            t = trends.get(s) or {}
            print(
                f"trend[{asset_label(s)}] bias={t.get('bias')} | {t.get('note')}"
            )
            print(
                f"  mode2={t.get('mode2_direction')} — {t.get('mode2_note')}"
            )
    else:
        print(f"trend bias={trend.get('bias')} | {trend.get('note')}")
        print(f"advice={trend.get('advice')} — {trend.get('advice_note')}")
        print(f"mode2={trend.get('mode2_direction')} — {trend.get('mode2_note')}")
    positions = get_positions_map(state)
    if positions:
        for s, pos in positions.items():
            if not pos:
                continue
            print(
                f"position[{asset_label(s)}] {pos.get('ticker')} "
                f"{str(pos.get('side') or '').upper()} "
                f"entry={pos.get('entry_price')} tp={pos.get('tp_price')} "
                f"abandoned={pos.get('exit_abandoned')}"
            )
    else:
        print("position: none")
    print(f"status_file: {STATUS_PATH}")


def cmd_status() -> int:
    state = load_state()
    # If marked armed but process gone, show OFF
    pid = state.get("pid")
    if state.get("armed") and pid:
        try:
            os.kill(int(pid), 0)
        except (OSError, TypeError, ValueError):
            state["armed"] = False
            state["pid"] = None
            save_state(state)
    trends = {}
    for s in MODE2_SERIES:
        t = detect_trend(series=s)
        trends[s] = {
            "series": s,
            "asset": t.get("asset"),
            "bias": t.get("bias"),
            "note": t.get("note"),
            "mode2_direction": t.get("mode2_direction"),
            "mode2_note": t.get("mode2_note"),
            "advice": t.get("advice"),
            "advice_note": t.get("advice_note"),
            "ts": utc_now().isoformat(),
        }
    trend = detect_trend(series=SERIES)  # full BTC block for primary
    state["last_trend"] = trends.get(SERIES)
    state["last_trends"] = trends
    save_state(state)
    write_status(state, trend=trend, trends=trends, offline=not state.get("armed"))
    print_status_stdout(state, trend, trends=trends)
    return 0


def cmd_trend() -> int:
    trends = {}
    for s in MODE2_SERIES:
        t = detect_trend(series=s)
        print(f"[{asset_label(s)}/{s}] bias={t.get('bias')}")
        print(f"  note={t.get('note')}")
        print(f"  advice={t.get('advice')} — {t.get('advice_note')}")
        print(f"  mode2={t.get('mode2_direction')} — {t.get('mode2_note')}")
        trends[s] = {
            "series": s,
            "asset": t.get("asset"),
            "bias": t.get("bias"),
            "note": t.get("note"),
            "mode2_direction": t.get("mode2_direction"),
            "mode2_note": t.get("mode2_note"),
            "ts": utc_now().isoformat(),
        }
    state = load_state()
    state["last_trend"] = trends.get(SERIES)
    state["last_trends"] = trends
    save_state(state)
    write_status(
        state,
        trend=detect_trend(series=SERIES),
        trends=trends,
        offline=not state.get("armed"),
    )
    return 0


def _kill_patterns(patterns: list[str]) -> list[str]:
    """Kill matching processes (fade_btc / omega.worker / this terminal)."""
    killed = []
    for pat in patterns:
        try:
            out = subprocess.check_output(["pgrep", "-af", pat], text=True)
        except subprocess.CalledProcessError:
            continue
        except FileNotFoundError:
            break
        for line in out.splitlines():
            line = line.strip()
            if not line:
                continue
            # skip our own pgrep / shell wrappers
            if "pgrep" in line or "btc_terminal stop" in line:
                continue
            parts = line.split(None, 1)
            try:
                pid = int(parts[0])
            except ValueError:
                continue
            if pid == os.getpid():
                continue
            try:
                os.kill(pid, signal.SIGTERM)
                killed.append(f"{pid}:{pat}")
                LOGGER.info("killed %s (%s)", pid, pat)
            except OSError:
                pass
    time.sleep(0.4)
    for item in list(killed):
        pid = int(item.split(":")[0])
        try:
            os.kill(pid, 0)
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    return killed


def cmd_stop() -> int:
    setup_logging()
    state = load_state()
    killed = _kill_patterns(
        [
            "python -m omega.btc_terminal",
            "omega.btc_terminal",
            "fade_btc",
            "omega.worker",
            "omega.fade_btc",
        ]
    )
    state["armed"] = False
    state["pid"] = None
    # keep mode/position for inspection; do not invent flat if holding on exchange
    save_state(state)
    trend = detect_trend()
    write_status(
        state,
        trend=trend,
        offline=True,
        extra={"wait_note": f"STOPPED (killed={killed or 'none'})"},
    )
    print(f"stopped armed=false killed={killed or []}")
    return 0


def run_loop(mode: str) -> None:
    setup_logging()
    reset_key_cache()
    if not has_keys():
        LOGGER.error("No Kalshi keys at %s — cannot go LIVE", config.KEY_DIR)
        return
    live_env = os.getenv("LIVE", "").strip().lower()
    if live_env not in {"1", "true", "yes", "on"}:
        LOGGER.error("LIVE=1 required to start — refusing")
        return

    state = load_state()
    state["armed"] = True
    state["mode"] = mode
    state["pid"] = os.getpid()
    save_state(state)

    series_list = list(MODE2_SERIES) if mode == "fade" else [SERIES]
    LOGGER.info(
        "Multi-asset terminal LIVE start mode=%s series=%s stake~$%.2f max_open=%s. "
        "Mode2 rules per market: confirm age∈[%ss,%ss], UP→YES/DOWN→NO FOLLOW+VWAP120m, "
        "skip MIXED, ask≤0.70, TRAIL arm+0.10/dd-0.08 (cap 0.99, no fixed TP+0.20), "
        "MIDCUT@7.5m if trail never armed & bid<entry+0.05, max 3 tries. "
        "Max1 concurrent across series; same-cycle pick=clearest EMA gap then BTC→ETH→SOL. "
        "Mode1: rem(50,70] any≥1¢ (BTC only). Cash-gated; no deposits.",
        mode, ",".join(series_list), STAKE, MAX_OPEN if mode == "fade" else 1,
        ENTRY_CONFIRM_MIN_AGE_SEC, ENTRY_CONFIRM_MAX_AGE_SEC,
    )
    LOGGER.info(
        "Mode2 markets: KXBTC15M/BTCUSDT + KXETH15M/ETHUSDT + KXSOL15M/SOLUSDT | "
        "max_concurrent_open=%s confirm_window=%s–%ss trail=arm+%.2f/dd-%.2f",
        MAX_OPEN, ENTRY_CONFIRM_MIN_AGE_SEC, ENTRY_CONFIRM_MAX_AGE_SEC,
        TRAIL_ARM_ADD, TRAIL_DRAWDOWN,
    )

    def _on_sig(_signum, _frame):
        STOP.set()

    signal.signal(signal.SIGTERM, _on_sig)
    signal.signal(signal.SIGINT, _on_sig)

    cycle = 0
    while not STOP.is_set():
        # honor external stop (armed=false)
        disk = load_state()
        if disk.get("armed") is False and disk.get("pid") != os.getpid():
            pass
        if disk.get("armed") is False and str(disk.get("pid")) != str(os.getpid()):
            LOGGER.info("armed=false on disk — exiting loop")
            break
        if not disk.get("armed") and disk.get("pid") is None:
            LOGGER.info("stop cleared armed/pid — exiting")
            break

        cycle += 1
        cash = safe_balance()
        notes: list[str] = []
        sleep_for = POLL_SEC
        trends: dict = {}
        for s in series_list:
            try:
                trends[s] = detect_trend(series=s)
            except Exception as exc:
                LOGGER.warning("trend %s failed: %s", s, type(exc).__name__)
                trends[s] = {
                    "series": s,
                    "asset": asset_label(s),
                    "bias": "MIXED",
                    "note": f"trend error {type(exc).__name__}",
                    "mode2_direction": "SKIP MIXED",
                    "mode2_note": "trend unavailable",
                }

        state = load_state()
        state["armed"] = True
        state["mode"] = mode
        state["pid"] = os.getpid()
        state["last_trends"] = {
            s: {
                "series": s,
                "asset": trends[s].get("asset"),
                "bias": trends[s].get("bias"),
                "note": trends[s].get("note"),
                "mode2_direction": trends[s].get("mode2_direction"),
                "mode2_note": trends[s].get("mode2_note"),
                "ts": utc_now().isoformat(),
            }
            for s in series_list
        }
        state["last_trend"] = state["last_trends"].get(SERIES)

        try:
            manage_position(state, mode)
            state = load_state()
            state["armed"] = True
            state["mode"] = mode
            state["pid"] = os.getpid()

            # Entries: Mode2 loops all series; Mode1 BTC only
            if cash is not None:
                if mode == "first_phase":
                    if not get_series_position(state, SERIES):
                        note = try_enter_first_phase(
                            state, cash, trends.get(SERIES) or detect_trend(series=SERIES)
                        )
                        notes.append(note)
                        state = load_state()
                        state["armed"] = True
                        state["mode"] = mode
                        state["pid"] = os.getpid()
                else:
                    # Holding notes for any open (manage already ran); block NEW if max1.
                    for s in series_list:
                        pos = get_series_position(state, s)
                        if pos:
                            notes.append(
                                f"holding[{asset_label(s)}] {pos['ticker']} "
                                f"{pos['side'].upper()} entry={pos['entry_price']} "
                                f"tp={pos.get('tp_price')}"
                            )
                    n_open = open_position_count(state)
                    if n_open >= MAX_OPEN:
                        notes.append(
                            f"max1 concurrent open={n_open}/{MAX_OPEN} — skip new entries"
                        )
                    else:
                        # Rank candidates: clearest EMA gap first; tie → BTC→ETH→SOL.
                        ranked = sorted(
                            series_list,
                            key=lambda s: (
                                -mode2_ema_gap_score(trends.get(s) or {}),
                                MODE2_SERIES.index(s)
                                if s in MODE2_SERIES
                                else 99,
                            ),
                        )
                        for s in ranked:
                            state = load_state()
                            state["armed"] = True
                            state["mode"] = mode
                            state["pid"] = os.getpid()
                            if get_series_position(state, s):
                                continue
                            if open_position_count(state) >= MAX_OPEN:
                                notes.append(
                                    f"max1 hit after prior entry — skip remaining"
                                )
                                break
                            cash_now = safe_balance()
                            if cash_now is None:
                                notes.append(f"{s}: balance unavailable")
                                break
                            if cash_now < 0.50:
                                notes.append(f"{s}: cash too low {cash_now:.4f}")
                                break
                            gap = mode2_ema_gap_score(trends.get(s) or {})
                            note = try_enter_fade(
                                state, cash_now, trends.get(s) or {}, series=s
                            )
                            notes.append(
                                f"[{asset_label(s)} gap={gap:.5f}] {note}"
                            )
                            state = load_state()
                            state["armed"] = True
                            state["mode"] = mode
                            state["pid"] = os.getpid()
                            # Only one new entry per cycle under max1
                            if open_position_count(state) >= MAX_OPEN or note.startswith(
                                "ENTERED"
                            ):
                                break
            else:
                notes.append("balance unavailable")

            # Poll cadence: Mode2 fast near open / catch-up / empty list
            try:
                if mode == "fade" and near_interval_boundary():
                    sleep_for = OPEN_POLL_SEC
                any_open = False
                for s in series_list:
                    opens = list_open_series(s)
                    if opens:
                        any_open = True
                        age = window_age_sec(opens[0].get("close_time"))
                        rem = seconds_remaining(opens[0].get("close_time"))
                        if rem is not None and rem <= 0 and mode == "fade":
                            mark_transition_obs()
                            sleep_for = OPEN_POLL_SEC
                        if mode == "fade":
                            # Fast poll from open through end of confirm window (0–120s)
                            if age is not None and 0 <= age <= ENTRY_CONFIRM_MAX_AGE_SEC:
                                sleep_for = OPEN_POLL_SEC
                            elif rem is not None and rem <= NEAR_OPEN_SEC:
                                sleep_for = OPEN_POLL_SEC
                        else:
                            if rem is not None and FP_REM_LO < rem <= FP_REM_HI + 15:
                                sleep_for = FP_POLL_SEC
                    elif mode == "fade":
                        mark_transition_obs()
                        sleep_for = OPEN_POLL_SEC
                if mode == "fade" and not any_open:
                    sleep_for = OPEN_POLL_SEC
            except Exception:
                if mode == "fade":
                    sleep_for = OPEN_POLL_SEC
                    mark_transition_obs()

            # Holding note if we only managed
            if not notes and open_position_count(state):
                for s, pos in get_positions_map(state).items():
                    if not pos:
                        continue
                    notes.append(
                        f"holding {pos['ticker']} {pos['side'].upper()} "
                        f"entry={pos['entry_price']} tp={pos.get('tp_price')} "
                        f"tactic={pos.get('tactic')} abandoned={pos.get('exit_abandoned')}"
                    )
                    rem = seconds_remaining(pos.get("close_time"))
                    if rem is not None and rem <= NEAR_OPEN_SEC:
                        sleep_for = OPEN_POLL_SEC
        except Exception as exc:
            LOGGER.exception("cycle error: %s", exc)
            notes.append(f"error {type(exc).__name__}")

        note = " | ".join(notes) if notes else ""
        bias_summary = ",".join(
            f"{asset_label(s)}={trends.get(s, {}).get('bias')}" for s in series_list
        )
        if cycle <= 3 or cycle % 10 == 0 or sleep_for <= OPEN_POLL_SEC:
            if cycle <= 5 or cycle % (10 if sleep_for > OPEN_POLL_SEC else 20) == 0:
                LOGGER.info(
                    "cycle=%s mode=%s cash=%s open=%s/%s poll=%.2fs %s %s",
                    cycle,
                    mode,
                    None if cash is None else round(cash, 4),
                    open_position_count(state),
                    MAX_OPEN if mode == "fade" else 1,
                    sleep_for,
                    bias_summary,
                    note,
                )
        primary_trend = trends.get(SERIES) or detect_trend(series=SERIES)
        write_status(
            state,
            trend=primary_trend,
            trends=trends,
            extra={"cash": cash, "wait_note": note or "flat / waiting", "mode": mode},
        )
        save_state(state)
        STOP.wait(sleep_for)

    state = load_state()
    state["armed"] = False
    state["pid"] = None
    save_state(state)
    write_status(state, offline=True, extra={"wait_note": "loop exited"})
    LOGGER.info("btc_terminal loop exited")


def cmd_start(mode: str) -> int:
    setup_logging()
    live_env = os.getenv("LIVE", "").strip().lower()
    if live_env not in {"1", "true", "yes", "on"}:
        print("REFUSED: start requires LIVE=1 (no paper auto-start). Example:")
        print(f"  LIVE=1 python -m omega.btc_terminal start --mode {mode}")
        state = load_state()
        trend = detect_trend()
        write_status(
            state,
            trend=trend,
            offline=True,
            extra={"wait_note": "start refused — LIVE=1 not set"},
        )
        return 2
    if mode not in MODES:
        print(f"unknown mode {mode!r}; choose from {MODES}")
        return 2
    if not has_keys():
        print(f"REFUSED: no Kalshi keys at {config.KEY_DIR}")
        return 2

    # Kill leftover traders before arming
    killed = _kill_patterns(
        ["fade_btc", "omega.fade_btc", "omega.worker", "python -m omega.worker"]
    )
    if killed:
        LOGGER.info("pre-start killed leftovers: %s", killed)

    # Detach? User runs start in foreground typically; run loop in-process.
    run_loop(mode)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m omega.btc_terminal")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("status", help="Show OFF/ON status + trend (default)")
    sub.add_parser("trend", help="Show trend only")
    sp = sub.add_parser("start", help="Start LIVE loop (requires LIVE=1)")
    sp.add_argument(
        "--mode",
        required=True,
        choices=MODES,
        help="first_phase | fade",
    )
    sub.add_parser("stop", help="Stop terminal + mark armed=false")
    return p


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        return cmd_status()
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.cmd in (None, "status"):
        return cmd_status()
    if args.cmd == "trend":
        return cmd_trend()
    if args.cmd == "stop":
        return cmd_stop()
    if args.cmd == "start":
        return cmd_start(args.mode)
    return cmd_status()


if __name__ == "__main__":
    raise SystemExit(main())
