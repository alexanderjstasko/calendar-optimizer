"""
scout.py — finds public events on the sites in the Notion Website List and
recommends the ones worth attending, as proposals for the review panel.

Two jobs (see main.py):
  scout_fetch()      weekly: sync the Website List, fetch every site through
                     event_sources.py, upsert into discovered_events.
  scout_recommend()  daily: filter the discovered events to free evenings and
                     weekends inside the window, send them with the calendar,
                     goals, notes, decision history and the free-time target
                     to Claude (scout_prompt.md), validate the options, and
                     store them as `event` proposals. Earlier undecided event
                     proposals are superseded; approving one puts it on the
                     calendar and writes the site's "Upcoming Event" in
                     Notion; time_tracker.py counts it as attended once it
                     has passed.

The scout returns a menu, not a plan: a ranked shortlist of
LLM_SCOUT_SHORTLIST (16) options that may overlap each other (they are
alternatives). The best LLM_SCOUT_PAGE_SIZE (8) valid ones are shown
(status 'proposed'); the rest wait as 'queued' for reload(), which pages
through them instantly and only asks the model again once none are left
unseen. Approving one option supersedes the options that overlap it.

Preference learning is data, not training: every approve/reject/attend is
recorded on the discovered event and shown to the model as `history`.

    python3 scout.py fetch
    python3 scout.py recommend [--dry-run]
    python3 scout.py reload
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

import db
import event_sources
import geo
import hours
import llm_client
import notion_adapter
import optimizer
import preferences
from optimizer_schema import (
    DISPLAY_TIMEZONE,
    EXTRACT_TOOL_NAME,
    EXTRACTION_SCHEMA,
    RECOMMENDATION_SCHEMA,
    SCOUT_TOOL_DESCRIPTION,
    SCOUT_TOOL_NAME,
)

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env"))
logger = logging.getLogger(__name__)

NY = ZoneInfo(DISPLAY_TIMEZONE)
DEFAULT_WINDOW_DAYS = 30
DEFAULT_SHORTLIST = 16         # options the model ranks per run (LLM_SCOUT_SHORTLIST)
DEFAULT_PAGE_SIZE = 8          # options shown at once (LLM_SCOUT_PAGE_SIZE); the rest are queued
RELOAD_EXCLUDE_DAYS = 14       # a model reload skips candidates recommended this recently...
MIN_FRESH_CANDIDATES = 24      # ...unless that would leave fewer candidates than this
MAX_CANDIDATES = 150           # keeps the cli backend under its prompt cap
TRAVEL_BUFFER = dt.timedelta(minutes=30)
# Candidates must start inside the day's free window (hours.py, the app's Hours tab).
DEFAULT_SCOUT_PROMPT_PATH = Path(__file__).resolve().parent.parent / "scout_prompt.md"


def _setting(name: str, default: str) -> str:
    value = os.environ.get(name)
    return default if value is None or value.strip() == "" else value.strip()


def _int_setting(name: str, default: int) -> int:
    try:
        return int(_setting(name, str(default)))
    except ValueError as e:
        raise llm_client.LLMConfigError(f"{name} is not an integer") from e


def scout_prompt_path() -> Path:
    override = _setting("LLM_SCOUT_PROMPT_PATH", "")
    return Path(override).expanduser() if override else DEFAULT_SCOUT_PROMPT_PATH


def shortlist_size() -> int:
    """How many ranked options to ask for: LLM_SCOUT_SHORTLIST, else the old name LLM_SCOUT_MAX_RECOMMENDATIONS, else 16."""
    if _setting("LLM_SCOUT_SHORTLIST", ""):
        return max(1, _int_setting("LLM_SCOUT_SHORTLIST", DEFAULT_SHORTLIST))
    return max(1, _int_setting("LLM_SCOUT_MAX_RECOMMENDATIONS", DEFAULT_SHORTLIST))


def page_size() -> int:
    """How many options the Events tab shows at once (LLM_SCOUT_PAGE_SIZE, 8)."""
    return max(1, _int_setting("LLM_SCOUT_PAGE_SIZE", DEFAULT_PAGE_SIZE))


# --- Website List sync ---------------------------------------------------------------


def sync_websites() -> dict[str, int]:
    sites = notion_adapter.sync_websites()
    fetched = {s["source_website_id"] for s in sites}
    active = db.get_active_source_website_ids("notion")
    db.upsert_websites(sites)
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    stale = active - fetched
    for site_id in stale:
        db.soft_delete_website("notion", site_id, now)
    logger.info("Website List sync: %d upserted, %d soft-deleted", len(sites), len(stale))
    return {"upserted": len(sites), "soft_deleted": len(stale)}


# --- fetch ------------------------------------------------------------------------------


def _llm_extract(url: str, text: str) -> list[dict]:
    """Model-based extraction for text-only pages, constrained to the scout window."""
    today = dt.date.today()
    window_end = today + dt.timedelta(days=_int_setting("LLM_SCOUT_WINDOW_DAYS", DEFAULT_WINDOW_DAYS))
    system = (
        "You extract public events from the text of a New York City events web page. "
        f"Today is {today}. Return only events with a concrete calendar date between {today} and {window_end} "
        "inclusive; skip anything undated, in the past, or that is a venue or category link rather than an event. "
        "One entry per event (a listing with several dates becomes several entries). Use empty strings when a "
        "field is unknown. Times are local New York time, 24-hour HH:MM. Keep summary to one short sentence. "
        "Links appear in the text as [https://...] right after the words they belong to: set url to the "
        "event's own page (the link after its title or its 'details'/'tickets' link), copied verbatim, and "
        "leave it empty rather than use a link to a category, venue or the listing page itself. "
        f"Call the {EXTRACT_TOOL_NAME} tool once."
    )
    result = llm_client.complete_structured(
        system, f"Source: {url}\nPage text:\n{text}", EXTRACTION_SCHEMA, effort="low",
        tool_name=EXTRACT_TOOL_NAME, tool_description="Return the extracted events.",
    )
    site = event_sources.site_of(url)
    events = []
    for e in result.data["events"]:
        start = f"{e['date']}T{e['start_time']}:00" if e.get("start_time") else e["date"]
        end = f"{e['date']}T{e['end_time']}:00" if (e.get("end_time") and e.get("start_time")) else None
        ev = event_sources.normalize(site, e["title"], event_sources.to_local_iso(start), event_sources.to_local_iso(end),
                                     location=e.get("location", ""), url=e.get("url") or url, cost=e.get("cost", ""),
                                     category=e.get("category", ""), summary=e.get("summary", ""))
        if ev:
            events.append(ev)
    return events


def scout_fetch(*, refresh_websites: bool = True, window_days: int | None = None,
                only_sites: list[str] | None = None) -> dict[str, Any]:
    """Fetch every active site and upsert what it lists inside the window. One site failing never stops the others."""
    db.init_db()
    summary: dict[str, Any] = {"sites": [], "new": 0, "updated": 0, "errors": 0}
    if refresh_websites:
        try:
            summary["website_list"] = sync_websites()
        except Exception as exc:
            logger.warning("Website List sync failed, using the rows already stored: %s", exc)
            summary["website_list"] = f"sync failed: {type(exc).__name__}: {exc}"
    window_days = window_days or _int_setting("LLM_SCOUT_WINDOW_DAYS", DEFAULT_WINDOW_DAYS)
    today = dt.date.today()
    horizon = (today + dt.timedelta(days=window_days)).isoformat()

    for site in db.get_websites():
        if only_sites and not any(needle in site["url"] for needle in only_sites):
            continue
        started = time.monotonic()
        seen_at = dt.datetime.now(dt.timezone.utc).isoformat()
        try:
            events, method = event_sources.fetch_for_url(site["url"], llm_extract=_llm_extract)
        except Exception as exc:
            db.set_website_fetch_status(site["id"], seen_at, "error", 0, f"{type(exc).__name__}: {exc}"[:300])
            summary["sites"].append({"url": site["url"], "method": "error", "error": f"{type(exc).__name__}: {exc}"[:200]})
            summary["errors"] += 1
            logger.warning("scout: %s failed: %s", site["url"], exc)
            continue
        in_window = [e for e in events if today.isoformat() <= e["start"][:10] <= horizon]
        new, updated = db.upsert_discovered_events(in_window, site["id"], seen_at)
        db.set_website_fetch_status(site["id"], seen_at, method, len(in_window), None)
        summary["sites"].append({"url": site["url"], "method": method, "fetched": len(events), "in_window": len(in_window),
                                 "new": new, "seconds": round(time.monotonic() - started, 1)})
        summary["new"] += new
        summary["updated"] += updated
        if method in ("unreadable", "needs-llm"):
            logger.warning("scout: %s is %s; nothing extracted", site["url"], method)
    return summary


# --- recommend ---------------------------------------------------------------------------


def _parse(value: str, all_day: bool) -> dt.datetime:
    return db.parse_event_time(value, all_day)


def _candidate_window(now: dt.datetime, window_days: int) -> tuple[dt.datetime, dt.datetime]:
    start = (now + dt.timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
    return start, start + dt.timedelta(days=window_days)


def filter_candidates(events: list[dict], busy: list[tuple[dt.datetime, dt.datetime]], window_start: dt.datetime,
                      window_end: dt.datetime, free_hours: dict | None = None) -> tuple[list[dict], dict[str, int]]:
    """
    Deterministic schedule filter: starts inside the day's free window (hours.py:
    weekday evenings and weekend days by default), not overlapping busy blocks
    (+ travel buffer). All-day events only need to be in the window.
    """
    free_hours = free_hours or hours.load()
    kept: list[dict] = []
    dropped = {"past_or_outside": 0, "before_free": 0, "after_free": 0, "busy": 0, "unparseable": 0}
    for e in events:
        try:
            start = _parse(e["start"], bool(e["all_day"]))
            end = _parse(e["end"], bool(e["all_day"])) if e.get("end") else None
        except (ValueError, TypeError):
            dropped["unparseable"] += 1
            continue
        if e["all_day"]:
            end = start + dt.timedelta(days=1)
        elif end is None or end <= start:
            end = start + dt.timedelta(hours=2)
        if start < window_start or start >= window_end:
            dropped["past_or_outside"] += 1
            continue
        if not e["all_day"]:
            free = hours.ranges_for(start.astimezone(NY).date(), free_hours)["free"]
            local = start.astimezone(NY)
            if not hours.in_range(local.time(), free):
                dropped["before_free" if hours.minutes(local.strftime("%H:%M")) < hours.minutes(free["start"]) else "after_free"] += 1
                continue
            if hours.too_soon_after_work(start, free_hours, TRAVEL_BUFFER):
                dropped["before_free"] += 1
                continue
            if any(start - TRAVEL_BUFFER < b_end and end + TRAVEL_BUFFER > b_start for b_start, b_end in busy):
                dropped["busy"] += 1
                continue
        kept.append(e)
    return kept, dropped


def spread_sample(events: list[dict], limit: int) -> list[dict]:
    """Cap a date-ordered list by sampling evenly across it, so later weeks keep candidates."""
    if len(events) <= limit:
        return events
    step = len(events) / limit
    return [events[int(i * step)] for i in range(limit)]


def keyword_match(event: dict, keywords: list[str]) -> bool:
    """True when any key word from the Hours tab appears in the title, summary or category (case-insensitive)."""
    if not keywords:
        return False
    text = " ".join(str(event.get(k) or "") for k in ("title", "summary", "category")).lower()
    return any(k.lower() in text for k in keywords)


def cap_candidates(events: list[dict], limit: int, keywords: list[str]) -> tuple[list[dict], int]:
    """
    Keep every key-word match (never dropped by the cap, up to `limit`), then
    fill the rest by sampling the others across the window. Date order is
    kept. Returns (candidates, number of key-word matches).
    """
    matches = [e for e in events if keyword_match(e, keywords)]
    if len(events) <= limit:
        return events, len(matches)
    chosen = {id(e) for e in matches[:limit]}
    rest = spread_sample([e for e in events if id(e) not in chosen], max(0, limit - len(chosen)))
    chosen.update(id(e) for e in rest)
    return [e for e in events if id(e) in chosen], len(matches)


def _compact(e: dict) -> dict:
    return {
        "id": e["id"], "title": e["title"][:80], "start": e["start"], "end": e.get("end") or "",
        "all_day": bool(e["all_day"]), "site": e["site"], "location": (e.get("location") or "")[:60],
        "cost": (e.get("cost") or "")[:20], "category": (e.get("category") or "")[:30],
        "summary": (e.get("summary") or "")[:110], "url": (e.get("url") or "")[:100],
    }


def _recommended_since(candidate: dict, cutoff: dt.datetime) -> bool:
    value = candidate.get("recommended_at")
    if not value:
        return False
    try:
        return db.parse_event_time(value, False) >= cutoff
    except (ValueError, TypeError):
        return False


def drop_recently_recommended(candidates: list[dict], now: dt.datetime, days: int) -> tuple[list[dict], int]:
    """
    For a reload: leave out candidates shown in the last `days` days (the
    person has seen them), unless fewer than MIN_FRESH_CANDIDATES would remain
    -- then keep everything. Returns (candidates, how many were left out).
    """
    cutoff = now - dt.timedelta(days=days)
    fresh = [c for c in candidates if not _recommended_since(c, cutoff)]
    if len(fresh) < MIN_FRESH_CANDIDATES:
        return candidates, 0
    return fresh, len(candidates) - len(fresh)


def build_scout_payload(now: dt.datetime, window_days: int,
                        exclude_recent_days: int | None = None) -> tuple[dict, list[dict], list[dict]]:
    """(payload, fixed calendar events with parsed times, candidate rows)."""
    window_start, window_end = _candidate_window(now, window_days)
    opt_payload, fixed = optimizer.build_payload(window_start, window_end)
    busy = [(ev["start_dt"], ev["end_dt"]) for ev in fixed if not ev["all_day"]]
    # Undecided only: approved/rejected/attended events never come back. Events
    # still pending from yesterday may be picked again; that run is superseded.
    discovered = db.get_discovered_events(window_start.isoformat(), window_end.isoformat(), undecided_only=True)
    candidates, dropped = filter_candidates(discovered, busy, window_start, window_end)
    if exclude_recent_days:
        candidates, dropped["recently_recommended"] = drop_recently_recommended(candidates, now, exclude_recent_days)
    keywords = hours.event_keywords()
    candidates, keyword_hits = cap_candidates(candidates, MAX_CANDIDATES, keywords)

    already_planned = [
        {"title": p["title"], "start": p["start"], "goal_id": p.get("goal_id") or ""}
        for p in db.get_proposals(status="promoted") if p["item_type"] == "event" and p["start"] >= window_start.isoformat()
    ]
    payload = {
        "generated_at": now.isoformat(timespec="minutes"),
        "timezone": DISPLAY_TIMEZONE,
        "window": {"start": window_start.isoformat(), "end": window_end.isoformat()},
        "preferences": {
            **{k: opt_payload["preferences"][k] for k in ("home_location", "default_travel_minutes", "free_time_hours_per_week", "hours")},
            "shortlist_size": shortlist_size(),
            # The Hours tab's "Event key words": what to look out for this time, beside the goals' notes.
            "event_keywords": keywords,
        },
        "calendar_busy": [{"title": e["title"][:50], "start": e["start"], "end": e["end"]} for e in opt_payload["calendar"] if not e["all_day"]],
        "goals": optimizer.event_interest_goals(),  # Type "Event Interest" (and untyped goals)
        "context_goals": opt_payload["context_goals"],
        "already_planned": already_planned,
        "history": db.get_decision_history(limit=60),
        "person": {k: v for k, v in preferences.person_context(recent=15).items() if k in ("stated_preferences", "learned_preferences")},
        "candidates": [{**_compact(e), "keyword_match": keyword_match(e, keywords)} for e in candidates],
        "filter_stats": {"discovered_in_window": len(discovered), "candidates": len(candidates), "dropped": dropped,
                         "keyword_matches": keyword_hits},
    }
    return payload, fixed, candidates


def validate_recommendations(recs: list[dict], candidates: list[dict], fixed: list[dict], goals: list[dict],
                             window_start: dt.datetime, window_end: dt.datetime) -> list[dict]:
    """
    Turn the model's ranked options into proposal rows (status proposed or
    invalid), never dropping anything; `rank` is the model's order (1 = best).
    Options may overlap each other -- they are alternatives, and approving one
    supersedes the others -- so the only overlap check is against the fixed
    calendar. Listing the same candidate twice is invalid.
    """
    by_id = {c["id"]: c for c in candidates}
    free_hours = hours.load()
    goal_ids = {g["id"] for g in goals}
    fixed_timed = [(ev["start_dt"], ev["end_dt"], ev.get("title") or ev["id"]) for ev in fixed if not ev["all_day"]]
    rows: list[dict] = []
    offered: set[str] = set()
    for rank, r in enumerate(recs, start=1):
        cand = by_id.get(r.get("discovered_event_id", ""))
        reason = None
        start = end = None
        if cand is None:
            reason = f"unknown candidate id {r.get('discovered_event_id')!r}"
        elif cand["id"] in offered:
            reason = "repeats an earlier option"
        else:
            try:
                start = _parse(r["start"], False)
                end = _parse(r["end"], False)
            except (ValueError, TypeError):
                reason = "unparseable start/end"
            else:
                if end <= start:
                    reason = "end is not after start"
                elif start < window_start or start >= window_end:
                    reason = "outside the window"
                elif start.strftime("%Y-%m-%d") != cand["start"][:10]:
                    reason = f"date {start:%Y-%m-%d} differs from the candidate's {cand['start'][:10]}"
                elif r.get("goal_id") and r["goal_id"] not in goal_ids:
                    reason = f"unknown goal id {r['goal_id']!r}"
                elif hours.too_soon_after_work(start, free_hours, TRAVEL_BUFFER):
                    reason = "starts before work ends (plus travel)"
                else:
                    for f_start, f_end, f_title in fixed_timed:
                        if start < f_end and end > f_start:
                            reason = f"overlaps existing event {f_title!r}"
                            break
        if cand is not None and reason is None:
            offered.add(cand["id"])
        title = cand["title"] if cand else str(r.get("discovered_event_id"))
        row = {
            "item_type": "event",
            "item_id": cand["id"] if cand else "",
            "discovered_event_id": cand["id"] if cand else None,
            "website_id": cand.get("website_id") if cand else None,
            "goal_id": r.get("goal_id") or None,
            "url": cand.get("url") if cand else None,
            "cost": (cand or {}).get("cost") or "",
            "title": title,
            "description": " | ".join(x for x in ((cand or {}).get("summary") or "", (cand or {}).get("cost") or "", (cand or {}).get("url") or "") if x)[:400],
            "location": (cand or {}).get("location") or "",
            "start": r.get("start") or (cand["start"] if cand else ""),
            "end": r.get("end") or (cand.get("end") if cand else "") or "",
            "all_day": False,
            "timezone": DISPLAY_TIMEZONE,
            "rationale": f"{r.get('why', '')} Fit: {r.get('schedule_fit', '')}".strip(),
            "status": "invalid" if reason else "proposed",
            "invalid_reason": reason,
            "rank": rank,
        }
        rows.append(row)
    return rows


def split_page(rows: list[dict], size: int, shown_at: str) -> tuple[int, int]:
    """
    The best `size` valid options (by rank, i.e. list order) stay 'proposed'
    and get shown_at; the other valid ones become 'queued' for reload().
    Mutates the rows; returns (shown, queued).
    """
    shown = queued = 0
    for row in rows:
        if row["status"] != "proposed":
            continue
        if shown < size:
            row["shown_at"] = shown_at
            shown += 1
        else:
            row["status"] = "queued"
            queued += 1
    return shown, queued


def scout_recommend(*, now: dt.datetime | None = None, dry_run: bool = False, window_days: int | None = None,
                    exclude_recent_days: int | None = None) -> dict[str, Any]:
    """
    One recommendation run: a ranked shortlist from the model, the best page
    shown, the rest queued. exclude_recent_days (used by reload) leaves out
    candidates recommended that recently, see drop_recently_recommended().
    """
    db.init_db()
    now = now or dt.datetime.now(NY)
    window_days = window_days or _int_setting("LLM_SCOUT_WINDOW_DAYS", DEFAULT_WINDOW_DAYS)
    if shortlist_size() <= page_size():
        logger.warning("scout shortlist (%d) fits on one page (%d): nothing is queued, so every reload asks the model; "
                       "set LLM_SCOUT_SHORTLIST (or drop an old LLM_SCOUT_MAX_RECOMMENDATIONS) to change that",
                       shortlist_size(), page_size())
    if not dry_run:
        preferences.refresh_learned()  # digest new decisions first; never fatal
    payload, fixed, candidates = build_scout_payload(now, window_days, exclude_recent_days)
    window_start, window_end = _candidate_window(now, window_days)
    system = optimizer.load_prompt_file(scout_prompt_path(), SCOUT_TOOL_NAME, "LLM_SCOUT_PROMPT_PATH")
    user = json.dumps(payload, separators=(",", ":"))
    model = llm_client._env("LLM_MODEL", llm_client.DEFAULT_MODEL) or llm_client.DEFAULT_MODEL
    backend = llm_client.get_backend()
    summary: dict[str, Any] = {
        "kind": "scout", "backend": backend, "model": model,
        "window_start": window_start.isoformat(), "window_end": window_end.isoformat(),
        "candidates": len(candidates), "filter_stats": payload["filter_stats"], "history": len(payload["history"]),
        "scout_prompt_path": str(scout_prompt_path()),
    }
    if dry_run:
        tokens, method = llm_client.estimate_tokens(system, user, RECOMMENDATION_SCHEMA, model=model,
                                                    tool_name=SCOUT_TOOL_NAME, tool_description=SCOUT_TOOL_DESCRIPTION)
        summary.update(dry_run=True, input_tokens_estimate=tokens, estimate_method=method, payload_chars=len(user))
        return summary
    if not candidates:
        summary.update(run_id=None, proposed=0, invalid=0, notes="no candidates in the window; run a fetch first")
        return summary

    started = dt.datetime.now(dt.timezone.utc)
    run_id = f"scout_{started.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
    db.create_optimizer_run(run_id, started.isoformat(), backend, model, summary["window_start"], summary["window_end"],
                            system_prompt=system, payload_json=user, kind="scout")
    clock = time.monotonic()
    try:
        result = llm_client.complete_structured(system, user, RECOMMENDATION_SCHEMA, max_tokens=16000,
                                                tool_name=SCOUT_TOOL_NAME, tool_description=SCOUT_TOOL_DESCRIPTION)
    except llm_client.LLMError as exc:
        db.finish_optimizer_run(run_id, status="failed", finished_at=dt.datetime.now(dt.timezone.utc).isoformat(),
                                error=f"{type(exc).__name__}: {exc}")
        raise
    elapsed = time.monotonic() - clock
    rows = validate_recommendations(result.data["recommendations"], candidates, fixed, payload["goals"], window_start, window_end)
    now_iso = dt.datetime.now(dt.timezone.utc).isoformat()
    shown, queued = split_page(rows, page_size(), now_iso)
    superseded = db.supersede_open_proposals(now_iso, ("event",))  # shown and queued alike
    db.insert_proposals(run_id, rows, now_iso)
    # Only what is on the page counts as recommended: queued options stay
    # eligible for a later model reload if the person never sees them.
    db.mark_discovered_recommended([r["discovered_event_id"] for r in rows if r.get("shown_at") and r["discovered_event_id"]], now_iso)
    usage = result.usage or {}
    input_tokens = sum(int(usage.get(k) or 0) for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
    invalid = len(rows) - shown - queued
    db.finish_optimizer_run(run_id, status="succeeded", finished_at=now_iso, input_tokens=input_tokens or None,
                            output_tokens=usage.get("output_tokens"), cost_usd=result.cost_usd,
                            proposed_count=shown + queued, invalid_count=invalid, notes=result.data.get("notes"))
    summary.update(run_id=run_id, proposed=shown, queued=queued, invalid=invalid,
                   invalid_reasons=[r["invalid_reason"] for r in rows if r["status"] == "invalid"],
                   notes=result.data.get("notes", ""), input_tokens=input_tokens, output_tokens=usage.get("output_tokens"),
                   cost_usd=result.cost_usd, elapsed_s=round(elapsed, 1), superseded=superseded)
    logger.info("scout %s: %d shown, %d queued, %d invalid, %.1fs", run_id, shown, queued, invalid, elapsed)
    _places_soon()
    return summary


def _places_soon() -> None:
    """Neighbourhoods and map pins for the new options, looked up in the background (geo.py, ~1 s each)."""
    if not geo.enabled():
        return

    def run() -> None:
        try:
            geo.backfill()
        except Exception:  # noqa: BLE001 -- best effort; the tab falls back to the venue name
            logger.exception("geo backfill failed")

    threading.Thread(target=run, name="geo-backfill", daemon=True).start()


def reload(*, now: dt.datetime | None = None) -> dict[str, Any]:
    """
    The Events tab's Reload (POST /scout/reload). While unseen options are
    queued: the next page from the queue, instantly and without tokens
    (db.rotate_event_options; the page just shown goes back to the end of the
    queue, nothing is rejected). Otherwise a model run that skips candidates
    recommended in the last RELOAD_EXCLUDE_DAYS days; the shown options are
    superseded as with any run. Raises llm_client.LLMError when that call fails.
    -> {source: "queue"|"model", shown: [proposal rows], queued: unseen left, run_id?}
    """
    db.init_db()
    now = now or dt.datetime.now(NY)
    db.expire_past_proposals(now)
    rotated = db.rotate_event_options(page_size(), dt.datetime.now(dt.timezone.utc).isoformat())
    if rotated is not None:
        out: dict[str, Any] = {"source": "queue"}
    else:
        summary = scout_recommend(now=now, exclude_recent_days=RELOAD_EXCLUDE_DAYS)
        out = {"source": "model", "run_id": summary.get("run_id"), "notes": summary.get("notes", ""),
               "invalid": summary.get("invalid", 0)}
    out["shown"] = db.get_proposals(status="proposed", item_type="event")
    out["queued"] = db.count_unseen_queued_options()
    return out


# --- decisions (called from the approval routes) ----------------------------------------


def record_decision(proposal: dict, decision: str) -> dict[str, Any]:
    """
    After an event proposal is approved or rejected: mirror the decision on the
    discovered event (already done by db.promote/revoke) and, on approval,
    write the site's "Upcoming Event" in Notion. Best effort: a Notion failure
    is reported, never raised, so approval itself always succeeds.
    """
    out: dict[str, Any] = {"decision": decision}
    if proposal.get("item_type") != "event":
        return out
    if proposal.get("discovered_event_id"):
        db.set_discovered_decision(proposal["discovered_event_id"], "approved" if decision == "approved" else "rejected")
    site = db.get_website(proposal["website_id"]) if proposal.get("website_id") else None
    if decision == "approved" and site and site.get("source") == "notion":
        text = upcoming_event_text(proposal)
        try:
            written = notion_adapter.update_text_property(site["source_website_id"], notion_adapter.UPCOMING_EVENT_PROP, text)
            db.set_website_notion_mirror(site["id"], upcoming_event=written)
            out["upcoming_event"] = written
        except Exception as exc:
            logger.warning("scout: could not write Upcoming Event for %s: %s", site["url"], exc)
            out["upcoming_event_error"] = f"{type(exc).__name__}: {exc}"
    return out


def upcoming_event_text(proposal: dict) -> str:
    """What an approval writes to the site's "Upcoming Event" in Notion."""
    return f"{proposal['title']} — {proposal['start'][:10]}"


def clear_upcoming_event(proposal: dict) -> dict[str, Any]:
    """
    Undo the Notion side of an approval (on revoke): clear the site's
    "Upcoming Event" -- but only while it still holds the text this approval
    wrote, read fresh, so a later approval or a hand edit is never erased.
    Best effort like record_decision: failures are reported, never raised.
    """
    out: dict[str, Any] = {}
    site = db.get_website(proposal["website_id"]) if proposal.get("website_id") else None
    if proposal.get("item_type") != "event" or not site or site.get("source") != "notion":
        return out
    expected = upcoming_event_text(proposal)
    prop = notion_adapter.UPCOMING_EVENT_PROP
    try:
        current = notion_adapter.read_text_property(site["source_website_id"], prop)
        if current.strip() != expected.strip():
            out["upcoming_event_kept"] = current
            return out
        written = notion_adapter.update_text_property(site["source_website_id"], prop, "")
        db.set_website_notion_mirror(site["id"], upcoming_event=written)
        out["upcoming_event_cleared"] = expected
    except Exception as exc:
        logger.warning("scout: could not clear Upcoming Event for %s: %s", site["url"], exc)
        out["upcoming_event_error"] = f"{type(exc).__name__}: {exc}"
    return out


def status() -> dict[str, Any]:
    now = dt.datetime.now(NY)
    window_days = _int_setting("LLM_SCOUT_WINDOW_DAYS", DEFAULT_WINDOW_DAYS)
    ws, we = _candidate_window(now, window_days)
    return {
        "websites": [{k: s.get(k) for k in ("id", "url", "upcoming_event", "events_attended", "last_fetched_at",
                                             "last_fetch_method", "last_fetch_count", "last_fetch_error")} for s in db.get_websites()],
        "discovered_in_window": len(db.get_discovered_events(ws.isoformat(), we.isoformat(), undecided_only=False)),
        "undecided_in_window": len(db.get_discovered_events(ws.isoformat(), we.isoformat(), undecided_only=True)),
        "pending_event_proposals": sum(1 for p in db.get_proposals(status="proposed") if p["item_type"] == "event"),
        "queued_unseen_options": db.count_unseen_queued_options(),
        "shortlist_size": shortlist_size(),
        "page_size": page_size(),
        "last_run": next(iter(db.get_optimizer_runs(limit=1, kind="scout")), None),
        "window": {"start": ws.isoformat(), "end": we.isoformat()},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["fetch", "recommend", "reload", "status"])
    parser.add_argument("--dry-run", action="store_true", help="recommend: count tokens only; no model call")
    parser.add_argument("--no-website-sync", action="store_true", help="fetch: skip the Notion Website List refresh")
    parser.add_argument("--only", nargs="*", help="fetch: only sites whose URL contains one of these strings")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        if args.command == "fetch":
            out = scout_fetch(refresh_websites=not args.no_website_sync, only_sites=args.only or None)
        elif args.command == "recommend":
            out = scout_recommend(dry_run=args.dry_run)
        elif args.command == "reload":
            out = reload()
        else:
            out = status()
    except llm_client.LLMError as exc:
        print(json.dumps({"ok": False, "error": type(exc).__name__, "detail": str(exc)}, indent=2))
        return 1
    print(json.dumps(out, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
