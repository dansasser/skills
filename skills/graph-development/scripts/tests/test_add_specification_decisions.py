from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

from validate_graph import validate_graph  # noqa: E402
from validate_specifications import validate_specifications  # noqa: E402
try:
    from fixtures import apply_new_graph  # noqa: E402
except ModuleNotFoundError:
    from scripts.tests.fixtures import apply_new_graph  # noqa: E402


class AddSpecificationDecisionsTests(unittest.TestCase):
    def _scaffold(self, root: Path) -> tuple[Path, Path, Path]:
        command = [
            sys.executable,
            str(SCRIPTS / "new_graph.py"),
            "--project-root",
            str(root),
            "--output-dir",
            str(root),
            "--graph-id",
            "decision-helper-test",
            "--title",
            "Decision Helper Test",
            "--template",
            "specification-to-delivery",
            "--context-version",
            "test:decision-helper",
        ]
        result = apply_new_graph(command)
        self.assertEqual(result.returncode, 0, result.stderr)
        return (
            root / "development-graph.json",
            root / "specification-manifest.json",
            root / "development-graph.md",
        )

    def test_batch_adds_decision_nodes_records_and_missing_consumers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            graph_path, manifest_path, markdown_path = self._scaffold(root)
            decisions_path = root / "decisions.json"
            decisions_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "decisions": [
                            {
                                "id": "d1",
                                "title": "D1 Helper Wire Framing",
                                "question": "Which helper wire framing is authoritative?",
                                "status": "open",
                                "source_path": "doc/decisions/d1.md",
                                "affected_nodes": [
                                    "scaffold-helper",
                                    "implement-helper",
                                ],
                            },
                            {
                                "id": "d2",
                                "title": "D2 Durable Store",
                                "question": "Which durable store supports resumability?",
                                "status": "open",
                                "source_path": "doc/decisions/d2.md",
                                "affected_nodes": [
                                    "implement-persistence",
                                    "test-resume",
                                ],
                            },
                        ],
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            command = [
                sys.executable,
                str(SCRIPTS / "add_specification_decisions.py"),
                str(graph_path),
                str(manifest_path),
                str(decisions_path),
                "--markdown",
                str(markdown_path),
            ]

            result = subprocess.run(
                command,
                cwd=root.parent,
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Added specification decisions: d1, d2", result.stdout)
            graph = json.loads(graph_path.read_text(encoding="utf-8"))
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            markdown = markdown_path.read_text(encoding="utf-8")
            self.assertTrue(validate_graph(graph, markdown)["valid"])
            self.assertEqual(graph["graph_version"], 2)
            self.assertEqual(manifest["graph_version"], 2)
            nodes = {node["id"]: node for node in graph["nodes"]}
            self.assertNotIn("resolve-blocking-decisions", nodes)
            for decision_id in ("d1", "d2"):
                node = nodes[f"resolve-{decision_id}"]
                self.assertEqual(node["type"], "decision")
                self.assertEqual(node["state"], "needs_human")
                self.assertEqual(node["executor"]["kind"], "human")
                self.assertTrue((root / f"doc/decisions/{decision_id}.md").is_file())

            edges = {
                (
                    edge["from"],
                    edge["to"],
                    edge["type"],
                    tuple(edge["artifacts"]),
                )
                for edge in graph["edges"]
            }
            self.assertIn(
                (
                    "resolve-d1",
                    "scaffold-helper",
                    "consumes",
                    ("decision:d1",),
                ),
                edges,
            )
            self.assertIn(
                (
                    "author-implementation-plan",
                    "scaffold-helper",
                    "consumes",
                    ("doc/implementation-plan.md",),
                ),
                edges,
            )
            self.assertIn(
                (
                    "approve-specification",
                    "scaffold-helper",
                    "approves",
                    ("artifact:specification-approval",),
                ),
                edges,
            )
            self.assertIn(
                (
                    "scaffold-helper",
                    "verify-delivery",
                    "consumes",
                    ("artifact:scaffold-helper",),
                ),
                edges,
            )
            self.assertEqual(
                [decision["id"] for decision in manifest["decisions"]],
                ["d1", "d2"],
            )
            for node_id in [
                "scaffold-helper",
                "implement-helper",
                "implement-persistence",
                "test-resume",
            ]:
                self.assertIn(
                    node_id,
                    manifest["implementation_plan"]["implementation_nodes"],
                )
                self.assertIn(node_id, manifest["approval"]["targets"])

            for document in manifest["documents"]:
                (root / document["path"]).parent.mkdir(parents=True, exist_ok=True)
                (root / document["path"]).write_text(
                    f"# {document['id']}\n\nEvidence-backed test content.\n",
                    encoding="utf-8",
                )
            plan_path = root / manifest["implementation_plan"]["path"]
            plan_path.write_text("# Plan\n\nConsume the approved specification.\n", encoding="utf-8")
            report = validate_specifications(graph, manifest, root)
            self.assertTrue(report["valid"], report)

            before = graph_path.read_bytes()
            duplicate = subprocess.run(
                command,
                cwd=root.parent,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(duplicate.returncode, 0)
            self.assertEqual(graph_path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
