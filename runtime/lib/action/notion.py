"""Minimal Notion write helpers for hosted actions."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from lib import action
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
    raw = _client().patch(f"blocks/{page_id}/children", json=body)
    results = raw.get("results") if isinstance(raw, dict) else None
    block = results[0] if results else raw
    return NotionBlock(id=str(block.get("id", "")), raw=block)


def create_page(*, parent_id: str, title: str, properties: dict | None = None) -> NotionPage:
    props = dict(properties or {})
    if not _has_title_property(props):
        props.setdefault("title", {"title": _rich_text(title)})
    body = {"parent": {"page_id": parent_id}, "properties": props}
    raw = _client().post("pages", json=body)
    return _page(raw)


def update_properties(*, page_id: str, properties: dict) -> NotionPage:
    raw = _client().patch(f"pages/{page_id}", json={"properties": properties})
    return _page(raw)


def _page(raw: dict[str, Any]) -> NotionPage:
    return NotionPage(id=str(raw.get("id", "")), url=str(raw.get("url", "")), raw=notion_read._compact_page(raw))


def _rich_text(text: str) -> list[dict[str, Any]]:
    return [{"type": "text", "text": {"content": text}}]


def _has_title_property(properties: dict[str, Any]) -> bool:
    for value in properties.values():
        if isinstance(value, dict) and "title" in value:
            return True
    return False
