"""BTC 15m dual-mode terminal — Flask web UI for Render.

Default: armed=false (OFF). Start buttons spawn a worker with LIVE=1.
Status/trend work from public Binance + public Kalshi market data.
Trading requires Kalshi keys via env (KALSHI_API_KEY_ID + KALSHI_PRIVATE_KEY).
HARD LOCK: never deposit/withdraw/bank. Never place orders unless Start is clicked.
"""

from __future__ import annotations

import atexit
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from flask import Flask, Response, jsonify, request

# Ensure package root is importable
ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("DATA_DIR", str(ROOT))
os.environ.setdefault("LOG_DIR", str(ROOT))

from omega import config  # noqa: E402
from omega.btc_terminal import (  # noqa: E402
    MODES,
    STATE_PATH,
    cmd_stop,
    default_state,
    load_state,
    save_state,
    write_status,
)
from omega.btc_trend import detect_trend, mode2_side_from_trend  # noqa: E402
from omega.kalshi import get_balance, has_keys, reset_key_cache  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("btc15m.web")

app = Flask(__name__)

_lock = threading.Lock()
_worker: subprocess.Popen | None = None
_worker_mode: str | None = None


def _ensure_dirs() -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not STATE_PATH.is_file():
        save_state(default_state())


def _pid_alive(pid) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, TypeError, ValueError):
        return False


def _sync_worker_state() -> dict:
    """Reconcile in-memory worker handle with on-disk armed/pid state."""
    global _worker, _worker_mode
    state = load_state()
    with _lock:
        alive = _worker is not None and _worker.poll() is None
        if _worker is not None and _worker.poll() is not None:
            log.info("worker exited code=%s", _worker.returncode)
            _worker = None
            _worker_mode = None
            if state.get("armed"):
                state["armed"] = False
                state["pid"] = None
                save_state(state)
        disk_pid = state.get("pid")
        if state.get("armed") and not alive and not _pid_alive(disk_pid):
            state["armed"] = False
            state["pid"] = None
            save_state(state)
        elif not state.get("armed") and alive:
            # stop cleared armed — kill leftover
            try:
                _worker.terminate()
            except Exception:
                pass
            _worker = None
            _worker_mode = None
    return load_state()


def _safe_cash() -> float | None:
    if not has_keys():
        return None
    try:
        reset_key_cache()
        bal = get_balance()
        if isinstance(bal, dict) and bal.get("cash") is not None:
            return float(bal["cash"])
    except Exception as exc:
        log.warning("balance fetch failed: %s", exc)
    return None


def _status_payload() -> dict:
    state = _sync_worker_state()
    try:
        trend = detect_trend()
    except Exception as exc:
        trend = {
            "bias": "MIXED",
            "note": f"trend error: {type(exc).__name__}",
            "mode2_direction": "FADE prior settle",
            "mode2_note": str(exc),
            "advice": "CAUTION",
            "advice_note": "trend unavailable",
        }
    prior = None
    ls = state.get("last_settle") or {}
    if isinstance(ls, dict):
        prior = ls.get("result") or ls.get("side")
    m2_side, m2_label = mode2_side_from_trend(trend.get("bias"), prior)
    cash = _safe_cash()
    armed = bool(state.get("armed"))
    return {
        "armed": armed,
        "mode": state.get("mode"),
        "pid": state.get("pid"),
        "keys_present": has_keys(),
        "cash": cash,
        "position": state.get("position"),
        "stats": state.get("stats") or {},
        "last_settle": state.get("last_settle"),
        "last_entry_attempt": state.get("last_entry_attempt"),
        "trend": {
            "bias": trend.get("bias"),
            "note": trend.get("note"),
            "advice": trend.get("advice"),
            "advice_note": trend.get("advice_note"),
            "mode2_direction": trend.get("mode2_direction"),
            "mode2_note": trend.get("mode2_note"),
            "price": trend.get("price"),
            "ema3": trend.get("ema3"),
            "ema9": trend.get("ema9"),
        },
        "mode2_preview": {
            "side": m2_side,
            "label": m2_label,
            "prior": prior,
        },
        "worker_alive": _worker is not None and _worker.poll() is None,
        "updated_at": state.get("updated_at"),
        "live_default": False,
        "note": (
            "OFF by default. Start requires LIVE=1 on the worker subprocess. "
            "No deposit/withdraw. Keys via env when trading."
        ),
    }


def _start_worker(mode: str) -> tuple[bool, str]:
    global _worker, _worker_mode
    if mode not in MODES:
        return False, f"unknown mode {mode!r}"
    if not has_keys():
        return False, "no Kalshi keys configured (set KALSHI_API_KEY_ID + KALSHI_PRIVATE_KEY)"
    with _lock:
        state = _sync_worker_state()
        if state.get("armed") or (_worker is not None and _worker.poll() is None):
            return False, f"already armed mode={state.get('mode')}"
        env = os.environ.copy()
        env["LIVE"] = "1"
        env["DATA_DIR"] = str(ROOT)
        env["LOG_DIR"] = str(ROOT)
        env["PYTHONUNBUFFERED"] = "1"
        # Do not inherit a global LIVE=0 refusal; worker must see LIVE=1
        cmd = [
            sys.executable,
            "-m",
            "omega.btc_terminal",
            "start",
            "--mode",
            mode,
        ]
        try:
            _worker = subprocess.Popen(
                cmd,
                cwd=str(ROOT),
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            _worker_mode = mode
        except Exception as exc:
            _worker = None
            _worker_mode = None
            return False, f"spawn failed: {exc}"
        # seed armed so UI flips quickly; loop will overwrite pid
        state = load_state()
        state["armed"] = True
        state["mode"] = mode
        state["pid"] = _worker.pid
        save_state(state)
        log.info("spawned worker pid=%s mode=%s LIVE=1", _worker.pid, mode)
        return True, f"started mode={mode} pid={_worker.pid}"


def _stop_worker() -> tuple[bool, str]:
    global _worker, _worker_mode
    with _lock:
        msgs = []
        if _worker is not None:
            pid = _worker.pid
            try:
                os.killpg(os.getpgid(pid), signal.SIGTERM)
            except Exception:
                try:
                    _worker.terminate()
                except Exception:
                    pass
            try:
                _worker.wait(timeout=3)
            except Exception:
                try:
                    os.killpg(os.getpgid(pid), signal.SIGKILL)
                except Exception:
                    pass
            msgs.append(f"killed_worker={pid}")
            _worker = None
            _worker_mode = None
    # Also use terminal stop (clears armed + leftover patterns)
    try:
        cmd_stop()
        msgs.append("cmd_stop=ok")
    except Exception as exc:
        state = load_state()
        state["armed"] = False
        state["pid"] = None
        save_state(state)
        write_status(state, offline=True, extra={"wait_note": f"web stop: {exc}"})
        msgs.append(f"cmd_stop_err={type(exc).__name__}")
    return True, "; ".join(msgs) or "stopped"


HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>BTC 15m Dual-Mode Terminal</title>
<style>
  :root { --bg:#0b1220; --card:#141e2e; --fg:#e8eef8; --muted:#8aa0b8;
          --up:#3ddc97; --down:#ff6b6b; --mix:#f0c14b; --off:#6b7c93;
          --btn:#2b6cb0; --stop:#c53030; --border:#243447; }
  * { box-sizing: border-box; }
  body { margin:0; font-family: ui-sans-serif, system-ui, sans-serif;
         background:var(--bg); color:var(--fg); padding:1.25rem; }
  h1 { font-size:1.35rem; margin:0 0 .25rem; }
  .sub { color:var(--muted); font-size:.9rem; margin-bottom:1rem; }
  .grid { display:grid; gap:1rem; grid-template-columns:repeat(auto-fit,minmax(260px,1fr)); }
  .card { background:var(--card); border:1px solid var(--border); border-radius:12px; padding:1rem; }
  .label { color:var(--muted); font-size:.75rem; text-transform:uppercase; letter-spacing:.04em; }
  .value { font-size:1.6rem; font-weight:700; margin:.25rem 0; }
  .UP { color:var(--up); } .DOWN { color:var(--down); } .MIXED { color:var(--mix); }
  .ON { color:var(--up); } .OFF { color:var(--off); }
  .note { color:var(--muted); font-size:.85rem; word-break:break-word; }
  .btns { display:flex; flex-wrap:wrap; gap:.6rem; margin-top:1rem; }
  button { border:0; border-radius:8px; padding:.7rem 1rem; font-weight:600; cursor:pointer; color:#fff; }
  button:disabled { opacity:.45; cursor:not-allowed; }
  .b1 { background:#2f855a; } .b2 { background:var(--btn); } .bs { background:var(--stop); }
  pre { background:#0a1018; padding:.75rem; border-radius:8px; overflow:auto; font-size:.8rem; }
  .warn { color:var(--mix); font-size:.85rem; margin-top:.75rem; }
  a { color:#63b3ed; }
</style>
</head>
<body>
  <h1>BTC 15m Dual-Mode Terminal</h1>
  <div class="sub">Kalshi KXBTC15M · default <b>OFF</b> · Start injects LIVE=1 into worker only · never deposit/withdraw</div>
  <div class="grid">
    <div class="card">
      <div class="label">Armed</div>
      <div class="value" id="armed">…</div>
      <div class="note" id="mode">mode: —</div>
      <div class="note" id="pid">pid: —</div>
    </div>
    <div class="card">
      <div class="label">Trend bias</div>
      <div class="value" id="bias">…</div>
      <div class="note" id="tnote">—</div>
    </div>
    <div class="card">
      <div class="label">Mode 2 direction preview</div>
      <div class="value" id="m2" style="font-size:1.2rem">…</div>
      <div class="note" id="m2note">—</div>
    </div>
    <div class="card">
      <div class="label">Cash (Ex2)</div>
      <div class="value" id="cash" style="font-size:1.3rem">—</div>
      <div class="note" id="keys">keys: —</div>
    </div>
  </div>
  <div class="btns">
    <button class="b1" id="btn1" onclick="startMode('first_phase')">Start Mode 1 (first_phase)</button>
    <button class="b2" id="btn2" onclick="startMode('fade')">Start Mode 2 (fade)</button>
    <button class="bs" id="btnStop" onclick="stopAll()">Stop</button>
  </div>
  <div class="warn" id="msg"></div>
  <div class="card" style="margin-top:1rem">
    <div class="label">Position / stats</div>
    <pre id="detail">loading…</pre>
  </div>
  <p class="note" style="margin-top:1rem">
    Docs: Mode 1 enters last ~1m any-gain; Mode 2 exact-open follow/fade (UP→YES, DOWN→NO, MIXED→fade prior).
    Free Render sleeps — wake via this URL. Keys: set <code>KALSHI_API_KEY_ID</code> + <code>KALSHI_PRIVATE_KEY</code> in Render env (do not commit).
  </p>
<script>
async function refresh() {
  try {
    const r = await fetch('/api/status');
    const j = await r.json();
    const armed = !!j.armed;
    const bias = (j.trend && j.trend.bias) || 'MIXED';
    document.getElementById('armed').textContent = armed ? 'ON' : 'OFF';
    document.getElementById('armed').className = 'value ' + (armed ? 'ON' : 'OFF');
    document.getElementById('mode').textContent = 'mode: ' + (j.mode || '(none)');
    document.getElementById('pid').textContent = 'pid: ' + (j.pid || 'n/a') + (j.worker_alive ? ' (alive)' : '');
    document.getElementById('bias').textContent = bias;
    document.getElementById('bias').className = 'value ' + bias;
    document.getElementById('tnote').textContent = (j.trend && j.trend.note) || '';
    document.getElementById('m2').textContent = (j.trend && j.trend.mode2_direction) || '—';
    document.getElementById('m2note').textContent = (j.mode2_preview && j.mode2_preview.label) || (j.trend && j.trend.mode2_note) || '';
    const cash = j.cash;
    document.getElementById('cash').textContent = (cash == null) ? (j.keys_present ? 'n/a' : 'no keys') : ('$' + Number(cash).toFixed(2));
    document.getElementById('keys').textContent = 'keys: ' + (j.keys_present ? 'present' : 'not configured');
    document.getElementById('detail').textContent = JSON.stringify({
      position: j.position, stats: j.stats, last_settle: j.last_settle,
      last_entry_attempt: j.last_entry_attempt, note: j.note
    }, null, 2);
    document.getElementById('btn1').disabled = armed;
    document.getElementById('btn2').disabled = armed;
    document.getElementById('btnStop').disabled = !armed && !j.worker_alive;
  } catch (e) {
    document.getElementById('msg').textContent = 'status error: ' + e;
  }
}
async function startMode(mode) {
  document.getElementById('msg').textContent = 'starting ' + mode + '…';
  const r = await fetch('/api/start', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({mode})});
  const j = await r.json();
  document.getElementById('msg').textContent = (j.ok ? 'OK: ' : 'ERR: ') + (j.message || JSON.stringify(j));
  refresh();
}
async function stopAll() {
  document.getElementById('msg').textContent = 'stopping…';
  const r = await fetch('/api/stop', {method:'POST'});
  const j = await r.json();
  document.getElementById('msg').textContent = (j.ok ? 'OK: ' : 'ERR: ') + (j.message || JSON.stringify(j));
  refresh();
}
refresh();
setInterval(refresh, 5000);
</script>
</body>
</html>
"""


@app.get("/")
def index() -> Response:
    return Response(HTML, mimetype="text/html")


@app.get("/api/status")
def api_status():
    return jsonify(_status_payload())


@app.post("/api/start")
def api_start():
    body = request.get_json(silent=True) or {}
    mode = str(body.get("mode") or request.args.get("mode") or "").strip()
    if not mode:
        return jsonify({"ok": False, "message": "mode required (first_phase|fade)"}), 400
    ok, msg = _start_worker(mode)
    code = 200 if ok else 400
    return jsonify({"ok": ok, "message": msg, "status": _status_payload()}), code


@app.post("/api/stop")
def api_stop():
    ok, msg = _stop_worker()
    return jsonify({"ok": ok, "message": msg, "status": _status_payload()})


@app.get("/health")
def health():
    return jsonify({"ok": True, "armed": bool(load_state().get("armed")), "service": "btc-15m-terminal"})


def _shutdown():
    try:
        _stop_worker()
    except Exception:
        pass


atexit.register(_shutdown)

# Boot: ensure OFF state — never auto-arm on web process start
_ensure_dirs()
_boot = load_state()
if _boot.get("armed") and not _pid_alive(_boot.get("pid")):
    _boot["armed"] = False
    _boot["pid"] = None
    save_state(_boot)
    write_status(_boot, offline=True, extra={"wait_note": "web boot — forced OFF (stale armed)"})
log.info(
    "btc-15m-terminal web ready armed=%s keys=%s DATA_DIR=%s",
    bool(load_state().get("armed")),
    has_keys(),
    ROOT,
)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    # Debug server only — production uses gunicorn
    app.run(host="0.0.0.0", port=port, debug=False)
