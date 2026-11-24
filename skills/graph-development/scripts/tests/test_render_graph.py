from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1]
TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(TESTS))

from render_graph import render_markdown  # noqa: E402
from test_validate_graph import valid_graph  # noqa: E402
from validate_graph import canonical_checksum, validate_graph  # noqa: E402


class RenderGraphTests(unittest.TestCase):
    def test_render_is_deterministic_and_has_checksum(self) -> None:
        graph = valid_graph()
        first = render_markdown(graph)
        second = render_markdown(graph)
        self.assertEqual(first, second)
        self.assertIn(f"<!-- development-graph-sha256: {canonical_checksum(graph)} -->", first)
        self.assertIn("```mermaid", first)
        self.assertIn("## Node contracts", first)
        self.assertTrue(validate_graph(graph, first)["valid"])

    def test_cli_renders_then_validator_confirms_parity(self) -> None:
        graph = valid_graph()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            graph_path = root / "development-graph.json"
            markdown_path = root / "development-graph.md"
            graph_path.write_text(json.dumps(graph, indent=2), encoding="utf-8")
            render = subprocess.run(
                [sys.executable, str(SCRIPTS / "render_graph.py"), str(graph_path), "--output", str(markdown_path)],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(render.returncode, 0, render.stderr)
            validate = subprocess.run(
                [sys.executable, str(SCRIPTS / "validate_graph.py"), str(graph_path), "--markdown", str(markdown_path)],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(validate.returncode, 0, validate.stdout + validate.stderr)
            self.assertIn("Graph validation: PASS", validate.stdout)

    def test_cli_refuses_invalid_graph(self) -> None:
        graph = valid_graph()
        graph["nodes"][1]["state"] = "invalidated"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            graph_path = root / "development-graph.json"
            markdown_path = root / "development-graph.md"
            graph_path.write_text(json.dumps(graph), encoding="utf-8")
            result = subprocess.run(
                [sys.executable, str(SCRIPTS / "render_graph.py"), str(graph_path), "--output", str(markdown_path)],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(markdown_path.exists())


if __name__ == "__main__":
    unittest.main()
