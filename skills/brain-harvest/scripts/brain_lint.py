# /// script
# requires-python = ">=3.11"
# ///
"""Deterministic privacy + brain-contract linter for `brain-harvest`. Scans brain Markdown for the
things that must NEVER survive a harvest into a committed brain file — leaked secrets, raw thread text,
payment links/addresses — and flags soft brain-contract smells (response-mechanics / persona wording).

Stdlib only: run it with `uv run --no-project python brain_lint.py` or plain `python3 brain_lint.py`.
It is a pre-commit gate, not a formatter — it never edits files.

    python3 brain_lint.py                 # scan STAGED *.md (git diff --cached), the pre-commit gate
    python3 brain_lint.py --all           # scan every tracked/untracked *.md under the tree
    python3 brain_lint.py notes/ x.md     # scan explicit files/dirs
    python3 brain_lint.py --strict        # soft (contract) findings also fail the run
    python3 brain_lint.py --selftest      # run built-in regex self-checks (no repo needed)

Exit status: 1 if any HARD finding (secret / raw-thread / payment) is present, or if `--strict` and any
SOFT finding is present; else 0. Findings print grep-style: `path:line: <category>: <snippet>`.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

# ── categories ────────────────────────────────────────────────────────────────────────────────────
# HARD = raw customer data or a secret that must never land in a brain file; blocks the commit.
# SOFT = brain-contract smell (response mechanics / persona / channel wording) that belongs in persona
#        settings, not brain files; a warning unless --strict. See docs/brain-model.md prompt boundary.
HARD = "HARD"
SOFT = "SOFT"

# ── HARD: credentials / secrets ─────────────────────────────────────────────────────────────────
# High-precision provider token shapes plus a couple of generic ones. Deliberately narrow so a green
# lint is meaningful: we want near-zero false positives on prose, not a maximal secret scanner.
SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("aws-access-key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    # Interior hyphens for modern project/service keys (sk-proj-…, sk-svcacct-…, sk-ant-…).
    ("openai-key", re.compile(r"\bsk-[A-Za-z0-9-]{20,}\b")),
    ("slack-token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("github-token", re.compile(r"\bgh[posru]_[A-Za-z0-9]{20,}\b")),
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("private-key-block", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----")),
    ("bearer-token", re.compile(r"\b[Bb]earer\s+[A-Za-z0-9._\-]{20,}\b")),
    # Inline credentials in a connection URL: postgres://user:pass@host/db, mongodb://…, redis://…
    ("db-url-credential", re.compile(r"\b[a-z][a-z0-9+.\-]*://[^\s/@:]+:[^\s/@]+@")),
    # Value assignment to a secret-ish key. Skip obvious placeholders so example/instructional prose
    # ("password: <your-password>", "token=xxx", "secret=***") does not hard-block a legit commit.
    ("password-assign", re.compile(
        r"(?i)\b(?:password|passwd|secret|api[_-]?key|token)\s*[:=]\s*"
        r"(?!<|x{3,}\b|\*{3,}|\.{3}|your[_-]|placeholder\b|redacted\b|example\b)\S{6,}")),
    # Long high-entropy base64-ish blob (>=40 chars) — likely a raw key/token pasted from a thread.
    # Require a base64 signal char (+ or =) OR a mixed-case+digit shape, so plain-hex git SHAs/digests
    # (all lowercase, no +/=) and slash-separated route/slug paths (cases/billing/refunds/…) do NOT
    # hard-fail a legit commit; specific provider patterns above still catch real hex-ish keys by prefix.
    # `/` is deliberately excluded from the char class — a path breaks into short non-matching tokens.
    ("high-entropy-blob", re.compile(
        r"\b(?=[A-Za-z0-9+]{40,}={0,2}\b)"
        r"(?:[A-Za-z0-9+]*[+=]|(?=[A-Za-z0-9]*[a-z])(?=[A-Za-z0-9]*[A-Z])(?=[A-Za-z0-9]*\d))"
        r"[A-Za-z0-9+]{40,}={0,2}\b")),
]

# ── HARD: raw-thread shape ──────────────────────────────────────────────────────────────────────
# Verbatim email plumbing that means someone pasted a raw thread instead of distilling it.
RAWTHREAD_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("quoted-reply", re.compile(r"^\s*>")),
    ("on-x-wrote", re.compile(r"(?i)^\s*On .+ wrote:\s*$")),
    ("mail-header", re.compile(r"(?i)^\s*(?:From|To|Cc|Bcc|Sent|Reply-To|Date|Subject)\s*:\s*\S")),
    ("forwarded-block", re.compile(r"(?i)-{3,}\s*(?:Forwarded message|Original Message)\s*-{3,}")),
]

# ── HARD: payment links ─────────────────────────────────────────────────────────────────────────
# Precise, low-false-positive shapes: a pay link or an IBAN is real PII/financial data, never a
# distilled pattern. (Street addresses are handled as SOFT below — that heuristic is coarse.)
PAYMENT_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("stripe-pay-link", re.compile(r"(?i)\b(?:checkout\.)?stripe\.com/pay\b")),
    ("paypal-me", re.compile(r"(?i)\bpaypal\.me/\S+")),
    # Real IBANs are all-caps + digits. Restricting the body/tail groups to [A-Z0-9] stops the pattern
    # greedily eating a trailing lowercase English word (e.g. "US12 ABCD 1234 for" / "AB12 CDEF 3456 GHIJ").
    ("iban", re.compile(r"\b[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]{4}){2,7}[ ]?[A-Z0-9]{1,3}\b")),
]

# ── SOFT: response-mechanics / persona wording + coarse address heuristic ────────────────────────
# These are warnings, not commit blockers. Persona wording belongs in persona settings, not brain
# files (see docs/brain-model.md prompt boundary). The address heuristic is deliberately coarse —
# house number + name word(s) + street suffix — so it surfaces likely addresses for operator review
# without hard-blocking a legit commit on a false match (which would just train `--no-verify`).
CONTRACT_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("street-address", re.compile(
        r"(?i)\b\d{1,5}\s+(?:[A-Z][A-Za-z.]+\s+){0,2}[A-Z][A-Za-z.]+\s*"
        r"(?:street|avenue|ave|road|boulevard|blvd|lane|drive|straat|laan)\b")),
    ("response-mechanics", re.compile(
        r"(?i)\b(?:sign[\s-]?off|greeting|salutation|tone of voice|our tone|"
        r"(?:draft|write|compose) (?:a )?repl\w*|customer-facing tone)\b")),
    ("persona-voice", re.compile(
        r"(?i)\b(?:sound more like us|brand voice|use a (?:friendly|formal|warm) tone|"
        r"always sign|email signature)\b")),
]


def _iter_targets(paths: list[str]) -> list[Path]:
    """Expand file/dir args into Markdown files. Dirs recurse; explicit non-md files are honored."""
    out: list[Path] = []
    for raw in paths:
        p = Path(raw)
        if p.is_dir():
            out.extend(sorted(p.rglob("*.md")))
        elif p.exists():
            out.append(p)
    return out


def _staged_markdown() -> list[Path]:
    """The pre-commit target set: staged (`git diff --cached`) `*.md` paths that still exist on disk."""
    proc = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        print(f"error: git diff --cached failed: {proc.stderr.strip()}", file=sys.stderr)
        return []
    return [Path(ln) for ln in proc.stdout.splitlines() if ln.endswith(".md") and Path(ln).exists()]


def _all_markdown() -> list[Path]:
    """`--all` target set: every `*.md` under the tree, skipping the gitignored `.rootcause/` export
    dumps and VCS/build noise so we never lint the raw corpus we are trying to keep OUT of the brain."""
    skip = {".git", ".rootcause", "__pycache__", ".ruff_cache", ".pytest_cache", "node_modules"}
    return [p for p in sorted(Path(".").rglob("*.md")) if not (skip & set(p.parts))]


def _scan_line(line: str) -> list[tuple[str, str, str]]:
    """Return (severity, category, snippet) for every pattern hit on one line."""
    hits: list[tuple[str, str, str]] = []
    for groups, severity in (
        (SECRET_PATTERNS, HARD),
        (RAWTHREAD_PATTERNS, HARD),
        (PAYMENT_PATTERNS, HARD),
        (CONTRACT_PATTERNS, SOFT),
    ):
        for name, pat in groups:
            m = pat.search(line)
            if m:
                snippet = m.group(0).strip()
                if len(snippet) > 60:
                    snippet = snippet[:57] + "..."
                hits.append((severity, name, snippet))
    return hits


def lint_file(path: Path) -> list[tuple[int, str, str, str]]:
    """Scan one file. Returns (lineno, severity, category, snippet) findings."""
    findings: list[tuple[int, str, str, str]] = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        print(f"error: cannot read {path}: {exc}", file=sys.stderr)
        return findings
    for n, line in enumerate(text.splitlines(), start=1):
        for severity, category, snippet in _scan_line(line):
            findings.append((n, severity, category, snippet))
    return findings


def _selftest() -> int:
    """Cheap built-in check that each category fires and clean prose stays clean. No repo needed."""
    must_flag = {
        "AKIA1234567890ABCDEF": HARD,
        "token: hunter2secret": HARD,
        "> quoted line from a thread": HARD,
        "On Tue, Jan 2 2024, Alice wrote:": HARD,
        "From: alice@example.com": HARD,
        "pay here https://stripe.com/pay/abc": HARD,
        "IBAN NL91 ABNA 0417 1643 00": HARD,
        "connect via postgres://user:pass@db.host:5432/app": HARD,   # db-url-credential
        "the key is sk-proj-abc123DEF456ghi789JKL012mno345": HARD,    # modern openai project key
        "Please draft a reply to the customer": SOFT,
        "sign off warmly with our name": SOFT,
        "The customer at 123 Main Street reported a duplicate charge.": SOFT,
    }
    must_pass = [
        "Customers on the Pro plan can export up to 10k rows.",
        "Route billing questions to the refund runbook.",
        "See the route cases/billing/refunds/proration/upgrades/x for the playbook.",  # slug path, not a blob
        "Order US12 ABCD 1234 for the batch shipped Monday.",         # not an IBAN (trailing word)
        "SKU AB12 CDEF 3456 GHIJ in the catalog is discontinued.",    # SKU-ish, not an IBAN
        "Set password: <your-password> in the local .env before running.",  # placeholder, not a secret
        "Use token=xxx as an example when documenting the API.",      # placeholder, not a secret
        "The invoice total reflects proration for mid-cycle upgrades.",
        "Refer to commit 3f2a1b9c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f90 for the fix.",  # git SHA, not a secret
        "The sha256 digest is e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855.",
    ]
    ok = True
    for text, want in must_flag.items():
        sev = {s for s, _, _ in _scan_line(text)}
        if want not in sev:
            print(f"selftest FAIL: expected {want} finding for {text!r}, got {sev}", file=sys.stderr)
            ok = False
    for text in must_pass:
        hits = _scan_line(text)
        if hits:
            print(f"selftest FAIL: clean prose flagged {text!r}: {hits}", file=sys.stderr)
            ok = False
    print("selftest ok" if ok else "selftest FAILED")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="brain_lint.py", description=__doc__.split("\n")[0])
    p.add_argument("paths", nargs="*", help="files/dirs to scan (default: staged *.md).")
    p.add_argument("--all", action="store_true", help="scan every *.md under the tree.")
    p.add_argument("--strict", action="store_true", help="soft (contract) findings also fail.")
    p.add_argument("--selftest", action="store_true", help="run built-in regex self-checks and exit.")
    args = p.parse_args(sys.argv[1:] if argv is None else argv)

    if args.selftest:
        return _selftest()

    if args.paths:
        targets = _iter_targets(args.paths)
    elif args.all:
        targets = _all_markdown()
    else:
        targets = _staged_markdown()

    if not targets:
        print("no markdown to scan (staged set empty — use --all or pass paths).")
        return 0

    hard = soft = 0
    for path in targets:
        for lineno, severity, category, snippet in lint_file(path):
            print(f"{path}:{lineno}: {severity} {category}: {snippet}")
            if severity == HARD:
                hard += 1
            else:
                soft += 1

    scanned = len(targets)
    if hard:
        print(f"\nFAIL: {hard} hard finding(s), {soft} soft, across {scanned} file(s). "
              "Distil patterns — do not commit raw mail or secrets.", file=sys.stderr)
        return 1
    if soft and args.strict:
        print(f"\nFAIL (--strict): {soft} soft contract finding(s) across {scanned} file(s). "
              "Move response-mechanics/persona wording to persona settings.", file=sys.stderr)
        return 1
    if soft:
        print(f"\nok with warnings: {soft} soft finding(s) across {scanned} file(s) "
              "(run --strict to enforce). No hard findings.")
    else:
        print(f"ok: no findings across {scanned} file(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
