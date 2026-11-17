"""Parsers in event_sources.py against canned payloads. No network."""

from __future__ import annotations

import json

import pytest

import event_sources as es


def test_site_and_stable_ids():
    assert es.site_of("https://www.eventbrite.com/d/ny--new-york/free--events/") == "eventbrite.com"
    a = es.event_id("nypl.org", "Poets  Out Loud ", "2026-10-03T19:00:00-04:00")
    b = es.event_id("nypl.org", "poets out loud", "2026-10-03")
    assert a == b and len(a) == 16


def test_to_local_iso_normalizes_zones_and_dates():
    assert es.to_local_iso("2026-09-20T20:00:00-05:00") == "2026-09-20T21:00:00-04:00"  # DoNYC's wrong-offset field
    assert es.to_local_iso("2026-09-27T18:00:00Z") == "2026-09-27T14:00:00-04:00"
    assert es.to_local_iso("2026-10-17") == "2026-10-17"
    assert es.to_local_iso("2026-09-28T19:00:00") == "2026-09-28T19:00:00-04:00"  # naive = New York
    assert es.to_local_iso("garbage") is None and es.to_local_iso(None) is None


def test_normalize_shapes_and_all_day():
    ev = es.normalize("x.org", "  Big   Talk ", "2026-10-01T19:00:00-04:00", "2026-10-01T20:30:00-04:00",
                      location=" The Hall ", summary="<p>Hello &amp; bye</p>", cost="Free")
    assert ev["title"] == "Big Talk" and ev["all_day"] is False and ev["summary"] == "Hello & bye"
    assert ev["end"] == "2026-10-01T20:30:00-04:00" and ev["timezone"] == "America/New_York"
    assert es.normalize("x.org", "Day", "2026-10-01")["all_day"] is True
    assert es.normalize("x.org", "Mismatch", "2026-10-01", "2026-10-01T20:00:00-04:00")["end"] is None
    assert es.normalize("x.org", "", "2026-10-01") is None and es.normalize("x.org", "T", None) is None


def test_jsonld_itemlist_and_graph():
    blobs = [json.dumps({"@type": "ItemList", "itemListElement": [
        {"item": {"@type": "Event", "name": "Lecture A", "startDate": "2026-10-02T18:00:00-04:00", "url": "https://e/a",
                  "location": {"name": "Cooper Union", "address": {"streetAddress": "7 E 7th St", "addressLocality": "New York"}},
                  "offers": {"price": "0"}}},
    ]}), json.dumps({"@graph": [{"@type": "BusinessEvent", "name": "Panel B", "startDate": "2026-10-05",
                                  "location": "Online", "offers": [{"price": 25, "priceCurrency": "USD"}]}]}),
             "not json"]
    evs = es.events_from_jsonld(blobs, "clubfreetime.com", "https://cft/page")
    assert [e["title"] for e in evs] == ["Lecture A", "Panel B"]
    assert evs[0]["location"] == "Cooper Union 7 E 7th St New York" and evs[0]["cost"] == "Free"
    assert evs[1]["all_day"] is True and evs[1]["cost"] == "25 USD" and evs[1]["url"] == "https://cft/page"


def test_parse_html_text_and_ldjson():
    page = '<html><head><script type="application/ld+json">{"@type":"Event","name":"X","startDate":"2026-10-01"}</script>' \
           '<style>.a{}</style></head><body><h1>Talks</h1><script>var x=1;</script><p>Oct 3 &amp; Oct 4</p></body></html>'
    text, blobs = es.parse_html(page)
    assert text == "Talks\nOct 3 & Oct 4" and len(blobs) == 1


def test_parse_html_keeps_event_links_for_the_extractor():
    page = ('<body><a href="/">Home</a><h2><a href="/events/talk-on-maps">A Talk on Maps</a></h2>'
            '<p>Nov 20 · <a href="https://tickets.example/123">Tickets</a> <a href="#top">Back to top</a>'
            ' <a href="mailto:x@example.org">Email us</a> <a href="https://site.example/list/">This page</a></p></body>')
    text, _ = es.parse_html(page, base_url="https://site.example/list/")
    assert "A Talk on Maps [https://site.example/events/talk-on-maps]" in text
    assert "Tickets [https://tickets.example/123]" in text
    assert "Home [" not in text  # too short to be an event link
    assert "#top" not in text and "mailto:" not in text and "This page [" not in text
    plain, _ = es.parse_html(page)  # without a base the text stays as it was
    assert "[" not in plain


def test_fetch_nypl_keeps_only_nypl_venues_from_eventbrite(monkeypatch):
    # NYPL events come from Eventbrite's search listings (the Refinery API is gone);
    # other library systems and look-alikes in the same results are dropped.
    def mk(title, loc, start="2026-10-28T19:00:00-04:00"):
        return es.normalize("eventbrite.com", title, start, "2026-10-28T20:30:00-04:00", location=loc,
                            url=f"https://www.eventbrite.com/e/{title.lower().replace(' ', '-')}", cost="Free", category="Arts", summary="A talk.")

    listings = {
        es.NYPL_EVENTBRITE_SEARCHES[0]: [
            mk("Afterglow", "New York Public Library for the Performing Arts, 40 Lincoln Center Plaza"),
            mk("Tour SNFL", "Stavros Niarchos Foundation Library (SNFL), 455 5th Ave"),
            mk("Webster talk", "Webster Library, 1465 York Ave"),
            mk("Storytime", "Brooklyn Public Library - Sheepshead Bay, 2636 E 14th St"),
            mk("Behind the scenes", "The New York Society Library, 53 E 79th St"),
            mk("Rooftop", "Monarch Rooftop, 71 W 35th St"),
        ],
        es.NYPL_EVENTBRITE_SEARCHES[1]: [
            mk("Afterglow", "New York Public Library for the Performing Arts, 40 Lincoln Center Plaza"),  # repeat
            mk("Schwarzman lecture", "New York Public Library- Stephen A Schwarzman Building, 476 5th Ave"),
            mk("Central", "Central Library, Information Commons, 10 Grand Army Plaza"),  # Brooklyn's
        ],
    }
    monkeypatch.setattr(es, "fetch_eventbrite", lambda url: listings[url])
    evs = es.fetch_nypl("https://www.nypl.org/events/calendar")
    assert [e["title"] for e in evs] == ["Afterglow", "Tour SNFL", "Webster talk", "Schwarzman lecture"]
    first = evs[0]
    assert first["site"] == "nypl.org" and first["id"] == es.event_id("nypl.org", "Afterglow", first["start"])
    assert (first["location"], first["cost"], first["url"], first["end"]) == (
        "New York Public Library for the Performing Arts, 40 Lincoln Center Plaza", "Free",
        "https://www.eventbrite.com/e/afterglow", "2026-10-28T20:30:00-04:00")


def test_is_nypl_venue():
    assert es.is_nypl_venue("NYPL - Ottendorfer, 135 2nd Ave")
    assert es.is_nypl_venue("Bronx Library Center")
    assert es.is_nypl_venue("Thomas Yoseloff Business Center, 455 5th Ave")
    assert not es.is_nypl_venue("Queens Public Library Langston Hughes")
    assert not es.is_nypl_venue("The Morgan Library & Museum (Outside)")
    assert not es.is_nypl_venue("")


def test_fetch_donyc_uses_json_twin_and_adjusted_times(monkeypatch):
    body = {"event_groups": [{"date": "2026-09-20", "events": [{
        "title": "Charlie Burg", "begin_date": "2026-09-20", "begin_time": "2026-09-20T20:00:00-05:00",
        "tz_adjusted_begin_date": "2026-09-20T20:00:00-04:00", "tz_adjusted_end_date": None,
        "venue": {"title": "Music Hall of Williamsburg", "full_address": "66 N 6th St, Brooklyn"},
        "permalink": "/events/2026/9/20/charlie-burg-tickets", "category": "Music", "is_free": False,
        "ticket_info": "18+", "excerpt": "Where were you..."}]}]}
    seen = {}

    class _Resp:
        def raise_for_status(self): pass
        def json(self): return body

    def fake_get(url, headers=None, timeout=None, **kw):
        seen["url"] = url
        return _Resp()

    monkeypatch.setattr(es.requests, "get", fake_get)
    evs = es.fetch_donyc("https://donyc.com/best-concerts-events-new-york-city")
    assert seen["url"] == "https://donyc.com/best-concerts-events-new-york-city.json"
    assert es.donyc_feed_url("https://donyc.com/") == es.DONYC_BEST_OF  # bare homepage row
    assert es.donyc_feed_url("https://donyc.com") == es.DONYC_BEST_OF
    assert es.donyc_feed_url("https://donyc.com/events/comedy?x=1") == "https://donyc.com/events/comedy.json"
    assert es.donyc_feed_url("https://donyc.com/events/2026/09/21.json") == "https://donyc.com/events/2026/09/21.json"
    e = evs[0]
    assert e["start"] == "2026-09-20T20:00:00-04:00"  # tz-adjusted field wins over the -05:00 one
    assert e["location"] == "Music Hall of Williamsburg, 66 N 6th St, Brooklyn"
    assert e["url"] == "https://donyc.com/events/2026/9/20/charlie-burg-tickets" and e["cost"] == "18+" and e["category"] == "Music"


def test_fetch_eventbrite_reads_server_data_blob(monkeypatch):
    blob = {"search": {"results": [{"_type": "destination_event", "name": "Lectures on Tap", "start_date": "2026-09-28",
                                    "start_time": "19:00", "end_date": "2026-09-28", "end_time": "20:30",
                                    "url": "https://www.eventbrite.com/e/1", "primary_venue": {"name": "Caveat",
                                    "address": {"localized_address_display": "21 A Clinton St, New York"}},
                                    "ticket_availability": {"is_free": False, "minimum_ticket_price": {"display": "$15.00"}},
                                    "tags": [{"display_name": "Science"}], "summary": "Talks over beer"},
                                   {"_type": "destination_event", "name": "All-day fair", "start_date": "2026-10-10",
                                    "start_time": "", "end_date": "2026-10-10", "end_time": "", "url": "https://www.eventbrite.com/e/2",
                                    "primary_venue": {}, "ticket_availability": {"is_free": True}}]}}
    html = "<html><script>window.__SERVER_DATA__ = " + json.dumps(blob) + ";\nwindow.other = 1;</script></html>"

    class _Resp:
        status_code = 200
        text = html

    calls = []
    monkeypatch.setattr(es.requests, "get", lambda url, params=None, headers=None, timeout=None: (calls.append(params), _Resp())[1])
    evs = es.fetch_eventbrite("https://www.eventbrite.com/d/ny--new-york/lectures--events/", pages=2)
    assert calls == [None, {"page": 2}]
    assert len(evs) == 4  # two per page, two pages (same fake body)
    a, b = evs[0], evs[1]
    assert a["start"] == "2026-09-28T19:00:00-04:00" and a["end"] == "2026-09-28T20:30:00-04:00"
    assert a["location"] == "Caveat, 21 A Clinton St, New York" and a["cost"] == "$15.00" and a["category"] == "Science"
    assert b["all_day"] is True and b["cost"] == "Free"


def test_fetch_for_url_dispatch_and_unreadable(monkeypatch):
    monkeypatch.setattr(es, "fetch_nypl", lambda url: [{"id": "n"}])
    assert es.fetch_for_url("https://www.nypl.org/events/calendar") == ([{"id": "n"}], "eventbrite")
    monkeypatch.setattr(es, "fetch_donyc", lambda url: [])
    assert es.fetch_for_url("https://donyc.com/x") == ([], "api")
    monkeypatch.setattr(es, "fetch_eventbrite", lambda url: [{"id": "e"}])
    assert es.fetch_for_url("https://www.eventbrite.com/d/ny--new-york/free--events/") == ([{"id": "e"}], "server-data")

    monkeypatch.setattr(es, "fetch_page", lambda url: ("tiny", []))
    assert es.fetch_for_url("https://www.92ny.org/whats-on/calendar") == ([], "unreadable")
    monkeypatch.setattr(es, "fetch_page", lambda url: ("x" * 5000, []))
    assert es.fetch_for_url("https://thoughtgallery.org/") == ([], "needs-llm")
    assert es.fetch_for_url("https://thoughtgallery.org/", llm_extract=lambda u, t: [{"id": "llm"}]) == ([{"id": "llm"}], "llm")
    monkeypatch.setattr(es, "fetch_page", lambda url: ("", [json.dumps({"@type": "Event", "name": "L", "startDate": "2026-10-01"})]))
    evs, method = es.fetch_for_url("https://www.clubfreetime.com/x")
    assert method == "json-ld" and evs[0]["site"] == "clubfreetime.com"
