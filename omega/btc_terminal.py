"""Kalshi BTC 15m dual-mode terminal (KXBTC15M).

Modes:
  first_phase — enter last ~1 min (rem in (50,70]), side from trend + cheap ask;
                exit any ~≥1¢ net after fees, else hold to settle.
  fade        — exact-open entry; direction from trend:
                  UP→YES follow, DOWN→NO follow, MIXED→fade prior settle;
                TP = entry+0.20 capped 0.99; max 3 TP sell attempts then abandon.

CLI (user activates manually; default is OFF / status only):
  python -m omega.btc_terminal status
  python -m omega.btc_terminal trend
  LIVE=1 python -m omega.btc_terminal start --mode first_phase
  LIVE=1 python -m omega.btc_terminal start --mode fade
  python -m omega.btc_terminal stop

HARD LOCK: never deposit/withdraw/bank. Ex2 cash only. Stake ~$1. One open max.
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
from omega.btc_trend import detect_trend, format_trend_block, mode2_side_from_trend
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

SERIES = "KXBTC15M"
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
MAX_OPEN = 1

# Mode 1 — first_phase
FP_REM_LO = 50.0  # exclusive lower
FP_REM_HI = 70.0  # inclusive upper → rem in (50, 70]
FP_ASK_MIN = 0.05
FP_ASK_MAX = 0.96
FP_MIXED_ASK_CAP = 0.85
FP_ANY_GAIN = 0.01  # ~1¢ net after fees

# Mode 2 — fade / follow
ENTRY_MAX_AGE_SEC = 20
ENTRY_SETTLE_GRACE_SEC = 45
TP_ADD = 0.20  # +20 cents from entry
TP_CAP = 0.99
MAX_TP_ATTEMPTS = 3
POLL_SEC = 2.0
OPEN_POLL_SEC = 0.25
NEAR_OPEN_SEC = 30
FP_POLL_SEC = 1.0

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
        "position": None,
        "traded_close_times": [],
        "last_settle": None,
        "last_entry_attempt": None,
        "last_trend": None,
        "stats": {
            "entries": 0,
            "tp": 0,
            "any_gain": 0,
            "settle_wins": 0,
            "settle_losses": 0,
            "exit_abandoned": 0,
            "mode2_follow": 0,
            "mode2_fade": 0,
        },
        "updated_at": None,
    }


def load_state() -> dict:
    if STATE_PATH.is_file():
        try:
            data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            base = default_state()
            base.update(data or {})
            return base
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


def list_open_btc() -> list[dict]:
    payload = _kalshi_get(
        "/markets",
        params={"series_ticker": SERIES, "status": "open", "limit": 10},
        timeout=10,
    )
    markets = []
    for m in payload.get("markets") or []:
        ticker = m.get("ticker") or ""
        if not str(ticker).startswith(SERIES):
            continue
        yes_bid = _dollars(m, "yes_bid_dollars", "yes_bid")
        yes_ask = _dollars(m, "yes_ask_dollars", "yes_ask")
        markets.append(
            {
                "ticker": ticker,
                "series": SERIES,
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
    markets.sort(key=lambda x: x.get("close_time") or "")
    return markets


def list_recent_settled(limit: int = 8) -> list[dict]:
    payload = _kalshi_get(
        "/markets",
        params={"series_ticker": SERIES, "status": "settled", "limit": limit},
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
    if tp_price is None:
        # Mode 2 style default if requested
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
        "series": SERIES,
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
        "exit_abandoned": False,
    }
    if extra:
        pos.update(extra)
    return pos


def close_sell(position: dict, bid: float) -> bool:
    """Sell IOC near bid. Returns True if filled."""
    ticker = position["ticker"]
    side = position["side"]
    qty = float(position["contracts"])
    px = round(max(0.01, float(bid)), 2)
    prices = []
    for p in (px, round(max(0.01, px - 0.01), 2), round(max(0.01, px - 0.02), 2)):
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


def exchange_open_count() -> int | None:
    try:
        live_pos = get_positions()
        open_count = 0
        for p in live_pos or []:
            fp = p.get("position_fp", p.get("position"))
            try:
                if abs(float(fp or 0)) > 1e-9:
                    open_count += 1
            except (TypeError, ValueError):
                pass
        return open_count
    except Exception as exc:
        LOGGER.warning("positions check failed: %s", type(exc).__name__)
        return None


def manage_position(state: dict, mode: str) -> None:
    pos = state.get("position")
    if not pos:
        return

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
                state["position"] = None
                save_state(state)
        return

    # Mode 2: fixed TP entry+0.20
    tp = float(pos.get("tp_price") or 0)
    if bid is not None and tp > 0 and float(bid) + 1e-9 >= tp:
        attempts = int(pos.get("tp_attempts") or 0)
        if attempts >= MAX_TP_ATTEMPTS:
            LOGGER.warning(
                "TP abandon %s after %s attempts — hold to settle, clear blocking",
                pos["ticker"], attempts,
            )
            pos["exit_abandoned"] = True
            stats = state.setdefault("stats", {})
            stats["exit_abandoned"] = int(stats.get("exit_abandoned") or 0) + 1
            state["position"] = pos
            save_state(state)
            return
        LOGGER.info(
            "TP trigger %s bid=%.4f >= tp=%.4f attempt=%s/%s",
            pos["ticker"], float(bid), tp, attempts + 1, MAX_TP_ATTEMPTS,
        )
        ok = close_sell(pos, float(bid))
        pos["tp_attempts"] = attempts + 1
        if ok:
            stats = state.setdefault("stats", {})
            stats["tp"] = int(stats.get("tp") or 0) + 1
            state["position"] = None
            save_state(state)
            return
        # failed attempt — if hit limit, abandon
        if pos["tp_attempts"] >= MAX_TP_ATTEMPTS:
            LOGGER.warning(
                "TP abandon after failed attempts on %s — hold to settle",
                pos["ticker"],
            )
            pos["exit_abandoned"] = True
            stats = state.setdefault("stats", {})
            stats["exit_abandoned"] = int(stats.get("exit_abandoned") or 0) + 1
        state["position"] = pos
        save_state(state)


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
        "result": result,
        "close_time": pos.get("close_time"),
        "side": pos["side"],
        "won": won,
        "pnl": pnl,
    }
    # Clear position so next open entry is never blocked after settle
    state["position"] = None
    save_state(state)


def try_enter_first_phase(state: dict, cash: float, trend: dict) -> str:
    if state.get("position"):
        return "already in position"
    opens = list_open_btc()
    if not opens:
        return "no open KXBTC15M market"
    market = opens[0]
    close_time = str(market.get("close_time") or "")
    rem = seconds_remaining(close_time)
    if rem is None:
        return "bad close_time"
    traded = set(state.get("traded_close_times") or [])
    if close_time in traded:
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
    count = exchange_open_count()
    if count is not None and count >= MAX_OPEN:
        return f"exchange already has {count} open position(s)"
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
        "side": side,
        "mode": "first_phase",
        "reason": reason,
        "filled": bool(pos),
    }
    if pos:
        state["position"] = pos
        traded.add(close_time)
        state["traded_close_times"] = sorted(traded)[-40:]
        stats = state.setdefault("stats", {})
        stats["entries"] = int(stats.get("entries") or 0) + 1
        save_state(state)
        return (
            f"ENTERED first_phase {pos['ticker']} {side.upper()} @ {pos['entry_price']} "
            f"oid={pos['order_id']}"
        )
    save_state(state)
    return f"entry attempt no fill on {market['ticker']} {side}"


def try_enter_fade(state: dict, cash: float, trend: dict) -> str:
    """Mode 2: exact-open; direction from trend (FOLLOW or FADE prior)."""
    if state.get("position"):
        return "already in position"
    opens = list_open_btc()
    if not opens:
        return "no open KXBTC15M market"
    market = opens[0]
    close_time = str(market.get("close_time") or "")
    age = window_age_sec(close_time)
    rem = seconds_remaining(close_time)
    if age is None:
        return "bad close_time"
    traded = set(state.get("traded_close_times") or [])
    if close_time in traded:
        return f"already traded this interval {market['ticker']}"

    settled = list_recent_settled(12)
    prior = prior_settle_for(close_time, settled)
    prior_ok = bool(prior and prior.get("result") in {"yes", "no"})
    bias = str(trend.get("bias") or "MIXED").upper()

    # Exact-open window; grace only if need prior for MIXED fade path
    need_prior = bias == "MIXED"
    max_age = ENTRY_MAX_AGE_SEC
    if need_prior and not prior_ok:
        max_age = ENTRY_SETTLE_GRACE_SEC
    if age > max_age:
        return (
            f"waiting for next open (age={age:.0f}s > {max_age}s, "
            f"rem={None if rem is None else round(rem)}s) on {market['ticker']}"
        )
    if age < -2:
        return f"market not open yet age={age:.0f}s"
    if need_prior and not prior_ok:
        return (
            f"at open MIXED but prior settle not ready age={age:.0f}s "
            f"on {market['ticker']} (retrying)"
        )

    if prior_ok:
        state["last_settle"] = prior

    side, label = mode2_side_from_trend(bias, (prior or {}).get("result"))
    LOGGER.info(
        "%s | bias=%s prior=%s age=%.0fs rem=%s ticker=%s",
        label,
        bias,
        (prior or {}).get("result"),
        age,
        None if rem is None else round(rem),
        market["ticker"],
    )
    if not side:
        return label

    count = exchange_open_count()
    if count is not None and count >= MAX_OPEN:
        return f"exchange already has {count} open position(s)"

    # Preview ask for skip bands
    ask = ask_for_side(market, side)
    if ask is not None:
        try:
            if float(ask) > FP_ASK_MAX or float(ask) < FP_ASK_MIN:
                return f"skip Mode2 ask out of band {side} ask={ask}"
        except (TypeError, ValueError):
            pass

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
    # Fix TP to entry+0.20 after fill (place_entry already does this when tp_price=None)
    state["last_entry_attempt"] = {
        "ts": utc_now().isoformat(),
        "ticker": market["ticker"],
        "side": side,
        "mode": "fade",
        "label": label,
        "bias": bias,
        "filled": bool(pos),
    }
    if pos:
        # Ensure TP is entry + 0.20
        fill_px = float(pos["entry_price"])
        tp = round(min(TP_CAP, fill_px + TP_ADD), 4)
        pos["tp_price"] = min(TP_CAP, math.ceil(tp * 100 - 1e-9) / 100.0)
        state["position"] = pos
        traded.add(close_time)
        state["traded_close_times"] = sorted(traded)[-40:]
        stats = state.setdefault("stats", {})
        stats["entries"] = int(stats.get("entries") or 0) + 1
        if "FOLLOW" in label:
            stats["mode2_follow"] = int(stats.get("mode2_follow") or 0) + 1
        else:
            stats["mode2_fade"] = int(stats.get("mode2_fade") or 0) + 1
        save_state(state)
        return (
            f"ENTERED {label} {pos['ticker']} {side.upper()} @ {pos['entry_price']} "
            f"tp={pos['tp_price']} oid={pos['order_id']}"
        )
    save_state(state)
    return f"entry attempt no fill on {market['ticker']} {side} ({label})"


def write_status(
    state: dict,
    *,
    trend: dict | None = None,
    extra: dict | None = None,
    offline: bool = False,
) -> None:
    trend = trend or state.get("last_trend") or detect_trend()
    pos = state.get("position")
    cash = (extra or {}).get("cash")
    armed = bool(state.get("armed")) and not offline
    mode = state.get("mode") or (extra or {}).get("mode")
    lines = [
        "# BTC 15m Dual-Mode Terminal Status",
        "",
        f"- Updated: {et_now().strftime('%Y-%m-%d %H:%M:%S %Z')}",
        f"- Armed: **{'ON' if armed else 'OFF'}**",
        f"- Mode: {mode or '(none — use start --mode …)'}",
        f"- PID: {state.get('pid') or (os.getpid() if armed else 'n/a')}",
        f"- Cash (Ex2): {cash if cash is not None else 'n/a'}",
        f"- Log: `{LOG_PATH}`",
        f"- State: `{STATE_PATH}`",
        "",
        "## Trend",
        "",
    ]
    lines += format_trend_block(trend)
    lines += [
        "",
        "## Mode 2 direction rule (final)",
        "",
        "- UP → FOLLOW YES · DOWN → FOLLOW NO · MIXED → FADE prior settle",
        "- Trend chooses direction only — it does **not** block Mode 2 entries",
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
    lines += ["", "## Position", ""]
    if pos:
        abandoned = " exit_abandoned=YES" if pos.get("exit_abandoned") else ""
        lines.append(
            f"- OPEN `{pos.get('ticker')}` side=**{str(pos.get('side') or '').upper()}** "
            f"qty={pos.get('contracts')} entry={pos.get('entry_price')} "
            f"tp={pos.get('tp_price')} tactic={pos.get('tactic')} "
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
        "- Mode 1 first_phase: rem∈(50,70], side from trend+cheap ask, any ≥1¢ net exit",
        "- Mode 2 fade/follow: exact open ≤20s; TP entry+20¢ cap 0.99; max 3 TP tries then abandon",
        "- Stake ~$1 IOC; one open max; Ex2 cash; NEVER deposit/withdraw/bank",
        "",
        "## Stats",
        "",
        f"- {json.dumps(state.get('stats') or {})}",
        "",
    ]
    STATUS_PATH.write_text("\n".join(lines), encoding="utf-8")


def print_status_stdout(state: dict, trend: dict) -> None:
    armed = "ON" if state.get("armed") else "OFF"
    print(f"btc_terminal armed={armed} mode={state.get('mode') or 'n/a'} pid={state.get('pid') or 'n/a'}")
    print(f"trend bias={trend.get('bias')} | {trend.get('note')}")
    print(f"advice={trend.get('advice')} — {trend.get('advice_note')}")
    print(f"mode2={trend.get('mode2_direction')} — {trend.get('mode2_note')}")
    pos = state.get("position")
    if pos:
        print(
            f"position {pos.get('ticker')} {str(pos.get('side') or '').upper()} "
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
    trend = detect_trend()
    state["last_trend"] = {
        "bias": trend.get("bias"),
        "note": trend.get("note"),
        "mode2_direction": trend.get("mode2_direction"),
        "ts": utc_now().isoformat(),
    }
    save_state(state)
    write_status(state, trend=trend, offline=not state.get("armed"))
    print_status_stdout(state, trend)
    return 0


def cmd_trend() -> int:
    trend = detect_trend()
    print(f"bias={trend.get('bias')}")
    print(f"note={trend.get('note')}")
    print(f"advice={trend.get('advice')} — {trend.get('advice_note')}")
    print(f"mode2={trend.get('mode2_direction')} — {trend.get('mode2_note')}")
    state = load_state()
    state["last_trend"] = {
        "bias": trend.get("bias"),
        "note": trend.get("note"),
        "mode2_direction": trend.get("mode2_direction"),
        "ts": utc_now().isoformat(),
    }
    save_state(state)
    write_status(state, trend=trend, offline=not state.get("armed"))
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

    LOGGER.info(
        "BTC terminal LIVE start mode=%s series=%s stake~$%.2f. "
        "Mode2: UP→YES FOLLOW, DOWN→NO FOLLOW, MIXED→FADE prior. "
        "Mode1: rem(50,70] any≥1¢. No deposits.",
        mode, SERIES, STAKE,
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
            # another stop cleared armed; exit
            if not disk.get("armed"):
                # if our pid still set, continue; else stop asked
                pass
        if disk.get("armed") is False and str(disk.get("pid")) != str(os.getpid()):
            LOGGER.info("armed=false on disk — exiting loop")
            break
        # re-read stop via state flag written by stop cmd after kill — also check armed
        if not disk.get("armed") and disk.get("pid") is None:
            LOGGER.info("stop cleared armed/pid — exiting")
            break

        cycle += 1
        cash = safe_balance()
        note = ""
        sleep_for = POLL_SEC
        trend = detect_trend()
        state = load_state()
        state["armed"] = True
        state["mode"] = mode
        state["pid"] = os.getpid()
        state["last_trend"] = {
            "bias": trend.get("bias"),
            "note": trend.get("note"),
            "mode2_direction": trend.get("mode2_direction"),
            "ts": utc_now().isoformat(),
        }

        try:
            manage_position(state, mode)
            state = load_state()
            state["armed"] = True
            state["mode"] = mode
            state["pid"] = os.getpid()
            if not state.get("position"):
                if cash is not None:
                    if mode == "first_phase":
                        note = try_enter_first_phase(state, cash, trend)
                    else:
                        note = try_enter_fade(state, cash, trend)
                    state = load_state()
                    state["armed"] = True
                    state["mode"] = mode
                    state["pid"] = os.getpid()
                else:
                    note = "balance unavailable"
                try:
                    opens = list_open_btc()
                    if opens:
                        age = window_age_sec(opens[0].get("close_time"))
                        rem = seconds_remaining(opens[0].get("close_time"))
                        if mode == "fade":
                            if age is not None and age <= ENTRY_SETTLE_GRACE_SEC:
                                sleep_for = OPEN_POLL_SEC
                            elif rem is not None and rem <= NEAR_OPEN_SEC:
                                sleep_for = OPEN_POLL_SEC
                        else:
                            if rem is not None and FP_REM_LO < rem <= FP_REM_HI + 15:
                                sleep_for = FP_POLL_SEC
                except Exception:
                    if mode == "fade":
                        sleep_for = OPEN_POLL_SEC
            else:
                pos = state["position"]
                note = (
                    f"holding {pos['ticker']} {pos['side'].upper()} "
                    f"entry={pos['entry_price']} tp={pos.get('tp_price')} "
                    f"tactic={pos.get('tactic')} abandoned={pos.get('exit_abandoned')}"
                )
                rem = seconds_remaining(pos.get("close_time"))
                if rem is not None and rem <= NEAR_OPEN_SEC:
                    sleep_for = OPEN_POLL_SEC
        except Exception as exc:
            LOGGER.exception("cycle error: %s", exc)
            note = f"error {type(exc).__name__}"

        if cycle <= 3 or cycle % 10 == 0 or sleep_for <= OPEN_POLL_SEC:
            if cycle <= 5 or cycle % (10 if sleep_for > OPEN_POLL_SEC else 20) == 0:
                LOGGER.info(
                    "cycle=%s mode=%s cash=%s open=%s poll=%.2fs bias=%s %s",
                    cycle,
                    mode,
                    None if cash is None else round(cash, 4),
                    1 if state.get("position") else 0,
                    sleep_for,
                    trend.get("bias"),
                    note,
                )
        write_status(state, trend=trend, extra={"cash": cash, "wait_note": note, "mode": mode})
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
