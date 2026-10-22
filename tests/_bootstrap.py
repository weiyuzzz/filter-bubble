"""Shared test bootstrap: isolated database inside the workspace, project root on sys.path.

Import this first in every test module so ``core.*`` resolves and no test ever writes
to the user's real data/filter-bubble.db.
"""
from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

if "GITPULSE_DB" not in os.environ:
    tmp = ROOT / ".testtmp" / uuid.uuid4().hex[:8]
    tmp.mkdir(parents=True, exist_ok=True)
    os.environ["GITPULSE_DATA"] = str(tmp)
    os.environ["GITPULSE_DB"] = str(tmp / "test.db")

TEST_DB = Path(os.environ["GITPULSE_DB"])
