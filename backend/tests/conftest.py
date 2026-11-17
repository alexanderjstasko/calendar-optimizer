"""Make backend/ importable when pytest is run from anywhere in the repo."""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import os

# geo.py would ask OpenStreetMap's geocoder; tests never reach the network.
os.environ["GEO_LOOKUPS"] = "0"

# notion_adapter reads these at import time; a fresh clone has no .env.
# setdefault keeps a real .env's values (python-dotenv never overrides them).
os.environ.setdefault("NOTION_TOKEN", "test-token")
os.environ.setdefault("NOTION_GOALS_DB_ID", "test-tree-db")
