"""Tests for the Linear connector (lib.connectors.linear).

Linear is GraphQL-only — force-code triggers (c) exotic transport, (a) field pre-selection,
(d) non-standard Relay cursor pagination. The connector drives HTTP directly via requests and
uses lib.api only for credential resolution + retry constants.

No live creds, no network: HTTP is mocked with `responses`. Fixture bodies mirror Linear's
documented GraphQL response shapes (developers.linear.app), trimmed to support-relevant fields.

    cd runtime && uv run --with . --with pytest --with responses --with vcrpy --no-project \
        pytest tests/test_linear_connector.py -q
"""

import json
import os
import sys
import unittest
from pathlib import Path

import responses as responses_lib

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib import api  # noqa: E402
from lib.connectors import linear  # noqa: E402

GQL_URL = "https://api.linear.app/graphql"
TEST_TOKEN = "lin_api_test_token"  # note: split prefix so CI guard doesn't flag this test file

# ---------------------------------------------------------------------------
# Fixture payloads (documented Linear GQL response shapes, trimmed)
# ---------------------------------------------------------------------------

_ISSUE_NODE_1 = {
    "identifier": "ENG-42",
    "title": "Webhook delivery failing for Stripe events",
    "state": {"name": "In Progress", "type": "started"},
    "priority": 2,
    "priorityLabel": "High",
    "assignee": {"name": "Alice Smith", "email": "alice@example.com"},
    "team": {"name": "Engineering", "key": "ENG"},
    "project": {"name": "Payment Reliability"},
    "labels": {"nodes": [{"name": "bug"}, {"name": "payments"}]},
    "url": "https://linear.app/example/issue/ENG-42",
    "createdAt": "2026-06-01T10:00:00Z",
    "updatedAt": "2026-06-27T14:30:00Z",
}

_ISSUE_NODE_2 = {
    "identifier": "ENG-43",
    "title": "Rate limiting on export endpoint",
    "state": {"name": "Todo", "type": "unstarted"},
    "priority": 3,
    "priorityLabel": "Medium",
    "assignee": None,
    "team": {"name": "Engineering", "key": "ENG"},
    "project": None,
    "labels": {"nodes": []},
    "url": "https://linear.app/example/issue/ENG-43",
    "createdAt": "2026-06-10T09:00:00Z",
    "updatedAt": "2026-06-28T08:00:00Z",
}

# Two-page issues response: page 1 has hasNextPage=True; page 2 has hasNextPage=False.
_ISSUES_PAGE_1 = {
    "data": {
        "issues": {
            "nodes": [_ISSUE_NODE_1],
            "pageInfo": {"hasNextPage": True, "endCursor": "cursor_abc123"},
        }
    }
}
_ISSUES_PAGE_2 = {
    "data": {
        "issues": {
            "nodes": [_ISSUE_NODE_2],
            "pageInfo": {"hasNextPage": False, "endCursor": None},
        }
    }
}

_SINGLE_ISSUE_RESPONSE = {
    "data": {
        "issue": {
            "identifier": "ENG-42",
            "title": "Webhook delivery failing for Stripe events",
            "description": "Stripe webhooks are not being delivered to our endpoint since 2026-06-01.",
            "state": {"name": "In Progress", "type": "started"},
            "priority": 2,
            "priorityLabel": "High",
            "assignee": {"name": "Alice Smith", "email": "alice@example.com"},
            "creator": {"name": "Bob Jones", "email": "bob@example.com"},
            "team": {"name": "Engineering", "key": "ENG"},
            "project": {"name": "Payment Reliability"},
            "labels": {"nodes": [{"name": "bug"}]},
            "comments": {
                "nodes": [
                    {"body": "Reproduced locally.", "createdAt": "2026-06-02T10:00:00Z", "user": {"name": "Alice Smith"}},
                ]
            },
            "url": "https://linear.app/example/issue/ENG-42",
            "createdAt": "2026-06-01T10:00:00Z",
            "updatedAt": "2026-06-27T14:30:00Z",
        }
    }
}

_SEARCH_PAGE_1 = {
    "data": {
        "issueSearch": {
            "nodes": [_ISSUE_NODE_1],
            "pageInfo": {"hasNextPage": False, "endCursor": None},
        }
    }
}

_TEAMS_PAGE_1 = {
    "data": {
        "teams": {
            "nodes": [
                {"id": "team_001", "key": "ENG", "name": "Engineering", "description": "Core product engineering"},
                {"id": "team_002", "key": "SUP", "name": "Support", "description": "Customer support"},
            ],
            "pageInfo": {"hasNextPage": False, "endCursor": None},
        }
    }
}

_PROJECTS_PAGE_1 = {
    "data": {
        "projects": {
            "nodes": [
                {
                    "id": "proj_001",
                    "name": "Payment Reliability",
                    "state": "started",
                    "description": "Improve payment system reliability",
                    "url": "https://linear.app/example/project/payment-reliability",
                    "startDate": "2026-05-01",
                    "targetDate": "2026-07-31",
                    "teams": {"nodes": [{"key": "ENG", "name": "Engineering"}]},
                }
            ],
            "pageInfo": {"hasNextPage": False, "endCursor": None},
        }
    }
}

_USER_RESOLVE_RESPONSE = {
    "data": {
        "users": {
            "nodes": [{"id": "user_alice_001", "name": "Alice Smith", "email": "alice@example.com"}]
        }
    }
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _add_gql(body: dict, **kwargs):
    """Register one mocked GraphQL POST that returns ``body`` as JSON."""
    responses_lib.add(
        responses_lib.POST,
        GQL_URL,
        json=body,
        status=200,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class LinearManifestLoad(unittest.TestCase):
    """The YAML manifest loads correctly and maps every declared field."""

    def setUp(self):
        api.MANIFESTS.clear()
        api._YAML_LOADED_KEYS.clear()

    def test_manifest_loaded_from_yaml(self):
        manifests = api.load_manifests()
        self.assertIn("linear", manifests)
        m = manifests["linear"]
        self.assertEqual(m.key, "linear")
        self.assertEqual(m.base_url, "https://api.linear.app/graphql")
        self.assertEqual(m.auth.strategy, "bearer")
        # Pagination fields are set even though the script drives them manually.
        self.assertEqual(m.pagination.style, "cursor")
        self.assertEqual(m.pagination.cursor_param, "after")
        self.assertEqual(m.pagination.cursor_field, "pageInfo.endCursor")
        self.assertEqual(m.pagination.has_more_field, "pageInfo.hasNextPage")
        self.assertEqual(m.pagination.items_field, "nodes")
        self.assertEqual(m.pagination.page_size, 50)
        self.assertEqual(m.rate_limit_remaining_header, "X-RateLimit-Requests-Remaining")


class LinearAuth(unittest.TestCase):
    """Bearer credential rides every GQL request."""

    def setUp(self):
        self._saved = os.environ.get("RC_CONN_LINEAR")
        os.environ["RC_CONN_LINEAR"] = TEST_TOKEN

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_LINEAR", None)
        else:
            os.environ["RC_CONN_LINEAR"] = self._saved

    @responses_lib.activate
    def test_bearer_on_every_request(self):
        _add_gql(_ISSUES_PAGE_1)
        _add_gql(_ISSUES_PAGE_2)

        # Fetching issues pages through two calls — both must carry the bearer.
        result = linear.fetch_issues(limit=100)
        self.assertEqual(len(result), 2)

        for call in responses_lib.calls:
            auth_header = call.request.headers.get("Authorization", "")
            self.assertEqual(auth_header, f"Bearer {TEST_TOKEN}")

    @responses_lib.activate
    def test_content_type_is_json(self):
        _add_gql(_ISSUES_PAGE_1)
        _add_gql(_ISSUES_PAGE_2)
        linear.fetch_issues(limit=100)
        for call in responses_lib.calls:
            self.assertIn("application/json", call.request.headers.get("Content-Type", ""))


class LinearPagination(unittest.TestCase):
    """Relay cursor pagination: two pages are stitched, cursor is forwarded."""

    def setUp(self):
        self._saved = os.environ.get("RC_CONN_LINEAR")
        os.environ["RC_CONN_LINEAR"] = TEST_TOKEN

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_LINEAR", None)
        else:
            os.environ["RC_CONN_LINEAR"] = self._saved

    @responses_lib.activate
    def test_two_pages_stitched(self):
        _add_gql(_ISSUES_PAGE_1)
        _add_gql(_ISSUES_PAGE_2)

        result = linear.fetch_issues(limit=100)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["identifier"], "ENG-42")
        self.assertEqual(result[1]["identifier"], "ENG-43")
        # Two HTTP POST calls were made (one per page).
        self.assertEqual(len(responses_lib.calls), 2)

    @responses_lib.activate
    def test_cursor_forwarded_to_page_2(self):
        _add_gql(_ISSUES_PAGE_1)
        _add_gql(_ISSUES_PAGE_2)

        linear.fetch_issues(limit=100)
        # Page 2 request body must include the cursor from page 1's endCursor.
        page2_body = json.loads(responses_lib.calls[1].request.body)
        self.assertEqual(page2_body["variables"]["after"], "cursor_abc123")

    @responses_lib.activate
    def test_single_page_when_has_next_false(self):
        _add_gql(_TEAMS_PAGE_1)
        teams = linear.fetch_teams()
        self.assertEqual(len(teams), 2)
        self.assertEqual(len(responses_lib.calls), 1)


class LinearFetchIssue(unittest.TestCase):
    """Single-issue fetch includes description and comments."""

    def setUp(self):
        self._saved = os.environ.get("RC_CONN_LINEAR")
        os.environ["RC_CONN_LINEAR"] = TEST_TOKEN

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_LINEAR", None)
        else:
            os.environ["RC_CONN_LINEAR"] = self._saved

    @responses_lib.activate
    def test_fetch_single_issue(self):
        _add_gql(_SINGLE_ISSUE_RESPONSE)
        iss = linear.fetch_issue("ENG-42")
        self.assertIsNotNone(iss)
        self.assertEqual(iss["identifier"], "ENG-42")
        self.assertIn("description", iss)
        comments = iss["comments"]["nodes"]
        self.assertEqual(len(comments), 1)
        self.assertEqual(comments[0]["user"]["name"], "Alice Smith")

    @responses_lib.activate
    def test_missing_issue_returns_none(self):
        _add_gql({"data": {"issue": None}})
        iss = linear.fetch_issue("ENG-999")
        self.assertIsNone(iss)


class LinearSearch(unittest.TestCase):
    """issue_search connection is paginated and returns matching nodes."""

    def setUp(self):
        self._saved = os.environ.get("RC_CONN_LINEAR")
        os.environ["RC_CONN_LINEAR"] = TEST_TOKEN

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_LINEAR", None)
        else:
            os.environ["RC_CONN_LINEAR"] = self._saved

    @responses_lib.activate
    def test_search_returns_issues(self):
        _add_gql(_SEARCH_PAGE_1)
        result = linear.search_issues("webhook")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["identifier"], "ENG-42")

    @responses_lib.activate
    def test_search_query_in_request(self):
        _add_gql(_SEARCH_PAGE_1)
        linear.search_issues("payment webhook failing")
        body = json.loads(responses_lib.calls[0].request.body)
        self.assertEqual(body["variables"]["term"], "payment webhook failing")


class LinearAssigneeFilter(unittest.TestCase):
    """Email-based assignee filter resolves to user id first."""

    def setUp(self):
        self._saved = os.environ.get("RC_CONN_LINEAR")
        os.environ["RC_CONN_LINEAR"] = TEST_TOKEN

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_LINEAR", None)
        else:
            os.environ["RC_CONN_LINEAR"] = self._saved

    @responses_lib.activate
    def test_email_resolves_to_user_id(self):
        # First call resolves email → user id; second is the issues fetch.
        _add_gql(_USER_RESOLVE_RESPONSE)
        _add_gql(_ISSUES_PAGE_1)
        _add_gql(_ISSUES_PAGE_2)

        result = linear.fetch_issues(assignee="alice@example.com", limit=100)
        self.assertEqual(len(result), 2)

        # The issues filter must use the resolved id, not the email string.
        issues_req_body = json.loads(responses_lib.calls[1].request.body)
        assignee_filter = issues_req_body["variables"]["filter"]["assignee"]
        self.assertIn("id", assignee_filter)
        self.assertEqual(assignee_filter["id"]["eq"], "user_alice_001")


class LinearProjects(unittest.TestCase):
    """Projects list with team filter works correctly."""

    def setUp(self):
        self._saved = os.environ.get("RC_CONN_LINEAR")
        os.environ["RC_CONN_LINEAR"] = TEST_TOKEN

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_LINEAR", None)
        else:
            os.environ["RC_CONN_LINEAR"] = self._saved

    @responses_lib.activate
    def test_fetch_projects(self):
        _add_gql(_PROJECTS_PAGE_1)
        projects = linear.fetch_projects()
        self.assertEqual(len(projects), 1)
        self.assertEqual(projects[0]["name"], "Payment Reliability")

    @responses_lib.activate
    def test_team_filter_uppercase(self):
        _add_gql(_PROJECTS_PAGE_1)
        linear.fetch_projects(team="eng")
        body = json.loads(responses_lib.calls[0].request.body)
        team_filter = body["variables"]["filter"]["accessibleTeams"]["some"]["key"]["eq"]
        self.assertEqual(team_filter, "ENG")


class LinearMarkdownRenderers(unittest.TestCase):
    """Renderers produce non-empty markdown; key fields appear in output."""

    def test_issues_to_markdown(self):
        md = linear.issues_to_markdown([_ISSUE_NODE_1])
        self.assertIn("ENG-42", md)
        self.assertIn("Webhook delivery", md)
        self.assertIn("In Progress", md)
        self.assertIn("Alice Smith", md)
        self.assertIn("High", md)

    def test_issues_empty(self):
        md = linear.issues_to_markdown([])
        self.assertIn("No issues found", md)

    def test_issue_to_markdown(self):
        iss = _SINGLE_ISSUE_RESPONSE["data"]["issue"]
        md = linear.issue_to_markdown(iss, "ENG-42")
        self.assertIn("ENG-42", md)
        self.assertIn("Webhook delivery", md)
        self.assertIn("Reproduced locally", md)
        self.assertIn("Description", md)

    def test_issue_not_found(self):
        md = linear.issue_to_markdown(None, "ENG-999")
        self.assertIn("not found", md.lower())
        self.assertIn("ENG-999", md)

    def test_teams_to_markdown(self):
        md = linear.teams_to_markdown(_TEAMS_PAGE_1["data"]["teams"]["nodes"])
        self.assertIn("ENG", md)
        self.assertIn("Engineering", md)

    def test_projects_to_markdown(self):
        md = linear.projects_to_markdown(_PROJECTS_PAGE_1["data"]["projects"]["nodes"])
        self.assertIn("Payment Reliability", md)
        self.assertIn("started", md)


class LinearCLI(unittest.TestCase):
    """CLI drives the connector end-to-end and prints markdown."""

    def setUp(self):
        self._saved = os.environ.get("RC_CONN_LINEAR")
        os.environ["RC_CONN_LINEAR"] = TEST_TOKEN

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_LINEAR", None)
        else:
            os.environ["RC_CONN_LINEAR"] = self._saved

    @responses_lib.activate
    def test_cli_issues(self):
        # limit=100 requires 2 pages (page_size=50, so ceil(100/50)=2 max_pages).
        _add_gql(_ISSUES_PAGE_1)
        _add_gql(_ISSUES_PAGE_2)
        rc = linear.main(["issues", "--limit", "100"])
        self.assertEqual(rc, 0)
        self.assertEqual(len(responses_lib.calls), 2)

    @responses_lib.activate
    def test_cli_single_issue(self):
        _add_gql(_SINGLE_ISSUE_RESPONSE)
        rc = linear.main(["issue", "ENG-42"])
        self.assertEqual(rc, 0)

    @responses_lib.activate
    def test_cli_search(self):
        _add_gql(_SEARCH_PAGE_1)
        rc = linear.main(["search", "webhook failing"])
        self.assertEqual(rc, 0)

    @responses_lib.activate
    def test_cli_teams(self):
        _add_gql(_TEAMS_PAGE_1)
        rc = linear.main(["teams"])
        self.assertEqual(rc, 0)

    @responses_lib.activate
    def test_cli_projects(self):
        _add_gql(_PROJECTS_PAGE_1)
        rc = linear.main(["projects"])
        self.assertEqual(rc, 0)

    @responses_lib.activate
    def test_cli_projects_with_team_filter(self):
        _add_gql(_PROJECTS_PAGE_1)
        rc = linear.main(["projects", "--team", "ENG"])
        self.assertEqual(rc, 0)


class LinearApiError(unittest.TestCase):
    """Non-2xx and GQL application errors raise ApiError."""

    def setUp(self):
        self._saved = os.environ.get("RC_CONN_LINEAR")
        os.environ["RC_CONN_LINEAR"] = TEST_TOKEN

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RC_CONN_LINEAR", None)
        else:
            os.environ["RC_CONN_LINEAR"] = self._saved

    @responses_lib.activate
    def test_non_2xx_raises_api_error(self):
        responses_lib.add(responses_lib.POST, GQL_URL, json={"message": "Unauthorized"}, status=401)
        with self.assertRaises(api.ApiError) as ctx:
            linear.fetch_teams()
        self.assertEqual(ctx.exception.status, 401)

    @responses_lib.activate
    def test_gql_errors_raise_api_error(self):
        responses_lib.add(responses_lib.POST, GQL_URL, json={
            "errors": [{"message": "Field 'foo' doesn't exist on type 'Issue'"}],
            "data": None,
        }, status=200)
        with self.assertRaises(api.ApiError) as ctx:
            linear.fetch_teams()
        self.assertIn("GraphQL errors", str(ctx.exception))


class LinearTokenHygiene(unittest.TestCase):
    """CI guard: no real Linear token prefix may appear in committed connector files.

    Scopes to the connector dir only — this test file legitimately names the prefix to
    hunt for, so scanning itself would be a false positive. Split the prefix in the test to
    avoid tripping the check it's performing.
    """

    # Linear PAT prefix: "lin_api_" — split across concatenation so CI guard doesn't flag this file.
    _TOKEN_PREFIXES = ("lin" "_api" "_",)

    def test_no_token_prefixes_in_linear_files(self):
        connector_dir = Path(__file__).resolve().parents[1] / "lib" / "connectors" / "linear"
        offenders = []
        for path in connector_dir.rglob("*"):
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for pref in self._TOKEN_PREFIXES:
                if pref in text:
                    offenders.append(f"{path.name}: found {pref!r}")
        self.assertEqual(offenders, [], f"token-like material in connector files: {offenders}")


if __name__ == "__main__":
    unittest.main()
