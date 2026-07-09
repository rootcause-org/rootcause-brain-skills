"""Airtable write helpers for hosted actions.

These helpers keep action scripts out of raw Airtable payload guessing: they read the live base/table
schema from Airtable's Metadata API, validate field names and writable cell values, then submit one
create/update request only after validation passes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from difflib import get_close_matches
from functools import lru_cache
from typing import Any, Mapping
from urllib.parse import quote

from lib import action, api


ACTION_HELPER_DOCS = {
    "provider": "airtable",
    "need": "Create/update Airtable records with schema-aware field validation",
    "connection": "airtable.write",
    "import": "from lib.action import airtable",
    "source_module": "lib.action.airtable",
    "manifest": [
        "Declare `connections: [airtable.write]`.",
        "Use `base_id`, `table_id_or_name`, and plain `fields`; call `validate_record_fields` before create/update.",
        "Pass table IDs when available; table names are accepted after live Metadata API lookup.",
        "Use human-readable Airtable field names in `fields`; field IDs are also accepted when scripts already have them.",
        "Default validation rejects new select choices; pass `typecast=True` only when creating choices is intentional.",
    ],
    "common_params": [
        "`base_id`: Airtable base ID, for example `appXXXXXXXXXXXXXX`.",
        "`table_id_or_name`: Airtable table ID or exact table name; table IDs are preferred.",
        "`record_id`: existing Airtable record ID for updates.",
        "`fields`: JSON object of Airtable field names to plain values.",
    ],
    "useful_for": [
        "create one Airtable record after checking field names, choices, and writable types",
        "update one Airtable record with PATCH semantics after validating the same payload",
        "dry-run a prospective Airtable write and show reviewers which base/table/fields will change",
    ],
    "helpers": {
        "retrieve_table_schema": "Fetch and return one Airtable table schema from the Metadata API.",
        "validate_record_fields": "Validate plain field values against a live Airtable table schema.",
        "record_validation_summary": "Build a reviewer-readable dry-run/preflight summary from validated fields.",
        "create_record": "Create one Airtable record after schema validation.",
        "update_record": "Patch one Airtable record after schema validation.",
    },
    "patterns": [
        {
            "title": "Create a record",
            "code": """
from lib import action
from lib.action import airtable

@action.main
def run(p: action.Params) -> dict:
    checked = airtable.validate_record_fields(p["base_id"], p["table_id_or_name"], p["fields"])
    if action.dry_run():
        return {"summary": airtable.record_validation_summary(checked, operation="Dry run: create record")}

    record = airtable.create_record(
        base_id=p["base_id"],
        table_id_or_name=p["table_id_or_name"],
        fields=p["fields"],
    )
    return {"summary": f"Created Airtable record **{record.id}**.", "record_id": record.id}
""",
        },
        {
            "title": "Update a record",
            "code": """
from lib import action
from lib.action import airtable

@action.main
def run(p: action.Params) -> dict:
    checked = airtable.validate_record_fields(p["base_id"], p["table_id_or_name"], p["fields"])
    if action.dry_run():
        return {"summary": airtable.record_validation_summary(checked, operation="Dry run: update record")}

    record = airtable.update_record(
        base_id=p["base_id"],
        table_id_or_name=p["table_id_or_name"],
        record_id=p["record_id"],
        fields=p["fields"],
    )
    return {"summary": f"Updated Airtable record **{record.id}**.", "record_id": record.id}
""",
        },
    ],
    "validation_failure": [
        "All helpers use `action.client(\"airtable.write\")`; missing `connections: [airtable.write]` or a missing project write grant fails before/at execution.",
        "Record helpers read the live Metadata API schema; unknown tables/fields include available names and close-name suggestions.",
        "Select fields are exact-name checked against live choices; wrong choices list valid values unless `typecast=True` is explicit.",
        "Writable field types are validated before the API call: text/rich text/long text, email, URL, phone, checkbox, number/currency/percent, rating, duration, date/dateTime, barcode, linked records, collaborator fields, attachments, single select, and multiple select.",
        "Read-only/computed fields are rejected: aiText, autoNumber, button, count, createdBy, createdTime, formula, lastModifiedBy, lastModifiedTime, multipleLookupValues, rollup, and externalSyncSource.",
        "Dry-run patterns call the same validators the write path uses, so reviewer-facing failures match execution failures.",
    ],
    "do_not": [
        "Do not skip `validate_record_fields` in dry runs; reviewers should see schema errors before execution.",
        "Do not guess Airtable field names or select choices; validate against the live schema first.",
        "Do not set read-only/computed fields such as formulas, lookups, rollups, created/modified fields, counts, buttons, or AI text.",
        "Do not rely on Airtable `typecast` to create new select choices unless the workflow explicitly wants that schema change.",
        "Do not pass `RC_CONN_*` tokens or raw Authorization headers; helpers use `RC_ACTION_*` via `airtable.write`.",
        "Do not import provider SDKs or call raw Airtable HTTP endpoints from hosted scripts when these helpers cover the write.",
    ],
}


@dataclass(frozen=True)
class AirtableRecord:
    id: str
    fields: dict[str, Any]
    raw: dict[str, Any]


@dataclass(frozen=True)
class AirtableTableSchema:
    base_id: str
    id: str
    name: str
    fields: dict[str, dict[str, Any]]
    fields_by_id: dict[str, dict[str, Any]]
    raw: dict[str, Any]


@dataclass(frozen=True)
class AirtableValidation:
    table: AirtableTableSchema
    fields: dict[str, Any]
    observed: dict[str, Any]


_READ_ONLY_FIELD_TYPES = {
    "aiText",
    "autoNumber",
    "button",
    "count",
    "createdBy",
    "createdTime",
    "formula",
    "lastModifiedBy",
    "lastModifiedTime",
    "multipleLookupValues",
    "rollup",
    "externalSyncSource",
}
_TEXT_FIELD_TYPES = {"singleLineText", "multilineText", "richText"}
_NUMBER_FIELD_TYPES = {"number", "currency", "percent"}
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@lru_cache(maxsize=1)
def _client():
    manifest = api.load_manifests().get("airtable")
    return action.client("airtable.write", manifest=manifest)


def retrieve_table_schema(base_id: str, table_id_or_name: str) -> AirtableTableSchema:
    """Return one Airtable table schema from ``GET /meta/bases/{baseId}/tables``."""
    base = _required_nonempty(base_id, "base_id")
    wanted = _required_nonempty(table_id_or_name, "table_id_or_name")
    raw = _client().get(f"meta/bases/{_path_part(base)}/tables")
    tables = raw.get("tables") if isinstance(raw, Mapping) else None
    if not isinstance(tables, list):
        raise action.ActionError(f"Airtable base {base!r} did not return a tables schema")
    matches = [
        table
        for table in tables
        if isinstance(table, Mapping) and (str(table.get("id") or "") == wanted or str(table.get("name") or "") == wanted)
    ]
    if len(matches) != 1:
        available = "; ".join(
            f"{table.get('name') or '(unnamed)'} ({table.get('id')})" for table in tables if isinstance(table, Mapping)
        )
        names = [str(table.get("name") or "") for table in tables if isinstance(table, Mapping)]
        close = get_close_matches(wanted, names, n=3, cutoff=0.55)
        suffix = f" Did you mean: {', '.join(close)}?" if close else ""
        raise action.ActionError(f"Airtable table {wanted!r} was not found in base {base!r}. Available tables: {available}.{suffix}")
    return _table_schema(base, matches[0])


def validate_record_fields(
    base_id: str,
    table_id_or_name: str,
    fields: Mapping[str, Any],
    *,
    typecast: bool = False,
) -> AirtableValidation:
    """Validate plain Airtable field values against the live table schema."""
    if not isinstance(fields, Mapping) or not fields:
        raise action.ActionError("fields must be a non-empty JSON object of Airtable field names to values")
    table = retrieve_table_schema(base_id, table_id_or_name)
    errors: list[str] = []
    payload: dict[str, Any] = {}
    for key, value in fields.items():
        if not isinstance(key, str) or not key.strip():
            errors.append(f"field name {key!r} is not a non-empty string")
            continue
        field = table.fields.get(key) or table.fields_by_id.get(key)
        if field is None:
            errors.append(_unknown_field_message(key, table.fields))
            continue
        name = str(field.get("name") or key)
        try:
            payload[name] = _field_value(name, field, value, typecast=typecast)
        except action.ActionError as e:
            errors.append(str(e))
    if errors:
        raise action.ActionError("Airtable record fields did not match the table schema:\n- " + "\n- ".join(errors))
    observed = {
        "base_id": table.base_id,
        "table_id": table.id,
        "table_name": table.name,
        "fields": {name: _field_summary(schema) for name, schema in table.fields.items()},
        "provided_fields": sorted(str(k) for k in fields),
        "typecast": bool(typecast),
    }
    return AirtableValidation(table=table, fields=payload, observed=observed)


def record_validation_summary(checked: AirtableValidation, *, operation: str) -> str:
    names = ", ".join(checked.observed["provided_fields"])
    table = checked.table.name or checked.table.id
    return f"{operation} would set **{names}** on Airtable table **{table}**."


def create_record(*, base_id: str, table_id_or_name: str, fields: Mapping[str, Any], typecast: bool = False) -> AirtableRecord:
    checked = validate_record_fields(base_id, table_id_or_name, fields, typecast=typecast)
    body = {"fields": checked.fields}
    if typecast:
        body["typecast"] = True
    raw = _client().post(f"{_path_part(checked.table.base_id)}/{_path_part(checked.table.id)}", json=body)
    return _record(raw)


def update_record(
    *,
    base_id: str,
    table_id_or_name: str,
    record_id: str,
    fields: Mapping[str, Any],
    typecast: bool = False,
) -> AirtableRecord:
    checked = validate_record_fields(base_id, table_id_or_name, fields, typecast=typecast)
    rid = _required_nonempty(record_id, "record_id")
    body = {"fields": checked.fields}
    if typecast:
        body["typecast"] = True
    raw = _client().patch(f"{_path_part(checked.table.base_id)}/{_path_part(checked.table.id)}/{_path_part(rid)}", json=body)
    return _record(raw)


def _table_schema(base_id: str, raw: Mapping[str, Any]) -> AirtableTableSchema:
    fields_raw = raw.get("fields")
    if not isinstance(fields_raw, list):
        raise action.ActionError(f"Airtable table {raw.get('name') or raw.get('id')!r} did not return a field schema")
    by_name: dict[str, dict[str, Any]] = {}
    by_id: dict[str, dict[str, Any]] = {}
    for field in fields_raw:
        if not isinstance(field, Mapping):
            continue
        item = dict(field)
        name = str(item.get("name") or "")
        fid = str(item.get("id") or "")
        if name:
            by_name[name] = item
        if fid:
            by_id[fid] = item
    return AirtableTableSchema(
        base_id=base_id,
        id=str(raw.get("id") or ""),
        name=str(raw.get("name") or ""),
        fields=by_name,
        fields_by_id=by_id,
        raw=dict(raw),
    )


def _record(raw: dict[str, Any]) -> AirtableRecord:
    return AirtableRecord(id=str(raw.get("id", "")), fields=dict(raw.get("fields") or {}), raw=raw)


def _field_value(name: str, field: Mapping[str, Any], value: Any, *, typecast: bool) -> Any:
    ftype = str(field.get("type") or "")
    if ftype in _READ_ONLY_FIELD_TYPES:
        raise action.ActionError(f"field {name!r} is read-only ({ftype}) and cannot be set through the API")
    if ftype in _TEXT_FIELD_TYPES:
        return _coerce_string(name, value, ftype)
    if ftype in _NUMBER_FIELD_TYPES:
        return _coerce_number(name, value, ftype)
    if ftype == "checkbox":
        if not isinstance(value, bool):
            raise action.ActionError(f"field {name!r} is checkbox; provide true or false")
        return value
    if ftype == "singleSelect":
        if value is None:
            return None
        return _coerce_single_select(name, field, value, typecast=typecast)
    if ftype == "multipleSelects":
        return _coerce_multiple_selects(name, field, value, typecast=typecast)
    if ftype == "singleCollaborator":
        return _coerce_single_collaborator(name, value)
    if ftype == "multipleCollaborators":
        return _coerce_string_list(name, value, "multipleCollaborators user/group IDs")
    if ftype == "multipleRecordLinks":
        return _coerce_string_list(name, value, "linked record IDs")
    if ftype == "multipleAttachments":
        return _coerce_attachments(name, value)
    if ftype == "date":
        return _coerce_date(name, value)
    if ftype == "dateTime":
        return _coerce_datetime(name, value)
    if ftype == "email":
        return _coerce_email(name, value)
    if ftype == "url":
        return _coerce_url(name, value)
    if ftype == "phoneNumber":
        return _coerce_string(name, value, "phoneNumber")
    if ftype == "rating":
        return _coerce_rating(name, field, value)
    if ftype == "duration":
        return _coerce_duration(name, value)
    if ftype == "barcode":
        return _coerce_barcode(name, value)
    raise action.ActionError(f"field {name!r} has unsupported Airtable field type {ftype!r}; omit it")


def _coerce_single_select(name: str, field: Mapping[str, Any], value: Any, *, typecast: bool) -> str:
    if not isinstance(value, str) or not value.strip():
        raise action.ActionError(f"field {name!r} is singleSelect; provide one option name")
    choice = value.strip()
    if not typecast:
        _validate_choice(name, field, choice, "singleSelect")
    return choice


def _coerce_multiple_selects(name: str, field: Mapping[str, Any], value: Any, *, typecast: bool) -> list[str]:
    values = _coerce_string_list(name, value, "multipleSelects option names")
    if not typecast:
        for choice in values:
            _validate_choice(name, field, choice, "multipleSelects")
    return values


def _validate_choice(name: str, field: Mapping[str, Any], choice: str, label: str) -> None:
    choices = _choice_names(field)
    if choices and choice not in choices:
        valid = ", ".join(choices)
        close = get_close_matches(choice, choices, n=3, cutoff=0.55)
        suffix = f" Did you mean: {', '.join(close)}?" if close else ""
        raise action.ActionError(f"field {name!r} is {label}; {choice!r} is not valid. Valid values: {valid}.{suffix}")


def _choice_names(field: Mapping[str, Any]) -> list[str]:
    options = field.get("options")
    choices = options.get("choices") if isinstance(options, Mapping) else None
    if not isinstance(choices, list):
        return []
    return [str(choice.get("name")) for choice in choices if isinstance(choice, Mapping) and choice.get("name")]


def _coerce_single_collaborator(name: str, value: Any) -> dict[str, str] | None:
    if value is None:
        return None
    if isinstance(value, str) and value.strip():
        text = value.strip()
        return {"email": text} if "@" in text else {"id": text}
    if isinstance(value, Mapping):
        if isinstance(value.get("id"), str) and value["id"].strip():
            return {"id": value["id"].strip()}
        if isinstance(value.get("email"), str) and value["email"].strip():
            email = _coerce_email(name, value["email"])
            return {"email": email} if email is not None else None
    raise action.ActionError(f"field {name!r} is singleCollaborator; provide a user/group ID or {{id}}/{{email}}")


def _coerce_attachments(name: str, value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise action.ActionError(f"field {name!r} is multipleAttachments; provide an array of attachment objects")
    out = []
    for item in value:
        if not isinstance(item, Mapping):
            raise action.ActionError(f"field {name!r} is multipleAttachments; every attachment must be an object")
        attachment = dict(item)
        url = attachment.get("url")
        att_id = attachment.get("id")
        if isinstance(url, str) and url.strip():
            _coerce_url(name, url)
        elif not (isinstance(att_id, str) and att_id.strip()):
            raise action.ActionError(f"field {name!r} is multipleAttachments; each attachment needs a url or existing id")
        if "filename" in attachment and not isinstance(attachment["filename"], str):
            raise action.ActionError(f"field {name!r} is multipleAttachments; filename must be a string")
        out.append(attachment)
    return out


def _coerce_barcode(name: str, value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, str) and value.strip():
        return {"text": value.strip()}
    if isinstance(value, Mapping):
        text = value.get("text")
        if not isinstance(text, str) or not text.strip():
            raise action.ActionError(f"field {name!r} is barcode; text must be a non-empty string")
        out = dict(value)
        if out.get("type") is not None and not isinstance(out["type"], str):
            raise action.ActionError(f"field {name!r} is barcode; type must be a string when provided")
        return out
    raise action.ActionError(f"field {name!r} is barcode; provide a string or {{text, type?}}")


def _coerce_date(name: str, value: Any) -> str | None:
    text = _coerce_string(name, value, "date")
    if text is None:
        return None
    try:
        date.fromisoformat(text)
    except ValueError as e:
        raise action.ActionError(f"field {name!r} is date; {text!r} is not an ISO date like YYYY-MM-DD") from e
    return text


def _coerce_datetime(name: str, value: Any) -> str | None:
    text = _coerce_string(name, value, "dateTime")
    if text is None:
        return None
    try:
        datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as e:
        raise action.ActionError(f"field {name!r} is dateTime; {text!r} is not ISO-8601") from e
    return text


def _coerce_email(name: str, value: Any) -> str | None:
    email = _coerce_string(name, value, "email")
    if email is not None and not _EMAIL_RE.match(email):
        raise action.ActionError(f"field {name!r} is email; {email!r} is not a valid email address")
    return email


def _coerce_url(name: str, value: Any) -> str | None:
    url = _coerce_string(name, value, "url")
    if url is not None and not (url.startswith("http://") or url.startswith("https://")):
        raise action.ActionError(f"field {name!r} is url; provide an http(s) URL")
    return url


def _coerce_number(name: str, value: Any, label: str) -> int | float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise action.ActionError(f"field {name!r} is {label}; provide a JSON number")
    return value


def _coerce_rating(name: str, field: Mapping[str, Any], value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise action.ActionError(f"field {name!r} is rating; provide a positive integer")
    max_value = _rating_max(field)
    if value < 1 or value > max_value:
        raise action.ActionError(f"field {name!r} is rating; provide an integer from 1 to {max_value}")
    return value


def _rating_max(field: Mapping[str, Any]) -> int:
    options = field.get("options")
    max_value = options.get("max") if isinstance(options, Mapping) else None
    return max_value if isinstance(max_value, int) and max_value > 0 else 10


def _coerce_duration(name: str, value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise action.ActionError(f"field {name!r} is duration; provide a non-negative integer number of seconds")
    return value


def _coerce_string(name: str, value: Any, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise action.ActionError(f"field {name!r} is {label}; provide a string")
    return value


def _coerce_string_list(name: str, value: Any, label: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, list):
        values = value
    else:
        raise action.ActionError(f"field {name!r} is {label}; provide a string array")
    if not all(isinstance(item, str) and item.strip() for item in values):
        raise action.ActionError(f"field {name!r} is {label}; every item must be a non-empty string")
    return [item.strip() for item in values]


def _unknown_field_message(name: str, fields: Mapping[str, Mapping[str, Any]]) -> str:
    names = list(fields)
    close = get_close_matches(name, names, n=3, cutoff=0.55)
    suffix = f" Did you mean: {', '.join(close)}?" if close else ""
    return f"field {name!r} does not exist. Available fields: {_schema_summary(fields)}.{suffix}"


def _schema_summary(fields: Mapping[str, Mapping[str, Any]]) -> str:
    return "; ".join(f"{name} ({_field_summary(field)})" for name, field in fields.items())


def _field_summary(field: Mapping[str, Any]) -> str:
    ftype = str(field.get("type") or "")
    choices = _choice_names(field)
    if choices:
        return f"{ftype}: {', '.join(choices)}"
    return ftype


def _required_nonempty(value: Any, name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise action.ActionError(f"{name} must be a non-empty string")
    return text


def _path_part(value: str) -> str:
    return quote(value, safe="")
