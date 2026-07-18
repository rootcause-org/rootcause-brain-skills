"""Linear support connector — GraphQL transport over ``lib.api``'s credential + retry layer.

Force-code triggers that justify this script:
  (c) Exotic transport — Linear is GraphQL-ONLY (POST https://api.linear.app/graphql).
      lib.api is a GET/REST client; a GraphQL POST needs a script.
  (a) Field pre-selection — raw GQL connection objects are huge; the connector projects
      to 4-6 support-relevant fields and prints compact markdown.
  (d) Non-standard pagination — Relay-style (pageInfo.endCursor + hasNextPage nested
      inside a connection), which lib.api's cursor_field/has_more_field mechanism
      can't navigate through the GQL response envelope.

The connector imports ``lib.api`` for credential resolution and retry machinery (it never
re-implements those), but drives HTTP directly via ``requests`` for the GQL POST.

CLI:
    python -m lib.connectors.linear issues [--assignee EMAIL_OR_ID] [--team SLUG] [--limit N]
    python -m lib.connectors.linear issue IDENTIFIER          # e.g. ENG-123
    python -m lib.connectors.linear search QUERY [--limit N]
    python -m lib.connectors.linear teams
    python -m lib.connectors.linear projects [--team SLUG]
"""

from __future__ import annotations

import argparse
import random
import time
from typing import Any

import requests

from lib import _http_audit, api, oauth

GRAPHQL_URL = "https://api.linear.app/graphql"
DEFAULT_PAGE_SIZE = 50
MAX_PAGES = 20  # safety cap per paginated call (~1 000 items)

# Re-use lib.api's timing constants for retry/backoff (same policy, same layer).
_MAX_RETRIES = api.DEFAULT_MAX_RETRIES
_BACKOFF_BASE = api.DEFAULT_BACKOFF_BASE
_BACKOFF_CAP = api.DEFAULT_BACKOFF_CAP
_CONNECT_TIMEOUT = api.DEFAULT_CONNECT_TIMEOUT
_READ_TIMEOUT = api.DEFAULT_READ_TIMEOUT


# ---------------------------------------------------------------------------
# Internal HTTP layer — one GQL POST with lib.api-compatible retry/backoff
# ---------------------------------------------------------------------------


def _bearer() -> str:
    """Resolve the credential from the environment via lib.oauth.token."""
    return oauth.token("linear")


def _gql(query: str, variables: dict | None = None, *, bearer: str | None = None) -> dict:
    """POST one GraphQL request with retry/backoff matching lib.api's policy.

    Raises ``api.ApiError`` on non-2xx (same posture as lib.api). GQL application-level
    errors (``data`` absent, ``errors`` present) also raise ``api.ApiError`` so callers
    never see a silent partial.
    """
    token = bearer or _bearer()
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    payload = {"query": query}
    if variables:
        payload["variables"] = variables

    rng = random.Random()
    attempt = 0
    reason = "initial"
    while True:
        try:
            resp = _http_audit.request(
                "POST",
                GRAPHQL_URL,
                headers=headers,
                json_body=payload,
                timeout=(_CONNECT_TIMEOUT, _READ_TIMEOUT),
                attempt=attempt + 1,
                reason=reason,
                endpoint_template="/graphql",
                known_secrets=(token,),
            )
        except requests.RequestException as exc:
            raise api.ApiError(0, str(exc), url=GRAPHQL_URL) from exc

        if resp.status_code == 429:
            # Honour Retry-After; fall through to retry logic below.
            delay_raw = resp.headers.get("Retry-After")
            delay = api.parse_retry_after(delay_raw)
            if delay is None:
                delay = api.DEFAULT_BACKOFF_CAP
            if attempt < _MAX_RETRIES:
                time.sleep(min(delay, api.MAX_RETRY_AFTER))
                reason = "retry_status_429"
                attempt += 1
                continue
            raise api.ApiError(429, resp.text, url=GRAPHQL_URL, retryable=True)

        if resp.status_code in {500, 502, 503, 504} and attempt < _MAX_RETRIES:
            sleep = _jitter(attempt, rng)
            time.sleep(sleep)
            reason = f"retry_status_{resp.status_code}"
            attempt += 1
            continue

        if not (200 <= resp.status_code < 300):
            raise api.ApiError(resp.status_code, resp.text, url=GRAPHQL_URL)

        try:
            body = resp.json()
        except ValueError:
            raise api.ApiError(resp.status_code, f"non-JSON body: {resp.text[:200]}", url=GRAPHQL_URL)

        if "errors" in body and body["errors"]:
            msg = "; ".join(e.get("message", str(e)) for e in body["errors"])
            raise api.ApiError(200, f"GraphQL errors: {msg}", url=GRAPHQL_URL)

        if "data" not in body:
            raise api.ApiError(200, f"unexpected GQL response (no data): {str(body)[:400]}", url=GRAPHQL_URL)

        return body["data"]


def _jitter(attempt: int, rng: random.Random) -> float:
    ceiling = min(_BACKOFF_CAP, _BACKOFF_BASE * (2 ** attempt))
    return rng.uniform(0, ceiling)


# ---------------------------------------------------------------------------
# Pagination helper
# ---------------------------------------------------------------------------


def _paginate_connection(
    query_template: str,
    variables: dict,
    *,
    connection_path: str,
    limit: int = DEFAULT_PAGE_SIZE,
    max_pages: int = MAX_PAGES,
) -> list[dict]:
    """Drive Relay-style pagination for one GQL connection.

    ``connection_path`` is a dot-separated path into the GQL response data to the connection
    object (e.g. ``"issues"`` or ``"team.issues"``). Each page the connector requests
    ``pageInfo { hasNextPage endCursor }`` and ``nodes { ... }``; this function extracts them,
    follows the cursor, and returns the accumulated ``nodes`` list.
    """
    nodes: list[dict] = []
    after: str | None = None
    pages = 0

    while pages < max_pages:
        v = dict(variables, first=limit, after=after)
        data = _gql(query_template, v)

        # Navigate the dot-separated connection_path.
        conn: Any = data
        for seg in connection_path.split("."):
            if not isinstance(conn, dict):
                raise api.ApiError(200, f"unexpected GQL shape at {connection_path!r}: {str(data)[:200]}", url=GRAPHQL_URL)
            conn = conn.get(seg)
            if conn is None:
                return nodes  # empty connection is valid

        page_nodes = conn.get("nodes") or []
        nodes.extend(page_nodes)
        pages += 1

        page_info = conn.get("pageInfo") or {}
        if not page_info.get("hasNextPage"):
            break
        after = page_info.get("endCursor")
        if not after:
            break

    return nodes


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------

_ISSUES_QUERY = """
query Issues($filter: IssueFilter, $first: Int, $after: String) {
  issues(filter: $filter, first: $first, after: $after, orderBy: updatedAt) {
    nodes {
      identifier
      title
      state { name type }
      priority
      priorityLabel
      assignee { name email }
      team { name key }
      project { name }
      labels { nodes { name } }
      url
      createdAt
      updatedAt
    }
    pageInfo { hasNextPage endCursor }
  }
}
"""

_ISSUE_BY_IDENTIFIER_QUERY = """
query IssueByIdentifier($id: String!) {
  issue(id: $id) {
    identifier
    title
    description
    state { name type }
    priority
    priorityLabel
    assignee { name email }
    creator { name email }
    team { name key }
    project { name }
    labels { nodes { name } }
    comments { nodes { body createdAt user { name } } }
    url
    createdAt
    updatedAt
  }
}
"""

_SEARCH_QUERY = """
query Search($term: String!, $first: Int, $after: String) {
  issueSearch(query: $term, first: $first, after: $after, orderBy: updatedAt) {
    nodes {
      identifier
      title
      state { name type }
      priority
      priorityLabel
      assignee { name email }
      team { name key }
      url
      updatedAt
    }
    pageInfo { hasNextPage endCursor }
  }
}
"""

_TEAMS_QUERY = """
query Teams($first: Int, $after: String) {
  teams(first: $first, after: $after) {
    nodes {
      id
      key
      name
      description
    }
    pageInfo { hasNextPage endCursor }
  }
}
"""

_PROJECTS_QUERY = """
query Projects($filter: ProjectFilter, $first: Int, $after: String) {
  projects(filter: $filter, first: $first, after: $after) {
    nodes {
      id
      name
      state
      description
      url
      startDate
      targetDate
      teams { nodes { key name } }
    }
    pageInfo { hasNextPage endCursor }
  }
}
"""

_USER_BY_EMAIL_QUERY = """
query UserByEmail($email: String!) {
  users(filter: { email: { eq: $email } }, first: 1) {
    nodes { id name email }
  }
}
"""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def fetch_issues(
    *,
    assignee: str | None = None,
    team: str | None = None,
    limit: int = DEFAULT_PAGE_SIZE,
) -> list[dict]:
    """Fetch open issues, optionally filtered by assignee (email or id) and/or team key."""
    filter_obj: dict[str, Any] = {}

    if team:
        filter_obj["team"] = {"key": {"eq": team.upper()}}

    if assignee:
        # If it looks like an email, resolve it to a user id first.
        if "@" in assignee:
            uid = _resolve_user_id(assignee)
            if uid:
                filter_obj["assignee"] = {"id": {"eq": uid}}
            else:
                # Fall back to email filter directly (works when display-email matches).
                filter_obj["assignee"] = {"email": {"eq": assignee}}
        else:
            filter_obj["assignee"] = {"id": {"eq": assignee}}

    variables: dict[str, Any] = {}
    if filter_obj:
        variables["filter"] = filter_obj

    return _paginate_connection(
        _ISSUES_QUERY,
        variables,
        connection_path="issues",
        limit=min(limit, DEFAULT_PAGE_SIZE),
        max_pages=max(1, (limit + DEFAULT_PAGE_SIZE - 1) // DEFAULT_PAGE_SIZE),
    )[:limit]


def fetch_issue(identifier: str) -> dict | None:
    """Fetch a single issue by its human identifier (e.g. ``ENG-123``) including comments."""
    data = _gql(_ISSUE_BY_IDENTIFIER_QUERY, {"id": identifier})
    return data.get("issue")


def search_issues(term: str, *, limit: int = DEFAULT_PAGE_SIZE) -> list[dict]:
    """Full-text search across issues."""
    return _paginate_connection(
        _SEARCH_QUERY,
        {"term": term},
        connection_path="issueSearch",
        limit=min(limit, DEFAULT_PAGE_SIZE),
        max_pages=max(1, (limit + DEFAULT_PAGE_SIZE - 1) // DEFAULT_PAGE_SIZE),
    )[:limit]


def fetch_teams() -> list[dict]:
    """List all teams in the workspace."""
    return _paginate_connection(
        _TEAMS_QUERY,
        {},
        connection_path="teams",
        limit=DEFAULT_PAGE_SIZE,
        max_pages=MAX_PAGES,
    )


def fetch_projects(*, team: str | None = None) -> list[dict]:
    """List projects, optionally filtered to one team by key."""
    variables: dict[str, Any] = {}
    if team:
        variables["filter"] = {"accessibleTeams": {"some": {"key": {"eq": team.upper()}}}}
    return _paginate_connection(
        _PROJECTS_QUERY,
        variables,
        connection_path="projects",
        limit=DEFAULT_PAGE_SIZE,
        max_pages=MAX_PAGES,
    )


def _resolve_user_id(email: str) -> str | None:
    data = _gql(_USER_BY_EMAIL_QUERY, {"email": email})
    nodes = (data.get("users") or {}).get("nodes") or []
    return nodes[0]["id"] if nodes else None


# ---------------------------------------------------------------------------
# Markdown renderers (compact grounding output)
# ---------------------------------------------------------------------------


def _priority_label(issue: dict) -> str:
    return issue.get("priorityLabel") or str(issue.get("priority", ""))


def _state_label(issue: dict) -> str:
    state = issue.get("state") or {}
    return state.get("name") or state.get("type") or "unknown"


def issues_to_markdown(issues: list[dict], *, heading: str = "Linear Issues") -> str:
    if not issues:
        return f"# {heading}\n\nNo issues found."
    lines = [f"# {heading}", ""]
    for iss in issues:
        assignee = (iss.get("assignee") or {}).get("name") or "unassigned"
        team_key = (iss.get("team") or {}).get("key") or ""
        label_names = [n["name"] for n in (iss.get("labels") or {}).get("nodes", [])]
        labels_str = f" [{', '.join(label_names)}]" if label_names else ""
        lines.append(
            f"- **{iss.get('identifier')}** {iss.get('title')} "
            f"[{_state_label(iss)}]{labels_str} — {assignee} | {team_key} | {_priority_label(iss)}"
        )
        lines.append(f"  {iss.get('url', '')}")
    return "\n".join(lines)


def issue_to_markdown(iss: dict | None, identifier: str = "") -> str:
    if iss is None:
        return f"# Linear issue not found\nNo issue matched `{identifier}`."
    assignee = (iss.get("assignee") or {}).get("name") or "unassigned"
    creator = (iss.get("creator") or {}).get("name") or "unknown"
    team = (iss.get("team") or {}).get("name") or ""
    project = (iss.get("project") or {}).get("name") or ""
    label_names = [n["name"] for n in (iss.get("labels") or {}).get("nodes", [])]
    lines = [
        f"# Linear: {iss.get('identifier')} — {iss.get('title')}",
        f"- State: **{_state_label(iss)}**",
        f"- Priority: {_priority_label(iss)}",
        f"- Assignee: {assignee} | Creator: {creator}",
        f"- Team: {team}" + (f" | Project: {project}" if project else ""),
    ]
    if label_names:
        lines.append(f"- Labels: {', '.join(label_names)}")
    lines.append(f"- URL: {iss.get('url', '')}")
    desc = (iss.get("description") or "").strip()
    if desc:
        lines.append(f"\n## Description\n{desc[:800]}" + ("…" if len(desc) > 800 else ""))
    comments = (iss.get("comments") or {}).get("nodes") or []
    if comments:
        lines.append("\n## Comments")
        for c in comments[:10]:
            author = (c.get("user") or {}).get("name") or "?"
            body = (c.get("body") or "").strip()[:400]
            lines.append(f"- **{author}**: {body}")
    return "\n".join(lines)


def teams_to_markdown(teams: list[dict]) -> str:
    if not teams:
        return "# Linear Teams\n\nNo teams found."
    lines = ["# Linear Teams", ""]
    for t in teams:
        desc = (t.get("description") or "").strip()
        lines.append(f"- **{t.get('key')}** — {t.get('name')}" + (f": {desc}" if desc else ""))
    return "\n".join(lines)


def projects_to_markdown(projects: list[dict]) -> str:
    if not projects:
        return "# Linear Projects\n\nNo projects found."
    lines = ["# Linear Projects", ""]
    for p in projects:
        team_keys = ", ".join(t["key"] for t in (p.get("teams") or {}).get("nodes", []))
        target = p.get("targetDate") or ""
        lines.append(
            f"- **{p.get('name')}** [{p.get('state', '')}]"
            + (f" due {target}" if target else "")
            + (f" | teams: {team_keys}" if team_keys else "")
        )
        if p.get("url"):
            lines.append(f"  {p['url']}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m lib.connectors.linear",
        description="Linear read-only connector — prints grounding markdown.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    # issues
    p_issues = sub.add_parser("issues", help="List issues (optionally filtered)")
    p_issues.add_argument("--assignee", default=None, help="Filter by assignee email or Linear user id")
    p_issues.add_argument("--team", default=None, help="Filter by team key (e.g. ENG)")
    p_issues.add_argument("--limit", type=int, default=DEFAULT_PAGE_SIZE)

    # issue (single)
    p_issue = sub.add_parser("issue", help="Fetch a single issue by identifier (e.g. ENG-123)")
    p_issue.add_argument("identifier", help="Issue identifier")

    # search
    p_search = sub.add_parser("search", help="Full-text search across issues")
    p_search.add_argument("query", help="Search text")
    p_search.add_argument("--limit", type=int, default=DEFAULT_PAGE_SIZE)

    # teams
    sub.add_parser("teams", help="List all teams")

    # projects
    p_projects = sub.add_parser("projects", help="List projects")
    p_projects.add_argument("--team", default=None, help="Filter by team key")

    args = parser.parse_args(argv)

    if args.cmd == "issues":
        result = fetch_issues(assignee=args.assignee, team=args.team, limit=args.limit)
        print(issues_to_markdown(result))
    elif args.cmd == "issue":
        result_single = fetch_issue(args.identifier)
        print(issue_to_markdown(result_single, args.identifier))
    elif args.cmd == "search":
        result_search = search_issues(args.query, limit=args.limit)
        print(issues_to_markdown(result_search, heading=f"Linear Search: {args.query}"))
    elif args.cmd == "teams":
        result_teams = fetch_teams()
        print(teams_to_markdown(result_teams))
    elif args.cmd == "projects":
        result_projects = fetch_projects(team=args.team)
        print(projects_to_markdown(result_projects))
    else:
        parser.error("unknown command")
        return 2

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
