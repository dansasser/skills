from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

from graph_core import ready_node_ids
from graphctl import approve_node, claim_node, complete_node, initialize_run

try:
    from fixtures import apply_new_graph
except ModuleNotFoundError:
    from scripts.tests.fixtures import apply_new_graph


class OptionalImplementationPlanTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def scaffold_command(self) -> list[str]:
        return [
            sys.executable, str(SCRIPTS / "new_graph.py"),
            "--project-root", str(self.root), "--output-dir", str(self.root),
            "--graph-id", "spec-handoff", "--title", "Specification handoff",
            "--template", "specification-to-delivery", "--context-version", "test:handoff",
            "--no-implementation-plan",
        ]

    def specctl(self, operation: str) -> list[str]:
        return [
            sys.executable, str(SCRIPTS / "specctl.py"), operation,
            "--project-root", str(self.root), "--graph", "development-graph.json",
            "--manifest", "specification-manifest.json", "--markdown", "development-graph.md",
        ]

    def run_json(self, command: list[str]) -> dict:
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        return json.loads(result.stdout)

    def read(self, path: str) -> dict:
        return json.loads((self.root / path).read_text(encoding="utf-8"))

    def scaffold(self) -> None:
        result = apply_new_graph(self.scaffold_command())
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)

    def test_complete_spec_scaffolds_without_plan_and_still_requires_approval(self) -> None:
        preview = self.run_json(self.scaffold_command())
        self.assertTrue(preview["valid"])
        self.assertEqual(list(self.root.iterdir()), [])
        self.scaffold()

        manifest = self.read("specification-manifest.json")
        self.assertEqual(manifest["implementation_nodes"], ["implement"])
        self.assertNotIn("implementation_plan", manifest)
        self.assertFalse((self.root / "doc/implementation-plan.md").exists())
        graph = self.read("development-graph.json")
        nodes = {node["id"]: node for node in graph["nodes"]}
        self.assertNotIn("author-implementation-plan", nodes)
        self.assertNotIn("resolve-blocking-decisions", nodes)
        self.assertTrue(self.run_json(self.specctl("verify"))["valid"])

        graph_path = self.root / "development-graph.json"
        run_dir = self.root / ".development-graph"
        run = initialize_run(graph_path, run_dir, actor="test")
        for node_id in (
            "capture-project-context", "define-product-and-scope",
            "define-constraints-and-risks", "define-architecture-and-interfaces",
            "define-acceptance-contract", "inventory-open-decisions",
            "verify-specification-set",
        ):
            run = claim_node(graph_path, run_dir, node_id, "test", run["run_version"])
            run = complete_node(
                graph_path, run_dir, node_id, "test", run["run_version"],
                {"source": "test:specification-handoff"},
            )
        self.assertEqual(run["nodes"]["approve-specification"]["state"], "needs_human")
        self.assertEqual(run["nodes"]["implement"]["state"], "blocked")
        run = approve_node(
            graph_path, run_dir, "approve-specification", "test-approver",
            run["run_version"], "test:verified-specification",
        )
        self.assertEqual(run["nodes"]["implement"]["state"], "ready")

    def test_scoped_decision_sync_leaves_approved_independent_work_ready(self) -> None:
        self.scaffold()
        decisions_path = self.root / "decisions.json"
        decisions_path.write_text(json.dumps({
            "schema_version": 1,
            "decisions": [{
                "id": "export-format", "title": "Export format",
                "question": "Which export format should the optional exporter support?",
                "status": "open", "source_path": "doc/decisions/export-format.md",
                "affected_nodes": ["implement-optional-export"],
            }],
        }), encoding="utf-8")
        command = self.specctl("sync") + ["--decisions", "decisions.json"]
        preview = self.run_json(command)
        self.assertTrue(preview["valid"])
        applied = self.run_json(command + [
            "--apply", "--expected-base-digest", preview["base_digest"],
            "--expected-plan-digest", preview["plan_digest"],
            "--actor", "test", "--authority", "test:selective-decision",
        ])
        self.assertTrue(applied["applied"])
        self.assertTrue(self.run_json(self.specctl("verify") + ["--decisions", "decisions.json"])["valid"])

        graph = self.read("development-graph.json")
        manifest = self.read("specification-manifest.json")
        self.assertCountEqual(manifest["implementation_nodes"], ["implement", "implement-optional-export"])
        consumers = {
            edge["to"] for edge in graph["edges"]
            if edge["from"] == "resolve-export-format" and edge["type"] == "consumes"
        }
        self.assertEqual(consumers, {"implement-optional-export"})
        states = {node["id"]: {"state": "verified"} for node in graph["nodes"]}
        states["resolve-export-format"]["state"] = "needs_human"
        states["implement"]["state"] = "planned"
        states["implement-optional-export"]["state"] = "planned"
        ready = ready_node_ids(graph, {"nodes": states})
        self.assertIn("implement", ready)
        self.assertNotIn("implement-optional-export", ready)


if __name__ == "__main__":
    unittest.main()
