# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Collect one window of customer questions + the help-centre inventory into `evidence.json`.

    uv run skills/brain-helpcenter-suggestions/scripts/collect.py --days 8 [--tenant SLUG] [--out DIR]

One report = one help centre = one owner: a project whose runs carry tenants must be collected per
tenant (`--tenant`), and the inventory is then the tenant's own `/kb/tenant/**` help centre.

Writes `<brain>/.rootcause/helpcenter/<window end date>[-<tenant>]/`:
  conversations.tsv / articles.tsv  the read-whole tier
  digest.md                         the drill tier (full bodies)
  evidence.json                     the judge contract (its sha256 goes into suggestions.json)
  raw/                              rc + console artifact cache, keyed by argv / page number

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
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from posixpath import dirname
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
OUT = "/tmp/rootcause-out"
# Verified 2026-09-08 (iBeauty, pro-backup): console stdout caps at 64 KiB — a Help Scout page is
# ~270 KB and a 190-article INDEX overflows too, so anything bulky goes through an artifact file.
HS_PAGE_SCRIPT = (
    "import json,subprocess,os; os.makedirs('/tmp/rootcause-out',exist_ok=True); "
    "r=subprocess.run(['python','-m','lib.api','get','helpscout','/conversations',"
    "'--query','status=all','--query','embed=threads','--query','page={page}',"
    "'--query','query=(createdAt:[{start} TO {end}])'],capture_output=True,text=True); "
    "p=json.loads(r.stdout); "
    "[c['_embedded'].update(threads=[t for t in c['_embedded'].get('threads',[]) if t.get('type')!='note']) "
    "for c in p.get('_embedded',{{}}).get('conversations',[]) if c.get('_embedded')]; "  # Beacon notes carry IP + browsing history
    "json.dump(p,open('/tmp/rootcause-out/hc-page-{page}.json','w')); "
    "print(json.dumps(p.get('page')))"
)
FIND_INDEXES = "find /kb -maxdepth 4 -name INDEX.md 2>/dev/null"
BRAIN_KNOWLEDGE_SCRIPT = (
    "find /brain/knowledge -name '*.md' -exec sh -c 'echo \"@@ $1\"; grep -m1 \"^# \" \"$1\"' _ {} \\;"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=7, help="window length in days (default 7)")
    parser.add_argument("--tenant", help="tenant slug: filters runs and scopes /kb to that tenant")
    parser.add_argument("--out", help="output directory (default .rootcause/helpcenter/<end date>)")
    return parser.parse_args()


def cover(feed: str, status: str, scanned: int, retained: int, reason: str | None = None) -> dict:
    return {"feed": feed, "status": status, "scanned": scanned, "retained": retained,
            "reason": clip(reason, 300) or None}


def _rc_console(brain_root: Path, tenant: str | None, *args: str) -> subprocess.CompletedProcess:
    """`--tenant` goes ONLY on console calls: RC_TENANT would also scope `rc fleet runs` to nothing."""
    argv = ["rc", "dev", "console"] + (["--tenant", tenant] if tenant else []) + list(args)
    return subprocess.run(argv, capture_output=True, text=True, timeout=CONSOLE_TIMEOUT, cwd=brain_root)


def console(brain_root: Path, script: str, tenant: str | None = None) -> dict[str, Any]:
    """`rc dev console bash run` envelope: {stdout, stderr, exit_code, stdout_truncated}."""
    try:
        done = _rc_console(brain_root, tenant, "bash", "run", "-o", "json", "--raw-output", script)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"exit_code": -1, "stderr": f"{type(exc).__name__}: {exc}", "stdout": ""}
    docs = [d for d in json_objects(done.stdout) if isinstance(d, dict) and "exit_code" in d]
    return docs[-1] if docs else {"exit_code": done.returncode or -1, "stdout": "",
                                  "stderr": (done.stderr or "no console envelope").strip()}


def spill(brain_root: Path, target: Path, remote: str, tenant: str | None = None) -> str:
    """Fetch a workspace artifact the console wrote (stdout caps at 64 KiB, files do not)."""
    done = _rc_console(brain_root, tenant, "file", "get", remote, "--out", str(target))
    if done.returncode != 0 or not target.exists():
        return ""
    return target.read_text(encoding="utf-8", errors="replace")


def at_blocks(text: str) -> dict[str, list[str]]:
    """`@@ <path>` header + its following lines — the shape every console reader here emits."""
    blocks: dict[str, list[str]] = {}
    current = ""
    for line in text.splitlines():
        if line.startswith("@@ "):
            current = line[3:].strip()
            blocks.setdefault(current, [])
        elif current:
            blocks[current].append(line)
    return blocks


# ------------------------------------------------------------------ sources


def from_runs(rc: Rc, runs: list[dict], start: datetime, end: datetime, tenant: str | None,
              domains: frozenset[str], simulations: int) -> tuple[list[dict], dict]:
    """Trace headers in parallel; runs on one thread — or on one identical mail — collapse into one."""
    def header(run: dict) -> dict | None:
        records = rc.jsonl("run", "trace", str(run.get("run_id") or run.get("id")), "--stream")
        return records[0] if records else None

    headers = [h for h in parallel(runs, guarded(rc, "runs", lambda r: str(r.get("run_id")), header)) if h]
    tenants = Counter(str(h.get("tenant") or "") for h in headers)
    if not tenant and tenants.keys() - {""}:
        require_tenant(tenants)
    kept = [h for h in headers if not tenant or str(h.get("tenant") or "") == tenant]
    by_thread: dict[str, dict] = {}
    for head in sorted(kept, key=lambda h: str(h.get("created_at") or "")):
        by_thread.setdefault(str(head.get("thread_id") or head.get("run_id") or id(head)), head)
    built = [c for c in (corpus.trace_conversation(h, domains) for h in by_thread.values()) if c]
    in_window = [c for c in built if corpus.in_window(c["created_at"], start, end)]
    conversations = corpus.merge_duplicates(in_window)
    corpus.tag_duplicate_outreach(conversations)
    others = " · ".join(f"{count} other tenant ({name})" for name, count in tenants.items()
                        if name and name != tenant)
    parts = [f"{len(conversations)} kept", others,
             f"{len(by_thread) - len(built)} no inbound text",
             f"{len(runs) - len(headers)} trace unavailable",
             f"{len(kept) - len(by_thread)} same thread",
             f"{len(in_window) - len(conversations)} duplicate mail",
             f"{simulations} simulation"]
    reason = f"{len(runs)} email runs in window: " + " · ".join(
        p for p in parts if p and not p.startswith("0 "))
    status = "complete" if len(headers) == len(runs) else "partial"
    return conversations, cover("email_runs", status, len(runs), len(conversations), reason)


def require_tenant(tenants: Counter) -> None:
    listing = " · ".join(f"{name}: {count}" for name, count in tenants.most_common())
    print(f"this project's runs are tenant-scoped ({listing}) and one report = one help centre = "
          f"one owner. Re-run per tenant: collect.py --tenant <slug>", file=sys.stderr)
    raise SystemExit(2)


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
            if not spill(brain_root, target, f"{OUT}/hc-page-{number}.json"):
                failed = f"page {number}: file get failed"
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
    corpus.tag_duplicate_outreach(conversations)
    coverage.update(scanned=scanned, retained=len(conversations),
                    status="unavailable" if not pages else coverage["status"],
                    reason=coverage["reason"] or f"{scanned} conversations on {len(pages)} page(s): "
                    f"{len(conversations)} kept · {scanned - len(seen)} outside the window · "
                    f"{len(seen) - len(conversations)} merged chat fragments")
    return conversations, coverage


# ------------------------------------------------------------------ inventory


def article(aid: str, home: str, path: str, title: str, **extra: Any) -> dict[str, Any]:
    base = {"id": aid, "home": home, "path": path, "title": title, "url": None, "keywords": [],
            "aliases": [], "summary": "", "collection": None, "area": None, "audience": None,
            "updated_at": None, "deprecated": False}
    return {**base, **extra}


def parse_indexes(text: str) -> list[dict[str, Any]]:
    """`@@ /kb/<…>/INDEX.md` blocks -> articles. The path is the truth; the header comment is not."""
    articles: list[dict[str, Any]] = []
    for index_path, lines in at_blocks(text).items():
        root = dirname(index_path)
        for line in lines:
            parts = [p.strip() for p in line.split(" · ")]
            if line.startswith("#") or len(parts) < 2 or not parts[0].endswith(".md"):
                continue
            labelled = {p.split(":", 1)[0].strip(): p.split(":", 1)[1].strip()
                        for p in parts[4:] if ":" in p}
            articles.append(article(
                f"A{len(articles) + 1}", "kb", f"{root}/{parts[0]}", parts[1],
                keywords=[k.strip() for k in parts[2].split(",") if k.strip()] if len(parts) > 2 else [],
                aliases=[a.strip() for a in labelled.get("aliases", "").split(",") if a.strip()],
                summary=parts[3] if len(parts) > 3 else "", collection=labelled.get("collection"),
                area=labelled.get("area"), audience=labelled.get("audience"),
                deprecated="DEPRECATED" in parts))
    # Ids follow path order, not INDEX order: stable across runs while the KB is unchanged.
    articles.sort(key=lambda a: a["path"])
    for number, entry in enumerate(articles, 1):
        entry["id"] = f"A{number}"
    return articles


def inventory(brain_root: Path, raw_dir: Path, tenant: str | None) -> tuple[list[dict], dict]:
    """One help centre: the tenant's `/kb/tenant/**` KBs, or every project-level KB. Never mixed."""
    found = console(brain_root, FIND_INDEXES, tenant)
    indexes = [p for p in str(found.get("stdout") or "").split()
               if p.startswith("/kb/tenant/") == bool(tenant)]
    if not indexes:
        scope = f"tenant {tenant} has no public help centre mounted at /kb/tenant" if tenant else \
            "no /kb INDEX.md outside /kb/tenant"
        print(f"{scope} — no report ({clip(found.get('stderr'), 120) or 'find returned nothing'})",
              file=sys.stderr)
        raise SystemExit(2)
    roots = sorted({dirname(p) for p in indexes})
    listed = " ".join(shlex.quote(p) for p in indexes)
    count_cmd = "; ".join(f"echo \"## {r} $(find {shlex.quote(r)} -name '*.md' ! -name INDEX.md "
                          f"| wc -l)\"" for r in roots)
    read = console(brain_root, f"mkdir -p {OUT}; for f in {listed}; do echo \"@@ $f\"; cat \"$f\"; "
                               f"done > {OUT}/hc-index.txt; {count_cmd}", tenant)
    articles = parse_indexes(spill(brain_root, raw_dir / "hc-index.txt", f"{OUT}/hc-index.txt", tenant))
    on_disk = sum(int(line.split()[-1]) for line in str(read.get("stdout") or "").splitlines()
                  if line.startswith("## ") and line.split()[-1].isdigit())
    if not articles:
        print(f"no articles parsed from {len(indexes)} INDEX.md under {', '.join(roots)} "
              f"({clip(read.get('stderr'), 160) or 'empty artifact'}) — a report on an empty "
              f"inventory would be confidently wrong", file=sys.stderr)
        raise SystemExit(2)
    console(brain_root, f"for f in $(find {' '.join(shlex.quote(r) for r in roots)} -name '*.md' "
                        f"! -name INDEX.md); do echo \"@@ $f\"; sed -n '2,25p' \"$f\" "
                        f"| grep -E '^(url|updated_at|audience):'; done > {OUT}/hc-meta.txt", tenant)
    meta = parse_meta(spill(brain_root, raw_dir / "hc-meta.txt", f"{OUT}/hc-meta.txt", tenant))
    for item in articles:
        found_meta = meta.get(item["path"]) or {}
        item.update(url=found_meta.get("url") or None, updated_at=found_meta.get("updated_at") or None,
                    audience=item["audience"] or found_meta.get("audience") or None)
    docs = parse_brain_docs(str(console(brain_root, BRAIN_KNOWLEDGE_SCRIPT, tenant).get("stdout") or ""),
                            len(articles))
    partial = bool(on_disk) and on_disk != len(articles)
    reason = f"{len(articles)} indexed vs {on_disk} .md on disk under {', '.join(roots)}" \
        if partial else None
    return articles + docs, {"status": "partial" if partial else "complete", "root": ", ".join(roots),
                             "articles": len(articles), "brain_docs": len(docs), "reason": reason}


def parse_meta(text: str) -> dict[str, dict[str, str]]:
    meta = {}
    for path, lines in at_blocks(text).items():
        pairs = (line.partition(":") for line in lines if ":" in line)
        meta[path] = {k.strip(): v.strip().strip("'\"") for k, _, v in pairs
                      if k.strip() in ("url", "updated_at", "audience")}
    return meta


def parse_brain_docs(text: str, offset: int) -> list[dict[str, Any]]:
    """`@@ <path>` + the file's first `# ` heading, as emitted by BRAIN_KNOWLEDGE_SCRIPT."""
    docs: list[dict[str, Any]] = []
    for path, lines in at_blocks(text).items():
        title = next((line[2:].strip() for line in lines if line.startswith("# ")), "")
        if title:
            docs.append(article(f"A{offset + len(docs) + 1}", "brain", path, title))
    return docs


# ------------------------------------------------------------------ main


def mailboxes(rc: Rc) -> list[dict]:
    boxes = rc.json("project", "mailbox", "ls")
    boxes = boxes.get("mailboxes") if isinstance(boxes, dict) else boxes
    return [b for b in (boxes or []) if isinstance(b, dict)]


def pick_source(rc: Rc, brain_root: Path, raw_dir: Path, args, start, end):
    payload = rc.json("fleet", "runs", "--days", str(args.days), "--kind", "email")
    rows = [r for r in ((payload.get("runs") if isinstance(payload, dict) else payload) or [])
            if isinstance(r, dict) and corpus.in_window(r.get("created_at"), start, end)]
    runs = [r for r in rows if not r.get("simulation")]
    boxes = mailboxes(rc)
    if runs:
        # our own addresses: an `is_inbound` turn from one of these domains is still an agent
        domains = frozenset(str(b.get("email_address") or "").lower().rpartition("@")[2]
                            for b in boxes) - {""}
        return "email_runs", *from_runs(rc, runs, start, end, args.tenant, domains,
                                        len(rows) - len(runs))
    providers = sorted({str(b.get("provider") or "") for b in boxes})
    if "helpscout" in providers:
        return "helpscout", *from_helpscout(brain_root, raw_dir, start, end)
    print(f"no email runs in the window and no helpscout mailbox (providers: "
          f"{', '.join(providers) or 'none'}). v1 reads email runs or Help Scout only "
          f"— a recipe for this provider still has to be written (say so in learnings).",
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
        brain_root / ".rootcause" / "helpcenter"
        / (end.date().isoformat() + (f"-{args.tenant}" if args.tenant else "")))
    raw_dir = out_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    rc = Rc(raw_dir=raw_dir, cwd=brain_root)

    source, conversations, coverage = pick_source(rc, brain_root, raw_dir, args, start, end)
    conversations.sort(key=lambda c: str(c.get("created_at")))
    articles, kb = inventory(brain_root, raw_dir, args.tenant)
    corpus.link_articles(conversations, articles)

    evidence = {
        "schema_version": 1, "project": project_name(brain_root), "tenant": args.tenant,
        "collected_at": end.isoformat(),
        "window": {"start": start.isoformat(), "end": end.isoformat(), "days": args.days},
        "source": source, "kb": kb, "conversations": conversations, "articles": articles,
        "coverage": [coverage, cover("kb", kb["status"], kb["articles"], kb["articles"],
                                     kb["reason"])],
    }
    payload = json.dumps(evidence, ensure_ascii=False, indent=1, sort_keys=True) + "\n"
    (out_dir / "evidence.json").write_text(payload, encoding="utf-8")
    (out_dir / "digest.md").write_text(corpus.write_digest(evidence), encoding="utf-8")
    convs_tsv, articles_tsv = corpus.write_tsvs(evidence)
    (out_dir / "conversations.tsv").write_text(convs_tsv, encoding="utf-8")
    (out_dir / "articles.tsv").write_text(articles_tsv, encoding="utf-8")

    noisy = sum(1 for c in conversations if c.get("noise"))
    print(f"{evidence['project']}{'/' + args.tenant if args.tenant else ''} "
          f"{start.date()}→{end.date()} · source {source} ({coverage['status']}) · "
          f"{len(conversations)} conversations ({noisy} pre-tagged noise, "
          f"{sum(1 for c in conversations if (c.get('reply') or {}).get('provenance') == 'human')} "
          f"with a human reply) · "
          f"{kb['articles']} kb articles + {kb['brain_docs']} brain docs ({kb['status']}) · "
          f"{rc.calls} rc calls · {len(rc.errors)} errors · {time.monotonic() - started:.1f}s")
    print(f"coverage: {coverage['reason'] or '-'}")
    print("evidence_sha256 " + hashlib.sha256(payload.encode("utf-8")).hexdigest()
          + "   (copy into suggestions.json)")
    if not conversations:
        print("no conversations collected — see coverage in evidence.json", file=sys.stderr)
    print(out_dir)
    return 1 if not conversations else 0


if __name__ == "__main__":
    raise SystemExit(main())
