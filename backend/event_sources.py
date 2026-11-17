"""
event_sources.py — fetch public events from the sites in the Notion Website
List and normalize them into one shape for the scout (scout.py).

Each site gets the cheapest reliable path, decided by domain:

  nypl.org          Eventbrite's search listings for the library, NYPL venues only
                    (the Refinery API and www.nypl.org are gone/walled, see fetch_nypl)
  donyc.com         the page's own JSON twin (append .json); the curated
                    best-of page yields event_groups[].events[]
  eventbrite.com    the __SERVER_DATA__ blob embedded in listing pages (has
                    start/end times, venue, price); JSON-LD as fallback
  anything else     JSON-LD Event markup if the page has it (Club Free Time),
                    else the page text handed to an LLM extractor supplied by
                    the caller; pages with almost no text are 'unreadable'

Normalized event: id, site, title, start, end, all_day, timezone, location,
url, cost, category, summary, raw. Times are America/New_York ISO strings
(date-only for all-day / time-unknown). `id` is stable across fetches
(site + title + date) so discovered_events can be upserted.

No new dependencies: requests + the standard library.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import html
import json
import logging
import re
from html.parser import HTMLParser
from typing import Any, Callable
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

import requests

logger = logging.getLogger(__name__)

DISPLAY_TZ = "America/New_York"
NY = ZoneInfo(DISPLAY_TZ)
UA = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}
TIMEOUT = 30
MIN_READABLE_CHARS = 1500      # below this a page is a JS shell / bot challenge
MAX_TEXT_FOR_LLM = 45000     # room for the [link] targets kept beside event titles
# Eventbrite search listings that carry NYPL's ticketed programs (see fetch_nypl).
NYPL_EVENTBRITE_SEARCHES = (
    "https://www.eventbrite.com/d/ny--new-york/new-york-public-library/",
    "https://www.eventbrite.com/d/ny--new-york/nypl/",
)
EVENTBRITE_PAGES = 3

LLMExtractor = Callable[[str, str], list[dict]]  # (url, page_text) -> normalized events


# --- helpers ------------------------------------------------------------------


def site_of(url: str) -> str:
    return urlparse(url).netloc.lower().removeprefix("www.")


def event_id(site: str, title: str, start: str) -> str:
    key = f"{site}|{re.sub(r'\s+', ' ', title.strip().lower())}|{(start or '')[:10]}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


def to_local_iso(value: str | None) -> str | None:
    """ISO datetime -> New York ISO with offset; date-only stays a date; None when unparseable."""
    if not value:
        return None
    value = value.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return value
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=NY)
    return parsed.astimezone(NY).isoformat(timespec="seconds")


_INVISIBLE = re.compile(r"[­​‌‍﻿]")  # soft hyphen ('&shy;'), zero-width characters


def clean_text(value: str | None) -> str:
    """Decode HTML entities ("Sci&shy;ence", "&amp;"), drop invisible characters, collapse whitespace."""
    return re.sub(r"\s+", " ", _INVISIBLE.sub("", html.unescape(value or ""))).strip()


def normalize(site: str, title: str, start: str | None, end: str | None = None, *, location: str = "",
              url: str = "", cost: str = "", category: str = "", summary: str = "", raw: Any = None) -> dict | None:
    if not title or not start:
        return None
    all_day = "T" not in start
    return {
        # The id still hashes the title as the source gave it, so events already
        # stored keep their id (and their decisions) now that titles are decoded.
        "id": event_id(site, title, start),
        "site": site,
        "title": clean_text(title)[:200],
        "start": start,
        "end": end if end and (("T" in end) == ("T" in start)) else None,
        "all_day": all_day,
        "timezone": DISPLAY_TZ,
        "location": clean_text(location)[:200],
        "url": (url or "")[:500],
        "cost": (cost or "")[:60],
        "category": (category or "")[:60],
        "summary": re.sub(r"\s+", " ", strip_html(summary or "")).strip()[:300],
        "raw": raw,
    }


class _TextParser(HTMLParser):
    """
    Visible text plus JSON-LD blobs. With a base_url, a link's target is kept
    after its text as " [https://...]", so the model extracting events from
    a text-only page can give each event its own page instead of the site's.
    """

    def __init__(self, base_url: str | None = None) -> None:
        super().__init__()
        self.parts: list[str] = []
        self.skip = 0
        self.ld_blobs: list[str] = []
        self._in_ld = False
        self.base_url = base_url
        self._href: str | None = None
        self._href_at = 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "a" and self.base_url and not self.skip:
            href = (a.get("href") or "").strip()
            if href and not href.startswith(("#", "mailto:", "tel:", "javascript:")):
                self._href = urljoin(self.base_url, href)
                self._href_at = len(self.parts)
        if tag in ("script", "style", "noscript", "svg", "template"):
            self.skip += 1
            if tag == "script" and a.get("type") == "application/ld+json":
                self._in_ld = True
        if tag in ("p", "div", "li", "h1", "h2", "h3", "h4", "br", "tr", "section", "article"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag == "a" and self._href:
            label = "".join(self.parts[self._href_at:]).strip()
            # Only links with real text (an event title, "Details"), and never the page itself.
            if len(label) >= 5 and self._href.split("#")[0].rstrip("/") != self.base_url.split("#")[0].rstrip("/"):
                self.parts.append(f" [{self._href}]")
            self._href = None
        if tag in ("script", "style", "noscript", "svg", "template"):
            self.skip = max(0, self.skip - 1)
            self._in_ld = False

    def handle_data(self, data):
        if self._in_ld:
            self.ld_blobs.append(data)
        elif not self.skip:
            self.parts.append(data)


def parse_html(page: str, base_url: str | None = None) -> tuple[str, list[str]]:
    """(visible text, raw JSON-LD blobs) from an HTML document; base_url keeps link targets (see _TextParser)."""
    p = _TextParser(base_url)
    p.feed(page)
    text = html.unescape("".join(p.parts))
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text).strip()
    return text, p.ld_blobs


def strip_html(fragment: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", " ", fragment or ""))


def _get(url: str, **kwargs) -> requests.Response:
    resp = requests.get(url, headers={**UA, **kwargs.pop("headers", {})}, timeout=kwargs.pop("timeout", TIMEOUT), **kwargs)
    resp.raise_for_status()
    return resp


# --- JSON-LD (Club Free Time and any schema.org-marked page) -----------------


def events_from_jsonld(blobs: list[str], site: str, page_url: str) -> list[dict]:
    found: list[dict] = []

    def visit(obj: Any) -> None:
        if isinstance(obj, list):
            for item in obj:
                visit(item)
            return
        if not isinstance(obj, dict):
            return
        typ = str(obj.get("@type", ""))
        if obj.get("@type") == "ItemList":
            for li in obj.get("itemListElement", []):
                visit(li.get("item", li) if isinstance(li, dict) else li)
            return
        if "@graph" in obj:
            visit(obj["@graph"])
        if "Event" in typ and obj.get("name"):
            loc = obj.get("location")
            if isinstance(loc, dict):
                addr = loc.get("address")
                addr_text = addr if isinstance(addr, str) else " ".join(
                    str(addr.get(k, "")) for k in ("streetAddress", "addressLocality") if isinstance(addr, dict)
                )
                location = " ".join(x for x in (loc.get("name"), addr_text) if x)
            else:
                location = str(loc or "")
            offers = obj.get("offers")
            if isinstance(offers, list) and offers:
                offers = offers[0]
            price = ""
            if isinstance(offers, dict):
                p = offers.get("price")
                price = "Free" if p in (0, "0", "0.00") else (f"{p} {offers.get('priceCurrency', '')}".strip() if p not in (None, "") else "")
            ev = normalize(site, obj.get("name", ""), to_local_iso(obj.get("startDate")), to_local_iso(obj.get("endDate")),
                           location=location, url=obj.get("url") or page_url, cost=price, summary=obj.get("description") or "")
            if ev:
                found.append(ev)

    for blob in blobs:
        try:
            visit(json.loads(blob.strip()))
        except json.JSONDecodeError:
            continue
    return found


# --- NYPL ----------------------------------------------------------------------
# The library's Refinery API (refinery.nypl.org) disappeared in October 2026:
# the host no longer resolves. www.nypl.org now sits behind the same Imperva
# challenge as 92NY, which even headless Firefox on the Pi does not get past,
# and NYPL's BiblioCommons site has its Events feature switched off (both
# checked 2026-10-18). What is left is Eventbrite, where NYPL lists its
# ticketed talks, tours and performances (SNFL, Schwarzman, Performing Arts,
# branches): read Eventbrite's search listings for the library and keep the
# events at NYPL venues. Free branch programs that were only on nypl.org are
# not available this way.

_NYPL_VENUE = re.compile(
    r"new york public library|\bnypl\b|schwarzman|stavros niarchos|\bsnfl\b|library for the performing arts"
    r"|schomburg|yoseloff|science, industry|\blibrary( center)?\b",
    re.I,
)
# Other library systems and "libraries" that the generic "... Library" match would let through.
_NOT_NYPL = re.compile(
    r"brooklyn public library|queens (public )?library|jersey city|bayonne|society library|morgan library|central library",
    re.I,
)


def is_nypl_venue(location: str) -> bool:
    """True for an Eventbrite venue string that names an NYPL building or branch."""
    return bool(location) and bool(_NYPL_VENUE.search(location)) and not _NOT_NYPL.search(location)


def fetch_nypl(url: str) -> list[dict]:
    """NYPL's events as listed on Eventbrite (see above), attributed to nypl.org and de-duplicated across the searches."""
    events: list[dict] = []
    seen: set[str] = set()
    for search in NYPL_EVENTBRITE_SEARCHES:
        for e in fetch_eventbrite(search):
            if not is_nypl_venue(e.get("location") or ""):
                continue
            ev = normalize("nypl.org", e["title"], e["start"], e.get("end"), location=e["location"], url=e["url"],
                           cost=e.get("cost") or "", category=e.get("category") or "", summary=e.get("summary") or "")
            if ev and ev["id"] not in seen:
                seen.add(ev["id"])
                events.append(ev)
    return events


# --- DoNYC ---------------------------------------------------------------------


DONYC_BEST_OF = "https://donyc.com/best-concerts-events-new-york-city.json"


def donyc_feed_url(url: str) -> str:
    """The JSON twin of a DoNYC page. The homepage (no path) means the curated best-of feed."""
    parts = urlparse(url)
    path = parts.path.rstrip("/")
    if not path:
        return DONYC_BEST_OF
    if path.endswith(".json"):
        return f"{parts.scheme}://{parts.netloc}{path}"
    return f"{parts.scheme}://{parts.netloc}{path}.json"


def fetch_donyc(url: str) -> list[dict]:
    """The page's JSON twin: the curated best-of page gives event_groups; day pages give events."""
    api = donyc_feed_url(url)
    data = _get(api, headers={"Accept": "application/json"}).json()
    items = [e for g in data.get("event_groups", []) for e in g.get("events", [])] or data.get("events", [])
    events: list[dict] = []
    for e in items:
        venue = e.get("venue") or {}
        location = ", ".join(x for x in (venue.get("title"), venue.get("full_address") or venue.get("address")) if x)
        link = e.get("permalink") or e.get("buy_url") or ""
        if link.startswith("/"):
            link = "https://donyc.com" + link
        cost = "Free" if e.get("is_free") else (e.get("ticket_info") or "")
        ev = normalize("donyc.com", e.get("title", ""), to_local_iso(e.get("tz_adjusted_begin_date") or e.get("begin_date")),
                       to_local_iso(e.get("tz_adjusted_end_date")), location=location, url=link, cost=cost,
                       category=e.get("category") or "", summary=e.get("excerpt") or "")
        if ev:
            events.append(ev)
    return events


# --- Eventbrite ----------------------------------------------------------------


def _server_data(page: str) -> dict | None:
    marker = page.find("window.__SERVER_DATA__")
    if marker < 0:
        return None
    brace = page.find("{", marker)
    try:
        obj, _ = json.JSONDecoder().raw_decode(page, brace)
    except (json.JSONDecodeError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None


def _destination_events(obj: Any, out: list[dict]) -> None:
    if isinstance(obj, dict):
        if obj.get("_type") == "destination_event" and obj.get("name"):
            out.append(obj)
            return
        for v in obj.values():
            _destination_events(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _destination_events(v, out)


def fetch_eventbrite(url: str, *, pages: int = EVENTBRITE_PAGES) -> list[dict]:
    events: list[dict] = []
    for page in range(1, pages + 1):
        resp = requests.get(url, params={"page": page} if page > 1 else None, headers=UA, timeout=TIMEOUT)
        if resp.status_code != 200:
            break
        data = _server_data(resp.text)
        raw_events: list[dict] = []
        if data:
            _destination_events(data, raw_events)
        if not raw_events:
            _, blobs = parse_html(resp.text)
            events.extend(events_from_jsonld(blobs, "eventbrite.com", url))
            continue
        for e in raw_events:
            start_date, start_time = e.get("start_date") or "", e.get("start_time") or ""
            end_date, end_time = e.get("end_date") or start_date, e.get("end_time") or ""
            start = to_local_iso(f"{start_date}T{start_time}:00" if start_time else start_date)
            end = to_local_iso(f"{end_date}T{end_time}:00" if (end_time and start_time) else (end_date if not start_time else None))
            venue = e.get("primary_venue") or {}
            addr = (venue.get("address") or {}).get("localized_address_display", "")
            location = ", ".join(x for x in (venue.get("name"), addr) if x)
            avail = e.get("ticket_availability") or {}
            cost = "Free" if avail.get("is_free") else ((avail.get("minimum_ticket_price") or {}).get("display") or "")
            tags = [t.get("display_name") for t in (e.get("tags") or []) if isinstance(t, dict) and t.get("display_name")]
            ev = normalize("eventbrite.com", e.get("name", ""), start, end, location=location, url=e.get("url") or url,
                           cost=cost, category=", ".join(tags[:3]), summary=e.get("summary") or "")
            if ev:
                events.append(ev)
    return events


# --- generic pages --------------------------------------------------------------


def fetch_page(url: str) -> tuple[str, list[str]]:
    resp = _get(url)
    return parse_html(resp.text, base_url=resp.url or url)


def fetch_for_url(url: str, *, llm_extract: LLMExtractor | None = None) -> tuple[list[dict], str]:
    """
    (events, method) for one Website List row. `method` is one of
    api | server-data | eventbrite (NYPL via Eventbrite) | json-ld | llm | unreadable |
    needs-llm. Network and
    parse errors propagate; the scout records them per site.
    """
    site = site_of(url)
    if site.endswith("nypl.org"):
        return fetch_nypl(url), "eventbrite"
    if site.endswith("donyc.com"):
        return fetch_donyc(url), "api"
    if "eventbrite." in site:
        return fetch_eventbrite(url), "server-data"
    text, blobs = fetch_page(url)
    events = events_from_jsonld(blobs, site, url)
    if events:
        return events, "json-ld"
    if len(text) < MIN_READABLE_CHARS:
        return [], "unreadable"
    if llm_extract is None:
        return [], "needs-llm"
    return llm_extract(url, text[:MAX_TEXT_FOR_LLM]), "llm"
