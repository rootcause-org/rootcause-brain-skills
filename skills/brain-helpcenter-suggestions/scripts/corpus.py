# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Pure normalisers for `brain-helpcenter-suggestions`: provider payload -> one conversation shape.

    from corpus import helpscout_conversations, trace_conversation, dump_conversations, write_digest

Every entry point returns the `evidence.json` `conversations[]` contract (see suggestions_schema.md):
id, url, channel, tenant, created_at, subject, customer, first_message, first_raw, reply, later[],
noise, truncated, tags. Stdlib only, no I/O, no rc: collect.py owns the network, this owns the text.
"""

from __future__ import annotations

import html as html_mod
import re
from datetime import datetime, timedelta, timezone
from typing import Any

BODY_LIMIT, REPLY_LIMIT, LATER_LIMIT, LATER_MAX = 12000, 3000, 800, 12
MERGE_WINDOW_MIN = 60  # Beacon splits one chat; one runs thread can retrigger on the same mail
_ARTICLE_ID = re.compile(r"/articles?/([0-9a-f]{24}|\d+)")
_PATH_ID = re.compile(r"/([0-9a-f]{24}|\d+)-")
HS_URL = "https://secure.helpscout.net/conversation/{cid}/{number}/"
DROP_THREADS = {"note", "lineitem"}
CUSTOMER_THREADS = {"customer", "beaconchat"}

_BLOCK = re.compile(r"(?i)<\s*(br|/p|/div|/tr|/li|/h[1-6]|/table)\b[^>]*>")
_TAG = re.compile(r"<[^>]+>")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_WORD = re.compile(r"[^\W\d_]+", re.UNICODE)
_QUOTED = re.compile(
    r"(?i)^\s*(>|on\b.{0,140}\bwrote:|op\b.{0,140}\bschreef|le\b.{0,140}\ba écrit|"
    r"-{2,}\s*original message|-{3,}\s*forwarded message|van:\s|from:\s|verzonden:\s)"
)
_SIGNATURE = re.compile(
    r"(?i)^\s*(--\s*$|__+\s*$|met vriendelijke groet|vriendelijke groet|met dank en vriendelijke|"
    r"mvg\b|m\.v\.g\b|groetjes|groeten\s*[,.]?\s*$|kind regards|best regards|met warme groet)"
)

# Turns that are chat furniture, not an answer: bot greetings, flow prompts, auto-replies.
BOILERPLATE = (
    "ik ben yumi", "i'm yumi", "closing this chat", "this conversation has been closed",
    "chat gesloten", "we're away right now", "automated message", "auto-reply", "out of office",
    "bedankt voor je bericht, we", "waar gaat je vraag over", "waar kunnen we je mee helpen",
    "how can we help you today", "antwoordt meestal binnen", "is morgen weer beschikbaar",
    "om verder te chatten", "to continue this chat",
)
GREETINGS = {"hallo", "hoi", "hey", "hi", "hello", "dag", "goeiemorgen", "goedemorgen",
             "goeiedag", "goedemiddag", "goedenavond", "bonjour", "salut", "hola"}
_NO_REPLY = re.compile(r"(?i)(noreply|no-reply|donotreply|mailer-daemon|notifications@)")
_TEST = re.compile(r"(?i)\b(test(je)?|hgh|ping)\b")
_BOT_SENDER = re.compile(r"(?i)(operator\+[^@]*@intercom\.io|^fin@|noreply|no-reply|bot@)")
_INVITE_BODY = re.compile(r"(?i)(a new event has been scheduled|view event in calendly|invitee time zone"
                          r"|event name:.*\n.*invitee)")
_INVITE = re.compile(r"(?i)^\s*((updated |canceled |geannuleerd: )?invitation|uitnodiging)\s*:")


def clip(text: Any, limit: int) -> str:
    value = str(text or "").strip()
    return value if len(value) <= limit else value[:limit].rstrip() + " …"


def words(text: Any) -> list[str]:
    return _WORD.findall(str(text or ""))


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


def clean(value: Any, limit: int = BODY_LIMIT) -> str:
    return clip(_EMAIL.sub("[e-mail]", strip_quoted_and_signature(html_to_text(value))), limit)


def in_window(value: Any, start: datetime, end: datetime) -> bool:
    """`created_at` is provider-shaped (`…Z`, naive, or offset); unparseable falls out of scope."""
    return _moment(value) is not None and start <= _moment(value) < end  # type: ignore[operator]


def _moment(value: Any) -> datetime | None:
    try:
        moment = datetime.fromisoformat(str(value or "").strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


# ------------------------------------------------------------------ turn semantics


def is_boilerplate(text: str) -> bool:
    """Bot greetings, flow prompts and canned closers — dropped from the conversation entirely."""
    tokens = words(text)
    # Only short turns: "out of office" inside a 50-word follow-up is context, not an auto-reply.
    if len(tokens) <= 40 and any(phrase in text.lower() for phrase in BOILERPLATE):
        return True
    if len(tokens) >= 6:
        return False
    return text.rstrip().endswith("!") or bool(tokens) and tokens[0].lower() in GREETINGS \
        and len(tokens) <= 3


def looks_like_question(text: str) -> bool:
    """The first customer turn is often a menu click or a salutation; the question has shape."""
    return "?" in text or len(words(text)) >= 8


def noise_reason(subject: Any, sender: Any, first_message: str, has_ics: bool = False) -> str | None:
    """Obvious not-KB traffic, pre-tagged so the judge does not spend its reading budget on it."""
    if not first_message.strip():
        return "empty"
    if has_ics or _INVITE.match(str(subject or "")) or _INVITE_BODY.search(first_message):
        return "calendar_invite"
    if _NO_REPLY.search(str(sender or "")):
        return "no_reply_sender"
    if len(words(first_message)) <= 6 and _TEST.search(first_message):
        return "test"
    return None


def build(cid, url, channel, created_at, subject, customer, tags, turns, provenance="human",
          tenant=None, has_ics=False):
    """turns = chronological [(role, text, by)]. Returns None when no customer turn survives."""
    turns = [(role, text, by) for role, text, by in turns if text and not is_boilerplate(text)]
    customer_at = [i for i, turn in enumerate(turns) if turn[0] == "customer"]
    if not customer_at:
        return None
    # The question, not the opener: chat widgets start with a placeholder or a menu click.
    ask = next((i for i in customer_at if looks_like_question(turns[i][1])), customer_at[0])
    first_message = turns[ask][1]
    reply, later = None, []
    for role, text, by in turns[ask + 1:]:
        if reply is None and role == "agent":
            reply = {"text": clip(text, REPLY_LIMIT), "by": by or None, "provenance": provenance}
        else:
            later.append({"role": role, "text": clip(text, LATER_LIMIT)})
    truncated = len(later) > LATER_MAX
    reason = noise_reason(subject, customer, first_message, has_ics)
    return {
        "id": cid, "url": url, "channel": channel, "tenant": tenant, "created_at": created_at,
        "subject": subject or None, "customer": customer or None,
        "first_message": first_message,
        "first_raw": turns[customer_at[0]][1] if ask != customer_at[0] else None,
        "reply": reply, "later": (later[:6] + later[-6:]) if truncated else later,
        "truncated": truncated, "linked_articles": [],
        "noise": {"verdict": "not_kb", "reason": reason} if reason else None,
        "tags": [str(t.get("tag") if isinstance(t, dict) else t) for t in tags or []],
    }


def merge_split_chats(conversations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Same customer, next conversation opened within MERGE_WINDOW_MIN: one conversation, first id/url."""
    out: list[dict[str, Any]] = []
    for conv in sorted(conversations, key=lambda c: str(c["created_at"])):
        prev = out[-1] if out else None
        if prev and conv.get("customer") and conv["customer"] == prev.get("customer") \
                and _gap(prev, conv) is not None and _gap(prev, conv) <= MERGE_WINDOW_MIN:
            turns = [{"role": "customer", "text": clip(conv["first_message"], LATER_LIMIT)}]
            turns += [{"role": "agent", "text": conv["reply"]["text"]}] if conv.get("reply") else []
            prev["later"] = prev["later"] + turns + conv["later"]
            prev["reply"] = prev.get("reply") or conv.get("reply")
            prev["tags"] = prev["tags"] + [f"merged:{conv['id']}"]
            continue
        out.append(conv)
    return out


def merge_duplicates(conversations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One inbound mail can trigger several runs: same sender + byte-identical question, one hour."""
    out: list[dict[str, Any]] = []
    for conv in sorted(conversations, key=lambda c: str(c["created_at"])):
        key = (str(conv.get("customer") or ""), " ".join(conv["first_message"].split()))
        twin = next((c for c in reversed(out)
                     if (str(c.get("customer") or ""), " ".join(c["first_message"].split())) == key
                     and (_gap(c, conv) or 1e9) <= MERGE_WINDOW_MIN), None)
        if twin is not None:
            twin["tags"] = twin["tags"] + [f"merged:{conv['id']}"]
            continue
        out.append(conv)
    return out


def _gap(prev: dict[str, Any], conv: dict[str, Any]) -> float | None:
    before, after = _moment(prev.get("created_at")), _moment(conv.get("created_at"))
    return None if before is None or after is None else (after - before) / timedelta(minutes=1)


def tag_duplicate_outreach(conversations: list[dict[str, Any]]) -> None:
    """The same pitch from several senders is cold outreach, never a help-centre question."""
    seen: dict[str, set[str]] = {}
    for conv in conversations:
        seen.setdefault(" ".join(conv["first_message"].split()), set()).add(
            str(conv.get("customer") or conv["id"]))
    for conv in conversations:
        if conv.get("noise") is None and len(seen[" ".join(conv["first_message"].split())]) > 1:
            conv["noise"] = {"verdict": "not_kb", "reason": "duplicate_outreach"}


def link_articles(conversations: list[dict[str, Any]], articles: list[dict[str, Any]]) -> None:
    """Help-centre URLs the human pasted in a reply → `linked_articles` (proof the content exists)."""
    by_id: dict[str, str] = {}
    by_url: list[tuple[str, str]] = []
    for a in articles:
        url, path = str(a.get("url") or ""), str(a.get("path") or "")
        if url.startswith("http"):
            by_url.append((url.rstrip("/"), a["id"]))
        for match in (_ARTICLE_ID.search(url), _ARTICLE_ID.search(path), _PATH_ID.search(path)):
            if match:
                by_id.setdefault(match.group(1), a["id"])
                break
    for conv in conversations:
        human = conv.get("reply") and conv["reply"].get("provenance") == "human"
        texts = [conv["reply"]["text"]] + [t["text"] for t in conv["later"] if t["role"] == "agent"] \
            if human else []
        found = {by_id[m.group(1)] for text in texts for m in _ARTICLE_ID.finditer(text)
                 if m.group(1) in by_id}
        found |= {aid for url, aid in by_url if any(url in text for text in texts)}
        conv["linked_articles"] = sorted(found, key=lambda a: int(a[1:]))


# ------------------------------------------------------------------ providers


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
        turns.append((role, clean(thread.get("body")), by))
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
        conv.get("subject"), customer, conv.get("tags"), turns,
    )


def address(value: Any) -> str:
    """`Name <a@b.c>` / `a@b.c` / an opaque chat contact id -> a comparable lowercase key."""
    match = _EMAIL.search(str(value or ""))
    return (match.group(0) if match else str(value or "")).strip().strip("<>").lower()


def trace_conversation(header: dict[str, Any],
                       mailbox_domains: frozenset[str] = frozenset()) -> dict[str, Any] | None:
    """First JSONL record of `rc run trace --stream` -> conversation (draft = the reply).

    `is_inbound` is *delivery direction*, not authorship: a sibling vendor cc'd on the thread also
    arrives inbound. Only the originating sender and our own mailbox domains are provable — the rest
    is `unknown`, and the validator refuses customer quotes from unknown turns.
    """
    prior = [m for m in header.get("prior_messages") or [] if isinstance(m, dict)]
    origin = next((address(m.get("sender")) for m in prior if m.get("is_inbound") is not False), "")
    # An opaque chat contact id is the same person as the e-mail they hand over later in the widget.
    opaque = bool(origin) and "@" not in origin

    def role(message: dict[str, Any]) -> str:
        sender = address(message.get("sender"))
        if message.get("is_inbound") is False or sender.rpartition("@")[2] in mailbox_domains:
            return "agent"
        return "customer" if opaque or sender == origin else "unknown"

    turns = [(role(m), clean(m.get("body")), m.get("sender")) for m in prior]
    turns.append(("customer", clean(header.get("question")), None))
    provenance = "human" if any(r == "agent" for r, _, _ in turns) else "draft"
    draft = clean(header.get("draft"), REPLY_LIMIT)
    has_ics = any(str(a.get("filename") or a.get("mime_type") or "").lower().endswith(
        (".ics", "/calendar")) for m in prior for a in m.get("attachments") or [])
    conv = build(
        f"run:{str(header.get('run_id') or '')[:8]}",
        (header.get("metadata") or {}).get("run_url"), "email",
        str(header.get("created_at") or ""), header.get("topic"),
        next((m.get("sender") for m in prior if m.get("is_inbound") is not False), None),
        [], turns + ([("agent", draft, None)] if draft else []), provenance,
        tenant=header.get("tenant") or None, has_ics=has_ics,
    )
    if conv and conv.get("reply") and _BOT_SENDER.search(address(conv["reply"].get("by"))):
        conv["reply"]["provenance"] = "bot"  # the mailbox's AI assistant, never a seed
    return conv


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
            [(r, clean(t), b) for r, t, b in turns])
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


# ------------------------------------------------------------------ output tiers


def _cell(value: Any, limit: int = 140) -> str:
    return clip(" ".join(str(value or "").split()), limit).replace("\t", " ") or "-"


def write_tsvs(evidence: dict[str, Any]) -> tuple[str, str]:
    """The read-whole tier: one line per conversation and per article, no bodies."""
    convs = [["id", "date", "channel", "tenant", "subject", "first_sentence", "n_later", "reply",
              "linked", "noise"]]
    for c in evidence.get("conversations") or []:
        convs.append([c["id"], str(c.get("created_at"))[:10], c["channel"], c.get("tenant") or "-",
                      _cell(c.get("subject"), 80), _cell(c["first_message"]),
                      str(len(c.get("later") or [])), (c.get("reply") or {}).get("provenance", "-"),
                      ",".join(c.get("linked_articles") or []) or "-",
                      (c.get("noise") or {}).get("reason", "-")])
    arts = [["id", "title", "collection", "audience", "keywords", "aliases", "path"]]
    for a in evidence.get("articles") or []:
        arts.append([a["id"], _cell(a.get("title"), 100), _cell(a.get("collection"), 40),
                     _cell(a.get("audience"), 20), ", ".join(a.get("keywords") or []) or "-",
                     ", ".join(a.get("aliases") or []) or "-", a.get("path") or "-"])
    return ("\n".join("\t".join(row) for row in convs) + "\n",
            "\n".join("\t".join(row) for row in arts) + "\n")


def _article_line(article: dict[str, Any]) -> str:
    bits = [article["id"], article.get("title") or "?"]
    bits += [", ".join(article[key]) for key in ("keywords", "aliases") if article.get(key)]
    bits += [str(article[key]) for key in ("summary", "collection", "audience", "path")
             if article.get(key)]
    return " · ".join(bits + (["DEPRECATED"] if article.get("deprecated") else []))


def write_digest(evidence: dict[str, Any]) -> str:
    """The drill tier: full conversation bodies + the article inventory (conversations.tsv first)."""
    window, kb = evidence.get("window") or {}, evidence.get("kb") or {}
    convs, articles = evidence.get("conversations") or [], evidence.get("articles") or []
    public = [a for a in articles if a.get("home") == "kb"]
    brain = [a for a in articles if a.get("home") == "brain"]
    coverage = " · ".join(f"{c.get('feed')}={c.get('status')}({c.get('retained')}/{c.get('scanned')})"
                          for c in evidence.get("coverage") or [])
    reasons = [f"{c['feed']}: {c['reason']}" for c in evidence.get("coverage") or [] if c.get("reason")]
    out = [f"# Help centre evidence — {evidence.get('project')}"
           + (f" / {evidence['tenant']}" if evidence.get("tenant") else ""), "",
           f"window {str(window.get('start'))[:16]} → {str(window.get('end'))[:16]} "
           f"({window.get('days')}d) · source {evidence.get('source')} · {len(convs)} conversations "
           f"· {len(public)} kb articles ({kb.get('status')}) · kb root {kb.get('root') or '-'}",
           f"coverage: {coverage or 'none'}"] + reasons + ["", "## Conversations", ""]
    for conv in convs:
        reply, noise = conv.get("reply"), conv.get("noise")
        out += [f"##### {conv['id']} | {conv['channel']} | {str(conv.get('created_at'))[:16]} | "
                f"subj={conv.get('subject') or '-'} | {conv.get('customer') or '-'} | "
                f"{'↗' if conv.get('url') else 'no-link'}"
                + (f" | linked {','.join(conv['linked_articles'])}" if conv.get('linked_articles') else '')
                + (f" | noise: {noise['reason']}" if noise else ''),
                f"[customer] {conv['first_message']}"]
        if conv.get("first_raw"):
            out.append(f"[customer|opener] {conv['first_raw']}")
        if reply:
            out.append(f"[agent|{reply.get('provenance')}|{reply.get('by') or '-'}] {reply['text']}")
        out += [f"[{turn['role']}] {turn['text']}" for turn in conv.get("later") or []]
        out += (["[…] middle turns omitted"] if conv.get("truncated") else []) + [""]
    out += [f"## Articles ({len(public)})", ""] + [_article_line(a) for a in public]
    out += ["", f"## Brain knowledge ({len(brain)})", ""]
    return "\n".join(out + [f"{a['id']} · {a.get('title') or a.get('path')}" for a in brain]) + "\n"
