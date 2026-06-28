"""Podio connector — thin script connector over ``lib.api``.

Force-code trigger (c): Podio requires ``Authorization: OAuth2 <token>`` (not the standard
``Bearer`` prefix that lib.api's bearer strategy emits). This connector owns the auth-header
injection so callers never hand-roll it.

Read-only: only GETs. The CLI renders concise markdown for the most common support lookups:
organizations, spaces, apps, items, tasks.

CLI:
    python -m lib.connectors.podio orgs
    python -m lib.connectors.podio spaces --org-id 12345
    python -m lib.connectors.podio apps --space-id 67890
    python -m lib.connectors.podio items --app-id 11111 [--limit 50]
    python -m lib.connectors.podio item --item-id 99999
    python -m lib.connectors.podio tasks [--space-id 67890]
"""

from __future__ import annotations

import argparse
from typing import Any

from lib import api, oauth

BASE_URL = "https://api.podio.com"

# Default page size for list calls; Podio allows up to 100 for /item/app/{id}/ GETs.
_PAGE_SIZE = 30


def _client() -> api.Client:
    """Build a lib.api Client with Podio's non-standard ``Authorization: OAuth2 <token>`` header.

    The credential is injected as ``RC_CONN_PODIO`` by the host. The manifest sets
    ``auth.strategy = none`` so lib.api's _apply_auth is a no-op; we own the placement.
    """
    token = oauth.token("podio")
    manifest = api.Manifest(
        key="podio",
        base_url=BASE_URL,
        auth=api.Auth(strategy="none"),
        pagination=api.Pagination(
            style="offset",
            offset_param="offset",
            limit_param="limit",
            items_field="items",
            page_size=_PAGE_SIZE,
        ),
        rate_limit_remaining_header="",
        default_headers={"Authorization": "OAuth2 " + token},
    )
    return api.Client(manifest=manifest, credential="")


# ---------------------------------------------------------------------------
# Data-access helpers (read-only GETs)
# ---------------------------------------------------------------------------


def get_orgs() -> list[dict]:
    """List all organizations the token has access to."""
    c = _client()
    result = c.get("/org/")
    if isinstance(result, list):
        return result
    return result.get("items", result) if isinstance(result, dict) else []


def get_spaces(org_id: int | str) -> list[dict]:
    """List all spaces (workspaces) in an organization."""
    c = _client()
    result = c.get(f"/org/{org_id}/all_spaces/")
    if isinstance(result, list):
        return result
    return result if isinstance(result, list) else []


def get_apps(space_id: int | str) -> list[dict]:
    """List all apps in a space."""
    c = _client()
    result = c.get(f"/app/space/{space_id}/")
    if isinstance(result, list):
        return result
    return []


def get_items(app_id: int | str, *, limit: int = _PAGE_SIZE) -> dict[str, Any]:
    """GET items from an app — offset-paginated, up to ``limit`` items total."""
    c = _client()
    return c.collect(f"/item/app/{app_id}/", query={"limit": min(limit, 100)}, max_items=limit)


def get_item(item_id: int | str) -> dict:
    """GET a single item by id."""
    c = _client()
    return c.get(f"/item/{item_id}/")


def get_tasks(*, space_id: int | str | None = None, limit: int = _PAGE_SIZE) -> dict[str, Any]:
    """List tasks — optionally scoped to a space.

    Podio's /task/ endpoints return a bare JSON array (not the {total, items} envelope used by
    /item/app/{id}/), so we fetch the page directly and normalise to the collect() result shape.
    """
    c = _client()
    if space_id is not None:
        path = f"/task/space/{space_id}/"
    else:
        path = "/task/"
    body = c.get(path, query={"limit": min(limit, 100)})
    # Normalise: bare list or an envelope both end up as {"items": [...], "incomplete": False, ...}
    if isinstance(body, list):
        items = body[:limit]
        return {"items": items, "incomplete": len(body) > limit, "reason": ""}
    # If it IS an envelope (future-proofing), fall through to collect.
    items = body.get("items", [])[:limit]
    return {"items": items, "incomplete": len(body.get("items", [])) > limit, "reason": ""}


# ---------------------------------------------------------------------------
# Markdown renderers
# ---------------------------------------------------------------------------


def _orgs_md(orgs: list[dict]) -> str:
    if not orgs:
        return "# Podio organizations\n(none found)"
    lines = ["# Podio organizations\n"]
    for o in orgs:
        lines.append(f"- **{o.get('name', '?')}** (id `{o.get('org_id', '?')}`) — {o.get('url', '')}")
    return "\n".join(lines)


def _spaces_md(spaces: list[dict], org_id: Any) -> str:
    if not spaces:
        return f"# Podio spaces (org {org_id})\n(none found)"
    lines = [f"# Podio spaces (org {org_id})\n"]
    for s in spaces:
        status = s.get("status", "")
        lines.append(f"- **{s.get('name', '?')}** (id `{s.get('space_id', '?')}`) — {status}")
    return "\n".join(lines)


def _apps_md(apps: list[dict], space_id: Any) -> str:
    if not apps:
        return f"# Podio apps (space {space_id})\n(none found)"
    lines = [f"# Podio apps (space {space_id})\n"]
    for a in apps:
        lines.append(f"- **{a.get('config', {}).get('name') or a.get('name', '?')}** (app_id `{a.get('app_id', '?')}`)")
    return "\n".join(lines)


def _items_md(result: dict, app_id: Any) -> str:
    items = result.get("items") or []
    incomplete = result.get("incomplete", False)
    lines = [f"# Podio items (app {app_id})\n"]
    if not items:
        lines.append("(no items)")
    for it in items:
        fields_summary = ", ".join(
            f"{f.get('label', '?')}: {_field_value(f)}"
            for f in (it.get("fields") or [])[:4]
        )
        lines.append(f"- **{it.get('title', '?')}** (id `{it.get('item_id', '?')}`) — {fields_summary}")
    if incomplete:
        lines.append(f"\n_(partial: {result.get('reason', 'more items exist')})_")
    return "\n".join(lines)


def _item_md(item: dict) -> str:
    lines = [f"# Podio item {item.get('item_id', '?')}: {item.get('title', '?')}\n"]
    lines.append(f"- Link: {item.get('link', '—')}")
    fields = item.get("fields") or []
    if fields:
        lines.append("\n## Fields")
        for f in fields:
            lines.append(f"- **{f.get('label', f.get('field_id', '?'))}**: {_field_value(f)}")
    rev = item.get("current_revision") or {}
    if rev.get("created_by"):
        lines.append(f"\n_Last updated by {rev['created_by'].get('name', '?')} on {rev.get('created_on', '?')}_")
    return "\n".join(lines)


def _tasks_md(result: dict) -> str:
    tasks = result.get("items") or []
    lines = ["# Podio tasks\n"]
    if not tasks:
        lines.append("(no tasks)")
    for t in tasks:
        done = " ~~(completed)~~" if t.get("status") == "completed" else ""
        due = f" — due {t['due_date']}" if t.get("due_date") else ""
        lines.append(f"- **{t.get('text', '?')}** (id `{t.get('task_id', '?')}`){done}{due}")
    if result.get("incomplete"):
        lines.append(f"\n_(partial: {result.get('reason', 'more tasks exist')})_")
    return "\n".join(lines)


def _field_value(f: dict) -> str:
    """Extract a readable string from a Podio field values list."""
    values = f.get("values") or []
    if not values:
        return "—"
    first = values[0]
    if isinstance(first, dict):
        # text / number / date fields wrap the value under a key
        for key in ("value", "start", "text"):
            if key in first:
                return str(first[key])
        # app-reference fields carry a nested item
        if "value" in first:
            return str(first["value"])
        return str(list(first.values())[0]) if first else "—"
    return str(first)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m lib.connectors.podio",
        description="Podio read-only grounding connector — renders concise markdown.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("orgs", help="list all organizations")

    sp = sub.add_parser("spaces", help="list spaces in an organization")
    sp.add_argument("--org-id", required=True, help="Podio organization id")

    ap = sub.add_parser("apps", help="list apps in a space")
    ap.add_argument("--space-id", required=True, help="Podio space id")

    ip = sub.add_parser("items", help="list items in an app")
    ip.add_argument("--app-id", required=True, help="Podio app id")
    ip.add_argument("--limit", type=int, default=_PAGE_SIZE, help="max items to return (default 30)")

    itp = sub.add_parser("item", help="get a single item by id")
    itp.add_argument("--item-id", required=True, help="Podio item id")

    tp = sub.add_parser("tasks", help="list tasks")
    tp.add_argument("--space-id", default=None, help="Podio space id (optional; scopes to the space)")
    tp.add_argument("--limit", type=int, default=_PAGE_SIZE, help="max tasks to return (default 30)")

    args = parser.parse_args(argv)

    if args.cmd == "orgs":
        print(_orgs_md(get_orgs()))
    elif args.cmd == "spaces":
        print(_spaces_md(get_spaces(args.org_id), args.org_id))
    elif args.cmd == "apps":
        print(_apps_md(get_apps(args.space_id), args.space_id))
    elif args.cmd == "items":
        print(_items_md(get_items(args.app_id, limit=args.limit), args.app_id))
    elif args.cmd == "item":
        print(_item_md(get_item(args.item_id)))
    elif args.cmd == "tasks":
        print(_tasks_md(get_tasks(space_id=args.space_id, limit=args.limit)))
    else:
        parser.error("unknown command")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
