from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class V4ReleaseContractTests(unittest.TestCase):
    def test_public_distribution_inventories_runtime_instead_of_private_release_state(self) -> None:
        from distribution_manifest import build_distribution_manifest
        paths = {entry["path"] for entry in build_distribution_manifest(ROOT)["files"]}
        self.assertIn("scripts/verify_distribution.py", paths)
        self.assertIn("references/development-graph.projection.schema.json", paths)
        self.assertFalse(any(path.startswith(("reports/", "docs/specifications/")) for path in paths))

    def test_generated_markdown_is_bound_to_a_fresh_synthetic_graph(self) -> None:
        from render_graph import render_markdown
        from validate_graph import validate_graph
        from test_validate_graph import valid_graph
        graph = valid_graph()
        markdown = render_markdown(graph, "development-graph.json")
        result = validate_graph(graph, markdown)
        self.assertTrue(result["valid"], result["errors"])
        changed = json.loads(json.dumps(graph))
        changed["graph_version"] += 1
        self.assertFalse(validate_graph(changed, markdown)["valid"])

    def test_public_metadata_names_supported_runtime_without_install_claims(self) -> None:
        text = "\n".join(p.read_text() for p in [ROOT / "README.md", ROOT / "SKILL.md",
                                              ROOT / "agents/openai.yaml", ROOT / "doc/architecture.md"])
        for expected in ("V4", "verify_distribution.py", "provider", "preview", "migration", "observability"):
            self.assertIn(expected, text)
        self.assertNotIn("Installation was performed", text)


if __name__ == "__main__":
    unittest.main()
