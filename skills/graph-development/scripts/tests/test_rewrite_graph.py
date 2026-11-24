from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(TESTS))

from fixtures import (  # noqa: E402
    backlog_node,
    mutation_for,
    outcome_contract,
    outcome_projection,
    runtime_graph,
)
from graph_core import (  # noqa: E402
    RuntimeStateError,
    StaleStateError,
    atomic_write_json,
    load_json,
    sha256_json,
)
from graphctl import (  # noqa: E402
    claim_node,
    initialize_run,
    load_run,
    status_report,
    verify_runtime_integrity,
)
from rewrite_graph import apply_mutation, preview_mutation  # noqa: E402
from render_graph import render_markdown  # noqa: E402
from test_validate_graph import edge, node  # noqa: E402
from validate_graph import canonical_checksum  # noqa: E402


class RewriteGraphTests(unittest.TestCase):
    def _setup(self, root: Path) -> tuple[Path, Path, dict, dict]:
        graph = runtime_graph(root)
        graph_path = root / "development-graph.json"
        run_dir = root / ".development-graph"
        atomic_write_json(graph_path, graph)
        (root / "development-graph.md").write_text(
            render_markdown(graph, graph_path.name),
            encoding="utf-8",
        )
        run = initialize_run(graph_path, run_dir, actor="test")
        return graph_path, run_dir, graph, run

    def _setup_projected(
        self,
        root: Path,
        graph: dict | None = None,
    ) -> tuple[Path, Path, dict, dict]:
        graph = graph or runtime_graph(root)
        graph_path = root / "development-graph.json"
        run_dir = root / ".development-graph"
        atomic_write_json(graph_path, graph)
        (root / "development-graph.md").write_text(
            render_markdown(graph, graph_path.name),
            encoding="utf-8",
        )
        contract = outcome_contract(graph)
        projection = outcome_projection(graph)
        try:
            run = initialize_run(
                graph_path,
                run_dir,
                actor="test",
                outcome_contract=contract,
                projection=projection,
            )
        except TypeError as exc:
            self.fail(
                "OLD BEHAVIOR: schema-v1 initialization has no immutable outcome "
                f"contract or run projection seam: {exc}"
            )
        return graph_path, run_dir, graph, run

    def _mark_completed(self, run_dir: Path, run: dict) -> dict:
        completed = copy.deepcopy(run)
        for state in completed["nodes"].values():
            state["state"] = "verified"
        completed["status"] = "completed"
        atomic_write_json(run_dir / "run.json", completed)
        return completed

    def _apply_record_only(
        self,
        mutation: dict,
        graph_path: Path,
        run_dir: Path,
        graph: dict,
        run: dict,
    ) -> dict:
        preview = preview_mutation(
            mutation,
            graph_path,
            run_dir,
            actor="test",
            authority="test:record-only",
        )
        self.assertTrue(preview["valid"], preview)
        return apply_mutation(
            mutation,
            graph_path,
            run_dir,
            expected_graph_checksum=canonical_checksum(graph),
            expected_run_version=run["run_version"],
            actor="test",
            authority="test:record-only",
            transaction_timestamp=preview["transaction_timestamp"],
            expected_lifecycle_base_digest=preview["lifecycle_base_digest"],
            expected_lifecycle_plan_digest=preview["lifecycle_plan_digest"],
        )

    def test_completed_run_record_only_addition_preserves_completion(self) -> None:
        """Old behavior enrolls a durable addition and reopens completed work."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            graph_path, run_dir, graph, run = self._setup_projected(root)
            run = self._mark_completed(run_dir, run)
            before_nodes = copy.deepcopy(run["nodes"])
            before_contract = copy.deepcopy(run["outcome_contract"])
            before_projection = copy.deepcopy(run["projection"])
            added = backlog_node()
            mutation = mutation_for(
                graph,
                {"op": "add-node", "id": added["id"], "value": added},
                mutation_id="record-future-debt",
                record_only=True,
                expected_run_version=run["run_version"],
            )
            mutation["operations"].extend(
                [
                    {
                        "op": "update-root",
                        "field": "entry_nodes",
                        "value": graph["entry_nodes"] + [added["id"]],
                    },
                    {
                        "op": "update-root",
                        "field": "terminal_nodes",
                        "value": graph["terminal_nodes"] + [added["id"]],
                    },
                ]
            )

            result = self._apply_record_only(
                mutation, graph_path, run_dir, graph, run
            )

            updated_graph = load_json(graph_path)
            updated_run = load_run(run_dir)
            self.assertIn(added["id"], {item["id"] for item in updated_graph["nodes"]})
            self.assertEqual(updated_run["nodes"], before_nodes)
            self.assertEqual(updated_run["outcome_contract"], before_contract)
            self.assertEqual(updated_run["projection"], before_projection)
            self.assertEqual(updated_run["status"], "completed")
            self.assertEqual(updated_run["observed_graph_version"], 2)
            report = status_report(graph_path, run_dir)
            backlog = report.get("dlg_backlog_nodes", report.get("backlog_nodes"))
            self.assertEqual(backlog, [added["id"]])
            self.assertEqual(result["invalidated_nodes"], [])
            self.assertEqual(verify_runtime_integrity(graph_path, run_dir), [])
            records = [
                json.loads(line)
                for line in (run_dir / "events.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
                if line
            ]
            self.assertEqual(records[-1]["action"], "graph-definition-recorded")
            self.assertEqual(
                records[-1]["details"]["projection_checksum_before"],
                records[-1]["details"]["projection_checksum_after"],
            )
            self.assertEqual(
                records[-1]["details"]["semantic_invalidation"],
                {"nodes": [], "artifacts": []},
            )


    def test_repair_episode_survives_fresh_mutation_ids_and_gates_independence(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            graph_path, run_dir, graph, run = self._setup_projected(root)
            run = self._mark_completed(run_dir, run)

            first = backlog_node("episode-finding-one")
            first_mutation = mutation_for(
                graph,
                {"op": "add-node", "id": first["id"], "value": first},
                mutation_id="episode-cycle-one",
                record_only=True,
                expected_run_version=run["run_version"],
            )
            first_mutation.update(
                {
                    "causal_episode_id": "same-repair-episode",
                    "episode_effect": "repair-cycle",
                    "episode_gate": None,
                    "evidence": ["evidence:same-root-cause"],
                }
            )
            first_mutation["operations"].extend(
                [
                    {
                        "op": "update-root",
                        "field": "entry_nodes",
                        "value": graph["entry_nodes"] + [first["id"]],
                    },
                    {
                        "op": "update-root",
                        "field": "terminal_nodes",
                        "value": graph["terminal_nodes"] + [first["id"]],
                    },
                ]
            )

            first_result = self._apply_record_only(
                first_mutation,
                graph_path,
                run_dir,
                graph,
                run,
            )

            graph_after_first = load_json(graph_path)
            run_after_first = load_run(run_dir)
            episode = run_after_first["episodes"]["same-repair-episode"]
            self.assertEqual(episode["status"], "needs_human")
            self.assertEqual(episode["repair_cycles"], 1)
            self.assertEqual(episode["no_progress_cycles"], 1)
            self.assertEqual(
                episode["operation_ids"],
                ["mutation:episode-cycle-one"],
            )
            self.assertEqual(run_after_first["status"], "completed")
            report = status_report(graph_path, run_dir)
            self.assertEqual(report["outcome_status"], "completed")
            self.assertEqual(
                report["needs_human_episodes"],
                ["same-repair-episode"],
            )
            self.assertEqual(report["dlg_backlog_nodes"], [first["id"]])
            self.assertEqual(
                first_result["causal_episode_id"],
                "same-repair-episode",
            )
            records = [
                json.loads(line)
                for line in (run_dir / "events.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            self.assertEqual(
                [record["action"] for record in records[-2:]],
                ["graph-definition-recorded", "repair-episode-needs-human"],
            )

            second = backlog_node("episode-finding-two")
            second_mutation = mutation_for(
                graph_after_first,
                {"op": "add-node", "id": second["id"], "value": second},
                mutation_id="episode-cycle-two",
                record_only=True,
                expected_run_version=run_after_first["run_version"],
            )
            second_mutation.update(
                {
                    "causal_episode_id": "same-repair-episode",
                    "episode_effect": "repair-cycle",
                    "episode_gate": None,
                    "evidence": ["evidence:same-root-cause"],
                }
            )
            second_mutation["operations"].extend(
                [
                    {
                        "op": "update-root",
                        "field": "entry_nodes",
                        "value": graph_after_first["entry_nodes"] + [second["id"]],
                    },
                    {
                        "op": "update-root",
                        "field": "terminal_nodes",
                        "value": graph_after_first["terminal_nodes"] + [second["id"]],
                    },
                ]
            )
            before = {
                "graph": graph_path.read_bytes(),
                "run": (run_dir / "run.json").read_bytes(),
                "events": (run_dir / "events.jsonl").read_bytes(),
            }

            blocked = preview_mutation(
                second_mutation,
                graph_path,
                run_dir,
                actor="test",
                authority="test:record-only",
            )

            self.assertTrue(blocked["valid"], blocked)
            self.assertFalse(blocked["apply_allowed"], blocked)
            self.assertEqual(
                blocked["episode_gate_required"],
                "authorize-next-cycle",
            )
            self.assertTrue(blocked["episode_operation_digest"])
            self.assertEqual(graph_path.read_bytes(), before["graph"])
            self.assertEqual((run_dir / "run.json").read_bytes(), before["run"])
            self.assertEqual(
                (run_dir / "events.jsonl").read_bytes(),
                before["events"],
            )

            independent = copy.deepcopy(second_mutation)
            independent["mutation_id"] = "independent-cycle"
            independent["new_context_version"] = "mutation:independent-cycle"
            independent["causal_episode_id"] = "independent-regression"
            independent["evidence"] = ["evidence:independent-defect"]
            independent_preview = preview_mutation(
                independent,
                graph_path,
                run_dir,
                actor="test",
                authority="test:record-only",
            )
            self.assertTrue(independent_preview["valid"], independent_preview)
            self.assertFalse(
                independent_preview["apply_allowed"],
                independent_preview,
            )
            self.assertEqual(
                independent_preview["episode_gate_required"],
                "approve-independent-episode",
            )
            independent["episode_gate"] = {
                "decision": "approve-independent-episode",
                "run_id": run_after_first["run_id"],
                "causal_episode_id": "independent-regression",
                "episode_operation_digest": independent_preview[
                    "episode_operation_digest"
                ],
                "approver": "test:record-only",
                "evidence_digest": independent_preview[
                    "expected_gate_evidence_digest"
                ],
                "approved_at": "2026-08-11T05:00:00Z",
            }

            independent_result = self._apply_record_only(
                independent,
                graph_path,
                run_dir,
                graph_after_first,
                run_after_first,
            )

            final_run = load_run(run_dir)
            self.assertTrue(independent_result["applied"])
            self.assertEqual(
                set(final_run["episodes"]),
                {"same-repair-episode", "independent-regression"},
            )
            self.assertEqual(
                final_run["episodes"]["same-repair-episode"],
                episode,
            )
            self.assertEqual(
                final_run["episodes"]["independent-regression"]["status"],
                "needs_human",
            )
            self.assertEqual(
                final_run["episodes"]["independent-regression"][
                    "repair_cycles"
                ],
                1,
            )
            self.assertEqual(final_run["status"], "completed")
            self.assertEqual(verify_runtime_integrity(graph_path, run_dir), [])

    def test_active_run_unrelated_addition_is_backlog_not_claimable(self) -> None:
        """Old behavior places every new DLG node into the active run inventory."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            graph_path, run_dir, graph, run = self._setup_projected(root)
            before_nodes = copy.deepcopy(run["nodes"])
            before_status = run["status"]
            added = backlog_node()
            mutation = mutation_for(
                graph,
                {"op": "add-node", "id": added["id"], "value": added},
                mutation_id="record-active-future-debt",
                record_only=True,
                expected_run_version=run["run_version"],
            )
            mutation["operations"].extend(
                [
                    {
                        "op": "update-root",
                        "field": "entry_nodes",
                        "value": graph["entry_nodes"] + [added["id"]],
                    },
                    {
                        "op": "update-root",
                        "field": "terminal_nodes",
                        "value": graph["terminal_nodes"] + [added["id"]],
                    },
                ]
            )

            self._apply_record_only(mutation, graph_path, run_dir, graph, run)

            updated_run = load_run(run_dir)
            self.assertEqual(updated_run["nodes"], before_nodes)
            self.assertEqual(updated_run["status"], before_status)
            report = status_report(graph_path, run_dir)
            self.assertNotIn(added["id"], report["ready_nodes"])
            backlog = report.get("dlg_backlog_nodes", report.get("backlog_nodes"))
            self.assertEqual(backlog, [added["id"]])
            with self.assertRaises(RuntimeStateError):
                claim_node(
                    graph_path,
                    run_dir,
                    added["id"],
                    "worker",
                    updated_run["run_version"],
                    60,
                )

    def test_scheduling_only_rewire_does_not_invalidate_verified_work(self) -> None:
        """Old behavior uses scheduling reachability as semantic staleness."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            graph = runtime_graph(root)
            graph["edges"].append(
                edge(
                    "scheduling-only-order",
                    "inspect-context",
                    "run-tests",
                    "requires",
                    [],
                )
            )
            graph_path, run_dir, graph, run = self._setup_projected(root, graph)
            run = self._mark_completed(run_dir, run)
            before_nodes = copy.deepcopy(run["nodes"])
            before_projection = copy.deepcopy(run["projection"])
            replacement = copy.deepcopy(graph["edges"][-1])
            replacement["to"] = "finish-change"
            mutation = mutation_for(
                graph,
                {
                    "op": "replace-edge",
                    "id": replacement["id"],
                    "value": replacement,
                },
                mutation_id="record-scheduling-rewire",
                record_only=True,
                expected_run_version=run["run_version"],
            )

            result = self._apply_record_only(
                mutation,
                graph_path,
                run_dir,
                graph,
                run,
            )

            updated_run = load_run(run_dir)
            self.assertEqual(result["invalidated_nodes"], [])
            self.assertEqual(updated_run["nodes"], before_nodes)
            self.assertEqual(updated_run["projection"], before_projection)
            self.assertEqual(updated_run["status"], "completed")

    def test_current_source_promotion_reproduction_is_record_only(self) -> None:
        """Old behavior reproduces automatic enroll, broad reopen, and completion drift."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            graph_path, run_dir, graph, run = self._setup_projected(root)
            run = self._mark_completed(run_dir, run)
            before_nodes = copy.deepcopy(run["nodes"])
            case = json.loads(
                (TESTS / "data" / "outcome_first_current_source_promotion.json").read_text(
                    encoding="utf-8"
                )
            )
            added = node(
                case["node"]["id"],
                case["node"]["type"],
                [],
                [case["node"]["output"]],
            )
            operations = [
                {"op": "add-node", "id": added["id"], "value": added}
            ]
            operations.extend(
                {
                    "op": "add-edge",
                    "id": item["id"],
                    "value": edge(
                        item["id"],
                        item["from"],
                        item["to"],
                        item["type"],
                        item["artifacts"],
                    ),
                }
                for item in case["edges"]
            )
            mutation = mutation_for(
                graph,
                operations[0],
                mutation_id=case["mutation_id"],
                record_only=True,
                expected_run_version=run["run_version"],
            )
            mutation["operations"] = operations

            result = self._apply_record_only(
                mutation,
                graph_path,
                run_dir,
                graph,
                run,
            )

            updated_graph = load_json(graph_path)
            updated_run = load_run(run_dir)
            self.assertEqual(len(updated_graph["nodes"]), len(graph["nodes"]) + 1)
            self.assertEqual(result["invalidated_nodes"], [])
            self.assertEqual(updated_run["nodes"], before_nodes)
            self.assertEqual(updated_run["nodes"]["finish-change"]["state"], "verified")
            self.assertEqual(updated_run["status"], "completed")


    def test_record_only_path_never_calls_scheduling_invalidation_helpers(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            graph_path, run_dir, graph, run = self._setup_projected(root)
            added = backlog_node("recorded-only")
            mutation = mutation_for(
                graph,
                {"op": "add-node", "id": added["id"], "value": added},
                mutation_id="record-without-invalidation",
                record_only=True,
                expected_run_version=run["run_version"],
            )
            mutation["operations"].extend(
                [
                    {
                        "op": "update-root",
                        "field": "entry_nodes",
                        "value": graph["entry_nodes"] + [added["id"]],
                    },
                    {
                        "op": "update-root",
                        "field": "terminal_nodes",
                        "value": graph["terminal_nodes"] + [added["id"]],
                    },
                ]
            )

            with mock.patch(
                "rewrite_graph.affected_node_ids"
            ) as affected, mock.patch(
                "rewrite_graph.artifact_affected_nodes"
            ) as artifact_affected:
                result = self._apply_record_only(
                    mutation,
                    graph_path,
                    run_dir,
                    graph,
                    run,
                )

            affected.assert_not_called()
            artifact_affected.assert_not_called()
            self.assertEqual(result["invalidated_nodes"], [])
            self.assertEqual(verify_runtime_integrity(graph_path, run_dir), [])

    def test_invalid_record_only_protocol_documents_are_no_write_refusals(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            graph_path, run_dir, graph, run = self._setup_projected(root)
            added = backlog_node("protocol-check")
            base = mutation_for(
                graph,
                {"op": "add-node", "id": added["id"], "value": added},
                mutation_id="record-protocol-check",
                record_only=True,
                expected_run_version=run["run_version"],
            )
            base["operations"].extend(
                [
                    {
                        "op": "update-root",
                        "field": "entry_nodes",
                        "value": graph["entry_nodes"] + [added["id"]],
                    },
                    {
                        "op": "update-root",
                        "field": "terminal_nodes",
                        "value": graph["terminal_nodes"] + [added["id"]],
                    },
                ]
            )
            stale = copy.deepcopy(base)
            stale["expected_run_version"] += 1
            wrong_effect = copy.deepcopy(base)
            wrong_effect["runtime_effect"] = "promote"
            invalidating = copy.deepcopy(base)
            invalidating["invalidate"]["nodes"] = ["inspect-context"]
            cases = [
                (
                    "stale run version",
                    stale,
                    "does not match the current run",
                ),
                (
                    "non-record runtime effect",
                    wrong_effect,
                    "must equal record-only",
                ),
                (
                    "nonempty invalidation",
                    invalidating,
                    "record-only mutation must be empty",
                ),
            ]
            before = {
                "graph": graph_path.read_bytes(),
                "markdown": (root / "development-graph.md").read_bytes(),
                "run": (run_dir / "run.json").read_bytes(),
                "events": (run_dir / "events.jsonl").read_bytes(),
                "projection": (
                    run_dir / run["projection"]["path"]
                ).read_bytes(),
            }

            for label, invalid, expected_error in cases:
                with self.subTest(label=label):
                    report = preview_mutation(
                        invalid,
                        graph_path,
                        run_dir,
                        actor="test",
                        authority="test:record-only-refusal",
                    )
                    self.assertFalse(report["valid"], report)
                    self.assertTrue(
                        any(
                            expected_error in error
                            for error in report["errors"]
                        ),
                        report,
                    )
                    self.assertEqual(
                        graph_path.read_bytes(),
                        before["graph"],
                    )
                    self.assertEqual(
                        (root / "development-graph.md").read_bytes(),
                        before["markdown"],
                    )
                    self.assertEqual(
                        (run_dir / "run.json").read_bytes(),
                        before["run"],
                    )
                    self.assertEqual(
                        (run_dir / "events.jsonl").read_bytes(),
                        before["events"],
                    )
                    self.assertEqual(
                        (run_dir / run["projection"]["path"]).read_bytes(),
                        before["projection"],
                    )

    def test_dry_run_does_not_change_graph(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            graph_path, run_dir, graph, _ = self._setup(root)
            replacement = copy.deepcopy(graph["nodes"][1])
            replacement["title"] = "Implement Revised Change"
            mutation = mutation_for(graph, {"op": "replace-node", "id": replacement["id"], "value": replacement})
            before = graph_path.read_bytes()
            report = preview_mutation(mutation, graph_path, run_dir)
            self.assertTrue(report["valid"], report)
            self.assertEqual(graph_path.read_bytes(), before)

    def test_apply_creates_snapshot_bumps_version_and_rejects_stale_base(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            graph_path, run_dir, graph, run = self._setup(root)
            replacement = copy.deepcopy(graph["nodes"][1])
            replacement["title"] = "Implement Revised Change"
            mutation = mutation_for(graph, {"op": "replace-node", "id": replacement["id"], "value": replacement})
            preview = preview_mutation(
                mutation,
                graph_path,
                run_dir,
                actor="test",
                authority="test:rewrite",
            )
            self.assertTrue(preview["valid"], preview)
            result = apply_mutation(
                mutation,
                graph_path,
                run_dir,
                expected_graph_checksum=canonical_checksum(graph),
                expected_run_version=run["run_version"],
                actor="test",
                authority="test:rewrite",
                transaction_timestamp=preview["transaction_timestamp"],
                expected_lifecycle_base_digest=preview[
                    "lifecycle_base_digest"
                ],
                expected_lifecycle_plan_digest=preview[
                    "lifecycle_plan_digest"
                ],
            )
            updated = load_json(graph_path)
            self.assertEqual(updated["graph_version"], 2)
            self.assertEqual(updated["nodes"][1]["title"], "Implement Revised Change")
            self.assertEqual(result["snapshot"].read_bytes(), result["previous_graph_bytes"])
            with self.assertRaises(StaleStateError):
                apply_mutation(
                    mutation,
                    graph_path,
                    run_dir,
                    expected_graph_checksum=canonical_checksum(graph),
                    expected_run_version=run["run_version"],
                    actor="test",
                    authority="test:rewrite",
                    transaction_timestamp=preview["transaction_timestamp"],
                    expected_lifecycle_base_digest=preview[
                        "lifecycle_base_digest"
                    ],
                    expected_lifecycle_plan_digest=preview[
                        "lifecycle_plan_digest"
                    ],
                )

    def test_late_transaction_failure_restores_graph_runtime_ledger_and_snapshot(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            graph_path, run_dir, graph, run = self._setup(root)
            replacement = copy.deepcopy(graph["nodes"][1])
            replacement["title"] = "Transactional Rewrite"
            mutation = mutation_for(
                graph,
                {
                    "op": "replace-node",
                    "id": replacement["id"],
                    "value": replacement,
                },
                mutation_id="transactional-rewrite",
            )
            preview = preview_mutation(
                mutation,
                graph_path,
                run_dir,
                actor="test",
                authority="test:rewrite-rollback",
            )
            self.assertTrue(preview["valid"], preview)
            before = {
                "graph": graph_path.read_bytes(),
                "markdown": (root / "development-graph.md").read_bytes(),
                "run": (run_dir / "run.json").read_bytes(),
                "events": (run_dir / "events.jsonl").read_bytes(),
            }
            from project_lifecycle import _replace_artifact

            calls = 0

            def fail_late(path: Path, payload: bytes, mode: int) -> None:
                nonlocal calls
                calls += 1
                if calls == 5:
                    raise OSError("injected mutation transaction failure")
                _replace_artifact(path, payload, mode)

            with mock.patch(
                "project_lifecycle._replace_artifact",
                side_effect=fail_late,
            ), self.assertRaisesRegex(
                RuntimeStateError,
                "transaction",
            ):
                apply_mutation(
                    mutation,
                    graph_path,
                    run_dir,
                    expected_graph_checksum=canonical_checksum(graph),
                    expected_run_version=run["run_version"],
                    actor="test",
                    authority="test:rewrite-rollback",
                    transaction_timestamp=preview["transaction_timestamp"],
                    expected_lifecycle_base_digest=preview[
                        "lifecycle_base_digest"
                    ],
                    expected_lifecycle_plan_digest=preview[
                        "lifecycle_plan_digest"
                    ],
                )

            self.assertEqual(graph_path.read_bytes(), before["graph"])
            self.assertEqual((run_dir / "run.json").read_bytes(), before["run"])
            self.assertEqual(
                (run_dir / "events.jsonl").read_bytes(),
                before["events"],
            )
            self.assertEqual(
                (root / "development-graph.md").read_bytes(),
                before["markdown"],
            )
            self.assertFalse(
                (
                    run_dir
                    / "snapshots"
                    / "graph-v1-transactional-rewrite.json"
                ).exists()
            )

    def test_invalid_topology_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            graph_path, run_dir, graph, _ = self._setup(root)
            mutation = mutation_for(graph, {"op": "remove-node", "id": "run-tests"})
            report = preview_mutation(mutation, graph_path, run_dir)
            self.assertFalse(report["valid"])
            self.assertTrue(report["errors"])

    def test_destructive_edge_removal_requires_checksum_bound_approval(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            graph_path, run_dir, graph, _ = self._setup(root)
            mutation = mutation_for(graph, {"op": "remove-edge", "id": "tests-feedback-to-implementation"})
            denied = preview_mutation(mutation, graph_path, run_dir)
            self.assertFalse(denied["valid"])
            self.assertTrue(any("approval" in error for error in denied["errors"]), denied)
            mutation["approval"] = {
                "decision": "approved",
                "approver": "operator",
                "evidence": "review:remove-feedback-edge",
                "graph_checksum": canonical_checksum(graph),
            }
            approved = preview_mutation(mutation, graph_path, run_dir)
            self.assertTrue(approved["valid"], approved)
            self.assertTrue(approved["requires_approval"])


if __name__ == "__main__":
    unittest.main()
