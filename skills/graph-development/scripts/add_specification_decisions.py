"""Add explicit open decisions and their affected consumers to a specification graph."""

from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path
from typing import Any

from graph_core import GraphControlError, atomic_write_batch, load_json
from path_contract import (
    PathContractError,
    project_root_for_graph,
    resolve_project_path,
)
from render_graph import render_markdown
from validate_graph import SLUG, validate_graph
from validate_specifications import _validate_manifest_shape


DECISION_SPEC_FIELDS = {
    "id",
    "title",
    "question",
    "status",
    "source_path",
    "affected_nodes",
}
SUPPORTED_STATUSES = {"open", "deferred"}
CATALOG_STATUSES = SUPPORTED_STATUSES | {"resolved"}
GENERIC_DECISION_NODE = "resolve-blocking-decisions"
GENERIC_DECISION_ARTIFACT = "artifact:blocking-decision-set"


def _contained_path(root: Path, raw_path: str, label: str) -> Path:
    try:
        return resolve_project_path(root, raw_path, label=label)
    except PathContractError as exc:
        raise GraphControlError(
            f"{label} escapes project root: {raw_path}"
        ) from exc


def _load_decision_specs(
    path: Path,
    *,
    allowed_statuses: set[str] = SUPPORTED_STATUSES,
) -> list[dict[str, Any]]:
    data = load_json(path)
    if set(data) != {"schema_version", "decisions"}:
        raise GraphControlError(
            "decision input must contain only schema_version and decisions"
        )
    if data["schema_version"] != 1:
        raise GraphControlError("decision input schema_version must equal 1")
    decisions = data["decisions"]
    if not isinstance(decisions, list) or not decisions:
        raise GraphControlError("decision input decisions must be a non-empty array")

    errors: list[str] = []
    seen_ids: set[str] = set()
    seen_paths: set[str] = set()
    for index, decision in enumerate(decisions):
        prefix = f"decisions[{index}]"
        if not isinstance(decision, dict):
            errors.append(f"{prefix} must be an object")
            continue
        missing = DECISION_SPEC_FIELDS - set(decision)
        extra = set(decision) - DECISION_SPEC_FIELDS
        if missing:
            errors.append(f"{prefix} missing fields: {', '.join(sorted(missing))}")
        if extra:
            errors.append(f"{prefix} unknown fields: {', '.join(sorted(extra))}")
        if missing or extra:
            continue
        decision_id = decision["id"]
        if not isinstance(decision_id, str) or not SLUG.fullmatch(decision_id):
            errors.append(f"{prefix}.id must be a lowercase slug")
        elif decision_id in seen_ids:
            errors.append(f"{prefix}.id duplicates {decision_id!r}")
        else:
            seen_ids.add(decision_id)
        for field in ("title", "question", "source_path"):
            value = decision[field]
            if not isinstance(value, str) or not value.strip():
                errors.append(f"{prefix}.{field} must be a non-empty string")
        if decision["status"] not in allowed_statuses:
            errors.append(
                f"{prefix}.status must be one of: {', '.join(sorted(allowed_statuses))}"
            )
        source_path = decision["source_path"]
        if isinstance(source_path, str):
            if source_path in seen_paths:
                errors.append(f"{prefix}.source_path duplicates {source_path!r}")
            seen_paths.add(source_path)
        affected = decision["affected_nodes"]
        if not isinstance(affected, list) or not affected:
            errors.append(f"{prefix}.affected_nodes must be a non-empty array")
        else:
            seen_nodes: set[str] = set()
            for node_index, node_id in enumerate(affected):
                node_path = f"{prefix}.affected_nodes[{node_index}]"
                if not isinstance(node_id, str) or not SLUG.fullmatch(node_id):
                    errors.append(f"{node_path} must be a lowercase slug")
                elif node_id in seen_nodes:
                    errors.append(f"{node_path} duplicates {node_id!r}")
                else:
                    seen_nodes.add(node_id)
    if errors:
        raise GraphControlError("; ".join(errors))
    return decisions


def _input(artifact: str, name: str | None = None) -> dict[str, Any]:
    return {
        "name": name or artifact.replace("artifact:", "").replace("decision:", ""),
        "artifact": artifact,
        "required": True,
    }


def _output(artifact: str, name: str, output_format: str) -> dict[str, Any]:
    return {"name": name, "artifact": artifact, "format": output_format}


def _edge(
    edge_id: str,
    source: str,
    target: str,
    edge_type: str,
    artifact: str,
) -> dict[str, Any]:
    return {
        "id": edge_id,
        "from": source,
        "to": target,
        "type": edge_type,
        "condition": "Upstream acceptance evidence is current and verified.",
        "artifacts": [artifact],
        "max_traversals": None,
        "exit_condition": None,
    }


def _has_input(node: dict[str, Any], artifact: str) -> bool:
    return any(
        isinstance(item, dict) and item.get("artifact") == artifact
        for item in node.get("inputs", [])
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


def _append_input(node: dict[str, Any], artifact: str, name: str) -> None:
    if not _has_input(node, artifact):
        node.setdefault("inputs", []).append(_input(artifact, name))


def _append_edge(
    edges: list[dict[str, Any]],
    edge_ids: set[str],
    edge_id: str,
    source: str,
    target: str,
    edge_type: str,
    artifact: str,
) -> None:
    if _has_edge(edges, source, target, edge_type, artifact):
        return
    if edge_id in edge_ids:
        raise GraphControlError(f"edge id collision: {edge_id}")
    edges.append(_edge(edge_id, source, target, edge_type, artifact))
    edge_ids.add(edge_id)


def _decision_node(
    decision: dict[str, Any],
    open_questions_artifact: str,
) -> dict[str, Any]:
    decision_id = decision["id"]
    node_id = f"resolve-{decision_id}"
    artifact = f"decision:{decision_id}"
    source_path = decision["source_path"]
    return {
        "id": node_id,
        "type": "decision",
        "title": decision["title"],
        "goal": (
            f"Resolve {decision['question']} from project evidence and accountable "
            "human judgment without inventing an answer."
        ),
        "state": "needs_human",
        "executor": {
            "kind": "human",
            "name": "accountable decision owner",
            "instructions": (
                f"Resolve {decision_id}, record alternatives, criteria, evidence, "
                "choice, consequences, and revisit trigger."
            ),
        },
        "inputs": [_input(open_questions_artifact, "open decision inventory")],
        "outputs": [_output(artifact, decision["title"], "decision-record")],
        "acceptance": [
            {
                "id": "decision-record-accepted",
                "kind": "review",
                "criterion": (
                    "The accountable owner recorded an evidence-backed choice, "
                    "consequences, and revisit trigger."
                ),
                "evidence": source_path,
            }
        ],
        "permissions": {
            "read": ["project", open_questions_artifact],
            "write": [source_path],
            "external": [],
            "destructive": False,
        },
        "execution": {
            "max_attempts": 1,
            "timeout_minutes": 30,
            "completion_condition": (
                "The decision record is complete and every acceptance criterion "
                "has durable evidence."
            ),
        },
        "side_effects": {
            "level": "reversible",
            "description": f"Records the {decision_id} decision.",
            "rollback": f"Restore {source_path} from version control.",
            "approval_required": False,
        },
        "resources": [source_path],
        "tags": ["specification", "decision", decision["status"]],
    }


def _implementation_node(
    node_id: str,
    plan_artifact: str | None,
    approval_artifact: str,
) -> dict[str, Any]:
    title = node_id.replace("-", " ").title()
    output_artifact = f"artifact:{node_id}"
    write_scope = f"implementation/{node_id}"
    inputs = [_input(approval_artifact, "specification approval")]
    if plan_artifact is not None:
        inputs.insert(0, _input(plan_artifact, "implementation plan"))
    handoff = "plan" if plan_artifact is not None else "specification"
    source_artifacts = (
        "approved specification, implementation plan, and current decision artifacts"
        if plan_artifact is not None
        else "approved specification and current decision artifacts"
    )
    return {
        "id": node_id,
        "type": "work",
        "title": title,
        "goal": f"Produce the approved {title.lower()} implementation artifact.",
        "state": "planned",
        "executor": {
            "kind": "agent",
            "name": "project implementation adapter",
            "instructions": f"Work only from the {source_artifacts}.",
        },
        "inputs": inputs,
        "outputs": [_output(output_artifact, title, "project-change")],
        "acceptance": [
            {
                "id": "implementation-evidence-accepted",
                "kind": "artifact",
                "criterion": (
                    f"The {title.lower()} output follows the approved {handoff} and has "
                    "independently inspectable evidence."
                ),
                "evidence": f"evidence:{node_id}",
            }
        ],
        "permissions": {
            "read": ["project", "doc"],
            "write": [write_scope],
            "external": [],
            "destructive": False,
        },
        "execution": {
            "max_attempts": 2,
            "timeout_minutes": 30,
            "completion_condition": "Every acceptance criterion has durable evidence.",
        },
        "side_effects": {
            "level": "reversible",
            "description": f"Writes bounded artifacts for {title.lower()}.",
            "rollback": f"Restore files changed by {node_id} from version control.",
            "approval_required": False,
        },
        "resources": [write_scope],
        "tags": ["specification-consumer", "implementation"],
    }


def _decision_record(decision: dict[str, Any]) -> bytes:
    affected = "\n".join(f"- `{node_id}`" for node_id in decision["affected_nodes"])
    text = (
        f"# {decision['title']}\n\n"
        f"- Decision ID: `{decision['id']}`\n"
        f"- Status: `{decision['status']}`\n"
        f"- Question: {decision['question']}\n\n"
        "## Affected graph nodes\n\n"
        f"{affected}\n\n"
        "## Resolution required\n\n"
        "Record alternatives, decision criteria, project evidence, the accountable "
        "owner's choice, consequences, and a revisit trigger. This scaffold does "
        "not claim that the decision has been resolved.\n"
    )
    return text.encode("utf-8")


def _build_specification_decision_candidates(
    graph: dict[str, Any],
    manifest: dict[str, Any],
    decisions: list[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    graph_report = validate_graph(graph)
    if not graph_report["valid"]:
        raise GraphControlError(
            "input graph is invalid: " + "; ".join(graph_report["errors"])
        )
    shape_errors: list[str] = []
    if not _validate_manifest_shape(manifest, shape_errors):
        raise GraphControlError(
            "input specification manifest is invalid: " + "; ".join(shape_errors)
        )
    if manifest["graph_id"] != graph["graph_id"]:
        raise GraphControlError("manifest graph_id does not match graph")
    if manifest["graph_version"] != graph["graph_version"]:
        raise GraphControlError("manifest graph_version does not match graph")
    if manifest["context_version"] != graph["project"]["context_version"]:
        raise GraphControlError("manifest context_version does not match graph")
    if "specification-to-delivery" not in graph.get("templates", []):
        raise GraphControlError(
            "decision helper requires a specification-to-delivery graph"
        )

    candidate = copy.deepcopy(graph)
    candidate_manifest = copy.deepcopy(manifest)
    nodes = {
        node["id"]: node
        for node in candidate["nodes"]
        if isinstance(node, dict) and isinstance(node.get("id"), str)
    }
    edges = candidate["edges"]
    edge_ids = {
        edge["id"]
        for edge in edges
        if isinstance(edge, dict) and isinstance(edge.get("id"), str)
    }
    existing_decisions = {
        decision["id"]
        for decision in candidate_manifest["decisions"]
        if isinstance(decision, dict) and isinstance(decision.get("id"), str)
    }
    for decision in decisions:
        if decision["id"] in existing_decisions:
            raise GraphControlError(f"decision already exists: {decision['id']}")
        node_id = f"resolve-{decision['id']}"
        if node_id in nodes:
            raise GraphControlError(f"decision node already exists: {node_id}")

    open_document = next(
        (
            document
            for document in candidate_manifest["documents"]
            if document["kind"] == "open-questions"
        ),
        None,
    )
    if open_document is None:
        raise GraphControlError("manifest has no open-questions document")
    verification_id = candidate_manifest["verification_node"]
    plan = candidate_manifest.get("implementation_plan")
    legacy = "implementation_nodes" not in candidate_manifest
    approval = candidate_manifest["approval"]
    approval_id = approval["node"]
    approval_artifact = approval["artifact"]
    verify_delivery_id = "verify-delivery"
    required_nodes = [
        open_document["producer_node"],
        verification_id,
        approval_id,
        verify_delivery_id,
    ]
    if plan is not None:
        required_nodes.append(plan["node"])
    for required_node in required_nodes:
        if required_node not in nodes:
            raise GraphControlError(
                f"specification graph lacks required node: {required_node}"
            )

    if GENERIC_DECISION_NODE in nodes:
        candidate["nodes"] = [
            node
            for node in candidate["nodes"]
            if node.get("id") != GENERIC_DECISION_NODE
        ]
        candidate["edges"] = [
            edge
            for edge in candidate["edges"]
            if edge.get("from") != GENERIC_DECISION_NODE
            and edge.get("to") != GENERIC_DECISION_NODE
        ]
        for node in candidate["nodes"]:
            node["inputs"] = [
                item
                for item in node.get("inputs", [])
                if item.get("artifact") != GENERIC_DECISION_ARTIFACT
            ]
        open_document["consumer_nodes"] = [
            node_id
            for node_id in open_document["consumer_nodes"]
            if node_id != GENERIC_DECISION_NODE
        ]
        nodes.pop(GENERIC_DECISION_NODE)
        edges = candidate["edges"]
        edge_ids = {edge["id"] for edge in edges}

    for decision in decisions:
        decision_id = decision["id"]
        decision_node_id = f"resolve-{decision_id}"
        decision_artifact = f"decision:{decision_id}"
        node = _decision_node(decision, open_document["artifact"])
        candidate["nodes"].append(node)
        nodes[decision_node_id] = node
        if decision_node_id not in open_document["consumer_nodes"]:
            open_document["consumer_nodes"].append(decision_node_id)
        _append_edge(
            edges,
            edge_ids,
            f"{open_document['producer_node']}-to-{decision_node_id}",
            open_document["producer_node"],
            decision_node_id,
            "consumes",
            open_document["artifact"],
        )

        consumers = list(decision["affected_nodes"])
        if legacy and plan is not None:
            consumers = list(dict.fromkeys((verification_id, plan["node"], *consumers)))

        for affected_id in consumers:
            if affected_id not in nodes:
                affected = _implementation_node(
                    affected_id,
                    plan["artifact"] if legacy and plan is not None else None,
                    approval_artifact,
                )
                candidate["nodes"].append(affected)
                nodes[affected_id] = affected
                if legacy and plan is not None:
                    if affected_id not in plan["implementation_nodes"]:
                        plan["implementation_nodes"].append(affected_id)
                    _append_edge(
                        edges,
                        edge_ids,
                        f"{plan['node']}-to-{affected_id}",
                        plan["node"],
                        affected_id,
                        "consumes",
                        plan["artifact"],
                    )
                else:
                    implementation_nodes = candidate_manifest["implementation_nodes"]
                    if affected_id not in implementation_nodes:
                        implementation_nodes.append(affected_id)
                    for document in candidate_manifest["documents"]:
                        if not document["required"]:
                            continue
                        if affected_id not in document["consumer_nodes"]:
                            document["consumer_nodes"].append(affected_id)
                        _append_input(affected, document["artifact"], document["id"])
                        _append_edge(
                            edges,
                            edge_ids,
                            f"{document['id']}-spec-to-{affected_id}",
                            document["producer_node"],
                            affected_id,
                            "consumes",
                            document["artifact"],
                        )
                if affected_id not in approval["targets"]:
                    approval["targets"].append(affected_id)
                _append_edge(
                    edges,
                    edge_ids,
                    f"{approval_id}-to-{affected_id}",
                    approval_id,
                    affected_id,
                    "consumes",
                    approval_artifact,
                )
                _append_edge(
                    edges,
                    edge_ids,
                    f"{approval_id}-approves-{affected_id}",
                    approval_id,
                    affected_id,
                    "approves",
                    approval_artifact,
                )
                affected_output = affected["outputs"][0]["artifact"]
                _append_input(
                    nodes[verify_delivery_id],
                    affected_output,
                    f"{affected_id} output",
                )
                _append_edge(
                    edges,
                    edge_ids,
                    f"{affected_id}-to-{verify_delivery_id}",
                    affected_id,
                    verify_delivery_id,
                    "consumes",
                    affected_output,
                )
            _append_input(
                nodes[affected_id],
                decision_artifact,
                f"{decision_id} decision",
            )
            _append_edge(
                edges,
                edge_ids,
                f"{decision_node_id}-to-{affected_id}",
                decision_node_id,
                affected_id,
                "consumes",
                decision_artifact,
            )

        candidate_manifest["decisions"].append(
            {
                "id": decision_id,
                "status": decision["status"],
                "source_path": decision["source_path"],
                "decision_node": decision_node_id,
                "artifact": decision_artifact,
                "affected_nodes": decision["affected_nodes"],
            }
        )
        existing_decisions.add(decision_id)

    candidate["graph_version"] += 1
    candidate_manifest["graph_version"] = candidate["graph_version"]
    report = validate_graph(candidate)
    if not report["valid"]:
        raise GraphControlError(
            "candidate graph is invalid: " + "; ".join(report["errors"])
        )
    final_shape_errors: list[str] = []
    if not _validate_manifest_shape(candidate_manifest, final_shape_errors):
        raise GraphControlError(
            "candidate specification manifest is invalid: "
            + "; ".join(final_shape_errors)
        )
    return candidate, candidate_manifest


def add_specification_decisions(
    graph: dict[str, Any],
    manifest: dict[str, Any],
    decisions: list[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Compatibility adapter for the specification-workflow deep module."""

    from specification_workflow import apply_specification_decisions

    return apply_specification_decisions(graph, manifest, decisions)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("graph", type=Path)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("decisions", type=Path)
    parser.add_argument("--project-root", type=Path)
    parser.add_argument("--markdown", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    graph_path = args.graph.resolve()
    manifest_path = args.manifest.resolve()
    try:
        graph = load_json(graph_path)
        manifest = load_json(manifest_path)
        decisions = _load_decision_specs(args.decisions.resolve())
        project_root = project_root_for_graph(
            graph_path,
            graph,
            explicit_project_root=args.project_root,
        )
        if not project_root.is_dir():
            raise GraphControlError(
                f"project root does not exist or is not a directory: {project_root}"
            )
        markdown_path = (
            args.markdown.resolve()
            if args.markdown is not None
            else graph_path.with_suffix(".md")
        )
        for target, label in (
            (graph_path, "graph"),
            (manifest_path, "manifest"),
            (markdown_path, "Markdown"),
        ):
            try:
                target.relative_to(project_root)
            except ValueError as exc:
                raise GraphControlError(
                    f"{label} path is outside project root: {target}"
                ) from exc

        record_paths: list[tuple[Path, bytes]] = []
        for decision in decisions:
            record_path = _contained_path(
                project_root,
                decision["source_path"],
                f"decision {decision['id']} source",
            )
            if record_path.exists():
                if not record_path.is_file() or not record_path.read_text(
                    encoding="utf-8"
                ).strip():
                    raise GraphControlError(
                        f"decision record exists but is not a nonempty file: {record_path}"
                    )
            else:
                record_paths.append((record_path, _decision_record(decision)))

        candidate, candidate_manifest = add_specification_decisions(
            graph,
            manifest,
            decisions,
        )
        markdown = render_markdown(candidate, graph_path.name)
        parity = validate_graph(candidate, markdown)
        if not parity["valid"]:
            raise GraphControlError(
                "candidate Markdown parity failed: " + "; ".join(parity["errors"])
            )

        writes = [(record_path, payload, 0o644) for record_path, payload in record_paths]
        writes.extend(
            [
                (
                    graph_path,
                    json.dumps(
                        candidate,
                        ensure_ascii=False,
                        indent=2,
                        sort_keys=True,
                    ).encode("utf-8")
                    + b"\n",
                    0o600,
                ),
                (
                    manifest_path,
                    json.dumps(
                        candidate_manifest,
                        ensure_ascii=False,
                        indent=2,
                        sort_keys=True,
                    ).encode("utf-8")
                    + b"\n",
                    0o600,
                ),
                (markdown_path, markdown.encode("utf-8"), 0o644),
            ]
        )
        atomic_write_batch(writes)
    except (GraphControlError, OSError, UnicodeError) as exc:
        print(f"Add specification decisions: FAIL\nERROR {exc}", file=sys.stderr)
        return 1

    decision_ids = ", ".join(decision["id"] for decision in decisions)
    print(f"Added specification decisions: {decision_ids}")
    print(f"Graph version: {candidate['graph_version']}")
    print("Graph validation: PASS")
    print("Markdown parity: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
