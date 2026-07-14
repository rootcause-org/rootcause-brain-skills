"""Bubble.io support connector — token-cheap swagger discovery over ``lib.api``.

Force-code trigger: every Bubble app exposes a DIFFERENT, customer-defined data model, described only
by an auto-generated ``/meta/swagger.json`` that is routinely several MB. Dumping it into the agent
loop would blow the context budget, so this connector fetches it through the shared ``lib.api``
request machinery (broker route, retry, timeouts — never hand-rolled auth) and parses it in Python,
emitting a COMPACT inventory (a few hundred tokens) of endpoints and data types.

The ordinary object reads (``obj/<type>``) need no code — the manifest's offset pagination drives
them via ``python -m lib.api get bubble ...``. This module owns only discovery.

    python -m lib.connectors.bubble endpoints          # METHOD path, grouped by data type
    python -m lib.connectors.bubble endpoints obj      # only paths matching "obj"
    python -m lib.connectors.bubble types              # data types + field names/types
    python -m lib.connectors.bubble types --full user  # one type's complete schema

Read-only: the module issues a single GET for the swagger document and never writes.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from lib import api

_MANIFEST_PATH = Path(__file__).resolve().parent / "manifest.yaml"
_MANIFEST = api._parse_manifest_file(_MANIFEST_PATH)
api.register(_MANIFEST)
MANIFEST = _MANIFEST

# Bubble's swagger is auto-generated at this relative path; the broker joins it onto the app base.
SWAGGER_PATH = "meta/swagger.json"

_SUMMARY_CHARS = 80  # first N chars of an endpoint summary/description kept in the inventory
_ENUM_SHOWN = 4      # enum values shown inline before truncating to "(+N more)"


def _client() -> api.Client:
    return api.client(MANIFEST, token_key="bubble")


def fetch_swagger(client: api.Client | None = None) -> dict[str, Any]:
    """GET the app's swagger/OpenAPI document. Raises ``api.ApiError`` loudly on failure."""
    c = client or _client()
    body = c.get(SWAGGER_PATH)
    if not isinstance(body, dict):
        raise api.ApiError(0, "swagger document was not a JSON object", url=SWAGGER_PATH)
    return body


def _definitions(swagger: dict[str, Any]) -> dict[str, Any]:
    """Type schemas — Swagger 2.0 ``definitions`` (Bubble) or OpenAPI 3 ``components.schemas``."""
    defs = swagger.get("definitions")
    if isinstance(defs, dict):
        return defs
    comp = swagger.get("components")
    if isinstance(comp, dict) and isinstance(comp.get("schemas"), dict):
        return comp["schemas"]
    return {}


def _truncate(text: Any, limit: int) -> str:
    s = " ".join(str(text or "").split())
    return s if len(s) <= limit else s[: limit - 1].rstrip() + "…"


def _group_of(path: str, op: dict[str, Any]) -> str:
    """Bucket an endpoint by its first tag, else its ``/obj/<type>`` type, else first path segment."""
    tags = op.get("tags")
    if isinstance(tags, list) and tags:
        return str(tags[0])
    segs = [s for s in path.split("/") if s]
    if len(segs) >= 2 and segs[0] == "obj":
        return segs[1]
    return segs[0] if segs else "(root)"


def collect_endpoints(swagger: dict[str, Any], path_filter: str | None = None) -> list[dict[str, str]]:
    """Flatten swagger ``paths`` into ``{method, path, group, summary}`` rows, filtered + sorted."""
    paths = swagger.get("paths")
    rows: list[dict[str, str]] = []
    if not isinstance(paths, dict):
        return rows
    needle = (path_filter or "").lower()
    for path, methods in paths.items():
        if not isinstance(methods, dict):
            continue
        if needle and needle not in str(path).lower():
            continue
        for method, op in methods.items():
            if not isinstance(op, dict):
                continue
            summary = op.get("summary") or op.get("description") or ""
            rows.append({
                "method": str(method).upper(),
                "path": str(path),
                "group": _group_of(str(path), op),
                "summary": _truncate(summary, _SUMMARY_CHARS),
            })
    rows.sort(key=lambda r: (r["group"].lower(), r["path"], r["method"]))
    return rows


def format_endpoints(rows: list[dict[str, str]]) -> str:
    """Render endpoint rows as compact markdown grouped by data type."""
    if not rows:
        return "No endpoints found in the swagger document (is the Data API enabled for any type?)."
    out: list[str] = []
    current = None
    for r in rows:
        if r["group"] != current:
            current = r["group"]
            out.append(f"\n## {current}")
        line = f"  {r['method']:6} {r['path']}"
        if r["summary"]:
            line += f"  — {r['summary']}"
        out.append(line)
    out.append(f"\n{len(rows)} endpoint(s).")
    return "\n".join(out).lstrip()


def _field_type(schema: Any) -> str:
    """Compact type label for one property schema, truncating long enums."""
    if not isinstance(schema, dict):
        return "?"
    if isinstance(schema.get("enum"), list):
        vals = schema["enum"]
        shown = ", ".join(str(v) for v in vals[:_ENUM_SHOWN])
        more = f", +{len(vals) - _ENUM_SHOWN} more" if len(vals) > _ENUM_SHOWN else ""
        return f"enum[{shown}{more}]"
    t = schema.get("type")
    if t == "array":
        return f"array<{_field_type(schema.get('items'))}>"
    if isinstance(schema.get("$ref"), str):
        return schema["$ref"].rsplit("/", 1)[-1]
    return str(t or "object")


def collect_types(swagger: dict[str, Any]) -> list[dict[str, Any]]:
    """One row per data type: ``{name, fields:[{name, type}]}`` from the swagger definitions."""
    rows: list[dict[str, Any]] = []
    for name, schema in _definitions(swagger).items():
        props = schema.get("properties") if isinstance(schema, dict) else None
        fields = []
        if isinstance(props, dict):
            for fname, fschema in props.items():
                fields.append({"name": str(fname), "type": _field_type(fschema)})
        rows.append({"name": str(name), "fields": fields})
    rows.sort(key=lambda r: r["name"].lower())
    return rows


def format_types(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "No data types found in the swagger definitions."
    out: list[str] = []
    for r in rows:
        fields = ", ".join(f"{f['name']} ({f['type']})" for f in r["fields"]) or "(no fields)"
        out.append(f"- {r['name']}: {fields}")
    out.append(f"\n{len(rows)} data type(s).")
    return "\n".join(out)


def format_full_type(swagger: dict[str, Any], type_name: str) -> str:
    """Pretty-print one type's complete definition schema (the escape hatch from the compact list)."""
    defs = _definitions(swagger)
    schema = defs.get(type_name)
    if schema is None:
        # Case-insensitive fallback so `--full user` matches a `User` definition.
        for name, s in defs.items():
            if name.lower() == type_name.lower():
                schema = s
                type_name = name
                break
    if schema is None:
        known = ", ".join(sorted(defs)) or "(none)"
        return f"Unknown type {type_name!r}. Known types: {known}"
    return f"# {type_name}\n{json.dumps(schema, indent=2, sort_keys=True)}"


def _cmd_endpoints(args: argparse.Namespace) -> int:
    swagger = fetch_swagger()
    rows = collect_endpoints(swagger, args.filter)
    print(json.dumps(rows, indent=2) if args.json else format_endpoints(rows))
    return 0


def _cmd_types(args: argparse.Namespace) -> int:
    swagger = fetch_swagger()
    if args.full:
        print(format_full_type(swagger, args.full))
        return 0
    rows = collect_types(swagger)
    print(json.dumps(rows, indent=2) if args.json else format_types(rows))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="lib.connectors.bubble", description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)

    ep = sub.add_parser("endpoints", help="compact inventory of swagger endpoints (METHOD path)")
    ep.add_argument("filter", nargs="?", default=None, help="only paths containing this substring (e.g. obj)")
    ep.add_argument("--json", action="store_true", help="machine-readable JSON output")
    ep.set_defaults(func=_cmd_endpoints)

    ty = sub.add_parser("types", help="exposed data types with field names/types")
    ty.add_argument("--full", metavar="TYPE", default=None, help="print one type's complete schema")
    ty.add_argument("--json", action="store_true", help="machine-readable JSON output")
    ty.set_defaults(func=_cmd_types)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)
