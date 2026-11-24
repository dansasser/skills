"""Validate a canonical project development graph using only the Python stdlib."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, Iterable

from path_contract import PathContractError, validate_relative_path


SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
MARKER = re.compile(r"<!-- development-graph-sha256: ([0-9a-f]{64}) -->")
SCHEDULING_TYPES = {"requires", "consumes", "validates", "approves", "blocks"}
FLOW_TYPES = SCHEDULING_TYPES | {"feedback"}
NODE_TYPES = {"work", "decision", "verification", "human_gate", "operation", "observation"}
NODE_STATES = {
    "planned", "ready", "blocked", "running", "verifying", "verified",
    "invalidated", "failed", "needs_human", "cancelled",
}
EXECUTOR_KINDS = {"deterministic", "agent", "human", "hybrid"}
ACCEPTANCE_KINDS = {"artifact", "test", "schema", "review", "policy", "probe", "manual"}
SIDE_EFFECT_LEVELS = {"none", "reversible", "destructive"}
EDGE_TYPES = SCHEDULING_TYPES | {"invalidates", "feedback", "conflicts"}
ENVIRONMENT_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
ROOT_FIELDS = {
    "schema_version", "graph_id", "graph_version", "title", "purpose", "project",
    "status", "templates", "entry_nodes", "terminal_nodes", "nodes", "edges",
    "assumptions", "risks",
}


def canonical_checksum(data: Any) -> str:
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_nonempty(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


class Validation:
    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warnings: list[str] = []

    def error(self, path: str, message: str) -> None:
        self.errors.append(f"{path}: {message}")

    def warning(self, path: str, message: str) -> None:
        self.warnings.append(f"{path}: {message}")

    def obj(self, value: Any, path: str, required: Iterable[str], allowed: Iterable[str]) -> bool:
        if not isinstance(value, dict):
            self.error(path, "must be an object")
            return False
        required_set = set(required)
        allowed_set = set(allowed)
        for key in sorted(required_set - value.keys()):
            self.error(f"{path}.{key}", "is required")
        for key in sorted(value.keys() - allowed_set):
            self.error(f"{path}.{key}", "is not allowed")
        return True

    def string(self, value: Any, path: str, *, slug: bool = False) -> bool:
        if not _is_nonempty(value):
            self.error(path, "must be a non-empty string")
            return False
        if slug and not SLUG.fullmatch(value):
            self.error(path, "must be a lowercase slug")
            return False
        return True

    def integer(self, value: Any, path: str, minimum: int = 1) -> bool:
        if not _is_int(value) or value < minimum:
            self.error(path, f"must be an integer >= {minimum}")
            return False
        return True

    def boolean(self, value: Any, path: str) -> bool:
        if not isinstance(value, bool):
            self.error(path, "must be a boolean")
            return False
        return True

    def enum(self, value: Any, path: str, allowed: set[str]) -> bool:
        if not isinstance(value, str) or value not in allowed:
            self.error(path, f"must be one of: {', '.join(sorted(allowed))}")
            return False
        return True

    def array(self, value: Any, path: str, *, minimum: int = 0) -> bool:
        if not isinstance(value, list):
            self.error(path, "must be an array")
            return False
        if len(value) < minimum:
            self.error(path, f"must contain at least {minimum} item(s)")
            return False
        return True

    def string_array(self, value: Any, path: str, *, minimum: int = 0, slug: bool = False) -> bool:
        if not self.array(value, path, minimum=minimum):
            return False
        seen: set[str] = set()
        for index, item in enumerate(value):
            item_path = f"{path}[{index}]"
            if self.string(item, item_path, slug=slug):
                if item in seen:
                    self.error(item_path, f"duplicates {item!r}")
                seen.add(item)
        return True


def _validate_shape(data: Any, v: Validation) -> None:
    if not v.obj(data, "$", ROOT_FIELDS, ROOT_FIELDS):
        return

    if data.get("schema_version") != 1:
        v.error("$.schema_version", "must equal 1")
    v.string(data.get("graph_id"), "$.graph_id", slug=True)
    v.integer(data.get("graph_version"), "$.graph_version")
    v.string(data.get("title"), "$.title")
    v.string(data.get("purpose"), "$.purpose")
    v.enum(data.get("status"), "$.status", {"draft", "validated", "approved", "archived"})
    v.string_array(data.get("templates"), "$.templates", minimum=1, slug=True)
    v.string_array(data.get("entry_nodes"), "$.entry_nodes", minimum=1, slug=True)
    v.string_array(data.get("terminal_nodes"), "$.terminal_nodes", minimum=1, slug=True)
    v.string_array(data.get("assumptions"), "$.assumptions")
    v.string_array(data.get("risks"), "$.risks")

    project = data.get("project")
    project_fields = {"name", "root", "instructions", "context_version"}
    if v.obj(project, "$.project", project_fields, project_fields):
        v.string(project.get("name"), "$.project.name")
        if v.string(project.get("root"), "$.project.root"):
            _validate_relative_path(project["root"], "$.project.root", v)
        if v.string_array(project.get("instructions"), "$.project.instructions"):
            for index, instruction in enumerate(project["instructions"]):
                _validate_relative_path(
                    instruction,
                    f"$.project.instructions[{index}]",
                    v,
                )
        v.string(project.get("context_version"), "$.project.context_version")

    nodes = data.get("nodes")
    if v.array(nodes, "$.nodes", minimum=1):
        for index, node in enumerate(nodes):
            _validate_node(node, f"$.nodes[{index}]", v)

    edges = data.get("edges")
    if v.array(edges, "$.edges"):
        for index, edge in enumerate(edges):
            _validate_edge(edge, f"$.edges[{index}]", v)


def _validate_node(node: Any, path: str, v: Validation) -> None:
    required_fields = {
        "id", "type", "title", "goal", "state", "executor", "inputs", "outputs",
        "acceptance", "permissions", "execution", "side_effects", "resources",
        "tags",
    }
    allowed_fields = required_fields | {"context_control"}
    if not v.obj(node, path, required_fields, allowed_fields):
        return
    v.string(node.get("id"), f"{path}.id", slug=True)
    v.enum(node.get("type"), f"{path}.type", NODE_TYPES)
    v.string(node.get("title"), f"{path}.title")
    v.string(node.get("goal"), f"{path}.goal")
    v.enum(node.get("state"), f"{path}.state", NODE_STATES)

    executor = node.get("executor")
    executor_fields = {"kind", "name", "instructions"}
    if v.obj(executor, f"{path}.executor", executor_fields, executor_fields):
        v.enum(executor.get("kind"), f"{path}.executor.kind", EXECUTOR_KINDS)
        v.string(executor.get("name"), f"{path}.executor.name")
        v.string(executor.get("instructions"), f"{path}.executor.instructions")

    inputs = node.get("inputs")
    if v.array(inputs, f"{path}.inputs"):
        for index, item in enumerate(inputs):
            item_path = f"{path}.inputs[{index}]"
            fields = {"name", "artifact", "required"}
            if v.obj(item, item_path, fields, fields):
                v.string(item.get("name"), f"{item_path}.name")
                v.string(item.get("artifact"), f"{item_path}.artifact")
                v.boolean(item.get("required"), f"{item_path}.required")

    outputs = node.get("outputs")
    if v.array(outputs, f"{path}.outputs", minimum=1):
        for index, item in enumerate(outputs):
            item_path = f"{path}.outputs[{index}]"
            fields = {"name", "artifact", "format"}
            if v.obj(item, item_path, fields, fields):
                v.string(item.get("name"), f"{item_path}.name")
                v.string(item.get("artifact"), f"{item_path}.artifact")
                v.string(item.get("format"), f"{item_path}.format")

    acceptance = node.get("acceptance")
    if v.array(acceptance, f"{path}.acceptance", minimum=1):
        for index, item in enumerate(acceptance):
            item_path = f"{path}.acceptance[{index}]"
            fields = {"id", "kind", "criterion", "evidence"}
            if v.obj(item, item_path, fields, fields):
                v.string(item.get("id"), f"{item_path}.id", slug=True)
                v.enum(item.get("kind"), f"{item_path}.kind", ACCEPTANCE_KINDS)
                v.string(item.get("criterion"), f"{item_path}.criterion")
                v.string(item.get("evidence"), f"{item_path}.evidence")

    permissions = node.get("permissions")
    permission_fields = {"read", "write", "external", "destructive"}
    if v.obj(permissions, f"{path}.permissions", permission_fields, permission_fields):
        v.string_array(permissions.get("read"), f"{path}.permissions.read")
        v.string_array(permissions.get("write"), f"{path}.permissions.write")
        v.string_array(permissions.get("external"), f"{path}.permissions.external")
        v.boolean(permissions.get("destructive"), f"{path}.permissions.destructive")

    execution = node.get("execution")
    required_execution_fields = {"max_attempts", "timeout_minutes", "completion_condition"}
    execution_fields = required_execution_fields | {
        "command", "working_directory", "environment", "evidence_paths",
        "capture_limit_bytes", "timeout_seconds",
    }
    if v.obj(execution, f"{path}.execution", required_execution_fields, execution_fields):
        v.integer(execution.get("max_attempts"), f"{path}.execution.max_attempts")
        v.integer(execution.get("timeout_minutes"), f"{path}.execution.timeout_minutes")
        v.string(execution.get("completion_condition"), f"{path}.execution.completion_condition")
        if "command" in execution:
            command = execution.get("command")
            if v.array(command, f"{path}.execution.command", minimum=1):
                for index, argument in enumerate(command):
                    if v.string(argument, f"{path}.execution.command[{index}]") and "\x00" in argument:
                        v.error(f"{path}.execution.command[{index}]", "must not contain NUL bytes")
        if "working_directory" in execution:
            working_directory = execution.get("working_directory")
            if v.string(working_directory, f"{path}.execution.working_directory"):
                _validate_relative_path(working_directory, f"{path}.execution.working_directory", v)
        if "environment" in execution and v.string_array(execution.get("environment"), f"{path}.execution.environment"):
            for index, name in enumerate(execution["environment"]):
                if not ENVIRONMENT_NAME.fullmatch(name):
                    v.error(f"{path}.execution.environment[{index}]", "must be an environment-variable name")
        if "evidence_paths" in execution and v.string_array(execution.get("evidence_paths"), f"{path}.execution.evidence_paths"):
            for index, evidence_path in enumerate(execution["evidence_paths"]):
                _validate_relative_path(evidence_path, f"{path}.execution.evidence_paths[{index}]", v)
        if "capture_limit_bytes" in execution:
            v.integer(execution.get("capture_limit_bytes"), f"{path}.execution.capture_limit_bytes", minimum=1024)
        if "timeout_seconds" in execution:
            v.integer(execution.get("timeout_seconds"), f"{path}.execution.timeout_seconds")

    if "context_control" in node:
        context = node.get("context_control")
        context_fields = {
            "version",
            "plan_path",
            "mode",
            "tokenizer",
            "adapter",
            "retention",
        }
        if v.obj(
            context,
            f"{path}.context_control",
            context_fields,
            context_fields,
        ):
            if context.get("version") != 3:
                v.error(f"{path}.context_control.version", "must equal 3")
            plan_path = context.get("plan_path")
            if v.string(plan_path, f"{path}.context_control.plan_path"):
                _validate_relative_path(
                    plan_path,
                    f"{path}.context_control.plan_path",
                    v,
                )
            v.enum(
                context.get("mode"),
                f"{path}.context_control.mode",
                {"preview", "executable"},
            )
            v.string(
                context.get("tokenizer"),
                f"{path}.context_control.tokenizer",
            )
            v.string(
                context.get("adapter"),
                f"{path}.context_control.adapter",
                slug=True,
            )
            v.enum(
                context.get("retention"),
                f"{path}.context_control.retention",
                {"digest-only", "explicit-payload"},
            )
        if (
            isinstance(executor, dict)
            and executor.get("kind") not in {"agent", "hybrid"}
        ):
            v.error(
                f"{path}.context_control",
                "requires an agent or hybrid executor",
            )

    side_effects = node.get("side_effects")
    effect_fields = {"level", "description", "rollback", "approval_required"}
    if v.obj(side_effects, f"{path}.side_effects", effect_fields, effect_fields):
        v.enum(side_effects.get("level"), f"{path}.side_effects.level", SIDE_EFFECT_LEVELS)
        v.string(side_effects.get("description"), f"{path}.side_effects.description")
        v.string(side_effects.get("rollback"), f"{path}.side_effects.rollback")
        v.boolean(side_effects.get("approval_required"), f"{path}.side_effects.approval_required")

    v.string_array(node.get("resources"), f"{path}.resources")
    v.string_array(node.get("tags"), f"{path}.tags")


def _validate_relative_path(value: str, path: str, v: Validation) -> None:
    try:
        validate_relative_path(value, label=path)
    except PathContractError:
        v.error(path, "must stay within the project root")


def _validate_edge(edge: Any, path: str, v: Validation) -> None:
    fields = {"id", "from", "to", "type", "condition", "artifacts", "max_traversals", "exit_condition"}
    if not v.obj(edge, path, fields, fields):
        return
    v.string(edge.get("id"), f"{path}.id", slug=True)
    v.string(edge.get("from"), f"{path}.from", slug=True)
    v.string(edge.get("to"), f"{path}.to", slug=True)
    v.enum(edge.get("type"), f"{path}.type", EDGE_TYPES)
    v.string(edge.get("condition"), f"{path}.condition")
    v.string_array(edge.get("artifacts"), f"{path}.artifacts")
    edge_type = edge.get("type")
    if edge_type == "feedback":
        v.integer(edge.get("max_traversals"), f"{path}.max_traversals")
        v.string(edge.get("exit_condition"), f"{path}.exit_condition")
    else:
        if edge.get("max_traversals") is not None:
            v.error(f"{path}.max_traversals", "must be null unless edge type is feedback")
        if edge.get("exit_condition") is not None:
            v.error(f"{path}.exit_condition", "must be null unless edge type is feedback")


def _validate_semantics(data: dict[str, Any], v: Validation) -> None:
    nodes_list = data.get("nodes")
    edges_list = data.get("edges")
    if not isinstance(nodes_list, list) or not isinstance(edges_list, list):
        return
    nodes: dict[str, dict[str, Any]] = {}
    node_paths: dict[str, str] = {}
    for index, node in enumerate(nodes_list):
        if not isinstance(node, dict) or not isinstance(node.get("id"), str):
            continue
        node_id = node["id"]
        path = f"$.nodes[{index}].id"
        if node_id in nodes:
            v.error(path, f"duplicates node id {node_id!r}")
        else:
            nodes[node_id] = node
            node_paths[node_id] = f"$.nodes[{index}]"
        acceptance_ids: set[str] = set()
        for a_index, item in enumerate(node.get("acceptance", [])):
            if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                continue
            acceptance_id = item["id"]
            if acceptance_id in acceptance_ids:
                v.error(f"$.nodes[{index}].acceptance[{a_index}].id", f"duplicates acceptance id {acceptance_id!r}")
            acceptance_ids.add(acceptance_id)

    edge_ids: set[str] = set()
    valid_edges: list[tuple[int, dict[str, Any]]] = []
    incoming: dict[str, list[dict[str, Any]]] = defaultdict(list)
    outgoing: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for index, edge in enumerate(edges_list):
        if not isinstance(edge, dict):
            continue
        edge_id = edge.get("id")
        if isinstance(edge_id, str):
            if edge_id in edge_ids:
                v.error(f"$.edges[{index}].id", f"duplicates edge id {edge_id!r}")
            edge_ids.add(edge_id)
        source = edge.get("from")
        target = edge.get("to")
        if source not in nodes:
            v.error(f"$.edges[{index}].from", f"references unknown node {source!r}")
        if target not in nodes:
            v.error(f"$.edges[{index}].to", f"references unknown node {target!r}")
        if source == target and source is not None:
            v.error(f"$.edges[{index}]", "self-edges are not allowed")
        if source in nodes and target in nodes:
            valid_edges.append((index, edge))
            incoming[target].append(edge)
            outgoing[source].append(edge)

    entries = data.get("entry_nodes") if isinstance(data.get("entry_nodes"), list) else []
    terminals = data.get("terminal_nodes") if isinstance(data.get("terminal_nodes"), list) else []
    for index, node_id in enumerate(entries):
        if node_id not in nodes:
            v.error(f"$.entry_nodes[{index}]", f"references unknown node {node_id!r}")
    for index, node_id in enumerate(terminals):
        if node_id not in nodes:
            v.error(f"$.terminal_nodes[{index}]", f"references unknown node {node_id!r}")

    for node_id in entries:
        scheduling = [edge for edge in incoming.get(node_id, []) if edge.get("type") in SCHEDULING_TYPES]
        if scheduling:
            v.error("$.entry_nodes", f"entry node {node_id!r} has incoming scheduling edge {scheduling[0].get('id')!r}")
    for node_id in terminals:
        scheduling = [edge for edge in outgoing.get(node_id, []) if edge.get("type") in SCHEDULING_TYPES]
        if scheduling:
            v.error("$.terminal_nodes", f"terminal node {node_id!r} has outgoing scheduling edge {scheduling[0].get('id')!r}")

    _validate_acyclic(nodes, valid_edges, v)
    _validate_reachability(nodes, valid_edges, entries, terminals, v)
    _validate_artifacts(nodes, node_paths, valid_edges, incoming, v)
    _validate_edge_types(nodes, valid_edges, terminals, v)
    _validate_effects(nodes, node_paths, incoming, v)
    _validate_resource_conflicts(nodes, valid_edges, v)


def _validate_acyclic(nodes: dict[str, dict[str, Any]], edges: list[tuple[int, dict[str, Any]]], v: Validation) -> None:
    adjacency: dict[str, list[str]] = defaultdict(list)
    indegree = {node_id: 0 for node_id in nodes}
    for _, edge in edges:
        if edge.get("type") in SCHEDULING_TYPES:
            source, target = edge["from"], edge["to"]
            adjacency[source].append(target)
            indegree[target] += 1
    queue = deque(sorted(node_id for node_id, degree in indegree.items() if degree == 0))
    visited = 0
    while queue:
        current = queue.popleft()
        visited += 1
        for target in adjacency.get(current, []):
            indegree[target] -= 1
            if indegree[target] == 0:
                queue.append(target)
    if visited != len(nodes):
        cycle_nodes = sorted(node_id for node_id, degree in indegree.items() if degree > 0)
        v.error("$.edges", f"scheduling edges contain a cycle involving: {', '.join(cycle_nodes)}")


def _validate_reachability(
    nodes: dict[str, dict[str, Any]],
    edges: list[tuple[int, dict[str, Any]]],
    entries: list[Any],
    terminals: list[Any],
    v: Validation,
) -> None:
    adjacency: dict[str, list[str]] = defaultdict(list)
    for _, edge in edges:
        if edge.get("type") in FLOW_TYPES:
            adjacency[edge["from"]].append(edge["to"])
    seen = {entry for entry in entries if entry in nodes}
    queue = deque(sorted(seen))
    while queue:
        current = queue.popleft()
        for target in adjacency.get(current, []):
            if target not in seen:
                seen.add(target)
                queue.append(target)
    unreachable = sorted(set(nodes) - seen)
    if unreachable:
        v.error("$.nodes", f"not reachable from an entry node: {', '.join(unreachable)}")
    unreachable_terminals = sorted(node_id for node_id in terminals if node_id in nodes and node_id not in seen)
    if unreachable_terminals:
        v.error("$.terminal_nodes", f"not reachable from an entry node: {', '.join(unreachable_terminals)}")


def _node_artifacts(node: dict[str, Any], field: str) -> set[str]:
    result: set[str] = set()
    for item in node.get(field, []):
        if isinstance(item, dict) and isinstance(item.get("artifact"), str):
            result.add(item["artifact"])
    return result


def _validate_artifacts(
    nodes: dict[str, dict[str, Any]],
    node_paths: dict[str, str],
    edges: list[tuple[int, dict[str, Any]]],
    incoming: dict[str, list[dict[str, Any]]],
    v: Validation,
) -> None:
    producers: dict[str, str] = {}
    for node_id, node in nodes.items():
        for index, item in enumerate(node.get("outputs", [])):
            if not isinstance(item, dict) or not isinstance(item.get("artifact"), str):
                continue
            artifact = item["artifact"]
            if artifact in producers:
                v.error(f"{node_paths[node_id]}.outputs[{index}].artifact", f"artifact {artifact!r} is also produced by {producers[artifact]!r}")
            else:
                producers[artifact] = node_id

    for node_id, node in nodes.items():
        for index, item in enumerate(node.get("inputs", [])):
            if not isinstance(item, dict) or not isinstance(item.get("artifact"), str):
                continue
            artifact = item["artifact"]
            if not item.get("required") or artifact.startswith("external:"):
                continue
            producer = producers.get(artifact)
            if producer is None:
                v.error(f"{node_paths[node_id]}.inputs[{index}].artifact", f"required artifact {artifact!r} has no producer; prefix external inputs with 'external:'")
                continue
            matching = [
                edge for edge in incoming.get(node_id, [])
                if edge.get("type") == "consumes" and edge.get("from") == producer and artifact in edge.get("artifacts", [])
            ]
            if not matching:
                v.error(f"{node_paths[node_id]}.inputs[{index}]", f"required artifact {artifact!r} needs a consumes edge from {producer!r}")

    for index, edge in edges:
        edge_type = edge.get("type")
        artifacts = edge.get("artifacts") if isinstance(edge.get("artifacts"), list) else []
        source_outputs = _node_artifacts(nodes[edge["from"]], "outputs")
        target_inputs = _node_artifacts(nodes[edge["to"]], "inputs")
        target_outputs = _node_artifacts(nodes[edge["to"]], "outputs")
        if edge_type in {"consumes", "validates", "approves", "invalidates"} and not artifacts:
            v.error(f"$.edges[{index}].artifacts", f"{edge_type} edges must name at least one artifact")
        if edge_type in {"consumes", "validates", "approves"}:
            for artifact in artifacts:
                if artifact not in source_outputs:
                    v.error(f"$.edges[{index}].artifacts", f"artifact {artifact!r} is not produced by source node {edge['from']!r}")
                if artifact not in target_inputs:
                    v.error(f"$.edges[{index}].artifacts", f"artifact {artifact!r} is not declared as an input by target node {edge['to']!r}")
        elif edge_type == "invalidates":
            for artifact in artifacts:
                if artifact not in target_outputs:
                    v.error(f"$.edges[{index}].artifacts", f"artifact {artifact!r} is not produced by invalidated target node {edge['to']!r}")

    for node_id, node in nodes.items():
        if node.get("state") == "invalidated" and not any(edge.get("type") == "invalidates" for edge in incoming.get(node_id, [])):
            v.error(f"{node_paths[node_id]}.state", "invalidated nodes require an incoming invalidates edge")


def _validate_edge_types(
    nodes: dict[str, dict[str, Any]],
    edges: list[tuple[int, dict[str, Any]]],
    terminals: list[Any],
    v: Validation,
) -> None:
    terminal_set = set(terminals)
    for index, edge in edges:
        edge_type = edge.get("type")
        source = nodes[edge["from"]]
        if edge_type == "validates" and source.get("type") != "verification":
            v.error(f"$.edges[{index}].from", "validates edges must start at a verification node")
        if edge_type == "approves" and source.get("type") != "human_gate":
            v.error(f"$.edges[{index}].from", "approves edges must start at a human_gate node")
        if edge_type == "feedback":
            if source.get("type") not in {"verification", "human_gate", "observation"}:
                v.error(f"$.edges[{index}].from", "feedback edges must start at verification, human_gate, or observation nodes")
            if edge.get("to") in terminal_set:
                v.error(f"$.edges[{index}].to", "feedback edges cannot target terminal nodes")


def _concrete_rollback(value: Any) -> bool:
    return _is_nonempty(value) and value.strip().lower() not in {"none", "n/a", "not applicable", "unknown", "tbd"}


def _validate_effects(
    nodes: dict[str, dict[str, Any]],
    node_paths: dict[str, str],
    incoming: dict[str, list[dict[str, Any]]],
    v: Validation,
) -> None:
    for node_id, node in nodes.items():
        path = node_paths[node_id]
        executor = node.get("executor") if isinstance(node.get("executor"), dict) else {}
        execution = node.get("execution") if isinstance(node.get("execution"), dict) else {}
        permissions = node.get("permissions") if isinstance(node.get("permissions"), dict) else {}
        effects = node.get("side_effects") if isinstance(node.get("side_effects"), dict) else {}
        if node.get("type") == "human_gate" and executor.get("kind") != "human":
            v.error(f"{path}.executor.kind", "human_gate nodes require a human executor")
        if "command" in execution and executor.get("kind") != "deterministic":
            v.error(f"{path}.execution.command", "local commands require a deterministic executor")
        level = effects.get("level")
        if permissions.get("destructive") is True and level != "destructive":
            v.error(f"{path}.side_effects.level", "destructive permission requires destructive side effects")
        if level == "destructive" and permissions.get("destructive") is not True:
            v.error(f"{path}.permissions.destructive", "destructive side effects require destructive permission")
        mutates = bool(permissions.get("write")) or bool(permissions.get("external"))
        if mutates and level == "none":
            v.error(f"{path}.side_effects.level", "write or external permissions require reversible or destructive side effects")
        if level in {"reversible", "destructive"} and not _concrete_rollback(effects.get("rollback")):
            v.error(f"{path}.side_effects.rollback", f"{level} side effects require a concrete rollback or compensation")
        if level == "destructive":
            if effects.get("approval_required") is not True:
                v.error(f"{path}.side_effects.approval_required", "destructive side effects require approval")
            approvals = [edge for edge in incoming.get(node_id, []) if edge.get("type") == "approves"]
            if not approvals:
                v.error(path, "destructive node requires an incoming approves edge from a human gate")
        if effects.get("approval_required") is True:
            approvals = [edge for edge in incoming.get(node_id, []) if edge.get("type") == "approves"]
            if not approvals:
                v.error(f"{path}.side_effects.approval_required", "approval_required needs an incoming approves edge")


def _validate_resource_conflicts(
    nodes: dict[str, dict[str, Any]],
    edges: list[tuple[int, dict[str, Any]]],
    v: Validation,
) -> None:
    adjacency: dict[str, set[str]] = defaultdict(set)
    conflicts: set[frozenset[str]] = set()
    for _, edge in edges:
        if edge.get("type") in SCHEDULING_TYPES:
            adjacency[edge["from"]].add(edge["to"])
        elif edge.get("type") == "conflicts":
            conflicts.add(frozenset((edge["from"], edge["to"])))

    reachable: dict[str, set[str]] = {}
    for node_id in nodes:
        seen: set[str] = set()
        pending = list(adjacency.get(node_id, set()))
        while pending:
            target = pending.pop()
            if target in seen:
                continue
            seen.add(target)
            pending.extend(adjacency.get(target, set()) - seen)
        reachable[node_id] = seen

    mutating: list[tuple[str, set[str]]] = []
    for node_id, node in nodes.items():
        permissions = node.get("permissions", {})
        if permissions.get("write") or permissions.get("external") or permissions.get("destructive"):
            mutating.append((node_id, set(node.get("resources", []))))

    for left_index, (left_id, left_resources) in enumerate(mutating):
        for right_id, right_resources in mutating[left_index + 1:]:
            shared = left_resources & right_resources
            if not shared:
                continue
            ordered = right_id in reachable[left_id] or left_id in reachable[right_id]
            declared = frozenset((left_id, right_id)) in conflicts
            if not ordered and not declared:
                v.error(
                    "$.nodes",
                    "unordered mutating nodes share resources without a conflicts edge: "
                    f"{left_id!r} and {right_id!r} share {', '.join(sorted(shared))}",
                )


def validate_graph(data: Any, markdown_text: str | None = None) -> dict[str, Any]:
    validation = Validation()
    _validate_shape(data, validation)
    if isinstance(data, dict) and not validation.errors:
        _validate_semantics(data, validation)
    if isinstance(data, dict):
        if markdown_text is not None:
            markers = MARKER.findall(markdown_text)
            if len(markers) != 1:
                validation.error("$markdown", "must contain exactly one development-graph SHA-256 marker")
            else:
                expected = canonical_checksum(data)
                if markers[0] != expected:
                    validation.error("$markdown", f"checksum mismatch: expected {expected}, found {markers[0]}")
    stats = {
        "nodes": len(data.get("nodes", [])) if isinstance(data, dict) and isinstance(data.get("nodes"), list) else 0,
        "edges": len(data.get("edges", [])) if isinstance(data, dict) and isinstance(data.get("edges"), list) else 0,
        "errors": len(validation.errors),
        "warnings": len(validation.warnings),
    }
    checksum = canonical_checksum(data) if isinstance(data, dict) else None
    marker = f"<!-- development-graph-sha256: {checksum} -->" if checksum is not None else None
    return {
        "valid": not validation.errors,
        "errors": validation.errors,
        "warnings": validation.warnings,
        "stats": stats,
        "canonical_sha256": checksum,
        "markdown_marker": marker,
    }


def _read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("graph", type=Path, help="canonical development-graph JSON file")
    parser.add_argument("--markdown", type=Path, help="generated Markdown file whose checksum must match")
    parser.add_argument("--json", action="store_true", dest="as_json", help="emit a machine-readable report")
    args = parser.parse_args(argv)

    try:
        data = _read_json(args.graph)
    except (OSError, json.JSONDecodeError) as exc:
        report = {
            "valid": False,
            "errors": [f"$file: {exc}"],
            "warnings": [],
            "stats": {"nodes": 0, "edges": 0, "errors": 1, "warnings": 0},
            "canonical_sha256": None,
            "markdown_marker": None,
        }
    else:
        markdown_text: str | None = None
        if args.markdown is not None:
            try:
                markdown_text = args.markdown.read_text(encoding="utf-8")
            except OSError as exc:
                checksum = canonical_checksum(data) if isinstance(data, dict) else None
                report = {
                    "valid": False,
                    "errors": [f"$markdown: {exc}"],
                    "warnings": [],
                    "stats": {"nodes": 0, "edges": 0, "errors": 1, "warnings": 0},
                    "canonical_sha256": checksum,
                    "markdown_marker": f"<!-- development-graph-sha256: {checksum} -->" if checksum is not None else None,
                }
            else:
                report = validate_graph(data, markdown_text)
        else:
            report = validate_graph(data)

    if args.as_json:
        print(json.dumps(report, indent=2, sort_keys=True))
    elif report["valid"]:
        print("Graph validation: PASS")
        print(f"Nodes: {report['stats']['nodes']}  Edges: {report['stats']['edges']}  Warnings: {report['stats']['warnings']}")
        for warning in report["warnings"]:
            print(f"WARNING {warning}")
    else:
        print("Graph validation: FAIL")
        for error in report["errors"]:
            print(f"ERROR {error}")
        for warning in report["warnings"]:
            print(f"WARNING {warning}")
    return 0 if report["valid"] else 1


if __name__ == "__main__":
    sys.exit(main())
