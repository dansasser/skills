"""Validate specification documents and decision lineage against a development graph."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from graph_core import (
    GraphControlError,
    SCHEDULING_EDGE_TYPES,
    affected_node_ids,
    load_json,
)
from path_contract import (
    PathContractError,
    project_root_for_graph,
    resolve_project_path,
)
from validate_graph import SLUG, validate_graph


ROOT_FIELDS = {
    "schema_version",
    "graph_id",
    "graph_version",
    "context_version",
    "specification_root",
    "documents",
    "decisions",
    "verification_node",
    "approval",
}
IMPLEMENTATION_FIELDS = {"implementation_nodes", "implementation_plan"}
DOCUMENT_FIELDS = {
    "id",
    "kind",
    "path",
    "artifact",
    "producer_node",
    "consumer_nodes",
    "required",
}
DECISION_FIELDS = {
    "id",
    "status",
    "source_path",
    "decision_node",
    "artifact",
    "affected_nodes",
}
APPROVAL_FIELDS = {"node", "artifact", "targets"}
PLAN_FIELDS = {"node", "path", "artifact", "implementation_nodes"}
BUILTIN_PLAN_NODE = "author-implementation-plan"
BUILTIN_PLAN_ARTIFACT = "doc/implementation-plan.md"
DOCUMENT_KINDS = {
    "product",
    "skill",
    "architecture",
    "governance",
    "interface",
    "provider",
    "security",
    "constraints",
    "acceptance",
    "open-questions",
    "research",
    "other",
}
DECISION_STATES = {"open", "resolved", "deferred"}


def _nonempty(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _object_fields(
    value: Any,
    path: str,
    required: set[str],
    errors: list[str],
    *,
    optional: set[str] | None = None,
) -> bool:
    if not isinstance(value, dict):
        errors.append(f"{path}: must be an object")
        return False
    allowed = required | optional if optional else required
    for field in sorted(required - set(value)):
        errors.append(f"{path}.{field}: is required")
    for field in sorted(set(value) - allowed):
        errors.append(f"{path}.{field}: is not allowed")
    return required <= set(value) <= allowed


def _slug(value: Any, path: str, errors: list[str]) -> bool:
    if not _nonempty(value) or not SLUG.fullmatch(value):
        errors.append(f"{path}: must be a lowercase slug")
        return False
    return True


def _string(value: Any, path: str, errors: list[str]) -> bool:
    if not _nonempty(value):
        errors.append(f"{path}: must be a non-empty string")
        return False
    return True


def _string_list(
    value: Any,
    path: str,
    errors: list[str],
    *,
    minimum: int,
    slugs: bool,
) -> bool:
    if not isinstance(value, list) or len(value) < minimum:
        errors.append(f"{path}: must be an array with at least {minimum} item(s)")
        return False
    seen: set[str] = set()
    valid = True
    for index, item in enumerate(value):
        item_path = f"{path}[{index}]"
        item_valid = _slug(item, item_path, errors) if slugs else _string(item, item_path, errors)
        valid = item_valid and valid
        if isinstance(item, str):
            if item in seen:
                errors.append(f"{item_path}: duplicates {item!r}")
                valid = False
            seen.add(item)
    return valid


def _validate_manifest_shape(manifest: Any, errors: list[str]) -> bool:
    if not _object_fields(
        manifest, "$", ROOT_FIELDS, errors, optional=IMPLEMENTATION_FIELDS
    ):
        return False
    if not IMPLEMENTATION_FIELDS.intersection(manifest):
        errors.append("$: implementation_nodes or implementation_plan is required")
    if manifest["schema_version"] != 1:
        errors.append("$.schema_version: must equal 1")
    _slug(manifest["graph_id"], "$.graph_id", errors)
    if (
        not isinstance(manifest["graph_version"], int)
        or isinstance(manifest["graph_version"], bool)
        or manifest["graph_version"] < 1
    ):
        errors.append("$.graph_version: must be an integer >= 1")
    _string(manifest["context_version"], "$.context_version", errors)
    _string(manifest["specification_root"], "$.specification_root", errors)

    documents = manifest["documents"]
    if not isinstance(documents, list) or not documents:
        errors.append("$.documents: must be a non-empty array")
    else:
        for index, document in enumerate(documents):
            path = f"$.documents[{index}]"
            if not _object_fields(document, path, DOCUMENT_FIELDS, errors):
                continue
            _slug(document["id"], f"{path}.id", errors)
            if document["kind"] not in DOCUMENT_KINDS:
                errors.append(
                    f"{path}.kind: must be one of: {', '.join(sorted(DOCUMENT_KINDS))}"
                )
            _string(document["path"], f"{path}.path", errors)
            _string(document["artifact"], f"{path}.artifact", errors)
            _slug(document["producer_node"], f"{path}.producer_node", errors)
            _string_list(
                document["consumer_nodes"],
                f"{path}.consumer_nodes",
                errors,
                minimum=1,
                slugs=True,
            )
            if not isinstance(document["required"], bool):
                errors.append(f"{path}.required: must be a boolean")

    decisions = manifest["decisions"]
    if not isinstance(decisions, list):
        errors.append("$.decisions: must be an array")
    else:
        for index, decision in enumerate(decisions):
            path = f"$.decisions[{index}]"
            if not _object_fields(decision, path, DECISION_FIELDS, errors):
                continue
            _slug(decision["id"], f"{path}.id", errors)
            if decision["status"] not in DECISION_STATES:
                errors.append(
                    f"{path}.status: must be one of: {', '.join(sorted(DECISION_STATES))}"
                )
            _string(decision["source_path"], f"{path}.source_path", errors)
            _slug(decision["decision_node"], f"{path}.decision_node", errors)
            _string(decision["artifact"], f"{path}.artifact", errors)
            _string_list(
                decision["affected_nodes"],
                f"{path}.affected_nodes",
                errors,
                minimum=1,
                slugs=True,
            )

    _slug(manifest["verification_node"], "$.verification_node", errors)
    approval = manifest["approval"]
    if _object_fields(approval, "$.approval", APPROVAL_FIELDS, errors):
        _slug(approval["node"], "$.approval.node", errors)
        _string(approval["artifact"], "$.approval.artifact", errors)
        _string_list(
            approval["targets"],
            "$.approval.targets",
            errors,
            minimum=1,
            slugs=True,
        )
    valid_implementation_nodes = False
    if "implementation_nodes" in manifest:
        valid_implementation_nodes = _string_list(
            manifest["implementation_nodes"],
            "$.implementation_nodes",
            errors,
            minimum=1,
            slugs=True,
        )
    if "implementation_plan" in manifest:
        plan = manifest["implementation_plan"]
        if _object_fields(plan, "$.implementation_plan", PLAN_FIELDS, errors):
            _slug(plan["node"], "$.implementation_plan.node", errors)
            _string(plan["path"], "$.implementation_plan.path", errors)
            _string(plan["artifact"], "$.implementation_plan.artifact", errors)
            valid_consumers = _string_list(
                plan["implementation_nodes"],
                "$.implementation_plan.implementation_nodes",
                errors,
                minimum=1,
                slugs=True,
            )
            if valid_consumers and valid_implementation_nodes:
                undeclared = set(plan["implementation_nodes"]) - set(
                    manifest["implementation_nodes"]
                )
                if undeclared:
                    errors.append(
                        "$.implementation_plan.implementation_nodes: plan consumers must "
                        f"be declared in $.implementation_nodes: {', '.join(sorted(undeclared))}"
                    )
    return not errors


def _contained_path(
    root: Path,
    raw_path: str,
    label: str,
    errors: list[str],
) -> Path | None:
    try:
        return resolve_project_path(root, raw_path, label=f"{label} path")
    except PathContractError:
        errors.append(f"{label} path escapes project root: {raw_path}")
        return None


def _has_input(node: dict[str, Any], artifact: str) -> bool:
    return any(
        isinstance(item, dict)
        and item.get("artifact") == artifact
        and item.get("required") is True
        for item in node.get("inputs", [])
    )


def _has_output(node: dict[str, Any], artifact: str) -> bool:
    return any(
        isinstance(item, dict) and item.get("artifact") == artifact
        for item in node.get("outputs", [])
    )


def _has_edge(
    edges: list[dict[str, Any]],
    source: str,
    target: str,
    edge_type: str,
    artifact: str,
) -> bool:
    return any(
        edge.get("from") == source
        and edge.get("to") == target
        and edge.get("type") == edge_type
        and artifact in edge.get("artifacts", [])
        for edge in edges
    )


def _has_consumption(
    nodes: dict[str, dict[str, Any]],
    edges: list[dict[str, Any]],
    source: str,
    target: str,
    artifact: str,
) -> bool:
    node = nodes.get(target)
    return (
        node is not None
        and _has_input(node, artifact)
        and _has_edge(edges, source, target, "consumes", artifact)
    )


def validate_specifications(
    graph: dict[str, Any],
    manifest: dict[str, Any],
    project_root: str | Path,
) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    root = Path(project_root).resolve()
    if not root.is_dir():
        errors.append(f"project root does not exist or is not a directory: {root}")

    graph_report = validate_graph(graph)
    if not graph_report["valid"]:
        errors.extend(f"graph: {error}" for error in graph_report["errors"])

    if not _validate_manifest_shape(manifest, errors):
        return {
            "valid": False,
            "errors": errors,
            "warnings": warnings,
            "stats": {"documents": 0, "decisions": 0},
        }

    if manifest["graph_id"] != graph.get("graph_id"):
        errors.append("manifest graph_id does not match graph")
    if manifest["graph_version"] != graph.get("graph_version"):
        errors.append("manifest graph_version does not match graph")
    graph_context = graph.get("project", {}).get("context_version")
    if manifest["context_version"] != graph_context:
        errors.append("manifest context_version does not match graph")
    specification_root = _contained_path(
        root,
        manifest["specification_root"],
        "specification root",
        errors,
    )
    if specification_root is not None and not specification_root.is_dir():
        errors.append(
            "specification root does not exist or is not a directory: "
            f"{manifest['specification_root']}"
        )

    nodes = {
        node["id"]: node
        for node in graph.get("nodes", [])
        if isinstance(node, dict) and isinstance(node.get("id"), str)
    }
    edges = [edge for edge in graph.get("edges", []) if isinstance(edge, dict)]

    document_ids: set[str] = set()
    document_paths: set[str] = set()
    document_artifacts: set[str] = set()
    verification_id = manifest["verification_node"]
    verification = nodes.get(verification_id)
    if verification is None:
        errors.append(f"verification node '{verification_id}' does not exist")
    elif verification.get("type") != "verification":
        errors.append(f"verification node '{verification_id}' must have type verification")

    plan = manifest.get("implementation_plan")
    plan_id = plan["node"] if plan else None
    planned_nodes = plan["implementation_nodes"] if plan else []
    modern_manifest = "implementation_nodes" in manifest
    implementation_nodes = manifest.get("implementation_nodes", planned_nodes)
    direct_nodes = [node for node in implementation_nodes if node not in planned_nodes]
    if plan is not None:
        plan_node = nodes.get(plan_id)
        if plan_node is None:
            errors.append(f"implementation plan node '{plan_id}' does not exist")
        elif not _has_output(plan_node, plan["artifact"]):
            errors.append(
                f"implementation plan node '{plan_id}' does not produce {plan['artifact']!r}"
            )
        plan_path = _contained_path(root, plan["path"], "implementation plan", errors)
        if plan_path is not None:
            if not plan_path.is_file():
                errors.append(f"implementation plan does not exist: {plan['path']}")
            elif not plan_path.read_text(encoding="utf-8").strip():
                errors.append(f"implementation plan is empty: {plan['path']}")

    for document in manifest["documents"]:
        document_id = document["id"]
        if document_id in document_ids:
            errors.append(f"document id is duplicated: {document_id}")
        document_ids.add(document_id)
        if document["path"] in document_paths:
            errors.append(f"document path is duplicated: {document['path']}")
        document_paths.add(document["path"])
        if document["artifact"] in document_artifacts:
            errors.append(f"document artifact is duplicated: {document['artifact']}")
        document_artifacts.add(document["artifact"])

        path = _contained_path(
            root,
            document["path"],
            f"document '{document_id}'",
            errors,
        )
        if path is not None and document["required"]:
            if not path.is_file():
                errors.append(f"required document does not exist: {document['path']}")
            elif not path.read_text(encoding="utf-8").strip():
                errors.append(f"required document is empty: {document['path']}")

        producer_id = document["producer_node"]
        producer = nodes.get(producer_id)
        if producer is None:
            errors.append(
                f"document '{document_id}' producer node '{producer_id}' does not exist"
            )
            continue
        if not _has_output(producer, document["artifact"]):
            errors.append(
                f"document '{document_id}' producer '{producer_id}' does not produce "
                f"{document['artifact']!r}"
            )

        for consumer_id in document["consumer_nodes"]:
            if not _has_consumption(
                nodes,
                edges,
                producer_id,
                consumer_id,
                document["artifact"],
            ):
                if consumer_id == verification_id:
                    errors.append(
                        f"verification node '{verification_id}' does not consume "
                        f"document '{document_id}'"
                    )
                else:
                    errors.append(
                        f"document '{document_id}' is not consumed by '{consumer_id}'"
                    )

        if verification_id not in document["consumer_nodes"] or not _has_consumption(
            nodes,
            edges,
            producer_id,
            verification_id,
            document["artifact"],
        ):
            expected = (
                f"verification node '{verification_id}' does not consume "
                f"document '{document_id}'"
            )
            if expected not in errors:
                errors.append(expected)

        if document["required"]:
            if plan_id in nodes and not _has_consumption(
                nodes, edges, producer_id, plan_id, document["artifact"]
            ):
                errors.append(
                    f"implementation plan does not consume required document '{document_id}'"
                )
            for direct_id in direct_nodes:
                if direct_id not in document["consumer_nodes"]:
                    errors.append(
                        f"direct implementation node '{direct_id}' is not a declared "
                        f"consumer of required document '{document_id}'"
                    )

    decision_ids: set[str] = set()
    decision_artifacts: set[str] = set()
    decision_source_paths: set[str] = set()
    for decision in manifest["decisions"]:
        decision_id = decision["id"]
        if decision_id in decision_ids:
            errors.append(f"decision id is duplicated: {decision_id}")
        decision_ids.add(decision_id)
        if decision["artifact"] in decision_artifacts:
            errors.append(f"decision artifact is duplicated: {decision['artifact']}")
        decision_artifacts.add(decision["artifact"])
        if decision["source_path"] in decision_source_paths:
            errors.append(
                f"decision source path is duplicated: {decision['source_path']}"
            )
        decision_source_paths.add(decision["source_path"])
        source_path = _contained_path(
            root,
            decision["source_path"],
            f"decision '{decision_id}' source",
            errors,
        )
        if source_path is not None:
            if not source_path.is_file():
                errors.append(
                    f"decision '{decision_id}' source does not exist: {decision['source_path']}"
                )
            else:
                try:
                    if not source_path.read_text(encoding="utf-8").strip():
                        errors.append(f"decision '{decision_id}' source is empty")
                except OSError as exc:
                    errors.append(
                        f"decision '{decision_id}' source cannot be read: {exc}"
                    )

        decision_node_id = decision["decision_node"]
        decision_node = nodes.get(decision_node_id)
        if decision_node is None:
            errors.append(
                f"decision '{decision_id}' node '{decision_node_id}' does not exist"
            )
            continue
        if decision_node.get("type") != "decision":
            errors.append(
                f"decision '{decision_id}' node '{decision_node_id}' must have type decision"
            )
        if not _has_output(decision_node, decision["artifact"]):
            errors.append(
                f"decision '{decision_id}' node '{decision_node_id}' does not produce "
                f"{decision['artifact']!r}"
            )
        for affected_node in decision["affected_nodes"]:
            if not _has_consumption(
                nodes,
                edges,
                decision_node_id,
                affected_node,
                decision["artifact"],
            ):
                errors.append(
                    f"decision '{decision_id}' is not consumed by affected node "
                    f"'{affected_node}'"
                )
        if decision["status"] == "resolved":
            if not _has_consumption(
                nodes,
                edges,
                decision_node_id,
                verification_id,
                decision["artifact"],
            ):
                errors.append(
                    f"verification node '{verification_id}' does not consume decision "
                    f"'{decision_id}'"
                )
            if not modern_manifest and not _has_consumption(
                nodes,
                edges,
                decision_node_id,
                plan_id,
                decision["artifact"],
            ):
                errors.append(
                    f"implementation plan does not consume decision '{decision_id}'"
                )
        elif modern_manifest:
            affected_nodes = set(decision["affected_nodes"])
            for edge in edges:
                if (
                    edge.get("from") == decision_node_id
                    and edge.get("type") in SCHEDULING_EDGE_TYPES
                    and edge.get("to") not in affected_nodes
                ):
                    errors.append(
                        f"unresolved decision '{decision_id}' blocks undeclared node "
                        f"'{edge.get('to')}'"
                    )

    if not manifest["decisions"]:
        warnings.append(
            "manifest catalogs no material decisions; confirm the source conversation, "
            "specifications, plans, code, tests, and research contain none"
        )

    approval = manifest["approval"]
    approval_id = approval["node"]
    approval_node = nodes.get(approval_id)
    if modern_manifest:
        for node_id in implementation_nodes:
            if node_id not in approval["targets"]:
                errors.append(f"implementation node '{node_id}' is not an approval target")
        if plan_id is not None and plan_id not in approval["targets"]:
            errors.append(f"implementation plan node '{plan_id}' is not an approval target")
    if approval_node is None:
        errors.append(f"approval node '{approval_id}' does not exist")
    else:
        if approval_node.get("type") != "human_gate":
            errors.append(f"approval node '{approval_id}' must have type human_gate")
        if not _has_output(approval_node, approval["artifact"]):
            errors.append(
                f"approval node '{approval_id}' does not produce {approval['artifact']!r}"
            )
        verification_outputs = [
            output.get("artifact")
            for output in (verification or {}).get("outputs", [])
            if isinstance(output, dict) and isinstance(output.get("artifact"), str)
        ]
        if not verification_outputs or not any(
            _has_consumption(nodes, edges, verification_id, approval_id, artifact)
            for artifact in verification_outputs
        ):
            errors.append(
                f"approval node '{approval_id}' does not consume verification evidence "
                f"from '{verification_id}'"
            )
        for target in approval["targets"]:
            if not _has_consumption(
                nodes,
                edges,
                approval_id,
                target,
                approval["artifact"],
            ):
                errors.append(
                    f"approval target '{target}' does not consume approval artifact"
                )
            if not _has_edge(
                edges,
                approval_id,
                target,
                "approves",
                approval["artifact"],
            ):
                errors.append(f"approval target '{target}' lacks an approves edge")

    if plan is not None:
        for implementation_node in planned_nodes:
            if not _has_consumption(
                nodes, edges, plan_id, implementation_node, plan["artifact"]
            ):
                errors.append(
                    f"implementation node '{implementation_node}' does not consume "
                    "the implementation plan"
                )

    scheduler_plan_id = plan_id
    if plan is None and _has_output(nodes.get(BUILTIN_PLAN_NODE, {}), BUILTIN_PLAN_ARTIFACT):
        scheduler_plan_id = BUILTIN_PLAN_NODE
    if direct_nodes and scheduler_plan_id is not None:
        # A direct slice can require the result of planned work. Check for
        # paths through the plan that reach a direct slice before that work.
        bypass_graph = {
            "nodes": list(nodes.values()),
            "edges": [
                edge for edge in edges
                if edge.get("type") in SCHEDULING_EDGE_TYPES
                and edge.get("to") not in planned_nodes
            ],
        }
        blocked_by_plan = set(affected_node_ids(bypass_graph, {scheduler_plan_id}))
        plan_label = "the plan" if plan is not None else "an undeclared implementation plan"
        for direct_id in direct_nodes:
            if direct_id in blocked_by_plan:
                errors.append(
                    f"direct implementation node '{direct_id}' depends on {plan_label}"
                )

    return {
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "stats": {
            "documents": len(manifest["documents"]),
            "decisions": len(manifest["decisions"]),
        },
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("graph", type=Path)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--project-root", type=Path)
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        graph = load_json(args.graph.resolve())
        manifest = load_json(args.manifest.resolve())
    except GraphControlError as exc:
        print(f"Specification validation: FAIL\nERROR {exc}", file=sys.stderr)
        return 1
    try:
        project_root = project_root_for_graph(
            args.graph,
            graph,
            explicit_project_root=args.project_root,
        )
    except PathContractError as exc:
        print(f"Specification validation: FAIL\nERROR {exc}", file=sys.stderr)
        return 1
    report = validate_specifications(graph, manifest, project_root)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        marker = "PASS" if report["valid"] else "FAIL"
        print(f"Specification validation: {marker}")
        print(
            f"Documents: {report['stats']['documents']} | "
            f"Decisions: {report['stats']['decisions']} | "
            f"Errors: {len(report['errors'])} | "
            f"Warnings: {len(report['warnings'])}"
        )
        for error in report["errors"]:
            print(f"ERROR {error}")
        for warning in report["warnings"]:
            print(f"WARNING {warning}")
    return 0 if report["valid"] else 1


if __name__ == "__main__":
    sys.exit(main())
