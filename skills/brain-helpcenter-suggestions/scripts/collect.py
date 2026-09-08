# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Collect one window of customer questions + the help-centre inventory into `evidence.json`.

    uv run skills/brain-helpcenter-suggestions/scripts/collect.py --days 8 [--out DIR] [--corpus dump.txt]

Writes `<brain>/.rootcause/helpcenter/<window end date>/`:
  evidence.json  the judge contract (its sha256 goes into suggestions.json)
  digest.md      the only file the judge reads whole
  raw/           rc + Help Scout page cache, keyed by argv / page number

Read-only: `rc fleet runs`, `rc run trace`, `rc project mailbox ls`, and guarded
`rc dev console bash run` reads. Never `rc ask`, never a write action.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shlex
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "brain-fleet-report" / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from fr_common import Rc, clip, find_brain_root, guarded, json_objects, parallel, project_name
except ImportError:  # noqa: BLE001 - the sibling skill is the only hard dependency
    raise SystemExit(
        "brain-helpcenter-suggestions needs the sibling skill `brain-fleet-report` "
        "(scripts/fr_common.py) installed next to it — reinstall the kit with brain-dev-upgrade."
    )

import corpus  # noqa: E402

MAX_HS_PAGES = 40
CONSOLE_TIMEOUT = 300
# Verified 2026-09-08 (iBeauty): console stdout caps at 64 KiB, a page is ~270 KB, so the workspace
# writes the raw page to an artifact we fetch and only echoes the pagination block.
HS_PAGE_SCRIPT = (
    "import json,subprocess,os; os.makedirs('/tmp/rootcause-out',exist_ok=True); "
    "r=subprocess.run(['python','-m','lib.api','get','helpscout','/conversations',"
    "'--query','status=all','--query','embed=threads','--query','page={page}',"
    "'--query','query=(createdAt:[{start} TO {end}])'],capture_output=True,text=True); "
    "p=json.loads(r.stdout); "
    "json.dump(p,open('/tmp/rootcause-out/hc-page-{page}.json','w')); "
    "print(json.dumps(p.get('page')))"
)
KB_INDEX_SCRIPT = "cat /kb/*/INDEX.md"
KB_META_SCRIPT = (
    "for f in /kb/*/*/*.md /kb/*/*.md; do [ -f \"$f\" ] && "
    "{ echo \"@@ $f\"; sed -n '2,25p' \"$f\" | grep -E '^(url|updated_at):'; }; done 2>/dev/null"
)
BRAIN_KNOWLEDGE_SCRIPT = (
    "find /brain/knowledge -name '*.md' -exec sh -c 'echo \"@@ $1\"; grep -m1 \"^# \" \"$1\"' _ {} \\;"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=7, help="window length in days (default 7)")
    parser.add_argument("--out", help="output directory (default .rootcause/helpcenter/<end date>)")
    parser.add_argument("--corpus", help="harvest dump.txt to read instead of a live source")
    return parser.parse_args()


def cover(feed: str, status: str, scanned: int, retained: int, reason: str | None = None) -> dict:
    return {"feed": feed, "status": status, "scanned": scanned, "retained": retained,
            "reason": clip(reason, 200) or None}


def console(brain_root: Path, script: str) -> dict[str, Any]:
    """`rc dev console bash run` envelope: {stdout, stderr, exit_code, stdout_truncated}."""
    try:
        done = subprocess.run(
            ["rc", "dev", "console", "bash", "run", "-o", "json", "--raw-output", script],
            capture_output=True, text=True, timeout=CONSOLE_TIMEOUT, cwd=brain_root)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"exit_code": -1, "stderr": f"{type(exc).__name__}: {exc}", "stdout": ""}
    docs = [d for d in json_objects(done.stdout) if isinstance(d, dict) and "exit_code" in d]
    if docs:
        return docs[-1]
    return {"exit_code": done.returncode or -1, "stdout": "",
            "stderr": (done.stderr or "no console envelope").strip()}


# ------------------------------------------------------------------ sources


def from_runs(rc: Rc, runs: list[dict], start: datetime, end: datetime) -> tuple[list[dict], dict]:
    """Trace headers in parallel; several runs on one thread collapse into the earliest one."""
    def header(run: dict) -> dict | None:
        records = rc.jsonl("run", "trace", str(run.get("run_id") or run.get("id")), "--stream")
        return records[0] if records else None

    headers = [h for h in parallel(runs, guarded(rc, "runs", lambda r: str(r.get("run_id")), header)) if h]
    by_thread: dict[str, dict] = {}
    for head in sorted(headers, key=lambda h: str(h.get("created_at") or "")):
        key = str(head.get("thread_id") or head.get("run_id") or id(head))
        by_thread.setdefault(key, head)
    conversations = [c for c in (corpus.trace_conversation(h) for h in by_thread.values()) if c]
    conversations = [c for c in conversations if corpus.in_window(c["created_at"], start, end)]
    status = "complete" if len(headers) == len(runs) else "partial"
    reason = None if status == "complete" else f"{len(runs) - len(headers)} trace(s) unavailable"
    return conversations, cover("email_runs", status, len(runs), len(conversations), reason)


def hs_pages(brain_root: Path, raw_dir: Path, start: datetime, end: datetime) -> tuple[list[Any], dict]:
    """Page loop over the workspace connector; each page lands in raw/hs-page-<n>.json."""
    stamp = "%Y-%m-%dT%H:%M:%SZ"
    pages: list[Any] = []
    total, number, failed = 1, 1, None
    while number <= total and number <= MAX_HS_PAGES:
        target = raw_dir / f"hs-page-{number}.json"
        if not target.exists():
            script = HS_PAGE_SCRIPT.format(page=number, start=start.strftime(stamp),
                                           end=end.strftime(stamp))
            envelope = console(brain_root, "python -c " + shlex.quote(script))
            if envelope.get("exit_code") != 0 or not str(envelope.get("stdout") or "").strip():
                failed = f"page {number}: {clip(envelope.get('stderr') or 'empty stdout', 160)}"
                break
            fetch = subprocess.run(
                ["rc", "dev", "console", "file", "get", f"/tmp/rootcause-out/hc-page-{number}.json",
                 "--out", str(target)], capture_output=True, text=True,
                timeout=CONSOLE_TIMEOUT, cwd=brain_root)
            if fetch.returncode != 0 or not target.exists():
                failed = f"page {number}: file get failed ({clip(fetch.stderr, 120)})"
                break
        try:
            page = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            failed = f"page {number}: {type(exc).__name__}"
            break
        pages.append(page)
        if number == 1:
            total = int(((page.get("page") or {}).get("totalPages")) or 1)
        number += 1
    return pages, cover("helpscout", "partial" if failed else "complete", 0, 0, failed)


def from_helpscout(brain_root: Path, raw_dir: Path, start: datetime, end: datetime):
    pages, coverage = hs_pages(brain_root, raw_dir, start, end)
    seen: dict[str, dict] = {}
    scanned = 0
    for page in pages:
        for conv in corpus.helpscout_conversations(page):
            scanned += 1
            if corpus.in_window(conv["created_at"], start, end):
                seen.setdefault(conv["id"], conv)
    conversations = corpus.merge_split_chats(list(seen.values()))
    coverage.update(scanned=scanned, retained=len(conversations))
    if not pages:
        coverage["status"] = "unavailable"
    return conversations, coverage


def from_corpus(path: Path, start: datetime, end: datetime):
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return [], cover("harvest", "unavailable", 0, 0, f"{path}: {exc}")
    parsed = corpus.dump_conversations(text)
    kept = [c for c in parsed if corpus.in_window(c["created_at"], start, end)]
    return kept, cover("harvest", "complete", len(parsed), len(kept),
                       "no conversation urls in a dump export")


# ------------------------------------------------------------------ inventory


def article(aid: str, home: str, path: str, title: str, **extra: Any) -> dict[str, Any]:
    base = {"id": aid, "home": home, "path": path, "title": title, "url": None, "keywords": [],
            "aliases": [], "summary": "", "collection": None, "area": None, "updated_at": None,
            "deprecated": False}
    return {**base, **extra}


def parse_index(text: str) -> list[dict[str, Any]]:
    articles, provider = [], "kb"
    for line in text.splitlines():
        if line.startswith("#"):
            if "(kb/" in line:
                provider = line.split("(kb/", 1)[1].split("/", 1)[0]
            continue
        parts = [p.strip() for p in line.split(" · ")]
        if len(parts) < 2 or not parts[0].endswith(".md"):
            continue
        labelled = {p.split(":", 1)[0].strip(): p.split(":", 1)[1].strip()
                    for p in parts[4:] if ":" in p}
        articles.append(article(
            f"A{len(articles) + 1}", "kb", f"/kb/{provider}/{parts[0]}", parts[1],
            keywords=[k.strip() for k in parts[2].split(",") if k.strip()] if len(parts) > 2 else [],
            aliases=[a.strip() for a in labelled.get("aliases", "").split(",") if a.strip()],
            summary=parts[3] if len(parts) > 3 else "", collection=labelled.get("collection"),
            area=labelled.get("area"), deprecated="DEPRECATED" in parts))
    return articles


def at_blocks(text: str) -> dict[str, list[str]]:
    """`@@ <path>` header + its following lines — the shape both console readers emit."""
    blocks: dict[str, list[str]] = {}
    current = ""
    for line in text.splitlines():
        if line.startswith("@@ "):
            current = line[3:].strip()
            blocks.setdefault(current, [])
        elif current:
            blocks[current].append(line)
    return blocks


def parse_meta(text: str) -> dict[str, dict[str, str]]:
    meta = {}
    for path, lines in at_blocks(text).items():
        pairs = (line.partition(":") for line in lines if ":" in line)
        meta[path] = {k.strip(): v.strip().strip("'\"") for k, _, v in pairs
                      if k.strip() in ("url", "updated_at")}
    return meta


def inventory(brain_root: Path) -> tuple[list[dict], dict]:
    index = console(brain_root, KB_INDEX_SCRIPT)
    articles = parse_index(str(index.get("stdout") or "")) if index.get("exit_code") == 0 else []
    partial, reason = bool(index.get("stdout_truncated")), None
    if index.get("exit_code") != 0:
        reason = clip(index.get("stderr") or "no /kb mount", 160)
    elif partial:
        reason = "INDEX output truncated at the console cap"
    if articles:
        meta_call = console(brain_root, KB_META_SCRIPT)
        meta = parse_meta(str(meta_call.get("stdout") or ""))
        partial = partial or bool(meta_call.get("stdout_truncated"))
        for article in articles:
            found = meta.get(article["path"]) or {}
            article["url"] = found.get("url") or None
            article["updated_at"] = found.get("updated_at") or None
    knowledge = console(brain_root, BRAIN_KNOWLEDGE_SCRIPT)
    docs = parse_brain_docs(str(knowledge.get("stdout") or ""), len(articles))
    status = "unavailable" if not articles else ("partial" if partial else "complete")
    return articles + docs, {"status": status, "articles": len(articles), "brain_docs": len(docs),
                             "reason": reason}


def parse_brain_docs(text: str, offset: int) -> list[dict[str, Any]]:
    """`@@ <path>` + the file's first `# ` heading, as emitted by BRAIN_KNOWLEDGE_SCRIPT."""
    docs: list[dict[str, Any]] = []
    for path, lines in at_blocks(text).items():
        title = next((line[2:].strip() for line in lines if line.startswith("# ")), "")
        if title:
            docs.append(article(f"A{offset + len(docs) + 1}", "brain", path, title))
    return docs


# ------------------------------------------------------------------ main


def pick_source(rc: Rc, brain_root: Path, raw_dir: Path, args, start, end):
    if args.corpus:
        return "harvest", *from_corpus(Path(args.corpus).expanduser(), start, end)
    payload = rc.json("fleet", "runs", "--days", str(args.days), "--kind", "email")
    rows = payload.get("runs") if isinstance(payload, dict) else payload
    runs = [r for r in (rows or []) if isinstance(r, dict) and not r.get("simulation")
            and corpus.in_window(r.get("created_at"), start, end)]
    if runs:
        return "email_runs", *from_runs(rc, runs, start, end)
    boxes = rc.json("project", "mailbox", "ls")
    boxes = boxes.get("mailboxes") if isinstance(boxes, dict) else boxes
    providers = {str(b.get("provider") or "") for b in (boxes or []) if isinstance(b, dict)}
    if "helpscout" in providers:
        return "helpscout", *from_helpscout(brain_root, raw_dir, start, end)
    print(f"no email runs in the window and no helpscout mailbox (providers: "
          f"{', '.join(sorted(providers)) or 'none'}). v1 reads email runs or Help Scout only — "
          f"harvest this mailbox and pass the export with --corpus dump.txt (recipe in SKILL.md).",
          file=sys.stderr)
    raise SystemExit(2)


def main() -> int:
    args = parse_args()
    started = time.monotonic()
    brain_root = find_brain_root()
    end = datetime.now(timezone.utc)
    start = (end - timedelta(days=max(1, args.days) - 1)).replace(
        hour=0, minute=0, second=0, microsecond=0)
    out_dir = Path(args.out).expanduser() if args.out else (
        brain_root / ".rootcause" / "helpcenter" / end.date().isoformat())
    raw_dir = out_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    rc = Rc(raw_dir=raw_dir, cwd=brain_root)

    source, conversations, coverage = pick_source(rc, brain_root, raw_dir, args, start, end)
    conversations.sort(key=lambda c: str(c.get("created_at")))
    articles, kb = inventory(brain_root)
    corpus.link_articles(conversations, articles)

    evidence = {
        "schema_version": 1, "project": project_name(brain_root), "collected_at": end.isoformat(),
        "window": {"start": start.isoformat(), "end": end.isoformat(), "days": args.days},
        "source": source, "kb": kb, "conversations": conversations, "articles": articles,
        "coverage": [coverage, cover("kb", kb["status"], kb["articles"], kb["articles"],
                                     kb["reason"])],
    }
    payload = json.dumps(evidence, ensure_ascii=False, indent=1, sort_keys=True) + "\n"
    (out_dir / "evidence.json").write_text(payload, encoding="utf-8")
    digest = corpus.write_digest(evidence)
    (out_dir / "digest.md").write_text(digest, encoding="utf-8")
    sha = hashlib.sha256(payload.encode("utf-8")).hexdigest()

    print(f"{evidence['project']} {start.date()}→{end.date()} · source {source} "
          f"({coverage['status']}) · {len(conversations)} conversations · "
          f"{kb['articles']} kb articles + {kb['brain_docs']} brain docs ({kb['status']}) · "
          f"{rc.calls} rc calls · {len(rc.errors)} errors · {time.monotonic() - started:.1f}s")
    print(f"evidence_sha256 {sha}   (copy into suggestions.json)")
    broken = not conversations and any(c["status"] != "complete" for c in evidence["coverage"])
    if broken:
        print("no conversations collected and a feed was incomplete — see coverage in evidence.json",
              file=sys.stderr)
    print(out_dir)
    return 1 if broken else 0


if __name__ == "__main__":
    raise SystemExit(main())
