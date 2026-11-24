from __future__ import annotations

import copy
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

from validate_graph import canonical_checksum, main, validate_graph  # noqa: E402


def node(
    node_id: str,
    node_type: str,
    inputs: list[dict],
    outputs: list[dict],
    *,
    writes: list[str] | None = None,
    state: str = "planned",
) -> dict:
    writes = writes or []
    side_level = "reversible" if writes else "none"
    rollback = f"Restore files changed by {node_id} from version control." if writes else "none"
    executor_kind = "human" if node_type == "human_gate" else "deterministic"
    return {
        "id": node_id,
        "type": node_type,
        "title": node_id.replace("-", " ").title(),
        "goal": f"Produce the bounded result for {node_id}.",
        "state": state,
        "executor": {
            "kind": executor_kind,
            "name": "test executor",
            "instructions": f"Execute {node_id} and preserve evidence.",
        },
        "inputs": inputs,
        "outputs": outputs,
        "acceptance": [{
            "id": "evidence-exists",
            "kind": "artifact",
            "criterion": f"The {node_id} artifact exists and matches its declared format.",
            "evidence": f"evidence:{node_id}",
        }],
        "permissions": {"read": ["project"], "write": writes, "external": [], "destructive": False},
        "execution": {"max_attempts": 2, "timeout_minutes": 20, "completion_condition": "Acceptance evidence is recorded."},
        "side_effects": {
            "level": side_level,
            "description": f"Writes {_join(writes)}." if writes else "none",
            "rollback": rollback,
            "approval_required": False,
        },
        "resources": writes,
        "tags": ["test"],
    }


def _join(values: list[str]) -> str:
    return ", ".join(values) if values else "nothing"


def edge(edge_id: str, source: str, target: str, edge_type: str, artifacts: list[str]) -> dict:
    return {
        "id": edge_id,
        "from": source,
        "to": target,
        "type": edge_type,
        "condition": "upstream acceptance evidence exists",
        "artifacts": artifacts,
        "max_traversals": None,
        "exit_condition": None,
    }


def valid_graph() -> dict:
    context = {"name": "context", "artifact": "artifact:context", "format": "markdown"}
    patch = {"name": "patch", "artifact": "artifact:patch", "format": "git-diff"}
    results = {"name": "test results", "artifact": "artifact:test-results", "format": "json"}
    handoff = {"name": "handoff", "artifact": "artifact:handoff", "format": "markdown"}
    return {
        "schema_version": 1,
        "graph_id": "sample-change",
        "graph_version": 1,
        "title": "Sample Change",
        "purpose": "Implement and verify one bounded sample change.",
        "project": {
            "name": "sample",
            "root": "sample",
            "instructions": ["sample/AGENTS.md"],
            "context_version": "commit:abc123",
        },
        "status": "draft",
        "templates": ["change", "bounded-feedback"],
        "entry_nodes": ["inspect-context"],
        "terminal_nodes": ["finish-change"],
        "nodes": [
            node("inspect-context", "operation", [], [context]),
            node("implement-change", "work", [{"name": "context", "artifact": "artifact:context", "required": True}], [patch], writes=["src/"]),
            node("run-tests", "verification", [{"name": "patch", "artifact": "artifact:patch", "required": True}], [results], writes=["reports/tests.json"]),
            node("finish-change", "work", [{"name": "test results", "artifact": "artifact:test-results", "required": True}], [handoff], writes=["reports/handoff.md"]),
        ],
        "edges": [
            edge("context-to-implementation", "inspect-context", "implement-change", "consumes", ["artifact:context"]),
            edge("implementation-to-tests", "implement-change", "run-tests", "consumes", ["artifact:patch"]),
            edge("tests-to-finish-consumption", "run-tests", "finish-change", "consumes", ["artifact:test-results"]),
            edge("tests-validate-finish", "run-tests", "finish-change", "validates", ["artifact:test-results"]),
            {
                "id": "tests-feedback-to-implementation",
                "from": "run-tests",
                "to": "implement-change",
                "type": "feedback",
                "condition": "required tests fail",
                "artifacts": ["artifact:test-results"],
                "max_traversals": 3,
                "exit_condition": "tests pass or three repairs exhaust and require human review",
            },
        ],
        "assumptions": ["The sample repository has a deterministic test command."],
        "risks": ["The sample test may expose an unrelated failure."],
    }


class ValidateGraphTests(unittest.TestCase):
    def assert_invalid_with(self, graph: dict, text: str) -> None:
        report = validate_graph(graph)
        self.assertFalse(report["valid"], report)
        self.assertTrue(any(text in error for error in report["errors"]), report["errors"])

    def test_valid_graph_passes(self) -> None:
        report = validate_graph(valid_graph())
        self.assertTrue(report["valid"], report)
        self.assertEqual(report["stats"]["nodes"], 4)
        self.assertEqual(report["stats"]["edges"], 5)

    def test_v3_context_binding_is_optional_and_portable(self) -> None:
        graph = valid_graph()
        target = graph["nodes"][1]
        target["executor"]["kind"] = "agent"
        target["context_control"] = {
            "version": 3,
            "plan_path": "context/implement-change.json",
            "mode": "executable",
            "tokenizer": "deterministic-word-v1",
            "adapter": "fake-deterministic",
            "retention": "digest-only",
        }

        report = validate_graph(graph)

        self.assertTrue(report["valid"], report)

    def test_v3_context_binding_rejects_absolute_path_and_deterministic_executor(self) -> None:
        graph = valid_graph()
        target = graph["nodes"][1]
        target["context_control"] = {
            "version": 3,
            "plan_path": "/" + "private/context.json",
            "mode": "executable",
            "tokenizer": "deterministic-word-v1",
            "adapter": "fake-deterministic",
            "retention": "digest-only",
        }

        report = validate_graph(graph)

        self.assertFalse(report["valid"])
        self.assertTrue(
            any("context_control.plan_path" in error for error in report["errors"]),
            report,
        )
        self.assertTrue(
            any("agent or hybrid" in error for error in report["errors"]),
            report,
        )

    def test_missing_required_field_fails(self) -> None:
        graph = valid_graph()
        del graph["purpose"]
        self.assert_invalid_with(graph, "$.purpose: is required")

    def test_unhashable_enum_value_is_reported_without_crashing(self) -> None:
        graph = valid_graph()
        graph["status"] = []
        self.assert_invalid_with(graph, "$.status")

    def test_unhashable_edge_endpoint_is_reported_without_crashing(self) -> None:
        graph = valid_graph()
        graph["edges"][0]["from"] = []
        self.assert_invalid_with(graph, "$.edges[0].from")

    def test_scheduling_cycle_fails(self) -> None:
        graph = valid_graph()
        graph["edges"].append(edge("finish-to-context", "finish-change", "inspect-context", "requires", []))
        self.assert_invalid_with(graph, "scheduling edges contain a cycle")

    def test_unknown_edge_endpoint_fails(self) -> None:
        graph = valid_graph()
        graph["edges"][0]["from"] = "missing-node"
        self.assert_invalid_with(graph, "references unknown node")

    def test_unreachable_node_fails(self) -> None:
        graph = valid_graph()
        isolated = node(
            "isolated-work",
            "work",
            [],
            [{"name": "isolated", "artifact": "artifact:isolated", "format": "text"}],
        )
        graph["nodes"].append(isolated)
        self.assert_invalid_with(graph, "not reachable from an entry node")

    def parallel_resource_graph(self) -> dict:
        graph = valid_graph()
        parallel = node(
            "parallel-work",
            "work",
            [{"name": "context", "artifact": "artifact:context", "required": True}],
            [{"name": "parallel patch", "artifact": "artifact:parallel-patch", "format": "git-diff"}],
            writes=["src/"],
        )
        graph["nodes"].insert(2, parallel)
        finish = next(item for item in graph["nodes"] if item["id"] == "finish-change")
        finish["inputs"].append({"name": "parallel patch", "artifact": "artifact:parallel-patch", "required": True})
        graph["edges"].extend([
            edge("context-to-parallel-work", "inspect-context", "parallel-work", "consumes", ["artifact:context"]),
            edge("parallel-work-to-finish", "parallel-work", "finish-change", "consumes", ["artifact:parallel-patch"]),
        ])
        return graph

    def test_parallel_mutations_on_shared_resource_require_conflict_edge(self) -> None:
        graph = self.parallel_resource_graph()
        self.assert_invalid_with(graph, "unordered mutating nodes share resources")

    def test_parallel_shared_resource_with_conflict_edge_passes(self) -> None:
        graph = self.parallel_resource_graph()
        graph["edges"].append(edge("implementation-conflicts-with-parallel", "implement-change", "parallel-work", "conflicts", []))
        report = validate_graph(graph)
        self.assertTrue(report["valid"], report)

    def test_parallel_mutations_may_share_read_only_permission(self) -> None:
        graph = self.parallel_resource_graph()
        parallel = next(item for item in graph["nodes"] if item["id"] == "parallel-work")
        parallel["permissions"]["write"] = ["docs/parallel.md"]
        parallel["resources"] = ["docs/parallel.md"]
        parallel["side_effects"]["description"] = "Writes docs/parallel.md."
        parallel["side_effects"]["rollback"] = "Restore docs/parallel.md from version control."
        report = validate_graph(graph)
        self.assertTrue(report["valid"], report)
        self.assertIn("project", parallel["permissions"]["read"])
        implement = next(item for item in graph["nodes"] if item["id"] == "implement-change")
        self.assertIn("project", implement["permissions"]["read"])

    def test_feedback_requires_bound_and_exit(self) -> None:
        graph = valid_graph()
        graph["edges"][-1]["max_traversals"] = None
        graph["edges"][-1]["exit_condition"] = None
        self.assert_invalid_with(graph, "$.edges[4].max_traversals")
        self.assert_invalid_with(graph, "$.edges[4].exit_condition")

    def test_required_artifact_needs_consumes_edge(self) -> None:
        graph = valid_graph()
        graph["edges"] = [item for item in graph["edges"] if item["id"] != "implementation-to-tests"]
        self.assert_invalid_with(graph, "needs a consumes edge")

    def test_duplicate_artifact_producer_fails(self) -> None:
        graph = valid_graph()
        graph["nodes"][1]["outputs"][0]["artifact"] = "artifact:context"
        self.assert_invalid_with(graph, "is also produced")

    def test_invalidated_state_requires_invalidation_edge(self) -> None:
        graph = valid_graph()
        graph["nodes"][1]["state"] = "invalidated"
        self.assert_invalid_with(graph, "invalidated nodes require an incoming invalidates edge")

    def test_destructive_node_requires_gate(self) -> None:
        graph = valid_graph()
        target = graph["nodes"][1]
        target["permissions"]["destructive"] = True
        target["side_effects"] = {
            "level": "destructive",
            "description": "Deletes incompatible stored state.",
            "rollback": "Restore snapshot snapshot:before-change.",
            "approval_required": True,
        }
        self.assert_invalid_with(graph, "requires an incoming approves edge")

    def test_destructive_node_with_human_gate_passes(self) -> None:
        graph = valid_graph()
        approval = {"name": "approval", "artifact": "artifact:approval", "format": "approval-record"}
        gate = node(
            "approve-change",
            "human_gate",
            [{"name": "context", "artifact": "artifact:context", "required": True}],
            [approval],
        )
        graph["nodes"].insert(1, gate)
        target = next(item for item in graph["nodes"] if item["id"] == "implement-change")
        target["inputs"].append({"name": "approval", "artifact": "artifact:approval", "required": True})
        target["permissions"]["destructive"] = True
        target["side_effects"] = {
            "level": "destructive",
            "description": "Deletes incompatible stored state.",
            "rollback": "Restore snapshot snapshot:before-change.",
            "approval_required": True,
        }
        graph["edges"].extend([
            edge("context-to-approval", "inspect-context", "approve-change", "consumes", ["artifact:context"]),
            edge("approval-to-implementation-consumption", "approve-change", "implement-change", "consumes", ["artifact:approval"]),
            edge("approval-authorizes-implementation", "approve-change", "implement-change", "approves", ["artifact:approval"]),
        ])
        report = validate_graph(graph)
        self.assertTrue(report["valid"], report)

    def test_approves_edge_requires_declared_target_input(self) -> None:
        graph = valid_graph()
        approval = {"name": "approval", "artifact": "artifact:approval", "format": "approval-record"}
        gate = node(
            "approve-change",
            "human_gate",
            [{"name": "context", "artifact": "artifact:context", "required": True}],
            [approval],
        )
        graph["nodes"].insert(1, gate)
        graph["edges"].extend([
            edge("context-to-approval", "inspect-context", "approve-change", "consumes", ["artifact:context"]),
            edge("approval-authorizes-implementation", "approve-change", "implement-change", "approves", ["artifact:approval"]),
        ])
        self.assert_invalid_with(graph, "is not declared as an input by target node 'implement-change'")

    def test_human_gate_must_use_human_executor(self) -> None:
        graph = valid_graph()
        graph["nodes"][0]["type"] = "human_gate"
        graph["nodes"][0]["executor"]["kind"] = "agent"
        self.assert_invalid_with(graph, "human_gate nodes require a human executor")

    def test_markdown_checksum_parity(self) -> None:
        graph = valid_graph()
        marker = f"<!-- development-graph-sha256: {canonical_checksum(graph)} -->"
        report = validate_graph(graph, marker)
        self.assertTrue(report["valid"])
        self.assertEqual(report["canonical_sha256"], canonical_checksum(graph))
        self.assertEqual(report["markdown_marker"], marker)
        changed = copy.deepcopy(graph)
        changed["graph_version"] = 2
        self.assert_invalid_with_markdown(changed, marker, "checksum mismatch")

    def assert_invalid_with_markdown(self, graph: dict, markdown: str, text: str) -> None:
        report = validate_graph(graph, markdown)
        self.assertFalse(report["valid"], report)
        self.assertTrue(any(text in error for error in report["errors"]), report["errors"])

    def test_cli_json_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            graph_path = Path(directory) / "graph.json"
            graph_path.write_text(json.dumps(valid_graph()), encoding="utf-8")
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                self.assertEqual(main([str(graph_path), "--json"]), 0)
            report = json.loads(stdout.getvalue())
            self.assertTrue(report["valid"])
            self.assertEqual(report["canonical_sha256"], canonical_checksum(valid_graph()))
            self.assertEqual(
                report["markdown_marker"],
                f"<!-- development-graph-sha256: {canonical_checksum(valid_graph())} -->",
            )

    def test_deterministic_binding_rejects_project_root_escape(self) -> None:
        graph = valid_graph()
        graph["nodes"][0]["execution"].update({
            "command": ["python3", "-c", "print('safe argv')"],
            "working_directory": "../outside",
            "environment": [],
            "evidence_paths": ["../../secret"],
            "capture_limit_bytes": 4096,
            "timeout_seconds": 5,
        })
        self.assert_invalid_with(graph, "must stay within the project root")


if __name__ == "__main__":
    unittest.main()
