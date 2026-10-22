"""GitPulse configuration."""
from __future__ import annotations

import os
import pathlib

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
WEB_DIR = BASE_DIR / "web"
DATA_DIR = pathlib.Path(os.environ.get("GITPULSE_DATA", str(BASE_DIR / "data")))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = pathlib.Path(os.environ.get("GITPULSE_DB", str(DATA_DIR / "gitpulse.db")))

HOST = os.environ.get("GITPULSE_HOST", "127.0.0.1")
PORT = int(os.environ.get("GITPULSE_PORT", "8791"))
REFRESH_TIME = os.environ.get("GITPULSE_REFRESH_TIME", "08:30")
TIMEZONE = os.environ.get("GITPULSE_TIMEZONE", "Asia/Shanghai")

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()
USER_AGENT = "GitPulse/1.0 (+local daily digest)"
HTTP_TIMEOUT = float(os.environ.get("GITPULSE_HTTP_TIMEOUT", "18"))
MAX_ITEMS = int(os.environ.get("GITPULSE_MAX_ITEMS", "30"))
TRANSLATE = os.environ.get("GITPULSE_TRANSLATE", "1") not in ("0", "false", "False", "")
TRANSLATE_DAILY_CHARS = int(os.environ.get("GITPULSE_TRANSLATE_CHARS", "5000"))
