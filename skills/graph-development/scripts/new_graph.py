"""Scaffold a validated project-specific development graph and Markdown view."""

from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path
from typing import Any

from graph_core import GraphControlError
from path_contract import PathContractError, resolve_project_path
from project_lifecycle import (
    LifecycleError,
    StaleLifecycleError,
    apply_project,
    preview_project,
)
from render_graph import render_markdown
from template_registry import load_template, project_files, template_ids
from validate_graph import validate_graph


TEMPLATES = template_ids()


def _artifact(node_id: str) -> str:
    return f"artifact:{node_id}"


def _title(node_id: str) -> str:
    return node_id.replace("-", " ").title()


def _node(
    node_id: str,
    node_type: str,
    inputs: list[str],
    *,
    writes: bool,
    destructive: bool,
) -> dict[str, Any]:
    executor_kind = "human" if node_type == "human_gate" else "agent"
    write_scope = ["project"] if writes or destructive else []
    if destructive:
        level = "destructive"
        rollback = f"Restore the pre-{node_id} snapshot and verify output-level behavior."
        description = f"{_title(node_id)} changes project state and may remove incompatible state."
    elif writes:
        level = "reversible"
        rollback = f"Restore files changed by {node_id} from version control."
        description = f"{_title(node_id)} writes bounded project artifacts."
    else:
        level = "none"
        rollback = "none"
        description = "Produces evidence without mutating project state."
    output = _artifact(node_id)
    return {
        "id": node_id,
        "type": node_type,
        "title": _title(node_id),
        "goal": f"Produce accepted, reviewable evidence for {_title(node_id).lower()}.",
        "state": "planned",
        "executor": {
            "kind": executor_kind,
            "name": f"{executor_kind} adapter",
            "instructions": f"Perform {_title(node_id).lower()} within declared permissions and record acceptance evidence.",
        },
        "inputs": [
            {"name": item.removeprefix("artifact:").replace("-", " "), "artifact": item, "required": True}
            for item in inputs
        ],
        "outputs": [{"name": _title(node_id), "artifact": output, "format": "evidence-record"}],
        "acceptance": [{
            "id": "evidence-accepted",
            "kind": "review" if node_type in {"decision", "human_gate"} else "artifact",
            "criterion": f"The {_title(node_id).lower()} result satisfies its declared goal and is independently inspectable.",
            "evidence": f"evidence:{node_id}",
        }],
        "permissions": {"read": ["project"], "write": write_scope, "external": [], "destructive": destructive},
        "execution": {
            "max_attempts": 2,
            "timeout_minutes": 30,
            "completion_condition": "Every acceptance criterion has durable evidence.",
        },
        "side_effects": {
            "level": level,
            "description": description,
            "rollback": rollback,
            "approval_required": destructive,
        },
        "resources": write_scope,
        "tags": ["scaffold", node_type.replace("_", "-")],
    }


def _edge(edge_id: str, source: str, target: str, edge_type: str, artifacts: list[str]) -> dict[str, Any]:
    return {
        "id": edge_id,
        "from": source,
        "to": target,
        "type": edge_type,
        "condition": "Upstream acceptance evidence is current and verified.",
        "artifacts": artifacts,
        "max_traversals": None,
        "exit_condition": None,
    }


def _configure_template_node(
    node: dict[str, Any],
    definition: dict[str, Any],
) -> None:
    artifact = definition["output"]["artifact"]
    output_format = definition["output"]["format"]
    node["outputs"] = [{
        "name": _title(node["id"]),
        "artifact": artifact,
        "format": output_format,
    }]
    write_scope = definition["write_scope"]
    node["permissions"]["write"] = write_scope
    node["resources"] = write_scope
    if write_scope and not definition["destructive"]:
        node["side_effects"] = {
            "level": "reversible",
            "description": f"{_title(node['id'])} writes bounded project artifacts.",
            "rollback": f"Restore files changed by {node['id']} from version control.",
            "approval_required": False,
        }


def build_specification_manifest(
    graph: dict[str, Any], *, include_implementation_plan: bool = True
) -> dict[str, Any]:
    specification = load_template(
        "specification-to-delivery",
        include_implementation_plan=include_implementation_plan,
    )["specification"]
    if not isinstance(specification, dict):
        raise ValueError("specification-to-delivery template lacks a manifest")
    manifest = copy.deepcopy(specification)
    manifest["graph_id"] = graph["graph_id"]
    manifest["graph_version"] = graph["graph_version"]
    manifest["context_version"] = graph["project"]["context_version"]
    return manifest


def build_graph(
    project_root: Path,
    graph_id: str,
    title: str,
    template: str,
    context_version: str,
    *,
    include_implementation_plan: bool = True,
) -> dict[str, Any]:
    definition = load_template(
        template, include_implementation_plan=include_implementation_plan
    )
    topology = definition["nodes"]
    outputs = {
        node["id"]: node["output"]["artifact"]
        for node in topology
    }
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    for item in topology:
        node_id = item["id"]
        predecessors = item["predecessors"]
        inputs = [outputs[predecessor] for predecessor in predecessors]
        node = _node(
            node_id,
            item["type"],
            inputs,
            writes=item["writes"],
            destructive=item["destructive"],
        )
        _configure_template_node(node, item)
        nodes.append(node)
        for predecessor in predecessors:
            artifact = outputs[predecessor]
            edges.append(_edge(
                f"{predecessor}-to-{node_id}",
                predecessor,
                node_id,
                "consumes",
                [artifact],
            ))

    for approval in definition["approvals"]:
        edges.append(_edge(
            f"{approval['from']}-approves-{approval['to']}",
            approval["from"],
            approval["to"],
            "approves",
            [approval["artifact"]],
        ))

    for feedback in definition["feedback"]:
        source = feedback["from"]
        target = feedback["to"]
        edges.append({
            "id": f"{source}-feedback-to-{target}",
            "from": source,
            "to": target,
            "type": "feedback",
            "condition": "Acceptance evidence identifies a correctable failure.",
            "artifacts": [outputs[source]],
            "max_traversals": feedback["max_traversals"],
            "exit_condition": feedback["exit_condition"],
        })
    return {
        "schema_version": 1,
        "graph_id": graph_id,
        "graph_version": 1,
        "title": title,
        "purpose": f"Coordinate and verify {title.lower()} for the {project_root.name} project.",
        "project": {
            "name": project_root.name,
            "root": ".",
            "instructions": [
                path.name
                for path in (
                    project_root / "AGENTS.md",
                    project_root / "CLAUDE.md",
                )
                if path.is_file()
            ],
            "context_version": context_version,
        },
        "status": "draft",
        "templates": [template],
        "entry_nodes": definition["entry_nodes"],
        "terminal_nodes": definition["terminal_nodes"],
        "nodes": nodes,
        "edges": edges,
        "assumptions": ["Project-specific commands and adapter bindings are added only after inspecting the project."],
        "risks": ["A scaffold is not execution proof; each node still requires project-specific evidence."],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--graph-id", required=True)
    parser.add_argument("--title", required=True)
    parser.add_argument("--template", choices=sorted(TEMPLATES), required=True)
    parser.add_argument("--context-version", required=True)
    parser.add_argument(
        "--no-implementation-plan",
        action="store_true",
        help="Scaffold specification-to-delivery with direct specification inputs to implementation.",
    )
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expected-base-digest")
    parser.add_argument("--expected-plan-digest")
    parser.add_argument("--actor")
    parser.add_argument("--authority")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    specification_template = args.template == "specification-to-delivery"
    if args.no_implementation_plan and not specification_template:
        parser.error("--no-implementation-plan requires --template specification-to-delivery")
    include_implementation_plan = not args.no_implementation_plan
    project_root = args.project_root.resolve()
    if not project_root.is_dir():
        print(f"Scaffold failed: project root is not a directory: {project_root}", file=sys.stderr)
        return 1
    output_dir = args.output_dir.resolve()
    try:
        output_dir.relative_to(project_root)
    except ValueError:
        print(
            f"Scaffold failed: output directory is outside project root: {output_dir}",
            file=sys.stderr,
        )
        return 1
    if output_dir != project_root:
        print(
            "Scaffold failed: output directory must equal project root so "
            "project.root='.' remains authoritative",
            file=sys.stderr,
        )
        return 1
    graph_path = output_dir / "development-graph.json"
    markdown_path = output_dir / "development-graph.md"
    manifest_path = output_dir / "specification-manifest.json"
    template_files = project_files(
        args.template, include_implementation_plan=include_implementation_plan
    )
    try:
        template_targets = {
            relative: resolve_project_path(
                project_root,
                relative,
                label="template project file",
            )
            for relative in template_files
        }
    except PathContractError as exc:
        print(f"Scaffold failed: {exc}", file=sys.stderr)
        return 1
    if specification_template:
        from specification_workflow import build_specification_scaffold

        try:
            graph, manifest, markdown = build_specification_scaffold(
                project_root,
                graph_id=args.graph_id,
                title=args.title,
                context_version=args.context_version,
                include_implementation_plan=include_implementation_plan,
            )
        except GraphControlError as exc:
            print(f"Scaffold failed: {exc}", file=sys.stderr)
            return 1
    else:
        graph = build_graph(
            project_root,
            args.graph_id,
            args.title,
            args.template,
            args.context_version,
        )
        report = validate_graph(graph)
        if not report["valid"]:
            print("Scaffold failed: generated graph is invalid", file=sys.stderr)
            for error in report["errors"]:
                print(f"ERROR {error}", file=sys.stderr)
            return 1
        markdown = render_markdown(graph, graph_path.name)
        parity = validate_graph(graph, markdown)
        if not parity["valid"]:
            print("Scaffold failed: generated Markdown does not match", file=sys.stderr)
            return 1
    graph["project"]["root"] = "."
    graph["project"]["instructions"] = [
        path.name
        for path in (output_dir / "AGENTS.md", output_dir / "CLAUDE.md")
        if path.is_file()
    ]
    markdown = render_markdown(graph, graph_path.name)
    parity = validate_graph(graph, markdown)
    if not parity["valid"]:
        print("Scaffold failed: generated graph or Markdown is invalid", file=sys.stderr)
        for error in parity["errors"]:
            print(f"ERROR {error}", file=sys.stderr)
        return 1
    artifacts = [
        {
            "path": graph_path.relative_to(project_root).as_posix(),
            "content": json.dumps(
                graph,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ).encode("utf-8")
            + b"\n",
            "mode": 0o600,
        },
        {
            "path": markdown_path.relative_to(project_root).as_posix(),
            "content": markdown.encode("utf-8"),
            "mode": 0o644,
        },
    ]
    if specification_template:
        artifacts.append(
            {
                "path": manifest_path.relative_to(project_root).as_posix(),
                "content": json.dumps(
                    manifest,
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                ).encode("utf-8") + b"\n",
                "mode": 0o600,
            }
        )
    for relative_path, payload in sorted(template_files.items()):
        artifacts.append(
            {
                "path": template_targets[relative_path]
                .relative_to(project_root)
                .as_posix(),
                "content": payload,
                "mode": 0o644,
            }
        )
    request = {
        "schema_version": 1,
        "operation": "bootstrap",
        "project_root": project_root,
        "artifacts": artifacts,
        "metadata": {
            "entrypoint": "new_graph",
            "template": args.template,
            "graph_id": args.graph_id,
            "nodes": len(graph["nodes"]),
            "edges": len(graph["edges"]),
        },
    }
    try:
        if not args.apply:
            plan = preview_project(request)
            print(json.dumps(plan.to_dict(), indent=2, sort_keys=True))
            return 0 if plan.valid else 1
        if (
            not args.expected_base_digest
            or not args.expected_plan_digest
            or not args.actor
            or not args.authority
        ):
            raise LifecycleError(
                "apply requires --expected-base-digest, --expected-plan-digest, "
                "--actor, and --authority"
            )
        receipt = apply_project(
            request,
            expected_base_digest=args.expected_base_digest,
            expected_plan_digest=args.expected_plan_digest,
            actor=args.actor,
            authority=args.authority,
        )
        print(json.dumps(receipt.to_dict(), indent=2, sort_keys=True))
        return 0 if receipt.applied else 1
    except (LifecycleError, StaleLifecycleError, OSError) as exc:
        print(f"Scaffold failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
