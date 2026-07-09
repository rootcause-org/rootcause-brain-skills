"""Tests for hosted-action helper metadata consumed by the dashboard overlay."""

from __future__ import annotations

import importlib
import sys
import unittest
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

HELPERS_YAML = Path(__file__).resolve().parents[1] / "lib" / "action" / "helpers.yaml"


class ActionHelperMetadata(unittest.TestCase):
    def setUp(self):
        self.catalog = yaml.safe_load(HELPERS_YAML.read_text(encoding="utf-8"))

    def test_catalog_names_real_public_action_helpers(self):
        families = self.catalog.get("families") or []
        self.assertGreaterEqual(len(families), 3)
        for family in families:
            module = importlib.import_module(family["source_module"])
            self.assertTrue(family["import"].startswith("from lib.action import "))
            for helper in family.get("helpers") or []:
                name = helper["name"]
                self.assertFalse(name.startswith("_"), f"{family['provider']} advertises private helper {name}")
                self.assertTrue(hasattr(module, name), f"{family['source_module']} missing {name}")

    def test_notion_metadata_keeps_connector_internals_private(self):
        notion = next(f for f in self.catalog["families"] if f["provider"] == "notion")
        names = {h["name"] for h in notion["helpers"]}
        for want in {"validate_database_values", "database_validation_summary", "create_database_row"}:
            self.assertIn(want, names)
        rendered = HELPERS_YAML.read_text(encoding="utf-8")
        self.assertNotIn("_compact_page", rendered)


if __name__ == "__main__":
    unittest.main()
