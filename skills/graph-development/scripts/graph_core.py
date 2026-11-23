"""Durable primitives shared by the graph-development control-plane tools."""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import uuid
import tempfile
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from path_contract import resolve_project_path


SCHEDULING_EDGE_TYPES = {"requires", "consumes", "validates", "approves", "blocks"}
INVALIDATION_EDGE_TYPES = SCHEDULING_EDGE_TYPES | {"invalidates"}
SLUG_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
CHECKSUM_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class GraphControlError(RuntimeError):
    """Base error for graph control-plane operations."""


class LockTimeout(GraphControlError):
    """Raised when an exclusive project runtime lock cannot be acquired."""


class StaleStateError(GraphControlError):
    """Raised when a compare-and-swap version or checksum is stale."""


class RuntimeStateError(GraphControlError):
    """Raised for an illegal runtime-state transition."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GraphControlError(f"cannot load JSON from {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise GraphControlError(f"JSON root in {path} must be an object")
    return value


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_write_bytes(path: Path, payload: bytes, *, mode: int = 0o600) -> None:
    """Replace *path* only after the complete same-directory temporary file is durable."""

    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except BaseException:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


def atomic_write_json(path: Path, value: Any) -> None:
    atomic_write_bytes(path, json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8") + b"\n")


def atomic_write_batch(writes: list[tuple[Path, bytes, int]]) -> None:
    """Apply a file batch and restore its prior file state after a caught failure."""

    normalized: list[tuple[Path, bytes, int]] = []
    snapshots: dict[Path, tuple[bool, bytes, int]] = {}
    created_directories: set[Path] = set()
    for raw_path, payload, mode in writes:
        path = raw_path.resolve()
        if path in snapshots:
            raise GraphControlError(f"atomic write batch contains duplicate target: {path}")
        existed = path.exists()
        if existed and not path.is_file():
            raise GraphControlError(f"atomic write batch target is not a file: {path}")
        snapshots[path] = (
            existed,
            path.read_bytes() if existed else b"",
            path.stat().st_mode & 0o777 if existed else mode,
        )
        normalized.append((path, payload, mode))
        parent = path.parent
        while not parent.exists():
            created_directories.add(parent)
            parent = parent.parent

    attempted: list[Path] = []
    try:
        for path, payload, mode in normalized:
            attempted.append(path)
            atomic_write_bytes(path, payload, mode=mode)
    except BaseException as write_error:
        rollback_errors: list[str] = []
        for path in reversed(attempted):
            existed, previous_payload, previous_mode = snapshots[path]
            try:
                if existed:
                    atomic_write_bytes(path, previous_payload, mode=previous_mode)
                elif path.exists():
                    path.unlink()
                    _fsync_directory(path.parent)
            except BaseException as rollback_error:
                rollback_errors.append(f"{path}: {rollback_error}")
        for directory in sorted(
            created_directories,
            key=lambda item: len(item.parts),
            reverse=True,
        ):
            try:
                directory.rmdir()
            except FileNotFoundError:
                pass
            except OSError:
                if directory.exists():
                    rollback_errors.append(
                        f"{directory}: could not remove created directory"
                    )
        if rollback_errors:
            raise GraphControlError(
                "atomic write batch failed and rollback was incomplete: "
                + "; ".join(rollback_errors)
            ) from write_error
        raise


@contextlib.contextmanager
def file_lock(path: Path, timeout_seconds: float = 10.0) -> Iterator[None]:
    """Acquire an advisory exclusive lock, waiting no longer than *timeout_seconds*."""

    if timeout_seconds < 0:
        raise ValueError("timeout_seconds must be non-negative")
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    deadline = time.monotonic() + timeout_seconds
    try:
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError as exc:
                if time.monotonic() >= deadline:
                    raise LockTimeout(f"timed out acquiring lock {path}") from exc
                time.sleep(min(0.025, max(0.0, deadline - time.monotonic())))
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _last_event(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    last = None
    try:
        with path.open("r", encoding="utf-8") as handle:
            for raw in handle:
                if raw.strip():
                    last = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise GraphControlError(f"cannot extend corrupt event ledger {path}: {exc}") from exc
    if last is not None and not isinstance(last, dict):
        raise GraphControlError(f"event ledger {path} contains a non-object record")
    return last


def append_event(path: Path, event: dict[str, Any]) -> dict[str, Any]:
    """Append one fsynced hash-chained JSON event.

    Callers that coordinate this with runtime state must already hold the runtime lock.
    """

    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    previous = _last_event(path)
    record = dict(event)
    record.setdefault("timestamp", utc_now())
    record["sequence"] = 1 if previous is None else int(previous["sequence"]) + 1
    record["previous_hash"] = None if previous is None else previous["event_hash"]
    record.pop("event_hash", None)
    record["event_hash"] = sha256_json(record)
    existed = path.exists()
    with path.open("ab", buffering=0) as handle:
        handle.write(canonical_bytes(record) + b"\n")
        os.fsync(handle.fileno())
    if not existed:
        _fsync_directory(path.parent)
    return record


def verify_event_chain(path: Path) -> list[str]:
    errors: list[str] = []
    if not path.exists():
        return [f"event ledger does not exist: {path}"]
    expected_previous = None
    expected_sequence = 1
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        return [f"cannot read event ledger: {exc}"]
    for line_number, raw in enumerate(lines, start=1):
        if not raw.strip():
            errors.append(f"line {line_number}: blank records are not allowed")
            continue
        try:
            record = json.loads(raw)
        except json.JSONDecodeError as exc:
            errors.append(f"line {line_number}: invalid JSON: {exc}")
            continue
        if not isinstance(record, dict):
            errors.append(f"line {line_number}: record must be an object")
            continue
        if record.get("sequence") != expected_sequence:
            errors.append(f"line {line_number}: expected sequence {expected_sequence}")
        if record.get("previous_hash") != expected_previous:
            errors.append(f"line {line_number}: previous hash mismatch")
        claimed = record.get("event_hash")
        candidate = dict(record)
        candidate.pop("event_hash", None)
        actual = sha256_json(candidate)
        if claimed != actual:
            errors.append(f"line {line_number}: event hash mismatch")
        expected_previous = claimed
        expected_sequence += 1
    return errors


def node_map(graph: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {item["id"]: item for item in graph.get("nodes", []) if isinstance(item, dict) and isinstance(item.get("id"), str)}


def scheduling_predecessors(graph: dict[str, Any]) -> dict[str, set[str]]:
    predecessors: dict[str, set[str]] = defaultdict(set)
    for edge in graph.get("edges", []):
        if isinstance(edge, dict) and edge.get("type") in SCHEDULING_EDGE_TYPES:
            source, target = edge.get("from"), edge.get("to")
            if isinstance(source, str) and isinstance(target, str):
                predecessors[target].add(source)
    return predecessors


def ready_node_ids(graph: dict[str, Any], run: dict[str, Any]) -> list[str]:
    predecessors = scheduling_predecessors(graph)
    states = run.get("nodes", {})
    ready: list[str] = []
    for node in graph.get("nodes", []):
        node_id = node.get("id") if isinstance(node, dict) else None
        if not isinstance(node_id, str) or node_id not in states:
            continue
        state = states[node_id].get("state")
        if state not in {"planned", "ready", "blocked"}:
            continue
        if all(states.get(source, {}).get("state") == "verified" for source in predecessors.get(node_id, set())):
            ready.append(node_id)
    return ready


def affected_node_ids(graph: dict[str, Any], changed: set[str]) -> list[str]:
    """Return changed nodes and their forward scheduling/invalidation descendants."""

    order = [item.get("id") for item in graph.get("nodes", []) if isinstance(item, dict)]
    known = {item for item in order if isinstance(item, str)}
    outgoing: dict[str, set[str]] = defaultdict(set)
    for edge in graph.get("edges", []):
        if isinstance(edge, dict) and edge.get("type") in INVALIDATION_EDGE_TYPES:
            source, target = edge.get("from"), edge.get("to")
            if isinstance(source, str) and isinstance(target, str):
                outgoing[source].add(target)
    seen = {item for item in changed if item in known}
    queue = deque(item for item in order if item in seen)
    while queue:
        current = queue.popleft()
        for target in sorted(outgoing.get(current, set())):
            if target not in seen:
                seen.add(target)
                queue.append(target)
    return [item for item in order if item in seen]


def semantic_affected_node_ids(
    projection: dict[str, Any],
    changed: set[str],
) -> list[str]:
    """Return enrolled seeds and explicit semantic descendants in projection order."""

    order = [
        item.get("id")
        for item in projection.get("nodes", [])
        if isinstance(item, dict)
    ]
    known = {item for item in order if isinstance(item, str)}
    outgoing: dict[str, set[str]] = defaultdict(set)
    for dependency in projection.get("semantic_dependencies", []):
        if not isinstance(dependency, dict):
            continue
        source, target = dependency.get("from"), dependency.get("to")
        if source in known and target in known:
            outgoing[source].add(target)
    seen = {item for item in changed if item in known}
    queue = deque(item for item in order if item in seen)
    while queue:
        current = queue.popleft()
        for target in sorted(outgoing.get(current, set())):
            if target not in seen:
                seen.add(target)
                queue.append(target)
    return [item for item in order if item in seen]


def artifact_affected_nodes(graph: dict[str, Any], artifacts: set[str]) -> set[str]:
    seeds: set[str] = set()
    for node in graph.get("nodes", []):
        if not isinstance(node, dict):
            continue
        produced = {item.get("artifact") for item in node.get("outputs", []) if isinstance(item, dict)}
        consumed = {item.get("artifact") for item in node.get("inputs", []) if isinstance(item, dict)}
        if produced & artifacts or consumed & artifacts:
            if isinstance(node.get("id"), str):
                seeds.add(node["id"])
    return set(affected_node_ids(graph, seeds))


def _nonempty_string_list_errors(
    value: Any,
    label: str,
    *,
    allow_empty: bool = False,
) -> list[str]:
    if not isinstance(value, list):
        return [f"{label} must be an array"]
    if not allow_empty and not value:
        return [f"{label} must not be empty"]
    if not all(isinstance(item, str) and item.strip() for item in value):
        return [f"{label} must contain only non-empty strings"]
    if len(value) != len(set(value)):
        return [f"{label} must contain unique values"]
    return []


def outcome_contract_digest(contract: dict[str, Any]) -> str:
    """Return the canonical digest that pins every outcome-contract field."""

    return sha256_json(contract)


def outcome_contract_errors(contract: Any) -> list[str]:
    """Return deterministic validation errors for an immutable outcome contract."""

    if not isinstance(contract, dict):
        return ["outcome contract must be an object"]
    required = {
        "contract_id",
        "contract_version",
        "outcome",
        "outcome_digest",
        "requirement_ids",
        "non_goals",
        "acceptance_ids",
        "acceptance_digest",
        "terminal_node_ids",
        "authority_evidence",
        "authority_digest",
        "impact_policy",
        "created_at",
        "actor",
    }
    errors = [
        *(f"outcome contract missing field: {field}" for field in sorted(required - set(contract))),
        *(f"outcome contract unknown field: {field}" for field in sorted(set(contract) - required)),
    ]
    for field in ("outcome", "created_at", "actor"):
        if not isinstance(contract.get(field), str) or not contract[field].strip():
            errors.append(f"outcome contract {field} must be a non-empty string")
    contract_id = contract.get("contract_id")
    if not isinstance(contract_id, str) or not SLUG_PATTERN.fullmatch(contract_id):
        errors.append("outcome contract contract_id must be a lowercase slug")
    if type(contract.get("contract_version")) is not int or contract["contract_version"] < 1:
        errors.append("outcome contract contract_version must be a positive integer")
    errors.extend(_nonempty_string_list_errors(contract.get("requirement_ids"), "outcome contract requirement_ids"))
    errors.extend(
        _nonempty_string_list_errors(
            contract.get("non_goals"),
            "outcome contract non_goals",
            allow_empty=True,
        )
    )
    errors.extend(_nonempty_string_list_errors(contract.get("acceptance_ids"), "outcome contract acceptance_ids"))
    terminal_errors = _nonempty_string_list_errors(
        contract.get("terminal_node_ids"),
        "outcome contract terminal_node_ids",
    )
    errors.extend(terminal_errors)
    if not terminal_errors and any(
        not SLUG_PATTERN.fullmatch(item)
        for item in contract["terminal_node_ids"]
    ):
        errors.append("outcome contract terminal_node_ids must contain lowercase slugs")
    errors.extend(_nonempty_string_list_errors(contract.get("authority_evidence"), "outcome contract authority_evidence"))
    impact_policy = contract.get("impact_policy")
    impact_fields = {
        "max_verified_node_count",
        "max_verified_node_fraction",
    }
    if not isinstance(impact_policy, dict):
        errors.append("outcome contract impact_policy must be an object")
    elif set(impact_policy) != impact_fields:
        errors.append("outcome contract impact_policy fields are invalid")
    else:
        count = impact_policy["max_verified_node_count"]
        fraction = impact_policy["max_verified_node_fraction"]
        if type(count) is not int or count < 0:
            errors.append(
                "outcome contract impact_policy.max_verified_node_count "
                "must be a non-negative integer"
            )
        if (
            not isinstance(fraction, (int, float))
            or isinstance(fraction, bool)
            or not 0 <= fraction <= 1
        ):
            errors.append(
                "outcome contract impact_policy.max_verified_node_fraction "
                "must be between 0 and 1"
            )
    digest_inputs = (
        ("outcome_digest", contract.get("outcome")),
        ("acceptance_digest", contract.get("acceptance_ids")),
        ("authority_digest", contract.get("authority_evidence")),
    )
    for field, value in digest_inputs:
        claimed = contract.get(field)
        if not isinstance(claimed, str) or claimed != sha256_json(value):
            errors.append(f"outcome contract {field} is not canonical")
    return errors


def projection_contract_errors(
    graph: dict[str, Any],
    contract: dict[str, Any],
    projection: Any,
    *,
    expected_graph_checksum: str | None = None,
    expected_run_id: str | None = None,
    require_current_source: bool = True,
) -> list[str]:
    """Validate an exact creation projection or a frozen runtime projection."""

    errors = outcome_contract_errors(contract)
    if not isinstance(projection, dict):
        return errors + ["projection must be an object"]
    required = {
        "schema_version",
        "graph_id",
        "source_graph_version",
        "source_graph_checksum",
        "contract_id",
        "contract_version",
        "projection_version",
        "nodes",
        "edges",
        "entry_nodes",
        "terminal_nodes",
        "semantic_dependencies",
        "requirement_mappings",
        "acceptance_mappings",
        "provenance",
    }
    allowed = required | {"run_id", "lineage"}
    errors.extend(f"projection missing field: {field}" for field in sorted(required - set(projection)))
    errors.extend(f"projection unknown field: {field}" for field in sorted(set(projection) - allowed))
    if projection.get("schema_version") != 1:
        errors.append("projection schema_version must be 1")
    if projection.get("graph_id") != graph.get("graph_id"):
        errors.append("projection graph ID does not match durable graph")
    if require_current_source:
        if projection.get("source_graph_version") != graph.get("graph_version"):
            errors.append("projection source graph version does not match durable graph")
        current_checksum = (
            expected_graph_checksum
            if expected_graph_checksum is not None
            else sha256_json(graph)
        )
        if projection.get("source_graph_checksum") != current_checksum:
            errors.append("projection source graph checksum does not match durable graph")
    else:
        if type(projection.get("source_graph_version")) is not int or projection["source_graph_version"] < 1:
            errors.append("projection source graph version must be a positive integer")
        checksum = projection.get("source_graph_checksum")
        if not isinstance(checksum, str) or len(checksum) != 64 or any(
            character not in "0123456789abcdef" for character in checksum
        ):
            errors.append("projection source graph checksum must be a SHA-256 digest")
    if projection.get("contract_id") != contract.get("contract_id"):
        errors.append("projection contract ID does not match outcome contract")
    if projection.get("contract_version") != contract.get("contract_version"):
        errors.append("projection contract version does not match outcome contract")
    if type(projection.get("projection_version")) is not int or projection["projection_version"] < 1:
        errors.append("projection_version must be a positive integer")
    projection_run_id = projection.get("run_id")
    if projection_run_id is not None:
        try:
            uuid.UUID(projection_run_id)
        except (AttributeError, TypeError, ValueError):
            errors.append("projection run ID must be a UUID")
    if expected_run_id is not None and projection_run_id != expected_run_id:
        errors.append("projection run ID does not match runtime")

    source_nodes = node_map(graph)
    projected_nodes = projection.get("nodes")
    projected_ids: list[str] = []
    if not isinstance(projected_nodes, list) or not projected_nodes:
        errors.append("projection nodes must be a non-empty array")
    else:
        for item in projected_nodes:
            node_id = item.get("id") if isinstance(item, dict) else None
            if not isinstance(node_id, str):
                errors.append("projection node must have a string ID")
                continue
            projected_ids.append(node_id)
            if require_current_source:
                source = source_nodes.get(node_id)
                if source is None:
                    errors.append(f"projection node does not exist in durable graph: {node_id}")
                elif canonical_bytes(item) != canonical_bytes(source):
                    errors.append(f"projection node differs from durable graph definition: {node_id}")
        if len(projected_ids) != len(set(projected_ids)):
            errors.append("projection node IDs must be unique")
    projected_set = set(projected_ids)

    source_edges = {
        item["id"]: item
        for item in graph.get("edges", [])
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    projected_edges = projection.get("edges")
    edge_ids: list[str] = []
    if not isinstance(projected_edges, list):
        errors.append("projection edges must be an array")
    else:
        for item in projected_edges:
            edge_id = item.get("id") if isinstance(item, dict) else None
            if not isinstance(edge_id, str):
                errors.append("projection edge must have a string ID")
                continue
            edge_ids.append(edge_id)
            if require_current_source:
                source = source_edges.get(edge_id)
                if source is None:
                    errors.append(f"projection edge does not exist in durable graph: {edge_id}")
                elif canonical_bytes(item) != canonical_bytes(source):
                    errors.append(f"projection edge differs from durable graph definition: {edge_id}")
            if item.get("from") not in projected_set or item.get("to") not in projected_set:
                errors.append(f"projection edge has an unenrolled endpoint: {edge_id}")
        if len(edge_ids) != len(set(edge_ids)):
            errors.append("projection edge IDs must be unique")

    for field in ("entry_nodes", "terminal_nodes"):
        value = projection.get(field)
        errors.extend(_nonempty_string_list_errors(value, f"projection {field}"))
        if isinstance(value, list):
            outside = sorted(set(item for item in value if isinstance(item, str)) - projected_set)
            if outside:
                errors.append(f"projection {field} contains unenrolled nodes: {', '.join(outside)}")
    projected_terminals = projection.get("terminal_nodes")
    contract_terminals = contract.get("terminal_node_ids")
    if (
        isinstance(projected_terminals, list)
        and isinstance(contract_terminals, list)
        and set(projected_terminals) != set(contract_terminals)
    ):
        errors.append("projection terminal nodes do not match outcome contract")

    def validate_mappings(field: str, contract_field: str) -> None:
        mappings = projection.get(field)
        if not isinstance(mappings, dict):
            errors.append(f"projection {field} must be an object")
            return
        expected_keys = set(contract.get(contract_field, []))
        actual_keys = set(mappings)
        if actual_keys != expected_keys:
            errors.append(
                f"projection {field} keys differ from outcome contract: "
                f"missing={sorted(expected_keys - actual_keys)} extra={sorted(actual_keys - expected_keys)}"
            )
        for key, value in mappings.items():
            errors.extend(_nonempty_string_list_errors(value, f"projection {field}.{key}"))
            if isinstance(value, list):
                outside = sorted(set(item for item in value if isinstance(item, str)) - projected_set)
                if outside:
                    errors.append(
                        f"projection {field}.{key} contains unenrolled nodes: {', '.join(outside)}"
                    )

    validate_mappings("requirement_mappings", "requirement_ids")
    validate_mappings("acceptance_mappings", "acceptance_ids")

    semantic_dependencies = projection.get("semantic_dependencies")
    if not isinstance(semantic_dependencies, list):
        errors.append("projection semantic_dependencies must be an array")
    else:
        dependency_ids: list[str] = []
        for item in semantic_dependencies:
            if not isinstance(item, dict):
                errors.append("projection semantic dependency must be an object")
                continue
            if set(item) != {"id", "from", "to", "rationale"}:
                errors.append("projection semantic dependency fields are invalid")
                continue
            dependency_id = item.get("id")
            if not isinstance(dependency_id, str) or not SLUG_PATTERN.fullmatch(dependency_id):
                errors.append("projection semantic dependency ID must be a lowercase slug")
            else:
                dependency_ids.append(dependency_id)
            if item.get("from") not in projected_set or item.get("to") not in projected_set:
                errors.append(f"projection semantic dependency has an unenrolled endpoint: {dependency_id}")
            if not isinstance(item.get("rationale"), str) or not item["rationale"].strip():
                errors.append(f"projection semantic dependency rationale is invalid: {dependency_id}")
        if len(dependency_ids) != len(set(dependency_ids)):
            errors.append("projection semantic dependency IDs must be unique")

    lineage = projection.get("lineage")
    projection_version = projection.get("projection_version")
    if lineage is None:
        if type(projection_version) is int and projection_version > 1:
            errors.append("projection versions after 1 require lineage")
    elif not isinstance(lineage, dict):
        errors.append("projection lineage must be an object")
    else:
        lineage_fields = {
            "parent_path", "parent_checksum", "parent_version", "promotion_id",
            "added_node_ids", "added_edge_ids",
        }
        if set(lineage) != lineage_fields:
            errors.append("projection lineage fields are invalid")
        else:
            if not isinstance(lineage["parent_path"], str) or not lineage["parent_path"].strip():
                errors.append("projection lineage parent path must be non-empty")
            parent_checksum = lineage["parent_checksum"]
            if not isinstance(parent_checksum, str) or len(parent_checksum) != 64 or any(
                character not in "0123456789abcdef" for character in parent_checksum
            ):
                errors.append("projection lineage parent checksum must be a SHA-256 digest")
            if (
                type(lineage["parent_version"]) is not int
                or lineage["parent_version"] < 1
                or type(projection_version) is not int
                or lineage["parent_version"] + 1 != projection_version
            ):
                errors.append("projection lineage parent version must precede projection version")
            if not isinstance(lineage["promotion_id"], str) or not SLUG_PATTERN.fullmatch(lineage["promotion_id"]):
                errors.append("projection lineage promotion ID must be a lowercase slug")
            for field in ("added_node_ids", "added_edge_ids"):
                values = lineage[field]
                errors.extend(
                    _nonempty_string_list_errors(
                        values,
                        f"projection lineage {field}",
                        allow_empty=True,
                    )
                )
                if isinstance(values, list) and any(
                    not SLUG_PATTERN.fullmatch(item)
                    for item in values
                    if isinstance(item, str)
                ):
                    errors.append(f"projection lineage {field} must contain lowercase slugs")

    provenance = projection.get("provenance")
    if not isinstance(provenance, dict) or set(provenance) != {"actor", "authority"}:
        errors.append("projection provenance must contain only actor and authority")
    elif not all(isinstance(provenance[field], str) and provenance[field].strip() for field in provenance):
        errors.append("projection provenance values must be non-empty strings")
    return errors


def execution_graph_from_projection(
    graph: dict[str, Any],
    projection: dict[str, Any],
) -> dict[str, Any]:
    """Return the frozen scheduling view consumed by one schema-v2 run."""

    execution = dict(graph)
    for field in (
        "nodes",
        "edges",
        "entry_nodes",
        "terminal_nodes",
        "semantic_dependencies",
        "requirement_mappings",
        "acceptance_mappings",
    ):
        execution[field] = projection[field]
    execution["graph_version"] = projection["source_graph_version"]
    return execution


def resolve_within(root: Path, relative: str) -> Path:
    # Compatibility for already-resolved CLI/workflow paths. Serialized graph,
    # manifest, runtime, and evidence paths still go through the strict relative
    # contract.
    candidate_path = Path(relative)
    if candidate_path.is_absolute():
        base = root.resolve()
        candidate = candidate_path.resolve()
        try:
            candidate.relative_to(base)
        except ValueError as exc:
            raise ValueError(f"path escapes project root: {relative}") from exc
        return candidate
    return resolve_project_path(root, relative)
