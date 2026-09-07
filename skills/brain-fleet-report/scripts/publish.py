# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Deliver a rendered fleet report: Drive upload + two mails. Dry-run unless `--send`.

    uv run skills/brain-fleet-report/scripts/publish.py --date 2026-09-04          # prints the plan
    uv run skills/brain-fleet-report/scripts/publish.py --date 2026-09-04 --send   # actually sends

Reads `[delivery]` from the brain's `_internal/fleet-report/config.toml`:

    [delivery]
    drive_folder_id      = "1AbC..."      # or drive_folder_name = "KampAdmin dagrapporten"
    recipients_technical = ["pj@..."]     # gets technical.email.html  (EN)
    recipients_owner     = ["thomas@..."] # gets owner.email.html      (NL)
    gw_cli = ["uv", "run", "--script", "…/google_workspace.py"]   # optional argv prefix

`gw_cli` is the google-workspace CLI (the one the v1 DentAI report used); it is invoked with
`drive-search` / `drive-create` / `drive-upload` / `gmail-send`, from the script's own directory so
its PEP 723 deps and OAuth token resolve. No recipients for a half ⇒ that half is uploaded but not
mailed.

`publish_state.json` next to the report records file hashes, Drive ids and message ids; a second run
on an unchanged report refuses to re-send unless `--force`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shlex
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fr_common import DEFAULT_TZ, find_brain_root, load_overlay, project_name, tzinfo  # noqa: E402

DEFAULT_GW_CLI = [
    "uv", "run", "--script",
    str(Path.home() / "code/dentai-org/dentai/.agents/skills/google-workspace/scripts/google_workspace.py"),
]
FOLDER_MIME = "application/vnd.google-apps.folder"

# (half, html, email html, text) — the owner half is NL and carries no technical content.
HALVES = (
    ("technical", "technical.html", "technical.email.html", "technical.txt"),
    ("owner", "owner.html", "owner.email.html", "owner.txt"),
)


class PublishError(RuntimeError):
    pass


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def gw(argv: Sequence[str], prefix: Sequence[str], *, send: bool) -> Any:
    """Run one google-workspace subcommand from the CLI's own directory; return parsed JSON."""
    cmd = [*prefix, *argv]
    cwd = None
    for part in reversed(prefix):
        candidate = Path(part).expanduser()
        if candidate.suffix == ".py" and candidate.exists():
            cwd = candidate.parent
            break
    if not send:
        where = f"(cd {shlex.quote(str(cwd))} && " if cwd else "("
        print(f"  [dry-run] {where}{shlex.join(cmd)})")
        return None
    done = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if done.returncode != 0:
        raise PublishError(f"{shlex.join(cmd)} failed ({done.returncode}):\n{done.stderr.strip()}")
    out = done.stdout.strip()
    if not out:
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return out


def folder_id(delivery: dict[str, Any], prefix: Sequence[str], *, send: bool) -> str | None:
    if delivery.get("drive_folder_id"):
        return str(delivery["drive_folder_id"])
    name = delivery.get("drive_folder_name")
    if not name:
        return None
    query = f"name = '{name}' and mimeType = '{FOLDER_MIME}' and trashed = false"
    found = gw(["drive-search", query, "-n", "5"], prefix, send=send)
    files = found.get("files") if isinstance(found, dict) else found
    if isinstance(files, list) and files and isinstance(files[0], dict) and files[0].get("id"):
        return str(files[0]["id"])
    created = gw(["drive-create", str(name), FOLDER_MIME], prefix, send=send)
    if not send:
        return "<folder-id>"
    if not isinstance(created, dict) or not created.get("id"):
        raise PublishError(f"could not create the Drive folder {name!r}: {created!r}")
    return str(created["id"])


def upload(path: Path, name: str, parent: str | None, prefix: Sequence[str], *, send: bool) -> dict[str, Any]:
    argv = ["drive-upload", str(path), "--name", name]
    if parent:
        argv += ["--folder", parent]
    result = gw(argv, prefix, send=send)
    if not send:
        return {"id": "<file-id>", "webViewLink": "<webViewLink>"}
    if not isinstance(result, dict) or not result.get("id"):
        raise PublishError(f"upload of {path.name} returned no file id: {result!r}")
    return result


def mail(recipients: Sequence[str], subject: str, html: Path, text: Path,
         prefix: Sequence[str], *, send: bool) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for address in recipients:
        argv = ["gmail-send", "--to", address, "--subject", subject, "--html-file", str(html)]
        if text.exists():
            argv += ["--body-file", str(text)]
        result = gw(argv, prefix, send=send)
        out.append({"to": address,
                    "message_id": (result or {}).get("id") if isinstance(result, dict) else None})
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Publish a rendered fleet report (dry-run by default)")
    parser.add_argument("--date", required=True, help="focus day YYYY-MM-DD of a rendered report")
    parser.add_argument("--report-id")
    parser.add_argument("--send", action="store_true", help="actually upload and mail")
    parser.add_argument("--force", action="store_true", help="re-send an unchanged report")
    args = parser.parse_args()

    brain_root = find_brain_root()
    overlay = load_overlay(brain_root)
    report_id = args.report_id or str(overlay.get("report_id") or project_name(brain_root))
    display = str(overlay.get("display_name") or report_id)
    tz = tzinfo(str(overlay.get("timezone", DEFAULT_TZ)))
    out_dir = brain_root / ".rootcause" / "fleet-report" / report_id / args.date
    if not out_dir.exists():
        print(f"no report at {out_dir} — run collect.py and render.py first", file=sys.stderr)
        return 1

    delivery = dict(overlay.get("delivery", {}) or {})
    if not delivery:
        print(f"no [delivery] block in {brain_root}/_internal/fleet-report/config.toml — "
              "see skills/brain-fleet-report/overlay.md", file=sys.stderr)
        return 1
    prefix = [str(p) for p in (delivery.get("gw_cli") or DEFAULT_GW_CLI)]
    parent = None

    files = {name: out_dir / name for _, *rest in HALVES for name in rest}
    files["report.json"] = out_dir / "report.json"
    missing = [str(p) for p in files.values() if not p.exists()]
    if missing:
        print("missing (run render.py first): " + ", ".join(missing), file=sys.stderr)
        return 1

    hashes = {name: sha(path) for name, path in sorted(files.items())}
    state_path = out_dir / "publish_state.json"
    previous = {}
    if state_path.exists():
        try:
            previous = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            previous = {}
    if previous.get("hashes") == hashes and not args.force:
        sent_at = previous.get("sent_at", "earlier")
        print(f"unchanged since {sent_at} — nothing to send (use --force to re-send)")
        return 0

    subjects = {"technical": f"[{display}] Fleet report {args.date}",
                "owner": f"[{display}] Dagrapport {args.date}"}
    recipients = {"technical": [str(a) for a in (delivery.get("recipients_technical") or [])],
                  "owner": [str(a) for a in (delivery.get("recipients_owner") or [])]}

    print(f"{'publish' if args.send else 'dry-run'} · {display} · {args.date} · {out_dir}")
    try:
        parent = folder_id(delivery, prefix, send=args.send)
        drive: dict[str, Any] = {}
        mails: dict[str, Any] = {}
        for half, html, email_html, text in HALVES:
            uploaded = upload(out_dir / html, f"{display} {half} {args.date}.html", parent,
                              prefix, send=args.send)
            drive[half] = {"id": uploaded.get("id"), "url": uploaded.get("webViewLink")}
            if not recipients[half]:
                print(f"  no recipients_{half} — uploaded only")
                continue
            mails[half] = mail(recipients[half], subjects[half], out_dir / email_html,
                               out_dir / text, prefix, send=args.send)
    except PublishError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if not args.send:
        print(f"  [dry-run] would write {state_path}")
        return 0

    state_path.write_text(json.dumps({
        "report_id": report_id,
        "date": args.date,
        "sent_at": datetime.now(tz).isoformat(timespec="seconds"),
        "hashes": hashes,
        "drive_folder_id": parent,
        "drive": drive,
        "mails": mails,
    }, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"published · {state_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
