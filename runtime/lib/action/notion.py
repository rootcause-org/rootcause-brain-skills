"""Notion write helpers for hosted actions.

The helpers here are intentionally opinionated: action scripts pass plain Python values, while this
module reads the live Notion data-source schema, validates column names/types/options, and converts
values into Notion page property payloads. Preflights can call the same functions as write bodies, so
the reviewer sees the same clear failure modes the executor would.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from difflib import get_close_matches
from functools import lru_cache
from typing import Any, Mapping

from lib import action, api
from lib.connectors import notion as notion_read


ACTION_HELPER_DOCS = {
    "provider": "notion",
    "need": "Create/update Notion pages, database rows, page text",
    "connection": "notion.write",
    "import": "from lib.action import notion",
    "source_module": "lib.action.notion",
    "manifest": [
        "Declare `connections: [notion.write]`.",
        "For database rows, use `database_id` plus plain `properties`; call `validate_database_values` before create/update.",
        "For row updates, include `record_id`; `update_database_row` verifies it belongs to `database_id` before writing.",
        "For page text, use `page_id`, `old_str`, `new_str`; validate that `old_str` is an exact unique editable-block match.",
    ],
    "common_params": [
        "`database_id`: Notion data-source ID, or a legacy database ID when it resolves to one data source.",
        "`properties`: JSON object of Notion column names to plain values; pass to `validate_database_values`, not raw Notion API JSON.",
        "`record_id`: existing Notion row/page ID for database-row updates.",
        "`page_id`: Notion page ID for text replacement or bookmark/link append.",
        "`old_str` / `new_str`: exact text anchor and replacement text; `old_str` must match exactly one editable block.",
    ],
    "useful_for": [
        "create a Notion database row from plain column values",
        "update an existing Notion database row after checking its parent data source",
        "replace one exact editable text occurrence on a Notion page",
        "append a bookmark/file link to a Notion page",
    ],
    "helpers": {
        "retrieve_data_source": "Resolve and return the live Notion data-source schema.",
        "validate_database_values": "Validate plain column values against the live data-source schema and convert them to Notion property payloads.",
        "database_validation_summary": "Build a reviewer-readable dry-run/preflight summary from validated row values.",
        "create_database_row": "Create one page row in a Notion data source.",
        "update_database_row": "Update one existing row after verifying it belongs to the requested data source.",
        "validate_page_replacement": "Confirm exactly one editable block contains the old text.",
        "page_replacement_summary": "Build a reviewer-readable dry-run/preflight summary for a page-text replacement.",
        "replace_page_text": "Replace the first exact match in one editable block, preserving simple block shape.",
        "create_page": "Create a child page under a page parent.",
        "update_properties": "Patch native Notion page properties when the script intentionally builds the payload.",
        "append_file_link": "Append a bookmark block pointing at an externally hosted file.",
        "find_page_text_matches": "Inspect editable blocks whose plain text contains an exact anchor before deciding whether replacement is safe.",
    },
    "patterns": [
        {
            "title": "Create a database row",
            "code": """
from lib import action
from lib.action import notion

@action.main
def run(p: action.Params) -> dict:
    checked = notion.validate_database_values(p["database_id"], p["properties"])
    if action.dry_run():
        return {"summary": notion.database_validation_summary(checked, operation="Dry run: create row")}

    page = notion.create_database_row(database_id=p["database_id"], values=p["properties"])
    return {"summary": f"Created Notion row **{page.id}**.", "page_id": page.id, "url": page.url}
""",
        },
        {
            "title": "Update a database row",
            "code": """
from lib import action
from lib.action import notion

@action.main
def run(p: action.Params) -> dict:
    checked = notion.validate_database_values(p["database_id"], p["properties"])
    if action.dry_run():
        return {"summary": notion.database_validation_summary(checked, operation="Dry run: update row")}

    page = notion.update_database_row(
        database_id=p["database_id"],
        record_id=p["record_id"],
        values=p["properties"],
    )
    return {"summary": f"Updated Notion row **{page.id}**.", "page_id": page.id, "url": page.url}
""",
        },
        {
            "title": "Replace exact page text",
            "code": """
from lib import action
from lib.action import notion

@action.main
def run(p: action.Params) -> dict:
    match = notion.validate_page_replacement(page_id=p["page_id"], old_str=p["old_str"], new_str=p["new_str"])
    if action.dry_run():
        return {"summary": notion.page_replacement_summary(match)}

    block = notion.replace_page_text(page_id=p["page_id"], old_str=p["old_str"], new_str=p["new_str"])
    return {"summary": f"Updated Notion block **{block.id}**.", "block_id": block.id}
""",
        },
    ],
    "validation_failure": [
        "All helpers use `action.client(\"notion.write\")`; missing `connections: [notion.write]` or a missing project write grant fails before/at execution.",
        "Database helpers read the live data-source schema; unknown columns include available columns and close-name suggestions.",
        "Plain values are coerced by property type: title/rich_text strings, JSON numbers, select/status option names or IDs, multi_select arrays, booleans, email/url/date checks, relation page IDs, people user IDs, files arrays, and wiki verification state.",
        "Native Notion property payloads are also validated before passing through; select/status/multi_select errors list the live valid options.",
        "Read-only/derived/unsupported property types are rejected: formula, rollup, created_by, created_time, last_edited_by, last_edited_time, unique_id, place.",
        "Legacy database IDs resolve only when exactly one data source exists; multi-source databases fail with the available data-source IDs.",
        "Page text replacement fails unless `old_str` appears in exactly one editable rich-text block; no-match errors include nearby editable text hints.",
        "Dry-run patterns call the same validators the write path uses, so reviewer-facing failures match execution failures.",
    ],
    "do_not": [
        "Do not build raw Notion property payloads for database rows when plain values plus `validate_database_values` fit.",
        "Do not skip dry-run validation; use `database_validation_summary` or `page_replacement_summary` so reviewers see the same checks before execution.",
        "Do not guess Notion column names or select/status option labels; validate against the live schema first.",
        "Do not pass a generic database ID when Notion reports multiple data sources; ground the exact data-source ID.",
        "Do not pass `RC_CONN_*` tokens or raw Authorization headers; helpers use `RC_ACTION_*` via `notion.write`.",
        "Do not set formula, rollup, created/edited, unique_id, place, or other read-only/unsupported columns.",
        "Do not use page text replacement for broad rewrites; `old_str` must be an exact unique anchor.",
        "Do not import underscored `lib.connectors.notion` internals. The generated action docs list only the supported `lib.action.notion` write helpers; read-only formatting helpers such as `compact_page` stay connector-side.",
        "Do not import sibling action files or provider SDKs; hosted scripts should use `lib` plus stdlib.",
    ],
}


@dataclass(frozen=True)
class NotionBlock:
    id: str
    raw: dict[str, Any]


@dataclass(frozen=True)
class NotionPage:
    id: str
    url: str
    raw: dict[str, Any]


@dataclass(frozen=True)
class NotionDataSource:
    id: str
    title: str
    properties: dict[str, Any]
    raw: dict[str, Any]


@dataclass(frozen=True)
class DatabaseValidation:
    data_source: NotionDataSource
    properties: dict[str, Any]
    observed: dict[str, Any]


@dataclass(frozen=True)
class PageTextMatch:
    block_id: str
    block_type: str
    plain_text: str
    raw: dict[str, Any]


_READ_ONLY_PROPERTY_TYPES = {
    "button",
    "created_by",
    "created_time",
    "formula",
    "last_edited_by",
    "last_edited_time",
    "rollup",
    "unique_id",
}
_UNSUPPORTED_PROPERTY_TYPES = {"place"}
_VERIFICATION_STATES = ("verified", "unverified", "expired")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@lru_cache(maxsize=1)
def _client():
    return action.client("notion.write", manifest=notion_read.MANIFEST)


def append_file_link(*, page_id: str, title: str, url: str) -> NotionBlock:
    body = {
        "children": [
            {
                "object": "block",
                "type": "bookmark",
                "bookmark": {"caption": _rich_text(title), "url": url},
            }
        ]
    }
    raw = _client().patch(f"blocks/{_notion_id(page_id)}/children", json=body)
    results = raw.get("results") if isinstance(raw, dict) else None
    block = results[0] if results else raw
    return NotionBlock(id=str(block.get("id", "")), raw=block)


def create_page(*, parent_id: str, title: str, properties: dict | None = None) -> NotionPage:
    props = dict(properties or {})
    if not _has_title_property(props):
        props.setdefault("title", {"title": _rich_text(title)})
    body = {"parent": {"page_id": _notion_id(parent_id)}, "properties": props}
    raw = _client().post("pages", json=body)
    return _page(raw)


def update_properties(*, page_id: str, properties: dict) -> NotionPage:
    raw = _client().patch(f"pages/{_notion_id(page_id)}", json={"properties": properties})
    return _page(raw)


def retrieve_data_source(database_id: str) -> NotionDataSource:
    """Return the live Notion data-source schema.

    ``database_id`` may already be a data-source ID. If it is an old database ID, this resolves the
    first data source under that database, unless the database has multiple data sources and needs a
    more specific ID.
    """
    object_id = _notion_id(database_id)
    try:
        raw = _client().get(f"data_sources/{object_id}")
        return _data_source(raw)
    except api.ApiError as e:
        if e.status not in {400, 404}:
            raise

    try:
        db = _client().get(f"databases/{object_id}")
    except api.ApiError as e:
        raise action.ActionError(
            f"Notion data source/database {database_id!r} was not found or is not shared with the integration."
        ) from e
    sources = db.get("data_sources") or []
    if len(sources) != 1:
        labels = [f"{_title_from_any(s) or '(untitled)'} ({s.get('id')})" for s in sources]
        detail = "; ".join(labels) if labels else "none returned"
        raise action.ActionError(
            "That Notion database has multiple or no data sources; pass the exact data source ID. "
            f"Available data sources: {detail}"
        )
    raw = _client().get(f"data_sources/{sources[0]['id']}")
    return _data_source(raw)


def validate_database_values(database_id: str, values: Mapping[str, Any]) -> DatabaseValidation:
    """Validate and convert plain property values for a Notion data-source row."""
    if not isinstance(values, Mapping) or not values:
        raise action.ActionError("properties must be a non-empty JSON object of Notion column names to values")
    ds = retrieve_data_source(database_id)
    errors: list[str] = []
    payload: dict[str, Any] = {}
    for name, value in values.items():
        if not isinstance(name, str) or not name.strip():
            errors.append(f"column name {name!r} is not a non-empty string")
            continue
        schema = ds.properties.get(name)
        if schema is None:
            errors.append(_unknown_property_message(name, ds.properties))
            continue
        try:
            payload[name] = _property_value(name, schema, value)
        except action.ActionError as e:
            errors.append(str(e))
    if errors:
        raise action.ActionError("Notion row values did not match the database schema:\n- " + "\n- ".join(errors))
    observed = {
        "data_source_id": ds.id,
        "data_source_title": ds.title,
        "columns": {name: _property_summary(schema) for name, schema in ds.properties.items()},
        "provided_columns": sorted(values),
    }
    return DatabaseValidation(data_source=ds, properties=payload, observed=observed)


def create_database_row(*, database_id: str, values: Mapping[str, Any]) -> NotionPage:
    checked = validate_database_values(database_id, values)
    raw = _client().post(
        "pages",
        json={"parent": {"data_source_id": checked.data_source.id}, "properties": checked.properties},
    )
    return _page(raw)


def update_database_row(*, database_id: str, record_id: str, values: Mapping[str, Any]) -> NotionPage:
    checked = validate_database_values(database_id, values)
    page_id = _notion_id(record_id)
    _assert_row_parent(page_id, checked.data_source.id)
    raw = _client().patch(f"pages/{page_id}", json={"properties": checked.properties})
    return _page(raw)


def find_page_text_matches(*, page_id: str, old_str: str) -> list[PageTextMatch]:
    needle = _required_nonempty(old_str, "old_str")
    matches = []
    for block in _walk_blocks(_notion_id(page_id)):
        text = _editable_plain_text(block)
        if text and needle in text:
            matches.append(
                PageTextMatch(
                    block_id=str(block.get("id", "")),
                    block_type=str(block.get("type", "")),
                    plain_text=text,
                    raw=block,
                )
            )
    return matches


def validate_page_replacement(*, page_id: str, old_str: str, new_str: str) -> PageTextMatch:
    _required_nonempty(new_str, "new_str")
    matches = find_page_text_matches(page_id=page_id, old_str=old_str)
    if len(matches) == 1:
        return matches[0]
    if not matches:
        suggestions = _replacement_suggestions(page_id=page_id, old_str=old_str)
        hint = "\nNearby editable text to anchor on instead:\n" + suggestions if suggestions else ""
        raise action.ActionError(f"Could not find old_str on the Notion page.{hint}")
    snippets = "\n".join(f"- {m.block_id} ({m.block_type}): {_snippet(m.plain_text)}" for m in matches[:8])
    raise action.ActionError(
        f"old_str appears in {len(matches)} editable Notion blocks; make it unique before replacing.\n{snippets}"
    )


def replace_page_text(*, page_id: str, old_str: str, new_str: str) -> NotionBlock:
    match = validate_page_replacement(page_id=page_id, old_str=old_str, new_str=new_str)
    updated_text = match.plain_text.replace(old_str, new_str, 1)
    first_line, extra_lines = _replacement_lines(updated_text)
    body = {match.block_type: _replacement_block_body(match, first_line)}
    raw = _client().patch(f"blocks/{match.block_id}", json=body)
    if extra_lines:
        parent_id = _block_parent_id(match.raw, fallback_page_id=page_id)
        _append_child_blocks(parent_id=parent_id, after_block_id=match.block_id, lines=extra_lines)
    return NotionBlock(id=str(raw.get("id", match.block_id)), raw=raw)


def database_validation_summary(checked: DatabaseValidation, *, operation: str) -> str:
    cols = ", ".join(checked.observed["provided_columns"])
    title = checked.data_source.title or checked.data_source.id
    return f"{operation} would set **{cols}** on Notion database **{title}**."


def page_replacement_summary(match: PageTextMatch) -> str:
    return f"Found one editable Notion {match.block_type} block to update: `{match.block_id}`."


def _data_source(raw: dict[str, Any]) -> NotionDataSource:
    props = raw.get("properties") or {}
    if not isinstance(props, dict):
        raise action.ActionError("Notion returned a data source without a properties schema")
    return NotionDataSource(
        id=str(raw.get("id", "")),
        title=_title_from_any(raw),
        properties=props,
        raw=raw,
    )


def _assert_row_parent(page_id: str, data_source_id: str) -> None:
    raw = _client().get(f"pages/{page_id}")
    parent = raw.get("parent") or {}
    parent_id = str(parent.get("data_source_id") or parent.get("database_id") or "")
    if parent_id and _strip_uuid(parent_id) != _strip_uuid(data_source_id):
        raise action.ActionError(
            f"record_id {page_id!r} belongs to a different Notion database/data source "
            f"({parent_id}); expected {data_source_id}"
        )


def _property_value(name: str, schema: Mapping[str, Any], value: Any) -> dict[str, Any]:
    ptype = str(schema.get("type") or "")
    if ptype in _READ_ONLY_PROPERTY_TYPES:
        raise action.ActionError(f"column {name!r} is read-only ({ptype}) and cannot be set through the API")
    if ptype in _UNSUPPORTED_PROPERTY_TYPES:
        raise action.ActionError(f"column {name!r} is unsupported by the Notion API ({ptype}); omit it")
    if isinstance(value, Mapping) and ptype in value:
        return {ptype: _coerce_native_property_value(name, schema, value[ptype])}
    if ptype == "title":
        return {"title": _coerce_rich_text(name, value, "title")}
    if ptype == "rich_text":
        return {"rich_text": _coerce_rich_text(name, value, "rich_text")}
    if ptype == "number":
        return {"number": _coerce_number(name, value)}
    if ptype in {"select", "status"}:
        if value is None:
            return {ptype: None}
        return {ptype: _coerce_option(name, schema, value, ptype)}
    if ptype == "multi_select":
        return {"multi_select": _coerce_options_list(name, schema, value)}
    if ptype == "checkbox":
        if not isinstance(value, bool):
            raise action.ActionError(f"column {name!r} is checkbox; provide true or false")
        return {"checkbox": value}
    if ptype == "email":
        return {"email": _coerce_email(name, value)}
    if ptype == "url":
        return {"url": _coerce_url(name, value)}
    if ptype == "phone_number":
        return {"phone_number": _coerce_string(name, value, "phone_number")}
    if ptype == "date":
        return {"date": _coerce_date(name, value)}
    if ptype == "relation":
        return {"relation": _coerce_id_objects(name, value, "relation page IDs")}
    if ptype == "people":
        return {"people": _coerce_id_objects(name, value, "people user IDs")}
    if ptype == "files":
        return {"files": _coerce_files(name, value)}
    if ptype == "verification":
        return {"verification": _coerce_verification(name, value)}
    raise action.ActionError(f"column {name!r} has unsupported Notion property type {ptype!r}")


def _coerce_native_property_value(name: str, schema: Mapping[str, Any], value: Any) -> Any:
    ptype = str(schema.get("type") or "")
    if ptype == "title":
        return _coerce_rich_text(name, value, "title")
    if ptype == "rich_text":
        return _coerce_rich_text(name, value, "rich_text")
    if ptype == "number":
        return _coerce_number(name, value)
    if ptype in {"select", "status"}:
        if value is None:
            return None
        return _coerce_option(name, schema, value, ptype)
    if ptype == "multi_select":
        return _coerce_options_list(name, schema, value)
    if ptype == "checkbox":
        if not isinstance(value, bool):
            raise action.ActionError(f"column {name!r} is checkbox; provide true or false")
        return value
    if ptype == "email":
        return _coerce_email(name, value)
    if ptype == "url":
        return _coerce_url(name, value)
    if ptype == "phone_number":
        return _coerce_string(name, value, "phone_number")
    if ptype == "date":
        return _coerce_date(name, value)
    if ptype == "relation":
        return _coerce_id_objects(name, value, "relation page IDs")
    if ptype == "people":
        return _coerce_id_objects(name, value, "people user IDs")
    if ptype == "files":
        return _coerce_files(name, value)
    if ptype == "verification":
        return _coerce_verification(name, value)
    raise action.ActionError(f"column {name!r} has unsupported Notion property type {ptype!r}")


def _option_catalog(schema: Mapping[str, Any]) -> tuple[dict[str, str], dict[str, str]]:
    ptype = str(schema.get("type") or "")
    options = ((schema.get(ptype) or {}).get("options") or []) if isinstance(schema.get(ptype), Mapping) else []
    by_name: dict[str, str] = {}
    by_id: dict[str, str] = {}
    for option in options:
        if not isinstance(option, Mapping):
            continue
        option_name = option.get("name")
        option_id = option.get("id")
        if isinstance(option_name, str) and option_name:
            by_name[option_name] = str(option_id or "")
        if isinstance(option_id, str) and option_id:
            by_id[option_id] = str(option_name or "")
    return by_name, by_id


def _validate_option(name: str, schema: Mapping[str, Any], value: str, *, key: str) -> None:
    by_name, by_id = _option_catalog(schema)
    if not by_name and not by_id:
        return
    if key == "name" and value not in by_name:
        valid = ", ".join(by_name)
        raise action.ActionError(
            f"column {name!r} is {schema.get('type')}; {value!r} is not valid. Valid values: {valid}"
        )
    if key == "id" and value not in by_id:
        valid = ", ".join(f"{oid} ({label})" if label else oid for oid, label in by_id.items())
        raise action.ActionError(
            f"column {name!r} is {schema.get('type')}; option ID {value!r} is not valid. Valid option IDs: {valid}"
        )


def _unknown_property_message(name: str, properties: Mapping[str, Any]) -> str:
    names = list(properties)
    close = get_close_matches(name, names, n=3, cutoff=0.55)
    suffix = f" Did you mean: {', '.join(close)}?" if close else ""
    return f"column {name!r} does not exist. Available columns: {_schema_summary(properties)}.{suffix}"


def _schema_summary(properties: Mapping[str, Any]) -> str:
    return "; ".join(f"{name} ({_property_summary(schema)})" for name, schema in properties.items())


def _property_summary(schema: Mapping[str, Any]) -> str:
    ptype = str(schema.get("type") or "")
    options = ((schema.get(ptype) or {}).get("options") or []) if isinstance(schema.get(ptype), Mapping) else []
    labels = [str(o.get("name")) for o in options if isinstance(o, Mapping) and o.get("name")]
    if labels:
        return f"{ptype}: {', '.join(labels)}"
    return ptype


def _walk_blocks(block_id: str, *, max_depth: int = 8) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out: list[dict[str, Any]] = []

    def visit(parent_id: str, depth: int) -> None:
        if depth > max_depth:
            return
        for child in _block_children(parent_id):
            cid = str(child.get("id", ""))
            if cid in seen:
                continue
            seen.add(cid)
            out.append(child)
            if child.get("has_children"):
                visit(cid, depth + 1)

    visit(block_id, 0)
    return out


def _block_children(block_id: str) -> list[dict[str, Any]]:
    cursor = None
    results: list[dict[str, Any]] = []
    while True:
        query = {"page_size": 100}
        if cursor:
            query["start_cursor"] = cursor
        page = _client().get(f"blocks/{block_id}/children", query=query)
        results.extend(page.get("results") or [])
        if not page.get("has_more"):
            return results
        cursor = page.get("next_cursor")
        if not cursor:
            return results


def _editable_plain_text(block: Mapping[str, Any]) -> str:
    typ = str(block.get("type") or "")
    body = block.get(typ)
    if not isinstance(body, Mapping):
        return ""
    rich = body.get("rich_text")
    if not isinstance(rich, list):
        return ""
    return notion_read._plain_text(rich)


def _replacement_lines(text: str) -> tuple[str, list[str]]:
    lines = text.split("\n")
    return lines[0], lines[1:]


def _replacement_block_body(match: PageTextMatch, text: str) -> dict[str, Any]:
    existing = match.raw.get(match.block_type)
    body: dict[str, Any] = {}
    if isinstance(existing, Mapping) and match.block_type == "to_do" and isinstance(existing.get("checked"), bool):
        body["checked"] = existing["checked"]
    body["rich_text"] = _rich_text(text)
    return body


def _block_parent_id(block: Mapping[str, Any], *, fallback_page_id: str) -> str:
    parent = block.get("parent")
    if isinstance(parent, Mapping):
        for key in ("page_id", "block_id"):
            if parent.get(key):
                return _notion_id(parent[key])
    return _notion_id(fallback_page_id)


def _append_child_blocks(*, parent_id: str, after_block_id: str, lines: list[str]) -> None:
    children = [_block_from_plain_line(line) for line in lines]
    try:
        _client().patch(f"blocks/{parent_id}/children", json={"after": after_block_id, "children": children})
    except api.ApiError as e:
        if e.status != 400 or "after should be not present" not in str(e):
            raise
        _client().patch(f"blocks/{parent_id}/children", json={"children": children})


def _block_from_plain_line(line: str) -> dict[str, Any]:
    text = line.strip()
    checked = False
    for marker in ("[] ", "[ ] "):
        if text.startswith(marker):
            text = text[len(marker) :].strip()
            return {"object": "block", "type": "to_do", "to_do": {"checked": False, "rich_text": _rich_text(text)}}
    for marker in ("[x] ", "[X] "):
        if text.startswith(marker):
            checked = True
            text = text[len(marker) :].strip()
            return {"object": "block", "type": "to_do", "to_do": {"checked": checked, "rich_text": _rich_text(text)}}
    if text.startswith(("- ", "* ")):
        text = text[2:].strip()
        return {"object": "block", "type": "bulleted_list_item", "bulleted_list_item": {"rich_text": _rich_text(text)}}
    numbered = re.match(r"^\d+[.)]\s+(.+)$", text)
    if numbered:
        return {
            "object": "block",
            "type": "numbered_list_item",
            "numbered_list_item": {"rich_text": _rich_text(numbered.group(1).strip())},
        }
    return {"object": "block", "type": "paragraph", "paragraph": {"rich_text": _rich_text(text)}}


def _replacement_suggestions(*, page_id: str, old_str: str) -> str:
    texts = []
    for block in _walk_blocks(_notion_id(page_id)):
        text = _editable_plain_text(block)
        if text:
            texts.append((str(block.get("id", "")), str(block.get("type", "")), text))
    close = get_close_matches(old_str, [t[2] for t in texts], n=5, cutoff=0.25)
    if close:
        chosen = [t for t in texts if t[2] in close]
    else:
        terms = [w.lower() for w in re.findall(r"\w{4,}", old_str)[:6]]
        chosen = [t for t in texts if any(term in t[2].lower() for term in terms)][:5]
    return "\n".join(f"- {bid} ({typ}): {_snippet(text)}" for bid, typ, text in chosen[:5])


def _page(raw: dict[str, Any]) -> NotionPage:
    return NotionPage(id=str(raw.get("id", "")), url=str(raw.get("url", "")), raw=notion_read.compact_page(raw))


def _rich_text(text: str) -> list[dict[str, Any]]:
    return [{"type": "text", "text": {"content": text}}]


def _coerce_rich_text(name: str, value: Any, label: str) -> list[dict[str, Any]]:
    if isinstance(value, str):
        return _rich_text(value)
    if isinstance(value, list) and all(isinstance(x, Mapping) for x in value):
        return [dict(x) for x in value]
    raise action.ActionError(f"column {name!r} is {label}; provide a string or Notion rich_text array")


def _coerce_number(name: str, value: Any) -> int | float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise action.ActionError(f"column {name!r} is number; provide a JSON number")
    return value


def _coerce_option(name: str, schema: Mapping[str, Any], value: Any, label: str) -> dict[str, str]:
    if isinstance(value, str) and value.strip():
        option_name = value.strip()
        _validate_option(name, schema, option_name, key="name")
        return {"name": option_name}
    if isinstance(value, Mapping) and isinstance(value.get("name"), str) and value["name"].strip():
        option_name = value["name"].strip()
        _validate_option(name, schema, option_name, key="name")
        return {"name": option_name}
    if isinstance(value, Mapping) and isinstance(value.get("id"), str) and value["id"].strip():
        option_id = value["id"].strip()
        _validate_option(name, schema, option_id, key="id")
        return {"id": option_id}
    raise action.ActionError(f"column {name!r} is {label}; provide one option name or option ID")


def _coerce_options_list(name: str, schema: Mapping[str, Any], value: Any) -> list[dict[str, str]]:
    if value is None:
        return []
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, list):
        values = value
    else:
        raise action.ActionError(f"column {name!r} is multi_select; provide a string array")
    return [_coerce_option(name, schema, entry, "multi_select") for entry in values]


def _coerce_string(name: str, value: Any, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise action.ActionError(f"column {name!r} is {label}; provide a string")
    return value


def _coerce_email(name: str, value: Any) -> str | None:
    email = _coerce_string(name, value, "email")
    if email is not None and not _EMAIL_RE.match(email):
        raise action.ActionError(f"column {name!r} is email; {email!r} is not a valid email address")
    return email


def _coerce_url(name: str, value: Any) -> str | None:
    url = _coerce_string(name, value, "url")
    if url is not None and not (url.startswith("http://") or url.startswith("https://")):
        raise action.ActionError(f"column {name!r} is url; provide an http(s) URL")
    return url


def _coerce_string_list(name: str, value: Any, label: str) -> list[str]:
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, list):
        values = value
    else:
        raise action.ActionError(f"column {name!r} is {label}; provide a string array")
    if not all(isinstance(x, str) and x.strip() for x in values):
        raise action.ActionError(f"column {name!r} is {label}; every item must be a non-empty string")
    return [x.strip() for x in values]


def _coerce_id_objects(name: str, value: Any, label: str) -> list[dict[str, str]]:
    if value is None:
        return []
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, list):
        values = value
    else:
        raise action.ActionError(f"column {name!r} is {label}; provide a string array")

    out = []
    for entry in values:
        if isinstance(entry, str) and entry.strip():
            out.append({"id": _notion_id(entry)})
            continue
        if isinstance(entry, Mapping) and isinstance(entry.get("id"), str) and entry["id"].strip():
            out.append({"id": _notion_id(entry["id"])})
            continue
        raise action.ActionError(f"column {name!r} is {label}; every item must be a non-empty ID string")
    return out


def _coerce_files(name: str, value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise action.ActionError(f"column {name!r} is files; provide a Notion files array")
    out = []
    for entry in value:
        if not isinstance(entry, Mapping):
            raise action.ActionError(f"column {name!r} is files; every item must be a Notion file object")
        item = dict(entry)
        external = item.get("external")
        if isinstance(external, Mapping):
            if not isinstance(item.get("name"), str) or not item["name"].strip():
                raise action.ActionError(f"column {name!r} is files; external files require a non-empty name")
            if not external.get("url"):
                raise action.ActionError(f"column {name!r} is files; external.url is required")
            _coerce_url(name, external.get("url"))
        elif "external" in item:
            raise action.ActionError(f"column {name!r} is files; external must be an object with a URL")
        if "name" in item and not isinstance(item["name"], str):
            raise action.ActionError(f"column {name!r} is files; every file name must be a string")
        out.append(item)
    return out


def _coerce_date(name: str, value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, str):
        _validate_isoish_date(name, value)
        return {"start": value}
    if isinstance(value, Mapping) and isinstance(value.get("start"), str):
        _validate_isoish_date(name, str(value["start"]))
        out = dict(value)
        if out.get("end") is not None:
            _validate_isoish_date(name, str(out["end"]))
        return out
    raise action.ActionError(f"column {name!r} is date; provide an ISO date string or {{start, end?}}")


def _coerce_verification(name: str, value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        out: dict[str, Any] = {"state": value.strip()}
    elif isinstance(value, Mapping):
        out = dict(value)
    else:
        raise action.ActionError(f"column {name!r} is verification; provide 'verified', 'unverified', or native verification JSON")

    state = out.get("state")
    if not isinstance(state, str) or state not in _VERIFICATION_STATES:
        valid = ", ".join(_VERIFICATION_STATES)
        raise action.ActionError(f"column {name!r} is verification; {state!r} is not valid. Valid values: {valid}")
    if "verified_by" in out:
        raise action.ActionError(f"column {name!r} is verification; verified_by is read-only and must be omitted")
    if out.get("date") is not None:
        out["date"] = _coerce_date(name, out["date"])
    return out


def _validate_isoish_date(name: str, value: str) -> None:
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as e:
        raise action.ActionError(f"column {name!r} is date; {value!r} is not ISO-8601") from e


def _has_title_property(properties: dict[str, Any]) -> bool:
    for value in properties.values():
        if isinstance(value, dict) and "title" in value:
            return True
    return False


def _title_from_any(raw: Mapping[str, Any]) -> str:
    title = raw.get("title")
    if isinstance(title, list):
        return notion_read._plain_text(title)
    if isinstance(title, str):
        return title
    return ""


def _required_nonempty(value: str, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise action.ActionError(f"{name} must be a non-empty string")
    return value


def _notion_id(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        raise action.ActionError("Notion ID is required")
    match = re.search(r"[0-9a-fA-F]{32}|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", text)
    return match.group(0) if match else text


def _strip_uuid(value: str) -> str:
    return value.replace("-", "").lower()


def _snippet(text: str, *, limit: int = 180) -> str:
    one_line = " ".join(text.split())
    if len(one_line) <= limit:
        return one_line
    return one_line[: limit - 1] + "…"
