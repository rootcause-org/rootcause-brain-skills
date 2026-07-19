# /// script
# requires-python = ">=3.11"
# ///
"""Deterministic local preparation for a brain-harvest run (spec §1, §3, §5, §5a, §7).

Parses a raw harvest corpus (format v1 or v2) into a private, opaque-ID manifest, a dumb work-
partitioning clustering with a mandatory `mixed` bucket, stratified per-cluster reading plans,
a risk-marker distribution report, a reserved holdout, and a machine-verified coverage ledger.
Everything lives under one gitignored scratch root; raw subjects/filenames never leave it.

Stdlib only: run with `uv run --no-project python prepare_harvest.py` or plain `python3`.

    prepare_harvest.py preflight --scratch .rootcause/harvest/<tag>
    prepare_harvest.py prepare --corpus corpus.md --scratch .rootcause/harvest/<tag>
    prepare_harvest.py verify --scratch .rootcause/harvest/<tag>
    prepare_harvest.py ledger apply --scratch .rootcause/harvest/<tag> drafts/C01.report.json
    prepare_harvest.py cleanup --scratch .rootcause/harvest/<tag> --yes

Determinism: manifest/clusters/ledger/holdout are byte-identical on re-run over the same corpus
bytes; every timestamp is sourced from the corpus `harvested_at`, never wall clock.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Callable, Iterable


# All numeric knobs are tunable defaults, not spec constants; override via --config JSON.
DEFAULTS: dict[str, Any] = {
    "era_recent_months": 24,        # <= this -> recent
    "era_mid_months": 72,           # <= this -> mid; else old
    "sample_cap": 50,               # per-cluster single-pass stratified reading plan cap
    "min_cluster_size": 3,          # smaller subject-family groups fall back to mixed
    "holdout_count": 8,             # reserved eval threads (spec range 5-10)
    "holdout_min_external_chars": 200,
    "prose_reply_min_chars": 40,    # §5 presence-without-prose-reply evidence threshold
    "risk_cap": 0.15,               # flagged share above this -> report over_cap (never auto-expand)
    "deep_thread_min_messages": 6,  # reply-depth risk marker
    "seed": 0,                      # stable pseudo-random tie-break for sampling/holdout spread
}

OUTPUT_NAMES = ("threads", "manifest.jsonl", "clusters.json", "ledger.json", "holdout.json")
PRIMARY_STATUSES = ("assigned", "holdout", "excluded_noise")
READ_STATES = ("none", "sampled", "deep")

HEADER_RE = re.compile(r"^## (.*) — #(\d+)\s*$")
MSG_RE = re.compile(r"^\*\*(.+?) \((\d{4}-\d{2}-\d{2})\):\*\*\s*$")
PART_RE = re.compile(r"^\*\*Participants:\*\*\s*(.*)$")
SPAN_RE = re.compile(r"^\*\*Span:\*\*\s*(.*)$")
OCC_RE = re.compile(r"^\*\*Occurrences:\*\*\s*(\d+)\s*$")
ATT_RE = re.compile(r"^_\[attachment:\s*(.*?)\]_\s*$")
# Colon-only: a hyphen separator would false-strip real subjects ("re-order confirmation").
REPLY_PREFIX = re.compile(r"^\s*(re|fw|fwd|aw|wg|antw|antwort|ref|rép|rep|tr|sv|vs|r)\s*:\s*", re.I)
WORD_RE = re.compile(r"[a-zà-ÿ']+")

# Generic subject families must never determine a topic cluster alone (spec §1); localized too.
GENERIC_FAMILIES = {
    "", "contact", "contact-form", "order", "orders", "invoice", "invoices", "info",
    "information", "support", "help", "question", "questions", "inquiry", "enquiry",
    "enquiries", "request", "requests", "hello", "hi", "hey", "general", "website",
    "feedback", "message", "kontakt", "kontaktformular", "contacto", "bestellung",
    "bestellungen", "commande", "commandes", "bestelling", "bestellingen", "rechnung",
    "rechnungen", "facture", "factures", "factuur", "facturen", "anfrage", "anfragen",
    "vraag", "vragen", "demande", "demandes", "aanvraag", "frage", "fragen", "hallo",
    "bonjour", "salut", "informatie", "renseignement", "renseignements",
}

FORM_RE = re.compile(
    r"contact\s*form|contactformulier|kontaktformular|via (?:the |our |your )?website|"
    r"submitted through|no[- ]?reply|do not reply|niet beantwoorden|this is an automated|"
    r"automated (?:message|notification|reply)|newsletter|unsubscribe|notification",
    re.I,
)

RISK_MARKERS: dict[str, re.Pattern[str]] = {
    "payment_dispute": re.compile(r"chargeback|charge ?back|dispute|unauthori[sz]ed|fraudulent charge", re.I),
    "refund": re.compile(r"refund|money back|reimburse|credit note|terugbetal", re.I),
    "legal": re.compile(r"lawyer|attorney|solicitor|legal action|lawsuit|court|subpoena|liabilit|gdpr|data protection", re.I),
    "complaint_escalation": re.compile(r"complaint|escalat|unacceptable|furious|speak to (?:a )?manager|worst", re.I),
    "safety": re.compile(r"injur|unsafe|hazard|danger|safety|\bharm\b|defect", re.I),
    "credential_request": re.compile(r"password|api[- ]?key|access token|credential|secret key|login details", re.I),
    "policy_exception": re.compile(r"exception|one[- ]time|waive|special case|out of policy|as a courtesy", re.I),
}

STOPWORDS: dict[str, set[str]] = {
    "en": {"the", "and", "you", "your", "for", "with", "this", "that", "have", "are", "will",
           "please", "thanks", "thank", "our", "we", "is", "to", "of", "in", "on", "be", "not"},
    "nl": {"de", "het", "een", "en", "van", "ik", "je", "uw", "met", "voor", "dank", "groeten",
           "is", "op", "aan", "niet", "wij", "ook", "dat", "graag", "kan", "worden"},
    "fr": {"le", "la", "les", "un", "une", "et", "de", "vous", "votre", "pour", "avec", "merci",
           "bonjour", "cordialement", "est", "nous", "ne", "pas", "que", "dans", "sur"},
    "de": {"der", "die", "das", "und", "ein", "eine", "sie", "ihre", "für", "mit", "danke",
           "freundlichen", "grüßen", "ist", "wir", "nicht", "auch", "haben", "sehr", "eine"},
}


class HarvestError(RuntimeError):
    pass


@dataclass
class Message:
    role: str
    date: str | None
    body: str
    attachments: list[str]


@dataclass
class Thread:
    source_format: str
    section_index: int
    subject: str
    occurrence_index: int
    occurrences: int
    participants: list[str]
    span: list[str]
    messages: list[Message]
    raw: str
    # derived
    id: str = ""
    date_first: str | None = None
    date_last: str | None = None
    era: str = "old"
    subject_family: str = ""
    language: str = "und"
    message_count: int = 0
    mailbox_message_count: int = 0
    external_message_count: int = 0
    direction: str = "external_first"
    form_source: bool = False
    attachments: bool = False
    prose_reply: bool = False
    prose_reply_count: int = 0
    risk_markers: list[str] = field(default_factory=list)
    max_external_chars: int = 0
    origin_cluster: str | None = None
    cluster: str | None = None
    secondary_clusters: list[str] = field(default_factory=list)
    holdout: bool = False
    status: str = "assigned"
    read: str = "none"
    routed_to: str | None = None


# --- corpus parsing -------------------------------------------------------------------------

def parse_front_matter(raw: str) -> tuple[dict[str, str], str]:
    text = raw.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n")
    if not text.startswith("---\n"):
        raise HarvestError(
            "missing '---' front-matter fence; expected a harvest corpus (harvest_format: v1|v2). "
            "Re-download with 'rc project corpus download --out <file>' and retry."
        )
    rest = text[4:]
    close = rest.find("\n---")
    if close == -1:
        raise HarvestError(
            "unterminated front-matter fence; expected a closing '---'. "
            "Re-download with 'rc project corpus download --out <file>' and retry."
        )
    front = rest[:close]
    after = rest[close + 4:]
    newline = after.find("\n")
    body = after[newline + 1:] if newline != -1 else ""
    meta: dict[str, str] = {}
    for line in front.split("\n"):
        line = line.strip()
        if not line or ":" not in line:
            continue
        key, value = line.split(":", 1)
        meta[key.strip()] = value.strip()
    return meta, body


def split_sections(body: str) -> list[str]:
    sections: list[str] = []
    for chunk in re.split(r"(?m)(?=^## )", body):
        if not chunk.startswith("## "):
            continue
        first = chunk.split("\n", 1)[0]
        if HEADER_RE.match(first):
            sections.append(chunk.rstrip("\n"))
        elif sections:
            # A stray '## ' inside a message body: rejoin (never mis-split on the \n## boundary).
            sections[-1] = sections[-1] + "\n" + chunk.rstrip("\n")
    return sections


def classify_role(token: str, fmt: str, mailbox: str) -> str:
    lowered = token.strip().lower()
    if fmt == "v2":
        return "mailbox" if lowered == "mailbox" else "external"
    return "mailbox" if mailbox and lowered == mailbox.strip().lower() else "external"


def parse_section(raw: str, fmt: str, mailbox: str, index: int) -> Thread:
    lines = raw.split("\n")
    match = HEADER_RE.match(lines[0])
    if not match:
        raise HarvestError(f"section {index} has no '## <subject> — #<n>' header")
    subject, occurrence_index = match.group(1), int(match.group(2))
    participants: list[str] = []
    span: list[str] = []
    occurrences = 1
    raw_messages: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for line in lines[1:]:
        header = MSG_RE.match(line)
        if header:
            current = {"role": classify_role(header.group(1), fmt, mailbox),
                       "date": header.group(2), "body": [], "attachments": []}
            raw_messages.append(current)
            continue
        if current is None:
            if PART_RE.match(line):
                participants = [p.strip() for p in PART_RE.match(line).group(1).split(",") if p.strip()]
            elif SPAN_RE.match(line):
                span = [p.strip() for p in re.split(r"→|->", SPAN_RE.match(line).group(1)) if p.strip()]
            elif OCC_RE.match(line):
                occurrences = int(OCC_RE.match(line).group(1))
            continue
        attachment = ATT_RE.match(line)
        if attachment:
            current["attachments"].append(attachment.group(1).strip())
        else:
            current["body"].append(line)
    messages = [Message(role=m["role"], date=m["date"], body="\n".join(m["body"]).strip(),
                        attachments=m["attachments"]) for m in raw_messages]
    return Thread(source_format=fmt, section_index=index, subject=subject,
                  occurrence_index=occurrence_index, occurrences=occurrences,
                  participants=participants, span=span, messages=messages, raw=raw)


def load_threads(files: list[Path]) -> tuple[list[Thread], str, list[str]]:
    threads: list[Thread] = []
    harvested: list[str] = []
    formats: list[str] = []
    index = 0
    for path in files:
        meta, body = parse_front_matter(path.read_bytes().decode("utf-8", "replace"))
        fmt = meta.get("harvest_format", "")
        if fmt not in ("v1", "v2"):
            raise HarvestError(
                f"unsupported harvest_format {fmt or '(missing)'!r} in {path.name}; this parser "
                "supports v1 and v2 only. Re-download with 'rc project corpus download --out <file>' "
                "and confirm the server emitted v1 or v2 before retrying (the 48h eviction window "
                "allows a fresh re-download)."
            )
        mailbox = meta.get("mailbox", "")
        harvested.append(meta.get("harvested_at", ""))
        formats.append(fmt)
        for section in split_sections(body):
            threads.append(parse_section(section, fmt, mailbox, index))
            index += 1
    if not threads:
        raise HarvestError("corpus contains no threads (no '## <subject> — #<n>' sections found)")
    harvested_at = max(h for h in harvested if h) if any(harvested) else ""
    fmt_label = "+".join(sorted(set(formats)))
    return threads, harvested_at, [fmt_label]


# --- metadata -------------------------------------------------------------------------------

def nonspace_len(text: str) -> int:
    return len(re.sub(r"\s", "", text))


def subject_family(subject: str) -> str:
    text = subject.strip()
    if text.lower() in ("(no subject)", ""):
        return ""
    previous = None
    while text != previous:
        previous = text
        text = REPLY_PREFIX.sub("", text)
    text = text.lower()
    text = re.sub(r"#\s*\d+", " ", text)
    text = re.sub(r"\d+", " ", text)
    text = re.sub(r"[^a-zà-ÿ]+", " ", text)
    return "-".join(text.split()[:6])


def detect_language(text: str) -> str:
    words = WORD_RE.findall(text.lower())
    if len(words) < 3:
        return "und"
    scores = {lang: sum(1 for w in words if w in stop) for lang, stop in STOPWORDS.items()}
    ranked = sorted(scores.values(), reverse=True)
    best = max(scores, key=lambda k: (scores[k], k))
    if ranked[0] < 2 or (len(ranked) > 1 and ranked[0] == ranked[1]):
        return "und"
    return best


def detect_risk(text: str, message_count: int, cfg: dict[str, Any]) -> list[str]:
    markers = [name for name, pattern in RISK_MARKERS.items() if pattern.search(text)]
    if message_count >= cfg["deep_thread_min_messages"]:
        markers.append("deep_thread")
    return sorted(markers)


def months_between(later: date, earlier: date) -> int:
    return (later.year - earlier.year) * 12 + (later.month - earlier.month) - (1 if later.day < earlier.day else 0)


def era_band(date_last: str | None, harvested: date | None, cfg: dict[str, Any]) -> str:
    if not date_last or harvested is None:
        return "old"
    try:
        months = months_between(harvested, date.fromisoformat(date_last))
    except ValueError:
        return "old"
    if months <= cfg["era_recent_months"]:
        return "recent"
    if months <= cfg["era_mid_months"]:
        return "mid"
    return "old"


def compute_metadata(thread: Thread, cfg: dict[str, Any], harvested: date | None) -> None:
    messages = thread.messages
    dates = [m.date for m in messages if m.date]
    if dates:
        thread.date_first, thread.date_last = min(dates), max(dates)
    elif thread.span:
        thread.date_first, thread.date_last = thread.span[0], thread.span[-1]
    thread.message_count = len(messages)
    thread.mailbox_message_count = sum(1 for m in messages if m.role == "mailbox")
    thread.external_message_count = sum(1 for m in messages if m.role == "external")
    thread.direction = (messages[0].role + "_first") if messages else "external_first"
    thread.attachments = any(m.attachments for m in messages)
    prose = [m for m in messages if m.role == "mailbox" and nonspace_len(m.body) >= cfg["prose_reply_min_chars"]]
    thread.prose_reply_count = len(prose)
    thread.prose_reply = bool(prose)
    thread.max_external_chars = max([nonspace_len(m.body) for m in messages if m.role == "external"] or [0])
    thread.subject_family = subject_family(thread.subject)
    corpus_text = " ".join([thread.subject] + [m.body for m in messages])
    thread.language = detect_language(corpus_text)
    thread.form_source = bool(FORM_RE.search(corpus_text))
    thread.risk_markers = detect_risk(corpus_text, thread.message_count, cfg)
    thread.era = era_band(thread.date_last, harvested, cfg)


def assign_ids(threads: list[Thread]) -> None:
    ordered = sorted(threads, key=lambda t: (t.date_first or "", t.section_index))
    for number, thread in enumerate(ordered, start=1):
        thread.id = f"H{number:06d}"


# --- clustering, holdout, sampling ----------------------------------------------------------

def cluster_threads(threads: list[Thread], cfg: dict[str, Any]) -> list[tuple[str, str, list[Thread]]]:
    groups: dict[str | None, list[Thread]] = defaultdict(list)
    for thread in threads:
        family = thread.subject_family
        key = None if (not family or family in GENERIC_FAMILIES) else family
        groups[key].append(thread)
    mixed = list(groups.pop(None, []))
    proper = sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    clusters: list[tuple[str, str, list[Thread]]] = []
    number = 0
    for family, members in proper:
        if len(members) >= cfg["min_cluster_size"]:
            number += 1
            clusters.append((f"C{number:02d}", family, members))
        else:
            mixed.extend(members)
    clusters.append(("mixed", "mixed", mixed))  # mandatory bucket, always present
    for cluster_id, _, members in clusters:
        for thread in members:
            thread.origin_cluster = cluster_id
    return clusters


def holdout_eligible(thread: Thread, cfg: dict[str, Any]) -> bool:
    return thread.prose_reply and thread.max_external_chars >= cfg["holdout_min_external_chars"]


def pseudo_rank(seed: int, tag: str, thread_id: str) -> str:
    return hashlib.sha1(f"{seed}:{tag}:{thread_id}".encode()).hexdigest()


def select_holdout(threads: list[Thread], cfg: dict[str, Any]) -> set[str]:
    eligible = [t for t in threads if holdout_eligible(t, cfg)]
    strata: dict[tuple[str, str | None], list[Thread]] = defaultdict(list)
    for thread in eligible:
        strata[(thread.era, thread.origin_cluster)].append(thread)
    order = sorted(strata, key=lambda k: (str(k[0]), str(k[1])))
    for key in order:
        strata[key].sort(key=lambda t: pseudo_rank(cfg["seed"], "hold", t.id))
    chosen: list[str] = []
    depth = 0
    target = cfg["holdout_count"]
    while len(chosen) < target:
        progressed = False
        for key in order:
            if depth < len(strata[key]):
                chosen.append(strata[key][depth].id)
                progressed = True
                if len(chosen) >= target:
                    break
        if not progressed:
            break
        depth += 1
    return set(chosen)


def depth_band(message_count: int) -> str:
    if message_count <= 1:
        return "1"
    if message_count <= 3:
        return "2-3"
    if message_count <= 6:
        return "4-6"
    return "7+"


def stratified_sample(threads: list[Thread], cap: int, seed: int) -> list[str]:
    if len(threads) <= cap:
        return sorted(t.id for t in threads)
    strata: dict[tuple[str, str, str, str], list[Thread]] = defaultdict(list)
    for thread in threads:
        strata[(thread.era, thread.language, thread.subject_family, depth_band(thread.message_count))].append(thread)
    order = sorted(strata)
    for key in order:
        strata[key].sort(key=lambda t: pseudo_rank(seed, "sample", t.id))
    chosen: list[str] = []
    depth = 0
    while len(chosen) < cap:
        progressed = False
        for key in order:
            if depth < len(strata[key]):
                chosen.append(strata[key][depth].id)
                progressed = True
                if len(chosen) >= cap:
                    break
        if not progressed:
            break
        depth += 1
    return sorted(chosen)


def finalize(threads: list[Thread], holdout: set[str]) -> None:
    for thread in threads:
        if thread.id in holdout:
            thread.holdout, thread.status, thread.cluster = True, "holdout", None
        elif thread.message_count == 0:
            thread.status, thread.cluster = "excluded_noise", None
        else:
            thread.status, thread.cluster = "assigned", thread.origin_cluster


def build_cluster_plans(clusters: list[tuple[str, str, list[Thread]]], cfg: dict[str, Any]
                        ) -> list[dict[str, Any]]:
    plans: list[dict[str, Any]] = []
    for cluster_id, label, members in clusters:
        assigned = [t for t in members if t.status == "assigned"]
        deep = sorted(t.id for t in assigned if t.risk_markers)
        ordinary = [t for t in assigned if not t.risk_markers]
        plans.append({
            "id": cluster_id, "label": label, "size": len(assigned),
            "thread_ids": sorted(t.id for t in assigned),
            "sample_ids": stratified_sample(ordinary, cfg["sample_cap"], cfg["seed"]),
            "deep_read_ids": deep,
        })
    return plans


# --- output builders ------------------------------------------------------------------------

def manifest_row(thread: Thread) -> dict[str, Any]:
    return {
        "id": thread.id, "source_format": thread.source_format, "section_index": thread.section_index,
        "date_first": thread.date_first, "date_last": thread.date_last, "era": thread.era,
        "subject_family": thread.subject_family, "language": thread.language,
        "message_count": thread.message_count, "mailbox_message_count": thread.mailbox_message_count,
        "external_message_count": thread.external_message_count, "direction": thread.direction,
        "form_source": thread.form_source, "attachments": thread.attachments,
        "prose_reply": thread.prose_reply, "prose_reply_count": thread.prose_reply_count,
        "occurrences": thread.occurrences, "risk_markers": thread.risk_markers,
        "cluster": thread.cluster, "secondary_clusters": thread.secondary_clusters,
        "holdout": thread.holdout,
    }


def risk_report(threads: list[Thread], cfg: dict[str, Any]) -> dict[str, Any]:
    flagged = [t for t in threads if t.risk_markers]
    by_marker: Counter[str] = Counter()
    for thread in flagged:
        by_marker.update(thread.risk_markers)
    share = round(len(flagged) / len(threads), 4) if threads else 0.0
    return {"flagged": len(flagged), "share": share, "over_cap": share > cfg["risk_cap"],
            "by_marker": dict(sorted(by_marker.items()))}


def build_outputs(threads: list[Thread], plans: list[dict[str, Any]], harvested_at: str,
                  corpus_format: str, cfg: dict[str, Any]) -> dict[str, str]:
    by_id = sorted(threads, key=lambda t: t.id)
    manifest = "".join(json.dumps(manifest_row(t), ensure_ascii=False) + "\n" for t in by_id)

    clusters_doc = {"generated_at": harvested_at, "clusters": plans}

    holdout_threads = [t for t in by_id if t.holdout]
    holdout_doc = {
        "count": len(holdout_threads),
        "ids": [t.id for t in holdout_threads],
        "threads": [{"id": t.id, "era": t.era, "origin_cluster": t.origin_cluster,
                     "external_chars": t.max_external_chars, "prose_reply_count": t.prose_reply_count}
                    for t in holdout_threads],
    }

    dated = [t for t in threads if t.date_first]
    span = ([min(t.date_first for t in dated), max(t.date_last or t.date_first for t in dated)]
            if dated else [None, None])
    assigned_count = sum(1 for t in threads if t.status == "assigned")
    ledger_doc = {
        "generated_at": harvested_at,
        "corpus": {"format": corpus_format, "threads": len(threads), "date_span": span},
        "clusters": [{"id": p["id"], "size": p["size"]} for p in plans],
        "threads": {t.id: {"cluster": t.cluster, "status": t.status, "read": t.read,
                           "routed_to": t.routed_to} for t in by_id},
        "risk": risk_report(threads, cfg),
        "holdout": {"count": len(holdout_threads), "ids": [t.id for t in holdout_threads]},
        "assigned": assigned_count,
    }
    return {
        "manifest.jsonl": manifest,
        "clusters.json": json.dumps(clusters_doc, indent=2, ensure_ascii=False) + "\n",
        "holdout.json": json.dumps(holdout_doc, indent=2, ensure_ascii=False) + "\n",
        "ledger.json": json.dumps(ledger_doc, indent=2, ensure_ascii=False) + "\n",
    }


# --- scratch safety + atomic publish --------------------------------------------------------

def check_safe_output(scratch: Path,
                      git_runner: Callable[..., Any] = subprocess.run) -> None:
    scratch = scratch.resolve()
    top = git_runner(["git", "rev-parse", "--show-toplevel"], text=True, capture_output=True)
    if top.returncode != 0:
        raise HarvestError("run from a git checkout and choose a gitignored --scratch directory")
    root = Path(top.stdout.strip()).resolve()
    try:
        scratch.relative_to(root)
    except ValueError as exc:
        raise HarvestError(f"--scratch must be inside the current git checkout ({root})") from exc
    probe = scratch / ".harvest-ignore-check"
    ignored = git_runner(["git", "check-ignore", "-q", str(probe)], cwd=root).returncode == 0
    if not ignored:
        raise HarvestError(f"refusing stageable scratch root {scratch}; add it to .gitignore first")


def clear_outputs(root: Path, names: Iterable[str]) -> None:
    for name in names:
        target = root / name
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink(missing_ok=True)


def publish_stage(stage: Path, root: Path, names: Iterable[str]) -> None:
    names = list(names)
    clear_outputs(root, names)
    for name in names:
        source = stage / name
        if source.exists():
            source.replace(root / name)


# --- corpus acquisition ---------------------------------------------------------------------

def corpus_files(path: Path) -> list[Path]:
    if path.is_dir():
        files = sorted(f for f in path.iterdir() if f.suffix in (".md", ".txt") and f.is_file())
    elif path.is_file():
        files = [path]
    else:
        raise HarvestError(f"corpus path not found: {path}")
    if not files:
        raise HarvestError(f"no corpus files (*.md/*.txt) under {path}")
    return files


# --- subcommands ----------------------------------------------------------------------------

def load_config(args: argparse.Namespace) -> dict[str, Any]:
    cfg = dict(DEFAULTS)
    config_path = getattr(args, "config", None)
    if config_path:
        data = json.loads(Path(config_path).read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise HarvestError("--config must be a JSON object")
        for key, value in data.items():
            if key not in DEFAULTS:
                continue
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise HarvestError(f"--config {key} must be a number, got {value!r}")
            cfg[key] = value
    if getattr(args, "holdout", None) is not None:
        cfg["holdout_count"] = args.holdout
    if getattr(args, "seed", None) is not None:
        cfg["seed"] = args.seed
    return cfg


def prepare_scratch(corpus: Path, scratch: Path, cfg: dict[str, Any]) -> dict[str, Any]:
    scratch.mkdir(parents=True, exist_ok=True)
    sources = corpus_files(corpus)
    corpus_dir = scratch / "corpus"
    corpus_dir.mkdir(exist_ok=True)
    for source in sources:
        destination = corpus_dir / source.name
        if source.resolve() != destination.resolve():
            shutil.copy2(source, destination)

    threads, harvested_at, formats = load_threads(sources)
    harvested = date.fromisoformat(harvested_at[:10]) if harvested_at else None
    for thread in threads:
        compute_metadata(thread, cfg, harvested)
    assign_ids(threads)
    clusters = cluster_threads(threads, cfg)
    holdout = select_holdout(threads, cfg)
    finalize(threads, holdout)
    plans = build_cluster_plans(clusters, cfg)
    outputs = build_outputs(threads, plans, harvested_at, formats[0], cfg)

    stage = Path(tempfile.mkdtemp(prefix=".prepare-stage-", dir=scratch))
    try:
        threads_dir = stage / "threads"
        threads_dir.mkdir()
        for thread in sorted(threads, key=lambda t: t.id):
            (threads_dir / f"{thread.id}.md").write_text(thread.raw + "\n", encoding="utf-8")
        for name in ("manifest.jsonl", "clusters.json", "holdout.json", "ledger.json"):
            (stage / name).write_text(outputs[name], encoding="utf-8")
        publish_stage(stage, scratch, OUTPUT_NAMES)
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    for name in ("drafts", "critic", "brief"):
        (scratch / name).mkdir(exist_ok=True)
    return {"threads": len(threads), "clusters": len(plans),
            "holdout": len(holdout), "assigned": sum(1 for t in threads if t.status == "assigned")}


def cmd_prepare(args: argparse.Namespace) -> int:
    scratch = Path(args.scratch).resolve()
    check_safe_output(scratch)
    cfg = load_config(args)
    summary = prepare_scratch(Path(args.corpus), scratch, cfg)
    violations = verify_scratch(scratch)
    if violations:
        raise HarvestError("prepared ledger failed self-verification:\n  " + "\n  ".join(violations))
    print(f"prepared {summary['threads']} threads, {summary['clusters']} clusters, "
          f"{summary['holdout']} holdout in {scratch}")
    return 0


def load_json(path: Path) -> Any:
    if not path.exists():
        raise HarvestError(f"missing {path.name}; run prepare first")
    return json.loads(path.read_text(encoding="utf-8"))


def read_manifest_ids(scratch: Path) -> list[str]:
    path = scratch / "manifest.jsonl"
    if not path.exists():
        raise HarvestError("missing manifest.jsonl; run prepare first")
    ids = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            ids.append(json.loads(line)["id"])
    return ids


def verify_scratch(scratch: Path) -> list[str]:
    return verify_docs(read_manifest_ids(scratch), load_json(scratch / "ledger.json"),
                       load_json(scratch / "clusters.json"))


def verify_docs(manifest_list: list[str], ledger: dict[str, Any],
                clusters_doc: dict[str, Any]) -> list[str]:
    manifest_ids = set(manifest_list)
    clusters = clusters_doc["clusters"]
    threads = ledger["threads"]
    violations: list[str] = []
    for tid, count in Counter(manifest_list).items():
        if count > 1:
            violations.append(f"{tid}: appears {count} times in manifest.jsonl")

    ledger_ids = set(threads)
    for missing in sorted(manifest_ids - ledger_ids):
        violations.append(f"{missing}: in manifest but absent from ledger")
    for extra in sorted(ledger_ids - manifest_ids):
        violations.append(f"{extra}: in ledger but absent from manifest")

    membership: dict[str, list[str]] = defaultdict(list)
    for cluster in clusters:
        cid = cluster["id"]
        thread_ids = cluster["thread_ids"]
        if cluster["size"] != len(thread_ids):
            violations.append(f"cluster {cid}: size {cluster['size']} != {len(thread_ids)} thread_ids")
        for tid in thread_ids:
            membership[tid].append(cid)
        for tid in cluster["sample_ids"]:
            if tid not in thread_ids:
                violations.append(f"cluster {cid}: sample_id {tid} not in thread_ids")
        for tid in cluster["deep_read_ids"]:
            if tid not in thread_ids:
                violations.append(f"cluster {cid}: deep_read_id {tid} not in thread_ids")

    assigned = 0
    for tid in sorted(ledger_ids):
        record = threads[tid]
        status = record.get("status")
        if status not in PRIMARY_STATUSES:
            violations.append(f"{tid}: invalid status {status!r}")
        if record.get("read") not in READ_STATES:
            violations.append(f"{tid}: invalid read state {record.get('read')!r}")
        homes = membership.get(tid, [])
        if status == "assigned":
            assigned += 1
            if len(homes) != 1:
                violations.append(f"{tid}: assigned but appears in {len(homes)} cluster lists {homes}")
        else:
            if homes:
                violations.append(f"{tid}: status {status} but appears in clusters {homes}")

    total_size = sum(c["size"] for c in clusters)
    if total_size != assigned:
        violations.append(f"cluster sizes sum to {total_size} but {assigned} threads are assigned")

    holdout_ids = set(ledger["holdout"]["ids"])
    status_holdout = {tid for tid, r in threads.items() if r.get("status") == "holdout"}
    if holdout_ids != status_holdout:
        violations.append(f"ledger.holdout.ids {sorted(holdout_ids)} != holdout-status threads "
                          f"{sorted(status_holdout)}")
    return violations


def cmd_verify(args: argparse.Namespace) -> int:
    violations = verify_scratch(Path(args.scratch).resolve())
    if violations:
        print("coverage ledger invariants FAILED:", file=sys.stderr)
        for violation in violations:
            print(f"  {violation}", file=sys.stderr)
        return 1
    print("coverage ledger OK")
    return 0


def apply_reports(scratch: Path, report_paths: list[Path]
                  ) -> tuple[dict[str, Any], dict[str, Any]]:
    ledger = load_json(scratch / "ledger.json")
    clusters_doc = load_json(scratch / "clusters.json")
    threads = ledger["threads"]
    by_id = {c["id"]: c for c in clusters_doc["clusters"]}

    def assigned(tid: str) -> bool:
        # Holdout/noise threads take no read or route updates: holdouts are reserved for the
        # evaluation and must never be marked read by a synthesis report.
        return tid in threads and threads[tid].get("status") == "assigned"

    for path in report_paths:
        report = json.loads(path.read_text(encoding="utf-8"))
        for tid in report.get("read_deep", []):
            if assigned(tid):
                threads[tid]["read"] = "deep"
        for tid in report.get("read_sampled", []):
            if assigned(tid) and threads[tid]["read"] != "deep":
                threads[tid]["read"] = "sampled"
        for move in report.get("route_elsewhere", []):
            tid, dest = move.get("id"), move.get("suggested_cluster")
            if not dest or not assigned(tid):
                continue
            source = threads[tid]["cluster"]
            threads[tid]["cluster"] = dest
            threads[tid]["routed_to"] = dest
            if source in by_id:
                for key in ("thread_ids", "sample_ids", "deep_read_ids"):
                    if tid in by_id[source][key]:
                        by_id[source][key].remove(tid)
            if dest not in by_id:
                by_id[dest] = {"id": dest, "label": dest, "size": 0,
                               "thread_ids": [], "sample_ids": [], "deep_read_ids": []}
                clusters_doc["clusters"].append(by_id[dest])
            if tid not in by_id[dest]["thread_ids"]:
                by_id[dest]["thread_ids"].append(tid)
                by_id[dest]["thread_ids"].sort()

    for cluster in clusters_doc["clusters"]:
        cluster["size"] = len(cluster["thread_ids"])
    ledger["clusters"] = [{"id": c["id"], "size": c["size"]} for c in clusters_doc["clusters"]]
    return ledger, clusters_doc


def cmd_ledger_apply(args: argparse.Namespace) -> int:
    scratch = Path(args.scratch).resolve()
    ledger, clusters_doc = apply_reports(scratch, [Path(p) for p in args.reports])
    violations = verify_docs(read_manifest_ids(scratch), ledger, clusters_doc)
    if violations:  # nothing is persisted when the merged result would break invariants
        raise HarvestError("ledger invariants broken after apply:\n  " + "\n  ".join(violations))
    (scratch / "clusters.json").write_text(json.dumps(clusters_doc, indent=2, ensure_ascii=False) + "\n",
                                           encoding="utf-8")
    (scratch / "ledger.json").write_text(json.dumps(ledger, indent=2, ensure_ascii=False) + "\n",
                                         encoding="utf-8")
    print(f"applied {len(args.reports)} report(s); coverage ledger OK")
    return 0


def cmd_cleanup(args: argparse.Namespace) -> int:
    scratch = Path(args.scratch).resolve()
    if not args.yes:
        raise HarvestError("refusing to delete the scratch root without --yes")
    # Deleting is irreversible: only remove a directory prepare actually populated.
    if scratch.exists() and not any((scratch / marker).exists()
                                    for marker in ("manifest.jsonl", "corpus")):
        raise HarvestError(f"{scratch} does not look like a harvest scratch root; refusing to delete")
    shutil.rmtree(scratch, ignore_errors=True)
    if scratch.exists():
        raise HarvestError(f"cleanup failed; {scratch} still exists")
    print(f"removed {scratch}")
    return 0


def default_rc_runner(argv: list[str]) -> tuple[int, str, str] | None:
    try:
        completed = subprocess.run(["rc", *argv], capture_output=True, text=True, timeout=30)
    except FileNotFoundError:
        return None
    except (OSError, subprocess.SubprocessError):
        return None
    return completed.returncode, completed.stdout, completed.stderr


def cmd_preflight(args: argparse.Namespace,
                  rc_runner: Callable[[list[str]], tuple[int, str, str] | None] | None = None,
                  git_runner: Callable[..., Any] = subprocess.run) -> int:
    scratch = Path(args.scratch).resolve()
    rc_runner = rc_runner or default_rc_runner
    checks: list[tuple[str, str, str]] = []

    top = git_runner(["git", "rev-parse", "--show-toplevel"], text=True, capture_output=True)
    if top.returncode != 0:
        checks.append(("FAIL", "git checkout", "not inside a git checkout"))
    else:
        checks.append(("OK", "git checkout", top.stdout.strip()))
        probe = scratch / ".harvest-ignore-check"
        ignored = git_runner(["git", "check-ignore", "-q", str(probe)],
                             cwd=Path(top.stdout.strip())).returncode == 0
        checks.append(("OK" if ignored else "FAIL", "scratch gitignored",
                       str(scratch) if ignored else f"{scratch} is stageable; add it to .gitignore"))

    corpus_dir = scratch / "corpus"
    files = [f for f in corpus_dir.glob("*") if f.suffix in (".md", ".txt")] if corpus_dir.is_dir() else []
    if not files:
        checks.append(("WARN", "corpus", f"no corpus files under {corpus_dir} yet"))
    else:
        formats = set()
        for path in files:
            try:
                meta, _ = parse_front_matter(path.read_bytes().decode("utf-8", "replace"))
                formats.add(meta.get("harvest_format", "?"))
            except HarvestError:
                formats.add("unparseable")
        unsupported = formats - {"v1", "v2"}
        checks.append(("FAIL" if unsupported else "OK", "corpus format",
                       f"{len(files)} file(s), formats {sorted(formats)}"))

    for label, argv in (("rc auth", ["auth", "status"]),
                        ("rc mailbox", ["project", "mailbox", "ls"]),
                        ("rc persona settings", ["project", "settings", "behavior", "get", "-o", "json"]),
                        ("rc triage policy", ["project", "triage", "policy", "get", "-o", "json"])):
        result = rc_runner(argv)
        if result is None:
            checks.append(("WARN", label, "rc not available; run these checks manually before fan-out"))
        else:
            code, out, err = result
            detail = (out or err).strip().splitlines()[0] if (out or err).strip() else "(no output)"
            checks.append(("OK" if code == 0 else "WARN", label, f"exit {code}: {detail}"))

    for level, label, detail in checks:
        print(f"[{level:4}] {label}: {detail}")
    return 1 if any(level == "FAIL" for level, _, _ in checks) else 0


# --- CLI ------------------------------------------------------------------------------------

def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    sub = root.add_subparsers(dest="command", required=True)

    pre = sub.add_parser("preflight", help="local + best-effort rc environment checks")
    pre.add_argument("--scratch", required=True)

    prep = sub.add_parser("prepare", help="parse corpus into the opaque manifest/ledger scratch root")
    prep.add_argument("--corpus", required=True, help="raw corpus file or directory of files")
    prep.add_argument("--scratch", required=True)
    prep.add_argument("--config", help="JSON file overriding DEFAULTS knobs")
    prep.add_argument("--holdout", type=int, help="override holdout_count")
    prep.add_argument("--seed", type=int, help="override sampling/holdout tie-break seed")

    ver = sub.add_parser("verify", help="check coverage-ledger invariants")
    ver.add_argument("--scratch", required=True)

    ledger = sub.add_parser("ledger", help="ledger operations")
    ledger_sub = ledger.add_subparsers(dest="ledger_command", required=True)
    apply = ledger_sub.add_parser("apply", help="merge agent coverage reports and re-verify")
    apply.add_argument("--scratch", required=True)
    apply.add_argument("reports", nargs="+", help="drafts/<cluster>.report.json files")

    clean = sub.add_parser("cleanup", help="delete the scratch root after operator approval")
    clean.add_argument("--scratch", required=True)
    clean.add_argument("--yes", action="store_true")
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "preflight":
            return cmd_preflight(args)
        if args.command == "prepare":
            return cmd_prepare(args)
        if args.command == "verify":
            return cmd_verify(args)
        if args.command == "ledger" and args.ledger_command == "apply":
            return cmd_ledger_apply(args)
        if args.command == "cleanup":
            return cmd_cleanup(args)
    except (HarvestError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"prepare-harvest: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
