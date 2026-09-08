"""Fixture round-trip + one table-driven case per validator rule.

    cd skills/brain-helpcenter-suggestions
    uv run --with pydantic --with pytest --no-project pytest tests -q
"""

from __future__ import annotations

import copy
import hashlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import render  # noqa: E402
from schema import load, rank, soft_warnings, summary, validation_errors  # noqa: E402

FIX = ROOT / "fixtures"


def _dump(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, sort_keys=True, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")


def _write(tmp_path: Path, ev: dict, sug: dict, fix_sha: bool = True) -> tuple[Path, Path]:
    ep, sp = tmp_path / "evidence.json", tmp_path / "suggestions.json"
    _dump(ep, ev)
    if fix_sha:
        sug["evidence_sha256"] = hashlib.sha256(ep.read_bytes()).hexdigest()
    _dump(sp, sug)
    return sp, ep


@pytest.fixture
def pair() -> tuple[dict, dict]:
    return (
        json.loads((FIX / "evidence.json").read_text(encoding="utf-8")),
        json.loads((FIX / "suggestions.json").read_text(encoding="utf-8")),
    )


def test_fixture_validates() -> None:
    assert validation_errors(FIX / "suggestions.json", FIX / "evidence.json") == []
    sug, ev = load(FIX / "suggestions.json", FIX / "evidence.json")
    tiles = summary(ev, sug)
    assert tiles["scanned"] == 6 and tiles["howto"] == 5 and tiles["missing"] == 1
    assert [s.id for _, s, _ in rank(sug.suggestions, sug.classification)][0] == "S1"  # missing weighs 3
    assert any("only one conversation" in w for w in soft_warnings(sug, ev))


def test_render_html(tmp_path: Path) -> None:
    sug, ev = load(FIX / "suggestions.json", FIX / "evidence.json")
    html = render.render_html(sug, ev)
    for s in sug.suggestions:
        assert s.title in html
    for c in ev.conversations:
        assert c.url and c.url in html
    assert "<script>" in html and "button class=\"copy\"" in html
    assert "noise hint" in html and "duplicate_outreach" in html          # collector pre-tag column
    assert "2 spam folder" in html                                        # coverage reason string
    assert "Chatgesprekken" in render.render_learnings(sug, ev)


def test_render_shows_tenant(tmp_path: Path, pair) -> None:
    ev, sug = pair
    ev["tenant"] = "yes_events"
    for c in ev["conversations"]:
        c["tenant"] = "yes_events"
    sp, ep = _write(tmp_path, ev, sug)
    s, v = load(sp, ep)
    assert "yes_events — help centre suggestions" in render.render_html(s, v)


def test_topics_are_multi_valued(tmp_path: Path, pair) -> None:
    """A conversation listed under two topics feeds the score of both suggestions."""
    ev, sug = pair
    by_id = {c["conversation_id"]: c for c in sug["classification"]}
    by_id["hs:1003"]["topics"] = ["facturen-archief", "boekingshorizon"]   # wrong_title, weight 1
    sp, ep = _write(tmp_path, ev, sug)
    assert validation_errors(sp, ep) == []
    s, v = load(sp, ep)
    scores = {sg.id: sc for _, sg, sc in rank(s.suggestions, s.classification)}
    assert scores["S1"] == 4 and scores["S3"] == 1                        # missing 3 + wrong_title 1
    assert dict(summary(v, s)["top_topics"])["boekingshorizon"] == 2


def test_render_refuses_and_keeps_existing(tmp_path: Path, pair) -> None:
    ev, sug = pair
    sug["suggestions"][0]["kind"] = "nonsense"
    sp, ep = _write(tmp_path, ev, sug)
    (tmp_path / "report.html").write_text("OLD", encoding="utf-8")
    rc = _run_render(sp, ep)
    assert rc == 1 and (tmp_path / "report.html").read_text() == "OLD"


def _run_render(sp: Path, ep: Path) -> int:
    argv = sys.argv
    sys.argv = ["render.py", str(sp), "--evidence", str(ep)]
    try:
        return render.main()
    finally:
        sys.argv = argv


# ------------------------------------------------------------------ mutations

def _m_unknown_key(ev, sug):
    sug["headlin"] = sug.pop("headline")


def _m_bad_enum(ev, sug):
    sug["suggestions"][0]["kind"] = "renmae"


def _m_unknown_conversation(ev, sug):
    sug["suggestions"][0]["evidence"][0]["conversation_id"] = "hs:9999"


def _m_not_verbatim(ev, sug):
    sug["suggestions"][0]["evidence"][0]["quote"] = "Hoe open ik de agenda voor 2027?"


def _m_unclassified(ev, sug):
    sug["classification"] = [c for c in sug["classification"] if c["conversation_id"] != "hs:1006"]


def _m_duplicate_classification(ev, sug):
    sug["classification"].append(copy.deepcopy(sug["classification"][0]))


def _m_topics_missing(ev, sug):
    sug["classification"][0]["topics"] = []


def _m_old_topic_key(ev, sug):
    c = sug["classification"][3]
    c["topic"] = c.pop("topics")


def _m_unknown_role_quote(ev, sug):
    """hs:1002 carries an unknown-role later turn — its words are not the customer's."""
    sug["suggestions"][1]["evidence"][0]["quote"] = "een schermafbeelding van de cabine-instellingen"


def _m_new_with_target(ev, sug):
    sug["suggestions"][0]["target_articles"] = ["A1"]


def _m_rewrite_no_section(ev, sug):
    sug["suggestions"][1]["section"] = None


def _m_retitle_same_title(ev, sug):
    sug["suggestions"][3]["title"] = "Online boekingsmodule"


def _m_alias_present(ev, sug):
    sug["suggestions"][2]["aliases"] = ["Facturatie"]


def _m_merge_destination(ev, sug):
    s = sug["suggestions"][1]
    s.update(kind="merge", target_articles=["A2", "A1"], destination="A3", section=None)


def _m_delete_no_destination(ev, sug):
    s = sug["suggestions"][2]
    s.update(kind="delete", destination=None, aliases=[])


def _m_brain_target(ev, sug):
    sug["suggestions"][2]["target_articles"] = ["A5"]


def _m_no_url(ev, sug):
    ev["conversations"][0]["url"] = None


def _m_answered_evidence(ev, sug):
    sug["classification"][0]["verdict"] = "answered"


def _m_seed_reply_draft(ev, sug):
    sug["suggestions"][0]["seed_reply"] = "hs:1005"


CASES = [
    ("unknown key", _m_unknown_key, "headlin:", "unknown key"),
    ("bad enum", _m_bad_enum, "suggestions[0].kind:", "not in the allowed list"),
    ("unknown conversation", _m_unknown_conversation, "suggestions[0].evidence[0].conversation_id:", "unknown 'hs:9999'"),
    ("non-verbatim quote", _m_not_verbatim, "suggestions[0].evidence[0].quote:", "copy an unchanged substring"),
    ("unclassified conversation", _m_unclassified, "classification:", "hs:1006"),
    ("duplicate classification", _m_duplicate_classification, "classification[6].conversation_id:", "exactly once"),
    ("topics required", _m_topics_missing, "classification[0].topics:", "not_kb"),
    ("old topic key", _m_old_topic_key, "classification[3].topic:", "use topics: [..]"),
    ("unknown-role quote", _m_unknown_role_quote, "suggestions[1].evidence[0].quote:", "unknown-role/agent turn in hs:1002"),
    ("new takes no target", _m_new_with_target, "suggestions[0].target_articles:", "no target"),
    ("rewrite needs section", _m_rewrite_no_section, "suggestions[1].section:", "section to replace"),
    ("retitle same title", _m_retitle_same_title, "suggestions[3].title:", "current title"),
    ("alias already present", _m_alias_present, "suggestions[2].aliases[0]:", "already on A3"),
    ("merge destination", _m_merge_destination, "suggestions[1].destination:", "one of target_articles"),
    ("delete destination", _m_delete_no_destination, "suggestions[2].destination:", "another existing article"),
    ("brain target", _m_brain_target, "suggestions[2].target_articles[0]:", "brain document"),
    ("evidence without url", _m_no_url, "suggestions[0].evidence[0].conversation_id:", "has no url"),
    ("evidence on answered", _m_answered_evidence, "suggestions[0].evidence[0].conversation_id:", "classified answered"),
    ("seed reply is a draft", _m_seed_reply_draft, "suggestions[0].seed_reply:", "no human reply"),
]


@pytest.mark.parametrize("name,mutate,prefix,hint", CASES, ids=[c[0] for c in CASES])
def test_mutation(tmp_path: Path, pair, name, mutate, prefix, hint) -> None:
    ev, sug = pair
    mutate(ev, sug)
    sp, ep = _write(tmp_path, ev, sug)
    errors = validation_errors(sp, ep)
    assert any(e.startswith(prefix) and hint in e for e in errors), f"{name}: {errors}"


def test_sha_mismatch(tmp_path: Path, pair) -> None:
    ev, sug = pair
    sug["evidence_sha256"] = "0" * 64
    sp, ep = _write(tmp_path, ev, sug, fix_sha=False)
    errors = validation_errors(sp, ep)
    assert any(e.startswith("evidence_sha256:") and "collect printed" in e for e in errors)


def test_invalid_evidence_is_reported_first(tmp_path: Path, pair) -> None:
    ev, sug = pair
    ev["conversations"][0]["channel"] = "carrier-pigeon"
    sp, ep = _write(tmp_path, ev, sug)
    errors = validation_errors(sp, ep)
    assert errors and errors[0].startswith("evidence.conversations[0].channel:")
