"""ClickUp support connector — reads tasks, comments, spaces, and lists via lib.api.

Force-code trigger (d): ClickUp paginates task lists with a 0-based PAGE NUMBER (page=0,1,2,…)
rather than an item-count offset. lib.api's offset style advances ``offset += len(items)``, which
would send ``page=100`` instead of ``page=1`` on the second request. The connector owns the
page-number loop and delegates auth, retry, and error handling to lib.api.

Auth: personal tokens (pk_…) ride verbatim in the Authorization header (no "Bearer " prefix) via
the api_key_header strategy. OAuth access tokens are injected identically by the host, so a single
auth strategy covers both. The credential is always taken from RC_CONN_CLICKUP via lib.oauth.token
and never appears in argv, logs, or model context.

CLI:
    python -m lib.connectors.clickup tasks list/{list_id}        # tasks in a list
    python -m lib.connectors.clickup tasks team/{team_id}        # cross-workspace task search
    python -m lib.connectors.clickup task {task_id}              # single task detail
    python -m lib.connectors.clickup comments {task_id}          # task comments
    python -m lib.connectors.clickup spaces {team_id}            # list spaces in a workspace
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from lib import api

# ---------------------------------------------------------------------------
# Manifest — registered so `python -m lib.api get clickup …` works for
# one-shot single-page reads (task detail, comments, team list) even though
# multi-page task lists are handled by this script's page loop.
# ---------------------------------------------------------------------------

_MANIFEST_PATH = Path(__file__).resolve().parent / "manifest.yaml"
MANIFEST = api.register(api._parse_manifest_file(_MANIFEST_PATH))

PAGE_SIZE = 100  # ClickUp server-enforced ceiling for task list endpoints


def _client() -> api.Client:
    return api.client(MANIFEST, token_key="clickup")


# ---------------------------------------------------------------------------
# Pagination: page-number loop (the force-code trigger)
# ---------------------------------------------------------------------------


def _collect_tasks(path: str, *, query: dict | None = None, max_pages: int = 50) -> dict:
    """Page through a ClickUp task list endpoint using 0-based page numbers.

    ClickUp returns at most 100 tasks/page and signals "done" by returning fewer than PAGE_SIZE
    items — there is no has_more field or next cursor. We stop when ``len(items) < PAGE_SIZE``.
    Returns the same ``{items, incomplete, reason}`` shape as ``lib.api.Client.collect``.
    """
    c = _client()
    base_q: dict[str, Any] = dict(query or {})
    all_items: list[dict] = []
    for page in range(max_pages):
        q = dict(base_q, page=page)
        body = c.get(path, query=q)
        # All task-list endpoints wrap results under "tasks"
        items: list[dict] = body.get("tasks") or [] if isinstance(body, dict) else []
        all_items.extend(items)
        if len(items) < PAGE_SIZE:
            # Short page (or empty) → exhausted
            return {"items": all_items, "incomplete": False, "reason": ""}
    return {
        "items": all_items,
        "incomplete": True,
        "reason": f"reached max_pages={max_pages}",
    }


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


TASK_PICK = "id,name,status.status,assignees.*.username,due_date,url,list.name,space.id,priority.priority"
COMMENT_PICK = "id,comment_text,user.username,date,resolved"


def get_tasks(scope: str, *, query: dict | None = None, max_pages: int = 50) -> dict:
    """Collect tasks from a list or team scope.

    ``scope`` is the URL fragment after the base, e.g. ``list/12345`` or ``team/67890``.
    Returns ``{items, incomplete, reason}`` with pre-selected support fields.
    """
    path = f"{scope.strip('/')}/task"
    raw = _collect_tasks(path, query=query, max_pages=max_pages)
    raw["items"] = [api.pick(t, TASK_PICK) for t in raw["items"]]
    return raw


def get_task(task_id: str) -> dict:
    """Fetch one task by ID and pre-select support-relevant fields."""
    c = _client()
    task = c.get(f"task/{task_id}")
    return api.pick(task, TASK_PICK + ",description,creator.username,date_created,date_updated")


def get_comments(task_id: str) -> list[dict]:
    """Fetch comments for a task (25 most recent, newest first per ClickUp default)."""
    c = _client()
    body = c.get(f"task/{task_id}/comment")
    comments = body.get("comments") or [] if isinstance(body, dict) else []
    return [api.pick(cm, COMMENT_PICK) for cm in comments]


def get_spaces(team_id: str) -> list[dict]:
    """List spaces in a workspace with basic metadata."""
    c = _client()
    body = c.get(f"team/{team_id}/space", query={"archived": "false"})
    spaces = body.get("spaces") or [] if isinstance(body, dict) else []
    return [api.pick(s, "id,name,statuses.*.status,features.due_dates.enabled") for s in spaces]


# ---------------------------------------------------------------------------
# Markdown rendering for the CLI
# ---------------------------------------------------------------------------


def _tasks_to_md(result: dict, *, scope: str) -> str:
    items = result["items"]
    lines = [f"# ClickUp tasks — {scope}", f"Total: {len(items)}"]
    if result["incomplete"]:
        lines.append(f"_Incomplete: {result['reason']}_")
    for t in items:
        status = t.get("status.status") or "—"
        assignees = ", ".join(t.get("assignees.*.username") or []) or "—"
        due = t.get("due_date") or "—"
        url = t.get("url") or ""
        lst = t.get("list.name") or "—"
        name = t.get("name") or t.get("id") or "?"
        lines.append(f"\n## [{name}]({url})" if url else f"\n## {name}")
        lines.append(f"- Status: **{status}** | List: {lst} | Assignees: {assignees} | Due: {due}")
    return "\n".join(lines)


def _task_to_md(t: dict) -> str:
    name = t.get("name") or t.get("id") or "?"
    url = t.get("url") or ""
    lines = [f"# [{name}]({url})" if url else f"# {name}"]
    lines.append(f"- Status: **{t.get('status.status') or '—'}**")
    lines.append(f"- Assignees: {', '.join(t.get('assignees.*.username') or []) or '—'}")
    lines.append(f"- Priority: {t.get('priority.priority') or '—'}")
    lines.append(f"- Due: {t.get('due_date') or '—'}")
    lines.append(f"- List: {t.get('list.name') or '—'}")
    lines.append(f"- Creator: {t.get('creator.username') or '—'}")
    lines.append(f"- Created: {t.get('date_created') or '—'} | Updated: {t.get('date_updated') or '—'}")
    desc = (t.get("description") or "").strip()
    if desc:
        lines.append(f"\n**Description:**\n{desc[:500]}" + (" …" if len(desc) > 500 else ""))
    return "\n".join(lines)


def _comments_to_md(comments: list[dict], *, task_id: str) -> str:
    lines = [f"# Comments — task {task_id}", f"Total: {len(comments)}"]
    for cm in comments:
        user = cm.get("user.username") or "unknown"
        date = cm.get("date") or "—"
        text = (cm.get("comment_text") or "").strip()
        resolved = " _(resolved)_" if cm.get("resolved") else ""
        lines.append(f"\n### {user} — {date}{resolved}")
        lines.append(text[:400] + (" …" if len(text) > 400 else ""))
    return "\n".join(lines)


def _spaces_to_md(spaces: list[dict], *, team_id: str) -> str:
    lines = [f"# ClickUp spaces — workspace {team_id}", f"Total: {len(spaces)}"]
    for s in spaces:
        name = s.get("name") or s.get("id") or "?"
        sid = s.get("id") or "?"
        due = s.get("features.due_dates.enabled")
        # After api.pick, "statuses.*.status" is already a list of status strings
        raw_statuses = s.get("statuses.*.status") or []
        statuses = [str(st) for st in raw_statuses] if isinstance(raw_statuses, list) else []
        lines.append(f"- **{name}** (`{sid}`) | due_dates: {due} | statuses: {', '.join(statuses) or '—'}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m lib.connectors.clickup")
    sub = parser.add_subparsers(dest="cmd", required=True)

    # tasks list/{list_id} | team/{team_id}
    p_tasks = sub.add_parser("tasks", help="list tasks in a list or workspace (paginated)")
    p_tasks.add_argument("scope", help="list/{list_id} or team/{team_id}")
    p_tasks.add_argument("--max-pages", type=int, default=50, help="page cap (default 50 = 5000 tasks)")
    p_tasks.add_argument("--query", action="append", default=[], metavar="K=V",
                         help="extra query params (repeatable), e.g. statuses[]=open")
    p_tasks.add_argument("--json", dest="as_json", action="store_true", help="output raw JSON instead of markdown")

    # task {task_id}
    p_task = sub.add_parser("task", help="get a single task")
    p_task.add_argument("task_id")
    p_task.add_argument("--json", dest="as_json", action="store_true")

    # comments {task_id}
    p_cmts = sub.add_parser("comments", help="get comments for a task (25 most recent)")
    p_cmts.add_argument("task_id")
    p_cmts.add_argument("--json", dest="as_json", action="store_true")

    # spaces {team_id}
    p_spaces = sub.add_parser("spaces", help="list spaces in a workspace")
    p_spaces.add_argument("team_id")
    p_spaces.add_argument("--json", dest="as_json", action="store_true")

    args = parser.parse_args(argv)

    if args.cmd == "tasks":
        extra = dict(kv.split("=", 1) for kv in args.query if "=" in kv)
        result = get_tasks(args.scope, query=extra or None, max_pages=args.max_pages)
        if args.as_json:
            print(json.dumps(result, indent=2, default=str))
        else:
            print(_tasks_to_md(result, scope=args.scope))
        return 0

    if args.cmd == "task":
        t = get_task(args.task_id)
        if args.as_json:
            print(json.dumps(t, indent=2, default=str))
        else:
            print(_task_to_md(t))
        return 0

    if args.cmd == "comments":
        cmts = get_comments(args.task_id)
        if args.as_json:
            print(json.dumps(cmts, indent=2, default=str))
        else:
            print(_comments_to_md(cmts, task_id=args.task_id))
        return 0

    if args.cmd == "spaces":
        spaces = get_spaces(args.team_id)
        if args.as_json:
            print(json.dumps(spaces, indent=2, default=str))
        else:
            print(_spaces_to_md(spaces, team_id=args.team_id))
        return 0

    parser.error("unknown command")
    return 2
