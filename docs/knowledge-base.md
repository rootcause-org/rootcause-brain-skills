# Knowledge Base Traversal

Use this when a brain has an external knowledge source mounted at `/kb`, or when a brain commits a
`knowledge/` directory. It is for read-only discovery from `rc bash`; durable routing belongs in the
brain's own skills/playbooks after you learn which articles matter.

## Mounts And Inventory

Hosted runs may have three relevant read-only trees:

| Path | Meaning |
|---|---|
| `/brain` | Committed brain ref. |
| `/kb` | Synced external knowledge-base snapshot, if configured. |
| `/brain/knowledge` | Committed project knowledge directory, if that brain uses one. |

Probe before assuming a shape:

```bash
rc bash run 'find /kb -maxdepth 3 -type d -print | sed -n "1,120p"'
rc bash run 'find /kb -type f | sed -n "1,80p"'
rc bash run 'test -d /brain/knowledge && find /brain/knowledge -type f | sed -n "1,80p" || true'
```

## Body Search

Use `rg` for broad discovery, then `sed` or `python` for one article:

```bash
rc bash run 'rg -n -i "refund|invoice|payment|receipt" /kb /brain/knowledge -g "*.md" 2>/dev/null | sed -n "1,60p"'
rc bash run 'sed -n "1,180p" /kb/provider/collection/article.md'
```

Search customer words first, then product vocabulary from the brain's terminology/source docs. Keep
results capped; KB snapshots can be large.

## Frontmatter Index

Many KB exports write YAML frontmatter above the article body. Inventory the real keys before filtering:

```bash
rc bash run 'python - <<'"'"'PY'"'"'
from collections import Counter
from pathlib import Path

keys = Counter()
for root in (Path("/kb"), Path("/brain/knowledge")):
    if not root.exists():
        continue
    for p in root.rglob("*.md"):
        text = p.read_text(errors="replace")
        if not text.startswith("---\n"):
            continue
        frontmatter = text.split("---", 2)[1]
        for line in frontmatter.splitlines():
            if ":" in line and not line.startswith((" ", "-")):
                keys[line.split(":", 1)[0]] += 1

for key, count in keys.most_common():
    print(f"{count:4} {key}")
PY'
```

Build a title index:

```bash
rc bash run 'python - <<'"'"'PY'"'"'
from pathlib import Path

for root in (Path("/kb"), Path("/brain/knowledge")):
    if not root.exists():
        continue
    for p in sorted(root.rglob("*.md")):
        text = p.read_text(errors="replace")
        title = ""
        if text.startswith("---\n"):
            frontmatter = text.split("---", 2)[1]
            for line in frontmatter.splitlines():
                if line.startswith("title:"):
                    title = line.split(":", 1)[1].strip().strip("\"'")
                    break
        if not title:
            for line in text.splitlines():
                if line.startswith("# "):
                    title = line[2:].strip()
                    break
        if title:
            print(f"{p}\t{title}")
PY'
```

Filter by common attributes such as `collection`, `status`, `keywords`, `summary`, `url`, or by path:

```bash
rc bash run 'python - <<'"'"'PY'"'"'
from pathlib import Path

terms = ("billing", "payment", "refund")
for root in (Path("/kb"), Path("/brain/knowledge")):
    if not root.exists():
        continue
    for p in sorted(root.rglob("*.md")):
        text = p.read_text(errors="replace")
        frontmatter = text.split("---", 2)[1] if text.startswith("---\n") else ""
        haystack = f"{p}\n{frontmatter}".lower()
        if any(term in haystack for term in terms):
            title = next(
                (line.split(":", 1)[1].strip().strip("\"'")
                 for line in frontmatter.splitlines()
                 if line.startswith("title:")),
                "",
            )
            print(f"{p}\t{title}")
PY'
```

## Choosing Evidence

Use KB articles for stable customer-facing guidance: setup steps, policy explanations, documented
limits, and public links. Use brain scripts and `rc db` for tenant-specific live state. Use source
mirrors for implementation truth. When an article answers the question, cite its frontmatter `url` if
present; when it conflicts with live data or source, report both and prefer live/source evidence.

## Quoting Notes

`rc bash run` takes one command string. For complex Python snippets, use a quoted heredoc as shown. If
the local shell says `unmatched "` or similar, the command did not reach RootCause; reduce quoting or
turn repeated logic into a committed brain script.
