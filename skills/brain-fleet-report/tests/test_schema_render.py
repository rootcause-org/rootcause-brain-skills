"""V2 contracts: recurrence, executable tasks and one projection across outputs."""

import copy
import html
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from prior import prior_findings, prior_table
from render import project_sections, render_all
from report_schema import (
    compose_prompt,
    evidence_errors,
    load_report,
    owner_prompt_allowed,
    soft_warnings,
    validation_errors,
)

FIXTURES = ROOT / "fixtures"
SAMPLE = FIXTURES / "sample_report.json"
PRIORS = [FIXTURES / "prior/2026-09-03"]


@pytest.fixture
def sample():
    return load_report(SAMPLE, prior_dirs=PRIORS)


def write_report(tmp_path, data, name="report.json"):
    p = tmp_path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data))
    return p


def changed_sample(tmp_path, mutate):
    data = json.loads(SAMPLE.read_text())
    mutate(data)
    return write_report(tmp_path, data)


def test_fixture_and_collector_contract(sample):
    assert not validation_errors(SAMPLE, prior_dirs=PRIORS)
    assert not validation_errors(FIXTURES / "quiet_report.json")
    assert not [
        w
        for w in soft_warnings(
            sample, FIXTURES / "sample_kpis.json", FIXTURES / "sample_manifest.json"
        )
        if w.startswith(("kpis:", "coverage:"))
    ]


@pytest.mark.parametrize(
    "mutate,expected",
    [
        (lambda d: d.update(schema_version=1), "schema_version"),
        (lambda d: d["findings"][0].update(oops=1), "oops"),
        (lambda d: d["findings"][0].pop("prompt"), "prompt"),
        (lambda d: d["findings"][0].pop("ask_nl"), "ask_nl"),
        (lambda d: d["findings"][-1].update(text_en=None), "unchanged forbids"),
        (lambda d: d["findings"][-1].update(prompt=None), "unchanged forbids"),
        (
            lambda d: d["findings"][0]["prompt"]["targets"][0].update(
                paths=["missing-fleet-fixture-file"]
            ),
            "path does not exist",
        ),
        (
            lambda d: d["findings"][0]["prompt"]["targets"][0].update(repo="relative"),
            "absolute checkout",
        ),
        (
            lambda d: d["findings"][0]["prompt"]["targets"][0].update(
                paths=["/etc/hosts"]
            ),
            "outside target",
        ),
        (lambda d: d["findings"][0]["prompt"].update(task_kind="decide"), "decision"),
        (
            lambda d: d["findings"][0]["prompt"].update(
                run_refs=["https://app.replypen.com/runs/a?t=x"]
            ),
            "canonical",
        ),
        (
            lambda d: d["findings"][0].update(
                scope={"level": "tenant", "tenant": "unknown"}
            ),
            "unknown tenant",
        ),
        (lambda d: d["findings"][0].update(update_en="unneeded"), "new forbids"),
    ],
)
def test_validation(tmp_path, mutate, expected):
    errors = validation_errors(changed_sample(tmp_path, mutate), prior_dirs=PRIORS)
    assert any(expected in e for e in errors), errors


def test_unchanged_requires_prior(tmp_path):
    errors = validation_errors(changed_sample(tmp_path, lambda d: None))
    assert any("needs a prior" in e for e in errors)


def test_prior_chain_and_date_isolation(tmp_path):
    prior = json.loads((PRIORS[0] / "report.json").read_text())
    write_report(tmp_path, prior, "2026-09-03/report.json")
    today = json.loads(SAMPLE.read_text())
    # Latest report carries only a carry-over: resolve back to original full payload.
    today["findings"] = [today["findings"][-1]]
    write_report(tmp_path, today, "2026-09-04/report.json")
    write_report(tmp_path, {**prior, "schema_version": 1}, "2026-09-02/report.json")
    write_report(tmp_path, {**prior, "report_id": "other"}, "2026-09-05/report.json")
    write_report(tmp_path, prior, "2026-09-07/report.json")
    (tmp_path / "2026-09-01").mkdir()
    (tmp_path / "2026-09-01/report.json").write_text("[]")
    found = prior_findings(
        tmp_path / "2026-09-06/report.json", report_id=today["report_id"]
    )
    entry = found[today["findings"][0]["signature"]]
    assert entry["date"] == "2026-09-04" and entry["prompt_date"] == "2026-09-03"
    assert (
        entry["finding"]["prompt"]["change"] == prior["findings"][0]["prompt"]["change"]
    )
    assert "has prompt" in prior_table(found)


def test_owner_prompt_boundary_and_projection(tmp_path, sample):
    brain = tmp_path / "rootcause-brain-test"
    brain.mkdir()
    (brain / "policy.md").write_text("policy")
    f = sample.findings[1]
    f.prompt.targets[0].repo = str(brain)
    f.prompt.targets[0].paths = ["policy.md"]
    f.root_cause.plane = "brain_content"
    assert owner_prompt_allowed(f)
    # A host/mirror finding with a prompt must not leak onto owner outputs.
    assert not owner_prompt_allowed(sample.findings[0])
    sample.findings[
        0
    ].prompt.conclusion = (
        "Unique developer-only diagnosis must never appear in owner output."
    )
    render_all(sample, tmp_path / "out")
    outputs = {p.name: p.read_text() for p in (tmp_path / "out").iterdir()}
    assert set(outputs) == {
        "technical.html",
        "owner.html",
        "technical.txt",
        "owner.txt",
    }
    for ext in ("html", "txt"):
        owner = outputs["owner." + ext]
        assert "brain-repo" in owner and "Paste this into" in owner
        assert sample.findings[0].prompt.conclusion not in owner
        assert sample.findings[1].text_en not in owner
        assert "Action funnel" not in owner and "Per-axis counts" not in owner
    for half in ("technical", "owner"):
        view = project_sections(sample, half)
        ids = [i["id"] for s in view["sections"] if "items" in s for i in s["items"]]
        positions = [outputs[half + ".html"].index(f'id="{i}"') for i in ids]
        assert positions == sorted(positions) and len(ids) == len(set(ids))
    assert "prompt from 2026-09-03, unchanged" in outputs["technical.html"]
    assert "prompt from 2026-09-03, unchanged" in outputs["technical.txt"]
    assert "Waiting on owner:" in outputs["technical.html"]
    assert sample.findings[-1].title in outputs["technical.txt"]
    assert outputs["owner.txt"].count(sample.findings[1].text_nl) == 1


def test_array_order_beats_severity_and_text_matches(tmp_path, sample):
    sample.findings[0].severity = "low"
    render_all(sample, tmp_path)
    for ext in ("html", "txt"):
        text = html.unescape((tmp_path / f"technical.{ext}").read_text())
        assert text.index(sample.findings[0].title) < text.index(
            sample.findings[1].title
        )
        assert (
            "Reviewer-confirmed" not in text
            and "Acceptance" not in text
            and "Automatic" not in text
        )
        assert "Executed" in text


def test_quiet_and_language(tmp_path):
    report = load_report(FIXTURES / "quiet_report.json")
    report.coverage.owner_lang = "en"
    render_all(report, tmp_path)
    html = (tmp_path / "owner.html").read_text()
    assert 'html lang="en"' in html and "No new tasks" in html
    assert 'class="card"' not in html
    assert "DIAGNOSTICS" in (tmp_path / "technical.txt").read_text()


def test_decide_and_investigate_composition(sample):
    f = sample.findings[0]
    assert "Seen " not in compose_prompt(f) and "Skills:" not in compose_prompt(f)
    from report_schema import Decision

    f.prompt.task_kind = "decide"
    f.prompt.decision = Decision(question="Which rule?", options=["Always", "Per case"])
    assert "Decision first" in compose_prompt(f) and "Per option" in compose_prompt(f)
    f.prompt.task_kind = "investigate"
    f.prompt.decision = None
    assert "Task / Checks" in compose_prompt(f)


def test_warnings_and_evidence(tmp_path, sample):
    sample.findings[0].prompt.repro = None
    sample.findings[
        0
    ].prompt.change = "Locate the manifest and consider either implementation."
    warnings = soft_warnings(sample)
    assert any("repro" in w for w in warnings) and any("hedge" in w for w in warnings)
    assert evidence_errors(sample, write_report(tmp_path, {"runs": []}))
    bad = write_report(tmp_path, {"invalid": True}, "manifest.json")
    assert any("cannot compare" in w for w in soft_warnings(sample, manifest_path=bad))


def test_owner_decision_carries_without_prompt(tmp_path):
    data = json.loads(SAMPLE.read_text())
    f = copy.deepcopy(data["findings"][2])
    prior = {**data, "date": "2026-09-03", "findings": [f]}
    write_report(tmp_path, prior, "2026-09-03/report.json")
    for k in (
        "text_en",
        "text_nl",
        "ask_nl",
        "ask_for",
        "prompt",
        "impact",
        "root_cause",
    ):
        f.pop(k, None)
    f.update(status="unchanged", update_nl="Nog geen beslissing.")
    data["findings"] = [f]
    report = load_report(write_report(tmp_path, data, "2026-09-04/report.json"))
    assert report.effective(report.findings[0]).ask_nl


def test_changed_and_ledger_churn_warnings(tmp_path):
    data = json.loads(SAMPLE.read_text())
    old = json.loads((PRIORS[0] / "report.json").read_text())["findings"][0]
    data["findings"][-1] = {
        **old,
        "status": "changed",
        "update_en": "The required columns now include a location.",
    }
    p = write_report(tmp_path, data)
    report = load_report(p, prior_dirs=PRIORS)
    assert project_sections(report, "technical")["sections"][0]["items"][-1]["update"]
    data["findings"][-1] = {**old, "status": "new", "signature": "different:slug"}
    p = write_report(tmp_path, data)
    ledger = tmp_path / "_internal/fleet-report/ledger.md"
    ledger.parent.mkdir(parents=True)
    ledger.write_text("| different:slug | **accepted** | never |")
    warnings = soft_warnings(load_report(p, prior_dirs=PRIORS))
    assert any("looks like prior signature" in w for w in warnings)
    assert any("ledger says accepted/noise" in w for w in warnings)


def test_null_errors_and_feedback_link(tmp_path, sample):
    sample.kpis.focus.run_errors = None
    sample.coverage.feedback_review = {"enabled": True, "cadence": "daily-lite"}
    render_all(sample, tmp_path)
    assert "None errors" not in (tmp_path / "technical.txt").read_text()
    for ext in ("html", "txt"):
        assert "feedback-review.html" in (tmp_path / f"owner.{ext}").read_text()


def test_known_signature_cannot_be_new(tmp_path):
    data = json.loads(SAMPLE.read_text())
    old = json.loads((PRIORS[0] / "report.json").read_text())["findings"][0]
    data["findings"] = [old]
    assert any(
        "use changed/unchanged" in e
        for e in validation_errors(write_report(tmp_path, data), prior_dirs=PRIORS)
    )


def test_stale_inherited_path_has_context(tmp_path):
    old = json.loads((PRIORS[0] / "report.json").read_text())
    old["findings"][0]["prompt"]["targets"][0]["paths"] = [
        "missing-fleet-inherited-file"
    ]
    directory = tmp_path / "2026-09-03"
    write_report(directory, old)
    data = json.loads(SAMPLE.read_text())
    data["findings"] = [data["findings"][-1]]
    errors = validation_errors(write_report(tmp_path / "2026-09-04", data))
    assert any(
        "findings[F5]: inherited prompt from 2026-09-03" in e and "use changed" in e
        for e in errors
    )


def test_prior_cadences_do_not_mix(tmp_path):
    old = json.loads((PRIORS[0] / "report.json").read_text())
    for suffix in ("", "-7d", "-3d"):
        data = copy.deepcopy(old)
        data["findings"][0]["title"] = "cadence " + (suffix or "daily")
        write_report(tmp_path, data, f"2026-09-03{suffix}/report.json")
    for suffix in ("", "-7d", "-3d"):
        found = prior_findings(tmp_path / f"2026-09-04{suffix}/report.json")
        assert next(iter(found.values()))["finding"]["title"] == "cadence " + (
            suffix or "daily"
        )
