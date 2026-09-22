from datetime import datetime, timezone

from omega import btc_terminal


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
