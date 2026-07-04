"""Fixture tests for the Fireflies.ai connector (script connector — GraphQL transcript reads)."""

import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import responses as responses_lib
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib import api  # noqa: E402
from lib.connectors import fireflies as fireflies_conn  # noqa: E402

GRAPHQL_URL = "https://api.fireflies.ai/graphql"
TRANSCRIPT_ID = "01J4FIRETRANSCRIPT001"

_SEARCH_TRANSCRIPT = {
    "id": TRANSCRIPT_ID,
    "title": "Customer onboarding sync",
    "date": 1720467226660,
    "dateString": "2024-07-08T22:13:46.660Z",
    "duration": 1800,
    "organizer_email": "owner@example.com",
    "participants": ["customer@example.com", "owner@example.com"],
    "transcript_url": "https://app.fireflies.ai/view/customer-onboarding",
    "meeting_link": "https://meet.example.com/customer-onboarding",
    "meeting_info": {
        "summary_status": "processed",
        "fred_joined": True,
        "silent_meeting": False,
    },
    "summary": {
        "short_summary": "Customer asked about onboarding steps and migration timing.",
        "gist": "Onboarding sync",
        "action_items": ["Send migration checklist", "Confirm go-live date"],
    },
}

_TRANSCRIPT = {
    **_SEARCH_TRANSCRIPT,
    "privacy": "teammates",
    "audio_url": "https://audio.example.com/transcript.mp3",
    "video_url": None,
    "meeting_attendees": [
        {"displayName": "Customer One", "email": "customer@example.com", "phoneNumber": None, "name": "Customer One", "location": None},
        {"displayName": "Owner", "email": "owner@example.com", "phoneNumber": None, "name": "Owner", "location": None},
    ],
    "meeting_attendance": [
        {"name": "Customer One", "join_time": "2024-07-08T22:14:00Z", "leave_time": "2024-07-08T22:43:00Z"},
    ],
    "speakers": [{"id": "speaker-1", "name": "Customer One"}, {"id": "speaker-2", "name": "Owner"}],
    "summary": {
        **_SEARCH_TRANSCRIPT["summary"],
        "keywords": ["onboarding", "migration"],
        "outline": "Discussed setup, data migration, and timeline.",
        "shorthand_bullet": "Migration checklist needed.",
        "overview": "The team reviewed onboarding blockers.",
        "bullet_gist": "Onboarding blockers",
        "short_overview": "Reviewed onboarding blockers.",
        "meeting_type": "Customer Call",
        "topics_discussed": ["setup", "migration"],
        "transcript_chapters": [],
    },
    "sentences": [
        {
            "index": 0,
            "speaker_name": "Customer One",
            "speaker_id": "speaker-1",
            "text": "Can you send the migration checklist?",
            "raw_text": "Can you send the migration checklist?",
            "start_time": 12.4,
            "end_time": 15.2,
        },
        {
            "index": 1,
            "speaker_name": "Owner",
            "speaker_id": "speaker-2",
            "text": "Yes, I will send it after this call.",
            "raw_text": "Yes I will send it after this call.",
            "start_time": 16.1,
            "end_time": 19.7,
        },
    ],
}


class FirefliesManifest(unittest.TestCase):
    def setUp(self):
        api.MANIFESTS.clear()
        api._YAML_LOADED_KEYS.clear()

    def test_manifest_loaded_from_yaml(self):
        manifests = api.load_manifests()
        self.assertIn("fireflies", manifests)
        f = manifests["fireflies"]
        self.assertEqual(f.key, "fireflies")
        self.assertEqual(f.base_url, GRAPHQL_URL)
        self.assertEqual(f.auth.strategy, "bearer")
        self.assertEqual(f.pagination.style, "none")

    def test_raw_manifest_catalog_fields(self):
        manifest_path = Path(__file__).resolve().parents[1] / "lib" / "connectors" / "fireflies" / "manifest.yaml"
        raw = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(raw["connector_module"], "lib.connectors.fireflies")
        self.assertEqual(raw["env_var"], "RC_CONN_FIREFLIES")
        self.assertEqual(raw["credential_exposure"], "env")
        self.assertNotIn("oauth", raw)
        self.assertIn("api.fireflies.ai", raw["egress_hosts"])

    def test_connector_registers_same_key(self):
        self.assertEqual(fireflies_conn.MANIFEST.key, "fireflies")
        self.assertEqual(fireflies_conn.MANIFEST.base_url, GRAPHQL_URL)


class FirefliesSearch(unittest.TestCase):
    def setUp(self):
        self._saved = os.environ.get("RC_CONN_FIREFLIES")
        os.environ["RC_CONN_FIREFLIES"] = "ff_test_token"

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_FIREFLIES", None)
        else:
            os.environ["RC_CONN_FIREFLIES"] = self._saved

    @responses_lib.activate
    def test_search_filters_by_participant_organizer_dates_and_limit(self):
        responses_lib.add(
            responses_lib.POST,
            GRAPHQL_URL,
            json={"data": {"transcripts": [_SEARCH_TRANSCRIPT]}},
            status=200,
        )

        results = fireflies_conn.search_transcripts(
            participants=["customer@example.com"],
            organizers=["owner@example.com"],
            from_date="2024-07-01",
            to_date="2024-07-09T10:11:12Z",
            limit=5,
        )

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["title"], "Customer onboarding sync")
        body = json.loads(responses_lib.calls[0].request.body)
        self.assertIn("transcripts(", body["query"])
        self.assertNotIn("mutation", body["query"])
        self.assertEqual(body["variables"]["participants"], ["customer@example.com"])
        self.assertEqual(body["variables"]["organizers"], ["owner@example.com"])
        self.assertEqual(body["variables"]["fromDate"], "2024-07-01T00:00:00.000Z")
        self.assertEqual(body["variables"]["toDate"], "2024-07-09T10:11:12.000Z")
        self.assertEqual(body["variables"]["limit"], 5)

    @responses_lib.activate
    def test_search_bearer_auth(self):
        responses_lib.add(responses_lib.POST, GRAPHQL_URL, json={"data": {"transcripts": []}}, status=200)

        fireflies_conn.search_transcripts(participants=["customer@example.com"])

        auth = responses_lib.calls[0].request.headers.get("Authorization", "")
        self.assertEqual(auth, "Bearer ff_test_token")
        self.assertEqual(responses_lib.calls[0].request.headers.get("Content-Type"), "application/json")

    @responses_lib.activate
    def test_search_keyword_scope(self):
        responses_lib.add(responses_lib.POST, GRAPHQL_URL, json={"data": {"transcripts": []}}, status=200)

        fireflies_conn.search_transcripts(keyword="onboarding", scope="all", limit=10)

        body = json.loads(responses_lib.calls[0].request.body)
        self.assertEqual(body["variables"]["keyword"], "onboarding")
        self.assertEqual(body["variables"]["scope"], "all")

    def test_search_rejects_limit_over_provider_cap(self):
        with self.assertRaises(ValueError):
            fireflies_conn.search_transcripts(limit=51)

    @responses_lib.activate
    def test_graphql_errors_raise_api_error(self):
        responses_lib.add(
            responses_lib.POST,
            GRAPHQL_URL,
            json={"errors": [{"message": "invalid participant email"}]},
            status=200,
        )

        with self.assertRaises(api.ApiError) as ctx:
            fireflies_conn.search_transcripts(participants=["not-an-email"])
        self.assertIn("invalid participant email", str(ctx.exception))


class FirefliesShow(unittest.TestCase):
    def setUp(self):
        self._saved = os.environ.get("RC_CONN_FIREFLIES")
        os.environ["RC_CONN_FIREFLIES"] = "ff_show_token"

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_FIREFLIES", None)
        else:
            os.environ["RC_CONN_FIREFLIES"] = self._saved

    @responses_lib.activate
    def test_get_transcript_compacts_summary_metadata_and_sentences(self):
        responses_lib.add(
            responses_lib.POST,
            GRAPHQL_URL,
            json={"data": {"transcript": _TRANSCRIPT}},
            status=200,
        )

        result = fireflies_conn.get_transcript(TRANSCRIPT_ID)

        self.assertEqual(result["id"], TRANSCRIPT_ID)
        self.assertEqual(result["short_summary"], "Customer asked about onboarding steps and migration timing.")
        self.assertEqual(result["meeting_attendees"][0]["email"], "customer@example.com")
        self.assertEqual(result["sentences"][0]["speaker_name"], "Customer One")
        body = json.loads(responses_lib.calls[0].request.body)
        self.assertEqual(body["variables"]["transcriptId"], TRANSCRIPT_ID)
        self.assertIn("transcript(id: $transcriptId)", body["query"])
        self.assertNotIn("mutation", body["query"])

    @responses_lib.activate
    def test_cli_search_prints_markdown(self):
        responses_lib.add(
            responses_lib.POST,
            GRAPHQL_URL,
            json={"data": {"transcripts": [_SEARCH_TRANSCRIPT]}},
            status=200,
        )

        buf = io.StringIO()
        with patch("sys.stdout", buf):
            rc = fireflies_conn.main(["search", "--participant", "customer@example.com", "--limit", "1"])

        self.assertEqual(rc, 0)
        output = buf.getvalue()
        self.assertIn("# Fireflies transcripts (1)", output)
        self.assertIn("Customer onboarding sync", output)

    @responses_lib.activate
    def test_cli_show_writes_sentences_to_file_and_keeps_stdout_compact(self):
        responses_lib.add(
            responses_lib.POST,
            GRAPHQL_URL,
            json={"data": {"transcript": _TRANSCRIPT}},
            status=200,
        )

        with tempfile.TemporaryDirectory() as tmp:
            out_path = Path(tmp) / "fireflies-transcript.txt"
            buf = io.StringIO()
            with patch("sys.stdout", buf):
                rc = fireflies_conn.main(["show", TRANSCRIPT_ID, "--sentences-file", str(out_path)])

            self.assertEqual(rc, 0)
            stdout = buf.getvalue()
            self.assertIn("Full transcript sentences written", stdout)
            self.assertNotIn("Can you send the migration checklist?", stdout)
            written = out_path.read_text(encoding="utf-8")
            self.assertIn("Customer One: Can you send the migration checklist?", written)
            self.assertIn("Owner: Yes, I will send it after this call.", written)

    @responses_lib.activate
    def test_cli_show_without_file_reports_sentence_count_not_full_text(self):
        responses_lib.add(
            responses_lib.POST,
            GRAPHQL_URL,
            json={"data": {"transcript": _TRANSCRIPT}},
            status=200,
        )

        buf = io.StringIO()
        with patch("sys.stdout", buf):
            rc = fireflies_conn.main(["show", TRANSCRIPT_ID])

        self.assertEqual(rc, 0)
        stdout = buf.getvalue()
        self.assertIn("Sentence count: 2", stdout)
        self.assertNotIn("Can you send the migration checklist?", stdout)

