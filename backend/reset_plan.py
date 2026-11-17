"""
reset_plan.py — the fresh start: every pending proposal and every approved
block that has not happened yet comes off the calendar.

    python3 reset_plan.py --dry-run     # what would go
    python3 reset_plan.py               # backup events.db, then do it

Pending (shown or queued) proposals become `superseded`. Approved blocks
whose end is still ahead become `retired` and their events are soft-deleted:
the approval stays in the decision history the preference learner reads
(db.APPROVED_STATUSES), the calendar is empty of them. A retired public
event's discovered row loses its decision, so the scout may offer it again,
and the "Upcoming Event" its approval wrote in Notion's Website List is
cleared (only while the cell still holds that text). Rejections and blocks
that already happened are left alone. Run it on the host that owns the
database (the Pi), with the service stopped or idle.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import sqlite3
from pathlib import Path

import db


def backup(path: Path) -> Path:
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    target = path.with_name(f"{path.name}.backup-{stamp}")
    src = sqlite3.connect(path)
    dst = sqlite3.connect(target)
    with dst:
        src.backup(dst)
    src.close()
    dst.close()
    return target


def preview(now: dt.datetime) -> dict:
    pending = [p for p in db.get_proposals() if p["status"] in ("proposed", "queued")]
    ahead = []
    for p in db.get_proposals(status="promoted"):
        try:
            end = db.parse_event_time(p["end"], p["all_day"])
        except (ValueError, TypeError):
            continue
        if end > now:
            ahead.append(p)
    return {"pending": len(pending), "approved_ahead": len(ahead),
            "approved_titles": sorted({p["title"] for p in ahead})[:40]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    db.init_db()
    now = dt.datetime.now(dt.timezone.utc)
    if args.dry_run:
        print(json.dumps({"dry_run": True, **preview(now)}, indent=2))
        return 0
    copy = backup(Path(db.DB_PATH))
    out = db.retire_open_plan(now)
    cleared = []
    if out["retired_events"]:
        try:
            import scout  # needs NOTION_* set

            for p in out["retired_events"]:
                cleared.append({p["title"]: scout.clear_upcoming_event(p)})
        except Exception as exc:  # noqa: BLE001 -- the calendar side is done either way
            cleared.append({"error": f"{type(exc).__name__}: {exc}"})
    print(json.dumps({"backup": str(copy), "superseded": out["superseded"], "retired": len(out["retired"]),
                      "retired_events": len(out["retired_events"]), "notion_cleared": cleared}, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
