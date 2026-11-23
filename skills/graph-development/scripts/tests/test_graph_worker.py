from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(TESTS))

from fixtures import (
    backlog_node,
    graph_with_gate,
    outcome_contract,
    outcome_projection,
    runtime_graph,
)  # noqa: E402
from graph_core import RuntimeStateError, atomic_write_json  # noqa: E402
from graph_worker import (  # noqa: E402
    _minimal_environment,
    claim_context_node,
    execute_node,
)
from graphctl import initialize_run, status_report  # noqa: E402


class GraphWorkerTests(unittest.TestCase):
    def _setup(self, root: Path, graph: dict) -> tuple[Path, Path, dict]:
        graph_path = root / "development-graph.json"
        run_dir = root / ".development-graph"
        atomic_write_json(graph_path, graph)
        run = initialize_run(graph_path, run_dir, actor="test")
        return graph_path, run_dir, run

    def test_executes_argv_and_records_output_level_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            graph_path, run_dir, run = self._setup(root, runtime_graph(root))
            unrelated = root / "unrelated caller cwd"
            unrelated.mkdir()
            previous = Path.cwd()
            try:
                os.chdir(unrelated)
                result = execute_node(
                    graph_path,
                    run_dir,
                    "inspect-context",
                    "worker-1",
                    run["run_version"],
                )
            finally:
                os.chdir(previous)
            self.assertEqual(result["exit_code"], 0)
            self.assertEqual(result["state"], "verified")
            self.assertEqual((root / "context.txt").read_text(encoding="utf-8"), "context")
            evidence_path = (
                run_dir / "evidence" / "inspect-context" / "attempt-1.json"
            )
            self.assertTrue(evidence_path.exists())
            evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
            self.assertEqual(evidence["cwd"], ".")
            self.assertEqual(
                result["evidence"],
                "evidence/inspect-context/attempt-1.json",
            )

    def test_minimal_environment_uses_runtime_default_path(self) -> None:
        with (
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch.object(os, "defpath", "runtime-default-path"),
        ):
            environment = _minimal_environment([])
        self.assertEqual(environment["PATH"], "runtime-default-path")

    def test_timeout_fails_node_without_duplicate_execution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            graph = runtime_graph(root)
            node = graph["nodes"][0]
            node["execution"]["command"] = [sys.executable, "-c", "import time; time.sleep(2)"]
            node["execution"]["timeout_seconds"] = 1
            graph_path, run_dir, run = self._setup(root, graph)
            result = execute_node(graph_path, run_dir, node["id"], "worker-1", run["run_version"])
            self.assertTrue(result["timed_out"])
            self.assertEqual(result["state"], "failed")

    def test_rejects_shell_string_and_unapproved_destructive_node(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            graph = runtime_graph(root)
            graph["nodes"][0]["execution"]["command"] = "echo unsafe"
            graph_path = root / "development-graph.json"
            atomic_write_json(graph_path, graph)
            with self.assertRaises(ValueError):
                initialize_run(graph_path, root / ".development-graph", actor="test")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            graph = graph_with_gate(root)
            graph_path, run_dir, run = self._setup(root, graph)
            inspect = execute_node(graph_path, run_dir, "inspect-context", "worker", run["run_version"])
            current_version = inspect["run_version"]
            with self.assertRaises(PermissionError):
                execute_node(graph_path, run_dir, "implement-change", "worker", current_version)

    def test_claim_context_node_seals_digest_first_envelope_and_leaves_lease_running(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            graph = runtime_graph(root)
            target = next(
                node for node in graph["nodes"] if node["id"] == "implement-change"
            )
            target["executor"]["kind"] = "agent"
            for field in (
                "command",
                "working_directory",
                "environment",
                "evidence_paths",
                "capture_limit_bytes",
                "timeout_seconds",
            ):
                target["execution"].pop(field, None)
            target["context_control"] = {
                "version": 3,
                "plan_path": "context/implement-change.json",
                "mode": "executable",
                "tokenizer": "deterministic-word-v1",
                "adapter": "fake-deterministic",
                "retention": "digest-only",
            }
            plan_path = root / "context/implement-change.json"
            plan_path.parent.mkdir(parents=True)
            plan_path.write_text(
                __import__("json").dumps(
                    {
                        "artifact_registry": {
                            "artifact:context": {
                                "content": "PRIVATE RUNTIME CONTEXT",
                                "source": "artifacts/context.json",
                                "requirements": ["evidence-exists"],
                                "authorized": True,
                                "fresh": True,
                                "provenance_complete": True,
                            }
                        },
                        "mandatory_extras": [],
                        "optional_candidates": [],
                        "requirement_weights": {},
                        "budget": {
                            "input_limit": 1000,
                            "output_reserve": 100,
                            "safety_margin": 20,
                        },
                    }
                )
                + "\n"
            )
            graph_path, run_dir, run = self._setup(root, graph)
            inspect = execute_node(
                graph_path,
                run_dir,
                "inspect-context",
                "worker",
                run["run_version"],
            )

            planned = claim_context_node(
                graph_path,
                run_dir,
                "implement-change",
                "context-worker",
                inspect["run_version"],
            )

            self.assertEqual(planned["state"], "running")
            self.assertEqual(planned["adapter"], "fake-deterministic")
            record = (
                run_dir
                / "context/envelopes"
                / f"{planned['envelope_digest']}.json"
            )
            self.assertTrue(record.is_file())
            retained = __import__("json").loads(record.read_text())
            self.assertNotIn("PRIVATE RUNTIME CONTEXT", record.read_text())
            checkpoint = retained["execution_context"]["checkpoint_id"]
            self.assertTrue(checkpoint.startswith(f"{run['run_id']}:"))
            self.assertEqual(
                retained["execution_context"]["adapter_identity"],
                "fake/deterministic-capsule/1",
            )
            self.assertEqual(
                sum(
                    retained["budget"][field]
                    for field in (
                        "fixed_tokens",
                        "capability_schema_tokens",
                        "mandatory_tokens",
                        "optional_tokens",
                        "dynamic_tokens",
                    )
                ),
                retained["budget"]["used_context"],
            )
            status = status_report(graph_path, run_dir)
            self.assertEqual(status["state_counts"]["running"], 1)


    def test_v2_worker_executes_projection_and_rejects_dlg_backlog(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            graph = runtime_graph(root)
            projected_ids = [item["id"] for item in graph["nodes"]]
            future = backlog_node()
            future["executor"]["kind"] = "deterministic"
            future["execution"].update(
                {
                    "command": [
                        sys.executable,
                        "-c",
                        "from pathlib import Path; Path('future.txt').write_text('ran')",
                    ],
                    "working_directory": ".",
                    "environment": [],
                    "evidence_paths": [],
                    "capture_limit_bytes": 65536,
                }
            )
            graph["nodes"].insert(0, future)
            graph["entry_nodes"].insert(0, future["id"])
            graph["terminal_nodes"].append(future["id"])
            graph_path = root / "development-graph.json"
            run_dir = root / ".development-graph"
            atomic_write_json(graph_path, graph)
            run = initialize_run(
                graph_path,
                run_dir,
                actor="test",
                outcome_contract=outcome_contract(graph),
                projection=outcome_projection(graph, projected_ids),
            )
            with self.assertRaisesRegex(RuntimeStateError, "unknown node"):
                execute_node(
                    graph_path,
                    run_dir,
                    "future-debt",
                    "worker",
                    run["run_version"],
                )
            self.assertFalse((root / "future.txt").exists())
            result = execute_node(
                graph_path,
                run_dir,
                "inspect-context",
                "worker",
                run["run_version"],
            )
            self.assertEqual(result["state"], "verified")
            self.assertEqual(
                status_report(graph_path, run_dir)["dlg_backlog_nodes"],
                ["future-debt"],
            )


    def test_v2_worker_rejects_contract_tamper_before_execution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            graph = runtime_graph(root)
            graph_path = root / "development-graph.json"
            run_dir = root / ".development-graph"
            atomic_write_json(graph_path, graph)
            run = initialize_run(
                graph_path,
                run_dir,
                actor="test",
                outcome_contract=outcome_contract(graph),
                projection=outcome_projection(graph),
            )
            run["outcome_contract"]["non_goals"] = ["Tampered non-goal."]
            atomic_write_json(run_dir / "run.json", run)
            with self.assertRaisesRegex(RuntimeStateError, "ledger integrity"):
                execute_node(
                    graph_path,
                    run_dir,
                    "inspect-context",
                    "worker",
                    run["run_version"],
                )
            self.assertFalse((root / "context.txt").exists())


if __name__ == "__main__":
    unittest.main()
