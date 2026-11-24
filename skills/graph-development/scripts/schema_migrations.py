"""Deterministic ordered migrations for graph-governed project artifacts."""

from __future__ import annotations

import copy
import hashlib
import json
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from graph_core import (
    projection_contract_errors,
    scheduling_predecessors,
    sha256_json,
)
from path_contract import (
    PathContractError,
    resolve_project_path,
    resolve_skill_asset,
    validate_relative_path,
)
from validate_graph import canonical_checksum, validate_graph
from validate_specifications import _validate_manifest_shape


class MigrationError(RuntimeError):
    """Raised when a schema migration cannot be planned safely."""


@dataclass(frozen=True)
class MigrationPlan:
    valid: bool
    dry_run: bool
    source_digest: str
    candidate_digest: str
    source_versions: dict[str, int]
    target_versions: dict[str, int]
    steps: tuple[dict[str, Any], ...]
    candidate_bundle: dict[str, Any]
    receipt: dict[str, Any]
    errors: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        """Return a compact plan without embedding migrated project payloads."""

        return {
            "schema_version": 1,
            "valid": self.valid,
            "dry_run": self.dry_run,
            "source_digest": self.source_digest,
            "candidate_digest": self.candidate_digest,
            "source_versions": dict(sorted(self.source_versions.items())),
            "target_versions": dict(sorted(self.target_versions.items())),
            "steps": [copy.deepcopy(step) for step in self.steps],
            "receipt": copy.deepcopy(self.receipt),
            "errors": list(self.errors),
        }


@dataclass(frozen=True)
class RestoredArtifact:
    """Exact artifact bytes and mode recovered from retained transaction evidence."""

    content: bytes
    mode: int


_AUTHENTICATED_REQUEST_TOKEN = object()


class AuthenticatedMigrationRequest(dict[str, Any]):
    """In-process migration request that can only be issued by the builder."""

    __slots__ = (
        "_authentication_token",
        "_descriptor",
        "_project_root",
        "_request_binding",
    )

    def __init__(
        self,
        value: dict[str, Any],
        *,
        token: object,
        descriptor: dict[str, Any],
        project_root: Path,
    ) -> None:
        if token is not _AUTHENTICATED_REQUEST_TOKEN:
            raise MigrationError(
                "authenticated migration requests must use the lifecycle builder"
            )
        super().__init__(value)
        self._authentication_token = token
        self._descriptor = copy.deepcopy(descriptor)
        self._project_root = project_root.resolve()
        self._request_binding = _lifecycle_request_binding(self)


_Transform = Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]
_KINDS = ("graph", "manifest", "context", "runtime")


def _canonical(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise MigrationError("migration bundle must be JSON-compatible") from exc


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _lifecycle_request_binding(request: dict[str, Any]) -> str:
    artifacts = request.get("artifacts")
    if not isinstance(artifacts, list):
        return ""
    material: list[dict[str, Any]] = []
    for record in artifacts:
        if not isinstance(record, dict):
            return ""
        content = record.get("content")
        if not isinstance(content, bytes):
            return ""
        material.append(
            {
                "path": record.get("path"),
                "mode": record.get("mode"),
                "expected_sha256": record.get("expected_sha256"),
                "candidate_sha256": hashlib.sha256(content).hexdigest(),
            }
        )
    return _digest(
        {
            "schema_version": request.get("schema_version"),
            "operation": request.get("operation"),
            "artifacts": material,
            "metadata": request.get("metadata"),
        }
    )


def _load_registry() -> dict[str, Any]:
    path = resolve_skill_asset("references/schema-history/registry.json")
    try:
        registry = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise MigrationError(f"cannot load schema migration registry: {exc}") from exc
    if (
        not isinstance(registry, dict)
        or registry.get("schema_version") != 1
        or not isinstance(registry.get("kinds"), dict)
    ):
        raise MigrationError("schema migration registry is invalid")
    if set(registry["kinds"]) != set(_KINDS):
        raise MigrationError("schema migration registry kind inventory is invalid")
    for kind, record in registry["kinds"].items():
        if not isinstance(record, dict):
            raise MigrationError(f"schema migration registry {kind} record is invalid")
        current = record.get("current_version")
        steps = record.get("steps")
        if (
            isinstance(current, bool)
            or not isinstance(current, int)
            or current < 1
            or not isinstance(steps, list)
        ):
            raise MigrationError(f"schema migration registry {kind} record is invalid")
        expected = 0
        for step in steps:
            if (
                not isinstance(step, dict)
                or set(step)
                != {
                    "from_version",
                    "to_version",
                    "reversible",
                    "irreversible_boundary",
                }
                or step.get("from_version") != expected
                or step.get("to_version") != expected + 1
                or not isinstance(step.get("reversible"), bool)
            ):
                raise MigrationError(
                    f"schema migration registry {kind} steps are not ordered"
                )
            boundary = step.get("irreversible_boundary")
            if boundary is not None and (
                not isinstance(boundary, str) or not boundary.strip()
            ):
                raise MigrationError(
                    f"schema migration registry {kind} boundary is invalid"
                )
            expected += 1
        if expected != current:
            raise MigrationError(
                f"schema migration registry {kind} does not reach current version"
            )
    return registry


def _normalize_root(document: dict[str, Any], field: str) -> None:
    raw = document.get(field)
    if not isinstance(raw, str):
        return
    normalized = validate_relative_path(raw, label=field)
    document[field] = normalized


def _graph_v0_to_v1(
    document: dict[str, Any],
    context: dict[str, Any],
) -> dict[str, Any]:
    del context
    candidate = copy.deepcopy(document)
    candidate["schema_version"] = 1
    project = candidate.get("project")
    if isinstance(project, dict):
        _normalize_root(project, "root")
    return candidate


def _manifest_v0_to_v1(
    document: dict[str, Any],
    context: dict[str, Any],
) -> dict[str, Any]:
    del context
    candidate = copy.deepcopy(document)
    candidate["schema_version"] = 1
    _normalize_root(candidate, "specification_root")
    return candidate


def _context_v0_to_v1(
    document: dict[str, Any],
    context: dict[str, Any],
) -> dict[str, Any]:
    del context
    claimed = document.get("digest")
    body = {key: value for key, value in document.items() if key != "digest"}
    if not isinstance(claimed, str) or claimed != sha256_json(body):
        raise MigrationError("context predecessor is not a valid sealed record")
    record_id = document.get("record_id")
    if not isinstance(record_id, str) or not record_id.strip():
        raise MigrationError("context predecessor requires record_id")
    successor: dict[str, Any] = {
        "schema_version": 1,
        "record_id": f"{record_id}-v1",
        "predecessor_digest": claimed,
        "sealed_source": copy.deepcopy(document),
    }
    successor["digest"] = sha256_json(successor)
    return successor


def _ledger_errors(
    records: Any,
    runtime: dict[str, Any] | None = None,
) -> list[str]:
    errors: list[str] = []
    if not isinstance(records, list) or not records:
        return ["runtime migration requires a non-empty ledger"]
    expected_sequence = 1
    previous_hash: str | None = None
    previous_run_version: int | None = None
    for index, record in enumerate(records):
        label = f"ledger event {index + 1}"
        if not isinstance(record, dict):
            errors.append(f"{label} must be an object")
            continue
        if record.get("sequence") != expected_sequence:
            errors.append(f"{label} sequence is invalid")
        if record.get("previous_hash") != previous_hash:
            errors.append(f"{label} previous hash is invalid")
        before = record.get("run_version_before")
        after = record.get("run_version_after")
        if (
            isinstance(before, bool)
            or not isinstance(before, int)
            or isinstance(after, bool)
            or not isinstance(after, int)
            or after != before + 1
        ):
            errors.append(f"{label} run version transition is invalid")
        elif index == 0 and before != 0:
            errors.append(f"{label} run version must begin at zero")
        elif previous_run_version is not None and before != previous_run_version:
            errors.append(f"{label} run version continuity is invalid")
        previous_run_version = after if isinstance(after, int) else None
        if runtime is not None:
            if record.get("run_id") != runtime.get("run_id"):
                errors.append(f"{label} run_id does not match runtime")
            if record.get("graph_id") != runtime.get("graph_id"):
                errors.append(f"{label} graph_id does not match runtime")
        if (
            record.get("action") == "schema-migrated"
            and record.get("irreversible_boundary") != "runtime-ledger-append"
        ):
            errors.append(
                f"{label} schema migration lacks irreversible boundary"
            )
        claimed = record.get("event_hash")
        body = {key: value for key, value in record.items() if key != "event_hash"}
        if not isinstance(claimed, str) or claimed != sha256_json(body):
            errors.append(f"{label} event hash is invalid")
        previous_hash = claimed if isinstance(claimed, str) else None
        expected_sequence += 1
    if runtime is not None and records:
        last = records[-1]
        if isinstance(last, dict):
            if runtime.get("last_event_sequence") != last.get("sequence"):
                errors.append("runtime last event sequence does not match ledger")
            if runtime.get("last_event_hash") != last.get("event_hash"):
                errors.append("runtime last event hash does not match ledger")
            if runtime.get("run_version") != last.get("run_version_after"):
                errors.append("runtime version does not match ledger")
    return errors


def _active_runtime_nodes(runtime: dict[str, Any]) -> list[str]:
    nodes = runtime.get("nodes")
    if not isinstance(nodes, dict):
        raise MigrationError("runtime nodes must be an object")
    active: list[str] = []
    for node_id, state in nodes.items():
        if not isinstance(state, dict):
            raise MigrationError(f"runtime node {node_id!r} is invalid")
        if state.get("state") in {"running", "verifying"} or state.get("lease") is not None:
            active.append(str(node_id))
    return sorted(active)


_RUNTIME_FIELDS = {
    "schema_version",
    "run_id",
    "run_version",
    "graph_id",
    "graph_version",
    "graph_checksum",
    "status",
    "created_at",
    "updated_at",
    "last_event_sequence",
    "last_event_hash",
    "feedback_traversals",
    "nodes",
}
_NODE_STATE_FIELDS = {
    "state",
    "attempts",
    "generation",
    "lease",
    "approval",
    "completed_idempotency_keys",
    "evidence",
    "last_result",
}
_APPROVAL_FIELDS = {
    "decision",
    "run_id",
    "graph_checksum",
    "node_id",
    "gate_node_id",
    "approver",
    "evidence_digest",
    "approved_at",
}
_LEASE_FIELDS = {"owner", "issued_at", "expires_at", "idempotency_key"}
_NODE_STATES = {
    "planned",
    "ready",
    "blocked",
    "running",
    "verifying",
    "verified",
    "invalidated",
    "failed",
    "needs_human",
    "cancelled",
}


def _is_checksum(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _runtime_errors(
    runtime: Any,
    graph: Any,
    *,
    expected_schema_version: int,
) -> list[str]:
    errors: list[str] = []
    if not isinstance(runtime, dict):
        return ["runtime must be an object"]
    missing = sorted(_RUNTIME_FIELDS - set(runtime))
    unknown = sorted(set(runtime) - _RUNTIME_FIELDS)
    errors.extend(f"runtime missing field: {field}" for field in missing)
    errors.extend(f"runtime unknown field: {field}" for field in unknown)
    if runtime.get("schema_version") != expected_schema_version:
        errors.append(
            f"runtime schema_version must equal {expected_schema_version}"
        )
    run_id = runtime.get("run_id")
    try:
        uuid.UUID(run_id)
    except (AttributeError, TypeError, ValueError):
        errors.append("runtime run_id must be a UUID")
    for field in ("run_version", "graph_version", "last_event_sequence"):
        value = runtime.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            errors.append(f"runtime {field} must be an integer >= 1")
    if not _is_checksum(runtime.get("graph_checksum")):
        errors.append("runtime graph_checksum must be a SHA-256 digest")
    if not _is_checksum(runtime.get("last_event_hash")):
        errors.append("runtime last_event_hash must be a SHA-256 digest")
    if runtime.get("status") not in {
        "active",
        "running",
        "needs_human",
        "completed",
        "cancelled",
    }:
        errors.append("runtime status is invalid")
    for field in ("created_at", "updated_at", "graph_id"):
        if not isinstance(runtime.get(field), str) or not runtime[field].strip():
            errors.append(f"runtime {field} must be non-empty text")
    feedback = runtime.get("feedback_traversals")
    if not isinstance(feedback, dict) or any(
        not isinstance(key, str)
        or isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
        for key, value in (
            feedback.items() if isinstance(feedback, dict) else ()
        )
    ):
        errors.append("runtime feedback_traversals is invalid")
    graph_nodes = {
        node.get("id")
        for node in graph.get("nodes", [])
        if isinstance(graph, dict)
        and isinstance(node, dict)
        and isinstance(node.get("id"), str)
    }
    nodes = runtime.get("nodes")
    if not isinstance(nodes, dict):
        errors.append("runtime nodes must be an object")
        return errors
    if set(nodes) != graph_nodes:
        errors.append(
            "runtime node inventory differs from graph: "
            f"missing={sorted(graph_nodes - set(nodes))} "
            f"extra={sorted(set(nodes) - graph_nodes)}"
        )
    for node_id, state in nodes.items():
        label = f"runtime node {node_id}"
        if not isinstance(state, dict):
            errors.append(f"{label} must be an object")
            continue
        missing_state = sorted(_NODE_STATE_FIELDS - set(state))
        unknown_state = sorted(set(state) - _NODE_STATE_FIELDS)
        errors.extend(f"{label} missing field: {field}" for field in missing_state)
        errors.extend(f"{label} unknown field: {field}" for field in unknown_state)
        if state.get("state") not in _NODE_STATES:
            errors.append(f"{label} state is invalid")
        for field, minimum in (("attempts", 0), ("generation", 1)):
            value = state.get(field)
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value < minimum
            ):
                errors.append(f"{label} {field} must be >= {minimum}")
        lease = state.get("lease")
        if lease is not None:
            if not isinstance(lease, dict) or set(lease) != _LEASE_FIELDS:
                errors.append(f"{label} lease schema is invalid")
            elif any(
                not isinstance(lease.get(field), str)
                or not lease[field].strip()
                for field in _LEASE_FIELDS
            ):
                errors.append(f"{label} lease fields must be non-empty text")
        approval = state.get("approval")
        if approval is not None:
            if not isinstance(approval, dict) or set(approval) != _APPROVAL_FIELDS:
                errors.append(f"{label} approval schema is invalid")
            else:
                if approval.get("decision") != "approved":
                    errors.append(f"{label} approval decision is invalid")
                if approval.get("run_id") != run_id:
                    errors.append(f"{label} approval run_id differs")
                if approval.get("node_id") != node_id:
                    errors.append(f"{label} approval node_id differs")
                if approval.get("gate_node_id") not in graph_nodes:
                    errors.append(f"{label} approval gate_node_id is unknown")
                if not _is_checksum(approval.get("graph_checksum")):
                    errors.append(f"{label} approval graph checksum is invalid")
                if not _is_checksum(approval.get("evidence_digest")):
                    errors.append(f"{label} approval evidence digest is invalid")
                for field in ("approver", "approved_at"):
                    if (
                        not isinstance(approval.get(field), str)
                        or not approval[field].strip()
                    ):
                        errors.append(
                            f"{label} approval {field} must be non-empty text"
                        )
        for field in ("completed_idempotency_keys", "evidence"):
            if not isinstance(state.get(field), list):
                errors.append(f"{label} {field} must be an array")
        keys = state.get("completed_idempotency_keys")
        if isinstance(keys, list) and (
            len(keys) != len(set(keys))
            or any(not isinstance(item, str) or not item.strip() for item in keys)
        ):
            errors.append(
                f"{label} completed_idempotency_keys must be unique non-empty text"
            )
        evidence = state.get("evidence")
        if isinstance(evidence, list) and any(
            not isinstance(item, dict) for item in evidence
        ):
            errors.append(f"{label} evidence entries must be objects")
        if state.get("last_result") is not None and not isinstance(
            state.get("last_result"), dict
        ):
            errors.append(f"{label} last_result must be an object or null")
    if isinstance(graph, dict):
        if runtime.get("graph_id") != graph.get("graph_id"):
            errors.append("runtime graph_id does not match graph")
        if runtime.get("graph_version") != graph.get("graph_version"):
            errors.append("runtime graph_version does not match graph")
        if runtime.get("graph_checksum") != canonical_checksum(graph):
            errors.append("runtime graph checksum does not match graph")
    return errors


def _manifest_errors(
    manifest: Any,
    graph: Any,
    *,
    source_version: int,
) -> list[str]:
    errors: list[str] = []
    if not isinstance(manifest, dict):
        return ["manifest must be an object"]
    current = copy.deepcopy(manifest)
    if source_version == 0:
        current = _manifest_v0_to_v1(current, {})
    if not _validate_manifest_shape(current, errors):
        return [f"manifest: {error}" for error in errors]
    if isinstance(graph, dict):
        if current.get("graph_id") != graph.get("graph_id"):
            errors.append("graph_id does not match graph")
        if current.get("graph_version") != graph.get("graph_version"):
            errors.append("graph_version does not match graph")
        if (
            current.get("context_version")
            != graph.get("project", {}).get("context_version")
        ):
            errors.append("context_version does not match graph")
    return [f"manifest: {error}" for error in errors]


def _context_errors(record: Any, *, expected_version: int) -> list[str]:
    if not isinstance(record, dict):
        return ["context record must be an object"]
    if record.get("schema_version") != expected_version:
        return ["context record schema_version is invalid"]
    claimed = record.get("digest")
    body = {key: value for key, value in record.items() if key != "digest"}
    if not isinstance(claimed, str) or claimed != sha256_json(body):
        return ["context record digest is invalid"]
    if expected_version == 0:
        if not isinstance(record.get("record_id"), str):
            return ["context predecessor requires record_id"]
    else:
        if set(record) != {
            "schema_version",
            "record_id",
            "predecessor_digest",
            "sealed_source",
            "digest",
        }:
            return ["context successor schema is invalid"]
        if not _is_checksum(record.get("predecessor_digest")):
            return ["context successor predecessor digest is invalid"]
        sealed = record.get("sealed_source")
        if not isinstance(sealed, dict) or sealed.get("digest") != record.get(
            "predecessor_digest"
        ):
            return ["context successor does not preserve sealed source"]
    return []


def _runtime_v0_to_v1(
    document: dict[str, Any],
    context: dict[str, Any],
) -> dict[str, Any]:
    candidate = copy.deepcopy(document)
    candidate["schema_version"] = 1
    return _rebind_runtime(candidate, context)


def _rebind_runtime(
    document: dict[str, Any],
    context: dict[str, Any],
) -> dict[str, Any]:
    """Bind inactive runtime state to the migrated definition at any schema version."""

    active = _active_runtime_nodes(document)
    if active:
        raise MigrationError(
            "runtime migration refuses active node(s): " + ", ".join(active)
        )
    ledger = context.get("ledger")
    ledger_errors = _ledger_errors(ledger, document)
    if ledger_errors:
        raise MigrationError("; ".join(ledger_errors))
    candidate = copy.deepcopy(document)
    new_checksum = context.get("graph_checksum")
    new_version = context.get("graph_version")
    if not isinstance(new_checksum, str) or len(new_checksum) != 64:
        raise MigrationError("runtime migration requires candidate graph checksum")
    if isinstance(new_version, bool) or not isinstance(new_version, int):
        raise MigrationError("runtime migration requires candidate graph version")
    old_checksum = candidate.get("graph_checksum")
    candidate["graph_checksum"] = new_checksum
    candidate["graph_version"] = new_version
    if old_checksum != new_checksum:
        for state in candidate["nodes"].values():
            state["approval"] = None
    return candidate


def _runtime_v1_to_v2(
    document: dict[str, Any],
    context: dict[str, Any],
) -> dict[str, Any]:
    """Convert one inactive whole-graph v1 run into an explicit v2 projection."""

    conversion = context.get("runtime_conversion")
    graph = context.get("graph")
    if not isinstance(conversion, dict) or set(conversion) != {
        "outcome_contract",
        "projection",
        "projection_path",
        "actor",
        "authority",
    }:
        raise MigrationError("runtime v1 to v2 requires explicit runtime conversion")
    if not isinstance(graph, dict):
        raise MigrationError("runtime conversion requires the durable graph")
    contract = conversion.get("outcome_contract")
    projection = conversion.get("projection")
    actor = conversion.get("actor")
    authority = conversion.get("authority")
    projection_path = conversion.get("projection_path")
    if not isinstance(actor, str) or not actor.strip():
        raise MigrationError("runtime conversion actor must be non-empty text")
    if not isinstance(authority, str) or not authority.strip():
        raise MigrationError("runtime conversion authority must be non-empty text")
    if not isinstance(contract, dict) or not isinstance(projection, dict):
        raise MigrationError("runtime conversion contract and projection must be objects")
    if contract.get("actor") != actor:
        raise MigrationError("runtime conversion actor does not match outcome contract actor")
    provenance = projection.get("provenance")
    if not isinstance(provenance, dict) or provenance.get("actor") != actor:
        raise MigrationError("runtime conversion actor does not match projection provenance")
    if provenance.get("authority") != authority:
        raise MigrationError("runtime conversion authority does not match projection provenance")
    evidence = contract.get("authority_evidence")
    if not isinstance(evidence, list) or authority not in evidence:
        raise MigrationError("runtime conversion authority is not bound by the outcome contract")
    expected_projection_path = (
        f"projections/{document.get('run_id')}-v"
        f"{projection.get('projection_version')}.json"
    )
    try:
        normalized_projection_path = validate_relative_path(
            projection_path,
            label="runtime conversion projection path",
            allow_dot=False,
        )
    except (PathContractError, TypeError) as exc:
        raise MigrationError(str(exc)) from exc
    if normalized_projection_path != expected_projection_path:
        raise MigrationError("runtime conversion projection path is not deterministic")
    projection_errors = projection_contract_errors(
        graph,
        contract,
        projection,
        expected_graph_checksum=canonical_checksum(graph),
        expected_run_id=document.get("run_id"),
    )
    if projection_errors:
        raise MigrationError("runtime conversion projection is invalid: " + "; ".join(projection_errors))
    active = _active_runtime_nodes(document)
    if active:
        raise MigrationError(
            "runtime conversion refuses active node(s): " + ", ".join(active)
        )
    retained = {
        node.get("id")
        for node in projection.get("nodes", [])
        if isinstance(node, dict) and isinstance(node.get("id"), str)
    }
    predecessors = scheduling_predecessors(graph)
    for node_id in sorted(retained):
        pending = list(predecessors.get(node_id, set()))
        visited: set[str] = set()
        while pending:
            predecessor = pending.pop()
            if predecessor in visited:
                continue
            visited.add(predecessor)
            if predecessor not in retained:
                raise MigrationError(
                    "runtime conversion projection omits prerequisite "
                    f"{predecessor} required by {node_id}"
                )
            pending.extend(predecessors.get(predecessor, set()))
    nodes = document.get("nodes")
    if not isinstance(nodes, dict):
        raise MigrationError("runtime conversion nodes must be an object")
    excluded = sorted(set(nodes) - retained)
    for node_id in excluded:
        state = nodes[node_id]
        pristine = (
            isinstance(state, dict)
            and state.get("state") in {"planned", "ready", "blocked"}
            and state.get("attempts") == 0
            and state.get("generation") == 1
            and state.get("lease") is None
            and state.get("approval") is None
            and state.get("completed_idempotency_keys") == []
            and state.get("evidence") == []
            and state.get("last_result") is None
        )
        if not pristine:
            raise MigrationError(
                f"runtime conversion excluded node is not pristine: {node_id}"
            )
    return {
        "schema_version": 2,
        "run_id": document["run_id"],
        "run_version": document["run_version"],
        "graph_id": document["graph_id"],
        "observed_graph_version": graph["graph_version"],
        "observed_graph_checksum": canonical_checksum(graph),
        "outcome_contract": copy.deepcopy(contract),
        "projection": {
            "version": projection["projection_version"],
            "path": normalized_projection_path,
            "checksum": sha256_json(projection),
        },
        "episodes": {},
        "status": document["status"],
        "created_at": document["created_at"],
        "updated_at": document["updated_at"],
        "last_event_sequence": document["last_event_sequence"],
        "last_event_hash": document["last_event_hash"],
        "feedback_traversals": copy.deepcopy(document["feedback_traversals"]),
        "nodes": {node_id: copy.deepcopy(nodes[node_id]) for node_id in nodes if node_id in retained},
    }


_TRANSFORMS: dict[tuple[str, int, int], _Transform] = {
    ("graph", 0, 1): _graph_v0_to_v1,
    ("manifest", 0, 1): _manifest_v0_to_v1,
    ("context", 0, 1): _context_v0_to_v1,
    ("runtime", 0, 1): _runtime_v0_to_v1,
    ("runtime", 1, 2): _runtime_v1_to_v2,
}


def migrate_document(
    kind: str,
    document: dict[str, Any],
    from_version: int,
    to_version: int,
    context: dict[str, Any],
) -> dict[str, Any]:
    """Apply every registered consecutive step without mutating the source."""

    registry = _load_registry()
    if kind not in registry["kinds"]:
        raise MigrationError(f"unknown migration kind: {kind}")
    if not isinstance(document, dict):
        raise MigrationError(f"{kind} document must be an object")
    if isinstance(from_version, bool) or not isinstance(from_version, int):
        raise MigrationError("from_version must be an integer")
    if isinstance(to_version, bool) or not isinstance(to_version, int):
        raise MigrationError("to_version must be an integer")
    current = registry["kinds"][kind]["current_version"]
    if from_version > current or to_version > current:
        raise MigrationError(
            f"{kind} schema version is a future version; current is {current}"
        )
    if to_version < from_version:
        raise MigrationError("forward migration target cannot precede its source")
    if document.get("schema_version") != from_version:
        raise MigrationError(
            f"{kind} document schema_version does not match from_version"
        )
    candidate = copy.deepcopy(document)
    version = from_version
    while version < to_version:
        transform = _TRANSFORMS.get((kind, version, version + 1))
        if transform is None:
            raise MigrationError(
                f"missing migration step for {kind} {version} -> {version + 1}"
            )
        candidate = transform(candidate, copy.deepcopy(context))
        if candidate.get("schema_version") != version + 1:
            raise MigrationError(
                f"{kind} migration {version} -> {version + 1} emitted wrong version"
            )
        version += 1
    return candidate


def restore_document(
    kind: str,
    migrated: dict[str, Any],
    context: dict[str, Any],
) -> RestoredArtifact:
    """Restore exact source bytes/mode after binding to the reviewed candidate."""

    if kind not in _KINDS:
        raise MigrationError(f"unknown migration kind: {kind}")
    source_bytes = context.get("source_bytes")
    candidate_bytes = context.get("candidate_bytes")
    source_mode = context.get("source_mode")
    candidate_sha256 = context.get("candidate_sha256")
    if not isinstance(source_bytes, bytes):
        raise MigrationError("restore requires exact source_bytes")
    if not isinstance(candidate_bytes, bytes):
        raise MigrationError("restore requires exact candidate_bytes")
    if (
        isinstance(source_mode, bool)
        or not isinstance(source_mode, int)
        or source_mode < 0
        or source_mode > 0o777
    ):
        raise MigrationError("restore requires a valid source_mode")
    if (
        not isinstance(candidate_sha256, str)
        or candidate_sha256 != hashlib.sha256(candidate_bytes).hexdigest()
    ):
        raise MigrationError("restore candidate checksum binding is stale")
    try:
        parsed_candidate = json.loads(candidate_bytes.decode("utf-8"))
        parsed_source = json.loads(source_bytes.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise MigrationError(f"restore snapshot is not valid JSON: {exc}") from exc
    if parsed_candidate != migrated:
        raise MigrationError("restore candidate bytes do not encode migrated document")
    if not isinstance(parsed_source, dict):
        raise MigrationError("restore source bytes must encode an object")
    from_version = context.get("from_version")
    to_version = context.get("to_version")
    registry = _load_registry()
    if (
        isinstance(from_version, bool)
        or not isinstance(from_version, int)
        or isinstance(to_version, bool)
        or not isinstance(to_version, int)
        or to_version <= from_version
    ):
        raise MigrationError("restore version boundary is invalid")
    for step in registry["kinds"][kind]["steps"]:
        if from_version <= step["from_version"] and step["to_version"] <= to_version:
            if not step["reversible"]:
                raise MigrationError(
                    f"{kind} migration crosses irreversible boundary "
                    f"{step['irreversible_boundary']}"
                )
    if parsed_source.get("schema_version") != from_version:
        raise MigrationError("restore source bytes have the wrong schema version")
    expected_migrated = migrate_document(
        kind,
        parsed_source,
        from_version,
        to_version,
        {},
    )
    if expected_migrated != migrated:
        raise MigrationError(
            "restore source bytes do not reproduce the bound migrated document"
        )
    return RestoredArtifact(content=source_bytes, mode=source_mode)


def _append_migration_event(
    runtime: dict[str, Any],
    ledger: list[dict[str, Any]],
    source_versions: dict[str, int],
    target_versions: dict[str, int],
    *,
    runtime_conversion: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    candidate_runtime = copy.deepcopy(runtime)
    candidate_ledger = copy.deepcopy(ledger)
    last = candidate_ledger[-1]
    graph_checksum = (
        candidate_runtime.get("observed_graph_checksum")
        if candidate_runtime.get("schema_version") == 2
        else candidate_runtime.get("graph_checksum")
    )
    event: dict[str, Any] = {
        "sequence": last["sequence"] + 1,
        "previous_hash": last["event_hash"],
        "timestamp": last.get("timestamp", "migration"),
        "run_id": candidate_runtime["run_id"],
        "graph_id": candidate_runtime["graph_id"],
        "graph_checksum": graph_checksum,
        "run_version_before": candidate_runtime["run_version"],
        "run_version_after": candidate_runtime["run_version"] + 1,
        "actor": (
            runtime_conversion["actor"]
            if runtime_conversion is not None
            else "schema-migration"
        ),
        "action": "schema-migrated",
        "irreversible_boundary": "runtime-ledger-append",
        "source_versions": dict(sorted(source_versions.items())),
        "target_versions": dict(sorted(target_versions.items())),
    }
    if runtime_conversion is not None:
        contract = runtime_conversion["outcome_contract"]
        projection = runtime_conversion["projection"]
        event.update(
            {
                "authority": runtime_conversion["authority"],
                "projection_checksum": sha256_json(projection),
                "outcome_contract_digest": sha256_json(contract),
                "outcome_digest": contract["outcome_digest"],
            }
        )
    event["event_hash"] = sha256_json(event)
    candidate_ledger.append(event)
    candidate_runtime["run_version"] = event["run_version_after"]
    candidate_runtime["updated_at"] = event["timestamp"]
    candidate_runtime["last_event_sequence"] = event["sequence"]
    candidate_runtime["last_event_hash"] = event["event_hash"]
    return candidate_runtime, candidate_ledger


def _target_map(target_versions: dict[str, int]) -> dict[str, int]:
    if not isinstance(target_versions, dict) or set(target_versions) != set(_KINDS):
        raise MigrationError(
            "target_versions must contain graph, manifest, context, and runtime"
        )
    result: dict[str, int] = {}
    for kind in _KINDS:
        value = target_versions[kind]
        if isinstance(value, bool) or not isinstance(value, int):
            raise MigrationError(f"target version for {kind} must be an integer")
        result[kind] = value
    return result


def _source_versions(bundle: dict[str, Any]) -> dict[str, int]:
    contexts = bundle.get("contexts")
    if not isinstance(contexts, list) or not contexts:
        raise MigrationError("migration bundle contexts must be a non-empty array")
    versions = {
        "graph": bundle.get("graph", {}).get("schema_version"),
        "manifest": bundle.get("manifest", {}).get("schema_version"),
        "context": contexts[0].get("schema_version")
        if isinstance(contexts[0], dict)
        else None,
        "runtime": bundle.get("runtime", {}).get("schema_version"),
    }
    if any(isinstance(value, bool) or not isinstance(value, int) for value in versions.values()):
        raise MigrationError("migration bundle source versions are invalid")
    if any(
        not isinstance(record, dict)
        or record.get("schema_version") != versions["context"]
        for record in contexts
    ):
        raise MigrationError("migration bundle context versions must agree")
    return versions  # type: ignore[return-value]


def _bundle_shape(bundle: Any) -> dict[str, Any]:
    if not isinstance(bundle, dict):
        raise MigrationError("migration bundle must be an object")
    required = {"graph", "manifest", "contexts", "runtime", "ledger"}
    if set(bundle) != required:
        raise MigrationError(
            "migration bundle must contain only graph, manifest, contexts, runtime, and ledger"
        )
    for key in ("graph", "manifest", "runtime"):
        if not isinstance(bundle.get(key), dict):
            raise MigrationError(f"migration bundle {key} must be an object")
    _canonical(bundle)
    return copy.deepcopy(bundle)


def _cross_errors(candidate: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    graph = candidate.get("graph")
    manifest = candidate.get("manifest")
    runtime = candidate.get("runtime")
    ledger = candidate.get("ledger")
    report = validate_graph(graph)
    errors.extend(f"graph: {error}" for error in report["errors"])
    if isinstance(graph, dict) and isinstance(manifest, dict):
        errors.extend(_manifest_errors(manifest, graph, source_version=1))
    if isinstance(runtime, dict) and isinstance(graph, dict):
        errors.extend(
            _runtime_errors(
                runtime,
                graph,
                expected_schema_version=1,
            )
        )
        try:
            active = _active_runtime_nodes(runtime)
        except MigrationError as exc:
            errors.append(str(exc))
        else:
            if active:
                errors.append("runtime contains active node(s): " + ", ".join(active))
    errors.extend(_ledger_errors(ledger, runtime if isinstance(runtime, dict) else None))
    contexts = candidate.get("contexts")
    successors = candidate.get("context_successors")
    if not isinstance(contexts, list) or not contexts:
        errors.append("context history must be a non-empty array")
    elif all(
        isinstance(source, dict) and source.get("schema_version") == 1
        for source in contexts
    ):
        if successors is not None:
            errors.append("current context history must not create self-successors")
        for index, source in enumerate(contexts):
            errors.extend(
                f"context {index}: {error}"
                for error in _context_errors(source, expected_version=1)
            )
    elif not isinstance(successors, list):
        errors.append("context successors must be an array")
    elif len(contexts) != len(successors):
        errors.append("every context predecessor requires one successor")
    else:
        for index, (source, successor) in enumerate(zip(contexts, successors)):
            errors.extend(
                f"context predecessor {index}: {error}"
                for error in _context_errors(source, expected_version=0)
            )
            errors.extend(
                f"context successor {index}: {error}"
                for error in _context_errors(successor, expected_version=1)
            )
            if not isinstance(source, dict) or not isinstance(successor, dict):
                errors.append(f"context successor {index} is invalid")
                continue
            if successor.get("sealed_source") != source:
                errors.append(f"context successor {index} does not preserve source")
            if successor.get("predecessor_digest") != source.get("digest"):
                errors.append(f"context successor {index} predecessor digest differs")
            body = {
                key: value for key, value in successor.items() if key != "digest"
            }
            if successor.get("digest") != sha256_json(body):
                errors.append(f"context successor {index} digest is invalid")
    return errors


def _conversion_cross_errors(
    candidate: dict[str, Any],
    source: dict[str, Any],
) -> list[str]:
    errors: list[str] = []
    expected_keys = {"graph", "manifest", "contexts", "runtime", "ledger", "projection"}
    if set(candidate) != expected_keys:
        errors.append("runtime conversion candidate fields are invalid")
    for field in ("graph", "manifest", "contexts"):
        if candidate.get(field) != source.get(field):
            errors.append(f"runtime conversion rewrote source {field}")
    graph = candidate.get("graph")
    runtime = candidate.get("runtime")
    projection = candidate.get("projection")
    if not isinstance(graph, dict) or not isinstance(runtime, dict) or not isinstance(projection, dict):
        return errors + ["runtime conversion candidate payloads are invalid"]
    contract = runtime.get("outcome_contract")
    if not isinstance(contract, dict):
        errors.append("runtime conversion outcome contract is invalid")
        return errors
    errors.extend(
        projection_contract_errors(
            graph,
            contract,
            projection,
            expected_graph_checksum=canonical_checksum(graph),
            expected_run_id=runtime.get("run_id"),
        )
    )
    required_runtime = {
        "schema_version",
        "run_id",
        "run_version",
        "graph_id",
        "observed_graph_version",
        "observed_graph_checksum",
        "outcome_contract",
        "projection",
        "episodes",
        "status",
        "created_at",
        "updated_at",
        "last_event_sequence",
        "last_event_hash",
        "feedback_traversals",
        "nodes",
    }
    if set(runtime) != required_runtime or runtime.get("schema_version") != 2:
        errors.append("runtime conversion emitted an invalid schema-v2 runtime shape")
    if runtime.get("graph_id") != graph.get("graph_id"):
        errors.append("runtime conversion graph ID differs")
    if runtime.get("observed_graph_version") != graph.get("graph_version"):
        errors.append("runtime conversion observed graph version differs")
    if runtime.get("observed_graph_checksum") != canonical_checksum(graph):
        errors.append("runtime conversion observed graph checksum differs")
    binding = runtime.get("projection")
    expected_binding = {
        "version": projection.get("projection_version"),
        "path": (
            f"projections/{runtime.get('run_id')}-v"
            f"{projection.get('projection_version')}.json"
        ),
        "checksum": sha256_json(projection),
    }
    if binding != expected_binding:
        errors.append("runtime conversion projection binding differs")
    projected_ids = {
        node.get("id")
        for node in projection.get("nodes", [])
        if isinstance(node, dict) and isinstance(node.get("id"), str)
    }
    nodes = runtime.get("nodes")
    if not isinstance(nodes, dict) or set(nodes) != projected_ids:
        errors.append("runtime conversion node inventory differs from projection")
    elif isinstance(source.get("runtime"), dict):
        source_nodes = source["runtime"].get("nodes", {})
        for node_id, state in nodes.items():
            if state != source_nodes.get(node_id):
                errors.append(f"runtime conversion rewrote retained node state: {node_id}")
    ledger = candidate.get("ledger")
    source_ledger = source.get("ledger")
    if not isinstance(ledger, list) or not isinstance(source_ledger, list):
        errors.append("runtime conversion ledger is invalid")
    else:
        if ledger[:-1] != source_ledger or len(ledger) != len(source_ledger) + 1:
            errors.append("runtime conversion did not preserve the exact ledger prefix")
        errors.extend(_ledger_errors(ledger, runtime))
        if ledger:
            boundary = ledger[-1]
            if (
                boundary.get("action") != "schema-migrated"
                or boundary.get("source_versions", {}).get("runtime") != 1
                or boundary.get("target_versions", {}).get("runtime") != 2
                or boundary.get("projection_checksum") != sha256_json(projection)
                or boundary.get("outcome_contract_digest") != sha256_json(contract)
                or boundary.get("outcome_digest") != contract.get("outcome_digest")
            ):
                errors.append("runtime conversion boundary event is invalid")
    return errors


def plan_migration(
    bundle: dict[str, Any],
    target_versions: dict[str, int],
    *,
    runtime_conversion: dict[str, Any] | None = None,
) -> MigrationPlan:
    """Plan the full graph -> manifest -> context -> runtime migration order."""

    source_digest = ""
    targets: dict[str, int] = {}
    sources: dict[str, int] = {}
    steps: list[dict[str, Any]] = []
    receipt = {
        "restore_strategy": "exact-source-bytes-and-mode",
        "irreversible_boundaries": [],
    }
    try:
        source = _bundle_shape(bundle)
        source_digest = _digest(source)
        targets = _target_map(target_versions)
        sources = _source_versions(source)
        registry = _load_registry()
        conversion_requested = (
            sources == {"graph": 1, "manifest": 1, "context": 1, "runtime": 1}
            and targets == {"graph": 1, "manifest": 1, "context": 1, "runtime": 2}
        )
        if targets.get("runtime") == 2 and not conversion_requested:
            raise MigrationError(
                "runtime v1 to v2 conversion requires the exact 1/1/1/1 to 1/1/1/2 matrix"
            )
        if conversion_requested and runtime_conversion is None:
            raise MigrationError(
                "runtime v1 to v2 requires explicit runtime conversion"
            )
        if not conversion_requested and runtime_conversion is not None:
            raise MigrationError(
                "explicit runtime conversion is valid only for runtime v1 to v2"
            )
        for kind in _KINDS:
            current = registry["kinds"][kind]["current_version"]
            if sources[kind] > current or targets[kind] > current:
                raise MigrationError(
                    f"{kind} schema version is a future version; current is {current}"
                )
            if targets[kind] < sources[kind]:
                raise MigrationError(f"{kind} target precedes source version")
            for version in range(sources[kind], targets[kind]):
                transform = _TRANSFORMS.get((kind, version, version + 1))
                if transform is None:
                    raise MigrationError(
                        f"missing migration step for {kind} {version} -> {version + 1}"
                    )
                record = next(
                    item
                    for item in registry["kinds"][kind]["steps"]
                    if item["from_version"] == version
                )
                step = {
                    "kind": kind,
                    "from_version": version,
                    "to_version": version + 1,
                    "reversible": record["reversible"],
                    "irreversible_boundary": record["irreversible_boundary"],
                }
                steps.append(step)
                if record["irreversible_boundary"] is not None:
                    receipt["irreversible_boundaries"].append(
                        record["irreversible_boundary"]
                    )

        source_graph = migrate_document(
            "graph",
            source["graph"],
            sources["graph"],
            registry["kinds"]["graph"]["current_version"],
            {},
        )
        source_graph_report = validate_graph(source_graph)
        if not source_graph_report["valid"]:
            raise MigrationError(
                "source graph schema is invalid: "
                + "; ".join(source_graph_report["errors"])
            )
        source_manifest_errors = _manifest_errors(
            source["manifest"],
            source_graph,
            source_version=sources["manifest"],
        )
        if source_manifest_errors:
            raise MigrationError("; ".join(source_manifest_errors))
        source_runtime_errors = _runtime_errors(
            source["runtime"],
            source["graph"],
            expected_schema_version=sources["runtime"],
        )
        if source_runtime_errors:
            raise MigrationError("; ".join(source_runtime_errors))
        for index, record in enumerate(source["contexts"]):
            context_errors = _context_errors(
                record,
                expected_version=sources["context"],
            )
            if context_errors:
                raise MigrationError(
                    f"context source {index}: " + "; ".join(context_errors)
                )
        ledger_errors = _ledger_errors(source["ledger"], source["runtime"])
        if ledger_errors:
            raise MigrationError("; ".join(ledger_errors))

        candidate = copy.deepcopy(source)
        candidate["graph"] = migrate_document(
            "graph",
            source["graph"],
            sources["graph"],
            targets["graph"],
            {},
        )
        graph_report = validate_graph(candidate["graph"])
        if not graph_report["valid"]:
            raise MigrationError(
                "graph candidate validation failed: "
                + "; ".join(graph_report["errors"])
            )
        candidate["manifest"] = migrate_document(
            "manifest",
            source["manifest"],
            sources["manifest"],
            targets["manifest"],
            {"graph": candidate["graph"]},
        )
        candidate_manifest_errors = _manifest_errors(
            candidate["manifest"],
            candidate["graph"],
            source_version=1,
        )
        if candidate_manifest_errors:
            raise MigrationError("; ".join(candidate_manifest_errors))
        if sources["context"] < targets["context"]:
            candidate["context_successors"] = [
                migrate_document(
                    "context",
                    record,
                    sources["context"],
                    targets["context"],
                    {"graph": candidate["graph"]},
                )
                for record in source["contexts"]
            ]
        else:
            candidate.pop("context_successors", None)
        candidate["runtime"] = migrate_document(
            "runtime",
            source["runtime"],
            sources["runtime"],
            targets["runtime"],
            {
                "graph_checksum": canonical_checksum(candidate["graph"]),
                "graph_version": candidate["graph"]["graph_version"],
                "ledger": source["ledger"],
                "graph": candidate["graph"],
                "runtime_conversion": runtime_conversion,
            },
        )
        if not conversion_requested:
            candidate["runtime"] = _rebind_runtime(
                candidate["runtime"],
                {
                    "graph_checksum": canonical_checksum(candidate["graph"]),
                    "graph_version": candidate["graph"]["graph_version"],
                    "ledger": source["ledger"],
                },
            )
        if steps:
            candidate["runtime"], candidate["ledger"] = _append_migration_event(
                candidate["runtime"],
                source["ledger"],
                sources,
                targets,
                runtime_conversion=runtime_conversion if conversion_requested else None,
            )
            if conversion_requested:
                candidate["projection"] = copy.deepcopy(
                    runtime_conversion["projection"]
                )
            if "runtime-ledger-append" not in receipt["irreversible_boundaries"]:
                receipt["irreversible_boundaries"].append(
                    "runtime-ledger-append"
                )
        else:
            candidate["ledger"] = copy.deepcopy(source["ledger"])
        errors = (
            _conversion_cross_errors(candidate, source)
            if conversion_requested
            else _cross_errors(candidate)
        )
        if errors:
            raise MigrationError("; ".join(errors))
        if conversion_requested:
            receipt.update(
                {
                    "required_actor": runtime_conversion["actor"],
                    "required_authority": runtime_conversion["authority"],
                    "outcome_contract_digest": sha256_json(
                        runtime_conversion["outcome_contract"]
                    ),
                    "projection_checksum": sha256_json(
                        runtime_conversion["projection"]
                    ),
                    "projection_path": runtime_conversion["projection_path"],
                }
            )
        candidate_digest = _digest(candidate)
        receipt["source_digest"] = source_digest
        receipt["candidate_digest"] = candidate_digest
        return MigrationPlan(
            valid=True,
            dry_run=True,
            source_digest=source_digest,
            candidate_digest=candidate_digest,
            source_versions=sources,
            target_versions=targets,
            steps=tuple(steps),
            candidate_bundle=candidate,
            receipt=receipt,
            errors=(),
        )
    except (MigrationError, ValueError, KeyError, StopIteration) as exc:
        return MigrationPlan(
            valid=False,
            dry_run=True,
            source_digest=source_digest,
            candidate_digest="",
            source_versions=sources,
            target_versions=targets,
            steps=tuple(steps),
            candidate_bundle={},
            receipt=receipt,
            errors=(str(exc),),
        )


def validate_migration_plan(
    plan: MigrationPlan,
    bundle: dict[str, Any],
    *,
    runtime_conversion: dict[str, Any] | None = None,
) -> tuple[str, ...]:
    """Revalidate source identity, candidate integrity, and ordered invariants."""

    errors: list[str] = []
    try:
        source = _bundle_shape(bundle)
    except MigrationError as exc:
        return (str(exc),)
    if plan.source_digest != _digest(source):
        errors.append("migration source digest is stale")
    if not plan.valid:
        errors.extend(plan.errors)
        return tuple(errors)
    if plan.candidate_digest != _digest(plan.candidate_bundle):
        errors.append("migration candidate digest differs")
    if plan.candidate_bundle.get("contexts") != source.get("contexts"):
        errors.append("migration rewrote sealed context history")
    if plan.target_versions.get("runtime") == 2:
        errors.extend(_conversion_cross_errors(plan.candidate_bundle, source))
    else:
        errors.extend(_cross_errors(plan.candidate_bundle))
    expected = plan_migration(
        source,
        plan.target_versions,
        runtime_conversion=runtime_conversion,
    )
    if not expected.valid:
        errors.append("migration plan no longer reproduces from its source")
        errors.extend(expected.errors)
        return tuple(dict.fromkeys(errors))
    if plan.source_versions != expected.source_versions:
        errors.append("migration source versions differ")
    if plan.target_versions != expected.target_versions:
        errors.append("migration target versions differ")
    if plan.steps != expected.steps:
        errors.append("migration steps differ")
    if plan.receipt != expected.receipt:
        errors.append("migration receipt differs")
    if plan.candidate_bundle != expected.candidate_bundle:
        errors.append("migration candidate differs from ordered replay")
    return tuple(dict.fromkeys(errors))


def _read_json_artifact(
    project_root: Path,
    relative: str,
    *,
    label: str,
) -> tuple[Path, bytes, dict[str, Any]]:
    try:
        normalized = validate_relative_path(relative, label=label, allow_dot=False)
        path = resolve_project_path(project_root, normalized, label=label)
    except (PathContractError, TypeError) as exc:
        raise MigrationError(str(exc)) from exc
    try:
        payload = path.read_bytes()
        value = json.loads(payload.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise MigrationError(f"cannot load {label}: {exc}") from exc
    if not isinstance(value, dict):
        raise MigrationError(f"{label} must contain a JSON object")
    return path, payload, value


def _read_ledger_artifact(
    project_root: Path,
    relative: str,
) -> tuple[Path, bytes, list[dict[str, Any]]]:
    try:
        normalized = validate_relative_path(
            relative,
            label="migration ledger path",
            allow_dot=False,
        )
        path = resolve_project_path(
            project_root,
            normalized,
            label="migration ledger path",
        )
        payload = path.read_bytes()
    except (PathContractError, OSError, TypeError) as exc:
        raise MigrationError(f"cannot load migration ledger: {exc}") from exc
    records: list[dict[str, Any]] = []
    try:
        for line_number, raw in enumerate(
            payload.decode("utf-8").splitlines(),
            start=1,
        ):
            if not raw.strip():
                raise MigrationError(
                    f"migration ledger line {line_number} is blank"
                )
            record = json.loads(raw)
            if not isinstance(record, dict):
                raise MigrationError(
                    f"migration ledger line {line_number} must be an object"
                )
            records.append(record)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise MigrationError(f"migration ledger is invalid: {exc}") from exc
    return path, payload, records


def _portable_descriptor_path(value: Any, *, label: str) -> str:
    if not isinstance(value, str):
        raise MigrationError(f"{label} must be a relative path")
    try:
        return validate_relative_path(value, label=label, allow_dot=False)
    except PathContractError as exc:
        raise MigrationError(str(exc)) from exc


def _evidence_tree_bindings(project_root: Path) -> list[dict[str, Any]]:
    evidence_root = project_root / ".development-graph" / "evidence"
    if evidence_root.is_symlink():
        raise MigrationError("migration evidence root must not be a symlink")
    if not evidence_root.exists():
        return []
    if not evidence_root.is_dir():
        raise MigrationError("migration evidence root must be a directory")
    bindings: list[dict[str, Any]] = []
    for path in sorted(evidence_root.rglob("*"), key=lambda item: item.as_posix()):
        relative = path.relative_to(project_root).as_posix()
        if path.is_symlink():
            raise MigrationError(f"migration evidence tree contains a symlink: {relative}")
        if path.is_dir():
            continue
        if not path.is_file():
            raise MigrationError(f"migration evidence tree contains a non-file: {relative}")
        bindings.append(
            {
                "path": relative,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "mode": stat.S_IMODE(path.stat().st_mode),
            }
        )
    return bindings


def _json_payload(value: dict[str, Any]) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True).encode(
            "utf-8"
        )
        + b"\n"
    )


def _build_v1_lifecycle_migration_request(
    project_root: Path,
    descriptor: dict[str, Any],
) -> tuple[dict[str, Any], MigrationPlan]:
    """Load a relative-path migration descriptor and build an exact-base request."""

    root = Path(project_root).resolve()
    if not root.is_dir():
        raise MigrationError(f"project root is not a directory: {root}")
    if not isinstance(descriptor, dict) or descriptor.get("schema_version") != 1:
        raise MigrationError("migration descriptor schema_version must equal 1")
    required = {"schema_version", "bundle", "outputs", "target_versions"}
    if set(descriptor) != required:
        raise MigrationError(
            "migration descriptor requires only schema_version, bundle, outputs, "
            "and target_versions"
        )
    bundle_paths = descriptor["bundle"]
    outputs = descriptor["outputs"]
    if not isinstance(bundle_paths, dict) or set(bundle_paths) != {
        "graph",
        "manifest",
        "contexts",
        "runtime",
        "ledger",
    }:
        raise MigrationError("migration descriptor bundle fields are invalid")
    if not isinstance(outputs, dict) or set(outputs) != {
        "graph",
        "manifest",
        "context_successors",
        "runtime",
        "ledger",
    }:
        raise MigrationError("migration descriptor output fields are invalid")
    contexts = bundle_paths["contexts"]
    successor_paths = outputs["context_successors"]
    if (
        not isinstance(contexts, list)
        or not contexts
        or not isinstance(successor_paths, list)
    ):
        raise MigrationError(
            "migration descriptor contexts and successor outputs must be arrays"
        )

    graph_path, graph_bytes, graph = _read_json_artifact(
        root,
        _portable_descriptor_path(
            bundle_paths["graph"],
            label="migration graph path",
        ),
        label="migration graph",
    )
    manifest_path, manifest_bytes, manifest = _read_json_artifact(
        root,
        _portable_descriptor_path(
            bundle_paths["manifest"],
            label="migration manifest path",
        ),
        label="migration manifest",
    )
    runtime_path, runtime_bytes, runtime = _read_json_artifact(
        root,
        _portable_descriptor_path(
            bundle_paths["runtime"],
            label="migration runtime path",
        ),
        label="migration runtime",
    )
    ledger_path, ledger_bytes, ledger = _read_ledger_artifact(
        root,
        _portable_descriptor_path(
            bundle_paths["ledger"],
            label="migration ledger path",
        ),
    )
    context_values: list[dict[str, Any]] = []
    context_sources: list[tuple[str, Path, bytes]] = []
    for index, raw in enumerate(contexts):
        relative = _portable_descriptor_path(
            raw,
            label=f"migration context path {index}",
        )
        path, payload, value = _read_json_artifact(
            root,
            relative,
            label=f"migration context {index}",
        )
        context_values.append(value)
        context_sources.append((relative, path, payload))

    source_bundle = {
        "graph": graph,
        "manifest": manifest,
        "contexts": context_values,
        "runtime": runtime,
        "ledger": ledger,
    }
    plan = plan_migration(source_bundle, descriptor["target_versions"])
    if not plan.valid:
        raise MigrationError("migration plan is invalid: " + "; ".join(plan.errors))
    validation_errors = validate_migration_plan(plan, source_bundle)
    if validation_errors:
        raise MigrationError(
            "migration plan validation failed: " + "; ".join(validation_errors)
        )
    successors = plan.candidate_bundle.get("context_successors", [])
    if not isinstance(successors, list) or len(successor_paths) != len(successors):
        raise MigrationError(
            "migration descriptor needs one successor output per planned successor"
        )

    output_graph = _portable_descriptor_path(
        outputs["graph"],
        label="migration graph output",
    )
    output_manifest = _portable_descriptor_path(
        outputs["manifest"],
        label="migration manifest output",
    )
    output_runtime = _portable_descriptor_path(
        outputs["runtime"],
        label="migration runtime output",
    )
    output_ledger = _portable_descriptor_path(
        outputs["ledger"],
        label="migration ledger output",
    )
    source_by_path = {
        _portable_descriptor_path(
            bundle_paths["graph"],
            label="migration graph path",
        ): (graph_path, graph_bytes),
        _portable_descriptor_path(
            bundle_paths["manifest"],
            label="migration manifest path",
        ): (manifest_path, manifest_bytes),
        _portable_descriptor_path(
            bundle_paths["runtime"],
            label="migration runtime path",
        ): (runtime_path, runtime_bytes),
        _portable_descriptor_path(
            bundle_paths["ledger"],
            label="migration ledger path",
        ): (ledger_path, ledger_bytes),
    }
    candidate_ledger = b"".join(
        _canonical(record) + b"\n"
        for record in plan.candidate_bundle["ledger"]
    )
    candidates = [
        (output_graph, _json_payload(plan.candidate_bundle["graph"])),
        (output_manifest, _json_payload(plan.candidate_bundle["manifest"])),
    ]
    candidates.extend(
        (
            _portable_descriptor_path(
                relative,
                label=f"migration context successor output {index}",
            ),
            _json_payload(successor),
        )
        for index, (relative, successor) in enumerate(
            zip(successor_paths, successors)
        )
    )
    candidates.extend(
        [
            (output_runtime, _json_payload(plan.candidate_bundle["runtime"])),
            (output_ledger, candidate_ledger),
        ]
    )
    seen: set[str] = set()
    artifacts: list[dict[str, Any]] = []
    artifact_bindings: list[dict[str, Any]] = []
    for relative, payload in candidates:
        if relative in seen:
            raise MigrationError(f"duplicate migration output path: {relative}")
        seen.add(relative)
        target = resolve_project_path(root, relative, label="migration output")
        record: dict[str, Any] = {
            "path": relative,
            "content": payload,
            "mode": (
                stat.S_IMODE(target.stat().st_mode)
                if target.is_file()
                else 0o600
            ),
        }
        source_record = source_by_path.get(relative)
        if source_record is not None:
            record["expected_sha256"] = hashlib.sha256(
                source_record[1]
            ).hexdigest()
        elif target.exists():
            raise MigrationError(
                f"migration successor output already exists: {relative}"
            )
        artifacts.append(record)
        artifact_bindings.append(
            {
                "path": relative,
                "source_sha256": record.get("expected_sha256"),
                "source_mode": (
                    stat.S_IMODE(target.stat().st_mode)
                    if target.is_file()
                    else None
                ),
                "candidate_sha256": hashlib.sha256(payload).hexdigest(),
                "candidate_mode": record["mode"],
            }
        )
    source_bindings = [
        {
            "path": relative,
            "sha256": hashlib.sha256(payload).hexdigest(),
            "mode": stat.S_IMODE(path.stat().st_mode),
        }
        for relative, path, payload in [
            (
                _portable_descriptor_path(
                    bundle_paths["graph"],
                    label="migration graph path",
                ),
                graph_path,
                graph_bytes,
            ),
            (
                _portable_descriptor_path(
                    bundle_paths["manifest"],
                    label="migration manifest path",
                ),
                manifest_path,
                manifest_bytes,
            ),
            *context_sources,
            (
                _portable_descriptor_path(
                    bundle_paths["runtime"],
                    label="migration runtime path",
                ),
                runtime_path,
                runtime_bytes,
            ),
            (
                _portable_descriptor_path(
                    bundle_paths["ledger"],
                    label="migration ledger path",
                ),
                ledger_path,
                ledger_bytes,
            ),
        ]
    ]
    migration_receipt = {
        **copy.deepcopy(plan.receipt),
        "source_bindings": source_bindings,
        "artifact_bindings": artifact_bindings,
    }
    value = {
            "schema_version": 1,
            "operation": "migration",
            "project_root": root,
            "artifacts": artifacts,
            "metadata": {
                "migration_plan": plan.to_dict(),
                "migration_receipt": migration_receipt,
                "commit_order": [record["path"] for record in artifacts],
            },
        }
    return (
        AuthenticatedMigrationRequest(
            value,
            token=_AUTHENTICATED_REQUEST_TOKEN,
            descriptor=descriptor,
            project_root=root,
        ),
        plan,
    )


def _build_v2_lifecycle_migration_request(
    project_root: Path,
    descriptor: dict[str, Any],
) -> tuple[dict[str, Any], MigrationPlan]:
    root = Path(project_root).resolve()
    if not root.is_dir():
        raise MigrationError(f"project root is not a directory: {root}")
    required = {
        "schema_version",
        "bundle",
        "outputs",
        "target_versions",
        "runtime_conversion",
    }
    if not isinstance(descriptor, dict) or descriptor.get("schema_version") != 2:
        raise MigrationError("runtime conversion descriptor schema_version must equal 2")
    if set(descriptor) != required:
        raise MigrationError(
            "runtime conversion descriptor fields are invalid"
        )
    bundle_paths = descriptor.get("bundle")
    outputs = descriptor.get("outputs")
    conversion_paths = descriptor.get("runtime_conversion")
    if not isinstance(bundle_paths, dict) or set(bundle_paths) != {
        "graph", "manifest", "contexts", "runtime", "ledger"
    }:
        raise MigrationError("runtime conversion bundle fields are invalid")
    if not isinstance(outputs, dict) or set(outputs) != {
        "projection", "runtime", "ledger"
    }:
        raise MigrationError("runtime conversion output fields are invalid")
    if not isinstance(conversion_paths, dict) or set(conversion_paths) != {
        "outcome_contract", "projection", "actor", "authority"
    }:
        raise MigrationError("runtime conversion authority fields are invalid")
    contexts = bundle_paths.get("contexts")
    if not isinstance(contexts, list) or not contexts:
        raise MigrationError("runtime conversion contexts must be a non-empty array")
    graph_relative = _portable_descriptor_path(
        bundle_paths["graph"], label="migration graph path"
    )
    manifest_relative = _portable_descriptor_path(
        bundle_paths["manifest"], label="migration manifest path"
    )
    runtime_relative = _portable_descriptor_path(
        bundle_paths["runtime"], label="migration runtime path"
    )
    ledger_relative = _portable_descriptor_path(
        bundle_paths["ledger"], label="migration ledger path"
    )
    if runtime_relative != ".development-graph/run.json" or ledger_relative != ".development-graph/events.jsonl":
        raise MigrationError("runtime conversion requires the canonical run and ledger paths")
    graph_path, graph_bytes, graph = _read_json_artifact(
        root, graph_relative, label="migration graph"
    )
    manifest_path, manifest_bytes, manifest = _read_json_artifact(
        root, manifest_relative, label="migration manifest"
    )
    runtime_path, runtime_bytes, runtime = _read_json_artifact(
        root, runtime_relative, label="migration runtime"
    )
    ledger_path, ledger_bytes, ledger = _read_ledger_artifact(root, ledger_relative)
    context_values: list[dict[str, Any]] = []
    context_sources: list[tuple[str, Path, bytes]] = []
    for index, raw in enumerate(contexts):
        relative = _portable_descriptor_path(
            raw, label=f"migration context path {index}"
        )
        path, payload, value = _read_json_artifact(
            root, relative, label=f"migration context {index}"
        )
        context_values.append(value)
        context_sources.append((relative, path, payload))
    contract_relative = _portable_descriptor_path(
        conversion_paths["outcome_contract"],
        label="runtime conversion outcome contract path",
    )
    projection_input_relative = _portable_descriptor_path(
        conversion_paths["projection"],
        label="runtime conversion projection input path",
    )
    contract_path, contract_bytes, contract = _read_json_artifact(
        root, contract_relative, label="runtime conversion outcome contract"
    )
    projection_input_path, projection_input_bytes, projection = _read_json_artifact(
        root, projection_input_relative, label="runtime conversion projection"
    )
    actor = conversion_paths.get("actor")
    authority = conversion_paths.get("authority")
    if not isinstance(actor, str) or not actor.strip():
        raise MigrationError("runtime conversion actor must be non-empty text")
    if not isinstance(authority, str) or not authority.strip():
        raise MigrationError("runtime conversion authority must be non-empty text")
    output_projection = _portable_descriptor_path(
        outputs["projection"], label="runtime conversion projection output"
    )
    output_runtime = _portable_descriptor_path(
        outputs["runtime"], label="runtime conversion runtime output"
    )
    output_ledger = _portable_descriptor_path(
        outputs["ledger"], label="runtime conversion ledger output"
    )
    if output_runtime != runtime_relative or output_ledger != ledger_relative:
        raise MigrationError("runtime conversion must replace the bound run and ledger")
    control_prefix = ".development-graph/"
    if not output_projection.startswith(control_prefix + "projections/"):
        raise MigrationError("runtime conversion projection output is outside projections")
    projection_path = output_projection[len(control_prefix):]
    conversion = {
        "outcome_contract": contract,
        "projection": projection,
        "projection_path": projection_path,
        "actor": actor,
        "authority": authority,
    }
    source_bundle = {
        "graph": graph,
        "manifest": manifest,
        "contexts": context_values,
        "runtime": runtime,
        "ledger": ledger,
    }
    plan = plan_migration(
        source_bundle,
        descriptor["target_versions"],
        runtime_conversion=conversion,
    )
    if not plan.valid:
        raise MigrationError("migration plan is invalid: " + "; ".join(plan.errors))
    validation_errors = validate_migration_plan(
        plan, source_bundle, runtime_conversion=conversion
    )
    if validation_errors:
        raise MigrationError(
            "migration plan validation failed: " + "; ".join(validation_errors)
        )
    if not ledger_bytes.endswith(b"\n"):
        raise MigrationError("runtime conversion ledger must end with a newline")
    boundary_bytes = _canonical(plan.candidate_bundle["ledger"][-1]) + b"\n"
    candidate_ledger = ledger_bytes + boundary_bytes
    candidates = [
        (output_projection, _json_payload(plan.candidate_bundle["projection"]), None),
        (output_runtime, _json_payload(plan.candidate_bundle["runtime"]), (runtime_path, runtime_bytes)),
        (output_ledger, candidate_ledger, (ledger_path, ledger_bytes)),
    ]
    artifacts: list[dict[str, Any]] = []
    artifact_bindings: list[dict[str, Any]] = []
    seen: set[str] = set()
    for relative, payload, source_record in candidates:
        if relative in seen:
            raise MigrationError(f"duplicate migration output path: {relative}")
        seen.add(relative)
        target = resolve_project_path(root, relative, label="migration output")
        if source_record is None:
            if target.exists():
                raise MigrationError(
                    f"migration successor output already exists: {relative}"
                )
            mode = 0o600
            expected_sha256 = None
            source_mode = None
        else:
            source_path, source_bytes = source_record
            mode = stat.S_IMODE(source_path.stat().st_mode)
            expected_sha256 = hashlib.sha256(source_bytes).hexdigest()
            source_mode = mode
        record: dict[str, Any] = {
            "path": relative,
            "content": payload,
            "mode": mode,
        }
        if expected_sha256 is not None:
            record["expected_sha256"] = expected_sha256
        artifacts.append(record)
        artifact_bindings.append(
            {
                "path": relative,
                "source_sha256": expected_sha256,
                "source_mode": source_mode,
                "candidate_sha256": hashlib.sha256(payload).hexdigest(),
                "candidate_mode": mode,
            }
        )
    sources = [
        (graph_relative, graph_path, graph_bytes),
        (manifest_relative, manifest_path, manifest_bytes),
        *context_sources,
        (runtime_relative, runtime_path, runtime_bytes),
        (ledger_relative, ledger_path, ledger_bytes),
        (contract_relative, contract_path, contract_bytes),
        (projection_input_relative, projection_input_path, projection_input_bytes),
    ]
    source_bindings = [
        {
            "path": relative,
            "sha256": hashlib.sha256(payload).hexdigest(),
            "mode": stat.S_IMODE(path.stat().st_mode),
        }
        for relative, path, payload in sources
    ]
    evidence_bindings = _evidence_tree_bindings(root)
    migration_receipt = {
        **copy.deepcopy(plan.receipt),
        "source_bindings": source_bindings,
        "evidence_bindings": evidence_bindings,
        "artifact_bindings": artifact_bindings,
    }
    value = {
        "schema_version": 1,
        "operation": "migration",
        "project_root": root,
        "artifacts": artifacts,
        "metadata": {
            "migration_plan": plan.to_dict(),
            "migration_receipt": migration_receipt,
            "commit_order": [record["path"] for record in artifacts],
        },
    }
    return (
        AuthenticatedMigrationRequest(
            value,
            token=_AUTHENTICATED_REQUEST_TOKEN,
            descriptor=descriptor,
            project_root=root,
        ),
        plan,
    )


def build_lifecycle_migration_request(
    project_root: Path,
    descriptor: dict[str, Any],
) -> tuple[dict[str, Any], MigrationPlan]:
    """Build an exact authenticated request for legacy or conversion descriptors."""

    if isinstance(descriptor, dict) and descriptor.get("schema_version") == 2:
        return _build_v2_lifecycle_migration_request(project_root, descriptor)
    return _build_v1_lifecycle_migration_request(project_root, descriptor)


def validate_authenticated_migration_request(
    request: dict[str, Any],
    project_root: Path,
    *,
    revalidate_sources: bool,
) -> tuple[str, ...]:
    """Validate builder authority and optionally rebuild from source under lock."""

    if (
        not isinstance(request, AuthenticatedMigrationRequest)
        or request._authentication_token is not _AUTHENTICATED_REQUEST_TOKEN
    ):
        return (
            "migration request is not authenticated; use "
            "build_lifecycle_migration_request",
        )
    root = Path(project_root).resolve()
    if request._project_root != root or Path(request.get("project_root", "")).resolve() != root:
        return ("authenticated migration project root differs",)
    current_binding = _lifecycle_request_binding(request)
    if (
        not current_binding
        or current_binding != request._request_binding
    ):
        return ("authenticated migration artifact binding differs",)
    artifacts = request.get("artifacts")
    metadata = request.get("metadata")
    if not isinstance(artifacts, list) or not isinstance(metadata, dict):
        return ("authenticated migration request shape is invalid",)
    order = metadata.get("commit_order")
    actual_order = [
        record.get("path") if isinstance(record, dict) else None
        for record in artifacts
    ]
    if order != actual_order:
        return ("authenticated migration commit order differs",)
    if not revalidate_sources:
        return ()
    try:
        rebuilt, _ = build_lifecycle_migration_request(
            root,
            request._descriptor,
        )
    except MigrationError as exc:
        return (f"authenticated migration source revalidation failed: {exc}",)
    if _lifecycle_request_binding(rebuilt) != request._request_binding:
        return ("authenticated migration source or candidate binding is stale",)
    if rebuilt.get("metadata") != request.get("metadata"):
        return ("authenticated migration plan evidence differs",)
    return ()
