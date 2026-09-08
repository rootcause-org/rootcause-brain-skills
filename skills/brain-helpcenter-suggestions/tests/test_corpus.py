"""Normaliser tests for `brain-helpcenter-suggestions`. Synthetic data only — never real customers.

    cd skills/brain-helpcenter-suggestions && uv run --with pytest --no-project pytest tests -q
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import collect  # noqa: E402
import corpus  # noqa: E402

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
START = datetime(2026, 9, 1, tzinfo=timezone.utc)
END = datetime(2026, 9, 8, tzinfo=timezone.utc)


def page() -> dict:
    return json.loads((FIXTURES / "helpscout_page.json").read_text(encoding="utf-8"))


def test_raw_page_shape():
    convs = corpus.helpscout_conversations(page())
    assert [c["id"] for c in convs] == ["hs:111", "hs:222", "hs:333"]
    chat = convs[0]
    assert chat["channel"] == "chat"
    assert chat["url"] == "https://secure.helpscout.net/conversation/111/11/"
    assert chat["customer"] == "SalonLies"
    # beaconchat is the customer, note/lineitem are dropped, html became text
    assert chat["first_message"] == "Hoe pas ik de bevestigingsmail aan?"
    assert chat["first_raw"] is None and chat["noise"] is None and chat["tenant"] is None
    assert "Technical Information" not in json.dumps(chat)
    assert chat["reply"] == {"text": "Dag Lies\nDat kan via Prijslijst > Reminders.",
                             "by": "Marie-Lore", "provenance": "human"}
    assert chat["later"] == [] and chat["truncated"] is False


def test_unanswered_and_quote_stripping():
    conv = corpus.helpscout_conversations(page())[1]
    assert conv["reply"] is None  # unanswered conversations are kept
    assert conv["channel"] == "email" and conv["tags"] == ["bonnen"]
    assert conv["first_message"] == "Waar vind ik mijn cadeaubon terug in de webshop?"


def test_window_filter_and_dedupe():
    convs = corpus.helpscout_conversations(page())
    kept = [c for c in convs if corpus.in_window(c["created_at"], START, END)]
    assert [c["id"] for c in kept] == ["hs:111", "hs:222"]


def test_signature_and_email_stripped():
    assert corpus.strip_quoted_and_signature(
        "Vraag?\nMet vriendelijke groeten\nLies") == "Vraag?"
    assert corpus.strip_quoted_and_signature("Vraag?\n> oud\nmeer") == "Vraag?"
    assert corpus.clean("mail me op lies@salon.be", 100) == "mail me op [e-mail]"


DUMP = """##### C1 | email | via=beacon-v2 | tags=[] | subj=None | 2026-09-01T09:00 -> closed 2026-09-01T09:39
[customer|LouLouGlow] Hoe kan ik de bevestigingsmail aanpassen voor mijn klanten?
[message|Michel] Dat kan via de prijslijst.
[customer|LouLouGlow] gevonden, dank

##### C2 | chat | via=beacon-v2 | tags=[boekingen] | subj=Re: agenda | 2026-09-02T11:27 -> active
[customer|Davy] Kan ik de agenda openzetten tot december?
Tweede regel van dezelfde vraag.
[note|Marie-Lore] @michel doe jij?
[message|Marie-Lore] Ja, via Instellingen.

##### C3 | email | via=beacon-v2 | tags=[] | subj=None | 2026-09-03T08:00 -> active
[customer|Ann] Nog geen antwoord gekregen.
"""


def test_dump_parse():
    convs = corpus.dump_conversations(DUMP)
    assert [c["id"] for c in convs] == ["harvest:C1", "harvest:C2", "harvest:C3"]
    assert convs[0]["url"] is None and convs[0]["subject"] is None
    assert convs[0]["reply"]["text"] == "Dat kan via de prijslijst."
    assert convs[0]["later"] == [{"role": "customer", "text": "gevonden, dank"}]
    second = convs[1]
    assert second["channel"] == "chat" and second["tags"] == ["boekingen"]
    assert second["subject"] == "Re: agenda" and second["customer"] == "Davy"
    assert second["first_message"].endswith("Tweede regel van dezelfde vraag.")
    assert "doe jij" not in json.dumps(second)  # notes dropped
    assert convs[2]["reply"] is None


# ---------------------------------------------------------------- P0: role labels on a trace


def trace_header() -> dict:
    return json.loads((FIXTURES / "trace_header.json").read_text(encoding="utf-8"))


def test_trace_roles_sibling_vendor_is_unknown():
    """is_inbound is delivery direction: a cc'd sibling vendor also arrives inbound."""
    conv = corpus.trace_conversation(trace_header(), frozenset({"vendor.example"}))
    assert conv["id"] == "run:53ff0f29" and conv["tenant"] is None
    assert conv["first_message"].startswith("Hi there")
    roles = [turn["role"] for turn in conv["later"]]
    assert roles[0] == "unknown"  # the sibling vendor answering in-thread
    assert "not shown in the Power Sync toolbox" in conv["later"][0]["text"]
    assert "customer" in roles  # the real customer's follow-up is still labelled
    # the vendor's own reply is the answer, never a customer quote
    assert conv["reply"]["by"] == "Vendor <info@vendor.example>"
    assert conv["reply"]["provenance"] == "human"


def test_trace_agent_by_mailbox_domain():
    """An outbound-looking reply that arrives inbound is still ours when the domain is ours."""
    header = trace_header()
    header["prior_messages"][3]["is_inbound"] = True
    conv = corpus.trace_conversation(header, frozenset({"vendor.example"}))
    assert conv["reply"]["by"] == "Vendor <info@vendor.example>"
    conv = corpus.trace_conversation(header, frozenset())
    assert conv["reply"]["provenance"] == "draft"  # nothing provable → the draft answers
    assert [t["role"] for t in conv["later"]].count("unknown") == 2


CHAT_HEADER = {
    "run_id": "7dca3d0a-0000-4000-8000-000000000000",
    "created_at": "2026-09-03T11:44:56Z", "tenant": "yes_events", "topic": None,
    "question": "dank je", "draft": "",
    "prior_messages": [
        {"sender": "6a995d491a5dd2cd1ca240c3", "is_inbound": True, "body": "Monitoren"},
        {"sender": "operator+box@intercom.io", "is_inbound": False,
         "body": "Hey, ik ben Yumi! 👋 Als ik je ergens mee kan helpen mag je het mij hier laten weten."},
        {"sender": "operator+box@intercom.io", "is_inbound": False, "body": "Hey ! 👋"},
        {"sender": "6a995d491a5dd2cd1ca240c3", "is_inbound": True, "body": "Nieuwe monitor"},
        {"sender": "operator+box@intercom.io", "is_inbound": False,
         "body": "Waar kunnen we je mee helpen? 😄"},
        {"sender": "zita@example.com", "is_inbound": True, "body": "Hallo,"},
        {"sender": "zita@example.com", "is_inbound": True,
         "body": "worden er in Rumst geen kampen meer georganiseerd?"},
        {"sender": "operator+box@intercom.io", "is_inbound": False,
         "body": "In Rumst organiseren we dit jaar geen kampen meer, wel in Boom."},
    ],
}


def test_chat_boilerplate_and_question_turn():
    conv = corpus.trace_conversation(CHAT_HEADER, frozenset())
    assert conv["tenant"] == "yes_events"
    # the bot greeting, the bare "Hey !" and the flow prompt are gone, the salutation too
    assert "Yumi" not in json.dumps(conv) and "Hallo" not in json.dumps(conv)
    assert conv["first_message"] == "worden er in Rumst geen kampen meer georganiseerd?"
    assert conv["first_raw"] == "Monitoren"  # the menu click is kept, never as the question
    assert conv["reply"]["text"].startswith("In Rumst organiseren we")
    # the widget hands over an e-mail mid-chat: same person, still the customer
    assert all(t["role"] != "unknown" for t in conv["later"])


# ---------------------------------------------------------------- P1: noise, merge, links


def test_noise_pretagging():
    assert corpus.noise_reason("Invitation: 20 min meeting", "cal@x.be", "A new event") == \
        "calendar_invite"
    assert corpus.noise_reason("Backup failed", "noreply@x.be", "Job 12 failed") == "no_reply_sender"
    assert corpus.noise_reason(None, "pj@x.be", "test") == "test"
    assert corpus.noise_reason(None, "pj@x.be", "  ") == "empty"
    assert corpus.noise_reason("Hoe pas ik dit aan?", "pj@x.be", "Hoe pas ik dit aan?") is None


def test_calendar_invite_from_attachment():
    header = trace_header()
    header["prior_messages"][0]["attachments"] = [{"filename": "invite.ics"}]
    assert corpus.trace_conversation(header)["noise"]["reason"] == "calendar_invite"


def make(cid, customer, text, when, reply=None):
    return corpus.build(cid, None, "email", when, "subj", customer, [],
                        [("customer", text, customer)] + ([("agent", reply, "a")] if reply else []))


def test_merge_duplicates_on_sender_and_text():
    text = "Ik heb nog credit op mijn account en wil graag een kamp boeken, hoe doe ik dat?"
    convs = [make("run:aaa", "ann@x.be", text, "2026-09-02T10:00:00Z", "Antwoord."),
             make("run:bbb", "ann@x.be", text + "  ", "2026-09-02T10:20:00Z"),
             make("run:ccc", "ann@x.be", text, "2026-09-04T10:00:00Z")]
    merged = corpus.merge_duplicates(convs)
    assert [c["id"] for c in merged] == ["run:aaa", "run:ccc"]  # outside the hour stays its own
    assert merged[0]["tags"] == ["merged:run:bbb"]


def test_duplicate_outreach_tagged():
    pitch = "We build AI agents for support teams, are you interested in a quick call?"
    convs = [make("run:a", "sales1@x.be", pitch, "2026-09-02T10:00:00Z"),
             make("run:b", "sales2@y.be", pitch, "2026-09-03T10:00:00Z")]
    corpus.tag_duplicate_outreach(convs)
    assert [c["noise"]["reason"] for c in convs] == ["duplicate_outreach"] * 2


def test_link_articles_by_url_and_id():
    articles = [
        {"id": "A1", "url": "https://faq.example.be/nl/articles/3164619-feestdag",
         "path": "/kb/tenant/intercom/algemeen/3164619-feestdag.md"},
        {"id": "A2", "url": None, "path": "/kb/knowledgeowl/aanbod/aanbod-voorbereiden.md"},
        {"id": "A3", "url": "https://help.example.com/article/9287008-invoices",
         "path": "/kb/helpscout/billing/9287008-invoices.md"},
    ]
    convs = [make("c1", "a@x.be", "Gaat het kamp door op een feestdag?", "2026-09-02T10:00:00Z",
                  "Zie https://faq.example.be/nl/articles/3164619-feestdag voor meer."),
             make("c2", "b@x.be", "Waar staan mijn facturen ergens in het portaal?",
                  "2026-09-02T10:00:00Z", "See https://help.example.com/article/9287008-invoices")]
    corpus.link_articles(convs, articles)
    assert convs[0]["linked_articles"] == ["A1"]
    assert convs[1]["linked_articles"] == ["A3"]


def test_tsv_and_digest_tiers():
    evidence = {"project": "demo", "tenant": "yes_events", "window": {"days": 8},
                "source": "email_runs", "coverage": [], "kb": {"status": "complete", "root": "/kb"},
                "conversations": [corpus.trace_conversation(CHAT_HEADER)],
                "articles": [{"id": "A1", "home": "kb", "title": "Feestdagen", "path": "/kb/a.md",
                              "collection": "Algemeen", "audience": "customer",
                              "keywords": ["feestdag"]}]}
    convs_tsv, articles_tsv = corpus.write_tsvs(evidence)
    assert convs_tsv.splitlines()[0].split("\t")[:4] == ["id", "date", "channel", "tenant"]
    assert convs_tsv.splitlines()[1].split("\t")[3] == "yes_events"
    assert articles_tsv.splitlines()[1].split("\t")[3] == "customer"
    digest = corpus.write_digest(evidence)
    assert "demo / yes_events" in digest and "[customer|opener] Monitoren" in digest


# ---------------------------------------------------------------- P0: inventory + tenant plumbing


INDEX_ARTIFACT = """@@ /kb/tenant/intercom/INDEX.md
# KB index — intercom
# Paths are relative to this directory (kb/intercom/).
algemeen/3164619-feestdag.md · Gaat het kamp door op een feestdag? · feestdag, kampweek · Hoe wij feestdagen verrekenen. · collection: Algemeen · locale: nl · audience: customer
inschrijven/3164700-annuleren.md · Hoe annuleer ik? · annuleren · Annuleren en terugbetaling. · collection: Inschrijven
"""


def fake_workspace(monkeypatch, index_text=INDEX_ARTIFACT, count=2):
    """Record every rc argv; serve the console envelopes and artifacts the inventory expects."""
    seen: list[list[str]] = []

    def fake_run(argv, **kwargs):
        seen.append(list(argv))
        script = argv[-1]
        if "file" in argv and "get" in argv:
            Path(argv[argv.index("--out") + 1]).write_text(
                index_text if "hc-index" in argv[-3] else "", encoding="utf-8")
            return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
        if script.startswith("find /kb"):
            out = "/kb/tenant/intercom/INDEX.md\n/kb/knowledgeowl/INDEX.md\n"
        elif "hc-index.txt" in script:
            out = f"## /kb/tenant/intercom {count}\n"
        else:
            out = ""
        return type("R", (), {"returncode": 0, "stderr": "",
                              "stdout": json.dumps({"exit_code": 0, "stdout": out})})()

    monkeypatch.setattr(collect.subprocess, "run", fake_run)
    return seen


def test_inventory_tenant_scope_and_paths(monkeypatch, tmp_path):
    seen = fake_workspace(monkeypatch)
    articles, kb = collect.inventory(tmp_path, tmp_path, "yes_events")
    assert kb["root"] == "/kb/tenant/intercom" and kb["status"] == "complete"
    # provider comes from the path, not from the INDEX header comment (which says kb/intercom/)
    assert [a["path"] for a in articles] == [
        "/kb/tenant/intercom/algemeen/3164619-feestdag.md",
        "/kb/tenant/intercom/inschrijven/3164700-annuleren.md"]
    assert articles[0]["audience"] == "customer" and articles[0]["title"].startswith("Gaat het kamp")
    # --tenant rides on every console call, and only on console calls
    console_calls = [a for a in seen if a[:3] == ["rc", "dev", "console"]]
    assert console_calls and all(a[3:5] == ["--tenant", "yes_events"] for a in console_calls)
    assert not any("fleet" in a or "trace" in a for a in seen)


def test_inventory_project_scope_skips_tenant_kbs(monkeypatch, tmp_path):
    fake_workspace(monkeypatch, index_text=INDEX_ARTIFACT.replace("/kb/tenant/intercom",
                                                                  "/kb/knowledgeowl"))
    _, kb = collect.inventory(tmp_path, tmp_path, None)
    assert kb["root"] == "/kb/knowledgeowl"


def test_empty_inventory_is_a_hard_exit(monkeypatch, tmp_path):
    fake_workspace(monkeypatch, index_text="@@ /kb/tenant/intercom/INDEX.md\n# empty\n")
    with pytest.raises(SystemExit) as exc:
        collect.inventory(tmp_path, tmp_path, "yes_events")
    assert exc.value.code == 2


def test_partial_inventory_needs_a_real_file_count(monkeypatch, tmp_path):
    fake_workspace(monkeypatch, count=9)
    _, kb = collect.inventory(tmp_path, tmp_path, "yes_events")
    assert kb["status"] == "partial" and "2 indexed vs 9 .md on disk" in kb["reason"]


def test_tenant_required(monkeypatch, tmp_path):
    from collections import Counter
    with pytest.raises(SystemExit) as exc:
        collect.require_tenant(Counter({"lbv": 104, "yes_events": 64}))
    assert exc.value.code == 2
