"""
sync_notion.py — Syncs the one Notion database (the Telos tree) into events.db.

One full fetch each run (there's no equivalent of Google's nextSyncToken for
Notion databases), split by `Level`: Telos/Goal/Project rows into
`tree_nodes`, Routine rows into `goals`, Task rows into `tasks`. Each table
is then diffed: a previously-synced row that is absent from the latest fetch
has been deleted or archived in Notion and is soft-deleted here -- the same
full-fetch-and-diff shape used for Partiful, without windowing.

By design this is NOT an APScheduler job. Notion data only needs to be
fresh when the optimizer runs, so the (future) optimizer runner calls
sync_notion() at the start of each run, then hands the tasks/goals to
Claude via llm_client.complete_structured() and gets events JSON back.

NOTE ON THE OPTIMIZER: tasks/goals rows carry a `scheduled_event_ids`
column (see db.py) that this file intentionally never writes to. That
column is for whatever eventually creates calendar events for a task or
goal -- the planned LLM optimizer -- to record what it already scheduled,
and to check before scheduling again. Re-running this sync should never
disturb that bookkeeping; it only touches the Notion-sourced columns.
"""

from __future__ import annotations

import datetime
import logging

import db
import notion_adapter

logger = logging.getLogger(__name__)


def _soft_delete_stale(source: str, fetched_ids: set[str], active_ids: set[str], soft_delete_fn) -> int:
    """
    Shared diff step for both tasks and goals: anything active in the DB
    but absent from the latest fetch has been deleted or archived in
    Notion, so it gets soft-deleted here too.
    """
    stale_ids = active_ids - fetched_ids
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    for stale_id in stale_ids:
        soft_delete_fn(source, stale_id, now)
    return len(stale_ids)


def _titled(rows: list[dict], title_key: str, kind: str) -> list[dict]:
    """
    Rows without a title (typically a blank row just added in Notion) can't be
    stored -- the title column is NOT NULL -- and used to make the whole sync
    fail, so the optimizer planned with stale data. Skip them with a warning.
    Their ids still count as fetched, so a row whose name is only cleared for
    a moment is not soft-deleted.
    """
    kept = [r for r in rows if (r.get(title_key) or "").strip()]
    if len(kept) < len(rows):
        logger.warning("Notion %s sync: skipped %d untitled row(s)", kind, len(rows) - len(kept))
    return kept


def _apply_tasks(fetched: list[dict], keep: set[str] = frozenset()) -> tuple[int, int]:
    fetched_ids = {t["source_task_id"] for t in fetched} | keep
    tasks = _titled(fetched, "task", "tasks")
    active_ids = db.get_active_source_task_ids("notion")
    db.upsert_tasks(tasks)
    deleted = _soft_delete_stale("notion", fetched_ids, active_ids, db.soft_delete_task)
    logger.info("Notion tasks sync: %d upserted, %d soft-deleted", len(tasks), deleted)
    return len(tasks), deleted


def _apply_goals(fetched: list[dict], keep: set[str] = frozenset()) -> tuple[int, int]:
    fetched_ids = {g["source_goal_id"] for g in fetched} | keep
    goals = _titled(fetched, "goal", "goals")
    active_ids = db.get_active_source_goal_ids("notion")
    db.upsert_goals(goals)
    deleted = _soft_delete_stale("notion", fetched_ids, active_ids, db.soft_delete_goal)
    logger.info("Notion goals sync: %d upserted, %d soft-deleted", len(goals), deleted)
    return len(goals), deleted


def _apply_nodes(fetched: list[dict], keep: set[str] = frozenset()) -> tuple[int, int]:
    fetched_ids = {n["source_node_id"] for n in fetched} | keep
    nodes = _titled(fetched, "title", "tree nodes")
    active_ids = db.get_active_source_node_ids("notion")
    db.upsert_tree_nodes(nodes)
    deleted = _soft_delete_stale("notion", fetched_ids, active_ids, db.soft_delete_tree_node)
    logger.info("Notion tree sync: %d nodes upserted, %d soft-deleted", len(nodes), deleted)
    return len(nodes), deleted


def sync_tasks() -> None:
    _apply_tasks(notion_adapter.sync_tasks())


def sync_goals() -> None:
    _apply_goals(notion_adapter.sync_goals())


def sync_notion() -> dict[str, int]:
    """Entry point: one fetch of the tree database, all three tables updated and diffed."""
    tree = notion_adapter.fetch_tree()
    empty = not (tree["nodes"] or tree["goals"] or tree["tasks"] or tree.get("skipped"))
    if empty and (db.get_active_source_goal_ids("notion") or db.get_active_source_task_ids("notion")):
        # An empty answer (a revoked share, a wrong database id) would soft-delete
        # every row and take every approved block off the calendar.
        raise RuntimeError("Notion returned no rows at all; nothing was changed")
    if tree.get("skipped"):
        logger.warning("Notion sync: %d row(s) without a known Level were skipped: %s", len(tree["skipped"]),
                       ", ".join(str(r.get("title")) for r in tree["skipped"][:10]))
    # A row skipped for its Level still exists in Notion: it keeps whatever it
    # was (and its approved blocks) until its Level is fixed.
    keep = {r["id"] for r in tree.get("skipped") or []}
    with db.batch():  # one transaction for the whole tree
        nodes, nodes_deleted = _apply_nodes(tree["nodes"], keep)
        goals, goals_deleted = _apply_goals(tree["goals"], keep)
        tasks, tasks_deleted = _apply_tasks(tree["tasks"], keep)
    return {"nodes": nodes, "nodes_deleted": nodes_deleted, "goals": goals, "goals_deleted": goals_deleted,
            "tasks": tasks, "tasks_deleted": tasks_deleted, "skipped": len(tree.get("skipped") or [])}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    db.init_db()
    print(sync_notion())
    print(f"Active nodes: {len(db.get_tree_nodes())}")
    print(f"Active tasks: {len(db.get_tasks())}")
    print(f"Active goals: {len(db.get_goals())}")
