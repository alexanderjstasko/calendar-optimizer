"""
preferences.py — what the system knows about the person's scheduling taste,
and how it keeps learning.

Two sources, both sent to the optimizer and the scout on every run:

  stated    the "Stated" section of <project root>/preferences.md, written by
            the person in plain language (never touched by code; in git)
  learned   <project root>/preferences.learned.md, rewritten by Claude from
            the person's decisions: every approval, rejection and reschedule
            of a proposal, plus the approve/reject/attend history of scouted
            events. Machine state, so it is git-ignored: each host (Mac, Pi)
            learns from its own events.db and a `git pull` never conflicts
            with it. Hand edits are kept as the base of the next rewrite.
            A legacy learned:start/end section in preferences.md is still
            read until the first rewrite moves it out.

Learning is data plus one small model call: db.plan_decision_stats() gives
plain counts (approval rate per kind and per time slot, how blocks get
moved), db.get_plan_decision_history() the recent decisions, and
refresh_learned() asks Claude to rewrite the learned note once enough new
decisions have accumulated (LLM_LEARN_EVERY_DECISIONS, default 5). The note
is short, bullet-style, and quotes patterns rather than guessing.

    python3 preferences.py            # show stated / learned / stats
    python3 preferences.py --refresh  # rewrite the learned note now
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

import db
import llm_client

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env"))
logger = logging.getLogger(__name__)

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "preferences.md"
LEARNED_HEADER = (
    "<!-- Rewritten by backend/preferences.py from your decisions (git-ignored, per host). "
    "Edit freely: the next rewrite starts from your version. -->"
)
DEFAULT_LEARN_EVERY = 5
LEARN_TOOL_NAME = "submit_preferences"
START_MARK = "<!-- learned:start -->"
END_MARK = "<!-- learned:end -->"
SETTING_SEEN = "preferences_decisions_seen"
SETTING_UPDATED = "preferences_learned_updated_at"

TEMPLATE = """# Preferences

Write anything you like under **Stated**; the optimizer and the scout read it
on every run. What the system learns from your approvals, rejections and
reschedules lives in preferences.learned.md next to this file (see
backend/preferences.py).

## Stated

(Nothing yet. Examples: "Keep Friday evenings free." "Deep work is best
before noon." "I prefer two long task blocks to many short ones.")
"""

LEARN_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "learned": {
            "type": "string",
            "description": "Markdown bullet list, at most ~12 bullets, of concrete scheduling preferences inferred from the decisions. Cite the pattern briefly, e.g. '- Task blocks are approved in the evening and rejected before noon (7 of 8).'",
        },
        "changed": {"type": "string", "description": "One sentence on what changed versus the previous note, or 'first note'."},
    },
    "required": ["learned", "changed"],
    "additionalProperties": False,
}

LEARN_SYSTEM = f"""You maintain a short note describing one person's scheduling preferences,
inferred only from their decisions on calendar proposals. You receive the
previous note, their own stated preferences, plain statistics, and recent
decisions (approved, rejected, or moved to a different time before approval;
for scouted public events also attended).

Write the new note as concrete, checkable bullets: what they accept, what
they reject, when they move things and in which direction, which kinds of
events they attend. Keep bullets that the data still supports, drop ones it
no longer does, and never contradict the stated preferences: those are theirs.
Public events (kind "event") happen at their listed times, so a decision on one
says something about the event, never about the time of day.
Do not speculate beyond the data; a pattern seen once is not a preference,
say "early signs" for weak ones. Call the `{LEARN_TOOL_NAME}` tool once."""


def path() -> Path:
    override = (os.environ.get("LLM_PREFERENCES_PATH") or "").strip()
    return Path(override).expanduser() if override else DEFAULT_PATH


def learned_path() -> Path:
    override = (os.environ.get("LLM_LEARNED_PREFERENCES_PATH") or "").strip()
    if override:
        return Path(override).expanduser()
    p = path()
    return p.with_name(f"{p.stem}.learned.md")


def ensure_file() -> Path:
    p = path()
    if not p.exists():
        p.write_text(TEMPLATE, encoding="utf-8")
    return p


def load() -> dict[str, str]:
    """{'stated': ..., 'learned': ...}; preferences.md is created from the template if missing."""
    text = ensure_file().read_text(encoding="utf-8")
    learned = ""
    lp = learned_path()
    if lp.exists():
        learned = re.sub(r"<!--.*?-->", "", lp.read_text(encoding="utf-8"), flags=re.S).strip()
    else:
        m = re.search(re.escape(START_MARK) + r"(.*?)" + re.escape(END_MARK), text, re.S)  # legacy layout
        if m:
            learned = m.group(1).strip()
    stated = ""
    s = re.search(r"^## Stated\s*\n(.*?)(?=^## |\Z)", text, re.S | re.M)
    if s:
        stated = s.group(1).strip()
    if stated.startswith("(Nothing yet"):
        stated = ""
    if learned.startswith("(Nothing learned yet"):
        learned = ""
    return {"stated": stated, "learned": learned}


def write_learned(note: str) -> None:
    """Replace the learned note. preferences.md (the person's file) is never written."""
    learned_path().write_text(f"{LEARNED_HEADER}\n\n{note.strip()}\n", encoding="utf-8")


def _compact_decisions(limit: int = 40) -> list[dict]:
    out = []
    for d in db.get_plan_decision_history(limit=limit):
        entry = {
            "kind": d["item_type"], "title": (d["title"] or "")[:60], "start": d["start"], "end": d["end"],
            "decision": "approved" if d["status"] in db.APPROVED_STATUSES else ("rejected" if d["status"] == "rejected" else "pending"),
        }
        if d.get("adjusted_at") and d.get("original_start"):
            entry["moved_from"] = {"start": d["original_start"], "end": d["original_end"]}
        out.append(entry)
    return out


def learning_payload() -> dict[str, Any]:
    prefs = load()
    return {
        "stated_preferences": prefs["stated"],
        "previous_learned_note": prefs["learned"],
        "decision_stats": db.plan_decision_stats(),
        "recent_decisions": _compact_decisions(40),
        "scouted_events": db.get_decision_history(limit=30),
    }


def person_context(recent: int = 25) -> dict[str, Any]:
    """The block both prompts receive: stated + learned text, stats, and recent decisions."""
    prefs = load()
    return {
        "stated_preferences": prefs["stated"],
        "learned_preferences": prefs["learned"],
        "decision_stats": db.plan_decision_stats(),
        "recent_decisions": _compact_decisions(recent),
    }


def refresh_learned_safely() -> dict[str, Any]:
    """refresh_learned() for the start of a plan or scout run: any failure is logged, never raised."""
    try:
        return refresh_learned()
    except Exception as exc:  # noqa: BLE001 -- a bad setting or a disk error must not stop the run
        logger.warning("preferences: learned-note refresh failed: %s", exc)
        return {"refreshed": False, "error": f"{type(exc).__name__}: {exc}"}


def refresh_learned(*, force: bool = False) -> dict[str, Any]:
    """
    Rewrite the learned note when enough new decisions exist (or force=True).
    Never raises on model failure: the previous note stays and the error is
    reported, so a run that calls this first can still proceed.
    """
    total = db.count_decisions()
    seen = int(db.get_setting(SETTING_SEEN, "0") or 0)
    try:
        threshold = int(os.environ.get("LLM_LEARN_EVERY_DECISIONS") or DEFAULT_LEARN_EVERY)
    except ValueError:
        logger.warning("LLM_LEARN_EVERY_DECISIONS is not a number; using %s", DEFAULT_LEARN_EVERY)
        threshold = int(DEFAULT_LEARN_EVERY)
    new = total - seen
    if not force and (new < threshold or total == 0):
        return {"refreshed": False, "new_decisions": new, "threshold": threshold}
    payload = learning_payload()
    try:
        result = llm_client.complete_structured(
            LEARN_SYSTEM, json.dumps(payload, separators=(",", ":")), LEARN_SCHEMA, effort="low", max_tokens=4000,
            tool_name=LEARN_TOOL_NAME, tool_description="Return the rewritten preferences note.",
        )
    except llm_client.LLMError as exc:
        logger.warning("preferences: could not refresh the learned note: %s", exc)
        return {"refreshed": False, "new_decisions": new, "error": f"{type(exc).__name__}: {exc}"}
    note = result.data["learned"].strip()
    write_learned(note)
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    db.set_setting(SETTING_SEEN, str(total), now)
    db.set_setting(SETTING_UPDATED, now, now)
    usage = result.usage or {}
    return {"refreshed": True, "new_decisions": new, "changed": result.data.get("changed", ""),
            "learned": note, "input_tokens": usage.get("input_tokens"), "output_tokens": usage.get("output_tokens")}


def status() -> dict[str, Any]:
    prefs = load()
    return {
        "path": str(path()),
        "learned_path": str(learned_path()),
        "stated": prefs["stated"],
        "learned": prefs["learned"],
        "learned_updated_at": db.get_setting(SETTING_UPDATED),
        "decisions_total": db.count_decisions(),
        "decisions_digested": int(db.get_setting(SETTING_SEEN, "0") or 0),
        "stats": db.plan_decision_stats(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--refresh", action="store_true", help="rewrite the learned note now (one small model call)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    db.init_db()
    out = refresh_learned(force=True) if args.refresh else status()
    print(json.dumps(out, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
