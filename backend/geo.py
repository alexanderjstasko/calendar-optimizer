"""
geo.py — where a scouted event is: its neighbourhood ("Upper West Side",
"Hell's Kitchen") and a point for the Events tab's map pin.

Venue strings are looked up once with OpenStreetMap's Nominatim geocoder
(free, no key; its policy allows at most one request a second and asks for
an identifying User-Agent) and cached in the `places` table, misses too.
Only public venue strings from the events sites are ever sent; nothing about
the person. Lookups run after a scout run, from the hourly backfill job and
from `python3 geo.py backfill`; the review summary only reads the cache, so
no request ever waits on the network. GEO_LOOKUPS=0 in .env turns lookups
off (the tab then shows the venue name, as before).

    python3 geo.py lookup "Symphony Space, 2537 Broadway, New York"
    python3 geo.py backfill
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import re
import threading
import time
from typing import Any

import requests

import db

logger = logging.getLogger(__name__)

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "calendar-optimizer/1.0 (personal calendar app)"
NYC_VIEWBOX = "-74.27,40.92,-73.68,40.49"  # left, top, right, bottom: biases results to the five boroughs
MIN_INTERVAL_S = 1.1
TIMEOUT_S = 10
RETRY_MISS_DAYS = 14
BACKFILL_LIMIT = 40  # lookups per backfill pass (~1 min at Nominatim's rate)
BOROUGHS = {"manhattan", "brooklyn", "queens", "the bronx", "bronx", "staten island"}
_lock = threading.Lock()
_last_request = 0.0


def enabled() -> bool:
    return (os.environ.get("GEO_LOOKUPS") or "1").strip().lower() not in ("0", "off", "false", "no")


def place_key(location: str | None) -> str:
    """The cache key of a venue string: lower-case, whitespace collapsed."""
    return re.sub(r"\s+", " ", (location or "").strip().lower())


def is_lookupable(location: str | None) -> bool:
    text = (location or "").strip()
    if len(text) < 4 or re.match(r"https?://", text):
        return False
    return not re.fullmatch(r"(online|virtual|zoom|tba|tbd|various locations?)\b.*", text, re.I)


def queries_for(location: str) -> list[str]:
    """
    What to ask, best first. Sources give "Venue, street, city" or a bare
    venue name; the free-form search handles a street address best, so the
    address part goes first, then the whole string, then the venue alone.
    """
    text = re.sub(r"\(\s*NYC\s*\)", "", location)
    text = re.sub(r"\s+", " ", text).strip(" ,")
    parts = [p.strip() for p in text.split(",") if p.strip()]
    out: list[str] = []
    if len(parts) > 1 and re.search(r"\d", parts[1]):
        out.append(", ".join(parts[1:]))
    out.append(text)
    if parts:
        out.append(f"{parts[0]}, New York")
    return list(dict.fromkeys(out))


def neighborhood_of(address: dict[str, Any]) -> tuple[str | None, str | None]:
    """(neighbourhood, borough) from a Nominatim address. In New York OSM puts boroughs in `suburb`."""
    borough = None
    for key in ("borough", "suburb", "city_district"):
        value = address.get(key)
        if value and value.lower() in BOROUGHS:
            borough = "The Bronx" if value.lower() == "bronx" else value
            break
    for key in ("neighbourhood", "quarter", "suburb", "city_district", "hamlet", "village"):
        value = address.get(key)
        if value and value.lower() not in BOROUGHS:
            return value, borough
    return borough, borough


def _search(query: str) -> dict | None:
    global _last_request
    with _lock:  # one request at a time, at most one a second
        wait = MIN_INTERVAL_S - (time.monotonic() - _last_request)
        if wait > 0:
            time.sleep(wait)
        try:
            resp = requests.get(
                NOMINATIM_URL,
                params={"q": query, "format": "jsonv2", "addressdetails": 1, "limit": 1, "viewbox": NYC_VIEWBOX,
                        "bounded": 1, "countrycodes": "us"},
                headers={"User-Agent": USER_AGENT, "Accept-Language": "en"},
                timeout=TIMEOUT_S,
            )
        finally:
            _last_request = time.monotonic()
    resp.raise_for_status()
    hits = resp.json()
    return hits[0] if hits else None


def lookup(location: str, *, now: dt.datetime | None = None, force: bool = False) -> dict | None:
    """
    The cached place for a venue string, looking it up when it is not cached
    (or a cached miss is older than RETRY_MISS_DAYS). -> {lat, lon,
    neighborhood, borough} or None. Network errors are logged, never raised,
    and not cached (the next pass tries again).
    """
    if not is_lookupable(location):
        return None
    key = place_key(location)
    now = now or dt.datetime.now(dt.timezone.utc)
    cached = db.get_places([key]).get(key)
    if cached and not force:
        if cached["lat"] is not None:
            return cached
        try:
            age = now - dt.datetime.fromisoformat(cached["looked_up_at"])
        except (TypeError, ValueError):
            age = dt.timedelta(days=RETRY_MISS_DAYS)
        if age < dt.timedelta(days=RETRY_MISS_DAYS):
            return None
    if not enabled():
        return None
    try:
        for query in queries_for(location):
            hit = _search(query)
            if hit:
                hood, borough = neighborhood_of(hit.get("address") or {})
                db.upsert_place(key, query, float(hit["lat"]), float(hit["lon"]), hood, borough, now.isoformat())
                return db.get_places([key])[key]
    except (requests.RequestException, ValueError, KeyError) as exc:
        logger.warning("geo: lookup of %r failed: %s", location[:80], exc)
        return None
    db.upsert_place(key, location, None, None, None, None, now.isoformat())
    logger.info("geo: no match for %r", location[:80])
    return None


def ensure(locations: list[str], *, limit: int = BACKFILL_LIMIT) -> dict[str, int]:
    """Look up the venue strings not cached yet (at most `limit` network lookups). Never raises."""
    todo = [loc for loc in dict.fromkeys(locations) if is_lookupable(loc)]
    cached = db.get_places([place_key(loc) for loc in todo])
    todo = [loc for loc in todo if place_key(loc) not in cached]
    found = 0
    for loc in todo[:limit]:
        if lookup(loc):
            found += 1
    return {"looked_up": min(len(todo), limit), "found": found, "waiting": max(0, len(todo) - limit)}


def backfill(limit: int = BACKFILL_LIMIT) -> dict[str, int]:
    """Places for every open event proposal (shown, queued or approved and upcoming)."""
    if not enabled():
        return {"looked_up": 0, "found": 0, "waiting": 0}
    rows = db.get_open_proposals(dt.datetime.now(dt.timezone.utc))
    return ensure([r.get("location") or "" for r in rows if r["item_type"] == "event"], limit=limit)


def attach(rows: list[dict]) -> list[dict]:
    """
    Adds `place` ({neighborhood, borough, lat, lon} or None) and `summary`
    (the discovered event's own description) to event proposal rows, from
    the cache only. Other rows pass through untouched.
    """
    events = [r for r in rows if r.get("item_type") == "event"]
    if not events:
        return rows
    places = db.get_places([place_key(r.get("location")) for r in events])
    summaries = db.get_discovered_summaries([r.get("discovered_event_id") for r in events])
    for r in events:
        p = places.get(place_key(r.get("location")))
        r["place"] = (
            {"neighborhood": p["neighborhood"], "borough": p["borough"], "lat": p["lat"], "lon": p["lon"]}
            if p and p["lat"] is not None else None
        )
        r["summary"] = summaries.get(r.get("discovered_event_id"), "")
    return rows


def main() -> int:
    from dotenv import load_dotenv
    from pathlib import Path

    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    one = sub.add_parser("lookup")
    one.add_argument("location")
    sub.add_parser("backfill")
    args = parser.parse_args()
    db.init_db()
    out = lookup(args.location, force=True) if args.cmd == "lookup" else backfill()
    print(json.dumps(out, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
