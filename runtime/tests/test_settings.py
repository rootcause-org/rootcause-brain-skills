"""Stubbed-API tests for the read-only dashboard settings helper."""

import sys
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib import settings  # noqa: E402


SCHEMA = {
    "resources": {"settings": {"fields": [
        {"key": "persona.tone", "help": "Reply voice", "type": "string", "settable_at": ["project", "tenant", "mailbox"]},
        {"key": "autonomy_mode", "help": "Draft or send automatically", "type": "enum", "enum": ["draft", "send"]},
    ]}},
    "hierarchy_settings": {"channel": {"settable_at": ["project", "tenant", "mailbox"], "fields": ["labeling_enabled"]}},
}


def stub(capabilities, responses=None):
    responses = responses or {}

    def fetch(path):
        if path == "meta/capabilities":
            return capabilities
        if path == "meta/schema":
            return SCHEMA
        return responses[path]

    return fetch


def test_find_ranks_voice_and_fences_tenant_pin():
    caps = {
        "project": {"name": "demo"}, "tenant": {"slug": "acme"},
        "writable_keys": ["persona.tone", "autonomy_mode", "channel.labeling_enabled"],
    }
    with mock.patch.dict("os.environ", {}, clear=True):
        result = settings.find("make our voice warmer", stub(caps))
    top = result["candidates"][0]
    assert top["key"] == "persona.tone"
    assert top["reachable_levels"] == ["tenant", "mailbox"]
    assert top["writable"] is True


def test_find_synthesizes_tenant_autonomy_discovery():
    caps = {"project": {"name": "demo"}, "tenant": {"slug": "acme"}, "writable_keys": ["autonomy_mode"]}
    with mock.patch.dict("os.environ", {}, clear=True):
        result = settings.find("stop auto sending", stub(caps))
    autonomy = next(row for row in result["candidates"] if row["key"] == "autonomy_mode")
    assert autonomy["settable_at"] == ["project", "tenant"]
    assert autonomy["reachable_levels"] == ["tenant"]
    assert "outside the hierarchy bag" in autonomy["note"]


def test_resolve_fetches_reachable_levels_and_keeps_provenance():
    caps = {"project": {"name": "demo"}, "tenant": {"slug": "acme"}, "writable_keys": ["persona.tone"]}
    responses = {
        "projects/demo/tenants/acme/settings?resolved=true": {
            "settings": {"persona": {}},
            "resolved": {"persona": {"tone": {"value": "formal", "source": "project"}}},
        },
    }
    with mock.patch.dict("os.environ", {}, clear=True):
        result = settings.resolve("persona.tone", stub(caps, responses))
    assert result["levels"] == [{
        "level": "tenant",
        "effective": {"value": "formal", "source": "project"},
        "override": None,
    }]


def test_requires_dashboard_environment_for_real_requests():
    with mock.patch.dict("os.environ", {}, clear=True), pytest.raises(settings.SettingsError, match="dashboard-only"):
        settings._fetch("meta/schema")
