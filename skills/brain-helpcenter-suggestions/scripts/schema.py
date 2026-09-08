#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pydantic>=2", "pyyaml"]
# ///
"""Models for `evidence.json` (collect writes) and `suggestions.json` (the agent writes).

One source of truth for `validate.py` and `render.py`. Field guide for the agent:
`../suggestions_schema.md`. Unknown keys are rejected on both files.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

Verdict = Literal["answered", "partial", "missing", "wrong_title", "recipe", "not_kb", "uncertain"]
Kind = Literal["new", "rewrite", "retitle", "merge", "delete", "add_alias"]
GAP_VERDICTS = ("partial", "missing", "wrong_title", "recipe", "uncertain")
WEIGHT = {"missing": 3, "partial": 2, "recipe": 2, "wrong_title": 1, "uncertain": 1}
KIND_COST = {"add_alias": 0, "retitle": 1, "rewrite": 2, "merge": 3, "new": 4, "delete": 5}
MAX_SUGGESTIONS_SOFT = 10
TOP_TOPICS = 8

class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")

class Window(_M):
    start: str
    end: str
    days: int

class Feed(_M):
    feed: str
    status: Literal["complete", "partial", "unavailable"]
    scanned: int = 0
    retained: int = 0
    reason: str | None = None

class Kb(_M):
    status: Literal["complete", "partial", "unavailable"]
    scope: Literal["project", "tenant"] = "project"
    provider: str | None = None
    base_url: str | None = None
    articles: int = 0
    brain_docs: int = 0
    root: str | None = None
    reason: str | None = None

class Reply(_M):
    text: str
    by: str | None = None
    provenance: Literal["human", "draft", "bot"]

class Turn(_M):
    role: Literal["customer", "agent", "unknown"]
    text: str

class Noise(_M):
    """Collector's pre-tag; the judge may override it with any verdict."""

    verdict: Literal["not_kb"]
    reason: str

class Conversation(_M):
    id: str
    url: str | None = None
    channel: Literal["email", "chat"]
    created_at: str
    tenant: str | None = None
    subject: str | None = None
    customer: str | None = None
    first_message: str
    first_raw: str | None = None
    reply: Reply | None = None
    later: list[Turn] = []
    truncated: bool = False
    tags: list[str] = []
    linked_articles: list[str] = []
    noise: Noise | None = None

    def _texts(self, *roles: str) -> str:
        return "\n".join(t.text for t in self.later if t.role in roles)

    def customer_text(self) -> str:
        """first_message + the raw first turn (when a later one was chosen) + customer turns."""
        head = [self.first_message] + ([self.first_raw] if self.first_raw else [])
        return "\n".join(head + [t.text for t in self.later if t.role == "customer"])

class Article(_M):
    id: str = Field(pattern=r"^A\d+$")
    home: Literal["kb", "brain"]
    path: str
    title: str
    url: str | None = None
    # Provider-native handles from the article frontmatter (the bot block needs them verbatim).
    provider: str | None = None
    provider_id: str | None = None
    number: str | None = None
    collection_id: str | None = None
    parent_type: Literal["collection", "section"] | None = None
    locale: str | None = None
    status: str | None = None
    keywords: list[str] = []
    aliases: list[str] = []
    summary: str = ""
    collection: str | None = None
    area: str | None = None
    updated_at: str | None = None
    audience: str | None = None
    deprecated: bool = False

class Evidence(_M):
    schema_version: int
    project: str
    tenant: str | None = None
    collected_at: str
    window: Window
    source: Literal["runs", "helpscout"]
    coverage: list[Feed] = []
    kb: Kb
    conversations: list[Conversation]
    articles: list[Article]

class Classification(_M):
    conversation_id: str
    verdict: Verdict
    article_ids: list[str] = []
    topics: list[str] = []

class Quote(_M):
    conversation_id: str
    quote: str

class Edit(_M):
    """Where the proposed text goes: exactly one of old / after / before (validated in _cross)."""

    old: str | None = None      # verbatim block of the current article body that `text` replaces
    after: str | None = None    # verbatim line of the current article; `text` is inserted after it
    before: str | None = None   # verbatim line of the current article; `text` is inserted before it


class Suggestion(_M):
    id: str = Field(pattern=r"^S\d+$")
    kind: Kind
    topic: str
    title: str
    target_articles: list[str] = []
    destination: str | None = None
    edit: Edit | None = None
    text: str | None = None
    aliases: list[str] = []
    why: str
    route: Literal["kb", "brain"]
    flags: list[Literal["contradiction"]] = []
    evidence: list[Quote] = Field(min_length=1, max_length=5)
    seed_reply: str | None = None

class Learning(_M):
    observation: str
    proposed_change: str
    target: Literal["rubric", "recipe", "normaliser", "validator", "render"]

class Suggestions(_M):
    schema_version: int
    evidence_sha256: str
    headline: str
    classification: list[Classification]
    suggestions: list[Suggestion]
    learnings: list[Learning] = Field(default=[], max_length=5)


# ---------------------------------------------------------------- cross checks

EM_DASH = "—"
EN_DASH = " – "
ELLIPSIS = "…"
EMOJI = re.compile("[\U0001f300-\U0001faff\U0001f900-\U0001f9ff☀-➿]")
# The help-centre write path converts markdown to provider HTML and back; these shapes do not
# survive the round trip, so the next sync would move the agent's own anchors.
NOT_CANON = (
    (re.compile(r"^\s*\|.*\|\s*$", re.M), "a GFM table"),
    (re.compile(r"~~[^~\n]+~~"), "strikethrough"),
    (re.compile(r"</?[a-zA-Z][^>\n]*>"), "raw HTML"),
    (re.compile(r"^\s*[*+]\s+\S", re.M), "a '*' or '+' bullet (use '- ')"),
    (re.compile(r"^[^\n#>\-\d|\s][^\n]*\n[^\n#>\-\d|\s]", re.M), "a soft-wrapped paragraph (one line per paragraph)"),
)


def _norm(text: str) -> str:
    return " ".join(text.split()).lower()


def _norm_body(text: str) -> str:
    """Line endings unified, trailing spaces per line dropped. Nothing else: anchors are verbatim."""
    return "\n".join(line.rstrip() for line in text.replace("\r\n", "\n").split("\n"))


def strip_frontmatter(text: str) -> str:
    text = text.replace("\r\n", "\n")
    if not text.startswith("---\n"):
        return text
    end = text.find("\n---", 3)
    return text[end + 4 :].lstrip("\n") if end != -1 else text


def _line_of(needle: str, source: str | None) -> str:
    """' on line 7' when the offending passage can be located in the source file."""
    if not source:
        return ""
    needle = needle.strip()
    for i, line in enumerate(source.replace("\r\n", "\n").split("\n"), 1):
        if needle and needle in line:
            return f" on line {i}"
    return ""


def _passage(value: str, marker: str) -> str:
    """The offending line of the field, so the agent sees which sentence to rewrite."""
    for line in value.split("\n"):
        if marker in line:
            return line
    return value


def _prose_fields(s: Suggestion) -> list[tuple[str, str]]:
    fields = [("title", s.title), ("why", s.why)]
    if s.text:
        fields.append(("text", s.text))
    fields += [(f"aliases[{i}]", a) for i, a in enumerate(s.aliases)]
    return fields


def _prose_problems(prefix: str, field: str, value: str, source: str | None) -> tuple[list[str], list[str]]:
    """(errors, warnings) for one prose field. Verbatim source text never passes through here."""
    errors, warns = [], []
    p = f"{prefix}.{field}" if prefix else field
    if EM_DASH in value:
        errors.append(
            f"{p}: em dash (U+2014){_line_of(_passage(value, EM_DASH), source)}: "
            f"{_excerpt(_passage(value, EM_DASH), 90)!r} (rewrite that passage without the em dash)"
        )
    if EN_DASH in value:
        warns.append(f"{p}: en dash used as punctuation{_line_of(_passage(value, EN_DASH), source)} - a comma or a full stop reads more human")
    if ELLIPSIS in value:
        warns.append(f"{p}: ellipsis character (U+2026){_line_of(_passage(value, ELLIPSIS), source)} - finish the sentence instead")
    hit = EMOJI.search(value)
    if hit:
        warns.append(f"{p}: emoji {hit.group()!r}{_line_of(_passage(value, hit.group()), source)} - the help centre articles do not use them")
    if field == "text":
        for pattern, what in NOT_CANON:
            found = pattern.search(value)
            if found:
                warns.append(f"{p}: {what}{_line_of(found.group().split(chr(10))[0], source)} - not in the help-centre markdown canon "
                             "('- ' bullets, **bold**, ATX headings, one line per paragraph, no tables/strikethrough/HTML); the publisher would mangle it")
    return errors, warns


def _prose(sug: Suggestions, sources: dict[str, str] | None) -> tuple[list[str], list[str]]:
    sources = sources or {}
    errors, warns = [], []
    for i, s in enumerate(sug.suggestions):
        prefix = f"suggestions[{i}]"
        for field, value in _prose_fields(s):
            e, w = _prose_problems(prefix, field, value, sources.get(prefix))
            errors += e
            warns += w
    if sug.headline:
        e, w = _prose_problems("", "headline", sug.headline, sources.get("headline"))
        errors += e
        warns += w
    for i, l in enumerate(sug.learnings):
        prefix = f"learnings[{i}]"
        for field, value in (("observation", l.observation), ("proposed_change", l.proposed_change)):
            e, w = _prose_problems(prefix, field, value, sources.get(prefix))
            errors += e
            warns += w
    return errors, warns


def _cross(sug: Suggestions, ev: Evidence, evidence_path: Path, articles_dir: Path | None, sources: dict[str, str] | None) -> list[str]:
    out: list[str] = []
    digest = hashlib.sha256(evidence_path.read_bytes()).hexdigest()
    if sug.evidence_sha256 != digest:
        out.append(
            f"evidence_sha256: does not match {evidence_path.name}, which hashes to {digest} "
            "(copy the sha256 collect printed for this evidence.json)"
        )
    convs = {c.id: c for c in ev.conversations}
    arts = {a.id: a for a in ev.articles}
    seen: dict[str, Classification] = {}
    for i, c in enumerate(sug.classification):
        p = f"classification[{i}]"
        if c.conversation_id not in convs:
            out.append(f"{p}.conversation_id: unknown {c.conversation_id!r} (use an evidence.conversations[].id)")
        elif c.conversation_id in seen:
            out.append(f"{p}.conversation_id: duplicate {c.conversation_id!r} (classify each conversation exactly once)")
        seen.setdefault(c.conversation_id, c)
        if c.verdict != "not_kb" and not c.topics:
            out.append(f"{p}.topics: required unless verdict is not_kb (short cluster slugs, first is primary)")
        for j, a in enumerate(c.article_ids):
            if a not in arts:
                out.append(f"{p}.article_ids[{j}]: unknown {a!r} (use an evidence.articles[].id)")
    absent = sorted(cid for cid in convs if cid not in seen)
    if absent:
        shown = ", ".join(absent[:20]) + (" …" if len(absent) > 20 else "")
        out.append(f"classification: {len(absent)} evidence conversation(s) missing: {shown} (classify every conversation)")
    topics = sorted({t for c in sug.classification for t in c.topics})
    for i, s in enumerate(sug.suggestions):
        out.extend(_check_suggestion(f"suggestions[{i}]", s, arts, convs, seen, topics, articles_dir))
    out.extend(_prose(sug, sources)[0])
    return out


def _check_edit(p, s, targets, articles_dir) -> list[str]:
    """Anchor rules. The verbatim check needs the bodies collect wrote to raw/articles/."""
    out: list[str] = []
    if s.kind != "rewrite":
        if s.edit is not None:
            out.append(f"{p}.edit: only kind rewrite takes an edit anchor (drop it)")
        return out
    if s.edit is None:
        out.append(f"{p}.edit: kind rewrite requires edit: with one of old / after / before")
        return out
    set_keys = [k for k in ("old", "after", "before") if getattr(s.edit, k)]
    if len(set_keys) != 1:
        out.append(
            f"{p}.edit: exactly one of old / after / before "
            f"({'none set' if not set_keys else 'set: ' + ', '.join(set_keys)})"
        )
        return out
    if articles_dir is None or len(targets) != 1:
        return out
    key = set_keys[0]
    target = targets[0]
    path = articles_dir / f"{target.id}.md"
    rel = f"raw/articles/{target.id}.md"
    try:
        body = _norm_body(strip_frontmatter(path.read_text(encoding="utf-8")))
    except OSError:
        out.append(f"{p}.edit.{key}: {rel} is missing (re-run collect.py so the article bodies are on disk)")
        return out
    value = _norm_body(getattr(s.edit, key))
    if key == "old":
        if value.strip() and value not in body:
            out.append(f"{p}.edit.old: not found verbatim in {rel} (copy an unchanged block out of that file)")
        return out
    anchor = value.strip()
    hits = sum(1 for line in body.split("\n") if line.strip() == anchor)
    if hits == 0:
        out.append(f"{p}.edit.{key}: {anchor!r} is not a line of {rel} (copy one whole line out of that file)")
    elif hits > 1:
        out.append(f"{p}.edit.{key}: {anchor!r} matches {hits} lines of {rel} (pick a line that occurs once, e.g. the heading above it)")
    return out


def _check_suggestion(p, s, arts, convs, seen, topics, articles_dir=None) -> list[str]:
    out: list[str] = []
    if s.topic not in topics:
        out.append(f"{p}.topic: {s.topic!r} is not a classification topic (reuse one of: {', '.join(topics[:8]) or 'none'})")
    targets: list[Article] = []
    for j, a in enumerate(s.target_articles):
        if a not in arts:
            out.append(f"{p}.target_articles[{j}]: unknown {a!r} (use an evidence.articles[].id)")
        elif arts[a].home != "kb":
            out.append(f"{p}.target_articles[{j}]: {a!r} is a brain document (a public suggestion only targets home=kb articles)")
        else:
            targets.append(arts[a])
    n = len(s.target_articles)
    one = targets[0] if len(targets) == 1 else None
    if s.kind == "new":
        if n:
            out.append(f"{p}.target_articles: kind new takes no target (drop them, or use rewrite/retitle)")
        if not s.text:
            out.append(f"{p}.text: kind new requires the proposed article body")
    elif s.kind == "rewrite":
        if n != 1:
            out.append(f"{p}.target_articles: kind rewrite needs exactly 1 target (got {n})")
        if not s.text:
            out.append(f"{p}.text: kind rewrite requires the replacement text under ## Edit")
    elif s.kind == "retitle":
        if n != 1:
            out.append(f"{p}.target_articles: kind retitle needs exactly 1 target (got {n})")
        if one is not None and _norm(s.title) == _norm(one.title):
            out.append(f"{p}.title: equals the current title of {one.id} (propose the words customers use)")
    elif s.kind == "add_alias":
        if n != 1:
            out.append(f"{p}.target_articles: kind add_alias needs exactly 1 target (got {n})")
        if not s.aliases:
            out.append(f"{p}.aliases: kind add_alias requires at least one alias")
        if one is not None:
            have = {_norm(a) for a in one.aliases} | {_norm(one.title)}
            for j, a in enumerate(s.aliases):
                if _norm(a) in have:
                    out.append(f"{p}.aliases[{j}]: {a!r} is already on {one.id} (propose a new search phrase)")
    elif s.kind == "merge":
        if n < 2:
            out.append(f"{p}.target_articles: kind merge needs at least 2 targets (got {n})")
        if s.destination not in s.target_articles:
            out.append(f"{p}.destination: {s.destination!r} must be one of target_articles (the surviving article)")
        if not s.text:
            out.append(f"{p}.text: kind merge requires the merged body")
    elif s.kind == "delete":
        if n != 1:
            out.append(f"{p}.target_articles: kind delete needs exactly 1 target (got {n})")
        if s.destination in s.target_articles or s.destination is None:
            out.append(f"{p}.destination: kind delete requires another existing article to redirect to")
    out.extend(_check_edit(p, s, targets, articles_dir))
    if s.flags and s.kind not in ("rewrite", "merge"):
        out.append(f"{p}.flags: contradiction only applies to a rewrite or a merge (kind is {s.kind})")
    if s.destination is not None and s.destination not in arts:
        out.append(f"{p}.destination: unknown {s.destination!r} (use an evidence.articles[].id)")
    elif s.destination is not None and arts[s.destination].home != "kb":
        out.append(f"{p}.destination: {s.destination!r} is a brain document (a public suggestion only targets home=kb articles)")
    for j, q in enumerate(s.evidence):
        e = f"{p}.evidence[{j}]"
        conv = convs.get(q.conversation_id)
        if conv is None:
            out.append(f"{e}.conversation_id: unknown {q.conversation_id!r} (use an evidence.conversations[].id)")
            continue
        if conv.url is None:
            out.append(f"{e}.conversation_id: {conv.id} has no url (evidence must be a conversation the owner can open)")
        cl = seen.get(conv.id)
        if cl is not None and cl.verdict not in GAP_VERDICTS:
            out.append(f"{e}.conversation_id: {conv.id} is classified {cl.verdict} (evidence must be {'/'.join(GAP_VERDICTS)})")
        if _norm(q.quote) not in _norm(conv.customer_text()):
            if _norm(q.quote) in _norm(conv._texts("unknown", "agent")):
                out.append(f"{e}.quote: matches only an unknown-role/agent turn in {conv.id} (quote customer turns only)")
            else:
                out.append(f"{e}.quote: not verbatim in {conv.id} customer text (copy an unchanged substring)")
    if s.seed_reply is not None:
        conv = convs.get(s.seed_reply)
        if conv is None:
            out.append(f"{p}.seed_reply: unknown {s.seed_reply!r} (use an evidence.conversations[].id)")
        elif conv.reply is None or conv.reply.provenance != "human":
            out.append(f"{p}.seed_reply: {conv.id} has no human reply (its reply is {conv.reply.provenance if conv.reply else 'absent'}, seed only from a human answer)")
    return out


def soft_warnings(sug: Suggestions, ev: Evidence, sources: dict[str, str] | None = None) -> list[str]:
    out: list[str] = []
    if len(sug.suggestions) > MAX_SUGGESTIONS_SOFT:
        out.append(f"suggestions: {len(sug.suggestions)} suggestions, signal-to-noise is the product, keep the top {MAX_SUGGESTIONS_SOFT}")
    for i, s in enumerate(sug.suggestions):
        if len({q.conversation_id for q in s.evidence}) < 2:
            out.append(f"suggestions[{i}].evidence: only one conversation backs {s.id}, a second one makes it a pattern")
    out.extend(_prose(sug, sources)[1])
    for f in ev.coverage:
        if f.status != "complete":
            out.append(f"coverage: feed {f.feed} is {f.status} ({f.reason or 'no reason given'}), the report says so too")
    if ev.kb.status != "complete":
        out.append(f"kb: article inventory is {ev.kb.status} ({ev.kb.reason or 'no reason given'}), some articles may be missing")
    return out


# ------------------------------------------------------------ ranking / tiles

def score(suggestion: Suggestion, classification: list[Classification]) -> int:
    seen: dict[str, str] = {}
    for c in classification:
        if suggestion.topic in c.topics:
            seen.setdefault(c.conversation_id, c.verdict)
    return sum(WEIGHT.get(v, 0) for v in seen.values())


def rank(suggestions: list[Suggestion], classification: list[Classification]) -> list[tuple[int, Suggestion, int]]:
    """(rank, suggestion, score): score desc, then cheapest kind, then id."""
    ordered = sorted(
        suggestions,
        key=lambda s: (-score(s, classification), KIND_COST[s.kind], s.id),
    )
    return [(i + 1, s, score(s, classification)) for i, s in enumerate(ordered)]


def summary(ev: Evidence, sug: Suggestions) -> dict[str, Any]:
    counts = {v: 0 for v in ("answered", "partial", "missing", "wrong_title", "recipe", "not_kb", "uncertain")}
    for c in sug.classification:
        counts[c.verdict] += 1
    topics: dict[str, set[str]] = {}
    for c in sug.classification:
        if c.verdict in ("missing", "partial", "wrong_title", "recipe"):
            for t in c.topics:
                topics.setdefault(t, set()).add(c.conversation_id)
    top = sorted(((t, len(ids)) for t, ids in topics.items()), key=lambda kv: (-kv[1], kv[0]))
    return {
        "scanned": len(ev.conversations),
        "howto": sum(n for v, n in counts.items() if v != "not_kb"),
        **counts,
        "top_topics": top[:TOP_TOPICS],
        "per_tenant": _per_tenant(ev, sug),
    }


def _per_tenant(ev: Evidence, sug: Suggestions) -> list[tuple[str, int, int]]:
    """[(tenant, conversations, gaps)] when the corpus spans more than one tenant, else []."""
    tenants = {c.tenant for c in ev.conversations if c.tenant}
    if len(tenants) < 2:
        return []
    verdicts = {c.conversation_id: c.verdict for c in sug.classification}
    rows: dict[str, list[int]] = {t: [0, 0] for t in tenants}
    for c in ev.conversations:
        if not c.tenant:
            continue
        rows[c.tenant][0] += 1
        if verdicts.get(c.id) in GAP_VERDICTS:
            rows[c.tenant][1] += 1
    return [(t, n, g) for t, (n, g) in sorted(rows.items(), key=lambda kv: (-kv[1][0], kv[0]))]


# ------------------------------------------------------- loading / formatting

_HINTS = {
    "missing": "required field is absent",
    "extra_forbidden": "unknown key, check the spelling against suggestions_schema.md",
    "literal_error": "value is not in the allowed list",
    "string_pattern_mismatch": "wrong format",
    "too_short": "too few items",
    "too_long": "too many items",
    "int_parsing": "must be a whole number",
    "string_type": "must be a string",
}


def _location(loc: tuple[Any, ...]) -> str:
    out = ""
    for part in loc:
        out += f"[{part}]" if isinstance(part, int) else (f".{part}" if out else str(part))
    return out or "<root>"


def _excerpt(value: Any, limit: int = 80) -> str:
    if value is None or isinstance(value, (dict, list)):
        return ""
    text = " ".join(str(value).split())
    return text[: limit - 1] + "…" if len(text) > limit else text


def _format_error(error: dict[str, Any]) -> str:
    loc = tuple(error.get("loc", ()))
    msg = str(error.get("msg", "")).removeprefix("Value error, ")
    kind = str(error.get("type", ""))
    loc = _location(loc)
    tail = f" ({_HINTS[kind]})" if kind in _HINTS else ""
    got = _excerpt(error.get("input"))
    if got and kind not in {"missing", "too_short", "too_long"}:
        tail += f" (got: {got!r})"
    return f"{loc}: {msg}{tail}"


def _model_errors(data: dict, model: type[BaseModel]) -> list[str]:
    try:
        model.model_validate(data)
        return []
    except ValidationError as exc:
        return [_format_error(e) for e in exc.errors()]


def _load(path: Path, model: type[BaseModel], label: str) -> tuple[list[str], Any]:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        return [f"<file>: cannot read {path} ({exc})"], None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        return [f"<root>: invalid JSON in {label} at line {exc.lineno}, column {exc.colno}: {exc.msg}"], None
    if not isinstance(data, dict):
        return [f"<root>: {label} must be a JSON object"], None
    try:
        return [], model.model_validate(data)
    except ValidationError as exc:
        return [_format_error(e) for e in exc.errors()], None


def load_evidence(evidence_path: str | Path) -> tuple[list[str], Evidence | None]:
    errors, ev = _load(Path(evidence_path), Evidence, "evidence.json")
    if errors:
        return [f"evidence.{e}" if not e.startswith("<") else f"evidence.json {e}" for e in errors], None
    return [], ev


def data_errors(
    data: dict,
    evidence_path: str | Path,
    *,
    articles_dir: str | Path | None = None,
    sources: dict[str, str] | None = None,
) -> list[str]:
    """Same checks as `validation_errors`, on an already-assembled suggestions dict."""
    ev_path = Path(evidence_path)
    errors, ev = load_evidence(ev_path)
    if errors or ev is None:
        return errors
    errors = _model_errors(data, Suggestions)
    if errors:
        return errors
    sug = Suggestions.model_validate(data)
    return _cross(sug, ev, ev_path, Path(articles_dir) if articles_dir else None, sources)


def validation_errors(
    suggestions_path: str | Path,
    evidence_path: str | Path,
    *,
    articles_dir: str | Path | None = None,
    sources: dict[str, str] | None = None,
) -> list[str]:
    """Actionable `json.path: message (hint)` lines; empty means valid."""
    ev_path = Path(evidence_path)
    errors, ev = load_evidence(ev_path)
    if errors or ev is None:
        return errors
    errors, sug = _load(Path(suggestions_path), Suggestions, "suggestions.json")
    if errors:
        return errors
    return _cross(sug, ev, ev_path, Path(articles_dir) if articles_dir else None, sources)


def load(suggestions_path: str | Path, evidence_path: str | Path, **kwargs) -> tuple[Suggestions, Evidence]:
    errors = validation_errors(suggestions_path, evidence_path, **kwargs)
    if errors:
        raise ValueError("\n".join(errors))
    ev = Evidence.model_validate(json.loads(Path(evidence_path).read_text(encoding="utf-8")))
    sug = Suggestions.model_validate(json.loads(Path(suggestions_path).read_text(encoding="utf-8")))
    return sug, ev


def relabel(errors: list[str], labels: dict[str, str]) -> list[str]:
    """`suggestions[2].why: …` -> `suggestions/S3.md: why: ...` so a fix is one small file."""
    out = []
    for error in errors:
        for prefix in sorted(labels, key=len, reverse=True):
            if error.startswith(prefix):
                rest = error[len(prefix) :].lstrip(".")
                out.append(f"{labels[prefix]}: {rest}" if rest else f"{labels[prefix]}: {error}")
                break
        else:
            out.append(error)
    return out
