"""Trello read-only support connector.

Force-code triggers fired:
  (c) exotic auth — Trello requires TWO query params on every request: key= (API key, not secret)
      and token= (user token, secret). lib.api's query_param strategy places only ONE credential
      param; a script is required to split "api_key:user_token" from RC_CONN_TRELLO and inject
      both.

  (a) field pre-selection — raw Trello card/board objects are large; the connector pre-selects the
      support-relevant subset so context stays tight.

  (b) multi-call join — the board summary joins boards → lists → cards (counts) → recent actions
      in one readable markdown block.

Credential format in RC_CONN_TRELLO: "<api_key>:<user_token>"  (colon-separated).

CLI:
    python -m lib.connectors.trello board <BOARD_ID>
    python -m lib.connectors.trello member-cards <BOARD_ID> <MEMBER_ID_OR_USERNAME>
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from lib import api, oauth

API_BASE = "https://api.trello.com/1"

# Load the manifest so `python -m lib.api get trello ...` is registered; auth.strategy=none so the
# generic CLI doesn't try to inject a credential (the script handles dual-param auth itself).
_manifest_path = Path(__file__).resolve().parent / "manifest.yaml"
MANIFEST = api.register(api._parse_manifest_file(_manifest_path))


def _creds() -> tuple[str, str]:
    """Split RC_CONN_TRELLO ("api_key:user_token") into (api_key, user_token).

    Raises loudly when absent or malformed — never falls back to anonymous calls.
    """
    raw = oauth.token("trello")
    api_key, sep, user_token = raw.partition(":")
    if not sep or not api_key or not user_token:
        raise RuntimeError(
            "RC_CONN_TRELLO must be formatted as 'api_key:user_token' (colon-separated); "
            "got a value without a ':' separator"
        )
    return api_key, user_token


def _client(api_key: str, user_token: str) -> api.Client:
    """Build a lib.api Client with auth.strategy=none; we inject key+token into every query
    manually via the base_query argument passed to each call."""
    return api.Client(manifest=MANIFEST, credential="")


def _auth_params(api_key: str, user_token: str) -> dict[str, str]:
    """The two query params Trello expects on every request."""
    return {"key": api_key, "token": user_token}


def get_board(board_id: str, *, api_key: str, user_token: str) -> dict[str, Any]:
    """Fetch board metadata."""
    c = _client(api_key, user_token)
    return c.get(f"boards/{board_id}", query=_auth_params(api_key, user_token))


def get_lists(board_id: str, *, api_key: str, user_token: str) -> list[dict]:
    """Fetch open lists on a board."""
    c = _client(api_key, user_token)
    q = dict(_auth_params(api_key, user_token), filter="open")
    result = c.get(f"boards/{board_id}/lists", query=q)
    return result if isinstance(result, list) else []


def get_cards(board_id: str, *, api_key: str, user_token: str, limit: int = 50) -> list[dict]:
    """Fetch open cards on a board (capped at limit)."""
    c = _client(api_key, user_token)
    q = dict(_auth_params(api_key, user_token), limit=limit)
    result = c.get(f"boards/{board_id}/cards/open", query=q)
    return result if isinstance(result, list) else []


def get_actions(board_id: str, *, api_key: str, user_token: str, limit: int = 25) -> list[dict]:
    """Fetch recent board actions (createCard, commentCard, updateCard) for activity history."""
    c = _client(api_key, user_token)
    q = dict(
        _auth_params(api_key, user_token),
        filter="createCard,commentCard,updateCard,addMemberToCard",
        limit=limit,
    )
    result = c.get(f"boards/{board_id}/actions", query=q)
    return result if isinstance(result, list) else []


def get_member_cards(board_id: str, member_ref: str, *, api_key: str, user_token: str) -> list[dict]:
    """Fetch open cards assigned to a member (by Trello member id or username) on a board."""
    c = _client(api_key, user_token)
    # Resolve member if needed: Trello accepts a username or id directly in the path.
    q = dict(_auth_params(api_key, user_token), limit=50)
    result = c.get(f"members/{member_ref}/cards", query=q)
    cards = result if isinstance(result, list) else []
    # Filter to the requested board — the endpoint returns cards across all boards.
    return [card for card in cards if card.get("idBoard") == board_id]


# ---------------------------------------------------------------------------
# Field pre-selection (trigger a)
# ---------------------------------------------------------------------------

_CARD_FIELDS = "id,name,desc,shortUrl,idList,due,dueComplete,closed,dateLastActivity,idMembers"
_ACTION_FIELDS = "id,type,date,data.text,data.card.name,data.card.id,memberCreator.username"


def _pick_card(card: dict) -> dict:
    return api.pick(card, _CARD_FIELDS)


def _pick_action(action: dict) -> dict:
    return api.pick(action, _ACTION_FIELDS)


# ---------------------------------------------------------------------------
# Multi-call join: board summary (trigger b)
# ---------------------------------------------------------------------------


def board_summary(board_id: str) -> dict:
    """Join board → lists → cards → recent actions into a compact support-ready dict.

    Multi-call: GET board, GET lists, GET cards/open, GET actions. Field pre-selection applied
    before returning so the result is tight.
    """
    api_key, user_token = _creds()
    board = get_board(board_id, api_key=api_key, user_token=user_token)
    lists = get_lists(board_id, api_key=api_key, user_token=user_token)
    cards = get_cards(board_id, api_key=api_key, user_token=user_token, limit=100)
    actions = get_actions(board_id, api_key=api_key, user_token=user_token, limit=25)

    # Build cards-per-list count index for the summary.
    counts: dict[str, int] = {}
    for card in cards:
        lid = card.get("idList", "")
        counts[lid] = counts.get(lid, 0) + 1

    return {
        "board": api.pick(board, "id,name,url,closed,dateLastActivity"),
        "lists": [
            {"id": lst["id"], "name": lst["name"], "open_cards": counts.get(lst["id"], 0)}
            for lst in lists
        ],
        "total_open_cards": len(cards),
        "recent_actions": [_pick_action(a) for a in actions],
    }


def member_cards_on_board(board_id: str, member_ref: str) -> list[dict]:
    """Return support-field-picked open cards for a member on a given board."""
    api_key, user_token = _creds()
    cards = get_member_cards(board_id, member_ref, api_key=api_key, user_token=user_token)
    return [_pick_card(c) for c in cards]


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------


def board_summary_to_markdown(s: dict) -> str:
    b = s["board"]
    lines = [f"# Trello Board: {b.get('name', b.get('id', ''))}"]
    lines.append(f"- URL: {b.get('url', '')}")
    lines.append(f"- Last activity: {b.get('dateLastActivity', 'unknown')}")
    if b.get("closed"):
        lines.append("- **Archived**")
    lines.append(f"- Total open cards: {s['total_open_cards']}")

    lines.append("\n## Lists")
    for lst in s["lists"]:
        lines.append(f"- {lst['name']}: {lst['open_cards']} open card(s)")

    lines.append("\n## Recent activity")
    for act in s["recent_actions"]:
        who = act.get("memberCreator.username", "?")
        when = (act.get("date") or "")[:10]
        atype = act.get("type", "")
        card_name = act.get("data.card.name", "")
        comment = act.get("data.text", "")
        detail = comment or card_name
        lines.append(f"- [{when}] {who} — {atype}" + (f": {detail[:80]}" if detail else ""))

    return "\n".join(lines)


def member_cards_to_markdown(member_ref: str, cards: list[dict]) -> str:
    lines = [f"# Trello cards for {member_ref}"]
    if not cards:
        lines.append("_No open cards assigned to this member on the board._")
        return "\n".join(lines)
    for card in cards:
        name = card.get("name", "")
        url = card.get("shortUrl", "")
        due = card.get("due") or ""
        done = card.get("dueComplete")
        due_str = f" (due {due[:10]}{'✓' if done else ''})" if due else ""
        lines.append(f"- [{name}]({url}){due_str}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m lib.connectors.trello")
    sub = parser.add_subparsers(dest="cmd", required=True)

    brd = sub.add_parser("board", help="render board summary (lists + cards + recent activity)")
    brd.add_argument("board_id", help="Trello board id or short link")

    mc = sub.add_parser("member-cards", help="list open cards for a member on a board")
    mc.add_argument("board_id", help="Trello board id")
    mc.add_argument("member_ref", help="Trello member id or username")

    args = parser.parse_args(argv)

    if args.cmd == "board":
        s = board_summary(args.board_id)
        print(board_summary_to_markdown(s))
        return 0
    if args.cmd == "member-cards":
        cards = member_cards_on_board(args.board_id, args.member_ref)
        print(member_cards_to_markdown(args.member_ref, cards))
        return 0

    parser.error("unknown command")
    return 2
