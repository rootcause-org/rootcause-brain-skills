# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Pure normalisers for `brain-helpcenter-suggestions`: provider payload -> one conversation shape.

    from corpus import helpscout_conversations, trace_conversation, dump_conversations, write_digest

Every entry point returns the `evidence.json` `conversations[]` contract (see suggestions_schema.md):
id, url, channel, created_at, subject, customer, first_message, reply{text,by,provenance}|null,
later[], truncated, tags. Stdlib only, no I/O, no rc: collect.py owns the network, this owns the text.
"""

from __future__ import annotations

import html as html_mod
import re
from datetime import datetime, timedelta, timezone
from typing import Any

FIRST_LIMIT, REPLY_LIMIT, LATER_LIMIT, LATER_MAX = 4000, 3000, 800, 12
MERGE_WINDOW_MIN = 60  # Help Scout Beacon splits one chat into several conversations
_ARTICLE_URL = re.compile(r"/article/([0-9a-f]{24}|\d+)\b")
HS_URL = "https://secure.helpscout.net/conversation/{cid}/{number}/"
DROP_THREADS = {"note", "lineitem"}
CUSTOMER_THREADS = {"customer", "beaconchat"}

_BLOCK = re.compile(r"(?i)<\s*(br|/p|/div|/tr|/li|/h[1-6]|/table)\b[^>]*>")
_TAG = re.compile(r"<[^>]+>")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_QUOTED = re.compile(
    r"(?i)^\s*(>|on\b.{0,140}\bwrote:|op\b.{0,140}\bschreef|le\b.{0,140}\ba écrit|"
    r"-{2,}\s*original message|-{3,}\s*forwarded message|van:\s|from:\s|verzonden:\s)"
)
_SIGNATURE = re.compile(
    r"(?i)^\s*(--\s*$|__+\s*$|met vriendelijke groet|vriendelijke groet|met dank en vriendelijke|"
    r"mvg\b|m\.v\.g\b|groetjes|groeten\s*[,.]?\s*$|kind regards|best regards|met warme groet)"
)


def clip(text: Any, limit: int) -> str:
    value = str(text or "").strip()
    return value if len(value) <= limit else value[:limit].rstrip() + " …"


def _collapse(lines: list[str]) -> str:
    out: list[str] = []
    for line in lines:
        line = line.rstrip()
        if not line and (not out or not out[-1]):
            continue
        out.append(line)
    return "\n".join(out).strip()


def html_to_text(value: Any) -> str:
    """Body html -> plain text: block tags become newlines, the rest is dropped and unescaped."""
    text = _BLOCK.sub("\n", str(value or ""))
    text = html_mod.unescape(_TAG.sub(" ", text)).replace("\xa0", " ")
    return _collapse([re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()])


def strip_quoted_and_signature(text: Any) -> str:
    """Drop the quoted history tail (`Op … schreef`, `>` lines) and a short signature block."""
    lines = str(text or "").splitlines()
    for index, line in enumerate(lines):
        if _QUOTED.match(line):
            lines = lines[:index]
            break
    for index in range(len(lines) - 1, -1, -1):
        tail = [line for line in lines[index + 1:] if line.strip()]
        # A signature is either short or made of signature-shaped lines (name, phone, address,
        # a bare url); a long prose paragraph after `Groeten` is a real message and must survive.
        if _SIGNATURE.match(lines[index]) and (
                len(tail) <= 6 or all(len(t) <= 60 or " " not in t.strip() for t in tail)):
            lines = lines[:index]
    return _collapse(lines)


def clean(value: Any, limit: int) -> str:
    return clip(_EMAIL.sub("[e-mail]", strip_quoted_and_signature(html_to_text(value))), limit)


def in_window(value: Any, start: datetime, end: datetime) -> bool:
    """`created_at` is provider-shaped (`…Z`, naive, or offset); unparseable falls out of scope."""
    try:
        moment = datetime.fromisoformat(str(value or "").strip().replace("Z", "+00:00"))
    except ValueError:
        return False
    return start <= (moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)) < end


def build(cid, url, channel, created_at, subject, customer, tags, turns, provenance="human"):
    """turns = chronological [(role, text, by)]. Returns None when no customer turn survives."""
    turns = [(role, text, by) for role, text, by in turns if text]
    start = next((i for i, turn in enumerate(turns) if turn[0] == "customer"), None)
    if start is None:
        return None
    reply, later = None, []
    for role, text, by in turns[start + 1:]:
        if reply is None and role == "agent":
            reply = {"text": clip(text, REPLY_LIMIT), "by": by or None, "provenance": provenance}
        else:
            later.append({"role": role, "text": clip(text, LATER_LIMIT)})
    truncated = len(later) > LATER_MAX
    return {
        "id": cid, "url": url, "channel": channel, "created_at": created_at,
        "subject": subject or None, "customer": customer or None,
        "first_message": clip(turns[start][1], FIRST_LIMIT), "reply": reply,
        "later": (later[:6] + later[-6:]) if truncated else later, "truncated": truncated,
        "linked_articles": [],
        "tags": [str(t.get("tag") if isinstance(t, dict) else t) for t in tags or []],
    }


def merge_split_chats(conversations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Same customer, next conversation opened within MERGE_WINDOW_MIN: one conversation, first id/url."""
    out: list[dict[str, Any]] = []
    for conv in sorted(conversations, key=lambda c: str(c["created_at"])):
        prev = out[-1] if out else None
        try:
            gap = (datetime.fromisoformat(conv["created_at"].replace("Z", "+00:00"))
                   - datetime.fromisoformat(prev["created_at"].replace("Z", "+00:00"))) if prev else None
        except (ValueError, TypeError):
            gap = None
        if prev and conv.get("customer") and conv["customer"] == prev.get("customer") and gap is not None \
                and gap <= timedelta(minutes=MERGE_WINDOW_MIN):
            turns = [{"role": "customer", "text": conv["first_message"]}]
            if conv.get("reply"):
                turns.append({"role": "agent", "text": conv["reply"]["text"]})
            prev["later"] = prev["later"] + turns + conv["later"]
            if prev.get("reply") is None and conv.get("reply"):
                prev["reply"] = conv["reply"]
            prev["tags"] = prev["tags"] + [f"merged:{conv['id']}"]
            continue
        out.append(conv)
    return out


def link_articles(conversations: list[dict[str, Any]], articles: list[dict[str, Any]]) -> None:
    """Help-centre URLs the human pasted in a reply → `linked_articles` (proof the content exists)."""
    by_number: dict[str, str] = {}
    for a in articles:
        for key in (a.get("url") or "", a.get("path") or ""):
            m = _ARTICLE_URL.search(key) or re.search(r"/([0-9a-f]{24})-", key)
            if m:
                by_number[m.group(1)] = a["id"]
    for conv in conversations:
        texts = [conv["reply"]["text"]] if conv.get("reply") else []
        texts += [t["text"] for t in conv["later"] if t["role"] == "agent"]
        found = [by_number[m.group(1)] for text in texts for m in _ARTICLE_URL.finditer(text)
                 if m.group(1) in by_number]
        conv["linked_articles"] = sorted(set(found), key=lambda a: int(a[1:]))


def helpscout_conversations(page_json: Any) -> list[dict[str, Any]]:
    """Accepts the raw API page (`_embedded.conversations[]`) and the flattened harvest list."""
    items = (((page_json.get("_embedded") or {}).get("conversations")) or []
             if isinstance(page_json, dict) else page_json or [])
    built = (_helpscout_one(c) for c in items if isinstance(c, dict))
    return [conv for conv in built if conv]


def _helpscout_one(conv: dict[str, Any]) -> dict[str, Any] | None:
    threads = (conv.get("_embedded") or {}).get("threads") or conv.get("threads") or []
    threads = sorted(threads, key=lambda t: str(t.get("createdAt") or t.get("at") or ""))
    turns, beacon = [], False
    for thread in threads:
        kind = str(thread.get("type") or "")
        if kind in DROP_THREADS:
            continue
        beacon = beacon or kind == "beaconchat"
        if kind in CUSTOMER_THREADS:
            role = "customer"
        elif kind == "message":
            role = "agent"
        else:
            continue
        by = thread.get("first") or (thread.get("createdBy") or {}).get("first")
        turns.append((role, clean(thread.get("body"), FIRST_LIMIT), by))
    source = str((conv.get("source") or {}).get("type") or "")
    chat = str(conv.get("type") or "") == "chat" or (source.startswith("beacon") and beacon)
    primary = conv.get("primaryCustomer")
    customer = (primary.get("first") if isinstance(primary, dict) else None) or next(
        (by for role, _, by in turns if role == "customer" and by), None)
    return build(
        f"hs:{conv.get('id')}",
        HS_URL.format(cid=conv.get("id"), number=conv.get("number")),
        "chat" if chat else "email",
        str(conv.get("createdAt") or conv.get("created_at") or ""),
        conv.get("subject"),
        customer,
        conv.get("tags"),
        turns,
    )


def trace_conversation(header: dict[str, Any]) -> dict[str, Any] | None:
    """First JSONL record of `rc run trace --stream` -> conversation (draft = the reply)."""
    prior = [m for m in header.get("prior_messages") or [] if isinstance(m, dict)]
    turns = [("agent" if m.get("is_inbound") is False else "customer",
              clean(m.get("body"), FIRST_LIMIT), m.get("sender")) for m in prior]
    turns.append(("customer", clean(header.get("question"), FIRST_LIMIT), None))
    # An earlier human turn is the reply when there is one; otherwise the reply is this run's draft.
    provenance = "human" if any(role == "agent" for role, _, _ in turns) else "draft"
    draft = clean(header.get("draft"), REPLY_LIMIT)
    return build(
        f"run:{str(header.get('run_id') or '')[:8]}",
        (header.get("metadata") or {}).get("run_url"), "email",
        str(header.get("created_at") or ""), header.get("topic"),
        next((m.get("sender") for m in prior if m.get("is_inbound") is not False), None),
        [], turns + ([("agent", draft, None)] if draft else []), provenance,
    )


_DUMP_HEAD = re.compile(r"^#{3,}\s*(\S+)\s*\|(.*)$")
_DUMP_TURN = re.compile(r"^\[([a-z]+)(?:\|([^\]]*))?\]\s?(.*)$")


def dump_conversations(text: str) -> list[dict[str, Any]]:
    """The `dump.txt` block format produced by a harvest run. No provider URLs exist there."""
    out: list[dict[str, Any]] = []
    head: dict[str, Any] | None = None
    turns: list[tuple[str, str, str | None]] = []
    skipping = False  # inside a dropped `[note|…]` block: its continuation lines go too

    def flush() -> None:
        built = head and build(
            head["id"], None, head["channel"], head["created_at"], head["subject"],
            next((by for role, _, by in turns if role == "customer"), None), head["tags"],
            [(r, clean(t, FIRST_LIMIT), b) for r, t, b in turns])
        if built:
            out.append(built)

    for line in str(text or "").splitlines():
        header = _DUMP_HEAD.match(line)
        if header:
            flush()
            head, turns, skipping = _dump_header(header.group(1), header.group(2)), [], False
            continue
        turn = _DUMP_TURN.match(line)
        if turn and head:
            kind = turn.group(1)
            skipping = kind in DROP_THREADS
            if not skipping:
                turns.append(("customer" if kind == "customer" else "agent",
                              turn.group(3), turn.group(2) or None))
        elif turns and head and line.strip() and not skipping:
            role, body, by = turns[-1]
            turns[-1] = (role, f"{body}\n{line}", by)
    flush()
    return out


def _dump_header(cid: str, rest: str) -> dict[str, Any]:
    parts = [p.strip() for p in rest.split("|")]
    fields = {p.split("=", 1)[0]: p.split("=", 1)[1] for p in parts if "=" in p}
    when = next((p for p in parts if re.match(r"^\d{4}-\d{2}-\d{2}", p)), "")
    subject = fields.get("subj", "")
    return {"id": f"harvest:{cid}",
            "channel": "chat" if parts and parts[0] == "chat" else "email",
            "created_at": when.split(" ")[0].strip(),
            "subject": None if subject in ("", "None") else subject,
            "tags": [t.strip() for t in fields.get("tags", "").strip("[]").split(",") if t.strip()]}


def _article_line(article: dict[str, Any]) -> str:
    bits = [article["id"], article.get("title") or "?"]
    bits += [", ".join(article[key]) for key in ("keywords", "aliases") if article.get(key)]
    bits += [str(article[key]) for key in ("summary", "collection", "path") if article.get(key)]
    return " · ".join(bits + (["DEPRECATED"] if article.get("deprecated") else []))


def write_digest(evidence: dict[str, Any]) -> str:
    """The one file the judge reads whole: conversation blocks + the article inventory."""
    window, kb = evidence.get("window") or {}, evidence.get("kb") or {}
    convs, articles = evidence.get("conversations") or [], evidence.get("articles") or []
    public = [a for a in articles if a.get("home") == "kb"]
    brain = [a for a in articles if a.get("home") == "brain"]
    coverage = " · ".join(f"{c.get('feed')}={c.get('status')}({c.get('retained')}/{c.get('scanned')})"
                          for c in evidence.get("coverage") or [])
    out = [f"# Help centre evidence — {evidence.get('project')}", "",
           f"window {str(window.get('start'))[:16]} → {str(window.get('end'))[:16]} "
           f"({window.get('days')}d) · source {evidence.get('source')} · {len(convs)} conversations "
           f"· {len(public)} kb articles ({kb.get('status')})",
           f"coverage: {coverage or 'none'}", "", "## Conversations", ""]
    for conv in convs:
        reply = conv.get("reply")
        out += [f"##### {conv['id']} | {conv['channel']} | {str(conv.get('created_at'))[:16]} | "
                f"subj={conv.get('subject') or '-'} | {conv.get('customer') or '-'} | "
                f"{'↗' if conv.get('url') else 'no-link'}"
                + (f" | linked {','.join(conv['linked_articles'])}" if conv.get('linked_articles') else ''),
                f"[customer] {conv['first_message']}"]
        if reply:
            out.append(f"[agent|{reply.get('by') or reply.get('provenance')}] {reply['text']}")
        out += [f"[{turn['role']}] {turn['text']}" for turn in conv.get("later") or []]
        out += (["[…] middle turns omitted"] if conv.get("truncated") else []) + [""]
    out += [f"## Articles ({len(public)})", ""] + [_article_line(a) for a in public]
    out += ["", f"## Brain knowledge ({len(brain)})", ""]
    return "\n".join(out + [f"{a['id']} · {a.get('title') or a.get('path')}" for a in brain]) + "\n"
