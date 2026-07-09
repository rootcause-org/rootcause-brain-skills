"""Tests for hosted-action helper metadata consumed by the dashboard overlay."""

from __future__ import annotations

import ast
import unittest
from pathlib import Path
from typing import Any


ACTION_DIR = Path(__file__).resolve().parents[1] / "lib" / "action"
REQUIRED_KEYS = {
    "provider",
    "need",
    "connection",
    "import",
    "source_module",
    "manifest",
    "common_params",
    "useful_for",
    "helpers",
    "patterns",
    "validation_failure",
    "do_not",
}


def docs_constant(path: Path) -> tuple[dict[str, Any] | None, set[str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    functions = {node.name for node in tree.body if isinstance(node, ast.FunctionDef)}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if any(isinstance(target, ast.Name) and target.id == "ACTION_HELPER_DOCS" for target in node.targets):
            value = ast.literal_eval(node.value)
            if not isinstance(value, dict):
                raise AssertionError(f"{path}: ACTION_HELPER_DOCS must be a dict")
            return value, functions
    return None, functions


class ActionHelperMetadata(unittest.TestCase):
    def setUp(self):
        self.docs = []
        for path in sorted(ACTION_DIR.glob("*.py")):
            if path.name == "__init__.py":
                continue
            docs, functions = docs_constant(path)
            if docs is not None:
                self.docs.append((path, docs, functions))

    def test_metadata_is_python_local_and_names_real_public_helpers(self):
        self.assertGreaterEqual(len(self.docs), 3)
        seen = set()
        for path, docs, functions in self.docs:
            self.assertEqual(set(docs), REQUIRED_KEYS)
            provider = docs["provider"]
            self.assertEqual(path.name, f"{provider}.py")
            self.assertNotIn(provider, seen)
            seen.add(provider)
            self.assertEqual(docs["source_module"], f"lib.action.{provider}")
            self.assertEqual(docs["import"], f"from lib.action import {provider}")
            self.assertTrue(str(docs["connection"]).endswith(".write"))
            helpers = docs["helpers"]
            self.assertIsInstance(helpers, dict)
            self.assertGreater(len(helpers), 0)
            for name, purpose in helpers.items():
                self.assertFalse(name.startswith("_"), f"{provider} advertises private helper {name}")
                self.assertIn(name, functions, f"{path} metadata names missing helper {name}")
                self.assertIsInstance(purpose, str)
                self.assertTrue(purpose.strip())

    def test_notion_metadata_keeps_connector_internals_private(self):
        path, notion, _ = next(entry for entry in self.docs if entry[1]["provider"] == "notion")
        names = set(notion["helpers"])
        for want in {"validate_database_values", "database_validation_summary", "create_database_row"}:
            self.assertIn(want, names)
        rendered_source = path.read_text(encoding="utf-8")
        docs_source = rendered_source[
            rendered_source.index("ACTION_HELPER_DOCS") : rendered_source.index("@dataclass", rendered_source.index("ACTION_HELPER_DOCS"))
        ]
        self.assertNotIn("_compact_page", docs_source)


if __name__ == "__main__":
    unittest.main()
