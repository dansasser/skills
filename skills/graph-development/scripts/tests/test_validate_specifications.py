from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

from graph_core import ready_node_ids  # noqa: E402
from new_graph import build_graph, build_specification_manifest  # noqa: E402
from validate_graph import validate_graph  # noqa: E402

try:
    from validate_specifications import validate_specifications  # type: ignore  # noqa: E402
except ModuleNotFoundError:
    validate_specifications = None


def _node(graph: dict[str, object], node_id: str) -> dict[str, object]:
    return next(
        item
        for item in graph["nodes"]  # type: ignore[index]
        if isinstance(item, dict) and item.get("id") == node_id
    )


def _remove_input(node: dict[str, object], artifact: str) -> None:
    node["inputs"] = [
        item
        for item in node["inputs"]  # type: ignore[index]
        if not isinstance(item, dict) or item.get("artifact") != artifact
    ]


def _remove_edge(
    graph: dict[str, object],
    source: str,
    target: str,
    edge_type: str,
    artifact: str,
) -> None:
    graph["edges"] = [
        edge
        for edge in graph["edges"]  # type: ignore[index]
        if not (
            isinstance(edge, dict)
            and edge.get("from") == source
            and edge.get("to") == target
            and edge.get("type") == edge_type
            and artifact in edge.get("artifacts", [])
        )
    ]


def _add_input(node: dict[str, object], name: str, artifact: str) -> None:
    node["inputs"].append(  # type: ignore[union-attr]
        {"name": name, "artifact": artifact, "required": True}
    )


def _add_consumes_edge(
    graph: dict[str, object],
    source: str,
    target: str,
    artifact: str,
) -> None:
    graph["edges"].append(  # type: ignore[union-attr]
        {
            "id": f"{source}-to-{target}",
            "from": source,
            "to": target,
            "type": "consumes",
            "condition": "The decision artifact is current.",
            "artifacts": [artifact],
            "max_traversals": None,
            "exit_condition": None,
        }
    )


def _add_storage_decision(
    graph: dict[str, object],
    manifest: dict[str, object],
) -> None:
    artifact = "artifact:decision-storage-backend"
    decision_node = {
        "id": "resolve-storage-backend",
        "type": "decision",
        "title": "Resolve Storage Backend",
        "goal": "Select the durable storage backend using explicit evidence.",
        "state": "planned",
        "executor": {
            "kind": "agent",
            "name": "architecture decision adapter",
            "instructions": "Compare bounded options and preserve the selected decision.",
        },
        "inputs": [
            {
                "name": "open decisions",
                "artifact": "doc/open-questions.md",
                "required": True,
            }
        ],
        "outputs": [
            {
                "name": "storage backend decision",
                "artifact": artifact,
                "format": "decision-record",
            }
        ],
        "acceptance": [
            {
                "id": "decision-recorded",
                "kind": "review",
                "criterion": "Alternatives, evidence, selection, consequences, and revisit trigger are recorded.",
                "evidence": "doc/decisions/storage-backend.md",
            }
        ],
        "permissions": {
            "read": ["doc/open-questions.md"],
            "write": ["doc/decisions/storage-backend.md"],
            "external": [],
            "destructive": False,
        },
        "execution": {
            "max_attempts": 2,
            "timeout_minutes": 30,
            "completion_condition": "The accepted storage decision is durable and reviewable.",
        },
        "side_effects": {
            "level": "reversible",
            "description": "Writes one architecture decision record.",
            "rollback": "Restore doc/decisions/storage-backend.md from version control.",
            "approval_required": False,
        },
        "resources": ["doc/decisions/storage-backend.md"],
        "tags": ["specification", "decision"],
    }
    graph["nodes"].append(decision_node)  # type: ignore[union-attr]
    _add_consumes_edge(
        graph,
        "inventory-open-decisions",
        "resolve-storage-backend",
        "doc/open-questions.md",
    )
    for target in (
        "resolve-blocking-decisions",
        "verify-specification-set",
        "author-implementation-plan",
        "implement",
    ):
        _add_input(_node(graph, target), "storage backend decision", artifact)
        _add_consumes_edge(graph, "resolve-storage-backend", target, artifact)
    manifest["decisions"].append(  # type: ignore[union-attr]
        {
            "id": "storage-backend",
            "status": "open",
            "source_path": "doc/open-questions.md",
            "decision_node": "resolve-storage-backend",
            "artifact": artifact,
            "affected_nodes": ["implement"],
        }
    )


class SpecificationValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.assertIsNotNone(
            validate_specifications,
            "validate_specifications.py has not been implemented",
        )
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.graph = build_graph(
            self.root,
            "sample-product",
            "Sample Product",
            "specification-to-delivery",
            "conversation:approved-seed",
        )
        self.manifest = build_specification_manifest(self.graph)
        for document in self.manifest["documents"]:
            path = self.root / document["path"]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                f"# {document['id']}\n\nEvidence-backed specification.\n",
                encoding="utf-8",
            )
        plan = self.root / self.manifest["implementation_plan"]["path"]
        plan.parent.mkdir(parents=True, exist_ok=True)
        plan.write_text("# Implementation plan\n\nApproved graph mapping.\n", encoding="utf-8")

    def validate(
        self,
        graph: dict[str, object] | None = None,
        manifest: dict[str, object] | None = None,
    ) -> dict[str, object]:
        assert validate_specifications is not None
        return validate_specifications(
            graph or self.graph,
            manifest or self.manifest,
            self.root,
        )

    def assert_error_contains(
        self,
        expected: str,
        graph: dict[str, object] | None = None,
        manifest: dict[str, object] | None = None,
    ) -> None:
        report = self.validate(graph, manifest)
        self.assertFalse(report["valid"], json.dumps(report, indent=2))
        self.assertTrue(
            any(expected in error for error in report["errors"]),
            json.dumps(report, indent=2),
        )

    def test_complete_manifest_and_graph_are_valid(self) -> None:
        report = self.validate()
        self.assertTrue(report["valid"], json.dumps(report, indent=2))
        self.assertEqual(report["stats"]["documents"], 5)
        self.assertEqual(report["stats"]["decisions"], 0)

    def mixed_handoff(self) -> tuple[dict, dict]:
        graph = copy.deepcopy(self.graph)
        manifest = copy.deepcopy(self.manifest)
        direct_id = "implement-independent-slice"
        direct = copy.deepcopy(_node(graph, "implement"))
        direct["id"] = direct_id
        direct["outputs"][0]["artifact"] = "artifact:independent-slice"
        direct["permissions"]["write"] = ["independent-slice"]
        direct["resources"] = ["independent-slice"]
        plan = manifest["implementation_plan"]
        _remove_input(direct, plan["artifact"])
        graph["nodes"].append(direct)
        manifest["implementation_nodes"] = ["implement", direct_id]

        approval = manifest["approval"]
        approval["targets"].append(direct_id)
        _add_consumes_edge(graph, approval["node"], direct_id, approval["artifact"])
        graph["edges"].append({
            **graph["edges"][-1],
            "id": f'{approval["node"]}-approves-{direct_id}',
            "type": "approves",
        })
        for document in manifest["documents"]:
            if document["required"]:
                document["consumer_nodes"].append(direct_id)
                _add_input(direct, document["id"], document["artifact"])
                _add_consumes_edge(graph, document["producer_node"], direct_id, document["artifact"])
        _add_input(_node(graph, "verify-delivery"), "Independent slice", "artifact:independent-slice")
        _add_consumes_edge(graph, direct_id, "verify-delivery", "artifact:independent-slice")
        return graph, manifest

    def test_targeted_plan_and_direct_handoff_can_share_a_graph(self) -> None:
        graph, manifest = self.mixed_handoff()
        report = self.validate(graph, manifest)
        self.assertTrue(report["valid"], json.dumps(report, indent=2))
        states = {node["id"]: {"state": "verified"} for node in graph["nodes"]}
        for node_id in ("author-implementation-plan", *manifest["implementation_nodes"]):
            states[node_id]["state"] = "planned"
        ready = ready_node_ids(graph, {"nodes": states})
        self.assertIn("implement-independent-slice", ready)
        self.assertNotIn("implement", ready)

    def test_direct_handoff_requires_a_declared_approval_target(self) -> None:
        graph, manifest = self.mixed_handoff()
        manifest["approval"]["targets"].remove("implement-independent-slice")
        self.assert_error_contains(
            "implementation node 'implement-independent-slice' is not an approval target",
            graph=graph,
            manifest=manifest,
        )

    def test_direct_handoff_requires_declared_specification_consumption(self) -> None:
        graph, manifest = self.mixed_handoff()
        manifest["documents"][0]["consumer_nodes"].remove("implement-independent-slice")
        self.assert_error_contains(
            "direct implementation node 'implement-independent-slice' is not a declared consumer of required document 'product-and-scope'",
            graph=graph,
            manifest=manifest,
        )

    def test_direct_handoff_rejects_an_undeclared_plan_dependency(self) -> None:
        graph, manifest = self.mixed_handoff()
        plan = manifest["implementation_plan"]
        direct_id = "implement-independent-slice"
        _add_input(_node(graph, direct_id), "Implementation plan", plan["artifact"])
        _add_consumes_edge(graph, plan["node"], direct_id, plan["artifact"])
        self.assert_error_contains(
            "direct implementation node 'implement-independent-slice' depends on the plan",
            graph=graph,
            manifest=manifest,
        )

    def test_direct_handoff_rejects_a_plan_dependency_through_shared_verification(self) -> None:
        graph, manifest = self.mixed_handoff()
        plan = manifest["implementation_plan"]
        direct_id = "implement-independent-slice"
        _remove_input(_node(graph, "verify-delivery"), "artifact:independent-slice")
        _remove_edge(graph, direct_id, "verify-delivery", "consumes", "artifact:independent-slice")
        for source, target in (
            (plan["node"], "verify-delivery"),
            ("verify-delivery", direct_id),
            (direct_id, "finish"),
        ):
            graph["edges"].append({
                "id": f"{source}-requires-{target}",
                "from": source,
                "to": target,
                "type": "requires",
                "condition": "The upstream step is complete.",
                "artifacts": [],
                "max_traversals": None,
                "exit_condition": None,
            })
        self.assertTrue(validate_graph(graph)["valid"])
        self.assert_error_contains(
            "direct implementation node 'implement-independent-slice' depends on the plan",
            graph=graph,
            manifest=manifest,
        )

    def test_direct_handoff_can_depend_on_a_planned_slice_result(self) -> None:
        graph, manifest = self.mixed_handoff()
        direct_id = "implement-independent-slice"
        result = _node(graph, "implement")["outputs"][0]["artifact"]
        _add_input(_node(graph, direct_id), "Planned slice result", result)
        _add_consumes_edge(graph, "implement", direct_id, result)
        report = self.validate(graph, manifest)
        self.assertTrue(report["valid"], json.dumps(report, indent=2))

    def test_plan_free_handoff_rejects_dependency_on_undeclared_builtin_plan(self) -> None:
        graph, manifest = self.mixed_handoff()
        plan = manifest.pop("implementation_plan")
        direct_id = "implement-independent-slice"
        manifest["implementation_nodes"] = [direct_id]
        (self.root / plan["path"]).unlink()

        # The existing planner belongs to a separate implementation slice.
        report = self.validate(graph, manifest)
        self.assertTrue(report["valid"], json.dumps(report, indent=2))

        _add_input(_node(graph, direct_id), "Implementation plan", plan["artifact"])
        _add_consumes_edge(graph, plan["node"], direct_id, plan["artifact"])
        self.assertTrue(validate_graph(graph)["valid"])
        self.assert_error_contains(
            "direct implementation node 'implement-independent-slice' depends on an undeclared implementation plan",
            graph=graph,
            manifest=manifest,
        )

    def test_plan_consumers_must_be_in_declared_implementation_inventory(self) -> None:
        graph, manifest = self.mixed_handoff()
        manifest["implementation_nodes"].remove("implement")
        self.assert_error_contains(
            "plan consumers must be declared in $.implementation_nodes",
            graph=graph,
            manifest=manifest,
        )

    def test_modern_unresolved_decision_rejects_undeclared_blocking_consumers(self) -> None:
        graph, manifest = self.mixed_handoff()
        _add_storage_decision(graph, manifest)
        self.assert_error_contains(
            "unresolved decision 'storage-backend' blocks undeclared node 'verify-specification-set'",
            graph=graph,
            manifest=manifest,
        )

    def test_rejects_missing_document_producer(self) -> None:
        manifest = copy.deepcopy(self.manifest)
        manifest["documents"][0]["producer_node"] = "missing-producer"
        self.assert_error_contains(
            "document 'product-and-scope' producer node 'missing-producer' does not exist",
            manifest=manifest,
        )

    def test_rejects_missing_document_consumer_lineage(self) -> None:
        graph = copy.deepcopy(self.graph)
        _remove_input(_node(graph, "author-implementation-plan"), "doc/product-spec.md")
        _remove_edge(
            graph,
            "define-product-and-scope",
            "author-implementation-plan",
            "consumes",
            "doc/product-spec.md",
        )
        self.assertTrue(validate_graph(graph)["valid"])
        self.assert_error_contains(
            "document 'product-and-scope' is not consumed by 'author-implementation-plan'",
            graph=graph,
        )

    def test_rejects_missing_verifier_consumption(self) -> None:
        graph = copy.deepcopy(self.graph)
        _remove_input(_node(graph, "verify-specification-set"), "doc/product-spec.md")
        _remove_edge(
            graph,
            "define-product-and-scope",
            "verify-specification-set",
            "consumes",
            "doc/product-spec.md",
        )
        self.assertTrue(validate_graph(graph)["valid"])
        self.assert_error_contains(
            "verification node 'verify-specification-set' does not consume document 'product-and-scope'",
            graph=graph,
        )

    def test_rejects_missing_approval_edge(self) -> None:
        graph = copy.deepcopy(self.graph)
        _remove_edge(
            graph,
            "approve-specification",
            "implement",
            "approves",
            "artifact:specification-approval",
        )
        self.assertTrue(validate_graph(graph)["valid"])
        self.assert_error_contains(
            "approval target 'implement' lacks an approves edge",
            graph=graph,
        )

    def test_rejects_missing_decision_affected_node_lineage(self) -> None:
        graph = copy.deepcopy(self.graph)
        manifest = copy.deepcopy(self.manifest)
        _add_storage_decision(graph, manifest)
        _remove_input(_node(graph, "implement"), "artifact:decision-storage-backend")
        _remove_edge(
            graph,
            "resolve-storage-backend",
            "implement",
            "consumes",
            "artifact:decision-storage-backend",
        )
        self.assertTrue(validate_graph(graph)["valid"])
        self.assert_error_contains(
            "decision 'storage-backend' is not consumed by affected node 'implement'",
            graph=graph,
            manifest=manifest,
        )

    def test_rejects_decision_missing_from_implementation_plan(self) -> None:
        graph = copy.deepcopy(self.graph)
        manifest = copy.deepcopy(self.manifest)
        _add_storage_decision(graph, manifest)
        manifest["decisions"][0]["status"] = "resolved"
        _remove_input(
            _node(graph, "author-implementation-plan"),
            "artifact:decision-storage-backend",
        )
        _remove_edge(
            graph,
            "resolve-storage-backend",
            "author-implementation-plan",
            "consumes",
            "artifact:decision-storage-backend",
        )
        self.assertTrue(validate_graph(graph)["valid"])
        self.assert_error_contains(
            "implementation plan does not consume decision 'storage-backend'",
            graph=graph,
            manifest=manifest,
        )

    def test_unresolved_decision_blocks_only_affected_nodes(self) -> None:
        graph = copy.deepcopy(self.graph)
        manifest = copy.deepcopy(self.manifest)
        _add_storage_decision(graph, manifest)
        for target in ("verify-specification-set", "author-implementation-plan"):
            _remove_input(
                _node(graph, target),
                "artifact:decision-storage-backend",
            )
            _remove_edge(
                graph,
                "resolve-storage-backend",
                target,
                "consumes",
                "artifact:decision-storage-backend",
            )

        graph_report = validate_graph(graph)
        specification_report = self.validate(graph, manifest)

        self.assertTrue(graph_report["valid"], json.dumps(graph_report, indent=2))
        self.assertTrue(
            specification_report["valid"],
            json.dumps(specification_report, indent=2),
        )

    def test_rejects_stale_graph_binding(self) -> None:
        manifest = copy.deepcopy(self.manifest)
        manifest["graph_version"] += 1
        self.assert_error_contains("manifest graph_version does not match graph", manifest=manifest)

    def test_rejects_missing_and_empty_document(self) -> None:
        product = self.root / "doc/product-spec.md"
        product.unlink()
        self.assert_error_contains("required document does not exist: doc/product-spec.md")
        product.write_text("", encoding="utf-8")
        self.assert_error_contains("required document is empty: doc/product-spec.md")

    def test_rejects_document_path_outside_project(self) -> None:
        manifest = copy.deepcopy(self.manifest)
        manifest["documents"][0]["path"] = "../outside.md"
        self.assert_error_contains(
            "document 'product-and-scope' path escapes project root",
            manifest=manifest,
        )

    def test_rejects_specification_root_outside_project(self) -> None:
        manifest = copy.deepcopy(self.manifest)
        manifest["specification_root"] = "../outside"
        self.assert_error_contains(
            "specification root path escapes project root",
            manifest=manifest,
        )

    def test_rejects_missing_specification_root(self) -> None:
        manifest = copy.deepcopy(self.manifest)
        manifest["specification_root"] = "missing-specifications"
        self.assert_error_contains(
            "specification root does not exist or is not a directory",
            manifest=manifest,
        )

    def test_rejects_empty_decision_record(self) -> None:
        graph = copy.deepcopy(self.graph)
        manifest = copy.deepcopy(self.manifest)
        _add_storage_decision(graph, manifest)
        source = self.root / "doc/decisions/storage-backend.md"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(" \n", encoding="utf-8")
        manifest["decisions"][0]["source_path"] = "doc/decisions/storage-backend.md"

        self.assert_error_contains(
            "decision 'storage-backend' source is empty",
            graph=graph,
            manifest=manifest,
        )

    def test_rejects_duplicate_decision_source_paths(self) -> None:
        graph = copy.deepcopy(self.graph)
        manifest = copy.deepcopy(self.manifest)
        _add_storage_decision(graph, manifest)
        duplicate = copy.deepcopy(manifest["decisions"][0])
        duplicate["id"] = "storage-backend-followup"
        duplicate["artifact"] = "artifact:decision-storage-backend-followup"
        manifest["decisions"].append(duplicate)

        self.assert_error_contains(
            "decision source path is duplicated: doc/open-questions.md",
            graph=graph,
            manifest=manifest,
        )


if __name__ == "__main__":
    unittest.main()
