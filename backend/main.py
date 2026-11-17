"""
FastAPI server for calendar_optimizer.

Serves events straight from events.db (via db.py) to the Schedule-X +
Preact frontend. All normalization -- field stripping, Temporal-string
assembly, filtering cancelled/deleted events -- already happens inside
db.get_schedule_x_events(), so this file is just routing + CORS + the
background sync schedule.

Run with:
    source .venv/bin/activate
    uvicorn main:app --reload --port 8000

Expects db.py (and the events.db it manages) to live alongside this file,
and .env to live one directory up (project root).
"""

from __future__ import annotations

import json
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Literal

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path

import Apple_ics_pull
import Google_cal_pull
import Outlook_ics_pull
import Partiful_ics_pull
from Apple_ics_pull import fetch_and_sync as sync_apple
from Google_cal_pull import fetch_and_sync as sync_google
from Outlook_ics_pull import fetch_and_sync as sync_outlook
from Partiful_ics_pull import fetch_and_sync as sync_partiful

import db
import geo
import hours
import llm_client
import notifier
import review_model
import schedule_config

# So APScheduler's own error/info logs (e.g. a failed job) actually print
# somewhere, instead of being silently swallowed.
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("calendar_optimizer")

scheduler = BackgroundScheduler()


def scout_fetch_job() -> None:
    """Weekly: refresh the Website List and pull every site's upcoming events."""
    import scout  # lazy: needs NOTION_* set

    summary = scout.scout_fetch()
    logger.info("scout fetch: %d new, %d updated, %d site errors", summary["new"], summary["updated"], summary["errors"])


def scout_recommend_job() -> None:
    """Daily, in the morning: recommend events from what the weekly fetch found."""
    import scout

    try:
        summary = scout.scout_recommend()
    except llm_client.LLMError as exc:
        logger.error("scout recommend failed: %s", exc)
        return
    logger.info("scout recommend: %s proposed, %s invalid", summary.get("proposed"), summary.get("invalid"))
    notifier.notify_run(summary, "scout")


def optimizer_run_job() -> None:
    """Scheduled optimizer pass (off by default; OPTIMIZER_RUN_DAYS/TIME in .env)."""
    import optimizer  # lazy: pulls in the Notion adapter

    try:
        summary = optimizer.run_optimizer()
    except (llm_client.LLMError, optimizer.RunInProgress) as exc:
        logger.error("scheduled optimizer run failed: %s", exc)
        return
    logger.info("scheduled optimizer run: %s proposed, %s invalid", summary.get("proposed"), summary.get("invalid"))
    notifier.notify_run(summary, "optimizer")


def notion_check_job() -> None:
    """Hourly: new Notion rows are placed as pending proposals, deleted ones leave the calendar; then the change email."""
    import triggers  # lazy: pulls in the Notion adapter

    summary = triggers.notion_check()
    if summary["new"] or summary["deleted"] or summary["errors"]:
        logger.info("notion check: %d new, %d deleted, %d errors", len(summary["new"]), len(summary["deleted"]), len(summary["errors"]))


def conflict_check_job() -> None:
    """After the syncs: flag approved blocks a synced event now overlaps and propose moves (pending)."""
    import triggers

    summary = triggers.conflict_check()
    if summary["conflicts"] or summary["cleared"] or summary["errors"]:
        logger.info("conflict check: %d conflicts (%d proposed), %d cleared, %d errors",
                    len(summary["conflicts"]), len(summary["proposed"]), len(summary["cleared"]), len(summary["errors"]))


def settle_task_time_job() -> None:
    """Write occurred optimizer task blocks back to Notion's Time Spent (hr). Idempotent."""
    import time_tracker  # lazy: pulls in the Notion adapter, which needs NOTION_* set

    summary = time_tracker.settle_task_time()
    if summary["settled"] or summary["errors"]:
        logger.info("time_tracker: settled=%d errors=%d", len(summary["settled"]), len(summary["errors"]))


@asynccontextmanager
async def lifespan(app: FastAPI):
    # CREATE TABLE IF NOT EXISTS -- safe to run on every boot.
    db.init_db()

    # Cadence for every job comes from .env (see schedule_config.py); "off"
    # disables a job. Distinct ids are required -- APScheduler rejects
    # duplicates at add_job time. next_run_time=datetime.now() forces an
    # immediate first run for the interval jobs, so a restart never shows
    # stale data for a whole interval.
    schedule = schedule_config.load_schedule()
    for problem in schedule.errors:
        logger.error("schedule setting: %s", problem)
    tz = schedule_config.TIMEZONE

    if schedule.calendars.enabled:
        every = schedule.calendars.minutes
        # Staggered 20 s apart (first run and every run after it): the four used
        # to fire in the same second and fight over SQLite's write lock
        # ("database is locked", 2026-10-08).
        first = datetime.now()
        # Only the calendars set up in .env (an ICS link; for Google the OAuth
        # files): an unused source is skipped quietly. A configured one runs,
        # and fails loudly, exactly as before.
        syncs = [(job_id, job) for job_id, job, configured in (
            ("google_sync", sync_google, Google_cal_pull.TOKEN_PATH.exists() or Google_cal_pull.CREDENTIALS_PATH.exists()),
            ("apple_sync", sync_apple, bool(Apple_ics_pull.APPLE_ICS_URL)),
            ("outlook_sync", sync_outlook, bool(Outlook_ics_pull.OUTLOOK_ICS_URL)),
            ("partiful_sync", sync_partiful, bool(Partiful_ics_pull.PARTIFUL_ICS_URL)),
        ) if configured]
        for i, (job_id, job) in enumerate(syncs):
            scheduler.add_job(job, "interval", minutes=every, id=job_id, next_run_time=first + timedelta(seconds=20 * i))
    if schedule.settle.enabled:
        # Only writer to Notion; acts only on approved events whose end has passed, once each.
        scheduler.add_job(
            settle_task_time_job, "interval", minutes=schedule.settle.minutes, id="task_time_settle",
            next_run_time=datetime.now(), max_instances=1, coalesce=True,
        )
    # The events scout reads its sites from the Notion Website List; without one it is skipped quietly.
    scout_configured = bool((os.environ.get("NOTION_WEBSITES_DB_ID") or "").strip())
    if schedule.scout_fetch.enabled and scout_configured:
        j = schedule.scout_fetch
        scheduler.add_job(
            scout_fetch_job, CronTrigger(day_of_week=j.day_of_week, hour=j.hour, minute=j.minute, timezone=tz),
            id="scout_fetch", max_instances=1, coalesce=True, misfire_grace_time=6 * 3600,
        )
    if schedule.scout_recommend.enabled and scout_configured:
        j = schedule.scout_recommend
        scheduler.add_job(
            scout_recommend_job, CronTrigger(day_of_week=j.day_of_week, hour=j.hour, minute=j.minute, timezone=tz),
            id="scout_recommend", max_instances=1, coalesce=True, misfire_grace_time=3 * 3600,
        )
    if schedule.optimizer.enabled:
        j = schedule.optimizer
        scheduler.add_job(
            optimizer_run_job, CronTrigger(day_of_week=j.day_of_week, hour=j.hour, minute=j.minute, timezone=tz),
            id="optimizer_run", max_instances=1, coalesce=True, misfire_grace_time=3 * 3600,
        )
    if schedule.notion_check.enabled:
        # First pass a few minutes after boot (the calendar syncs go first).
        scheduler.add_job(
            notion_check_job, "interval", minutes=schedule.notion_check.minutes, id="notion_check",
            next_run_time=datetime.now() + timedelta(minutes=3), max_instances=1, coalesce=True,
        )
    if schedule.conflict_check.enabled:
        # Runs two minutes after each round of syncs, so it sees what they brought.
        scheduler.add_job(
            conflict_check_job, "interval", minutes=schedule.conflict_check.minutes, id="conflict_check",
            next_run_time=datetime.now() + timedelta(minutes=2), max_instances=1, coalesce=True,
        )
    if geo.enabled():
        # Neighbourhoods and map pins for the Events tab (geo.py; cached, ~1 lookup a second).
        scheduler.add_job(
            geo.backfill, "interval", minutes=60, id="geo_backfill",
            next_run_time=datetime.now() + timedelta(minutes=1), max_instances=1, coalesce=True,
        )
    logger.info("Schedule: %s", schedule.describe())
    scheduler.start()
    logger.info("Scheduler started with jobs: %s", [j.id for j in scheduler.get_jobs()])

    yield

    scheduler.shutdown(wait=False)
    logger.info("Scheduler shut down.")


app = FastAPI(title="calendar_optimizer", lifespan=lifespan)

# Allow the local Vite dev server, plus access over Tailscale, to call this API.
# TAILNET in .env (e.g. "example-tailnet.ts.net", the suffix of your MagicDNS
# names) admits every device on it by name; 100.x.y.z addresses always pass.
# Any port, http or https: `tailscale serve` can front the app over https
# (443 -> localhost:8000) so the PWA runs in a secure context (service worker).
_TAILNET = (os.environ.get("TAILNET") or "").strip().strip(".").lower()
_TAILNET_HOSTS = rf"|[a-z0-9-]+\.{re.escape(_TAILNET)}" if _TAILNET else ""
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ],
    allow_origin_regex=rf"^https?://(localhost|127\.0\.0\.1|100\.\d{{1,3}}\.\d{{1,3}}\.\d{{1,3}}{_TAILNET_HOSTS})(:\d+)?$",
    allow_methods=["GET", "POST", "PUT"],
    allow_headers=["*"],
)


@app.get("/events")
def get_events(calendar_id: str | None = None) -> list[dict]:
    """Return events ready for Schedule-X, optionally filtered by calendarId."""
    return db.get_schedule_x_events(calendar_id=calendar_id)


@app.get("/events/preview")
def events_preview() -> list[dict]:
    """
    Every pending proposal as a Schedule-X event (calendarId 'proposed', id
    'proposal_<id>', rationale as description): the calendar's "if I accept
    everything" overlay. Queued scout options are not on the page, so not here.
    """
    db.expire_past_proposals(datetime.now(timezone.utc))
    return db.get_preview_events()


@app.get("/dash/day")
def dash_day(date: str | None = None) -> dict:
    """One New York day for the Daily Dash (dash.py): events with kind and Telos, the awake window, free bands."""
    import dash

    try:
        return dash.day(date)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@app.get("/sync")
def sync(since: str | None = None) -> dict:
    """
    All four sources now populate sync_state and stamp last_modified on
    writes, but this endpoint doesn't yet diff against `since` -- it's
    still a placeholder. The frontend should keep calling /events for a
    full refresh until this is built out.
    """
    return {
        "added": [],
        "updated": [],
        "removed": [],
        "note": "Incremental sync not implemented yet — call /events for a full refresh.",
    }


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/health/llm")
def health_llm() -> JSONResponse:
    """
    Token-free readiness probe for the Claude backend selected by
    CLAUDE_BACKEND (see llm_client.py). 200 when the backend is usable,
    503 with a reason otherwise -- nothing here spends tokens.
    """
    try:
        info = llm_client.check_backend()
    except llm_client.LLMError as exc:
        return JSONResponse(
            status_code=503,
            content={"ok": False, "error": type(exc).__name__, "detail": str(exc)},
        )
    return JSONResponse(status_code=200 if info.get("ok") else 503, content=info)


# --- optimizer ------------------------------------------------------------------
# Thin routing over optimizer.py / db.py. A live run blocks for as long as the
# model call takes (tens of seconds); FastAPI runs these sync handlers in a
# thread pool so the rest of the API stays responsive.


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@app.post("/optimizer/run")
def optimizer_run(horizon_days: int | None = Query(None, ge=1, le=60), dry_run: bool = False,
                  refresh_notion: bool = True) -> dict:
    """Run one optimizer pass; dry_run=true only builds the payload and counts tokens."""
    import optimizer  # lazy: pulls in the Notion adapter, which needs NOTION_* set

    try:
        summary = optimizer.run_optimizer(horizon_days=horizon_days, dry_run=dry_run, refresh_notion=refresh_notion)
    except optimizer.RunInProgress as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except llm_client.LLMError as exc:
        raise HTTPException(status_code=502, detail=f"{type(exc).__name__}: {exc}")
    notifier.notify_run(summary, "optimizer")
    return summary


@app.get("/optimizer/runs")
def optimizer_runs(limit: int = Query(20, ge=1, le=200)) -> list[dict]:
    return db.get_optimizer_runs(limit=limit)


@app.get("/optimizer/runs/{run_id}/request")
def optimizer_run_request(run_id: str) -> dict:
    """Exactly what was sent to the model for one run: system prompt, structured payload, tool schema."""
    from optimizer_schema import PROPOSED_EVENTS_SCHEMA, SUBMIT_TOOL_DESCRIPTION, SUBMIT_TOOL_NAME

    run = db.get_optimizer_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    try:
        payload = json.loads(run["payload_json"]) if run.get("payload_json") else None
    except json.JSONDecodeError:
        payload = run["payload_json"]
    return {
        "run_id": run_id,
        "backend": run["backend"],
        "model": run["model"],
        "system_prompt": run.get("system_prompt"),
        "payload": payload,
        "tool": {"name": SUBMIT_TOOL_NAME, "description": SUBMIT_TOOL_DESCRIPTION, "input_schema": PROPOSED_EVENTS_SCHEMA},
    }


@app.get("/optimizer/proposals")
def optimizer_proposals(run_id: str | None = None, status: str | None = None) -> list[dict]:
    db.expire_past_proposals(datetime.now(timezone.utc))  # every view lists only what can still happen
    return db.get_proposals(run_id=run_id, status=status)


class Times(BaseModel):
    """A user-chosen start/end, ISO 8601; naive values are in the display zone (TIMEZONE)."""
    start: str
    end: str


def _reschedule(proposal_id: str, times: Times) -> None:
    try:
        moved = db.reschedule_proposal(proposal_id, times.start, times.end, _utc_now())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    if not moved:
        raise HTTPException(status_code=409, detail="proposal is already decided")


@app.post("/optimizer/proposals/{proposal_id}/reschedule")
def reschedule_proposal(proposal_id: str, times: Times) -> dict:
    """Move a pending proposal to the user's own start/end (kept as a learning signal)."""
    if db.get_proposal(proposal_id) is None:
        raise HTTPException(status_code=404, detail="proposal not found")
    _reschedule(proposal_id, times)
    p = db.get_proposal(proposal_id)
    return {"proposal_id": proposal_id, "start": p["start"], "end": p["end"], "original_start": p["original_start"]}


@app.post("/events/{event_id}/move")
def move_event(event_id: str, times: Times) -> dict:
    """
    Move an approved optimizer block from the calendar's event details (the
    free stand-in for Schedule-X's premium drag-and-drop). The block's proposal
    moves with it (a learning signal, like the Plan tab's pencil). Synced events
    are refused with 422: the app never writes to Google/Apple/Outlook/Partiful.
    """
    try:
        row = db.move_optimizer_event(event_id, times.start, times.end, _utc_now())
    except LookupError:
        raise HTTPException(status_code=404, detail="event not found")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return {"event": db.to_schedule_x_event(row), "proposal_id": row["source_event_id"]}


@app.post("/events/{event_id}/delete")
def delete_event(event_id: str) -> dict:
    """
    Take an approved optimizer block off the calendar from its event details.
    The proposal becomes `rejected` (a decision: the learner reads it and the
    routine expander never refills that date); attached travel goes with it.
    Synced events are refused with 422, like /move.
    """
    try:
        out = db.delete_optimizer_event(event_id, _utc_now())
    except LookupError:
        raise HTTPException(status_code=404, detail="event not found")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    proposal = out["proposal"]
    result = {"proposal_id": proposal["id"], "status": "rejected", "event_id": event_id, "travel": out["travel"]}
    if proposal["item_type"] == "event":
        try:
            import scout

            result["scout"] = scout.clear_upcoming_event(proposal)  # nothing stays in Notion once declined
        except Exception as exc:  # noqa: BLE001 -- the block is already off the calendar
            logger.warning("could not clear Upcoming Event for %s: %s", proposal["id"], exc)
            result["scout"] = {"upcoming_event_error": f"{type(exc).__name__}: {exc}"}
    return result


# Decisions. Approve and reject are real decisions (the preference learner reads
# them); superseded / expired / queued are not. One code path per action, used by
# the single routes, the batch route and approve-all, so they cannot drift.


class Undecidable(Exception):
    """The proposal is not in a state this action applies to (HTTP 409)."""


def _record_event_decision(proposal: dict, decision: str) -> dict:
    """
    The scout's side of a decision on an event option: the discovered event's
    decision and, on approval, the site's Notion "Upcoming Event". Best
    effort -- scout.record_decision() already swallows a failed Notion write;
    this also covers scout failing to import (NOTION_* unset), so a decision
    that has happened is never reported as failed.
    """
    try:
        import scout

        return scout.record_decision(proposal, decision)
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not record the %s decision for %s with the scout: %s", decision, proposal["id"], exc)
        if proposal.get("discovered_event_id"):
            db.set_discovered_decision(proposal["discovered_event_id"], "approved" if decision == "approved" else "rejected")
        return {"decision": decision, "error": f"{type(exc).__name__}: {exc}"}


def _approve(proposal: dict, now_iso: str) -> dict:
    """
    Promote onto the calendar; for an event option also record the decision
    and supersede overlapping alternatives. A move (replaces_event_id, see
    triggers.py) first takes the old block and its travel off the calendar.
    """
    if proposal["status"] not in ("proposed", "approved"):
        raise Undecidable(f"proposal is already {proposal['status']}")
    event_id = db.promote_proposal(proposal["id"], now_iso)
    if event_id is None:
        current = db.get_proposal(proposal["id"]) or proposal
        raise Undecidable(f"proposal is already {current['status']}")
    # Only once the move itself is on the calendar does the old block leave it.
    replaced = db.replace_block(proposal["replaces_event_id"], now_iso, proposal["id"]) if proposal.get("replaces_event_id") else []
    out = {"proposal_id": proposal["id"], "status": "promoted", "event_id": event_id, "superseded_overlaps": [],
           "replaced": replaced}
    if proposal["item_type"] == "event":
        out["superseded_overlaps"] = db.supersede_overlapping_options(proposal["id"], now_iso)
        out["scout"] = _record_event_decision(proposal, "approved")
    return out


def _reject(proposal: dict, now_iso: str) -> dict:
    if not db.reject_proposal(proposal["id"], now_iso):
        current = db.get_proposal(proposal["id"]) or proposal
        raise Undecidable(f"proposal is already {current['status']}")
    out = {"proposal_id": proposal["id"], "status": "rejected", "event_id": None}
    if proposal["item_type"] == "event":
        out["scout"] = _record_event_decision(proposal, "rejected")
    return out


def _revoke(proposal: dict, now_iso: str) -> dict:
    event_id = db.revoke_proposal(proposal["id"], now_iso)
    if event_id is None:
        current = db.get_proposal(proposal["id"]) or proposal
        if current.get("settled_at"):
            raise Undecidable("this block has already been logged to Notion, so it stays")
        raise Undecidable(f"proposal is {current['status']}, not promoted")
    # A routine block comes back as 'rejected' (see db.revoke_proposal), anything else as 'proposed'.
    after = db.get_proposal(proposal["id"]) or {}
    out = {"proposal_id": proposal["id"], "status": after.get("status", "proposed"), "event_id": event_id, "event": "cancelled"}
    if proposal.get("replaces_event_id"):
        # Undoing a move puts the block it replaced (and its travel) back where it was.
        out["restored"] = db.restore_replaced(proposal["id"], datetime.now(timezone.utc), now_iso)
    if proposal["item_type"] == "event":
        try:
            import scout

            out["scout"] = scout.clear_upcoming_event(proposal)  # nothing stays in Notion unless approved
        except Exception as exc:  # noqa: BLE001 -- the revoke itself has happened
            logger.warning("could not clear Upcoming Event for %s: %s", proposal["id"], exc)
            out["scout"] = {"upcoming_event_error": f"{type(exc).__name__}: {exc}"}
    return out


_ACTIONS = {"approve": _approve, "reject": _reject, "revoke": _revoke}


def _decide_one(proposal_id: str, action: str) -> dict:
    proposal = db.get_proposal(proposal_id)
    if proposal is None:
        raise HTTPException(status_code=404, detail="proposal not found")
    try:
        return _ACTIONS[action](proposal, _utc_now())
    except Undecidable as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@app.post("/optimizer/proposals/{proposal_id}/approve")
def approve_proposal(proposal_id: str, times: Times | None = None) -> dict:
    """
    Promote one proposal into `events` (source='optimizer'); it then shows on
    the calendar. An optional JSON body {start, end} first moves it to the
    user's own times. Approving an event option supersedes the undecided
    options that overlap it; their ids come back as `superseded_overlaps`.
    """
    if db.get_proposal(proposal_id) is None:
        raise HTTPException(status_code=404, detail="proposal not found")
    if times is not None:
        _reschedule(proposal_id, times)
    return _decide_one(proposal_id, "approve")


@app.post("/optimizer/proposals/{proposal_id}/reject")
def reject_proposal(proposal_id: str) -> dict:
    return _decide_one(proposal_id, "reject")


@app.post("/optimizer/proposals/{proposal_id}/revoke")
def revoke_proposal(proposal_id: str) -> dict:
    """Undo an approval: the event leaves the calendar and the proposal returns to pending."""
    return _decide_one(proposal_id, "revoke")


class Batch(BaseModel):
    """Several proposals, one action. `revoke` is accepted too (series toggles off)."""
    action: Literal["approve", "reject", "revoke"]
    ids: list[str]


@app.post("/optimizer/proposals/batch")
def proposals_batch(batch: Batch) -> dict:
    """
    Apply one action to many proposals (a series, or a whole day) through the
    same path as the single routes; every occurrence is its own decision.
    One failing id never stops the rest: it lands in `failed` with the reason.
    Approving event options also returns the overlapping options it removed.
    """
    db.expire_past_proposals(datetime.now(timezone.utc))  # never promote something already over
    done: list[dict] = []
    failed: list[dict] = []
    superseded: list[str] = []
    for proposal_id in dict.fromkeys(batch.ids):  # de-duplicated, order kept
        proposal = db.get_proposal(proposal_id)
        if proposal is None:
            failed.append({"id": proposal_id, "detail": "proposal not found"})
            continue
        try:
            out = _ACTIONS[batch.action](proposal, _utc_now())
        except Undecidable as exc:
            failed.append({"id": proposal_id, "detail": str(exc)})
            continue
        except Exception as exc:  # noqa: BLE001 -- report it, keep going
            logger.exception("batch %s failed for %s", batch.action, proposal_id)
            failed.append({"id": proposal_id, "detail": f"{type(exc).__name__}: {exc}"})
            continue
        done.append({"id": proposal_id, "status": out["status"], "event_id": out["event_id"]})
        superseded += [s for s in out.get("superseded_overlaps", []) if s not in superseded]
    return {"action": batch.action, "done": done, "failed": failed, "superseded_overlaps": superseded}


def _approve_pending(proposals: list[dict]) -> tuple[list[dict], list[str]]:
    """
    approve-all: only status 'proposed' rows are passed in (queued options are
    never approved wholesale). Event options go best rank first, so when two
    alternatives overlap the better one wins and the other is superseded.
    """
    now = _utc_now()
    order = sorted(proposals, key=lambda p: (p["item_type"] != "event", p.get("rank") is None, p.get("rank") or 0, p["start"]))
    promoted, superseded = [], []
    for proposal in order:
        try:
            out = _approve(proposal, now)
        except Undecidable:
            continue  # superseded by an earlier overlap, or decided meanwhile
        promoted.append({"proposal_id": proposal["id"], "event_id": out["event_id"]})
        superseded += out["superseded_overlaps"]
    return promoted, superseded


@app.post("/optimizer/approve-all")
def approve_all_pending(group: str | None = None) -> dict:
    """
    Promote every pending proposal, whichever run it came from. `group`
    ('events' or 'plan', see db.PROPOSAL_GROUPS) limits it to one section of
    the review tabs; omitted = everything. Queued options are never included.
    """
    if group is not None and group not in db.PROPOSAL_GROUPS:
        raise HTTPException(status_code=422, detail=f"group must be one of {sorted(db.PROPOSAL_GROUPS)}")
    db.expire_past_proposals(datetime.now(timezone.utc))  # never promote something already over
    pending = [p for p in db.get_proposals(status="proposed")
               if group is None or db.proposal_group(p["item_type"]) == group]
    promoted, superseded = _approve_pending(pending)
    return {"group": group, "promoted": promoted, "superseded_overlaps": superseded}


@app.post("/optimizer/runs/{run_id}/approve-all")
def approve_all(run_id: str) -> dict:
    """Promote every still-undecided (shown) proposal of a run."""
    db.expire_past_proposals(datetime.now(timezone.utc))  # never promote something already over
    promoted, superseded = _approve_pending(db.get_proposals(run_id=run_id, status="proposed"))
    return {"run_id": run_id, "promoted": promoted, "superseded_overlaps": superseded}


# --- time tracking --------------------------------------------------------------


@app.post("/routines/expand")
def routines_expand(dry_run: bool = False, horizon_days: int | None = None) -> dict:
    """Propose the routine goals' missing occurrences for the planning window now (no model call; pending)."""
    import routine

    return routine.expand_now(dry_run=dry_run, horizon_days=horizon_days)


# --- triggers: what changes the plan between Sunday runs (triggers.py) ----------------


@app.post("/triggers/notion")
def trigger_notion(dry_run: bool = False) -> dict:
    """The hourly Notion check, now: new rows placed (pending), deleted rows removed, change email."""
    import triggers

    return triggers.notion_check(dry_run=dry_run)


@app.post("/triggers/conflicts")
def trigger_conflicts(dry_run: bool = False) -> dict:
    """The conflict check, now: flag clashes with synced events and propose moves (pending)."""
    import triggers

    return triggers.conflict_check(dry_run=dry_run)


@app.post("/triggers/place/{item_type}/{item_id}")
def trigger_place(item_type: Literal["task", "goal"], item_id: str, dry_run: bool = False) -> dict:
    """Place one task or goal in the rest of the week now (pending proposals)."""
    import triggers

    try:
        return triggers.place_item(item_type, item_id, reason="Placed by hand", dry_run=dry_run)
    except llm_client.LLMError as exc:
        raise HTTPException(status_code=502, detail=f"{type(exc).__name__}: {exc}")


# --- a tapped free slot: what could go there (slot.py) -------------------------------


@app.get("/slot/options")
def slot_options(start: str, end: str) -> dict:
    """Scouted events and open tasks/goals that fit the slot [start, end] (ISO 8601; naive = New York)."""
    import slot

    try:
        return slot.options(start, end)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


class SlotPick(BaseModel):
    """What the person chose for the slot: kind event|task|goal, its id, and the times to book."""
    kind: Literal["event", "task", "goal"]
    id: str
    start: str
    end: str


@app.post("/slot/place")
def slot_place(pick: SlotPick) -> dict:
    """Book the chosen option in the slot: a proposal the person just approved, promoted at once."""
    import slot

    try:
        return slot.place(pick.kind, pick.id, pick.start, pick.end)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@app.post("/tasks/settle-time")
def settle_time(dry_run: bool = False) -> dict:
    """
    Run one settlement pass now: occurred optimizer task blocks are added to
    the task's Notion "Time Spent (hr)". dry_run=true reports without writing.
    """
    import time_tracker

    return time_tracker.settle_task_time(dry_run=dry_run)


# --- events scout ---------------------------------------------------------------


@app.get("/scout/status")
def scout_status() -> dict:
    import scout

    return scout.status()


@app.post("/scout/fetch")
def scout_fetch(refresh_websites: bool = True) -> dict:
    """Refresh the Website List and fetch every site now (slow: minutes). Normally weekly."""
    import scout

    return scout.scout_fetch(refresh_websites=refresh_websites)


@app.post("/scout/recommend")
def scout_recommend(dry_run: bool = False) -> dict:
    """Recommend events from the discovered candidates now. Normally every morning."""
    import scout

    try:
        summary = scout.scout_recommend(dry_run=dry_run)
    except llm_client.LLMError as exc:
        raise HTTPException(status_code=502, detail=f"{type(exc).__name__}: {exc}")
    notifier.notify_run(summary, "scout")
    return summary


@app.post("/scout/reload")
def scout_reload() -> dict:
    """
    The Events tab's Reload: the next page from the queue when unseen options
    are waiting (instant, no tokens), else a fresh scout run that skips what
    was recommended in the last 14 days (~2 min). Sends no email.
    -> {source: "queue"|"model", shown: [Proposal], queued: n, run_id?}
    """
    import scout

    try:
        return scout.reload()
    except llm_client.LLMError as exc:
        raise HTTPException(status_code=502, detail=f"{type(exc).__name__}: {exc}")


# --- email -----------------------------------------------------------------------


@app.post("/notify/pending")
def notify_pending(dry_run: bool = False) -> dict:
    """Email every upcoming pending proposal now; dry_run=true returns the text instead."""
    try:
        return notifier.send_pending_digest(dry_run=dry_run)
    except notifier.NotifyError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


# --- review ----------------------------------------------------------------------
# The review UI lives in the calendar app as hash-routed tabs (#/events, #/plan);
# this is the one document both tabs and their badges read.


def _scout_page_size() -> int:
    try:
        import scout  # lazy like the other scout routes; needs NOTION_* set

        return scout.page_size()
    except Exception:  # noqa: BLE001 -- a summary must render even if the scout cannot load
        return 8


@app.get("/review/summary")
def review_summary() -> dict:
    """
    {generated_at, events:{pending, queued, approved_upcoming, page_size, reload, items},
     plan:{pending, approved_upcoming, pending_series, series, held_back}, runs:{optimizer, scout}}.
    Items/occurrences hold pending and upcoming approved proposals; see review_model.py.
    """
    now = datetime.now(timezone.utc)
    db.expire_past_proposals(now)  # every view lists only what can still happen
    runs = {"optimizer": db.get_latest_run("optimizer"), "scout": db.get_latest_run("scout")}
    held_back = db.get_proposals(run_id=runs["optimizer"]["run_id"], status="invalid") if runs["optimizer"] else []
    rows = geo.attach(db.get_open_proposals(now))  # event rows gain place + summary, from the cache only
    return review_model.build_summary(rows, held_back, runs, now, page_size=_scout_page_size())


@app.api_route("/review", methods=["GET", "HEAD"], include_in_schema=False)
def review_page() -> RedirectResponse:
    """The old standalone approvals page moved into the app: old emails and bookmarks land on the Plan tab."""
    return RedirectResponse(url="/#/plan", status_code=307)


@app.get("/schedule")
def schedule_view() -> dict:
    """The effective cadence of every job, as read from .env at startup, plus any settings that fell back to defaults."""
    return {
        **schedule_config.load_schedule().describe(),
        "jobs": [{"id": j.id, "next_run": j.next_run_time.isoformat() if j.next_run_time else None} for j in scheduler.get_jobs()],
    }


# --- hours (sleep, work, home, office days, planning settings) -------------------
# The app's Hours tab. hours.validate() is the contract (the body is checked
# there, so a legacy document with a `free` range still saves).


@app.get("/hours")
def hours_view() -> dict:
    """Sleep and work hours per profile, home and office days, the free-time floor, review length and key words."""
    return hours.status()


@app.put("/hours")
def hours_save(body: dict) -> dict:
    """Replace the hours document. 422 with the field and the reason when a value makes no sense."""
    try:
        return hours.save(body)
    except hours.HoursError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e


# --- preferences -----------------------------------------------------------------


@app.get("/preferences")
def preferences_view() -> dict:
    """Stated and learned preferences, decision statistics, and learner bookkeeping."""
    import preferences

    return preferences.status()


@app.post("/preferences/refresh")
def preferences_refresh() -> dict:
    """Rewrite the learned-preferences note from all decisions now (one small model call)."""
    import preferences

    return preferences.refresh_learned(force=True)


# --- calendar app (production build) ---------------------------------------------
# On the Pi there is no Vite dev server: deploy.sh runs `npm run build` and the
# built app is served from here, same origin as the API, on port 8000. Mounted
# last so every API route above wins; skipped when no build exists (dev on the Mac).

FRONTEND_DIST = Path(__file__).resolve().parent.parent / "frontend" / "dist"
if FRONTEND_DIST.is_dir():
    app.mount("/", StaticFiles(directory=FRONTEND_DIST, html=True), name="frontend")
