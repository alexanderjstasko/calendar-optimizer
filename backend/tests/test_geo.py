"""geo.py: neighbourhoods and map pins for scouted events, cached; no network in tests."""

from __future__ import annotations

import datetime as dt

import pytest

import db
import geo
import slot


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "events.db")
    db.init_db()
    monkeypatch.setenv("GEO_LOOKUPS", "1")
    monkeypatch.setattr(geo, "MIN_INTERVAL_S", 0)


def _fake_search(monkeypatch, answers):
    asked = []

    def search(query):
        asked.append(query)
        return answers.get(query)

    monkeypatch.setattr(geo, "_search", search)
    return asked


def test_neighborhood_prefers_the_neighbourhood_over_the_borough():
    assert geo.neighborhood_of({"neighbourhood": "Hell's Kitchen", "suburb": "Manhattan"}) == ("Hell's Kitchen", "Manhattan")
    assert geo.neighborhood_of({"quarter": "Upper West Side", "suburb": "Manhattan"}) == ("Upper West Side", "Manhattan")
    assert geo.neighborhood_of({"suburb": "Brooklyn"}) == ("Brooklyn", "Brooklyn")
    assert geo.neighborhood_of({"suburb": "Williamsburg", "borough": "Brooklyn"}) == ("Williamsburg", "Brooklyn")


def test_queries_try_the_street_address_first():
    assert geo.queries_for("Symphony Space, 2537 Broadway, New York, NY") == [
        "2537 Broadway, New York, NY", "Symphony Space, 2537 Broadway, New York, NY", "Symphony Space, New York"]
    assert geo.queries_for("New York City ( NYC ) New York") == ["New York City New York", "New York City New York, New York"]


def test_lookup_caches_hits_and_misses(monkeypatch):
    asked = _fake_search(monkeypatch, {
        "2537 Broadway, New York": {"lat": "40.7997", "lon": "-73.9696",
                                    "address": {"neighbourhood": "Upper West Side", "suburb": "Manhattan"}},
    })
    place = geo.lookup("Symphony Space, 2537 Broadway, New York")
    assert place["neighborhood"] == "Upper West Side" and place["lat"] == pytest.approx(40.7997)
    assert geo.lookup("symphony space,  2537 Broadway, New York") == place  # same key, no new request
    assert len(asked) == 1
    assert geo.lookup("Nowhere Hall") is None
    n = len(asked)
    assert geo.lookup("Nowhere Hall") is None and len(asked) == n  # the miss is cached
    later = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=geo.RETRY_MISS_DAYS + 1)
    geo.lookup("Nowhere Hall", now=later)
    assert len(asked) > n  # and retried once it is old


def test_lookup_skips_virtual_and_respects_the_switch(monkeypatch):
    asked = _fake_search(monkeypatch, {})
    assert geo.lookup("https://zoom.us/j/1") is None and geo.lookup("Online") is None and geo.lookup("") is None
    monkeypatch.setenv("GEO_LOOKUPS", "0")
    assert geo.lookup("Carnegie Hall") is None
    assert asked == []


def test_network_errors_are_not_cached(monkeypatch):
    import requests

    def boom(query):
        raise requests.ConnectionError("down")

    monkeypatch.setattr(geo, "_search", boom)
    assert geo.lookup("Carnegie Hall, 881 7th Ave") is None
    assert db.get_places([geo.place_key("Carnegie Hall, 881 7th Ave")]) == {}


def test_attach_adds_place_and_summary_from_the_cache(monkeypatch):
    db.upsert_discovered_events([{
        "id": "d1", "site": "x", "title": "Talk", "start": "2026-12-01T19:00:00-05:00", "end": None, "all_day": 0,
        "timezone": "America/New_York", "location": "Carnegie Hall", "url": "https://x/e/1", "cost": "Free",
        "category": "", "summary": "A talk about maps.", "website_id": None}], None, "2026-11-01T00:00:00+00:00")
    db.upsert_place(geo.place_key("Carnegie Hall"), "Carnegie Hall", 40.765, -73.98, "Midtown", "Manhattan", "2026-11-01")
    rows = [{"item_type": "event", "location": "Carnegie Hall", "discovered_event_id": "d1"},
            {"item_type": "event", "location": "Unknown", "discovered_event_id": "zz"},
            {"item_type": "goal", "location": "Gym"}]
    out = geo.attach(rows)
    assert out[0]["place"]["neighborhood"] == "Midtown" and out[0]["summary"] == "A talk about maps."
    assert out[1]["place"] is None and out[1]["summary"] == ""
    assert "place" not in out[2]


def test_slot_where_tells_home_from_out():
    assert slot.where("", "1 Main St") == "home"
    assert slot.where("Home", "") == "home"
    assert slot.where("https://zoom.us/j/1") == "home"
    assert slot.where("1 main st", "1 Main St") == "home"
    assert slot.where("Brooklyn Boulders") == "out"
