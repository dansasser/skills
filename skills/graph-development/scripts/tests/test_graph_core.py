from __future__ import annotations

import json
import multiprocessing
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(TESTS))

import graph_core  # noqa: E402
from fixtures import outcome_contract, outcome_projection, runtime_graph  # noqa: E402
from graph_core import (  # noqa: E402
    LockTimeout,
    affected_node_ids,
    atomic_write_batch,
    atomic_write_bytes,
    append_event,
    atomic_write_json,
    execution_graph_from_projection,
    file_lock,
    projection_contract_errors,
    ready_node_ids,
    verify_event_chain,
)


def _hold_lock(path: str, ready: multiprocessing.Queue) -> None:
    with file_lock(Path(path), timeout_seconds=1):
        ready.put(True)
        time.sleep(0.5)


class GraphCoreTests(unittest.TestCase):
    def test_atomic_write_json_replaces_complete_document(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            atomic_write_json(path, {"version": 1})
            atomic_write_json(path, {"version": 2, "ok": True})
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"ok": True, "version": 2})
            self.assertEqual(list(path.parent.glob(f".{path.name}.*")), [])

    def test_atomic_write_batch_restores_every_target_after_late_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            existing = root / "existing.txt"
            created = root / "nested" / "created.txt"
            late = root / "late.txt"
            untouched = root / "untouched.txt"
            existing.write_bytes(b"before")
            untouched.write_bytes(b"untouched")
            real_atomic_write = atomic_write_bytes
            calls = 0
            attempted_paths: list[Path] = []

            def fail_third_write(path: Path, payload: bytes, *, mode: int = 0o600) -> None:
                nonlocal calls
                calls += 1
                attempted_paths.append(path)
                if calls == 3:
                    raise OSError("injected late write failure")
                real_atomic_write(path, payload, mode=mode)

            with mock.patch("graph_core.atomic_write_bytes", side_effect=fail_third_write):
                with self.assertRaisesRegex(OSError, "injected late write failure"):
                    atomic_write_batch(
                        [
                            (existing, b"after", 0o600),
                            (created, b"created", 0o600),
                            (late, b"late", 0o600),
                            (untouched, b"changed", 0o600),
                        ]
                    )

            self.assertEqual(existing.read_bytes(), b"before")
            self.assertFalse(created.exists())
            self.assertFalse(late.exists())
            self.assertEqual(untouched.read_bytes(), b"untouched")
            self.assertNotIn(untouched, attempted_paths)
            self.assertFalse((root / "nested").exists())

    def test_file_lock_times_out_under_contention(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime.lock"
            ready: multiprocessing.Queue = multiprocessing.Queue()
            process = multiprocessing.Process(target=_hold_lock, args=(str(path), ready))
            process.start()
            self.assertTrue(ready.get(timeout=2))
            with self.assertRaises(LockTimeout):
                with file_lock(path, timeout_seconds=0.05):
                    pass
            process.join(timeout=2)
            self.assertEqual(process.exitcode, 0)

    def test_event_chain_detects_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            first = append_event(path, {"action": "run-initialized", "actor": "test"})
            second = append_event(path, {"action": "node-claimed", "actor": "worker", "node_id": "inspect-context"})
            self.assertEqual(second["previous_hash"], first["event_hash"])
            self.assertEqual(verify_event_chain(path), [])
            lines = path.read_text(encoding="utf-8").splitlines()
            lines[0] = lines[0].replace("run-initialized", "tampered")
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            self.assertTrue(any("hash" in error for error in verify_event_chain(path)))

    def test_ready_nodes_and_targeted_descendants_are_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            graph = runtime_graph(Path(directory))
            run = {"nodes": {item["id"]: {"state": "planned"} for item in graph["nodes"]}}
            self.assertEqual(ready_node_ids(graph, run), ["inspect-context"])
            self.assertEqual(
                affected_node_ids(graph, {"implement-change"}),
                ["implement-change", "run-tests", "finish-change"],
            )


    def test_projection_contract_proves_exact_durable_subset(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            graph = runtime_graph(Path(directory))
            contract = outcome_contract(graph)
            projection = outcome_projection(
                graph,
                ["inspect-context", "implement-change", "run-tests", "finish-change"],
            )
            self.assertEqual(
                projection_contract_errors(graph, contract, projection),
                [],
            )
            execution = execution_graph_from_projection(graph, projection)
            self.assertEqual(
                [item["id"] for item in execution["nodes"]],
                ["inspect-context", "implement-change", "run-tests", "finish-change"],
            )
            tampered = json.loads(json.dumps(projection))
            tampered["nodes"][0]["title"] = "Changed after projection"
            errors = projection_contract_errors(graph, contract, tampered)
            self.assertTrue(
                any("differs from durable graph definition" in error for error in errors),
                errors,
            )


    def test_projection_validation_reports_malformed_values_without_crashing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            graph = runtime_graph(Path(directory))
            contract = outcome_contract(graph)
            contract["contract_id"] = "Not A Slug"
            errors = projection_contract_errors(
                graph,
                contract,
                outcome_projection(graph),
            )
            self.assertTrue(any("lowercase slug" in error for error in errors), errors)

            contract = outcome_contract(graph)
            malformed = outcome_projection(graph)
            malformed["edges"] = [None]
            malformed["terminal_nodes"] = 42
            errors = projection_contract_errors(graph, contract, malformed)
            self.assertTrue(any("edge must have a string ID" in error for error in errors), errors)
            self.assertTrue(any("terminal_nodes must be an array" in error for error in errors), errors)


    def test_projection_contract_requires_valid_impact_policy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            graph = runtime_graph(Path(directory))
            contract = outcome_contract(graph)
            contract["impact_policy"] = {
                "max_verified_node_count": 3,
                "max_verified_node_fraction": 0.75,
            }
            projection = outcome_projection(graph)
            self.assertEqual(
                projection_contract_errors(graph, contract, projection),
                [],
            )

            missing = json.loads(json.dumps(contract))
            missing.pop("impact_policy")
            self.assertTrue(
                any(
                    "missing field: impact_policy" in error
                    for error in projection_contract_errors(graph, missing, projection)
                )
            )

            invalid_cases = [
                (
                    {
                        "max_verified_node_count": True,
                        "max_verified_node_fraction": 0.75,
                    },
                    "max_verified_node_count",
                ),
                (
                    {
                        "max_verified_node_count": 3,
                        "max_verified_node_fraction": 1.01,
                    },
                    "max_verified_node_fraction",
                ),
                (
                    {
                        "max_verified_node_count": 3,
                        "max_verified_node_fraction": 0.75,
                        "global_default": 99,
                    },
                    "fields are invalid",
                ),
            ]
            for policy, expected in invalid_cases:
                with self.subTest(policy=policy):
                    invalid = json.loads(json.dumps(contract))
                    invalid["impact_policy"] = policy
                    errors = projection_contract_errors(graph, invalid, projection)
                    self.assertTrue(any(expected in error for error in errors), errors)

    def test_semantic_impact_uses_explicit_enrolled_dependencies_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            graph = runtime_graph(Path(directory))
            projection = outcome_projection(graph)
            projection["semantic_dependencies"] = [
                {
                    "id": "context-semantically-affects-tests",
                    "from": "inspect-context",
                    "to": "run-tests",
                    "rationale": "The tests consume interpreted context evidence.",
                },
                {
                    "id": "tests-semantically-affect-finish",
                    "from": "run-tests",
                    "to": "finish-change",
                    "rationale": "The handoff accepts the verified test evidence.",
                },
            ]

            affected = graph_core.semantic_affected_node_ids(
                projection,
                {"inspect-context", "outside-projection"},
            )

            self.assertEqual(
                affected,
                ["inspect-context", "run-tests", "finish-change"],
            )
            self.assertNotIn(
                "implement-change",
                affected,
                "the scheduling edge must not become semantic invalidation",
            )


if __name__ == "__main__":
    unittest.main()
