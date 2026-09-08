"""Normaliser tests for `brain-helpcenter-suggestions`. Synthetic data only — never real customers.

    cd skills/brain-helpcenter-suggestions && uv run --with pytest --no-project pytest tests -q
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

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
    seen = {}
    for conv in convs + convs:
        seen.setdefault(conv["id"], conv)
    assert len(seen) == 3


def test_signature_and_email_stripped():
    assert corpus.strip_quoted_and_signature(
        "Vraag?\nMet vriendelijke groeten\nLies") == "Vraag?"
    assert corpus.strip_quoted_and_signature("Vraag?\n> oud\nmeer") == "Vraag?"
    assert corpus.clean("mail me op lies@salon.be", 100) == "mail me op [e-mail]"


def test_flattened_harvest_shape():
    flat = [{
        "id": 444, "number": 44, "subject": None, "type": "email", "status": "closed",
        "source": {"type": "beacon-v2", "via": "customer"}, "tags": [],
        "createdAt": "2026-09-04T10:00:00Z",
        "threads": [
            {"type": "note", "by": "user", "first": "Michel", "at": "2026-09-04T10:00:00Z",
             "body": "Technical Information"},
            {"type": "customer", "by": "customer", "first": "Davy", "at": "2026-09-04T10:01:00Z",
             "body": "Kan ik dubbel boeken?"},
            {"type": "message", "by": "user", "first": "Marie-Lore", "at": "2026-09-04T10:05:00Z",
             "body": "Ja, via de agenda-instellingen."},
        ],
    }]
    conv = corpus.helpscout_conversations(flat)[0]
    assert conv["id"] == "hs:444" and conv["customer"] == "Davy"
    assert conv["first_message"] == "Kan ik dubbel boeken?"
    assert conv["reply"]["by"] == "Marie-Lore"


DUMP = """##### C1 | email | via=beacon-v2 | tags=[] | subj=None | 2026-09-01T09:00 -> closed 2026-09-01T09:39
[customer|LouLouGlow] bevestigingsmail aanpassen
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


def test_trace_header():
    header = {
        "run_id": "abcdef12-3456-7890-abcd-ef1234567890",
        "created_at": "2026-09-05T07:00:00Z",
        "topic": "agenda openzetten",
        "thread_id": "th-1",
        "question": "Hoe zet ik mijn agenda open tot december?",
        "draft": "Dat doe je via Instellingen > Agenda.",
        "prior_messages": [
            {"body": "Eerdere vraag over de agenda", "sender": "Davy",
             "sent_at": "2026-09-04T07:00:00Z", "is_inbound": True},
        ],
        "metadata": {"run_url": "https://app.example/runs/abcdef12"},
    }
    conv = corpus.trace_conversation(header)
    assert conv["id"] == "run:abcdef12" and conv["url"] == "https://app.example/runs/abcdef12"
    assert conv["subject"] == "agenda openzetten" and conv["customer"] == "Davy"
    assert conv["first_message"] == "Eerdere vraag over de agenda"
    assert conv["reply"]["provenance"] == "draft"
    assert conv["later"] == [{"role": "customer", "text": "Hoe zet ik mijn agenda open tot december?"}]
