from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from omega import btc_terminal


ET = ZoneInfo("America/New_York")


def _market(ticker, close_time):
    return {
        "ticker": ticker,
        "close_time": close_time,
        "status": "open",
        "yes_bid": 50,
        "yes_ask": 51,
    }


def test_list_open_ignores_stale_expired_and_selects_current(monkeypatch):
    now = datetime(2026, 9, 22, 1, 15, 22, tzinfo=timezone.utc)
    old = _market("KXBTC15M-OLD", "2026-09-22T01:15:00Z")
    current = _market("KXBTC15M-CURRENT", "2026-09-22T01:30:00Z")
    future = _market("KXBTC15M-FUTURE", "2026-09-22T01:45:00Z")
    monkeypatch.setattr(btc_terminal, "utc_now", lambda: now)
    monkeypatch.setattr(
        btc_terminal,
        "_kalshi_get",
        lambda *args, **kwargs: {"markets": [old, current, future]},
    )

    opens = btc_terminal.list_open_btc()

    assert [market["ticker"] for market in opens] == ["KXBTC15M-CURRENT"]


def test_list_open_switches_at_exact_boundary(monkeypatch):
    now = datetime(2026, 9, 22, 1, 15, 0, tzinfo=timezone.utc)
    old = _market("KXBTC15M-OLD", "2026-09-22T01:15:00Z")
    current = _market("KXBTC15M-CURRENT", "2026-09-22T01:30:00Z")
    monkeypatch.setattr(btc_terminal, "utc_now", lambda: now)
    monkeypatch.setattr(
        btc_terminal,
        "_kalshi_get",
        lambda *args, **kwargs: {"markets": [old, current]},
    )

    opens = btc_terminal.list_open_btc()

    assert [market["ticker"] for market in opens] == ["KXBTC15M-CURRENT"]


def test_kxbtc15m_ticker_for_close():
    close = datetime(2026, 9, 21, 21, 45, tzinfo=ET)
    assert btc_terminal.kxbtc15m_ticker_for_close(close) == "KXBTC15M-26SEP212145-45"


def test_near_interval_boundary_clock():
    # 21:29:45 → 15s before :30 → near
    assert btc_terminal.near_interval_boundary(
        datetime(2026, 9, 21, 21, 29, 45, tzinfo=ET)
    )
    # 21:30:20 → 20s after :30 → near
    assert btc_terminal.near_interval_boundary(
        datetime(2026, 9, 21, 21, 30, 20, tzinfo=ET)
    )
    # 21:37:00 → mid-interval → not near
    assert not btc_terminal.near_interval_boundary(
        datetime(2026, 9, 21, 21, 37, 0, tzinfo=ET)
    )


def test_mode2_entry_allowed_confirm_window(monkeypatch):
    btc_terminal._last_transition_obs_mono = None

    # exact-open / early age → too_early (do NOT enter ≤20s anymore)
    ok, mx, path = btc_terminal.mode2_entry_allowed(
        15.0, need_prior=False, prior_ok=True
    )
    assert not ok and path == "too_early" and mx == 60

    ok, mx, path = btc_terminal.mode2_entry_allowed(
        46.0, need_prior=False, prior_ok=True
    )
    assert not ok and path == "too_early" and mx == 60

    # confirm window [60, 120]
    ok, mx, path = btc_terminal.mode2_entry_allowed(
        60.0, need_prior=False, prior_ok=True
    )
    assert ok and path == "confirm_window" and mx == 120

    ok, mx, path = btc_terminal.mode2_entry_allowed(
        90.0, need_prior=False, prior_ok=True
    )
    assert ok and path == "confirm_window" and mx == 120

    # listing-lag catch-up flag still ok inside window
    btc_terminal.mark_transition_obs()
    ok, mx, path = btc_terminal.mode2_entry_allowed(
        110.0, need_prior=False, prior_ok=True
    )
    assert ok and path == "confirm_catchup" and mx == 120

    # age > 120 → blocked even with catch-up
    ok, mx, path = btc_terminal.mode2_entry_allowed(
        121.0, need_prior=False, prior_ok=True
    )
    assert not ok and path == "blocked" and mx == 120


def test_mode2_ema_gap_score_and_max_open():
    assert btc_terminal.MAX_OPEN == 1
    assert btc_terminal.ENTRY_CONFIRM_MIN_AGE_SEC == 60
    assert btc_terminal.ENTRY_CONFIRM_MAX_AGE_SEC == 120
    assert btc_terminal.TRAIL_ARM_ADD == 0.10
    assert btc_terminal.TRAIL_DRAWDOWN == 0.08
    assert btc_terminal.TP_CAP == 0.99
    weak = {"ema3": 100.0, "ema9": 99.9}
    strong = {"ema3": 110.0, "ema9": 100.0}
    assert btc_terminal.mode2_ema_gap_score(strong) > btc_terminal.mode2_ema_gap_score(weak)
    assert btc_terminal.mode2_ema_gap_score({}) == 0.0


def test_transition_catchup_expires(monkeypatch):
    btc_terminal._last_transition_obs_mono = None
    assert not btc_terminal.transition_catchup_active()
    btc_terminal.mark_transition_obs()
    assert btc_terminal.transition_catchup_active(lookback=90)
    # Simulate observation 100s ago
    btc_terminal._last_transition_obs_mono -= 100
    assert not btc_terminal.transition_catchup_active(lookback=90)


def test_resolve_fade_market_empty_marks_transition(monkeypatch):
    btc_terminal._last_transition_obs_mono = None
    monkeypatch.setattr(btc_terminal, "list_open_series", lambda series=None: [])
    monkeypatch.setattr(btc_terminal, "list_open_btc", lambda: [])
    monkeypatch.setattr(btc_terminal, "fetch_market_by_clock", lambda series=None: None)
    market, src = btc_terminal.resolve_fade_market()
    assert market is None and src == "empty"
    assert btc_terminal.transition_catchup_active()


def test_resolve_fade_market_clock_fallback(monkeypatch):
    btc_terminal._last_transition_obs_mono = None
    clock = _market("KXBTC15M-26SEP212145-45", "2026-09-22T01:45:00Z")
    monkeypatch.setattr(btc_terminal, "list_open_series", lambda series=None: [])
    monkeypatch.setattr(btc_terminal, "list_open_btc", lambda: [])
    monkeypatch.setattr(btc_terminal, "fetch_market_by_clock", lambda series=None: clock)
    market, src = btc_terminal.resolve_fade_market()
    assert market["ticker"] == "KXBTC15M-26SEP212145-45"
    assert src == "clock_after_empty"
    assert btc_terminal.transition_catchup_active()


def test_mode2_combined_score_prefers_clear_cheap():
    weak = {"ema3": 100.0, "ema9": 99.95, "vwap_spot": 100.0, "vwap": 99.99}
    strong = {"ema3": 110.0, "ema9": 100.0, "vwap_spot": 110.0, "vwap": 100.0}
    s_weak, _ = btc_terminal.mode2_combined_score(weak, 0.65)
    s_strong, parts = btc_terminal.mode2_combined_score(strong, 0.40)
    assert s_strong > s_weak
    assert parts["ask_edge"] == round(btc_terminal.MODE2_ASK_MAX - 0.40, 4)
    # Lower ask beats equal trend
    a, _ = btc_terminal.mode2_combined_score(strong, 0.60)
    b, _ = btc_terminal.mode2_combined_score(strong, 0.30)
    assert b > a


def test_refresh_mode2_series_crypto_only(monkeypatch):
    monkeypatch.setattr(
        btc_terminal,
        "mode2_crypto_series",
        lambda force=False: ["KXBTC15M", "KXETH15M", "KXXRP15M", "KXDOGE15M"],
    )
    got = btc_terminal.refresh_mode2_series(force=True)
    assert got == ("KXBTC15M", "KXETH15M", "KXXRP15M", "KXDOGE15M")
    assert btc_terminal.MAX_OPEN == 1
    assert "KXGOLD15M" not in got


def test_mode2_slow_filter_and_midcut_reason():
    from omega.btc_trend import mode2_slow_filter_ok, mode2_bias_adverse_to_side, MODE2_MIN_EMA_GAP

    assert btc_terminal.STAKE == 0.50
    assert btc_terminal.MIDCUT_ADVERSE_BID == 0.15
    assert MODE2_MIN_EMA_GAP > 0

    # Strong UP + EMA21>EMA50 → OK
    up = {"bias": "UP", "ema3": 110.0, "ema9": 100.0, "ema21": 105.0, "ema50": 100.0}
    ok, msg = mode2_slow_filter_ok(up, "UP")
    assert ok, msg

    # Fast UP but slow DOWN → reject
    conflict = {"bias": "UP", "ema3": 110.0, "ema9": 100.0, "ema21": 99.0, "ema50": 100.0}
    ok, msg = mode2_slow_filter_ok(conflict, "UP")
    assert not ok and "EMA21/50" in msg

    # Gap too small → reject
    tiny = {"bias": "UP", "ema3": 100.03, "ema9": 100.0, "ema21": 101.0, "ema50": 100.0}
    ok, msg = mode2_slow_filter_ok(tiny, "UP")
    assert not ok and "ema_gap" in msg

    assert mode2_bias_adverse_to_side("MIXED", "yes")
    assert mode2_bias_adverse_to_side("DOWN", "yes")
    assert not mode2_bias_adverse_to_side("UP", "yes")
    assert mode2_bias_adverse_to_side("UP", "no")
    assert not mode2_bias_adverse_to_side("DOWN", "no")

    # Weak progress alone at 7.5m with aligned bias → NO midcut
    r = btc_terminal.mode2_midcut_reason(
        trail_armed=False, age=450, entry=0.50, bid=0.52, side="yes", bias="UP"
    )
    assert r is None

    # Bias flipped → midcut
    r = btc_terminal.mode2_midcut_reason(
        trail_armed=False, age=450, entry=0.50, bid=0.52, side="yes", bias="DOWN"
    )
    assert r and "bias_against" in r

    # Deep adverse even if bias still UP → midcut
    r = btc_terminal.mode2_midcut_reason(
        trail_armed=False, age=450, entry=0.50, bid=0.30, side="yes", bias="UP"
    )
    assert r and "deep_adverse" in r

    # Trail armed → never midcut via this helper
    r = btc_terminal.mode2_midcut_reason(
        trail_armed=True, age=500, entry=0.50, bid=0.20, side="yes", bias="MIXED"
    )
    assert r is None
