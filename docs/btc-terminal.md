# BTC 15m Dual-Mode Terminal (`python -m omega.btc_terminal`)

Kalshi **KXBTC15M** only. Stake ~$1 IOC, one open position max, Exchange 2 cash.
**HARD LOCK:** never deposit / withdraw / bank.

Default is **OFF**. `start` is the only path that runs the trading loop, and it
requires `LIVE=1`.

## Commands

```bash
cd btc-15m-terminal  # or omega10-live on the box

# OFF status + trend (also the no-arg default)
python -m omega.btc_terminal status
python -m omega.btc_terminal          # same

# trend only
python -m omega.btc_terminal trend

# Mode 1 — last ~1 minute any-gain
LIVE=1 python -m omega.btc_terminal start --mode first_phase

# Mode 2 — exact-open follow/fade
LIVE=1 python -m omega.btc_terminal start --mode fade

# stop (armed=false; kills btc_terminal / leftover fade_btc / omega.worker)
python -m omega.btc_terminal stop
```

Without `LIVE=1`, `start` **refuses** and exits (no paper loop).

## Trend detector

Uses Binance BTCUSDT 1m closes (EMA3 vs EMA9, last closes, optional momentum).

| Bias | Meaning |
|------|---------|
| **UP** | Short EMA above slow / rising |
| **DOWN** | Short EMA below slow / falling |
| **MIXED** | Flat / conflicting / insufficient data |

Printed on `status` / `trend` and written into `btc-terminal-status.md (or STATUS_PATH)`
so you see bias **before** activating.

Advice vs a prospective side (when known): **ALIGNED** / **CONFLICT** / **CAUTION**.

## Mode 1 — `first_phase`

- Series: KXBTC15M only
- **Entry window:** seconds remaining in current open interval ∈ **(50, 70]**
  (≈ last 1 minute)
- **Side:**
  - bias **UP** → buy **YES** (if ask in [0.05, 0.96])
  - bias **DOWN** → buy **NO** (same ask band)
  - bias **MIXED** → buy the **cheaper** ask if that ask **&lt; 0.85**
  - Skip if chosen ask &gt; 0.96 or &lt; 0.05
- **Exit:** any estimated **net PnL ≥ ~1¢ after fees** → sell IOC immediately
  (same spirit as late1m any-gain). Not a fixed target.
- If no TP before settle: **hold to settle**
- No Mode-2-style trend gate; trend only picks side

## Mode 2 — `fade` (follow / fade)

Exact open of new interval:

- Age ≤ **20s** after open
- Settle-lag grace ≤ **45s** only when bias is MIXED and prior result not ready yet

**Direction (FINAL — trend does NOT block entries):**

| Trend bias | Action | Log label example |
|------------|--------|-------------------|
| **UP** | Follow → buy **YES** | `Mode2 FOLLOW UP→YES` |
| **DOWN** | Follow → buy **NO** | `Mode2 FOLLOW DOWN→NO` |
| **MIXED / SIDEWAYS** | Fade prior settle: YES→**NO**, NO→**YES** | `Mode2 FADE prior=NO→YES` |

- **TP:** entry price **+ 0.20** (20 cents), capped at **0.99**
  (example: entry 0.917 → TP 0.99)
- Fast poll near open (**0.25s**)
- **Exit retry:** max **3** TP sell attempts. If all fail (KalshiError / no fill):
  set `exit_abandoned`, clear the blocking retry loop, **hold to settle**.
  After settle, clear position so the **next open entry is never blocked**.
- One open max; never spin forever on TP

## Files

| Path | Role |
|------|------|
| `omega/btc_terminal.py` | CLI + live loop |
| `omega/btc_trend.py` | Trend + Mode 2 direction helper |
| `data/btc_terminal_state.json` | armed/mode/position/stats |
| `btc_terminal.log` | runtime log |
| `btc-terminal-status.md (or STATUS_PATH)` | human status (shows OFF when stopped) |
| `docs/btc-terminal.md` | this doc |

## Safety

- Reuses `omega.kalshi` allowlist (no deposit/withdraw/bank paths)
- Keys: env `KALSHI_*` or `KALSHI_KEY_DIR` (never commit)
- Do not leave `start` running unless you intentionally armed it
