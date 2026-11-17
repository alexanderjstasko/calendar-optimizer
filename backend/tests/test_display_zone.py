"""TIMEZONE in .env: default New York, any IANA zone, a typo falls back."""
from __future__ import annotations

import importlib

import display_zone


def _reload(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("TIMEZONE", raising=False)
    else:
        monkeypatch.setenv("TIMEZONE", value)
    return importlib.reload(display_zone)


def test_timezone_setting(monkeypatch):
    try:
        assert _reload(monkeypatch, "").NAME == "America/New_York"
        assert _reload(monkeypatch, "Europe/London").NAME == "Europe/London"
        assert _reload(monkeypatch, "Mars/Base").NAME == "America/New_York"
    finally:
        _reload(monkeypatch, None)
