"""Stubbed-API tests for the read-only dashboard settings helper."""

import sys
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib import settings  # noqa: E402


SCHEMA = {
    "resources": {
        "settings": {"fields": [
        {"key": "persona.tone", "help": "Reply voice", "type": "string", "settable_at": ["project", "tenant", "mailbox"]},
        {"key": "autonomy_mode", "help": "Draft or send automatically", "type": "enum", "enum": ["draft", "send"]},
        ]},
        "branding": {"fields": [{"key": "name", "help": "Brand name", "type": "string"}]},
    },
    "hierarchy_settings": {"channel": {
        "settable_at": ["project", "tenant", "mailbox"],
        "fields": ["labeling_enabled"],
        "field_schemas": [{"key": "channel.labeling_enabled", "help": "Apply labels", "type": "bool", "settable_at": ["project", "tenant", "mailbox"]}],
    }},
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


def test_find_keeps_hierarchy_types_and_qualifies_other_bags():
    caps = {"project": {"name": "demo"}, "writable_keys": ["channel.labeling_enabled", "branding.name"]}
    with mock.patch.dict("os.environ", {}, clear=True):
        labels = settings.find("apply labels", stub(caps))["candidates"][0]
        brand = settings.find("brand name", stub(caps))["candidates"][0]
    assert (labels["key"], labels["type"], labels["help"]) == ("channel.labeling_enabled", "bool", "Apply labels")
    assert brand["key"] == "branding.name"


def test_resolve_flat_bag_uses_qualified_key_and_bag_endpoint():
    caps = {"project": {"name": "demo"}, "writable_keys": ["branding.name"]}
    responses = {"branding": {"name": {"value": "Acme", "effective": "Acme", "source": "override"}}}
    with mock.patch.dict("os.environ", {}, clear=True):
        result = settings.resolve("branding.name", stub(caps, responses))
    assert result["levels"][0]["effective"]["effective"] == "Acme"


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
