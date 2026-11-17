"""
The one time zone the whole app shows and plans in: TIMEZONE in .env (an
IANA name such as Europe/London), default America/New_York. Read once at
import; restart the backend (and rebuild the frontend, which bakes the same
value in at build time) after changing it. An unknown name is logged and the
default is used, so a typo never stops the server.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")
logger = logging.getLogger(__name__)

DEFAULT = "America/New_York"


def _configured() -> str:
    name = (os.environ.get("TIMEZONE") or "").strip() or DEFAULT
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        logger.error("TIMEZONE=%r is not a known time zone; using %s", name, DEFAULT)
        return DEFAULT
    return name


NAME = _configured()
ZONE = ZoneInfo(NAME)
