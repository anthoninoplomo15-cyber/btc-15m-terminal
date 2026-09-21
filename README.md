# BTC 15m Dual-Mode Terminal (Kalshi KXBTC15M)

Web UI + CLI for Ramon’s BTC 15-minute dual-mode Kalshi terminal.

**Default is OFF (`armed=false`).** The Render/web process never auto-starts live trading.
Clicking **Start** in the UI spawns a worker with `LIVE=1`. Without Kalshi keys, Start returns “no keys” — status and trend still work from public Binance + public Kalshi market data.

## Hard locks

- Never deposit / withdraw / bank (Kalshi path allowlist in `omega/kalshi.py`).
- Stake ~$1 IOC, one open position max, Exchange 2 cash.
- Do **not** commit secrets (`.env`, `*.pem`, `private.key`, `/home/box/.kalshi/`).

## Modes

| Mode | CLI / UI | Behavior |
|------|----------|----------|
| **Mode 1** `first_phase` | Start Mode 1 | Enter last ~1 min (`rem ∈ (50,70]`). Side from trend (UP→YES, DOWN→NO, MIXED→cheaper ask &lt; 0.85). Exit any ~≥1¢ net after fees, else hold to settle. |
| **Mode 2** `fade` | Start Mode 2 | Exact open (age ≤20s). **UP→FOLLOW YES**, **DOWN→FOLLOW NO**, **MIXED→FADE prior settle**. TP = entry+0.20 capped at 0.99; max 3 TP sells then abandon / hold settle. |

Trend chooses Mode 2 **direction only** — it does not block entries.

## Local CLI

```bash
cd btc-15m-terminal
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

python -m omega.btc_terminal status   # default OFF + trend
python -m omega.btc_terminal trend
python -m omega.btc_terminal stop

# LIVE trading (keys required)
LIVE=1 python -m omega.btc_terminal start --mode first_phase
LIVE=1 python -m omega.btc_terminal start --mode fade
```

## Web UI (Flask)

```bash
export PORT=10000
gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --threads 2 --timeout 120
# open http://127.0.0.1:10000
```

Buttons: **Start Mode 1**, **Start Mode 2**, **Stop**. Status JSON: `GET /api/status`. Health: `GET /health`.

## Render deploy

1. New **Web Service** → connect GitHub repo `anthoninoplomo15-cyber/btc-15m-terminal`.
2. Runtime: Python. Build: `pip install -r requirements.txt`. Start: Procfile / `gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --threads 2 --timeout 120`.
3. Free tier OK. Do **not** set `LIVE=1` as a service env var.
4. Optional secrets later: `KALSHI_API_KEY_ID`, `KALSHI_PRIVATE_KEY` (PEM with `\n` newlines). Until then, Start shows “no keys”; trend still works.
5. Free instances sleep — wake by opening the `*.onrender.com` URL.

Blueprint: see `render.yaml` (creates a dedicated service named `btc-15m-terminal` — does not overwrite `spy-btc-telegram-alerts` or `proyecto-2-bot`).

## Env vars

| Var | Purpose |
|-----|---------|
| `KALSHI_API_KEY_ID` | API key id |
| `KALSHI_PRIVATE_KEY` | PEM private key (`\n` escaped) |
| `KALSHI_PRIVATE_KEY_FILE` / `KALSHI_KEY_DIR` | File-based keys |
| `DATA_DIR` / `LOG_DIR` | State + log directory (defaults to package root) |
| `PORT` | Web bind port (Render sets this) |
| `LIVE` | Only on the **worker** subprocess when Start is clicked |

## Layout

```
app.py                 Flask UI (default OFF)
omega/btc_terminal.py  CLI + live loop
omega/btc_trend.py     Trend + Mode 2 direction
omega/kalshi.py        Signed API (allowlisted)
omega/fetch.py         Public markets / Binance
omega/signal.py        Fees / sizing helpers
omega/config.py        Env + key loading
docs/btc-terminal.md   Full mode rules
```
