"""Load and validate versioned project templates from portable skill assets."""

from __future__ import annotations

import json
import re
from pathlib import Path, PurePosixPath
from typing import Any

from path_contract import resolve_skill_asset, validate_relative_path


_SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_NODE_TYPES = {
    "operation",
    "work",
    "decision",
    "verification",
    "human_gate",
    "observation",
}
_REGISTRY_PATH = "assets/templates/registry.json"


class TemplateRegistryError(ValueError):
    """Raised when a template registry or asset violates its contract."""


def _read_json(path: Path, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise TemplateRegistryError(f"{label} is not valid UTF-8 JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise TemplateRegistryError(f"{label} must be a JSON object")
    return value


def _require_keys(
    value: dict[str, Any],
    required: set[str],
    *,
    label: str,
) -> None:
    missing = required - set(value)
    extra = set(value) - required
    if missing:
        raise TemplateRegistryError(
            f"{label} is missing fields: {', '.join(sorted(missing))}"
        )
    if extra:
        raise TemplateRegistryError(
            f"{label} has unknown fields: {', '.join(sorted(extra))}"
        )


def _slug(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not _SLUG.fullmatch(value):
        raise TemplateRegistryError(f"{label} must be a lowercase slug")
    return value


def _portable_path(value: object, *, label: str) -> str:
    if not isinstance(value, str):
        raise TemplateRegistryError(f"{label} must be a relative path")
    try:
        return validate_relative_path(value, label=label, allow_dot=False)
    except ValueError as exc:
        raise TemplateRegistryError(str(exc)) from exc


def load_registry() -> dict[str, Any]:
    """Load the registry relative to this executing skill, never caller CWD."""

    registry = _read_json(
        resolve_skill_asset(_REGISTRY_PATH),
        label="template registry",
    )
    _require_keys(
        registry,
        {"schema_version", "templates"},
        label="template registry",
    )
    if registry["schema_version"] != 1:
        raise TemplateRegistryError("template registry schema_version must equal 1")
    records = registry["templates"]
    if not isinstance(records, list) or not records:
        raise TemplateRegistryError("template registry templates must be non-empty")
    seen: set[str] = set()
    for index, record in enumerate(records):
        label = f"template registry record {index}"
        if not isinstance(record, dict):
            raise TemplateRegistryError(f"{label} must be an object")
        _require_keys(
            record,
            {"id", "current_version", "asset"},
            label=label,
        )
        template_id = _slug(record["id"], label=f"{label} id")
        if template_id in seen:
            raise TemplateRegistryError(f"duplicate template id: {template_id}")
        seen.add(template_id)
        if record["current_version"] != 1:
            raise TemplateRegistryError(
                f"{label} current_version must equal the shipped version 1"
            )
        asset = _portable_path(record["asset"], label=f"{label} asset")
        expected = PurePosixPath(
            "assets",
            "templates",
            template_id,
            "v1",
            "template.json",
        ).as_posix()
        if asset != expected:
            raise TemplateRegistryError(
                f"{label} asset must be the versioned path {expected!r}"
            )
    return registry


def template_ids() -> tuple[str, ...]:
    """Return stable public entry-mode identifiers."""

    return tuple(sorted(record["id"] for record in load_registry()["templates"]))


def _record(template_id: str) -> dict[str, Any]:
    _slug(template_id, label="template id")
    for record in load_registry()["templates"]:
        if record["id"] == template_id:
            return record
    raise TemplateRegistryError(f"unknown template id: {template_id}")


def _validate_template(template: dict[str, Any], *, expected_id: str) -> None:
    _require_keys(
        template,
        {
            "schema_version",
            "id",
            "version",
            "entry_mode",
            "entry_nodes",
            "terminal_nodes",
            "nodes",
            "feedback",
            "approvals",
            "project_files",
            "specification",
        },
        label=f"template {expected_id}",
    )
    if template["schema_version"] != 1 or template["version"] != 1:
        raise TemplateRegistryError(
            f"template {expected_id} schema_version and version must equal 1"
        )
    if template["id"] != expected_id or template["entry_mode"] != expected_id:
        raise TemplateRegistryError(
            f"template {expected_id} id and entry_mode must match its registry id"
        )
    nodes = template["nodes"]
    if not isinstance(nodes, list) or not nodes:
        raise TemplateRegistryError(f"template {expected_id} nodes must be non-empty")
    node_ids: set[str] = set()
    for index, node in enumerate(nodes):
        label = f"template {expected_id} node {index}"
        if not isinstance(node, dict):
            raise TemplateRegistryError(f"{label} must be an object")
        _require_keys(
            node,
            {
                "id",
                "type",
                "writes",
                "destructive",
                "predecessors",
                "output",
                "write_scope",
            },
            label=label,
        )
        node_id = _slug(node["id"], label=f"{label} id")
        if node_id in node_ids:
            raise TemplateRegistryError(
                f"template {expected_id} duplicates node {node_id}"
            )
        node_ids.add(node_id)
        if node["type"] not in _NODE_TYPES:
            raise TemplateRegistryError(f"{label} has unknown node type")
        if not isinstance(node["writes"], bool) or not isinstance(
            node["destructive"], bool
        ):
            raise TemplateRegistryError(f"{label} write flags must be booleans")
        if not isinstance(node["predecessors"], list) or not all(
            isinstance(item, str) and _SLUG.fullmatch(item)
            for item in node["predecessors"]
        ):
            raise TemplateRegistryError(f"{label} predecessors must be slugs")
        if len(node["predecessors"]) != len(set(node["predecessors"])):
            raise TemplateRegistryError(f"{label} predecessors must be unique")
        if not isinstance(node["write_scope"], list):
            raise TemplateRegistryError(f"{label} write_scope must be an array")
        if len(node["write_scope"]) != len(set(node["write_scope"])):
            raise TemplateRegistryError(f"{label} write_scope must be unique")
        for scope in node["write_scope"]:
            _portable_path(scope, label=f"{label} write scope")
        output = node["output"]
        if (
            not isinstance(output, dict)
            or set(output) != {"artifact", "format"}
            or not all(isinstance(item, str) and item.strip() for item in output.values())
        ):
            raise TemplateRegistryError(
                f"{label} output must contain non-empty artifact and format"
            )
    for node in nodes:
        unknown = set(node["predecessors"]) - node_ids
        if unknown:
            raise TemplateRegistryError(
                f"template {expected_id} node {node['id']} has unknown predecessors"
            )

    for field in ("entry_nodes", "terminal_nodes"):
        values = template[field]
        if not isinstance(values, list) or not values:
            raise TemplateRegistryError(
                f"template {expected_id} {field} must be non-empty"
            )
        if any(value not in node_ids for value in values):
            raise TemplateRegistryError(
                f"template {expected_id} {field} references an unknown node"
            )
        if len(values) != len(set(values)):
            raise TemplateRegistryError(
                f"template {expected_id} {field} must contain unique nodes"
            )

    nodes_by_id = {node["id"]: node for node in nodes}
    for field in ("feedback", "approvals"):
        records = template[field]
        if not isinstance(records, list):
            raise TemplateRegistryError(
                f"template {expected_id} {field} must be an array"
            )
        required = (
            {"from", "to", "max_traversals", "exit_condition"}
            if field == "feedback"
            else {"from", "to", "artifact"}
        )
        for index, record in enumerate(records):
            label = f"template {expected_id} {field} record {index}"
            if not isinstance(record, dict):
                raise TemplateRegistryError(f"{label} must be an object")
            _require_keys(record, required, label=label)
            if record["from"] not in node_ids or record["to"] not in node_ids:
                raise TemplateRegistryError(f"{label} references an unknown node")
            if record["from"] == record["to"]:
                raise TemplateRegistryError(f"{label} must not be a self-edge")
            if field == "feedback":
                max_traversals = record["max_traversals"]
                if (
                    not isinstance(max_traversals, int)
                    or isinstance(max_traversals, bool)
                    or max_traversals < 1
                ):
                    raise TemplateRegistryError(
                        f"{label} max_traversals must be an integer >= 1"
                    )
                if (
                    not isinstance(record["exit_condition"], str)
                    or not record["exit_condition"].strip()
                ):
                    raise TemplateRegistryError(
                        f"{label} exit_condition must be non-empty"
                    )
            else:
                artifact = record["artifact"]
                if not isinstance(artifact, str) or not artifact.strip():
                    raise TemplateRegistryError(
                        f"{label} artifact must be non-empty"
                    )
                source_output = nodes_by_id[record["from"]]["output"]["artifact"]
                if artifact != source_output:
                    raise TemplateRegistryError(
                        f"{label} artifact must match the source output"
                    )
                if record["from"] not in nodes_by_id[record["to"]]["predecessors"]:
                    raise TemplateRegistryError(
                        f"{label} target must declare the source as a predecessor"
                    )
        identities = [
            tuple(record[key] for key in sorted(required))
            for record in records
        ]
        if len(identities) != len(set(identities)):
            raise TemplateRegistryError(
                f"template {expected_id} {field} records must be unique"
            )

    project_files = template["project_files"]
    if not isinstance(project_files, list):
        raise TemplateRegistryError(
            f"template {expected_id} project_files must be an array"
        )
    seen_paths: set[str] = set()
    for index, record in enumerate(project_files):
        label = f"template {expected_id} project file {index}"
        if not isinstance(record, dict):
            raise TemplateRegistryError(f"{label} must be an object")
        _require_keys(record, {"path", "source"}, label=label)
        target = _portable_path(record["path"], label=f"{label} path")
        _portable_path(record["source"], label=f"{label} source")
        if target in seen_paths:
            raise TemplateRegistryError(
                f"template {expected_id} duplicates project file {target}"
            )
        seen_paths.add(target)

    specification = template["specification"]
    if specification is not None and not isinstance(specification, dict):
        raise TemplateRegistryError(
            f"template {expected_id} specification must be an object or null"
        )
    if expected_id == "specification-to-delivery" and specification is None:
        raise TemplateRegistryError(
            "specification-to-delivery must declare a specification manifest"
        )
    if expected_id != "specification-to-delivery" and specification is not None:
        raise TemplateRegistryError(
            f"template {expected_id} must not declare a specification manifest"
        )


def _omit_implementation_plan(template: dict[str, Any]) -> None:
    """Adapt the specification scaffold for direct, approval-gated implementation."""

    specification = template["specification"]
    plan = specification.pop("implementation_plan")
    implementation_nodes = plan["implementation_nodes"]
    specification["implementation_nodes"] = implementation_nodes
    omitted = {plan["node"], "resolve-blocking-decisions"}
    specification["approval"]["targets"] = [
        target for target in specification["approval"]["targets"]
        if target not in omitted
    ]
    template["nodes"] = [
        node for node in template["nodes"] if node["id"] not in omitted
    ]
    required_producers: list[str] = []
    for document in specification["documents"]:
        consumers = [
            consumer for consumer in document["consumer_nodes"]
            if consumer not in omitted
        ]
        if document["required"]:
            required_producers.append(document["producer_node"])
            consumers.extend(
                node_id for node_id in implementation_nodes
                if node_id not in consumers
            )
        document["consumer_nodes"] = consumers
    for node in template["nodes"]:
        predecessors = [
            predecessor for predecessor in node["predecessors"]
            if predecessor not in omitted
        ]
        if node["id"] in implementation_nodes:
            predecessors.extend(
                producer for producer in required_producers
                if producer not in predecessors
            )
        node["predecessors"] = predecessors
    for key in ("approvals", "feedback"):
        template[key] = [
            edge for edge in template[key]
            if edge["from"] not in omitted and edge["to"] not in omitted
        ]
    template["project_files"] = [
        record for record in template["project_files"] if record["path"] != plan["path"]
    ]


def load_template(
    template_id: str, *, include_implementation_plan: bool = True
) -> dict[str, Any]:
    """Load and validate the current version of one public template."""

    record = _record(template_id)
    template = _read_json(
        resolve_skill_asset(record["asset"]),
        label=f"template {template_id}",
    )
    _validate_template(template, expected_id=template_id)
    if not include_implementation_plan:
        if template_id != "specification-to-delivery":
            raise TemplateRegistryError(
                "Omitting the implementation plan requires specification-to-delivery"
            )
        _omit_implementation_plan(template)
        _validate_template(template, expected_id=template_id)
    return template


def project_files(
    template_id: str, *, include_implementation_plan: bool = True
) -> dict[str, bytes]:
    """Return portable target paths and immutable bytes owned by a template."""

    record = _record(template_id)
    template = load_template(
        template_id, include_implementation_plan=include_implementation_plan
    )
    asset_parent = PurePosixPath(record["asset"]).parent
    result: dict[str, bytes] = {}
    for file_record in template["project_files"]:
        source = (asset_parent / file_record["source"]).as_posix()
        try:
            result[file_record["path"]] = resolve_skill_asset(source).read_bytes()
        except OSError as exc:
            raise TemplateRegistryError(
                f"template {template_id} project file cannot be read: {source}"
            ) from exc
    return result
