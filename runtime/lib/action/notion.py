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
    "created_by",
    "created_time",
    "formula",
    "last_edited_by",
    "last_edited_time",
    "rollup",
    "unique_id",
}
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
    body = {match.block_type: {"rich_text": _rich_text(updated_text)}}
    raw = _client().patch(f"blocks/{match.block_id}", json=body)
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
    if isinstance(value, Mapping) and ptype in value:
        _validate_native_property_value(name, schema, value[ptype])
        return {ptype: value[ptype]}
    if ptype == "title":
        return {"title": _coerce_rich_text(name, value, "title")}
    if ptype == "rich_text":
        return {"rich_text": _coerce_rich_text(name, value, "rich_text")}
    if ptype == "number":
        return {"number": _coerce_number(name, value)}
    if ptype in {"select", "status"}:
        if value is None:
            return {ptype: None}
        label = _coerce_name(name, value, ptype)
        _validate_option(name, schema, label)
        return {ptype: {"name": label}}
    if ptype == "multi_select":
        labels = _coerce_string_list(name, value, "multi_select")
        for label in labels:
            _validate_option(name, schema, label)
        return {"multi_select": [{"name": label} for label in labels]}
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
        return {"relation": [{"id": _notion_id(x)} for x in _coerce_string_list(name, value, "relation page IDs")]}
    if ptype == "people":
        return {"people": [{"id": str(x)} for x in _coerce_string_list(name, value, "people user IDs")]}
    if ptype in {"files", "place"}:
        if isinstance(value, list) or isinstance(value, Mapping):
            return {ptype: value}
        raise action.ActionError(f"column {name!r} is {ptype}; provide Notion's native {ptype} JSON value")
    raise action.ActionError(f"column {name!r} has unsupported Notion property type {ptype!r}")


def _validate_native_property_value(name: str, schema: Mapping[str, Any], value: Any) -> None:
    ptype = str(schema.get("type") or "")
    if ptype in {"select", "status"} and isinstance(value, Mapping) and value.get("name"):
        _validate_option(name, schema, str(value["name"]))
    if ptype == "multi_select" and isinstance(value, list):
        for entry in value:
            if isinstance(entry, Mapping) and entry.get("name"):
                _validate_option(name, schema, str(entry["name"]))


def _validate_option(name: str, schema: Mapping[str, Any], label: str) -> None:
    ptype = str(schema.get("type") or "")
    options = ((schema.get(ptype) or {}).get("options") or []) if isinstance(schema.get(ptype), Mapping) else []
    valid = [str(o.get("name")) for o in options if isinstance(o, Mapping) and o.get("name")]
    if valid and label not in valid:
        raise action.ActionError(
            f"column {name!r} is {ptype}; {label!r} is not valid. Valid values: {', '.join(valid)}"
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
    return NotionPage(id=str(raw.get("id", "")), url=str(raw.get("url", "")), raw=notion_read._compact_page(raw))


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


def _coerce_name(name: str, value: Any, label: str) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, Mapping) and isinstance(value.get("name"), str) and value["name"].strip():
        return value["name"].strip()
    raise action.ActionError(f"column {name!r} is {label}; provide one option name")


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
