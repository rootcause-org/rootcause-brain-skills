"""Schema + render tests for brain-fleet-report.

    cd skills/brain-fleet-report && uv run --with pydantic --with pytest \
        --no-project pytest tests -q
"""

from __future__ import annotations

import copy
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from render import render_all  # noqa: E402
from report_schema import (  # noqa: E402
    compose_prompt,
    evidence_errors,
    load_report,
    soft_warnings,
    validation_errors,
)

FIXTURES = ROOT / "fixtures"
SAMPLE = FIXTURES / "sample_report.json"
QUIET = FIXTURES / "quiet_report.json"


@pytest.fixture(scope="module")
def sample():
    return load_report(SAMPLE)


def _broken(tmp_path: Path, mutate) -> list[str]:
    data = json.loads(SAMPLE.read_text(encoding="utf-8"))
    mutate(data)
    path = tmp_path / "broken.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return validation_errors(path)


# --- schema ---------------------------------------------------------------


def test_fixtures_validate():
    assert validation_errors(SAMPLE) == []
    assert validation_errors(QUIET) == []


def test_fixture_matches_collector_artefacts(sample):
    assert soft_warnings(
        sample, FIXTURES / "sample_kpis.json", FIXTURES / "sample_manifest.json"
    ) == []


def test_unknown_key_is_rejected(tmp_path):
    def mutate(data):
        data["findings"][0]["oops"] = 1

    errors = _broken(tmp_path, mutate)
    assert any(line.startswith("findings[0].oops:") for line in errors), errors


def test_high_severity_needs_a_prompt(tmp_path):
    def mutate(data):
        del data["findings"][0]["prompt"]

    errors = _broken(tmp_path, mutate)
    assert any("findings[F1].prompt:" in line for line in errors), errors


def test_owner_policy_question_may_skip_the_prompt(sample):
    f7 = sample.finding_by_id("F7")
    assert f7.severity == "high" and f7.prompt is None and f7.audience == "owner"


def test_text_language_fields_are_enforced(tmp_path):
    def mutate(data):
        data["findings"][2]["text_nl"] = ""

    errors = _broken(tmp_path, mutate)
    assert any("findings[F3].text_nl:" in line for line in errors), errors


def test_prompt_run_refs_must_be_canonical(tmp_path):
    def mutate(data):
        data["findings"][0]["prompt"]["run_refs"][0] += "?t=abc"

    errors = _broken(tmp_path, mutate)
    assert any("findings[F1].prompt.run_refs[0]:" in line for line in errors), errors


def test_evidence_urls_may_be_tokenized(sample):
    assert any("?t=" in u for f in sample.findings for u in f.evidence.run_urls)


def test_unknown_finding_reference(tmp_path):
    def mutate(data):
        data["technical"]["actions"][0]["finding_id"] = "F99"

    errors = _broken(tmp_path, mutate)
    assert any("technical.actions[0].finding_id:" in line for line in errors), errors


def test_unknown_tenant_reference(tmp_path):
    def mutate(data):
        data["findings"][2]["scope"]["tenant"] = "nope"

    errors = _broken(tmp_path, mutate)
    assert any("findings[F3].scope.tenant:" in line for line in errors), errors


def test_tenant_ratio_warns(sample):
    heavy = copy.deepcopy(sample)
    heavy.findings = heavy.findings[:4]
    for finding in heavy.findings[:2]:
        finding.scope.level = "tenant"
        finding.scope.tenant = "lbv"
    assert any(w.startswith("findings: ") for w in soft_warnings(heavy))


def test_language_heuristics_warn(sample):
    wrong = copy.deepcopy(sample)
    wrong.findings[0].text_nl = "This is clearly written in English and not in Dutch at all."
    warnings = soft_warnings(wrong)
    assert any("text_nl: reads as English" in w for w in warnings), warnings


def test_evidence_run_ids_are_checked(sample, tmp_path):
    path = tmp_path / "evidence.json"
    path.write_text(json.dumps({"runs": [{"id": "c725536a"}]}), encoding="utf-8")
    errors = evidence_errors(sample, path)
    assert errors and all("evidence.run_ids" in line for line in errors)
    assert not any("c725536a" in line for line in errors)


def test_every_error_line_carries_a_json_path(tmp_path):
    def mutate(data):
        data["findings"][0]["severity"] = "catastrophic"

    for line in _broken(tmp_path, mutate):
        assert ": " in line


# --- prompt composer ------------------------------------------------------


def test_prompt_composes_within_the_word_band(sample):
    for finding in sample.findings:
        if not finding.prompt:
            continue
        text = compose_prompt(finding)
        assert 100 <= len(text.split()) <= 220, (finding.id, len(text.split()))
        assert finding.prompt.target_repo in text
        assert finding.prompt.conclusion in text
        assert finding.prompt.verification in text
        for url in finding.prompt.run_refs:
            assert url in text


# --- render ---------------------------------------------------------------


@pytest.fixture(scope="module")
def rendered(tmp_path_factory, sample):
    out = tmp_path_factory.mktemp("rendered")
    render_all(sample, out)
    return {p.name: p.read_text(encoding="utf-8") for p in out.iterdir()}


def test_renders_six_files(rendered):
    assert set(rendered) == {
        "technical.html",
        "owner.html",
        "technical.email.html",
        "owner.email.html",
        "technical.txt",
        "owner.txt",
    }


def test_owner_html_carries_the_prompts_but_no_technical_body(rendered, sample):
    owner = rendered["owner.html"]
    owner_facing = [f for f in sample.findings if f.audience in ("owner", "both") and f.prompt]
    assert owner_facing
    for finding in owner_facing:
        assert f'id="prompt-{finding.id}"' in owner
        assert finding.prompt.conclusion in owner
    assert "Prompt voor een coding agent (Engels) — kopieer" in owner
    assert 'button class="copy"' in owner
    assert "<pre" in owner
    # outside the (English, verbatim) prompt bodies nothing technical leaks
    outside = re.sub(r"<pre.*?</pre>", "", owner, flags=re.S)
    visible = re.sub(r"<[^>]+>", " ", outside)  # links keep their href, not their id
    for finding in sample.findings:
        if finding.text_en:
            assert finding.text_en not in outside
        assert finding.title not in outside
        for run_id in finding.evidence.run_ids:
            assert run_id not in visible


def test_owner_only_shows_owner_findings(rendered, sample):
    owner = rendered["owner.html"]
    technical_only = [f for f in sample.findings if f.audience == "technical"]
    assert technical_only
    for finding in technical_only:
        assert f'id="{finding.id}"' not in owner
    for finding in sample.findings:
        if finding.audience in ("owner", "both"):
            assert f'id="{finding.id}"' in owner


def test_technical_html_carries_every_finding_and_the_copy_button(rendered, sample):
    html = rendered["technical.html"]
    for finding in sample.findings:
        assert f'id="{finding.id}"' in html
    assert 'button class="copy"' in html
    assert "<pre" in html
    assert "Data coverage" in html
    assert 'target="_blank"' in html


def test_email_variants_have_no_js(rendered):
    for name in ("technical.email.html", "owner.email.html"):
        assert "<script" not in rendered[name]
        assert "<details" not in rendered[name]
    assert "<pre" in rendered["technical.email.html"]


def test_txt_carries_the_prompts(rendered, sample):
    text = rendered["technical.txt"]
    assert sample.findings[0].prompt.proposed_change in text
    assert sample.owner.headline_nl in rendered["owner.txt"]


def test_custom_section_html_is_passed_through(rendered, sample):
    section = sample.custom_sections[0]
    assert section.html in rendered["technical.html"]
    assert section.html not in rendered["owner.html"]


def test_quiet_report_renders_one_sentence(tmp_path):
    report = load_report(QUIET)
    render_all(report, tmp_path)
    html = (tmp_path / "technical.html").read_text(encoding="utf-8")
    assert 'class="quiet"' in html
    assert report.technical.headline in html
    assert 'class="finding"' not in html
    assert "Data coverage" in html
    owner = (tmp_path / "owner.html").read_text(encoding="utf-8")
    assert report.owner.headline_nl in owner
    assert 'class="finding"' not in owner


# --- manifest round-trip / owner language / scope key ---------------------


def test_manifest_round_trips_with_raw_and_owner_lang(tmp_path, sample):
    """What collect.py writes must validate here: the documented "copy verbatim" is literal."""
    collected = {
        **json.loads((FIXTURES / "sample_manifest.json").read_text(encoding="utf-8")),
        "owner_lang": "en",
        "ledger_md": False,
        "raw": {"path": "raw", "files": 412, "mb": 63.1, "pruned": False},
    }
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(collected), encoding="utf-8")

    data = json.loads(SAMPLE.read_text(encoding="utf-8"))
    data["coverage"] = collected
    report_path = tmp_path / "report.json"
    report_path.write_text(json.dumps(data), encoding="utf-8")

    assert validation_errors(report_path) == []
    report = load_report(report_path)
    assert report.coverage.raw.files == 412 and report.coverage.lang() == "en"
    assert not [w for w in soft_warnings(report, None, path) if w.startswith("coverage:")]


def test_unparseable_manifest_warns_instead_of_raising(tmp_path, sample):
    broken = tmp_path / "manifest.json"
    broken.write_text('{"report_id": 5}', encoding="utf-8")
    warnings = soft_warnings(sample, None, broken)
    assert any(w.startswith("coverage: cannot compare") for w in warnings), warnings


def test_english_owner_half_skips_the_dutch_heuristic(tmp_path):
    data = json.loads(SAMPLE.read_text(encoding="utf-8"))
    data["coverage"]["owner_lang"] = "en"
    for finding in data["findings"]:
        if finding.get("text_nl"):
            finding["text_nl"] = "The agent stopped answering and the customer waited all day."
    data["owner"]["headline_nl"] = "Two things need a decision from you today."
    path = tmp_path / "report.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    report = load_report(path)
    assert not [w for w in soft_warnings(report) if "reads as English" in w]

    from render import owner_lang, render_all  # noqa: PLC0415

    assert owner_lang(report) == "en"
    render_all(report, tmp_path)
    owner = (tmp_path / "owner.html").read_text(encoding="utf-8")
    assert 'html lang="en"' in owner
    assert "DAGRAPPORT" not in owner.upper() and "Dagrapport" not in owner
    assert "What we pick up today" in owner
    # the owner half stays owner-only, but the English prompts come along
    assert "Prompt for a coding agent — copy" in owner
    assert "Copy prompt" in owner


def test_owner_page_never_labels_a_link_with_a_run_id(rendered, sample):
    owner = rendered["owner.html"]
    for finding in sample.findings:
        if finding.audience == "technical":
            continue
        for run_id in finding.evidence.run_ids:
            assert f">{run_id} ↗<" not in owner
    if any(f.evidence.run_urls for f in sample.findings if f.audience in ("owner", "both")):
        assert "gesprek ↗" in owner or "gesprek 1 ↗" in owner


def test_scope_key_names_a_channel_or_member(tmp_path):
    data = json.loads(SAMPLE.read_text(encoding="utf-8"))
    data["findings"][1]["scope"] = {"level": "channel", "key": "intercom"}
    path = tmp_path / "report.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    assert validation_errors(path) == []
    # an unknown key is steering, not failure
    assert any("scope.key" in w for w in soft_warnings(load_report(path)))


def test_scope_key_is_rejected_on_a_project_finding(tmp_path):
    def mutate(data):
        data["findings"][0]["scope"] = {"level": "project", "key": "lbv"}

    errors = _broken(tmp_path, mutate)
    assert any("key" in line for line in errors), errors


def test_prompt_word_warning_names_the_longest_field(sample):
    import copy as _copy  # noqa: PLC0415

    fat = _copy.deepcopy(sample)
    finding = next(f for f in fat.findings if f.prompt)
    finding.prompt.proposed_change = " ".join(["word"] * 300)
    warnings = [w for w in soft_warnings(fat) if f"findings[{finding.id}].prompt:" in w]
    assert warnings and "proposed_change" in warnings[0], warnings
