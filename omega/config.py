"""Runtime config. Never logs or returns private key material."""

from __future__ import annotations

import os
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / ".env"
# Prefer env; fall back to local key dir (box or KALSHI_KEY_DIR).
KEY_DIR = Path(
    os.environ.get("KALSHI_KEY_DIR", str(Path.home() / ".kalshi"))
).expanduser()
DEFAULT_KEY_FILE = KEY_DIR / "private.key"
DEFAULT_KEY_ID_FILE = KEY_DIR / "api_key_id"
BLOCKED_PATH_TOKENS = (
    "deposit",
    "withdraw",
    "transfer",
    "ach",
    "funding",
    "payout",
    "wire",
    "bank",
)
# Fail closed: only these prefixes may be requested.
ALLOWED_PATH_PREFIXES = (
    "/portfolio/balance",
    "/portfolio/positions",
    "/portfolio/orders",
    "/portfolio/events/orders",
    "/markets",
    "/events",
    "/series",
    "/exchange",
)


def _load_dotenv(path: Path = ENV_PATH) -> None:
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


_load_dotenv()


def _truthy(value: str | None) -> bool:
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def api_key_id() -> str:
    env = os.getenv("KALSHI_API_KEY_ID", "").strip()
    if env:
        return env
    return _read_text(DEFAULT_KEY_ID_FILE)


def private_key_pem() -> str:
    env = os.getenv("KALSHI_PRIVATE_KEY", "").replace("\\n", "\n").strip()
    if env:
        return env
    file_path = os.getenv("KALSHI_PRIVATE_KEY_FILE", "").strip()
    candidates = []
    if file_path:
        candidates.append(Path(file_path).expanduser())
    candidates.append(DEFAULT_KEY_FILE)
    for path in candidates:
        pem = _read_text(path)
        if pem:
            return pem
    return ""


def keys_present() -> bool:
    return bool(api_key_id() and private_key_pem())


def keys_on_disk() -> bool:
    return DEFAULT_KEY_ID_FILE.is_file() and DEFAULT_KEY_FILE.is_file() and keys_present()


def live_requested() -> bool:
    raw = os.getenv("LIVE")
    if raw is None or raw.strip() == "":
        return keys_present()
    return _truthy(raw)


def live_ready() -> bool:
    return live_requested() and keys_present()


def trading_mode() -> str:
    if live_ready():
        return "LIVE"
    if live_requested() or not keys_present():
        return "WAITING_KEYS"
    return "PAPER"


def stake_for_cash(cash: float) -> float:
    """$1 until $15, then $1.50; +50% stake each +50% cash. Cap ~10% after $15."""
    try:
        cash = float(cash)
    except (TypeError, ValueError):
        return 0.0
    if cash < 1.0:
        return 0.0
    if cash < 15.0:
        return 1.00
    last_cash = 15.0
    stake = 1.50
    while last_cash * 1.5 <= cash + 1e-9:
        last_cash *= 1.5
        stake *= 1.5
    cap = cash * 0.10
    return round(min(stake, cap, cash), 2)


def max_per_crypto(cash: float | None = None) -> float:
    if cash is not None:
        return stake_for_cash(cash)
    try:
        value = float(os.getenv("MAX_PER_CRYPTO", "1.00"))
    except (TypeError, ValueError):
        value = 1.00
    return value if value > 0 else 1.00


def trail_arm_for_cost(cost: float) -> float:
    try:
        cost = float(cost)
    except (TypeError, ValueError):
        return 1.05
    return round(cost * 1.05, 4)


def db_path() -> Path:
    override = os.getenv("OMEGA_DB", "").strip()
    if override:
        return Path(override)
    path = ROOT / "data" / "omega.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def poll_seconds() -> float:
    try:
        value = float(os.getenv("POLL_SECONDS", "3"))
    except (TypeError, ValueError):
        value = 3.0
    return value if value > 0 else 3.0


def web_port() -> int:
    try:
        return int(os.getenv("PORT", "8765"))
    except (TypeError, ValueError):
        return 8765


def paper_start_cash() -> float:
    try:
        return float(os.getenv("PAPER_CASH", "14.00"))
    except (TypeError, ValueError):
        return 14.00
