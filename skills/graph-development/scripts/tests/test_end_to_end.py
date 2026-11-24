from __future__ import annotations

import copy
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(TESTS))

from fixtures import mutation_for  # noqa: E402
from graph_core import atomic_write_json, load_json, verify_event_chain  # noqa: E402
from graph_worker import execute_node  # noqa: E402
from graphctl import initialize_run, load_run, resume_nodes  # noqa: E402
from new_graph import build_graph  # noqa: E402
from render_graph import render_markdown  # noqa: E402
from rewrite_graph import apply_mutation, preview_mutation  # noqa: E402
from validate_graph import canonical_checksum, validate_graph  # noqa: E402


class EndToEndTests(unittest.TestCase):
    def test_scaffold_run_rewrite_invalidate_resume_and_finish(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            graph_path = root / "development-graph.json"
            markdown_path = root / "development-graph.md"
            run_dir = root / ".development-graph"
            graph = build_graph(root, "complete-change", "Complete Change", "change", "commit:start")
            for node in graph["nodes"]:
                node["executor"]["kind"] = "deterministic"
                node["execution"].update({
                    "command": [
                        sys.executable,
                        "-c",
                        f"from pathlib import Path; Path('{node['id']}.txt').write_text('verified', encoding='utf-8')",
                    ],
                    "working_directory": ".",
                    "environment": [],
                    "evidence_paths": [f"{node['id']}.txt"],
                    "capture_limit_bytes": 4096,
                    "timeout_seconds": 10,
                })
            self.assertTrue(validate_graph(graph)["valid"])
            atomic_write_json(graph_path, graph)
            markdown_path.write_text(render_markdown(graph), encoding="utf-8")
            self.assertTrue(validate_graph(graph, markdown_path.read_text(encoding="utf-8"))["valid"])

            run = initialize_run(graph_path, run_dir, actor="e2e")
            for node in graph["nodes"]:
                result = execute_node(graph_path, run_dir, node["id"], "e2e-worker", run["run_version"])
                self.assertEqual(result["state"], "verified", result)
                run = load_run(run_dir)
            self.assertEqual(run["status"], "completed")
            self.assertEqual(verify_event_chain(run_dir / "events.jsonl"), [])

            current = load_json(graph_path)
            replacement = copy.deepcopy(next(item for item in current["nodes"] if item["id"] == "verify"))
            replacement["title"] = "Verify Revised Contract"
            mutation = mutation_for(current, {"op": "replace-node", "id": "verify", "value": replacement}, mutation_id="revise-verification")
            before = graph_path.read_bytes()
            preview = preview_mutation(
                mutation,
                graph_path,
                run_dir,
                actor="e2e",
                authority="test:e2e-rewrite",
            )
            self.assertTrue(preview["valid"], preview)
            self.assertEqual(graph_path.read_bytes(), before)
            applied = apply_mutation(
                mutation,
                graph_path,
                run_dir,
                expected_graph_checksum=canonical_checksum(current),
                expected_run_version=run["run_version"],
                actor="e2e",
                authority="test:e2e-rewrite",
                transaction_timestamp=preview["transaction_timestamp"],
                expected_lifecycle_base_digest=preview[
                    "lifecycle_base_digest"
                ],
                expected_lifecycle_plan_digest=preview[
                    "lifecycle_plan_digest"
                ],
            )
            run = load_run(run_dir)
            self.assertEqual(run["status"], "active")
            self.assertEqual(run["nodes"]["verify"]["state"], "invalidated")
            self.assertEqual(run["nodes"]["finish"]["state"], "invalidated")
            self.assertEqual(run["nodes"]["implement"]["state"], "verified")
            self.assertEqual(applied["snapshot"].read_bytes(), before)

            run = resume_nodes(
                graph_path,
                run_dir,
                ["verify", "finish"],
                "e2e",
                run["run_version"],
            )
            for node_id in ("verify", "finish"):
                result = execute_node(graph_path, run_dir, node_id, "e2e-worker", run["run_version"])
                self.assertEqual(result["state"], "verified", result)
                run = load_run(run_dir)
            self.assertEqual(run["status"], "completed")
            updated = load_json(graph_path)
            self.assertEqual(updated["graph_version"], 2)
            self.assertTrue(validate_graph(updated, markdown_path.read_text(encoding="utf-8"))["valid"])
            self.assertEqual(verify_event_chain(run_dir / "events.jsonl"), [])
            self.assertGreaterEqual(len((run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()), 17)


if __name__ == "__main__":
    unittest.main()
