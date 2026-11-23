"""Render a validated development-graph JSON document as deterministic Markdown."""

from __future__ import annotations

import argparse
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

from validate_graph import canonical_checksum, validate_graph


def _cell(value: Any) -> str:
    text = str(value).replace("|", "\\|").replace("\n", " ")
    return text if text else "—"


def _join(values: list[Any]) -> str:
    return ", ".join(str(value) for value in values) if values else "—"


def _mermaid_id(node_id: str) -> str:
    return "n_" + re.sub(r"[^A-Za-z0-9_]", "_", node_id)


def _mermaid_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', "'").replace("\n", " ")


def render_markdown(data: dict[str, Any], source_name: str = "development-graph.json") -> str:
    checksum = canonical_checksum(data)
    nodes = data["nodes"]
    edges = data["edges"]
    lines: list[str] = [
        f"<!-- development-graph-sha256: {checksum} -->",
        "<!-- Generated from canonical JSON. Do not edit by hand. -->",
        f"# {data['title']}",
        "",
        data["purpose"],
        "",
        "## Graph metadata",
        "",
        "| Field | Value |",
        "|---|---|",
        f"| Graph ID | `{_cell(data['graph_id'])}` |",
        f"| Graph version | `{data['graph_version']}` |",
        f"| Schema version | `{data['schema_version']}` |",
        f"| Status | `{_cell(data['status'])}` |",
        f"| Project | {_cell(data['project']['name'])} |",
        f"| Project root | `{_cell(data['project']['root'])}` |",
        f"| Context version | `{_cell(data['project']['context_version'])}` |",
        f"| Templates | {_cell(_join(data['templates']))} |",
        f"| Entry nodes | {_cell(_join(data['entry_nodes']))} |",
        f"| Terminal nodes | {_cell(_join(data['terminal_nodes']))} |",
        f"| Canonical checksum | `{checksum}` |",
        "",
        "## Flow",
        "",
        "```mermaid",
        "flowchart TD",
    ]

    shape = {
        "decision": ("{", "}"),
        "human_gate": ("{{", "}}"),
        "verification": ("([", "])") ,
        "observation": ("([", "])") ,
        "operation": ("[[", "]]"),
        "work": ("[", "]"),
    }
    for node in nodes:
        left, right = shape[node["type"]]
        label = _mermaid_label(f"{node['title']}\\n({node['type']} / {node['state']})")
        lines.append(f"    {_mermaid_id(node['id'])}{left}\"{label}\"{right}")

    for edge in edges:
        source = _mermaid_id(edge["from"])
        target = _mermaid_id(edge["to"])
        label = _mermaid_label(edge["type"])
        if edge["type"] == "feedback":
            lines.append(f"    {source} -. \"{label} <= {edge['max_traversals']}\" .-> {target}")
        elif edge["type"] in {"invalidates", "conflicts"}:
            lines.append(f"    {source} -. \"{label}\" .-> {target}")
        else:
            lines.append(f"    {source} -- \"{label}\" --> {target}")
    lines.extend(["```", ""])

    lines.extend([
        "## Nodes",
        "",
        "| ID | Type | State | Executor | Goal | Outputs |",
        "|---|---|---|---|---|---|",
    ])
    for node in nodes:
        outputs = [item["artifact"] for item in node["outputs"]]
        lines.append(
            f"| `{_cell(node['id'])}` | `{_cell(node['type'])}` | `{_cell(node['state'])}` | "
            f"{_cell(node['executor']['kind'])}: {_cell(node['executor']['name'])} | "
            f"{_cell(node['goal'])} | {_cell(_join(outputs))} |"
        )
    lines.append("")

    lines.extend([
        "## Edges",
        "",
        "| ID | From | Type | To | Condition | Artifacts | Bound / exit |",
        "|---|---|---|---|---|---|---|",
    ])
    for edge in edges:
        bound = "—"
        if edge["type"] == "feedback":
            bound = f"max {edge['max_traversals']}; {edge['exit_condition']}"
        lines.append(
            f"| `{_cell(edge['id'])}` | `{_cell(edge['from'])}` | `{_cell(edge['type'])}` | "
            f"`{_cell(edge['to'])}` | {_cell(edge['condition'])} | {_cell(_join(edge['artifacts']))} | {_cell(bound)} |"
        )
    lines.append("")

    lines.extend(["## Node contracts", ""])
    for node in nodes:
        permissions = node["permissions"]
        effects = node["side_effects"]
        lines.extend([
            f"### `{node['id']}` — {node['title']}",
            "",
            f"- Goal: {node['goal']}",
            f"- Executor instructions: {node['executor']['instructions']}",
            f"- Inputs: {_join([item['artifact'] for item in node['inputs']])}",
            f"- Resources: {_join(node['resources'])}",
            f"- Permissions: read [{_join(permissions['read'])}]; write [{_join(permissions['write'])}]; external [{_join(permissions['external'])}]; destructive `{str(permissions['destructive']).lower()}`",
            f"- Execution: max `{node['execution']['max_attempts']}` attempt(s), `{node['execution']['timeout_minutes']}` minute(s); {node['execution']['completion_condition']}",
            f"- Side effects: `{effects['level']}` — {effects['description']}",
            f"- Rollback: {effects['rollback']}",
            f"- Approval required: `{str(effects['approval_required']).lower()}`",
        ])
        context_control = node.get("context_control")
        if isinstance(context_control, dict):
            lines.append(
                "- Context control: "
                f"V{context_control['version']} `{context_control['mode']}` via "
                f"`{context_control['adapter']}` / `{context_control['tokenizer']}`; "
                f"plan `{context_control['plan_path']}`; retention "
                f"`{context_control['retention']}`"
            )
        lines.append("- Acceptance:")
        for acceptance in node["acceptance"]:
            lines.append(
                f"  - `{acceptance['id']}` ({acceptance['kind']}): {acceptance['criterion']} Evidence: `{acceptance['evidence']}`"
            )
        lines.append("")

    lines.extend(["## Assumptions", ""])
    if data["assumptions"]:
        lines.extend(f"- {item}" for item in data["assumptions"])
    else:
        lines.append("- None declared.")
    lines.extend(["", "## Risks", ""])
    if data["risks"]:
        lines.extend(f"- {item}" for item in data["risks"])
    else:
        lines.append("- None declared.")
    lines.extend(["", "## Provenance and validation", ""])
    if data["project"]["instructions"]:
        lines.append(f"Project instructions: {_join(data['project']['instructions'])}")
    else:
        lines.append("Project instructions: none declared.")
    lines.extend([
        "",
        f"Canonical source: `{source_name}`",
        "",
        "Run these commands from the `graph-development` skill root after setting `PROJECT_ROOT` to this project's root:",
        "",
        "```bash",
        ': "${PROJECT_ROOT:?set PROJECT_ROOT to this project directory}"',
        f'python3 scripts/validate_graph.py "$PROJECT_ROOT/{source_name}"',
        f'python3 scripts/render_graph.py "$PROJECT_ROOT/{source_name}" --output "$PROJECT_ROOT/development-graph.md"',
        f'python3 scripts/validate_graph.py "$PROJECT_ROOT/{source_name}" --markdown "$PROJECT_ROOT/development-graph.md"',
        "```",
        "",
    ])
    return "\n".join(lines)


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent, text=True)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("graph", type=Path, help="validated development-graph JSON file")
    parser.add_argument("--output", type=Path, required=True, help="Markdown destination")
    args = parser.parse_args(argv)

    try:
        import json

        with args.graph.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Render failed: {exc}", file=sys.stderr)
        return 1

    report = validate_graph(data)
    if not report["valid"]:
        print("Render failed: canonical graph is invalid", file=sys.stderr)
        for error in report["errors"]:
            print(f"ERROR {error}", file=sys.stderr)
        return 1

    content = render_markdown(data, args.graph.name)
    try:
        _atomic_write(args.output, content)
    except OSError as exc:
        print(f"Render failed: {exc}", file=sys.stderr)
        return 1
    print(f"Rendered {args.output}")
    print(f"Canonical SHA-256: {canonical_checksum(data)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
