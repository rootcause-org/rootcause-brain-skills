"""Minimal Google Drive write helpers for hosted actions."""

from __future__ import annotations

import mimetypes
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from lib import action, api


@dataclass(frozen=True)
class DriveFile:
    id: str
    name: str
    web_url: str
    raw: dict[str, Any]


@lru_cache(maxsize=1)
def _client():
    api.load_manifests()
    return action.client("googledrive.write", manifest=api.MANIFESTS.get("googledrive"))


def upload_file(
    *,
    folder_id: str,
    file: action.FileParam | Path | str,
    name: str | None = None,
    mime_type: str | None = None,
) -> DriveFile:
    source = _source(file)
    filename = name or source["name"]
    content_type = mime_type or source["mime_type"] or "application/octet-stream"
    metadata = {"name": filename, "parents": [folder_id]}
    raw = _client().upload(
        "https://www.googleapis.com/upload/drive/v3/files",
        data=source["data"],
        content_type=content_type,
        metadata=metadata,
        query={"fields": "id,name,webViewLink"},
    )
    return DriveFile(
        id=str(raw.get("id", "")),
        name=str(raw.get("name", filename)),
        web_url=str(raw.get("webViewLink", "")),
        raw=raw,
    )


def upload_attachment(*, folder_id: str, attachment: action.FileParam, name: str | None = None) -> DriveFile:
    return upload_file(
        folder_id=folder_id,
        file=attachment,
        name=name or attachment.filename,
        mime_type=attachment.mime_type,
    )


def _source(file: action.FileParam | Path | str) -> dict[str, Any]:
    if isinstance(file, action.FileParam):
        return {
            "data": file.read_bytes(),
            "name": file.filename or file.path.name,
            "mime_type": file.mime_type,
        }
    path = Path(file)
    return {
        "data": path.read_bytes(),
        "name": path.name,
        "mime_type": mimetypes.guess_type(path.name)[0] or "",
    }
