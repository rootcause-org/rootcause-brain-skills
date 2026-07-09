"""Minimal Google Drive write helpers for hosted actions."""

from __future__ import annotations

import mimetypes
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from lib import action, api


ACTION_HELPER_DOCS = {
    "provider": "googledrive",
    "need": "Upload files or attachments to Google Drive",
    "connection": "googledrive.write",
    "import": "from lib.action import googledrive",
    "source_module": "lib.action.googledrive",
    "manifest": [
        "Declare `connections: [googledrive.write]`.",
        "Use `type: attachment` params for user-provided files; pass the file param directly to `upload_attachment`.",
        "Use `type: generated_file` with `generator: email_message_pdf` when the host should render an email body to PDF before upload; consume it with `p.file(...)` and `upload_file`.",
    ],
    "common_params": [
        "`folder_id`: destination Drive folder ID.",
        "`attachment`: hosted-action attachment param when uploading a user-provided file.",
        "`email_pdf`: hosted generated_file param when uploading a rendered email-message PDF.",
        "`name`: optional override for the Drive filename.",
    ],
    "useful_for": [
        "upload an email/dashboard attachment into a known Drive folder",
        "upload a generated local file from the action workspace",
        "return the Drive file ID and reviewer/shareable web URL",
    ],
    "helpers": {
        "upload_file": "Upload an action attachment or local file path to a Google Drive folder.",
        "upload_attachment": "Convenience wrapper for hosted-action attachment params.",
    },
    "patterns": [
        {
            "title": "Upload an action attachment",
            "code": """
from lib import action
from lib.action import googledrive

@action.main
def run(p: action.Params) -> dict:
    if action.dry_run():
        return {"summary": f"Would upload **{p['attachment'].filename}** to Google Drive."}

    file = googledrive.upload_attachment(folder_id=p["folder_id"], attachment=p["attachment"])
    return {"summary": f"Uploaded **{file.name}** to Google Drive.", "file_id": file.id, "url": file.web_url}
""",
        },
        {
            "title": "Upload a generated file",
            "code": """
from pathlib import Path
from lib import action
from lib.action import googledrive

@action.main
def run(p: action.Params) -> dict:
    path = Path("/tmp/report.txt")
    path.write_text("reviewed\\n")
    if action.dry_run():
        return {"summary": "Would upload `report.txt` to Google Drive."}

    file = googledrive.upload_file(folder_id=p["folder_id"], file=path, name="report.txt")
    return {"summary": f"Uploaded **{file.name}**.", "file_id": file.id, "url": file.web_url}
""",
        },
    ],
    "validation_failure": [
        "Local paths are read from disk; missing files surface as normal file errors.",
        "Drive/API failures surface through `api.ApiError`/`action.ActionError` and become reviewer-visible action failures.",
        "The helper infers MIME type from the attachment or filename unless `mime_type` is provided.",
    ],
    "do_not": [
        "Do not call Drive upload endpoints manually; the helper handles multipart upload metadata and content.",
        "Do not use `RC_CONN_GOOGLEDRIVE`; hosted actions use the `googledrive.write` action connection.",
        "Do not assume a folder name is unique; pass a known `folder_id` discovered during grounding/preflight.",
    ],
}


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
