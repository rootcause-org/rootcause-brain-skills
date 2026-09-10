#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pydantic>=2"]
# ///
"""Pydantic models for `report.json` — the one artefact the LLM writes.

One source of truth for `validate.py`, `render.py` and `prompt_compose.py`.
Keys and technical content are English; the owner half is Dutch.

Field guide for the LLM: `../report_schema.md`.
Contract with the collector: `kpis.json` / `manifest.json` (see PLAN, slice A).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

RUN_URL_PREFIX = "https://app.replypen.com/runs/"
_UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
CANONICAL_RUN_URL = re.compile(rf"^{re.escape(RUN_URL_PREFIX)}{_UUID}$")
TOKENIZED_RUN_URL = re.compile(rf"^{re.escape(RUN_URL_PREFIX)}{_UUID}(\?t=[A-Za-z0-9_\-]+)?$")

MAX_FINDINGS = 20
TENANT_RATIO_MAX = 0.25
PROMPT_WORDS_MIN = 100
PROMPT_WORDS_MAX = 220

Severity = Literal["high", "medium", "low", "good"]
Audience = Literal["technical", "owner", "both"]
Confidence = Literal["low", "medium", "high"]
ScopeLevel = Literal["project", "member", "channel", "tenant"]
RecurrenceState = Literal["new", "recurring", "gone"]
TaskKind = Literal["fix", "investigate", "decide"]
FeedStatus = Literal["complete", "partial", "unavailable"]
MailboxMode = Literal["live", "shadow", "watch", "off"]

FindingKind = Literal[
    "lost_runs",
    "action_failure",
    "script_error",
    "capture_gap",
    "correction",
    "open_feedback",
    "policy_question",
    "regression",
    "watch",
    "pattern",
    "good",
]

Plane = Literal[
    "host",
    "action_plane",
    "brain_script",
    "brain_content",
    "tenant_brain",
    "persona",
    "settings",
    "mirror",
    "project_code",
    "human_policy",
    "human_context",
    "noise",
    "unknown",
]

SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2, "good": 3}
SEVERITY_COLOR = {
    "high": "#b42318",
    "medium": "#b54708",
    "low": "#475467",
    "good": "#12734a",
}
SEVERITY_LABEL_EN = {"high": "High", "medium": "Medium", "low": "Low", "good": "Good"}
SEVERITY_LABEL_NL = {"high": "Hoog", "medium": "Midden", "low": "Laag", "good": "Goed"}

KIND_LABEL_EN = {
    "lost_runs": "Lost runs",
    "action_failure": "Action failure",
    "script_error": "Script error",
    "capture_gap": "Capture gap",
    "correction": "Human correction",
    "open_feedback": "Open feedback",
    "policy_question": "Policy question",
    "regression": "Regression",
    "watch": "Watch item",
    "pattern": "Pattern",
    "good": "Good",
}
KIND_LABEL_NL = {
    "lost_runs": "Verloren runs",
    "action_failure": "Gefaalde actie",
    "script_error": "Scriptfout",
    "capture_gap": "Meetgat",
    "correction": "Correctie",
    "open_feedback": "Open feedback",
    "policy_question": "Beleidsvraag",
    "regression": "Regressie",
    "watch": "Opvolgpunt",
    "pattern": "Patroon",
    "good": "Goed",
}

PLANE_LABEL_EN = {
    "host": "RootCause host",
    "action_plane": "Action plane",
    "brain_script": "Brain script",
    "brain_content": "Brain content",
    "tenant_brain": "Tenant brain",
    "persona": "Persona",
    "settings": "Settings",
    "mirror": "Source mirror",
    "project_code": "Project code",
    "human_policy": "Human policy",
    "human_context": "Human context",
    "noise": "Noise",
    "unknown": "Unknown",
}
PLANE_LABEL_NL = {
    "host": "RootCause-host",
    "action_plane": "Actievlak",
    "brain_script": "Brain-script",
    "brain_content": "Brain-inhoud",
    "tenant_brain": "Tenant-brain",
    "persona": "Persona",
    "settings": "Instellingen",
    "mirror": "Bronspiegel",
    "project_code": "Projectcode",
    "human_policy": "Menselijk beleid",
    "human_context": "Menselijke context",
    "noise": "Ruis",
    "unknown": "Onbekend",
}

TASK_KIND_LABEL = {"fix": "Fix", "investigate": "Investigate", "decide": "Decide"}
FEED_STATUS_LABEL_EN = {
    "complete": "complete",
    "partial": "partial",
    "unavailable": "unavailable",
}
FEED_STATUS_LABEL_NL = {
    "complete": "volledig",
    "partial": "gedeeltelijk",
    "unavailable": "niet beschikbaar",
}

NonEmpty = Annotated[str, Field(min_length=1)]
IsoDate = Annotated[str, Field(pattern=r"^\d{4}-\d{2}-\d{2}$")]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


# --------------------------------------------------------------------------
# kpis.json / manifest.json (produced by collect.py, copied verbatim)
# --------------------------------------------------------------------------


class Counters(_Model):
    """The 18 counters of the A↔B contract; every one defaults to 0."""

    runs: int = Field(default=0, ge=0)
    counted: int = Field(default=0, ge=0)
    excluded: int = Field(default=0, ge=0)
    drafts: int = Field(default=0, ge=0)
    sent_as_proposed: int = Field(default=0, ge=0)
    edited: int = Field(default=0, ge=0)
    shadow_compared: int = Field(default=0, ge=0)
    not_sent_yet: int = Field(default=0, ge=0)
    no_draft: int = Field(default=0, ge=0)
    actions_ok: int = Field(default=0, ge=0)
    actions_failed: int = Field(default=0, ge=0)
    actions_proposed: int = Field(default=0, ge=0)
    run_errors: int = Field(default=0, ge=0)
    bash_real_errors: int = Field(default=0, ge=0)
    usage_errors: int = Field(default=0, ge=0)
    deltas_live: int = Field(default=0, ge=0)
    deltas_shadow: int = Field(default=0, ge=0)
    feedback: int = Field(default=0, ge=0)


class DayKpis(Counters):
    date: IsoDate


class AxisKpis(Counters):
    axis: Literal["tenant", "channel", "member"]
    key: NonEmpty
    name: NonEmpty
    mode: MailboxMode | None = None


class ActionFunnelRow(_Model):
    action_id: NonEmpty
    proposed_total: int = Field(ge=0)
    succeeded: int = Field(ge=0)
    failed: int = Field(ge=0)
    superseded: int = Field(ge=0)
    canceled: int = Field(ge=0)
    executing: int = Field(ge=0)
    pending: int = Field(ge=0)
    stale: int = Field(ge=0)
    human_confirmed: int = Field(ge=0)
    auto: int = Field(ge=0)
    acceptance_rate: float | None = Field(ge=0, le=1)


class ActionFunnelTable(_Model):
    rows: list[ActionFunnelRow]
    total: ActionFunnelRow


class ActionFunnelAxis(ActionFunnelTable):
    axis: Literal["tenant", "channel", "member"]
    key: NonEmpty


class ActionFunnelRule(_Model):
    reviewer_confirmed_after_s: int = Field(ge=0)
    stale_after_h: int = Field(ge=0)


class ActionFunnel(_Model):
    focus: ActionFunnelTable
    context: ActionFunnelTable
    per_axis: list[ActionFunnelAxis]
    rule: ActionFunnelRule


class Kpis(_Model):
    action_funnel: ActionFunnel | None = None
    focus: DayKpis
    context_days: list[DayKpis] = Field(default_factory=list)
    per_axis: list[AxisKpis] = Field(default_factory=list)


class FeedCoverage(_Model):
    feed: NonEmpty
    status: FeedStatus
    reason: str | None = None
    fetched: int = Field(default=0, ge=0)
    observed_at: str | None = None


class Window(_Model):
    focus: IsoDate
    focus_days: list[IsoDate] = Field(default_factory=list)
    context_days: list[IsoDate] = Field(default_factory=list)


class ManifestRaw(_Model):
    """`manifest.raw` — where the rc response cache landed and how big it got."""

    path: str | None = None
    files: int = Field(default=0, ge=0)
    mb: float = Field(default=0.0, ge=0)
    pruned: bool = False


class Manifest(_Model):
    """`manifest.json` verbatim; lands on `report.coverage`.

    Round-trip contract: whatever `collect.py` writes must validate here, so the documented
    "copy manifest.json verbatim" is literally true — including `raw` and `owner_lang`.
    """

    report_id: NonEmpty
    projects: list[NonEmpty] = Field(min_length=1)
    window: Window
    generated_at: NonEmpty
    rc_version: str | None = None
    coverage: list[FeedCoverage] = Field(default_factory=list)
    excluded: dict[str, int] = Field(default_factory=dict)
    owner_lang: str | None = None
    feedback_review: dict = Field(default_factory=dict)
    ledger_md: bool | None = None
    raw: ManifestRaw | None = None

    def lang(self) -> str:
        """The owner half's language; `nl` unless the overlay says otherwise."""
        return (self.owner_lang or "nl").strip().lower()[:2] or "nl"


# --------------------------------------------------------------------------
# findings
# --------------------------------------------------------------------------


class Scope(_Model):
    """`level` plus the axis value it names.

    `tenant` stays the tenant-only slug (it is cross-checked against `kpis.per_axis`).
    `key` is the generic axis value — the channel on a tenantless project, the member on a
    multi-member report, the tenant slug on a tenant finding. `level: project` is keyless.
    """

    level: ScopeLevel
    tenant: str | None = None
    key: str | None = None

    @model_validator(mode="after")
    def _tenant_only_on_tenant_level(self) -> Scope:
        if self.level == "tenant" and not self.tenant:
            raise ValueError("level 'tenant' needs a 'tenant' slug")
        if self.level != "tenant" and self.tenant:
            raise ValueError("'tenant' is only allowed when level is 'tenant'")
        if self.level == "project" and self.key:
            raise ValueError(
                "'key' is not allowed on level 'project' (a project finding names no axis value)"
            )
        if self.level == "tenant" and self.key and self.key != self.tenant:
            raise ValueError(
                f"'key' {self.key!r} differs from 'tenant' {self.tenant!r} "
                "(on level 'tenant' they are the same slug — set one or both to it)"
            )
        return self

    def axis_value(self) -> str | None:
        return self.tenant or self.key


class Impact(_Model):
    customer_effect: Annotated[str, Field(min_length=1, max_length=400)]
    runs: int = Field(default=0, ge=0)
    threads: int = Field(default=0, ge=0)
    confidence: Confidence = "medium"


class Recurrence(_Model):
    first_seen: IsoDate
    last_seen: IsoDate
    focus_count: int = Field(default=0, ge=0)
    context_count: int = Field(default=0, ge=0)
    state: RecurrenceState
    known_since: IsoDate | None = None


class Evidence(_Model):
    run_ids: list[NonEmpty] = Field(default_factory=list)
    run_urls: list[NonEmpty] = Field(default_factory=list)


class RootCause(_Model):
    plane: Plane
    detail: Annotated[str, Field(min_length=1, max_length=600)]
    confidence: Confidence = "medium"


class FollowUp(_Model):
    status: Literal["done", "partial", "open"]
    evidence: Annotated[str, Field(min_length=1, max_length=1200)]


class Prompt(_Model):
    """Structured fields; `prompt_compose.py` turns them into the copy blob."""

    task_kind: TaskKind
    target_repo: NonEmpty
    paths: list[NonEmpty] = Field(default_factory=list)
    skills: list[NonEmpty] = Field(default_factory=list)
    run_refs: list[NonEmpty] = Field(default_factory=list)
    conclusion: Annotated[str, Field(min_length=20, max_length=1200)]
    proposed_change: Annotated[str, Field(min_length=20, max_length=1200)]
    verification: Annotated[str, Field(min_length=10, max_length=600)]
    decision_needed: str | None = Field(default=None, max_length=400)


class Finding(_Model):
    id: Annotated[str, Field(pattern=r"^F\d{1,3}$")]
    signature: NonEmpty
    kind: FindingKind
    audience: Audience
    members: list[NonEmpty] = Field(default_factory=list)
    scope: Scope
    severity: Severity
    impact: Impact
    recurrence: Recurrence
    title: Annotated[str, Field(min_length=1, max_length=100)]
    text_en: str | None = Field(default=None, max_length=1400)
    text_nl: str | None = Field(default=None, max_length=1400)
    evidence: Evidence = Field(default_factory=Evidence)
    root_cause: RootCause
    followup: FollowUp | None = None
    recommendation: str | None = Field(default=None, max_length=400)
    prompt: Prompt | None = None

    @model_validator(mode="after")
    def _rules(self) -> Finding:
        errors: list[str] = []
        if self.audience in ("technical", "both") and not (self.text_en or "").strip():
            errors.append(
                f"findings[{self.id}].text_en: required for audience "
                f"{self.audience!r} (write the technical explanation in English)"
            )
        if self.audience in ("owner", "both") and not (self.text_nl or "").strip():
            errors.append(
                f"findings[{self.id}].text_nl: required for audience "
                f"{self.audience!r} (write the owner explanation in Dutch)"
            )
        if self.severity == "high" and self.prompt is None:
            if not (self.audience == "owner" and self.kind == "policy_question"):
                errors.append(
                    f"findings[{self.id}].prompt: required for severity 'high' "
                    "(only an owner-audience policy_question may go without one)"
                )
        for i, url in enumerate(self.evidence.run_urls):
            if not TOKENIZED_RUN_URL.match(url):
                errors.append(
                    f"findings[{self.id}].evidence.run_urls[{i}]: not a run URL "
                    f"({RUN_URL_PREFIX}<uuid> optionally with ?t=<token>)"
                )
        if self.prompt:
            for i, url in enumerate(self.prompt.run_refs):
                if not CANONICAL_RUN_URL.match(url):
                    errors.append(
                        f"findings[{self.id}].prompt.run_refs[{i}]: must be the canonical "
                        f"{RUN_URL_PREFIX}<uuid> (strip the ?t=… token)"
                    )
            if self.prompt.task_kind == "decide" and not self.prompt.decision_needed:
                errors.append(
                    f"findings[{self.id}].prompt.decision_needed: required when "
                    "task_kind is 'decide' (name the choice the human must make)"
                )
        if errors:
            raise ValueError("\n".join(errors))
        return self


# --------------------------------------------------------------------------
# views
# --------------------------------------------------------------------------


class TldrItem(_Model):
    text: Annotated[str, Field(min_length=1, max_length=280)]
    severity: Severity
    finding_ids: list[str] = Field(default_factory=list)


class ActionItem(_Model):
    rank: int = Field(ge=1)
    finding_id: str
    summary: Annotated[str, Field(min_length=1, max_length=300)]


class TechnicalView(_Model):
    headline: Annotated[str, Field(min_length=1, max_length=280)]
    tldr: list[TldrItem] = Field(default_factory=list, max_length=6)
    actions: list[ActionItem] = Field(default_factory=list)
    regressions: list[Annotated[str, Field(min_length=1, max_length=300)]] = Field(
        default_factory=list
    )
    watch: list[Annotated[str, Field(min_length=1, max_length=300)]] = Field(
        default_factory=list
    )
    noise_note: str | None = Field(default=None, max_length=600)


class OwnerTenant(_Model):
    slug: NonEmpty
    policy_difference_nl: Annotated[str, Field(min_length=1, max_length=400)]
    bullets: list[Annotated[str, Field(min_length=1, max_length=300)]] = Field(
        default_factory=list, max_length=3
    )
    finding_ids: list[str] = Field(default_factory=list)


class OwnerView(_Model):
    headline_nl: Annotated[str, Field(min_length=1, max_length=280)]
    tldr_nl: list[TldrItem] = Field(default_factory=list, max_length=5)
    actions_nl: list[Annotated[str, Field(min_length=1, max_length=300)]] = Field(
        default_factory=list
    )
    policy_questions_nl: list[Annotated[str, Field(min_length=1, max_length=300)]] = Field(
        default_factory=list
    )
    good_nl: list[Annotated[str, Field(min_length=1, max_length=300)]] = Field(
        default_factory=list
    )
    tenants: list[OwnerTenant] = Field(default_factory=list)


class CustomSection(_Model):
    id: Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9_\-]{1,40}$")]
    audience: Audience
    member: str | None = None
    title: Annotated[str, Field(min_length=1, max_length=120)]
    html: NonEmpty
    text: NonEmpty


class Meta(_Model):
    signal_note: str | None = Field(default=None, max_length=1200)


class Report(_Model):
    schema_version: int = Field(ge=1)
    report_id: NonEmpty
    date: IsoDate
    generated_at: NonEmpty
    window: Window
    kpis: Kpis
    coverage: Manifest
    findings: list[Finding] = Field(default_factory=list, max_length=MAX_FINDINGS)
    technical: TechnicalView
    owner: OwnerView
    custom_sections: list[CustomSection] = Field(default_factory=list)
    meta: Meta | None = None

    @model_validator(mode="after")
    def _cross_refs(self) -> Report:
        errors: list[str] = []
        ids: set[str] = set()
        for finding in self.findings:
            if finding.id in ids:
                errors.append(
                    f"findings: duplicate id {finding.id!r} (every finding needs a unique F<n>)"
                )
            ids.add(finding.id)

        def check_ids(where: str, refs: list[str]) -> None:
            for ref in refs:
                if ref not in ids:
                    errors.append(
                        f"{where}: unknown finding_id {ref!r} "
                        f"(known: {', '.join(sorted(ids)) or 'none'})"
                    )

        for i, item in enumerate(self.technical.tldr):
            check_ids(f"technical.tldr[{i}].finding_ids", item.finding_ids)
        for i, action in enumerate(self.technical.actions):
            check_ids(f"technical.actions[{i}].finding_id", [action.finding_id])
        for i, item in enumerate(self.owner.tldr_nl):
            check_ids(f"owner.tldr_nl[{i}].finding_ids", item.finding_ids)
        for i, tenant in enumerate(self.owner.tenants):
            check_ids(f"owner.tenants[{i}].finding_ids", tenant.finding_ids)

        tenant_keys = {a.key for a in self.kpis.per_axis if a.axis == "tenant"}
        for finding in self.findings:
            slug = finding.scope.tenant
            if slug and slug not in tenant_keys:
                errors.append(
                    f"findings[{finding.id}].scope.tenant: unknown tenant {slug!r} "
                    f"(must be a kpis.per_axis key with axis 'tenant': "
                    f"{', '.join(sorted(tenant_keys)) or 'none'})"
                )
        for i, tenant in enumerate(self.owner.tenants):
            if tenant.slug not in tenant_keys:
                errors.append(
                    f"owner.tenants[{i}].slug: unknown tenant {tenant.slug!r} "
                    f"(must be a kpis.per_axis key with axis 'tenant')"
                )

        if self.date != self.window.focus:
            errors.append(
                f"window.focus: {self.window.focus!r} differs from date {self.date!r} "
                "(the focus day is the report day)"
            )
        if errors:
            raise ValueError("\n".join(errors))
        return self

    # --- helpers for the renderers ----------------------------------------

    def finding_by_id(self, finding_id: str) -> Finding | None:
        return next((f for f in self.findings if f.id == finding_id), None)

    def axis_rows(self) -> list[AxisKpis]:
        """The KPI table axis: tenants if any, else channels, else members."""
        for axis in ("tenant", "channel", "member"):
            rows = [a for a in self.kpis.per_axis if a.axis == axis]
            if rows:
                return rows
        return []

    def axis_name(self, key: str) -> str:
        for row in self.kpis.per_axis:
            if row.key == key:
                return row.name
        return key

    def findings_for(self, audience: str) -> list[Finding]:
        wanted = {audience, "both"}
        return [f for f in self.findings_sorted() if f.audience in wanted]

    def findings_sorted(self) -> list[Finding]:
        return sorted(
            self.findings,
            key=lambda f: (SEVERITY_ORDER[f.severity], _index(f.id)),
        )

    def sections_for(self, audience: str) -> list[CustomSection]:
        wanted = {audience, "both"}
        return [s for s in self.custom_sections if s.audience in wanted]

    def is_quiet(self) -> bool:
        return not self.findings


def _index(finding_id: str) -> int:
    try:
        return int(finding_id[1:])
    except ValueError:  # pragma: no cover - the pattern guarantees digits
        return 0


# --------------------------------------------------------------------------
# soft rules (warnings — never block a render)
# --------------------------------------------------------------------------

_NL_WORDS = {
    "de", "het", "een", "en", "van", "niet", "voor", "met", "is", "dat", "op", "te",
    "zijn", "wordt", "naar", "maar", "die", "aan", "er", "ook", "nog", "bij", "we",
    "wel", "geen", "hij", "ze", "over", "dan", "als", "om", "wat", "heeft",
}
_EN_WORDS = {
    "the", "and", "is", "to", "of", "in", "that", "for", "with", "this", "are",
    "not", "on", "it", "we", "a", "an", "as", "by", "from", "was", "were", "but",
    "so", "at", "has", "have", "does", "which", "when", "should",
}


def _lang_score(text: str) -> tuple[int, int]:
    words = re.findall(r"[a-zA-Zà-üÀ-Ü]+", text.lower())
    return (
        sum(1 for w in words if w in _NL_WORDS),
        sum(1 for w in words if w in _EN_WORDS),
    )


def _looks_english(text: str) -> bool:
    nl, en = _lang_score(text)
    return en > nl


def _looks_dutch(text: str) -> bool:
    nl, en = _lang_score(text)
    return nl > en


def _load_json(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _run_ids_in(blob: Any) -> set[str]:
    """Every uuid-ish string anywhere in evidence.json (shape-tolerant)."""
    found: set[str] = set()
    stack = [blob]
    uuidish = re.compile(rf"^{_UUID}$")
    short = re.compile(r"^[0-9a-f]{8}$")
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
        elif isinstance(node, str):
            if uuidish.match(node):
                found.add(node)
                found.add(node[:8])
            elif short.match(node):
                found.add(node)
    return found


def _longest_prompt_field(prompt: Prompt) -> str:
    """The field to cut (or grow) first — the word budget is spent almost entirely on these."""
    fields = {
        "conclusion": prompt.conclusion,
        "proposed_change": prompt.proposed_change,
        "verification": prompt.verification,
        "decision_needed": prompt.decision_needed or "",
    }
    name = max(fields, key=lambda k: len(fields[k].split()))
    return f"{name} ({len(fields[name].split())} words)"


def soft_warnings(
    report: Report,
    kpis_path: str | Path | None = None,
    manifest_path: str | Path | None = None,
) -> list[str]:
    """`json.path: message (hint)` lines that steer but do not block.

    The owner half is written in `coverage.owner_lang` (default `nl`); the `*_nl` field names are
    historical. The Dutch heuristic only runs when that language really is Dutch.
    """
    out: list[str] = []
    owner_lang = report.coverage.lang()
    owner_is_dutch = owner_lang == "nl"

    tenant_scoped = sum(1 for f in report.findings if f.scope.level == "tenant")
    if report.findings and tenant_scoped / len(report.findings) > TENANT_RATIO_MAX:
        pct = round(tenant_scoped / len(report.findings) * 100)
        out.append(
            f"findings: {pct}% of findings are tenant-scoped "
            f"(keep it under {round(TENANT_RATIO_MAX * 100)}% — the report is about the project; "
            "a tenant finding needs a policy difference, not just an example)"
        )

    axis_keys = {a.key for a in report.kpis.per_axis}
    for finding in report.findings:
        key = finding.scope.key
        if key and key not in axis_keys:
            out.append(
                f"findings[{finding.id}].scope.key: {key!r} is not a kpis.per_axis key "
                f"(known: {', '.join(sorted(axis_keys)) or 'none'})"
            )
        if owner_is_dutch and finding.text_nl and _looks_english(finding.text_nl):
            out.append(
                f"findings[{finding.id}].text_nl: reads as English "
                "(the owner half is Dutch)"
            )
        if finding.text_en and _looks_dutch(finding.text_en):
            out.append(
                f"findings[{finding.id}].text_en: reads as Dutch "
                "(the technical half is English)"
            )
        if finding.prompt:
            for field in ("conclusion", "proposed_change", "verification"):
                value = getattr(finding.prompt, field)
                if value and _looks_dutch(value):
                    out.append(
                        f"findings[{finding.id}].prompt.{field}: reads as Dutch "
                        "(prompts are always English)"
                    )
            words = len(compose_prompt(finding).split())
            if words < PROMPT_WORDS_MIN or words > PROMPT_WORDS_MAX:
                verb = "trim" if words > PROMPT_WORDS_MAX else "grow"
                out.append(
                    f"findings[{finding.id}].prompt: composes to {words} words "
                    f"(aim for {PROMPT_WORDS_MIN}–{PROMPT_WORDS_MAX}: enough to act on, "
                    f"short enough to edit — {verb} "
                    f"{_longest_prompt_field(finding.prompt)} first)"
                )

    if owner_is_dutch:
        for i, item in enumerate(report.owner.tldr_nl):
            if _looks_english(item.text):
                out.append(f"owner.tldr_nl[{i}].text: reads as English (the owner half is Dutch)")
        for i, text in enumerate(report.owner.actions_nl):
            if _looks_english(text):
                out.append(f"owner.actions_nl[{i}]: reads as English (the owner half is Dutch)")
        if _looks_english(report.owner.headline_nl):
            out.append("owner.headline_nl: reads as English (the owner half is Dutch)")
    if _looks_dutch(report.technical.headline):
        out.append("technical.headline: reads as Dutch (the technical half is English)")

    # A drift check must never be able to kill the run: an on-disk artefact that no longer parses
    # is a `warn:` line, not a traceback in the middle of the documented happy path.
    if kpis_path:
        try:
            on_disk = Kpis.model_validate(_load_json(kpis_path))
        except (OSError, json.JSONDecodeError, ValidationError, ValueError) as exc:
            out.append(f"kpis: cannot compare against {kpis_path} ({_one_line_exc(exc)})")
        else:
            if report.kpis.model_dump(exclude_none=False) != on_disk.model_dump(exclude_none=False):
                out.append(
                    f"kpis: differs from {kpis_path} "
                    "(copy the collector's kpis.json verbatim — never retype counters)"
                )
    if manifest_path:
        try:
            on_disk_manifest = Manifest.model_validate(_load_json(manifest_path))
        except (OSError, json.JSONDecodeError, ValidationError, ValueError) as exc:
            out.append(f"coverage: cannot compare against {manifest_path} ({_one_line_exc(exc)})")
        else:
            if report.coverage.model_dump() != on_disk_manifest.model_dump():
                out.append(
                    f"coverage: differs from {manifest_path} "
                    "(copy the collector's manifest.json verbatim)"
                )
    return out


def _one_line_exc(exc: Exception) -> str:
    return _excerpt(f"{type(exc).__name__}: {exc}", 200)


def evidence_errors(report: Report, evidence_path: str | Path) -> list[str]:
    """Run ids cited by findings must exist in the collector's evidence.json."""
    try:
        known = _run_ids_in(_load_json(evidence_path))
    except (OSError, json.JSONDecodeError) as exc:
        return [f"<evidence>: cannot read {evidence_path} ({exc})"]
    out: list[str] = []
    for finding in report.findings:
        for i, run_id in enumerate(finding.evidence.run_ids):
            if run_id not in known and run_id[:8] not in known:
                out.append(
                    f"findings[{finding.id}].evidence.run_ids[{i}]: {run_id!r} is not in "
                    f"{Path(evidence_path).name} (only cite runs the collector saw)"
                )
    return out


# --------------------------------------------------------------------------
# prompt composition (Python owns the blob; the LLM only fills the fields)
# --------------------------------------------------------------------------


def compose_prompt(finding: Finding) -> str:
    """Render `finding.prompt` into the editable plain-text prompt."""
    prompt = finding.prompt
    if prompt is None:
        return ""
    lines = [f"{TASK_KIND_LABEL[prompt.task_kind]}: {finding.title}", ""]
    lines.append("Context")
    lines.append(f"- Repo: {prompt.target_repo}")
    if prompt.paths:
        for path in prompt.paths:
            lines.append(f"- Path: {path}")
    if prompt.skills:
        lines.append(f"- Skills: {', '.join(prompt.skills)}")
    if prompt.run_refs:
        for url in prompt.run_refs:
            lines.append(f"- Run: {url}")
    lines.append(
        f"- Seen {finding.recurrence.focus_count}× on {finding.recurrence.last_seen}, "
        f"{finding.recurrence.context_count}× in the context window "
        f"(first {finding.recurrence.first_seen}, {finding.recurrence.state})."
    )
    lines += ["", "Conclusion", f"- {prompt.conclusion}"]
    lines += ["", "Proposed change", f"- {prompt.proposed_change}"]
    lines += ["", "Verification", f"- {prompt.verification}"]
    if prompt.decision_needed:
        lines += ["", "Decision needed", f"- {prompt.decision_needed}"]
    return "\n".join(lines)


# --------------------------------------------------------------------------
# loading / error formatting
# --------------------------------------------------------------------------

_HINTS = {
    "missing": "required field is absent",
    "extra_forbidden": "unknown key — check the spelling against report_schema.md",
    "literal_error": "value is not in the allowed list",
    "string_too_long": "shorten the text",
    "string_too_short": "text is too short",
    "too_short": "too few items",
    "too_long": "too many items",
    "string_pattern_mismatch": "wrong format",
    "greater_than_equal": "number must not be negative",
    "int_parsing": "must be a whole number",
    "json_invalid": "invalid JSON — check commas and quotes",
}

_PATH_LINE = re.compile(r"^[a-z_]+(\[[^\]]+\])?(\.[a-z_]+(\[[^\]]+\])?)*: ")


def _location(loc: tuple[Any, ...]) -> str:
    out = ""
    for part in loc:
        if isinstance(part, int):
            out += f"[{part}]"
        else:
            out += f".{part}" if out else str(part)
    return out or "<root>"


def _excerpt(value: Any, limit: int = 80) -> str:
    if value is None:
        return ""
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    text = " ".join(text.split())
    return text[: limit - 1] + "…" if len(text) > limit else text


def _format_error(error: dict[str, Any]) -> list[str]:
    loc = _location(tuple(error.get("loc", ())))
    msg = str(error.get("msg", "")).removeprefix("Value error, ")
    kind = str(error.get("type", ""))
    hint = _HINTS.get(kind, "")
    raw = error.get("input")
    excerpt = _excerpt(raw) if not isinstance(raw, (dict, list)) else ""
    lines: list[str] = []
    for part in (p.strip() for p in msg.split("\n")):
        if not part:
            continue
        if _PATH_LINE.match(part):
            lines.append(part)
            continue
        tail = f" ({hint})" if hint else ""
        if excerpt and kind not in {"missing", "too_short", "too_long"}:
            tail += f" (got: {excerpt!r})"
        lines.append(f"{loc}: {part}{tail}")
    return lines or [f"{loc}: {msg}"]


def validation_errors(path: str | Path) -> list[str]:
    """Actionable `json.path: message (hint)` lines; empty means valid."""
    path = Path(path)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        return [f"<file>: cannot read {path} ({exc})"]
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        lines = raw.splitlines()
        line = lines[exc.lineno - 1] if 0 < exc.lineno <= len(lines) else ""
        return [
            f"<root>: invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg} "
            f"({_excerpt(line.strip(), 100)!r})"
        ]
    if not isinstance(data, dict):
        return ["<root>: report.json must be a JSON object, not a list or scalar"]
    try:
        Report.model_validate(data)
    except ValidationError as exc:
        out: list[str] = []
        for error in exc.errors():
            out.extend(_format_error(error))
        return out
    return []


def load_report(path: str | Path) -> Report:
    errors = validation_errors(path)
    if errors:
        raise ValueError("\n".join(errors))
    return Report.model_validate(json.loads(Path(path).read_text(encoding="utf-8")))
