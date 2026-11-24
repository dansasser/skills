from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(TESTS))

import graphctl  # noqa: E402
from fixtures import (  # noqa: E402
    backlog_node,
    graph_with_gate,
    outcome_contract,
    outcome_projection,
    runtime_graph,
)
from graph_core import (  # noqa: E402
    RuntimeStateError,
    StaleStateError,
    atomic_write_json,
    outcome_contract_digest,
    sha256_json,
)
from graphctl import (  # noqa: E402
    apply_defer,
    apply_promotion,
    approve_node,
    build_parser,
    claim_node,
    complete_node,
    fail_node,
    follow_feedback,
    initialize_run,
    invalidate_nodes,
    load_run,
    preview_defer,
    preview_outcome_migration,
    preview_promotion,
    resume_nodes,
    status_report,
    verify_runtime_integrity,
)
from validate_graph import canonical_checksum  # noqa: E402


class GraphControlTests(unittest.TestCase):
    def test_claim_excludes_conflicting_nodes_until_recovered_or_completed(self) -> None:
        for projected in (False, True):
            for reverse in (False, True):
                with self.subTest(projected=projected, reverse=reverse), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    graph = runtime_graph(root)
                    parallel = copy.deepcopy(graph["nodes"][1])
                    parallel["id"] = "parallel-work"
                    parallel["outputs"][0]["artifact"] = "artifact:parallel"
                    graph["nodes"].append(parallel)
                    dependency = copy.deepcopy(graph["edges"][0])
                    dependency.update(id="context-to-parallel", to="parallel-work")
                    graph["edges"].append(dependency)
                    independent = copy.deepcopy(parallel)
                    independent["id"] = "independent-work"
                    independent["outputs"][0]["artifact"] = "artifact:independent"
                    independent["permissions"]["write"] = ["other/"]
                    independent["resources"] = ["other/"]
                    graph["nodes"].append(independent)
                    dependency = copy.deepcopy(dependency)
                    dependency.update(id="context-to-independent", to="independent-work")
                    graph["edges"].append(dependency)
                    first, second = ("parallel-work", "implement-change") if reverse else ("implement-change", "parallel-work")
                    graph["edges"].append({
                        "id": "exclusive-workspace", "from": "implement-change",
                        "to": "parallel-work", "type": "conflicts",
                        "condition": "Exclusive workspace", "artifacts": [],
                        "max_traversals": None, "exit_condition": None,
                    })
                    graph_path = self._write_graph(root, graph)
                    run_dir = root / ".development-graph"
                    options = {"outcome_contract": outcome_contract(graph), "projection": outcome_projection(graph)} if projected else {}
                    run = initialize_run(graph_path, run_dir, "test", **options)
                    run = claim_node(graph_path, run_dir, "inspect-context", "worker", run["run_version"])
                    run = complete_node(graph_path, run_dir, "inspect-context", "worker", run["run_version"], {"status": "verified"})
                    run = claim_node(graph_path, run_dir, first, "worker-a", run["run_version"])
                    run = claim_node(graph_path, run_dir, "independent-work", "worker-c", run["run_version"])
                    self.assertEqual(run["nodes"][first]["state"], "running")
                    self.assertEqual(run["nodes"]["independent-work"]["state"], "running")
                    before = (run_dir / "run.json").read_bytes(), (run_dir / "events.jsonl").read_bytes()
                    with self.assertRaisesRegex(RuntimeStateError, "conflict"):
                        claim_node(graph_path, run_dir, second, "worker-b", run["run_version"])
                    self.assertEqual(before, ((run_dir / "run.json").read_bytes(), (run_dir / "events.jsonl").read_bytes()))
                    # An expired lease still requires explicit recovery; it does not fence its worker.
                    with mock.patch("graphctl.datetime") as clock:
                        clock.now.return_value = datetime.now(timezone.utc) + timedelta(days=1)
                        with self.assertRaisesRegex(RuntimeStateError, "conflict"):
                            claim_node(graph_path, run_dir, second, "worker-b", run["run_version"])
                    run = complete_node(graph_path, run_dir, first, "worker-a", run["run_version"], {"status": "verified"})
                    run = claim_node(graph_path, run_dir, second, "worker-b", run["run_version"])
                    self.assertEqual(run["nodes"][second]["state"], "running")
                    self.assertEqual(verify_runtime_integrity(graph_path, run_dir), [])

    def _write_graph(self, root: Path, graph: dict) -> Path:
        path = root / "development-graph.json"
        atomic_write_json(path, graph)
        return path

    def _initialize_projected_with_backlog(
        self,
        root: Path,
    ) -> tuple[Path, Path, dict, dict]:
        graph = runtime_graph(root)
        original_node_ids = [item["id"] for item in graph["nodes"]]
        future = backlog_node()
        graph["nodes"].append(future)
        graph["entry_nodes"].append(future["id"])
        graph["terminal_nodes"].append(future["id"])
        graph_path = self._write_graph(root, graph)
        run_dir = root / ".development-graph"
        try:
            run = initialize_run(
                graph_path,
                run_dir,
                actor="test",
                outcome_contract=outcome_contract(graph),
                projection=outcome_projection(graph, original_node_ids),
            )
        except TypeError as exc:
            self.fail(
                "OLD BEHAVIOR: initialize_run enrolls the whole DLG because it has "
                f"no schema-v2 projection contract: {exc}"
            )
        return graph_path, run_dir, graph, run

    def _mapped_promotion_case(
        self,
        root: Path,
        *,
        semantic_dependencies: list[dict] | None = None,
    ) -> tuple[Path, Path, dict, dict, dict]:
        graph = runtime_graph(root)
        original_node_ids = [item["id"] for item in graph["nodes"]]
        graph["nodes"].append(backlog_node("mapped-gap"))
        graph["entry_nodes"].append("mapped-gap")
        graph["edges"].append(
            {
                "id": "mapped-gap-before-implementation",
                "from": "mapped-gap",
                "to": "implement-change",
                "type": "requires",
                "condition": "mapped requirement decomposition is complete",
                "artifacts": [],
                "max_traversals": None,
                "exit_condition": None,
            }
        )
        graph_path = self._write_graph(root, graph)
        run_dir = root / ".development-graph"
        projection = outcome_projection(graph, original_node_ids)
        projection["semantic_dependencies"] = copy.deepcopy(
            semantic_dependencies or []
        )
        run = initialize_run(
            graph_path,
            run_dir,
            actor="test",
            outcome_contract=outcome_contract(graph),
            projection=projection,
        )
        requirement_id = run["outcome_contract"]["requirement_ids"][0]
        request = {
            "schema_version": 1,
            "promotion_id": "mapped-gap-promotion",
            "causal_episode_id": "mapped-gap-episode",
            "target_run_id": run["run_id"],
            "expected_run_version": run["run_version"],
            "expected_outcome_contract_digest": outcome_contract_digest(
                run["outcome_contract"]
            ),
            "expected_graph_version": graph["graph_version"],
            "expected_graph_checksum": canonical_checksum(graph),
            "expected_projection_checksum": run["projection"]["checksum"],
            "classification": "existing-requirement",
            "node_ids": ["mapped-gap"],
            "edge_ids": ["mapped-gap-before-implementation"],
            "requirement_mappings": {requirement_id: ["mapped-gap"]},
            "acceptance_mappings": {},
            "semantic_invalidation": {"nodes": [], "artifacts": []},
            "fanout_policy_result": {
                "allowed": True,
                "basis": "zero semantic invalidation",
            },
            "impact_gate": None,
            "episode_gate": None,
            "actor": "test",
            "authority": "test:mapped-requirement",
            "authority_evidence": ["test:owner-approved-mapped-gap"],
            "rationale": "Required decomposition of the approved requirement.",
            "evidence": ["test:mapped-gap-evidence"],
        }
        return graph_path, run_dir, graph, run, request

    def _migration_request(
        self,
        graph: dict,
        run: dict,
        projection: dict,
    ) -> dict:
        proposed_contract = copy.deepcopy(run["outcome_contract"])
        proposed_contract["contract_version"] += 1
        proposed_projection = copy.deepcopy(projection)
        proposed_projection["contract_version"] = proposed_contract[
            "contract_version"
        ]
        proposed_projection["projection_version"] += 1
        proposed_projection["lineage"] = {
            "parent_path": run["projection"]["path"],
            "parent_checksum": run["projection"]["checksum"],
            "parent_version": projection["projection_version"],
            "promotion_id": "outcome-migration-preview",
            "added_node_ids": [],
            "added_edge_ids": [],
        }
        proposed_projection["provenance"] = {
            "actor": "test",
            "authority": "test:outcome-migration",
        }
        return {
            "schema_version": 1,
            "migration_id": "outcome-migration-preview",
            "target_run_id": run["run_id"],
            "expected_run_version": run["run_version"],
            "expected_outcome_contract_digest": outcome_contract_digest(
                run["outcome_contract"]
            ),
            "expected_graph_version": graph["graph_version"],
            "expected_graph_checksum": canonical_checksum(graph),
            "expected_projection_checksum": run["projection"]["checksum"],
            "mode": "same-run",
            "proposed_outcome_contract": proposed_contract,
            "proposed_projection": proposed_projection,
            "actor": "test",
            "authority": "test:outcome-migration",
            "authority_evidence": ["test:explicit-owner-authority"],
            "rationale": "Preview a new contract version without applying it.",
            "evidence": ["test:migration-preview-evidence"],
        }

    def _semantic_invalidation_case(
        self,
        root: Path,
        *,
        reaches_terminal: bool = False,
        cross_requirement: bool = False,
        impact_policy: dict | None = None,
    ) -> tuple[Path, Path, dict, dict, dict]:
        graph = runtime_graph(root)
        reviewer = backlog_node("review-context")
        reviewer["inputs"] = [
            {
                "name": "context",
                "artifact": "artifact:context",
                "required": True,
            }
        ]
        reviewer["outputs"] = [
            {
                "name": "context review",
                "artifact": "artifact:context-review",
                "format": "markdown",
            }
        ]
        graph["nodes"].append(reviewer)
        graph["edges"].append(
            {
                "id": "context-to-review",
                "from": "inspect-context",
                "to": "review-context",
                "type": "consumes",
                "condition": "context evidence exists",
                "artifacts": ["artifact:context"],
                "max_traversals": None,
                "exit_condition": None,
            }
        )
        outside = backlog_node("outside-finding")
        graph["nodes"].append(outside)
        graph["entry_nodes"].append(outside["id"])
        graph["terminal_nodes"].append(outside["id"])
        enrolled = [
            item["id"]
            for item in graph["nodes"]
            if item["id"] != outside["id"]
        ]
        policy = impact_policy or {
            "max_verified_node_count": 3,
            "max_verified_node_fraction": 0.75,
        }
        contract = outcome_contract(graph, impact_policy=policy)
        projection = outcome_projection(graph, enrolled)
        if cross_requirement:
            contract["requirement_ids"].append("requirement:context-review")
            projection["requirement_mappings"] = {
                "requirement:requested-change": ["implement-change"],
                "requirement:context-review": ["review-context"],
            }
        else:
            projection["requirement_mappings"] = {
                "requirement:requested-change": ["finish-change"]
            }
        projection["semantic_dependencies"] = [
            {
                "id": "implementation-semantically-affects-tests",
                "from": "implement-change",
                "to": "run-tests",
                "rationale": "Tests verify the implementation evidence.",
            }
        ]
        if reaches_terminal:
            projection["semantic_dependencies"].append(
                {
                    "id": "tests-semantically-affect-finish",
                    "from": "run-tests",
                    "to": "finish-change",
                    "rationale": "The handoff accepts the test evidence.",
                }
            )
        graph_path = self._write_graph(root, graph)
        run_dir = root / ".development-graph"
        run = initialize_run(
            graph_path,
            run_dir,
            actor="test",
            outcome_contract=contract,
            projection=projection,
        )
        context_digest = sha256_json("context-v1")
        evidence_by_node = {
            "inspect-context": {"observed": "context"},
            "implement-change": {
                "consumed_artifacts": [
                    {
                        "artifact_id": "artifact:context",
                        "artifact_digest": context_digest,
                    }
                ]
            },
            "review-context": {
                "consumed_artifacts": [
                    {
                        "artifact_id": "artifact:context",
                        "artifact_digest": context_digest,
                    }
                ]
            },
            "run-tests": {
                "consumed_artifacts": [
                    {
                        "artifact_id": "artifact:patch",
                        "artifact_digest": sha256_json("patch-v1"),
                    }
                ]
            },
            "finish-change": {
                "consumed_artifacts": [
                    {
                        "artifact_id": "artifact:test-results",
                        "artifact_digest": sha256_json("tests-v1"),
                    }
                ]
            },
        }
        for node_id in (
            "inspect-context",
            "implement-change",
            "review-context",
            "run-tests",
            "finish-change",
        ):
            run = claim_node(
                graph_path,
                run_dir,
                node_id,
                "semantic-worker",
                run["run_version"],
                60,
            )
            run = complete_node(
                graph_path,
                run_dir,
                node_id,
                "semantic-worker",
                run["run_version"],
                evidence_by_node[node_id],
            )
        request = {
            "schema_version": 1,
            "invalidation_id": "context-artifact-changed",
            "causal_episode_id": "context-regression",
            "target_run_id": run["run_id"],
            "expected_run_version": run["run_version"],
            "expected_outcome_contract_digest": outcome_contract_digest(
                run["outcome_contract"]
            ),
            "expected_graph_version": graph["graph_version"],
            "expected_graph_checksum": canonical_checksum(graph),
            "expected_projection_checksum": run["projection"]["checksum"],
            "node_ids": ["outside-finding"],
            "artifact_changes": [
                {
                    "artifact_id": "artifact:context",
                    "previous_digest": context_digest,
                    "current_digest": sha256_json("context-v2"),
                }
            ],
            "reason": "The accepted context artifact changed.",
            "actor": "test",
            "authority": "test:current-regression",
            "authority_evidence": ["test:artifact-change"],
            "impact_gate": None,
            "episode_gate": None,
        }
        return graph_path, run_dir, graph, run, request

    def _impact_gate(self, run: dict, impact_digest: str) -> dict:
        return {
            "decision": "approved",
            "run_id": run["run_id"],
            "impact_digest": impact_digest,
            "approver": "operator",
            "gate_node_id": "semantic-impact-gate",
            "evidence_digest": sha256_json("approved semantic impact"),
            "approved_at": "2026-08-11T00:00:00Z",
        }


    def _episode_gate(
        self,
        run: dict,
        episode_id: str,
        operation_digest: str,
        decision: str,
    ) -> dict:
        return {
            "decision": decision,
            "run_id": run["run_id"],
            "causal_episode_id": episode_id,
            "episode_operation_digest": operation_digest,
            "approver": "operator",
            "evidence_digest": sha256_json(f"episode decision:{decision}"),
            "approved_at": "2026-08-11T00:00:00Z",
        }

    def test_v2_projection_is_valid_strict_subset_of_dlg(self) -> None:
        """Old integrity rules reject a run projection that is a strict DLG subset."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            graph_path, run_dir, graph, run = self._initialize_projected_with_backlog(root)
            projected_ids = {
                "inspect-context",
                "implement-change",
                "run-tests",
                "finish-change",
            }
            self.assertEqual(set(run["nodes"]), projected_ids)
            self.assertEqual(verify_runtime_integrity(graph_path, run_dir), [])
            report = status_report(graph_path, run_dir)
            self.assertNotIn("future-debt", report["ready_nodes"])
            backlog = report.get("dlg_backlog_nodes", report.get("backlog_nodes"))
            self.assertEqual(backlog, ["future-debt"])
            with self.assertRaises(RuntimeStateError):
                claim_node(
                    graph_path,
                    run_dir,
                    "future-debt",
                    "worker",
                    run["run_version"],
                    60,
                )

    def test_v2_completion_is_pinned_while_dlg_backlog_is_unresolved(self) -> None:
        """Old completion reads global terminals and lets unresolved backlog block outcome."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            graph_path, run_dir, graph, run = self._initialize_projected_with_backlog(root)
            for node_id in (
                "inspect-context",
                "implement-change",
                "run-tests",
                "finish-change",
            ):
                run = claim_node(
                    graph_path,
                    run_dir,
                    node_id,
                    "worker",
                    run["run_version"],
                    60,
                )
                run = complete_node(
                    graph_path,
                    run_dir,
                    node_id,
                    "worker",
                    run["run_version"],
                    {"accepted": True},
                )
            self.assertEqual(run["status"], "completed")
            self.assertNotIn("future-debt", run["nodes"])
            report = status_report(graph_path, run_dir)
            backlog = report.get("dlg_backlog_nodes", report.get("backlog_nodes"))
            self.assertEqual(backlog, ["future-debt"])
            self.assertEqual(report.get("outcome_status", report["status"]), "completed")

    def test_initialize_claim_complete_advances_dependency(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            graph_path = self._write_graph(root, runtime_graph(root))
            run_dir = root / ".development-graph"
            run = initialize_run(graph_path, run_dir, actor="test")
            self.assertEqual(run["nodes"]["inspect-context"]["state"], "ready")
            run = claim_node(graph_path, run_dir, "inspect-context", "worker-1", run["run_version"], 60)
            self.assertEqual(run["nodes"]["inspect-context"]["state"], "running")
            with self.assertRaises(StaleStateError):
                claim_node(graph_path, run_dir, "inspect-context", "worker-2", 1, 60)
            run = complete_node(graph_path, run_dir, "inspect-context", "worker-1", run["run_version"], {"ok": True})
            self.assertEqual(run["nodes"]["inspect-context"]["state"], "verified")
            self.assertEqual(run["nodes"]["implement-change"]["state"], "ready")

    def test_approval_is_bound_to_graph_and_unlocks_destructive_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            graph = graph_with_gate(root)
            graph_path = self._write_graph(root, graph)
            run_dir = root / ".development-graph"
            run = initialize_run(graph_path, run_dir, actor="test")
            self.assertEqual(run["nodes"]["approve-change"]["state"], "needs_human")
            run = approve_node(graph_path, run_dir, "approve-change", "operator", run["run_version"], "evidence:reviewed")
            approval = run["nodes"]["implement-change"]["approval"]
            self.assertEqual(approval["graph_checksum"], run["graph_checksum"])
            self.assertEqual(approval["approver"], "operator")

    def test_invalidate_and_resume_touch_only_affected_branch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            graph_path = self._write_graph(root, runtime_graph(root))
            run_dir = root / ".development-graph"
            run = initialize_run(graph_path, run_dir, actor="test")
            for node_id in run["nodes"]:
                run["nodes"][node_id]["state"] = "verified"
            atomic_write_json(run_dir / "run.json", run)
            run = invalidate_nodes(graph_path, run_dir, ["implement-change"], [], "contract changed", run["run_version"])
            self.assertEqual(run["nodes"]["inspect-context"]["state"], "verified")
            self.assertEqual(run["nodes"]["implement-change"]["state"], "invalidated")
            self.assertEqual(run["nodes"]["finish-change"]["state"], "invalidated")
            run = resume_nodes(graph_path, run_dir, ["implement-change"], "test", run["run_version"])
            self.assertEqual(run["nodes"]["implement-change"]["state"], "ready")

    def test_run_file_is_json_and_ledger_exists(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            graph_path = self._write_graph(root, runtime_graph(root))
            run_dir = root / ".development-graph"
            initialize_run(graph_path, run_dir, actor="test")
            self.assertEqual(load_run(run_dir)["schema_version"], 1)
            event = json.loads(
                (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()[0]
            )
            self.assertEqual(event["definition_path"], "development-graph.json")

    def test_feedback_traversal_is_bounded_and_reopens_only_the_rework_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            graph_path = self._write_graph(root, runtime_graph(root))
            run_dir = root / ".development-graph"
            run = initialize_run(graph_path, run_dir, actor="test")
            for node_id in ("inspect-context", "implement-change"):
                run = claim_node(graph_path, run_dir, node_id, "worker", run["run_version"], 60)
                run = complete_node(graph_path, run_dir, node_id, "worker", run["run_version"], {"ok": True})
            run = claim_node(graph_path, run_dir, "run-tests", "worker", run["run_version"], 60)
            run = fail_node(graph_path, run_dir, "run-tests", "worker", run["run_version"], {"ok": False})
            run = follow_feedback(
                graph_path,
                run_dir,
                "tests-feedback-to-implementation",
                "test",
                run["run_version"],
            )
            self.assertEqual(run["feedback_traversals"]["tests-feedback-to-implementation"], 1)
            self.assertEqual(run["nodes"]["implement-change"]["state"], "ready")
            self.assertEqual(run["nodes"]["run-tests"]["state"], "blocked")
            self.assertEqual(run["nodes"]["finish-change"]["state"], "blocked")

    def test_runtime_integrity_detects_run_pointer_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            graph_path = self._write_graph(root, runtime_graph(root))
            run_dir = root / ".development-graph"
            run = initialize_run(graph_path, run_dir, actor="test")
            self.assertEqual(verify_runtime_integrity(graph_path, run_dir), [])
            run["last_event_hash"] = "0" * 64
            atomic_write_json(run_dir / "run.json", run)
            errors = verify_runtime_integrity(graph_path, run_dir)
            self.assertTrue(any("last event hash" in error for error in errors), errors)

    def test_completed_idempotency_key_cannot_be_reused_after_invalidation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            graph_path = self._write_graph(root, runtime_graph(root))
            run_dir = root / ".development-graph"
            run = initialize_run(graph_path, run_dir, actor="test")
            run = claim_node(
                graph_path,
                run_dir,
                "inspect-context",
                "worker",
                run["run_version"],
                60,
                idempotency_key="fixed-side-effect-key",
            )
            run = complete_node(graph_path, run_dir, "inspect-context", "worker", run["run_version"], {"ok": True})
            run = invalidate_nodes(graph_path, run_dir, ["inspect-context"], [], "input changed", run["run_version"])
            run = resume_nodes(graph_path, run_dir, ["inspect-context"], "test", run["run_version"])
            with self.assertRaises(RuntimeStateError):
                claim_node(
                    graph_path,
                    run_dir,
                    "inspect-context",
                    "worker",
                    run["run_version"],
                    60,
                    idempotency_key="fixed-side-effect-key",
                )

    def test_exhausted_feedback_escalates_and_refuses_another_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            graph = runtime_graph(root)
            graph["edges"][-1]["max_traversals"] = 1
            graph_path = self._write_graph(root, graph)
            run_dir = root / ".development-graph"
            run = initialize_run(graph_path, run_dir, actor="test")
            for node_id in ("inspect-context", "implement-change"):
                run = claim_node(graph_path, run_dir, node_id, "worker", run["run_version"], 60)
                run = complete_node(graph_path, run_dir, node_id, "worker", run["run_version"], {"ok": True})
            run = claim_node(graph_path, run_dir, "run-tests", "worker", run["run_version"], 60)
            run = fail_node(graph_path, run_dir, "run-tests", "worker", run["run_version"], {"ok": False})
            run = follow_feedback(graph_path, run_dir, "tests-feedback-to-implementation", "test", run["run_version"])
            for node_id in ("implement-change", "run-tests"):
                run = claim_node(graph_path, run_dir, node_id, "worker", run["run_version"], 60)
                if node_id == "implement-change":
                    run = complete_node(graph_path, run_dir, node_id, "worker", run["run_version"], {"ok": True})
                else:
                    run = fail_node(graph_path, run_dir, node_id, "worker", run["run_version"], {"ok": False})
            self.assertEqual(run["nodes"]["run-tests"]["state"], "needs_human")
            with self.assertRaises(RuntimeStateError):
                follow_feedback(graph_path, run_dir, "tests-feedback-to-implementation", "test", run["run_version"])

    def test_expired_lease_requires_explicit_resume(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            graph_path = self._write_graph(root, runtime_graph(root))
            run_dir = root / ".development-graph"
            run = initialize_run(graph_path, run_dir, actor="test")
            run = claim_node(graph_path, run_dir, "inspect-context", "worker", run["run_version"], 60)
            run["nodes"]["inspect-context"]["lease"]["expires_at"] = "2000-01-01T00:00:00Z"
            atomic_write_json(run_dir / "run.json", run)
            run = resume_nodes(graph_path, run_dir, ["inspect-context"], "operator", run["run_version"])
            self.assertEqual(run["nodes"]["inspect-context"]["state"], "ready")

    def _claimed_resume_case(self, root: Path, schema_version: int, *, peer: bool = False) -> tuple:
        graph = runtime_graph(root)
        graph["nodes"][0]["execution"]["max_attempts"] = 1
        if peer:
            graph["nodes"].append(backlog_node("peer-context"))
            graph["entry_nodes"].append("peer-context")
            graph["terminal_nodes"].append("peer-context")
        graph_path = self._write_graph(root, graph)
        run_dir = root / ".development-graph"
        inputs = {}
        if schema_version == 2:
            inputs = {"outcome_contract": outcome_contract(graph), "projection": outcome_projection(graph)}
        run = initialize_run(graph_path, run_dir, actor="test", **inputs)
        claimed_at = datetime.now(timezone.utc)
        with mock.patch.object(graphctl, "datetime", wraps=datetime) as clock:
            clock.now.return_value = claimed_at
            run = claim_node(graph_path, run_dir, "inspect-context", "worker", run["run_version"], 1)
            if peer:
                run = claim_node(graph_path, run_dir, "peer-context", "peer-worker", run["run_version"], 60)
        return graph_path, run_dir, run, claimed_at

    def test_expired_final_lease_commits_human_escalation(self) -> None:
        for schema_version in (1, 2):
            with self.subTest(schema_version=schema_version), tempfile.TemporaryDirectory() as directory:
                graph_path, run_dir, claimed, claimed_at = self._claimed_resume_case(Path(directory), schema_version)
                with mock.patch.object(graphctl, "datetime", wraps=datetime) as clock:
                    clock.now.return_value = claimed_at + timedelta(seconds=2)
                    run = resume_nodes(graph_path, run_dir, ["inspect-context"], "operator", claimed["run_version"])
                state = run["nodes"]["inspect-context"]
                self.assertEqual(state["state"], "needs_human")
                self.assertIsNone(state["lease"])
                self.assertEqual(state["attempts"], 1)
                self.assertEqual(run["run_version"], claimed["run_version"] + 1)
                self.assertEqual(load_run(run_dir), run)
                event = json.loads((run_dir / "events.jsonl").read_text().splitlines()[-1])
                self.assertEqual(event["action"], "nodes-resumed")
                self.assertEqual(event["details"]["resumed_nodes"], [])
                self.assertEqual(event["details"]["escalated_nodes"], [{
                    "node_id": "inspect-context", "reason": "attempts-exhausted",
                    "attempts": 1, "max_attempts": 1,
                }])
                self.assertEqual(verify_runtime_integrity(graph_path, run_dir), [])
                with self.assertRaises(RuntimeStateError):
                    claim_node(graph_path, run_dir, "inspect-context", "replacement", run["run_version"], 60)
                with self.assertRaises(RuntimeStateError):
                    resume_nodes(graph_path, run_dir, ["inspect-context"], "operator", run["run_version"])
                self.assertEqual(load_run(run_dir), run)

    def test_resume_exhaustion_preserves_batch_atomicity_for_invalid_or_live_nodes(self) -> None:
        for schema_version in (1, 2):
            for invalid in ("unknown-node", "peer-context"):
                with self.subTest(schema_version=schema_version, invalid=invalid), tempfile.TemporaryDirectory() as directory:
                    graph_path, run_dir, claimed, claimed_at = self._claimed_resume_case(Path(directory), schema_version, peer=True)
                    before = {p.relative_to(run_dir): p.read_bytes() for p in run_dir.rglob("*") if p.is_file()}
                    with mock.patch.object(graphctl, "datetime", wraps=datetime) as clock:
                        clock.now.return_value = claimed_at + timedelta(seconds=2)
                        with self.assertRaisesRegex(RuntimeStateError, "unknown node" if invalid == "unknown-node" else "live lease"):
                            resume_nodes(graph_path, run_dir, ["inspect-context", invalid], "operator", claimed["run_version"])
                    after = {p.relative_to(run_dir): p.read_bytes() for p in run_dir.rglob("*") if p.is_file()}
                    self.assertEqual(after, before)
                    self.assertEqual(load_run(run_dir), claimed)
                    self.assertEqual(verify_runtime_integrity(graph_path, run_dir), [])

    def test_resume_commits_exhaustion_and_retryable_node_together(self) -> None:
        for schema_version in (1, 2):
            with self.subTest(schema_version=schema_version), tempfile.TemporaryDirectory() as directory:
                graph_path, run_dir, claimed, claimed_at = self._claimed_resume_case(Path(directory), schema_version, peer=True)
                failed = fail_node(graph_path, run_dir, "peer-context", "peer-worker", claimed["run_version"], {"ok": False})
                with mock.patch.object(graphctl, "datetime", wraps=datetime) as clock:
                    clock.now.return_value = claimed_at + timedelta(seconds=2)
                    run = resume_nodes(graph_path, run_dir, ["inspect-context", "peer-context"], "operator", failed["run_version"])
                self.assertEqual(run["nodes"]["inspect-context"]["state"], "needs_human")
                self.assertEqual(run["nodes"]["peer-context"]["state"], "ready")
                self.assertIsNone(run["nodes"]["inspect-context"]["lease"])
                self.assertEqual(run["run_version"], failed["run_version"] + 1)
                event = json.loads((run_dir / "events.jsonl").read_text().splitlines()[-1])
                self.assertEqual(event["details"]["resumed_nodes"], ["peer-context"])
                self.assertEqual(verify_runtime_integrity(graph_path, run_dir), [])

    def test_failed_final_attempt_remains_a_non_resumable_human_gate(self) -> None:
        for schema_version in (1, 2):
            with self.subTest(schema_version=schema_version), tempfile.TemporaryDirectory() as directory:
                graph_path, run_dir, claimed, _ = self._claimed_resume_case(Path(directory), schema_version)
                failed = fail_node(graph_path, run_dir, "inspect-context", "worker", claimed["run_version"], {"ok": False})
                self.assertEqual(failed["nodes"]["inspect-context"]["state"], "needs_human")
                with self.assertRaises(RuntimeStateError):
                    resume_nodes(graph_path, run_dir, ["inspect-context"], "operator", failed["run_version"])
                self.assertEqual(load_run(run_dir), failed)
                self.assertEqual(verify_runtime_integrity(graph_path, run_dir), [])


    def test_v2_init_requires_paired_valid_inputs_before_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            graph = runtime_graph(root)
            graph_path = self._write_graph(root, graph)
            run_dir = root / ".development-graph"
            with self.assertRaisesRegex(ValueError, "supplied together"):
                initialize_run(
                    graph_path,
                    run_dir,
                    actor="test",
                    outcome_contract=outcome_contract(graph),
                )
            self.assertFalse(run_dir.exists())

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            graph = runtime_graph(root)
            graph_path = self._write_graph(root, graph)
            run_dir = root / ".development-graph"
            projection = outcome_projection(graph)
            projection["nodes"][0]["title"] = "Not the durable definition"
            with self.assertRaisesRegex(ValueError, "differs from durable graph definition"):
                initialize_run(
                    graph_path,
                    run_dir,
                    actor="test",
                    outcome_contract=outcome_contract(graph),
                    projection=projection,
                )
            self.assertFalse(run_dir.exists())

    def test_v2_integrity_detects_projection_snapshot_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            graph = runtime_graph(root)
            graph_path = self._write_graph(root, graph)
            run_dir = root / ".development-graph"
            run = initialize_run(
                graph_path,
                run_dir,
                actor="test",
                outcome_contract=outcome_contract(graph),
                projection=outcome_projection(graph),
            )
            self.assertEqual(verify_runtime_integrity(graph_path, run_dir), [])
            projection_path = run_dir / run["projection"]["path"]
            frozen = json.loads(projection_path.read_text(encoding="utf-8"))
            frozen["provenance"]["authority"] = "tampered"
            atomic_write_json(projection_path, frozen)
            errors = verify_runtime_integrity(graph_path, run_dir)
            self.assertTrue(any("checksum" in error for error in errors), errors)

    def test_v2_approval_binds_to_projection_not_whole_dlg(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            graph = graph_with_gate(root)
            graph_path = self._write_graph(root, graph)
            run_dir = root / ".development-graph"
            run = initialize_run(
                graph_path,
                run_dir,
                actor="test",
                outcome_contract=outcome_contract(graph),
                projection=outcome_projection(graph),
            )
            run = approve_node(
                graph_path,
                run_dir,
                "approve-change",
                "operator",
                run["run_version"],
                "evidence:reviewed",
            )
            approval = run["nodes"]["implement-change"]["approval"]
            self.assertEqual(approval["graph_checksum"], run["projection"]["checksum"])
            self.assertNotEqual(
                approval["graph_checksum"],
                run["observed_graph_checksum"],
            )

    def test_v2_invalidation_and_feedback_fail_closed_without_writes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            graph = runtime_graph(root)
            graph_path = self._write_graph(root, graph)
            run_dir = root / ".development-graph"
            run = initialize_run(
                graph_path,
                run_dir,
                actor="test",
                outcome_contract=outcome_contract(graph),
                projection=outcome_projection(graph),
            )
            run_before = (run_dir / "run.json").read_bytes()
            events_before = (run_dir / "events.jsonl").read_bytes()
            with self.assertRaisesRegex(RuntimeStateError, "semantic invalidation"):
                invalidate_nodes(
                    graph_path,
                    run_dir,
                    ["inspect-context"],
                    [],
                    "input changed",
                    run["run_version"],
                )
            with self.assertRaisesRegex(RuntimeStateError, "semantic invalidation"):
                follow_feedback(
                    graph_path,
                    run_dir,
                    "tests-feedback-to-implementation",
                    "test",
                    run["run_version"],
                )
            self.assertEqual((run_dir / "run.json").read_bytes(), run_before)
            self.assertEqual((run_dir / "events.jsonl").read_bytes(), events_before)
            self.assertEqual(
                run["projection"]["checksum"],
                sha256_json(
                    json.loads(
                        (run_dir / run["projection"]["path"]).read_text(
                            encoding="utf-8"
                        )
                    )
                ),
            )


    def test_v2_ledger_pins_complete_contract_and_projection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            graph = runtime_graph(root)
            graph_path = self._write_graph(root, graph)
            run_dir = root / ".development-graph"
            run = initialize_run(
                graph_path,
                run_dir,
                actor="test",
                outcome_contract=outcome_contract(graph),
                projection=outcome_projection(graph),
            )
            frozen_path = run_dir / run["projection"]["path"]
            frozen = json.loads(frozen_path.read_text(encoding="utf-8"))
            frozen["provenance"]["authority"] = "tampered-authority"
            atomic_write_json(frozen_path, frozen)
            run["projection"]["checksum"] = sha256_json(frozen)
            run["outcome_contract"]["authority_evidence"] = ["tampered-authority"]
            run["outcome_contract"]["authority_digest"] = sha256_json(
                run["outcome_contract"]["authority_evidence"]
            )
            atomic_write_json(run_dir / "run.json", run)

            errors = verify_runtime_integrity(graph_path, run_dir)
            self.assertTrue(
                any("outcome_contract_digest" in error for error in errors),
                errors,
            )
            self.assertTrue(
                any("projection_checksum" in error for error in errors),
                errors,
            )
            with self.assertRaisesRegex(RuntimeStateError, "ledger integrity"):
                status_report(graph_path, run_dir)
            with self.assertRaisesRegex(RuntimeStateError, "ledger integrity"):
                claim_node(
                    graph_path,
                    run_dir,
                    "inspect-context",
                    "worker",
                    run["run_version"],
                )

    def test_v2_malformed_binding_reports_controlled_errors(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            graph = runtime_graph(root)
            graph_path = self._write_graph(root, graph)
            run_dir = root / ".development-graph"
            run = initialize_run(
                graph_path,
                run_dir,
                actor="test",
                outcome_contract=outcome_contract(graph),
                projection=outcome_projection(graph),
            )
            run["projection"]["path"] = 42
            atomic_write_json(run_dir / "run.json", run)
            errors = verify_runtime_integrity(graph_path, run_dir)
            self.assertTrue(
                any("projection path must be a non-empty string" in error for error in errors),
                errors,
            )
            with self.assertRaisesRegex(RuntimeStateError, "invalid schema-v2 runtime"):
                status_report(graph_path, run_dir)

    def test_v2_initialization_batch_rolls_back_and_can_retry(self) -> None:
        import graph_core

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            graph = runtime_graph(root)
            graph_path = self._write_graph(root, graph)
            run_dir = root / ".development-graph"
            real_atomic_write = graph_core.atomic_write_bytes
            calls = 0

            def fail_second_write(
                path: Path,
                payload: bytes,
                *,
                mode: int = 0o600,
            ) -> None:
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("injected initialization failure")
                real_atomic_write(path, payload, mode=mode)

            with mock.patch(
                "graph_core.atomic_write_bytes",
                side_effect=fail_second_write,
            ):
                with self.assertRaisesRegex(
                    OSError,
                    "injected initialization failure",
                ):
                    initialize_run(
                        graph_path,
                        run_dir,
                        actor="test",
                        outcome_contract=outcome_contract(graph),
                        projection=outcome_projection(graph),
                    )
            self.assertFalse((run_dir / "run.json").exists())
            self.assertFalse((run_dir / "events.jsonl").exists())
            self.assertEqual(
                list((run_dir / "projections").glob("*.json")),
                [],
            )
            retried = initialize_run(
                graph_path,
                run_dir,
                actor="test",
                outcome_contract=outcome_contract(graph),
                projection=outcome_projection(graph),
            )
            self.assertEqual(retried["schema_version"], 2)
            self.assertEqual(verify_runtime_integrity(graph_path, run_dir), [])


    def test_mapped_existing_requirement_promotion_is_atomic_and_integrity_safe(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            graph_path, run_dir, graph, run, request = (
                self._mapped_promotion_case(root)
            )
            graph_before = graph_path.read_bytes()
            parent_path = run_dir / run["projection"]["path"]
            parent_before = parent_path.read_bytes()
            timestamp = "2026-08-11T00:00:00Z"

            preview = preview_promotion(
                request,
                graph_path,
                run_dir,
                transaction_timestamp=timestamp,
            )

            self.assertTrue(preview["valid"], preview)
            self.assertTrue(preview["apply_allowed"], preview)
            self.assertEqual(preview["route"], "apply-promotion")
            self.assertEqual(preview["candidate_projection_version"], 2)
            result = apply_promotion(
                request,
                graph_path,
                run_dir,
                transaction_timestamp=timestamp,
                expected_lifecycle_base_digest=preview[
                    "lifecycle_base_digest"
                ],
                expected_lifecycle_plan_digest=preview[
                    "lifecycle_plan_digest"
                ],
            )

            updated_run = load_run(run_dir)
            candidate_path = run_dir / updated_run["projection"]["path"]
            candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
            self.assertTrue(result["applied"])
            self.assertEqual(result["projection_version"], 2)
            self.assertEqual(
                set(updated_run["nodes"]) - set(run["nodes"]),
                {"mapped-gap"},
            )
            self.assertEqual(updated_run["nodes"]["mapped-gap"]["state"], "ready")
            self.assertIn(
                "mapped-gap",
                {item["id"] for item in candidate["nodes"]},
            )
            self.assertIn(
                "mapped-gap-before-implementation",
                {item["id"] for item in candidate["edges"]},
            )
            self.assertEqual(parent_path.read_bytes(), parent_before)
            self.assertEqual(graph_path.read_bytes(), graph_before)
            records = [
                json.loads(line)
                for line in (run_dir / "events.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
                if line
            ]
            self.assertEqual(records[-1]["action"], "run-nodes-promoted")
            self.assertEqual(verify_runtime_integrity(graph_path, run_dir), [])


    def test_acceptance_evidence_reopens_a_bounded_repair_episode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            graph_path, run_dir, _, run, request = (
                self._mapped_promotion_case(root)
            )
            acceptance_id = run["outcome_contract"]["acceptance_ids"][0]
            request["requirement_mappings"] = {}
            request["acceptance_mappings"] = {
                acceptance_id: ["mapped-gap"]
            }
            request["episode_gate"] = None
            timestamp = "2026-08-11T05:30:00Z"
            preview = preview_promotion(
                request,
                graph_path,
                run_dir,
                transaction_timestamp=timestamp,
            )
            self.assertTrue(preview["valid"], preview)
            self.assertTrue(preview["apply_allowed"], preview)
            result = apply_promotion(
                request,
                graph_path,
                run_dir,
                transaction_timestamp=timestamp,
                expected_lifecycle_base_digest=preview[
                    "lifecycle_base_digest"
                ],
                expected_lifecycle_plan_digest=preview[
                    "lifecycle_plan_digest"
                ],
            )

            promoted = load_run(run_dir)
            episode_id = request["causal_episode_id"]
            episode = promoted["episodes"][episode_id]
            self.assertEqual(result["causal_episode_id"], episode_id)
            self.assertEqual(episode["status"], "needs_human")
            self.assertEqual(episode["repair_cycles"], 1)
            self.assertEqual(
                status_report(graph_path, run_dir)["needs_human_episodes"],
                [episode_id],
            )

            promoted = claim_node(
                graph_path,
                run_dir,
                "mapped-gap",
                "acceptance-worker",
                promoted["run_version"],
                60,
            )
            completed = complete_node(
                graph_path,
                run_dir,
                "mapped-gap",
                "acceptance-worker",
                promoted["run_version"],
                {"accepted": "mapped-gap"},
            )

            progressed = completed["episodes"][episode_id]
            self.assertEqual(progressed["status"], "active")
            self.assertEqual(progressed["progress_count"], 1)
            self.assertEqual(progressed["no_progress_cycles"], 0)
            self.assertEqual(
                status_report(graph_path, run_dir)["needs_human_episodes"],
                [],
            )
            records = [
                json.loads(line)
                for line in (run_dir / "events.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            updates = records[-1]["details"]["episode_progress"]
            self.assertEqual(
                [item["causal_episode_id"] for item in updates],
                [episode_id],
            )
            self.assertEqual(verify_runtime_integrity(graph_path, run_dir), [])

    def test_current_regression_promotion_atomically_reopens_semantic_consumers(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            graph_path, run_dir, graph, run, request = (
                self._mapped_promotion_case(
                    root,
                    semantic_dependencies=[
                        {
                            "id": "implementation-semantically-affects-tests",
                            "from": "implement-change",
                            "to": "run-tests",
                            "rationale": "Tests verify implementation evidence.",
                        },
                        {
                            "id": "tests-semantically-affect-finish",
                            "from": "run-tests",
                            "to": "finish-change",
                            "rationale": "The handoff accepts test evidence.",
                        },
                    ],
                )
            )
            for node_id in (
                "inspect-context",
                "implement-change",
                "run-tests",
                "finish-change",
            ):
                run = claim_node(
                    graph_path,
                    run_dir,
                    node_id,
                    "regression-worker",
                    run["run_version"],
                    60,
                )
                run = complete_node(
                    graph_path,
                    run_dir,
                    node_id,
                    "regression-worker",
                    run["run_version"],
                    {"verified": node_id},
                )
            self.assertEqual(run["status"], "completed")
            request["expected_run_version"] = run["run_version"]
            request["expected_outcome_contract_digest"] = (
                outcome_contract_digest(run["outcome_contract"])
            )
            request["expected_projection_checksum"] = run["projection"][
                "checksum"
            ]
            request["classification"] = "current-regression"
            request["semantic_invalidation"] = {
                "nodes": ["implement-change"],
                "artifacts": [],
            }
            request["fanout_policy_result"] = {
                "allowed": True,
                "basis": "within the contract fanout; categorical gate required",
            }

            gated = preview_promotion(
                request,
                graph_path,
                run_dir,
                transaction_timestamp="2026-08-11T03:00:00Z",
            )

            self.assertTrue(gated["valid"], gated)
            self.assertFalse(gated["apply_allowed"])
            self.assertEqual(gated["route"], "apply-current-regression")
            self.assertEqual(
                gated["semantic_impact"]["verified_node_ids"],
                ["implement-change", "run-tests", "finish-change"],
            )
            self.assertEqual(
                set(gated["semantic_impact"]["gate_reasons"]),
                {"accepted-requirement", "acceptance", "terminal"},
            )

            approved = copy.deepcopy(request)
            approved["impact_gate"] = self._impact_gate(
                run,
                gated["impact_digest"],
            )
            preview = preview_promotion(
                approved,
                graph_path,
                run_dir,
                transaction_timestamp="2026-08-11T03:00:00Z",
            )
            self.assertTrue(preview["valid"], preview)
            self.assertTrue(preview["apply_allowed"], preview)
            result = apply_promotion(
                approved,
                graph_path,
                run_dir,
                transaction_timestamp=preview["transaction_timestamp"],
                expected_lifecycle_base_digest=preview[
                    "lifecycle_base_digest"
                ],
                expected_lifecycle_plan_digest=preview[
                    "lifecycle_plan_digest"
                ],
            )

            updated = load_run(run_dir)
            self.assertTrue(result["applied"], result)
            self.assertEqual(
                result["invalidated_node_ids"],
                ["implement-change", "run-tests", "finish-change"],
            )
            self.assertEqual(updated["nodes"]["inspect-context"]["state"], "verified")
            self.assertEqual(updated["nodes"]["mapped-gap"]["state"], "ready")
            for node_id in ("implement-change", "run-tests", "finish-change"):
                self.assertEqual(updated["nodes"][node_id]["state"], "invalidated")
            self.assertEqual(updated["status"], "active")
            records = [
                json.loads(line)
                for line in (run_dir / "events.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            self.assertEqual(
                [record["action"] for record in records[-2:]],
                ["run-nodes-promoted", "semantic-nodes-invalidated"],
            )
            self.assertEqual(verify_runtime_integrity(graph_path, run_dir), [])

    def test_promotion_transaction_failure_restores_all_runtime_bytes(
        self,
    ) -> None:
        import project_lifecycle

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            graph_path, run_dir, _, run, request = self._mapped_promotion_case(
                root
            )
            timestamp = "2026-08-11T00:00:00Z"
            preview = preview_promotion(
                request,
                graph_path,
                run_dir,
                transaction_timestamp=timestamp,
            )
            self.assertTrue(preview["valid"], preview)
            before_run = (run_dir / "run.json").read_bytes()
            before_events = (run_dir / "events.jsonl").read_bytes()
            parent_path = run_dir / run["projection"]["path"]
            before_parent = parent_path.read_bytes()
            candidate_path = (
                run_dir / "projections" / f"{run['run_id']}-v2.json"
            )
            real_replace = project_lifecycle._replace_project_artifact
            calls = 0

            def fail_second(
                project_root: Path,
                relative: str,
                payload: bytes,
                mode: int,
            ) -> None:
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("injected promotion write failure")
                real_replace(project_root, relative, payload, mode)

            with mock.patch(
                "project_lifecycle._replace_project_artifact",
                side_effect=fail_second,
            ), self.assertRaisesRegex(
                RuntimeStateError,
                "promotion transaction rolled-back",
            ):
                apply_promotion(
                    request,
                    graph_path,
                    run_dir,
                    transaction_timestamp=timestamp,
                    expected_lifecycle_base_digest=preview[
                        "lifecycle_base_digest"
                    ],
                    expected_lifecycle_plan_digest=preview[
                        "lifecycle_plan_digest"
                    ],
                )

            self.assertEqual((run_dir / "run.json").read_bytes(), before_run)
            self.assertEqual(
                (run_dir / "events.jsonl").read_bytes(),
                before_events,
            )
            self.assertEqual(parent_path.read_bytes(), before_parent)
            self.assertFalse(candidate_path.exists())
            transactions = list((run_dir / "transactions").iterdir())
            self.assertEqual(len(transactions), 1)
            manifest = json.loads(
                (transactions[0] / "manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["state"], "rolled-back")
            self.assertIn("injected promotion write failure", manifest["error"])
            self.assertTrue((transactions[0] / "receipt.json").is_file())

    def test_deferral_records_backlog_without_changing_runtime_inventory(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            graph_path, run_dir, graph, run = (
                self._initialize_projected_with_backlog(root)
            )
            before_nodes = copy.deepcopy(run["nodes"])
            projection_path = run_dir / run["projection"]["path"]
            before_projection = projection_path.read_bytes()
            request = {
                "schema_version": 1,
                "deferral_id": "future-debt-deferral",
                "causal_episode_id": "future-debt-episode",
                "target_run_id": run["run_id"],
                "expected_run_version": run["run_version"],
                "expected_outcome_contract_digest": outcome_contract_digest(
                    run["outcome_contract"]
                ),
                "expected_graph_version": graph["graph_version"],
                "expected_graph_checksum": canonical_checksum(graph),
                "expected_projection_checksum": run["projection"]["checksum"],
                "finding_id": "future-debt",
                "reason": "Outside the current approved outcome.",
                "evidence": ["test:deferral-evidence"],
                "actor": "test",
                "authority": "test:deferral",
            }
            timestamp = "2026-08-11T00:00:00Z"

            preview = preview_defer(
                request,
                graph_path,
                run_dir,
                transaction_timestamp=timestamp,
            )
            self.assertTrue(preview["valid"], preview)
            result = apply_defer(
                request,
                graph_path,
                run_dir,
                transaction_timestamp=timestamp,
                expected_run_sha256=preview["expected_run_sha256"],
                expected_events_sha256=preview["expected_events_sha256"],
            )

            updated_run = load_run(run_dir)
            self.assertTrue(result["applied"])
            self.assertEqual(updated_run["nodes"], before_nodes)
            self.assertEqual(projection_path.read_bytes(), before_projection)
            self.assertEqual(updated_run["run_version"], run["run_version"] + 1)
            self.assertEqual(
                status_report(graph_path, run_dir)["deferred_findings"],
                ["future-debt"],
            )
            records = [
                json.loads(line)
                for line in (run_dir / "events.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
                if line
            ]
            self.assertEqual(records[-1]["action"], "finding-deferred")
            self.assertEqual(verify_runtime_integrity(graph_path, run_dir), [])

    def test_promotion_refuses_uncertain_mapping_and_outcome_change_without_writes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            graph_path, run_dir, _, run, request = self._mapped_promotion_case(
                root
            )
            projection_path = run_dir / run["projection"]["path"]
            before = {
                "graph": graph_path.read_bytes(),
                "run": (run_dir / "run.json").read_bytes(),
                "events": (run_dir / "events.jsonl").read_bytes(),
                "projection": projection_path.read_bytes(),
            }
            uncertain = copy.deepcopy(request)
            uncertain["requirement_mappings"] = {
                "requirement:not-approved": ["mapped-gap"]
            }
            uncertain_report = preview_promotion(
                uncertain,
                graph_path,
                run_dir,
            )
            self.assertFalse(uncertain_report["valid"])
            self.assertTrue(
                any(
                    "outside the outcome contract" in error
                    for error in uncertain_report["errors"]
                ),
                uncertain_report,
            )

            outcome_change = copy.deepcopy(request)
            outcome_change["classification"] = "approved-outcome-change"
            change_report = preview_promotion(
                outcome_change,
                graph_path,
                run_dir,
            )
            self.assertFalse(change_report["valid"])
            self.assertTrue(
                any("use migrate-outcome" in error for error in change_report["errors"]),
                change_report,
            )
            self.assertEqual(graph_path.read_bytes(), before["graph"])
            self.assertEqual((run_dir / "run.json").read_bytes(), before["run"])
            self.assertEqual(
                (run_dir / "events.jsonl").read_bytes(),
                before["events"],
            )
            self.assertEqual(projection_path.read_bytes(), before["projection"])

    def test_authorized_outcome_expansion_creates_successor_and_preserves_source(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            graph_path, run_dir, graph, run = (
                self._initialize_projected_with_backlog(root)
            )
            projection_path = run_dir / run["projection"]["path"]
            projection = json.loads(projection_path.read_text(encoding="utf-8"))
            request = self._migration_request(graph, run, projection)
            successor_run_id = "00000000-0000-4000-8000-000000000021"
            expansion_authority = ["test:explicit-owner-expansion-authority"]
            proposed_contract = request["proposed_outcome_contract"]
            proposed_contract["outcome"] = (
                "Complete the approved requested change and the "
                "owner-authorized expansion."
            )
            proposed_contract["outcome_digest"] = sha256_json(
                proposed_contract["outcome"]
            )
            proposed_contract["requirement_ids"].append(
                "requirement:authorized-expansion"
            )
            proposed_contract["acceptance_ids"].append(
                "acceptance:authorized-expansion"
            )
            proposed_contract["acceptance_digest"] = sha256_json(
                proposed_contract["acceptance_ids"]
            )
            proposed_contract["authority_evidence"] = expansion_authority
            proposed_contract["authority_digest"] = sha256_json(
                expansion_authority
            )
            proposed_contract["created_at"] = "2026-08-16T12:30:00Z"
            proposed_contract["actor"] = "test"

            proposed_projection = request["proposed_projection"]
            proposed_projection["run_id"] = successor_run_id
            proposed_projection.pop("lineage", None)
            proposed_projection["projection_version"] = 1
            proposed_projection["requirement_mappings"][
                "requirement:authorized-expansion"
            ] = ["finish-change"]
            proposed_projection["acceptance_mappings"][
                "acceptance:authorized-expansion"
            ] = ["finish-change"]
            proposed_projection["provenance"] = {
                "actor": "test",
                "authority": "test:authorized-outcome-expansion",
            }

            request.update(
                {
                    "migration_id": "authorized-outcome-expansion",
                    "mode": "successor-run",
                    "successor_run_id": successor_run_id,
                    "authority": "test:authorized-outcome-expansion",
                    "authority_evidence": expansion_authority,
                    "rationale": "Apply the explicitly authorized expansion.",
                    "evidence": ["test:authorized-expansion-evidence"],
                }
            )
            source_before = {
                "run": (run_dir / "run.json").read_bytes(),
                "events": (run_dir / "events.jsonl").read_bytes(),
                "projection": projection_path.read_bytes(),
            }
            transaction_timestamp = "2026-08-16T12:30:00Z"

            preview = preview_outcome_migration(
                request,
                graph_path,
                run_dir,
                transaction_timestamp=transaction_timestamp,
            )

            self.assertTrue(preview["valid"], preview)
            self.assertTrue(preview["apply_supported"], preview)
            self.assertEqual(preview["recommended_mode"], "successor-run")
            self.assertEqual(preview["successor_run_id"], successor_run_id)
            self.assertEqual(
                preview["successor_run_path"],
                f"successors/{successor_run_id}",
            )
            self.assertTrue(preview["lifecycle_base_digest"])
            self.assertTrue(preview["lifecycle_plan_digest"])

            successor_dir = run_dir / "successors" / successor_run_id
            with self.assertRaisesRegex(
                StaleStateError,
                "stale outcome migration lifecycle base digest",
            ):
                graphctl.apply_outcome_migration(
                    request,
                    graph_path,
                    run_dir,
                    transaction_timestamp=transaction_timestamp,
                    expected_lifecycle_base_digest="0" * 64,
                    expected_lifecycle_plan_digest=preview[
                        "lifecycle_plan_digest"
                    ],
                )
            self.assertFalse(successor_dir.exists())
            self.assertEqual((run_dir / "run.json").read_bytes(), source_before["run"])
            self.assertEqual(
                (run_dir / "events.jsonl").read_bytes(),
                source_before["events"],
            )
            self.assertEqual(projection_path.read_bytes(), source_before["projection"])

            with mock.patch.object(
                graphctl,
                "atomic_write_batch",
                side_effect=OSError("injected successor write failure"),
            ):
                with self.assertRaisesRegex(
                    OSError,
                    "injected successor write failure",
                ):
                    graphctl.apply_outcome_migration(
                        request,
                        graph_path,
                        run_dir,
                        transaction_timestamp=transaction_timestamp,
                        expected_lifecycle_base_digest=preview[
                            "lifecycle_base_digest"
                        ],
                        expected_lifecycle_plan_digest=preview[
                            "lifecycle_plan_digest"
                        ],
                    )
            self.assertFalse(successor_dir.exists())
            self.assertEqual((run_dir / "run.json").read_bytes(), source_before["run"])
            self.assertEqual(
                (run_dir / "events.jsonl").read_bytes(),
                source_before["events"],
            )
            self.assertEqual(projection_path.read_bytes(), source_before["projection"])

            applied = graphctl.apply_outcome_migration(
                request,
                graph_path,
                run_dir,
                transaction_timestamp=transaction_timestamp,
                expected_lifecycle_base_digest=preview[
                    "lifecycle_base_digest"
                ],
                expected_lifecycle_plan_digest=preview[
                    "lifecycle_plan_digest"
                ],
            )

            self.assertTrue(applied["applied"], applied)
            self.assertEqual(applied["successor_run_id"], successor_run_id)
            self.assertEqual(
                (run_dir / "run.json").read_bytes(),
                source_before["run"],
            )
            self.assertEqual(
                (run_dir / "events.jsonl").read_bytes(),
                source_before["events"],
            )
            self.assertEqual(projection_path.read_bytes(), source_before["projection"])

            successor_dir = run_dir / "successors" / successor_run_id
            successor = load_run(successor_dir)
            self.assertEqual(successor["run_id"], successor_run_id)
            self.assertEqual(
                successor["outcome_contract"],
                proposed_contract,
            )
            self.assertEqual(
                verify_runtime_integrity(graph_path, successor_dir),
                [],
            )
            successor_projection = json.loads(
                (successor_dir / successor["projection"]["path"]).read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(successor_projection, proposed_projection)
            records = [
                json.loads(line)
                for line in (successor_dir / "events.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
                if line
            ]
            self.assertEqual(
                [record["action"] for record in records],
                ["outcome-migration-applied"],
            )
            self.assertEqual(records[0]["source_run_id"], run["run_id"])
            self.assertEqual(
                records[0]["source_outcome_contract_digest"],
                outcome_contract_digest(run["outcome_contract"]),
            )

            with self.assertRaises(RuntimeStateError):
                graphctl.apply_outcome_migration(
                    request,
                    graph_path,
                    run_dir,
                    transaction_timestamp=transaction_timestamp,
                    expected_lifecycle_base_digest=preview[
                        "lifecycle_base_digest"
                    ],
                    expected_lifecycle_plan_digest=preview[
                        "lifecycle_plan_digest"
                    ],
                )

    def test_outcome_migration_is_preview_only_and_rejects_corrupt_runtime(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            graph_path, run_dir, graph, run = (
                self._initialize_projected_with_backlog(root)
            )
            projection = json.loads(
                (run_dir / run["projection"]["path"]).read_text(encoding="utf-8")
            )
            request = self._migration_request(graph, run, projection)

            report = preview_outcome_migration(request, graph_path, run_dir)

            self.assertTrue(report["valid"], report)
            self.assertTrue(report["dry_run"])
            self.assertFalse(report["apply_supported"])
            self.assertIn("preview-only", report["message"])
            missing_authority = copy.deepcopy(request)
            missing_authority["authority_evidence"] = []
            refused = preview_outcome_migration(
                missing_authority,
                graph_path,
                run_dir,
            )
            self.assertFalse(refused["valid"])

            tampered = load_run(run_dir)
            tampered["projection"]["checksum"] = "0" * 64
            atomic_write_json(run_dir / "run.json", tampered)
            corrupt_request = copy.deepcopy(request)
            corrupt_request["expected_projection_checksum"] = "0" * 64
            corrupt = preview_outcome_migration(
                corrupt_request,
                graph_path,
                run_dir,
            )
            self.assertFalse(corrupt["valid"], corrupt)
            self.assertTrue(
                any(
                    "runtime ledger integrity failed" in error
                    for error in corrupt["errors"]
                ),
                corrupt,
            )
            self.assertFalse(corrupt["apply_supported"])

    def test_v2_complete_validates_sealed_consumed_artifact_evidence(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            graph = runtime_graph(root)
            graph_path = self._write_graph(root, graph)
            run_dir = root / ".development-graph"
            run = initialize_run(
                graph_path,
                run_dir,
                actor="test",
                outcome_contract=outcome_contract(graph),
                projection=outcome_projection(graph),
            )
            run = claim_node(
                graph_path,
                run_dir,
                "inspect-context",
                "worker",
                run["run_version"],
                60,
            )
            run_before = (run_dir / "run.json").read_bytes()
            events_before = (run_dir / "events.jsonl").read_bytes()
            with self.assertRaisesRegex(
                RuntimeStateError,
                "undeclared consumed artifact",
            ):
                complete_node(
                    graph_path,
                    run_dir,
                    "inspect-context",
                    "worker",
                    run["run_version"],
                    {
                        "consumed_artifacts": [
                            {
                                "artifact_id": "artifact:context",
                                "artifact_digest": sha256_json("context-v1"),
                            }
                        ]
                    },
                )
            self.assertEqual((run_dir / "run.json").read_bytes(), run_before)
            self.assertEqual((run_dir / "events.jsonl").read_bytes(), events_before)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            graph = runtime_graph(root)
            graph_path = self._write_graph(root, graph)
            run_dir = root / ".development-graph"
            run = initialize_run(
                graph_path,
                run_dir,
                actor="test",
                outcome_contract=outcome_contract(graph),
                projection=outcome_projection(graph),
            )
            run = claim_node(
                graph_path,
                run_dir,
                "inspect-context",
                "worker",
                run["run_version"],
                60,
            )
            run = complete_node(
                graph_path,
                run_dir,
                "inspect-context",
                "worker",
                run["run_version"],
                {"observed": "context"},
            )
            run = claim_node(
                graph_path,
                run_dir,
                "implement-change",
                "worker",
                run["run_version"],
                60,
            )
            run_before = (run_dir / "run.json").read_bytes()
            events_before = (run_dir / "events.jsonl").read_bytes()
            with self.assertRaisesRegex(
                RuntimeStateError,
                "artifact_digest",
            ):
                complete_node(
                    graph_path,
                    run_dir,
                    "implement-change",
                    "worker",
                    run["run_version"],
                    {
                        "consumed_artifacts": [
                            {
                                "artifact_id": "artifact:context",
                                "artifact_digest": "not-a-digest",
                            }
                        ]
                    },
                )
            self.assertEqual((run_dir / "run.json").read_bytes(), run_before)
            self.assertEqual((run_dir / "events.jsonl").read_bytes(), events_before)

    def test_v2_semantic_artifact_change_previews_only_proven_consumers(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            graph_path, run_dir, _, _, request = (
                self._semantic_invalidation_case(root)
            )

            preview = graphctl.preview_semantic_invalidation(
                request,
                graph_path,
                run_dir,
            )

            self.assertTrue(preview["valid"], preview)
            self.assertTrue(preview["apply_allowed"], preview)
            self.assertFalse(preview["gate_required"])
            impact = preview["impact"]
            self.assertEqual(
                impact["proven_artifact_consumer_node_ids"],
                ["implement-change", "review-context"],
            )
            self.assertEqual(
                impact["semantic_affected_node_ids"],
                ["implement-change", "run-tests", "review-context"],
            )
            self.assertEqual(
                impact["verified_node_ids"],
                ["implement-change", "run-tests", "review-context"],
            )
            self.assertEqual(
                impact["outside_projection_node_ids"],
                ["outside-finding"],
            )
            self.assertEqual(impact["requirement_ids"], [])
            self.assertEqual(impact["acceptance_ids"], [])
            self.assertEqual(impact["terminal_node_ids"], [])
            self.assertAlmostEqual(impact["verified_node_fraction"], 0.6)

    def test_v2_semantic_impact_requires_digest_bound_categorical_gate(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            graph_path, run_dir, _, run, request = (
                self._semantic_invalidation_case(
                    root,
                    reaches_terminal=True,
                    impact_policy={
                        "max_verified_node_count": 5,
                        "max_verified_node_fraction": 1.0,
                    },
                )
            )
            preview = graphctl.preview_semantic_invalidation(
                request,
                graph_path,
                run_dir,
            )
            self.assertTrue(preview["valid"], preview)
            self.assertFalse(preview["apply_allowed"])
            self.assertTrue(preview["gate_required"])
            self.assertEqual(
                set(preview["impact"]["gate_reasons"]),
                {"accepted-requirement", "acceptance", "terminal"},
            )

            tampered = copy.deepcopy(request)
            tampered["impact_gate"] = self._impact_gate(run, "0" * 64)
            rejected = graphctl.preview_semantic_invalidation(
                tampered,
                graph_path,
                run_dir,
            )
            self.assertFalse(rejected["apply_allowed"])
            self.assertTrue(
                any("impact digest" in error for error in rejected["gate_errors"]),
                rejected,
            )

            approved = copy.deepcopy(request)
            approved["impact_gate"] = self._impact_gate(
                run,
                preview["impact_digest"],
            )
            accepted = graphctl.preview_semantic_invalidation(
                approved,
                graph_path,
                run_dir,
            )
            self.assertTrue(accepted["valid"], accepted)
            self.assertTrue(accepted["apply_allowed"], accepted)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            graph_path, run_dir, _, _, request = (
                self._semantic_invalidation_case(
                    root,
                    cross_requirement=True,
                    impact_policy={
                        "max_verified_node_count": 5,
                        "max_verified_node_fraction": 1.0,
                    },
                )
            )
            cross = graphctl.preview_semantic_invalidation(
                request,
                graph_path,
                run_dir,
            )
            self.assertTrue(cross["valid"], cross)
            self.assertFalse(cross["apply_allowed"])
            self.assertIn(
                "cross-requirement",
                cross["impact"]["gate_reasons"],
            )
            self.assertEqual(
                cross["impact"]["requirement_ids"],
                [
                    "requirement:context-review",
                    "requirement:requested-change",
                ],
            )

    def test_v2_semantic_impact_requires_contract_fanout_gate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            graph_path, run_dir, _, _, request = (
                self._semantic_invalidation_case(
                    root,
                    impact_policy={
                        "max_verified_node_count": 1,
                        "max_verified_node_fraction": 0.2,
                    },
                )
            )

            preview = graphctl.preview_semantic_invalidation(
                request,
                graph_path,
                run_dir,
            )

            self.assertTrue(preview["valid"], preview)
            self.assertFalse(preview["apply_allowed"])
            self.assertEqual(
                set(preview["impact"]["gate_reasons"]),
                {"verified-node-count", "verified-node-fraction"},
            )

    def test_v2_semantic_apply_rejects_stale_cas_without_writes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            graph_path, run_dir, _, _, request = (
                self._semantic_invalidation_case(root)
            )
            preview = graphctl.preview_semantic_invalidation(
                request,
                graph_path,
                run_dir,
                transaction_timestamp="2026-08-11T01:00:00Z",
            )
            self.assertTrue(preview["apply_allowed"], preview)
            run_before = (run_dir / "run.json").read_bytes()
            events_before = (run_dir / "events.jsonl").read_bytes()

            with self.assertRaisesRegex(StaleStateError, "stale run file"):
                graphctl.apply_semantic_invalidation(
                    request,
                    graph_path,
                    run_dir,
                    transaction_timestamp=preview["transaction_timestamp"],
                    expected_impact_digest=preview["impact_digest"],
                    expected_run_sha256="0" * 64,
                    expected_events_sha256=preview["expected_events_sha256"],
                )
            with self.assertRaisesRegex(StaleStateError, "impact digest"):
                graphctl.apply_semantic_invalidation(
                    request,
                    graph_path,
                    run_dir,
                    transaction_timestamp=preview["transaction_timestamp"],
                    expected_impact_digest="0" * 64,
                    expected_run_sha256=preview["expected_run_sha256"],
                    expected_events_sha256=preview["expected_events_sha256"],
                )
            self.assertEqual((run_dir / "run.json").read_bytes(), run_before)
            self.assertEqual((run_dir / "events.jsonl").read_bytes(), events_before)

    def test_v2_semantic_apply_appends_event_and_preserves_projection(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            graph_path, run_dir, _, _, request = (
                self._semantic_invalidation_case(root)
            )
            projection_path = run_dir / load_run(run_dir)["projection"]["path"]
            projection_before = projection_path.read_bytes()
            event_count_before = len(
                (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
            )
            preview = graphctl.preview_semantic_invalidation(
                request,
                graph_path,
                run_dir,
                transaction_timestamp="2026-08-11T02:00:00Z",
            )

            result = graphctl.apply_semantic_invalidation(
                request,
                graph_path,
                run_dir,
                transaction_timestamp=preview["transaction_timestamp"],
                expected_impact_digest=preview["impact_digest"],
                expected_run_sha256=preview["expected_run_sha256"],
                expected_events_sha256=preview["expected_events_sha256"],
            )

            updated = load_run(run_dir)
            self.assertTrue(result["applied"], result)
            self.assertEqual(
                result["invalidated_node_ids"],
                ["implement-change", "run-tests", "review-context"],
            )
            self.assertEqual(
                {
                    node_id
                    for node_id, state in updated["nodes"].items()
                    if state["state"] == "invalidated"
                },
                {"implement-change", "run-tests", "review-context"},
            )
            self.assertEqual(updated["nodes"]["inspect-context"]["state"], "verified")
            self.assertEqual(updated["nodes"]["finish-change"]["state"], "verified")
            self.assertEqual(updated["status"], "completed")
            self.assertEqual(projection_path.read_bytes(), projection_before)
            records = [
                json.loads(line)
                for line in (run_dir / "events.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            self.assertEqual(len(records), event_count_before + 1)
            self.assertEqual(records[-1]["action"], "semantic-nodes-invalidated")
            self.assertEqual(
                records[-1]["details"]["impact_digest"],
                preview["impact_digest"],
            )
            self.assertEqual(verify_runtime_integrity(graph_path, run_dir), [])


    def test_phase5_episode_schemas_are_strict_and_gate_bound(self) -> None:
        references = SCRIPTS.parent / "references"
        run_schema = json.loads(
            (references / "development-graph.run.schema.json").read_text(
                encoding="utf-8"
            )
        )
        episodes = run_schema["$defs"]["runtimeV2"]["properties"]["episodes"]
        self.assertEqual(
            episodes["additionalProperties"],
            {"$ref": "#/$defs/causalEpisode"},
        )
        episode = run_schema["$defs"]["causalEpisode"]
        self.assertFalse(episode["additionalProperties"])
        self.assertIn("repair_cycles", episode["required"])
        self.assertIn("progress_node_ids", episode["required"])
        self.assertIn("seen_acceptance_witness_digests", episode["required"])

        for name in (
            "development-graph.promotion.schema.json",
            "development-graph.invalidation.schema.json",
        ):
            schema = json.loads((references / name).read_text(encoding="utf-8"))
            self.assertIn("episode_gate", schema["required"], name)
            self.assertIn("episode_gate", schema["properties"], name)

        mutation = json.loads(
            (
                references / "development-graph.mutation.schema.json"
            ).read_text(encoding="utf-8")
        )
        record = mutation["$defs"]["version2Record"]
        for field in (
            "causal_episode_id",
            "episode_effect",
            "episode_gate",
        ):
            self.assertIn(field, record["required"])
            self.assertIn(field, record["properties"])

    def test_semantic_invalidation_schema_binds_preview_and_gate(self) -> None:
        schema_path = (
            SCRIPTS.parent
            / "references"
            / "development-graph.invalidation.schema.json"
        )
        self.assertTrue(schema_path.is_file(), schema_path)
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        self.assertIn("impact_gate", schema["properties"])
        self.assertIn("artifact_changes", schema["properties"])
        self.assertIn("expected_projection_checksum", schema["required"])

        promotion_path = (
            SCRIPTS.parent
            / "references"
            / "development-graph.promotion.schema.json"
        )
        promotion = json.loads(promotion_path.read_text(encoding="utf-8"))
        self.assertIn("impact_gate", promotion["required"])
        self.assertIn("impact_gate", promotion["properties"])
        self.assertNotIn(
            "maxItems",
            promotion["properties"]["semantic_invalidation"]["properties"][
                "nodes"
            ],
        )

    def test_phase3_cli_defaults_keep_operations_distinct(self) -> None:
        parser = build_parser()
        common = [
            "--graph",
            "graph.json",
            "--run-dir",
            "run",
            "--request",
            "request.json",
        ]

        promote = parser.parse_args(["promote", *common])
        defer = parser.parse_args(["defer", *common])
        migrate = parser.parse_args(["migrate-outcome", *common])
        semantic = parser.parse_args(["semantic-invalidate", *common])

        self.assertEqual(promote.command, "promote")
        self.assertFalse(promote.apply)
        self.assertEqual(defer.command, "defer")
        self.assertFalse(defer.apply)
        self.assertEqual(migrate.command, "migrate-outcome")
        self.assertFalse(migrate.apply)
        self.assertEqual(semantic.command, "semantic-invalidate")
        self.assertFalse(semantic.apply)


if __name__ == "__main__":
    unittest.main()
