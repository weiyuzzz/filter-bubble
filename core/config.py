"""GitPulse configuration."""
from __future__ import annotations

import os
import pathlib

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    """Tiny .env loader so local keys never have to be committed or exported."""
    path = BASE_DIR / ".env"
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'").strip('"')
        if key and key not in os.environ:
            os.environ[key] = value


_load_dotenv()

WEB_DIR = BASE_DIR / "web"
DATA_DIR = pathlib.Path(os.environ.get("GITPULSE_DATA", str(BASE_DIR / "data")))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = pathlib.Path(os.environ.get("GITPULSE_DB", str(DATA_DIR / "gitpulse.db")))

HOST = os.environ.get("GITPULSE_HOST", "127.0.0.1")
PORT = int(os.environ.get("GITPULSE_PORT", "8791"))
REFRESH_TIME = os.environ.get("GITPULSE_REFRESH_TIME", "08:00")
TIMEZONE = os.environ.get("GITPULSE_TIMEZONE", "Asia/Shanghai")

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()
USER_AGENT = "GitPulse/1.0 (+local daily digest)"
HTTP_TIMEOUT = float(os.environ.get("GITPULSE_HTTP_TIMEOUT", "18"))
MAX_ITEMS = int(os.environ.get("GITPULSE_MAX_ITEMS", "30"))
TRANSLATE = os.environ.get("GITPULSE_TRANSLATE", "1") not in ("0", "false", "False", "")
TRANSLATE_DAILY_CHARS = int(os.environ.get("GITPULSE_TRANSLATE_CHARS", "5000"))

# WeChat brief via Server酱 Turbo (SCT... SendKey).
SERVERCHAN_SENDKEY = os.environ.get("SERVERCHAN_SENDKEY", "").strip()
SERVERCHAN_API_BASE = os.environ.get(
    "SERVERCHAN_API_BASE", "https://sctapi.ftqq.com"
).rstrip("/")
PUSH_ENABLED = bool(SERVERCHAN_SENDKEY)
PUSH_ITEMS = max(1, min(5, int(os.environ.get("GITPULSE_PUSH_ITEMS", "3"))))
PUSH_TIMEOUT = float(os.environ.get("GITPULSE_PUSH_TIMEOUT", "15"))
