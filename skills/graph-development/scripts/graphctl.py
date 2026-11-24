"""Operate durable project-local state for a validated development graph."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from graph_core import (
    GraphControlError,
    RuntimeStateError,
    StaleStateError,
    affected_node_ids,
    append_event,
    artifact_affected_nodes,
    atomic_write_batch,
    atomic_write_json,
    canonical_bytes,
    file_lock,
    execution_graph_from_projection,
    load_json,
    outcome_contract_digest,
    outcome_contract_errors,
    projection_contract_errors,
    ready_node_ids,
    semantic_affected_node_ids,
    sha256_json,
    utc_now,
    verify_event_chain,
)
from causal_episodes import (
    apply_repair_cycle,
    episode_gate_document_errors,
    episode_ledger_errors,
    preview_repair_cycle,
    record_acceptance_progress,
    runtime_episodes_errors,
)
from validate_graph import canonical_checksum, validate_graph
from path_contract import project_root_for_graph, resolve_project_path, serialize_relative
from project_lifecycle import LifecycleError, apply_project, preview_project


RUN_FILENAME = "run.json"
EVENTS_FILENAME = "events.jsonl"
LOCK_FILENAME = "runtime.lock"


def _paths(run_dir: Path) -> tuple[Path, Path, Path]:
    run_dir = run_dir.resolve()
    return run_dir / RUN_FILENAME, run_dir / EVENTS_FILENAME, run_dir / LOCK_FILENAME


def _load_graph(graph_path: Path) -> dict[str, Any]:
    graph = load_json(graph_path.resolve())
    report = validate_graph(graph)
    if not report["valid"]:
        raise ValueError("graph validation failed: " + "; ".join(report["errors"]))
    return graph


def load_run(run_dir: Path) -> dict[str, Any]:
    run_path, _, _ = _paths(run_dir)
    run = load_json(run_path)
    if run.get("schema_version") not in {1, 2} or not isinstance(run.get("run_version"), int):
        raise RuntimeStateError(f"invalid runtime state in {run_path}")
    return run


def _node_definitions(graph: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {item["id"]: item for item in graph["nodes"]}


def _is_checksum(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _is_positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def _projection_binding_errors(binding: Any) -> list[str]:
    if not isinstance(binding, dict):
        return ["runtime projection binding must be an object"]
    required = {"version", "path", "checksum"}
    errors = [
        *(f"runtime projection binding missing field: {field}" for field in sorted(required - set(binding))),
        *(f"runtime projection binding unknown field: {field}" for field in sorted(set(binding) - required)),
    ]
    if not _is_positive_int(binding.get("version")):
        errors.append("runtime projection version must be a positive integer")
    if not isinstance(binding.get("path"), str) or not binding["path"].strip():
        errors.append("runtime projection path must be a non-empty string")
    if not _is_checksum(binding.get("checksum")):
        errors.append("runtime projection checksum must be a SHA-256 digest")
    return errors


def _v2_runtime_errors(run: dict[str, Any]) -> list[str]:
    required = {
        "schema_version", "run_id", "run_version", "graph_id",
        "observed_graph_version", "observed_graph_checksum", "outcome_contract",
        "projection", "episodes", "status", "created_at", "updated_at",
        "last_event_sequence", "last_event_hash", "feedback_traversals", "nodes",
    }
    errors = [
        *(f"run.json missing field: {field}" for field in sorted(required - set(run))),
        *(f"run.json unknown field: {field}" for field in sorted(set(run) - required)),
    ]
    try:
        uuid.UUID(run.get("run_id"))
    except (AttributeError, TypeError, ValueError):
        errors.append("runtime run_id must be a UUID")
    if not _is_positive_int(run.get("run_version")):
        errors.append("runtime run_version must be a positive integer")
    if not isinstance(run.get("graph_id"), str) or not run["graph_id"].strip():
        errors.append("runtime graph_id must be a non-empty string")
    if not _is_positive_int(run.get("observed_graph_version")):
        errors.append("runtime observed graph version must be a positive integer")
    if not _is_checksum(run.get("observed_graph_checksum")):
        errors.append("runtime observed graph checksum must be a SHA-256 digest")
    errors.extend(outcome_contract_errors(run.get("outcome_contract")))
    errors.extend(_projection_binding_errors(run.get("projection")))
    errors.extend(
        runtime_episodes_errors(
            run.get("episodes"),
            run_id=run.get("run_id"),
            last_event_sequence=run.get("last_event_sequence"),
        )
    )
    if run.get("status") not in {"active", "running", "needs_human", "completed", "cancelled"}:
        errors.append("runtime status is invalid")
    for field in ("created_at", "updated_at"):
        if not isinstance(run.get(field), str) or not run[field].strip():
            errors.append(f"runtime {field} must be a non-empty string")
    if not _is_positive_int(run.get("last_event_sequence")):
        errors.append("runtime last event sequence must be a positive integer")
    if not _is_checksum(run.get("last_event_hash")):
        errors.append("runtime last event hash must be a SHA-256 digest")
    feedback = run.get("feedback_traversals")
    if not isinstance(feedback, dict) or any(
        not isinstance(value, int) or isinstance(value, bool) or value < 0
        for value in feedback.values()
    ):
        errors.append("runtime feedback traversals must be non-negative integers")
    nodes = run.get("nodes")
    if not isinstance(nodes, dict) or not nodes:
        errors.append("runtime nodes must be a non-empty object")
    else:
        required_state = {
            "state", "attempts", "generation", "lease", "approval",
            "completed_idempotency_keys", "evidence", "last_result",
        }
        allowed_states = {
            "planned", "ready", "blocked", "running", "verifying", "verified",
            "invalidated", "failed", "needs_human", "cancelled",
        }
        for node_id, state in nodes.items():
            if not isinstance(node_id, str) or not node_id:
                errors.append("runtime node IDs must be non-empty strings")
                continue
            if not isinstance(state, dict):
                errors.append(f"runtime node state must be an object: {node_id}")
                continue
            if set(state) != required_state:
                errors.append(f"runtime node state fields are invalid: {node_id}")
                continue
            if state.get("state") not in allowed_states:
                errors.append(f"runtime node state value is invalid: {node_id}")
            if not isinstance(state.get("attempts"), int) or isinstance(state.get("attempts"), bool) or state["attempts"] < 0:
                errors.append(f"runtime node attempts are invalid: {node_id}")
            if not _is_positive_int(state.get("generation")):
                errors.append(f"runtime node generation is invalid: {node_id}")
            if state.get("lease") is not None and not isinstance(state.get("lease"), dict):
                errors.append(f"runtime node lease is invalid: {node_id}")
            if state.get("approval") is not None and not isinstance(state.get("approval"), dict):
                errors.append(f"runtime node approval is invalid: {node_id}")
            keys = state.get("completed_idempotency_keys")
            if not isinstance(keys, list) or not all(isinstance(item, str) and item for item in keys):
                errors.append(f"runtime completed idempotency keys are invalid: {node_id}")
            evidence = state.get("evidence")
            if not isinstance(evidence, list) or not all(isinstance(item, dict) for item in evidence):
                errors.append(f"runtime node evidence is invalid: {node_id}")
            if state.get("last_result") is not None and not isinstance(state.get("last_result"), dict):
                errors.append(f"runtime node last result is invalid: {node_id}")
    return errors


def run_observed_graph_checksum(run: dict[str, Any]) -> Any:
    field = "graph_checksum" if run.get("schema_version") == 1 else "observed_graph_checksum"
    return run.get(field)


def run_execution_checksum(run: dict[str, Any]) -> Any:
    if run.get("schema_version") == 1:
        return run.get("graph_checksum")
    binding = run.get("projection")
    return binding.get("checksum") if isinstance(binding, dict) else None


def _event_records(run_dir: Path) -> list[dict[str, Any]]:
    events_path = run_dir.resolve() / EVENTS_FILENAME
    try:
        return [
            json.loads(line)
            for line in events_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeStateError(f"cannot read event ledger: {exc}") from exc


def _sealed_consumed_artifacts(
    records: list[dict[str, Any]],
    node_id: str,
    state: dict[str, Any],
) -> dict[str, str] | None:
    evidence = state.get("last_result")
    if not isinstance(evidence, dict):
        return None
    observations = evidence.get("consumed_artifacts")
    if not isinstance(observations, list):
        return None
    normalized: list[dict[str, str]] = []
    seen: set[str] = set()
    for observation in observations:
        if not isinstance(observation, dict) or set(observation) != {
            "artifact_id",
            "artifact_digest",
        }:
            return None
        artifact_id = observation.get("artifact_id")
        artifact_digest = observation.get("artifact_digest")
        if (
            not isinstance(artifact_id, str)
            or not artifact_id.strip()
            or artifact_id in seen
            or not _is_checksum(artifact_digest)
        ):
            return None
        seen.add(artifact_id)
        normalized.append(
            {
                "artifact_id": artifact_id,
                "artifact_digest": artifact_digest,
            }
        )
    normalized.sort(key=lambda item: item["artifact_id"])
    if observations != normalized:
        return None
    evidence_digest = sha256_json(evidence)
    consumed_digest = sha256_json(normalized)
    generation = state.get("generation")
    sealed = any(
        record.get("action") == "node-completed"
        and record.get("node_id") == node_id
        and isinstance(record.get("details"), dict)
        and record["details"].get("evidence_digest") == evidence_digest
        and record["details"].get("consumed_artifacts_digest")
        == consumed_digest
        and record["details"].get("node_generation") == generation
        for record in records
    )
    if not sealed:
        return None
    return {
        item["artifact_id"]: item["artifact_digest"]
        for item in normalized
    }


def _runtime_ledger_errors(run_dir: Path, run: dict[str, Any]) -> list[str]:
    events_path = run_dir.resolve() / EVENTS_FILENAME
    errors = verify_event_chain(events_path)
    if errors:
        return errors
    try:
        records = _event_records(run_dir)
    except RuntimeStateError as exc:
        return [str(exc)]
    if not records:
        return ["event ledger is empty"]
    last = records[-1]
    if run.get("last_event_sequence") != last.get("sequence"):
        errors.append("run last event sequence does not match ledger")
    if run.get("last_event_hash") != last.get("event_hash"):
        errors.append("run last event hash does not match ledger")
    if run.get("run_version") != last.get("run_version_after"):
        errors.append("run version does not match the last ledger event")
    if run.get("schema_version") != 2:
        return errors

    contract = run.get("outcome_contract")
    immutable = {
        "run_id": run.get("run_id"),
        "graph_id": run.get("graph_id"),
        "outcome_contract_digest": (
            outcome_contract_digest(contract) if isinstance(contract, dict) else None
        ),
        "outcome_digest": (
            contract.get("outcome_digest") if isinstance(contract, dict) else None
        ),
    }
    boundary_indexes = [
        index
        for index, record in enumerate(records)
        if record.get("action") == "schema-migrated"
        and isinstance(record.get("source_versions"), dict)
        and isinstance(record.get("target_versions"), dict)
        and record["source_versions"].get("runtime") == 1
        and record["target_versions"].get("runtime") == 2
    ]
    conversion_index: int | None = None
    if boundary_indexes:
        if len(boundary_indexes) != 1:
            errors.append("converted runtime ledger must contain exactly one v1 to v2 boundary")
        else:
            conversion_index = boundary_indexes[0]
            if conversion_index == 0:
                errors.append("converted runtime ledger requires a legacy v1 prefix")
    graph_cursor: Any = None
    projection_cursor: Any = None
    for index, record in enumerate(records):
        sequence = record.get("sequence", "?")
        if record.get("run_id") != run.get("run_id"):
            errors.append(f"event {sequence} run_id does not match runtime")
        if record.get("graph_id") != run.get("graph_id"):
            errors.append(f"event {sequence} graph_id does not match runtime")
        if conversion_index is not None and index < conversion_index:
            if any(field in record for field in (
                "outcome_contract_digest",
                "outcome_digest",
                "projection_checksum",
            )):
                errors.append(
                    f"event {sequence} has v2 bindings before the conversion boundary"
                )
            graph_cursor = record.get("graph_checksum")
            continue
        for field, value in immutable.items():
            if record.get(field) != value:
                errors.append(
                    f"event {sequence} {field} does not match immutable runtime binding"
                )
        if conversion_index is not None and index == conversion_index:
            if record.get("graph_checksum") != graph_cursor:
                errors.append(
                    f"event {sequence} conversion graph binding differs from legacy prefix"
                )
            if record.get("projection_checksum") != run_execution_checksum(run):
                errors.append(
                    f"event {sequence} conversion projection binding differs from runtime"
                )
            graph_cursor = record.get("graph_checksum")
            projection_cursor = record.get("projection_checksum")
            continue
        if graph_cursor is None:
            graph_cursor = record.get("graph_checksum")
            projection_cursor = record.get("projection_checksum")
            continue
        details = record.get("details")
        details = details if isinstance(details, dict) else {}
        if record.get("action") == "graph-definition-recorded":
            before = details.get("graph_checksum_before")
            after = details.get("graph_checksum_after")
            if before != graph_cursor:
                errors.append(
                    f"event {sequence} graph definition base does not match ledger lineage"
                )
            if after != record.get("graph_checksum"):
                errors.append(
                    f"event {sequence} graph definition result does not match event binding"
                )
            if (
                details.get("projection_checksum_before") != projection_cursor
                or details.get("projection_checksum_after") != projection_cursor
            ):
                errors.append(
                    f"event {sequence} graph definition changed the run projection"
                )
            graph_cursor = after
        elif record.get("action") == "run-nodes-promoted":
            before = details.get("projection_checksum_before")
            after = details.get("projection_checksum_after")
            if record.get("graph_checksum") != graph_cursor:
                errors.append(
                    f"event {sequence} promotion changed the durable graph binding"
                )
            if before != projection_cursor:
                errors.append(
                    f"event {sequence} promotion base does not match projection lineage"
                )
            if after != record.get("projection_checksum"):
                errors.append(
                    f"event {sequence} promotion result does not match event binding"
                )
            projection_cursor = after
        else:
            if record.get("graph_checksum") != graph_cursor:
                errors.append(
                    f"event {sequence} graph_checksum does not match ledger lineage"
                )
            if record.get("projection_checksum") != projection_cursor:
                errors.append(
                    f"event {sequence} projection_checksum does not match ledger lineage"
                )
    for node_id, state in run.get("nodes", {}).items():
        if (
            isinstance(state, dict)
            and state.get("state") == "verified"
            and isinstance(state.get("last_result"), dict)
            and "consumed_artifacts" in state["last_result"]
            and _sealed_consumed_artifacts(records, node_id, state) is None
        ):
            errors.append(
                f"verified node consumed artifact evidence is not ledger-sealed: {node_id}"
            )
    errors.extend(
        episode_ledger_errors(records, run.get("episodes", {}))
    )
    if graph_cursor != run_observed_graph_checksum(run):
        errors.append("ledger graph lineage does not match runtime binding")
    if projection_cursor != run_execution_checksum(run):
        errors.append("ledger projection_checksum lineage does not match runtime binding")
    return errors


def _projection_lineage_errors(
    run_dir: Path,
    contract: dict[str, Any],
    projection: dict[str, Any],
    *,
    expected_run_id: str,
    visited: set[str] | None = None,
) -> list[str]:
    lineage = projection.get("lineage")
    if lineage is None or not isinstance(lineage, dict):
        return []
    errors: list[str] = []
    parent_relative = lineage.get("parent_path")
    if not isinstance(parent_relative, str):
        return ["projection lineage parent path is invalid"]
    visited = set() if visited is None else set(visited)
    if parent_relative in visited:
        return ["projection lineage contains a cycle"]
    visited.add(parent_relative)
    try:
        parent_path = resolve_project_path(run_dir.resolve(), parent_relative)
        parent = load_json(parent_path)
    except (ValueError, GraphControlError) as exc:
        return [f"cannot load parent projection: {exc}"]
    if sha256_json(parent) != lineage.get("parent_checksum"):
        errors.append("parent projection checksum does not match lineage")
    errors.extend(
        projection_contract_errors(
            {"graph_id": projection.get("graph_id")},
            contract,
            parent,
            expected_run_id=expected_run_id,
            require_current_source=False,
        )
    )
    if parent.get("projection_version") != lineage.get("parent_version"):
        errors.append("parent projection version does not match lineage")
    for field in ("run_id", "graph_id", "contract_id", "contract_version"):
        if parent.get(field) != projection.get(field):
            errors.append(f"parent projection {field} differs from child")
    for field, added_field in (
        ("nodes", "added_node_ids"),
        ("edges", "added_edge_ids"),
    ):
        parent_map = {
            item.get("id"): item
            for item in parent.get(field, [])
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        }
        child_map = {
            item.get("id"): item
            for item in projection.get(field, [])
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        }
        for item_id, item in parent_map.items():
            if item_id not in child_map:
                errors.append(f"promoted projection removed parent {field[:-1]}: {item_id}")
            elif canonical_bytes(item) != canonical_bytes(child_map[item_id]):
                errors.append(f"promoted projection changed parent {field[:-1]}: {item_id}")
        if set(child_map) - set(parent_map) != set(lineage.get(added_field, [])):
            errors.append(
                f"projection lineage {added_field} differs from actual additions"
            )
    if not set(parent.get("entry_nodes", [])).issubset(
        set(projection.get("entry_nodes", []))
    ):
        errors.append("promoted projection removed a parent entry node")
    if parent.get("terminal_nodes") != projection.get("terminal_nodes"):
        errors.append("promoted projection changed contract terminals")
    if parent.get("semantic_dependencies") != projection.get("semantic_dependencies"):
        errors.append("promoted projection changed semantic dependencies")
    for field in ("requirement_mappings", "acceptance_mappings"):
        parent_mappings = parent.get(field, {})
        child_mappings = projection.get(field, {})
        if isinstance(parent_mappings, dict) and isinstance(child_mappings, dict):
            for key, values in parent_mappings.items():
                if not set(values).issubset(set(child_mappings.get(key, []))):
                    errors.append(f"promoted projection removed parent {field}: {key}")
    errors.extend(
        _projection_lineage_errors(
            run_dir,
            contract,
            parent,
            expected_run_id=expected_run_id,
            visited=visited,
        )
    )
    return errors


def _bound_projection(
    run_dir: Path,
    run: dict[str, Any],
) -> tuple[Path, dict[str, Any]]:
    binding = run["projection"]
    try:
        projection_path = resolve_project_path(run_dir.resolve(), binding["path"])
        projection = load_json(projection_path)
    except (KeyError, TypeError, ValueError, GraphControlError) as exc:
        raise RuntimeStateError(f"cannot load runtime projection: {exc}") from exc
    if sha256_json(projection) != binding["checksum"]:
        raise RuntimeStateError("runtime projection checksum does not match its binding")
    return projection_path, projection


def load_execution_graph(
    graph_path: Path,
    run_dir: Path,
    *,
    graph: dict[str, Any] | None = None,
    run: dict[str, Any] | None = None,
    verify_ledger: bool = True,
) -> dict[str, Any]:
    graph = graph or _load_graph(graph_path)
    run = run or load_run(run_dir)
    if run["schema_version"] == 1:
        return graph
    runtime_errors = _v2_runtime_errors(run)
    if runtime_errors:
        raise RuntimeStateError("invalid schema-v2 runtime: " + "; ".join(runtime_errors))
    if verify_ledger:
        ledger_errors = _runtime_ledger_errors(run_dir, run)
        if ledger_errors:
            raise RuntimeStateError(
                "runtime ledger integrity failed: " + "; ".join(ledger_errors)
            )
    _, projection = _bound_projection(run_dir, run)
    contract = run["outcome_contract"]
    errors = projection_contract_errors(
        graph,
        contract,
        projection,
        expected_run_id=run["run_id"],
        require_current_source=False,
    )
    errors.extend(
        _projection_lineage_errors(
            run_dir,
            contract,
            projection,
            expected_run_id=run["run_id"],
        )
    )
    if errors:
        raise RuntimeStateError("invalid runtime projection: " + "; ".join(errors))
    return execution_graph_from_projection(graph, projection)

def _refresh_status(graph: dict[str, Any], run: dict[str, Any]) -> None:
    ready = set(ready_node_ids(graph, run))
    definitions = _node_definitions(graph)
    for node_id, state in run["nodes"].items():
        if state["state"] not in {"planned", "ready", "blocked"}:
            continue
        if node_id in ready:
            state["state"] = "needs_human" if definitions[node_id]["type"] == "human_gate" else "ready"
        else:
            state["state"] = "blocked"
    completion_nodes = set(graph["terminal_nodes"])
    if run["schema_version"] == 2:
        for mapped in graph.get("acceptance_mappings", {}).values():
            completion_nodes.update(mapped)
    completion_states = [run["nodes"][node_id]["state"] for node_id in sorted(completion_nodes)]
    if completion_states and all(state == "verified" for state in completion_states):
        run["status"] = "completed"
    elif any(item["state"] == "running" for item in run["nodes"].values()):
        run["status"] = "running"
    elif any(item["state"] == "needs_human" for item in run["nodes"].values()):
        run["status"] = "needs_human"
    else:
        run["status"] = "active"


def _event_base(run: dict[str, Any], action: str, actor: str, **details: Any) -> dict[str, Any]:
    event: dict[str, Any] = {
        "run_id": run["run_id"],
        "graph_id": run["graph_id"],
        "graph_checksum": run_observed_graph_checksum(run),
        "run_version_before": run["run_version"],
        "run_version_after": run["run_version"] + 1,
        "actor": actor,
        "action": action,
    }
    if run["schema_version"] == 2:
        event["projection_checksum"] = run_execution_checksum(run)
        event["outcome_contract_digest"] = sha256_json(run["outcome_contract"])
        event["outcome_digest"] = run["outcome_contract"]["outcome_digest"]
    event.update({key: value for key, value in details.items() if value is not None})
    return event


def _commit(run_dir: Path, run: dict[str, Any], event: dict[str, Any]) -> dict[str, Any]:
    run_path, events_path, _ = _paths(run_dir)
    record = append_event(events_path, event)
    run["run_version"] = event["run_version_after"]
    run["updated_at"] = record["timestamp"]
    run["last_event_sequence"] = record["sequence"]
    run["last_event_hash"] = record["event_hash"]
    atomic_write_json(run_path, run)
    return run


def _json_document_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
        + b"\n"
    )


def _commit_initialization(
    run_dir: Path,
    run: dict[str, Any],
    event: dict[str, Any],
    *,
    projection_write: tuple[Path, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    run_path, events_path, _ = _paths(run_dir)
    record = dict(event)
    record.setdefault("timestamp", utc_now())
    record["sequence"] = 1
    record["previous_hash"] = None
    record.pop("event_hash", None)
    record["event_hash"] = sha256_json(record)
    run["run_version"] = event["run_version_after"]
    run["updated_at"] = record["timestamp"]
    run["last_event_sequence"] = record["sequence"]
    run["last_event_hash"] = record["event_hash"]
    writes: list[tuple[Path, bytes, int]] = []
    if projection_write is not None:
        projection_path, frozen_projection = projection_write
        writes.append((projection_path, _json_document_bytes(frozen_projection), 0o600))
    writes.extend(
        [
            (events_path, canonical_bytes(record) + b"\n", 0o600),
            (run_path, _json_document_bytes(run), 0o600),
        ]
    )
    atomic_write_batch(writes)
    return run


def _check_current(graph: dict[str, Any], run: dict[str, Any], expected_run_version: int) -> None:
    if run["run_version"] != expected_run_version:
        raise StaleStateError(
            f"stale run version: expected {expected_run_version}, current {run['run_version']}"
        )
    checksum = canonical_checksum(graph)
    if run_observed_graph_checksum(run) != checksum:
        raise StaleStateError(
            "graph checksum differs from initialized runtime state; use the governed rewrite command"
        )


def _mutate(
    graph_path: Path,
    run_dir: Path,
    expected_run_version: int,
    actor: str,
    action: str,
    mutation: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any] | None],
    *,
    node_id: str | None = None,
) -> dict[str, Any]:
    graph = _load_graph(graph_path)
    _, _, lock_path = _paths(run_dir)
    with file_lock(lock_path):
        run = load_run(run_dir)
        _check_current(graph, run, expected_run_version)
        execution_graph = load_execution_graph(
            graph_path,
            run_dir,
            graph=graph,
            run=run,
        )
        details = mutation(execution_graph, run) or {}
        _refresh_status(execution_graph, run)
        event = _event_base(run, action, actor, node_id=node_id, details=details)
        return _commit(run_dir, run, event)


def initialize_run(
    graph_path: Path,
    run_dir: Path,
    actor: str,
    *,
    force: bool = False,
    outcome_contract: dict[str, Any] | None = None,
    projection: dict[str, Any] | None = None,
    run_id: str | None = None,
    created_at: str | None = None,
    initial_action: str = "run-initialized",
    initial_details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    graph = _load_graph(graph_path)
    if (outcome_contract is None) != (projection is None):
        raise ValueError("outcome contract and projection must be supplied together")

    graph_checksum = canonical_checksum(graph)
    project_root = project_root_for_graph(graph_path, graph)
    definition_path = serialize_relative(
        project_root,
        graph_path,
        label="graph definition",
    )
    if run_id is None:
        run_id = str(uuid.uuid4())
    else:
        try:
            parsed_run_id = uuid.UUID(run_id)
        except (TypeError, ValueError, AttributeError) as exc:
            raise ValueError("run_id must be a canonical UUID") from exc
        if str(parsed_run_id) != run_id:
            raise ValueError("run_id must be a canonical UUID")
    if created_at is not None:
        if not _nonempty_text(created_at):
            raise ValueError("created_at must be non-empty")
        _parse_time(created_at)
    if not _nonempty_text(initial_action):
        raise ValueError("initial_action must be non-empty")
    if initial_details is not None and not isinstance(initial_details, dict):
        raise ValueError("initial_details must be an object")
    schema_version = 2 if outcome_contract is not None else 1
    frozen_contract: dict[str, Any] | None = None
    frozen_projection: dict[str, Any] | None = None
    projection_relative: str | None = None
    projection_checksum: str | None = None
    execution_graph = graph
    if schema_version == 2:
        frozen_contract = json.loads(json.dumps(outcome_contract))
        frozen_projection = json.loads(json.dumps(projection))
        contract_errors = outcome_contract_errors(frozen_contract)
        projection_errors = projection_contract_errors(
            graph,
            frozen_contract,
            frozen_projection,
            expected_graph_checksum=graph_checksum,
        )
        errors = contract_errors + [
            item for item in projection_errors if item not in contract_errors
        ]
        if errors:
            raise ValueError("schema-v2 initialization failed: " + "; ".join(errors))
        frozen_projection["run_id"] = run_id
        persisted_errors = projection_contract_errors(
            graph,
            frozen_contract,
            frozen_projection,
            expected_graph_checksum=graph_checksum,
            expected_run_id=run_id,
        )
        if persisted_errors:
            raise ValueError("schema-v2 initialization failed: " + "; ".join(persisted_errors))
        projection_checksum = sha256_json(frozen_projection)
        projection_relative = (
            f"projections/{run_id}-v{frozen_projection['projection_version']}.json"
        )
        execution_graph = execution_graph_from_projection(graph, frozen_projection)

    run_path, events_path, lock_path = _paths(run_dir)
    run_dir = run_dir.resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "evidence").mkdir(exist_ok=True)
    (run_dir / "snapshots").mkdir(exist_ok=True)
    if schema_version == 2:
        (run_dir / "projections").mkdir(exist_ok=True)
    with file_lock(lock_path):
        if run_path.exists() and not force:
            raise RuntimeStateError(f"runtime already initialized: {run_path}")
        if events_path.exists():
            raise RuntimeStateError("refusing to overwrite an existing event ledger; create a new run directory")
        created = created_at or utc_now()
        run: dict[str, Any] = {
            "schema_version": schema_version,
            "run_id": run_id,
            "run_version": 0,
            "graph_id": graph["graph_id"],
            "status": "active",
            "created_at": created,
            "updated_at": created,
            "last_event_sequence": 0,
            "last_event_hash": None,
            "feedback_traversals": {},
            "nodes": {},
        }
        if schema_version == 1:
            run["graph_version"] = graph["graph_version"]
            run["graph_checksum"] = graph_checksum
        else:
            run["observed_graph_version"] = graph["graph_version"]
            run["observed_graph_checksum"] = graph_checksum
            run["outcome_contract"] = frozen_contract
            run["projection"] = {
                "version": frozen_projection["projection_version"],
                "path": projection_relative,
                "checksum": projection_checksum,
            }
            run["episodes"] = {}
        for node in execution_graph["nodes"]:
            run["nodes"][node["id"]] = {
                "state": "planned",
                "attempts": 0,
                "generation": 1,
                "lease": None,
                "approval": None,
                "completed_idempotency_keys": [],
                "evidence": [],
                "last_result": None,
            }
        _refresh_status(execution_graph, run)
        event_details: dict[str, Any] = {
            "definition_path": definition_path,
            "projection_path": projection_relative,
            "contract_id": (
                frozen_contract["contract_id"]
                if frozen_contract is not None
                else None
            ),
        }
        if initial_details:
            reserved = set(event_details) & set(initial_details)
            if reserved:
                raise ValueError(
                    "initial_details cannot replace initialization fields: "
                    + ", ".join(sorted(reserved))
                )
            event_details.update(copy.deepcopy(initial_details))
        event = _event_base(
            run,
            initial_action,
            actor,
            **event_details,
        )
        projection_write = (
            (
                resolve_project_path(run_dir, projection_relative),
                frozen_projection,
            )
            if schema_version == 2
            else None
        )
        return _commit_initialization(
            run_dir,
            run,
            event,
            projection_write=projection_write,
        )


def _approval_is_current(node_state: dict[str, Any], run: dict[str, Any], node_id: str) -> bool:
    approval = node_state.get("approval")
    return bool(
        isinstance(approval, dict)
        and approval.get("decision") == "approved"
        and approval.get("run_id") == run["run_id"]
        and approval.get("graph_checksum") == run_execution_checksum(run)
        and approval.get("node_id") == node_id
    )


def claim_node(
    graph_path: Path,
    run_dir: Path,
    node_id: str,
    worker_id: str,
    expected_run_version: int,
    lease_seconds: int = 300,
    *,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    if lease_seconds < 1:
        raise ValueError("lease_seconds must be positive")

    def claim(graph: dict[str, Any], run: dict[str, Any]) -> dict[str, Any]:
        definitions = _node_definitions(graph)
        if node_id not in definitions:
            raise RuntimeStateError(f"unknown node: {node_id}")
        state = run["nodes"][node_id]
        if state["state"] != "ready":
            raise RuntimeStateError(f"node {node_id} is {state['state']}, not ready")
        for edge in graph["edges"]:
            if edge["type"] != "conflicts" or node_id not in {edge["from"], edge["to"]}:
                continue
            other_id = edge["to"] if edge["from"] == node_id else edge["from"]
            if run["nodes"][other_id]["state"] in {"running", "verifying"}:
                raise RuntimeStateError(f"node {node_id} conflicts with active node {other_id}")
        definition = definitions[node_id]
        if definition["side_effects"]["approval_required"] and not _approval_is_current(state, run, node_id):
            raise PermissionError(f"node {node_id} requires a current approval")
        maximum = definition["execution"]["max_attempts"]
        if state["attempts"] >= maximum:
            state["state"] = "needs_human"
            raise RuntimeStateError(f"node {node_id} exhausted {maximum} attempts")
        attempt = state["attempts"] + 1
        key = idempotency_key or f"{run['run_id']}:{node_id}:g{state['generation']}:a{attempt}"
        if key in state["completed_idempotency_keys"]:
            raise RuntimeStateError(f"idempotency key already completed: {key}")
        expires = datetime.now(timezone.utc) + timedelta(seconds=lease_seconds)
        state["attempts"] = attempt
        state["state"] = "running"
        state["lease"] = {
            "owner": worker_id,
            "issued_at": utc_now(),
            "expires_at": expires.isoformat(timespec="microseconds").replace("+00:00", "Z"),
            "idempotency_key": key,
        }
        return {"worker_id": worker_id, "attempt": attempt, "lease_seconds": lease_seconds, "idempotency_key": key}

    return _mutate(graph_path, run_dir, expected_run_version, worker_id, "node-claimed", claim, node_id=node_id)


def _require_lease(state: dict[str, Any], worker_id: str) -> dict[str, Any]:
    lease = state.get("lease")
    if state.get("state") != "running" or not isinstance(lease, dict):
        raise RuntimeStateError("node is not running with an active lease")
    if lease.get("owner") != worker_id:
        raise PermissionError(f"lease is owned by {lease.get('owner')!r}, not {worker_id!r}")
    return lease


def _validated_consumed_artifact_evidence(
    evidence: Any,
    definition: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    if not isinstance(evidence, dict):
        raise RuntimeStateError("schema-v2 completion evidence must be an object")
    recorded = copy.deepcopy(evidence)
    if "consumed_artifacts" not in recorded:
        return recorded, []
    raw = recorded["consumed_artifacts"]
    if not isinstance(raw, list):
        raise RuntimeStateError("consumed_artifacts must be an array")
    declared = {
        item.get("artifact")
        for item in definition.get("inputs", [])
        if isinstance(item, dict) and isinstance(item.get("artifact"), str)
    }
    normalized: list[dict[str, str]] = []
    seen: set[str] = set()
    errors: list[str] = []
    for index, observation in enumerate(raw):
        label = f"consumed_artifacts[{index}]"
        if not isinstance(observation, dict) or set(observation) != {
            "artifact_id",
            "artifact_digest",
        }:
            errors.append(f"{label} fields are invalid")
            continue
        artifact_id = observation.get("artifact_id")
        artifact_digest = observation.get("artifact_digest")
        if not isinstance(artifact_id, str) or not artifact_id.strip():
            errors.append(f"{label}.artifact_id must be non-empty")
            continue
        if artifact_id in seen:
            errors.append(f"duplicate consumed artifact: {artifact_id}")
        seen.add(artifact_id)
        if artifact_id not in declared:
            errors.append(f"undeclared consumed artifact: {artifact_id}")
        if not _is_checksum(artifact_digest):
            errors.append(f"{label}.artifact_digest must be a SHA-256 digest")
        if artifact_id in declared and _is_checksum(artifact_digest):
            normalized.append(
                {
                    "artifact_id": artifact_id,
                    "artifact_digest": artifact_digest,
                }
            )
    if errors:
        raise RuntimeStateError(
            "invalid consumed artifact evidence: " + "; ".join(errors)
        )
    normalized.sort(key=lambda item: item["artifact_id"])
    recorded["consumed_artifacts"] = normalized
    return recorded, normalized


def complete_node(
    graph_path: Path,
    run_dir: Path,
    node_id: str,
    worker_id: str,
    expected_run_version: int,
    evidence: dict[str, Any],
) -> dict[str, Any]:
    def complete(graph: dict[str, Any], run: dict[str, Any]) -> dict[str, Any]:
        state = run["nodes"].get(node_id)
        if state is None:
            raise RuntimeStateError(f"unknown node: {node_id}")
        lease = _require_lease(state, worker_id)
        recorded_evidence = evidence
        consumed_artifacts: list[dict[str, str]] = []
        if run["schema_version"] == 2:
            recorded_evidence, consumed_artifacts = (
                _validated_consumed_artifact_evidence(
                    evidence,
                    _node_definitions(graph)[node_id],
                )
            )
        state["state"] = "verified"
        state["lease"] = None
        state["last_result"] = recorded_evidence
        state["evidence"].append(recorded_evidence)
        state["completed_idempotency_keys"].append(lease["idempotency_key"])
        details = {
            "worker_id": worker_id,
            "attempt": state["attempts"],
            "evidence_digest": sha256_json(recorded_evidence),
        }
        if run["schema_version"] == 2:
            details["node_generation"] = state["generation"]
            details["consumed_artifacts_digest"] = sha256_json(
                consumed_artifacts
            )
            episode_progress = record_acceptance_progress(
                run,
                graph,
                event_sequence=run["last_event_sequence"] + 1,
            )
            if episode_progress:
                details["episode_progress"] = episode_progress
        return details

    return _mutate(graph_path, run_dir, expected_run_version, worker_id, "node-completed", complete, node_id=node_id)


def fail_node(
    graph_path: Path,
    run_dir: Path,
    node_id: str,
    worker_id: str,
    expected_run_version: int,
    result: dict[str, Any],
) -> dict[str, Any]:
    def fail(graph: dict[str, Any], run: dict[str, Any]) -> dict[str, Any]:
        state = run["nodes"].get(node_id)
        if state is None:
            raise RuntimeStateError(f"unknown node: {node_id}")
        _require_lease(state, worker_id)
        maximum = _node_definitions(graph)[node_id]["execution"]["max_attempts"]
        feedback_exhausted = any(
            edge["type"] == "feedback"
            and edge["from"] == node_id
            and run["feedback_traversals"].get(edge["id"], 0) >= edge["max_traversals"]
            for edge in graph["edges"]
        )
        state["state"] = "needs_human" if state["attempts"] >= maximum or feedback_exhausted else "failed"
        state["lease"] = None
        state["last_result"] = result
        state["evidence"].append(result)
        return {"worker_id": worker_id, "attempt": state["attempts"], "result_digest": sha256_json(result)}

    return _mutate(graph_path, run_dir, expected_run_version, worker_id, "node-failed", fail, node_id=node_id)


def follow_feedback(
    graph_path: Path,
    run_dir: Path,
    edge_id: str,
    actor: str,
    expected_run_version: int,
) -> dict[str, Any]:
    def traverse(graph: dict[str, Any], run: dict[str, Any]) -> dict[str, Any]:
        if run["schema_version"] == 2:
            raise RuntimeStateError(
                "schema-v2 feedback traversal is unavailable until semantic invalidation is installed"
            )
        matches = [edge for edge in graph["edges"] if edge["id"] == edge_id and edge["type"] == "feedback"]
        if len(matches) != 1:
            raise RuntimeStateError(f"unknown feedback edge: {edge_id}")
        edge = matches[0]
        source_state = run["nodes"][edge["from"]]["state"]
        if source_state not in {"failed", "needs_human"}:
            raise RuntimeStateError(
                f"feedback source {edge['from']} is {source_state}; a recorded failure is required"
            )
        traversals = run["feedback_traversals"].get(edge_id, 0)
        if traversals >= edge["max_traversals"]:
            raise RuntimeStateError(
                f"feedback edge {edge_id} exhausted its {edge['max_traversals']} traversals"
            )
        affected = affected_node_ids(graph, {edge["to"]})
        running = [node_id for node_id in affected if run["nodes"][node_id]["state"] == "running"]
        if running:
            raise RuntimeStateError(f"cannot traverse feedback across running nodes: {', '.join(running)}")
        for affected_id in affected:
            state = run["nodes"][affected_id]
            state["state"] = "planned"
            state["attempts"] = 0
            state["generation"] += 1
            state["lease"] = None
            state["approval"] = None
            state["evidence"] = []
            state["last_result"] = None
        run["feedback_traversals"][edge_id] = traversals + 1
        return {
            "edge_id": edge_id,
            "source": edge["from"],
            "target": edge["to"],
            "traversal": traversals + 1,
            "maximum": edge["max_traversals"],
            "reopened_nodes": affected,
        }

    return _mutate(graph_path, run_dir, expected_run_version, actor, "feedback-traversed", traverse)


def approve_node(
    graph_path: Path,
    run_dir: Path,
    node_id: str,
    approver: str,
    expected_run_version: int,
    evidence: str,
) -> dict[str, Any]:
    def approve(graph: dict[str, Any], run: dict[str, Any]) -> dict[str, Any]:
        definitions = _node_definitions(graph)
        if node_id not in definitions or definitions[node_id]["type"] != "human_gate":
            raise RuntimeStateError(f"node {node_id} is not a human gate")
        gate_state = run["nodes"][node_id]
        if gate_state["state"] != "needs_human":
            raise RuntimeStateError(f"human gate {node_id} is {gate_state['state']}, not needs_human")
        approved_targets = []
        evidence_digest = hashlib.sha256(evidence.encode("utf-8")).hexdigest()
        for edge in graph["edges"]:
            if edge["type"] == "approves" and edge["from"] == node_id:
                target = edge["to"]
                run["nodes"][target]["approval"] = {
                    "decision": "approved",
                    "run_id": run["run_id"],
                    "graph_checksum": run_execution_checksum(run),
                    "node_id": target,
                    "gate_node_id": node_id,
                    "approver": approver,
                    "evidence_digest": evidence_digest,
                    "approved_at": utc_now(),
                }
                approved_targets.append(target)
        gate_state["state"] = "verified"
        gate_state["last_result"] = {"decision": "approved", "approver": approver, "evidence_digest": evidence_digest}
        return {"approver": approver, "approved_targets": sorted(approved_targets), "evidence_digest": evidence_digest}

    return _mutate(graph_path, run_dir, expected_run_version, approver, "gate-approved", approve, node_id=node_id)


def reject_node(
    graph_path: Path,
    run_dir: Path,
    node_id: str,
    approver: str,
    expected_run_version: int,
    reason: str,
) -> dict[str, Any]:
    def reject(graph: dict[str, Any], run: dict[str, Any]) -> dict[str, Any]:
        definition = _node_definitions(graph).get(node_id)
        if definition is None or definition["type"] != "human_gate":
            raise RuntimeStateError(f"node {node_id} is not a human gate")
        state = run["nodes"][node_id]
        if state["state"] != "needs_human":
            raise RuntimeStateError(f"human gate {node_id} is {state['state']}, not needs_human")
        state["state"] = "failed"
        state["last_result"] = {"decision": "rejected", "approver": approver, "reason": reason}
        return {"approver": approver, "reason": reason}

    return _mutate(graph_path, run_dir, expected_run_version, approver, "gate-rejected", reject, node_id=node_id)


def invalidate_nodes(
    graph_path: Path,
    run_dir: Path,
    node_ids: list[str],
    artifact_ids: list[str],
    reason: str,
    expected_run_version: int,
    *,
    actor: str = "operator",
) -> dict[str, Any]:
    def invalidate(graph: dict[str, Any], run: dict[str, Any]) -> dict[str, Any]:
        if run["schema_version"] == 2:
            raise RuntimeStateError(
                "schema-v2 invalidation is unavailable until semantic invalidation is installed"
            )
        known = set(run["nodes"])
        unknown = sorted(set(node_ids) - known)
        if unknown:
            raise RuntimeStateError(f"unknown invalidation nodes: {', '.join(unknown)}")
        seeds = set(node_ids) | artifact_affected_nodes(graph, set(artifact_ids))
        affected = affected_node_ids(graph, seeds)
        if not affected:
            raise RuntimeStateError("invalidation selected no nodes")
        running = [item for item in affected if run["nodes"][item]["state"] == "running"]
        if running:
            raise RuntimeStateError(f"cannot invalidate running nodes: {', '.join(running)}")
        for affected_id in affected:
            state = run["nodes"][affected_id]
            state["state"] = "invalidated"
            state["lease"] = None
            state["approval"] = None
            state["last_result"] = {"invalidated_reason": reason}
        return {"reason": reason, "requested_nodes": sorted(node_ids), "artifacts": sorted(artifact_ids), "affected_nodes": affected}

    return _mutate(graph_path, run_dir, expected_run_version, actor, "nodes-invalidated", invalidate)


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def resume_nodes(
    graph_path: Path,
    run_dir: Path,
    node_ids: list[str],
    actor: str,
    expected_run_version: int,
) -> dict[str, Any]:
    def resume(graph: dict[str, Any], run: dict[str, Any]) -> dict[str, Any]:
        resumed: list[str] = []
        escalated: list[dict[str, Any]] = []
        now = datetime.now(timezone.utc)
        for resume_id in node_ids:
            if resume_id not in run["nodes"]:
                raise RuntimeStateError(f"unknown node: {resume_id}")
            state = run["nodes"][resume_id]
            current = state["state"]
            if current == "running":
                lease = state.get("lease")
                if not isinstance(lease, dict) or _parse_time(lease["expires_at"]) > now:
                    raise RuntimeStateError(f"node {resume_id} has a live lease")
                state["lease"] = None
                state["state"] = "failed"
            if state["state"] == "invalidated":
                state["attempts"] = 0
                state["generation"] += 1
                state["evidence"] = []
                state["last_result"] = None
                state["state"] = "planned"
                resumed.append(resume_id)
            elif state["state"] == "failed":
                maximum = _node_definitions(graph)[resume_id]["execution"]["max_attempts"]
                if state["attempts"] >= maximum:
                    state["state"] = "needs_human"
                    escalated.append({
                        "node_id": resume_id,
                        "reason": "attempts-exhausted",
                        "attempts": state["attempts"],
                        "max_attempts": maximum,
                    })
                    continue
                state["state"] = "planned"
                resumed.append(resume_id)
            else:
                raise RuntimeStateError(f"node {resume_id} in state {current} cannot resume")
        return {"resumed_nodes": resumed, "escalated_nodes": escalated}

    return _mutate(graph_path, run_dir, expected_run_version, actor, "nodes-resumed", resume)



_SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

_PROMOTION_FIELDS = {
    "schema_version", "promotion_id", "causal_episode_id", "target_run_id",
    "expected_run_version", "expected_outcome_contract_digest",
    "expected_graph_version", "expected_graph_checksum",
    "expected_projection_checksum", "classification", "node_ids", "edge_ids",
    "requirement_mappings", "acceptance_mappings", "semantic_invalidation",
    "fanout_policy_result", "impact_gate", "episode_gate", "actor", "authority",
    "authority_evidence", "rationale", "evidence",
}


def _document_shape_errors(
    value: Any,
    expected: set[str],
    label: str,
) -> list[str]:
    if not isinstance(value, dict):
        return [f"{label} must be an object"]
    return [
        *(f"{label} missing field: {field}" for field in sorted(expected - set(value))),
        *(f"{label} unknown field: {field}" for field in sorted(set(value) - expected)),
    ]


def _nonempty_text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _slug_list_errors(value: Any, label: str, *, require_one: bool = False) -> list[str]:
    if not isinstance(value, list):
        return [f"{label} must be an array"]
    if require_one and not value:
        return [f"{label} must not be empty"]
    if any(not isinstance(item, str) or not _SLUG.fullmatch(item) for item in value):
        return [f"{label} must contain lowercase slugs"]
    if len(value) != len(set(value)):
        return [f"{label} must contain unique values"]
    return []


def _evidence_errors(value: Any, label: str) -> list[str]:
    if (
        not isinstance(value, list)
        or not value
        or any(not _nonempty_text(item) for item in value)
    ):
        return [f"{label} must contain non-empty evidence"]
    return []


def _mapping_errors(value: Any, label: str) -> list[str]:
    if not isinstance(value, dict):
        return [f"{label} must be an object"]
    errors: list[str] = []
    for key, node_ids in value.items():
        if not _nonempty_text(key):
            errors.append(f"{label} keys must be non-empty strings")
        errors.extend(_slug_list_errors(node_ids, f"{label}.{key}", require_one=True))
    return errors


def _binding_errors(
    request: dict[str, Any],
    graph: dict[str, Any],
    run: dict[str, Any],
) -> list[str]:
    errors: list[str] = []
    if run.get("schema_version") != 2:
        return ["operation requires a schema-v2 projected run"]
    if request.get("target_run_id") != run.get("run_id"):
        errors.append("target run ID does not match runtime")
    if request.get("expected_run_version") != run.get("run_version"):
        errors.append("expected run version is stale")
    if request.get("expected_graph_version") != graph.get("graph_version"):
        errors.append("expected graph version is stale")
    if request.get("expected_graph_checksum") != canonical_checksum(graph):
        errors.append("expected graph checksum is stale")
    if request.get("expected_outcome_contract_digest") != outcome_contract_digest(
        run["outcome_contract"]
    ):
        errors.append("expected outcome contract digest is stale")
    if request.get("expected_projection_checksum") != run_execution_checksum(run):
        errors.append("expected projection checksum is stale")
    return errors


def _request_common_errors(
    request: Any,
    fields: set[str],
    id_fields: tuple[str, ...],
) -> list[str]:
    errors = _document_shape_errors(request, fields, "request")
    if not isinstance(request, dict):
        return errors
    if request.get("schema_version") != 1:
        errors.append("request schema_version must equal 1")
    for field in id_fields:
        value = request.get(field)
        if not isinstance(value, str) or not _SLUG.fullmatch(value):
            errors.append(f"request {field} must be a lowercase slug")
    for field in (
        "expected_outcome_contract_digest",
        "expected_graph_checksum",
        "expected_projection_checksum",
    ):
        if not _is_checksum(request.get(field)):
            errors.append(f"request {field} must be a SHA-256 digest")
    for field in ("expected_run_version", "expected_graph_version"):
        if not _is_positive_int(request.get(field)):
            errors.append(f"request {field} must be a positive integer")
    for field in ("actor", "authority"):
        if not _nonempty_text(request.get(field)):
            errors.append(f"request {field} must be non-empty")
    try:
        uuid.UUID(request.get("target_run_id"))
    except (AttributeError, TypeError, ValueError):
        errors.append("request target_run_id must be a UUID")
    return errors



def promotion_document_errors(request: Any) -> list[str]:
    errors = _request_common_errors(
        request,
        _PROMOTION_FIELDS,
        ("promotion_id", "causal_episode_id"),
    )
    if not isinstance(request, dict):
        return errors
    classification = request.get("classification")
    if classification not in {
        "existing-requirement",
        "current-regression",
        "approved-outcome-change",
        "deferred",
    }:
        errors.append("promotion classification is invalid")
    errors.extend(
        _slug_list_errors(
            request.get("node_ids"),
            "promotion node_ids",
            require_one=True,
        )
    )
    errors.extend(_slug_list_errors(request.get("edge_ids"), "promotion edge_ids"))
    errors.extend(
        _mapping_errors(
            request.get("requirement_mappings"),
            "promotion requirement_mappings",
        )
    )
    errors.extend(
        _mapping_errors(
            request.get("acceptance_mappings"),
            "promotion acceptance_mappings",
        )
    )
    semantic = request.get("semantic_invalidation")
    if not isinstance(semantic, dict) or set(semantic) != {"nodes", "artifacts"}:
        errors.append(
            "promotion semantic_invalidation must contain exactly nodes and artifacts"
        )
    else:
        errors.extend(
            _slug_list_errors(
                semantic.get("nodes"),
                "promotion semantic_invalidation.nodes",
            )
        )
        errors.extend(
            _artifact_changes_errors(
                semantic.get("artifacts"),
                "promotion semantic_invalidation.artifacts",
            )
        )
        if classification == "current-regression":
            if semantic.get("nodes") == [] and semantic.get("artifacts") == []:
                errors.append(
                    "current-regression promotion requires at least one semantic "
                    "node or artifact seed"
                )
        elif semantic.get("nodes") != [] or semantic.get("artifacts") != []:
            errors.append(
                "only current-regression promotion accepts semantic invalidation seeds"
            )
    fanout = request.get("fanout_policy_result")
    if (
        not isinstance(fanout, dict)
        or set(fanout) != {"allowed", "basis"}
        or not isinstance(fanout.get("allowed"), bool)
        or not _nonempty_text(fanout.get("basis"))
    ):
        errors.append(
            "promotion fanout policy must contain a boolean allowed result "
            "and non-empty basis"
        )
    elif classification != "current-regression" and fanout.get("allowed") is not True:
        errors.append(
            "non-regression promotion fanout policy must allow zero invalidation impact"
        )
    errors.extend(_impact_gate_document_errors(request.get("impact_gate")))
    errors.extend(episode_gate_document_errors(request.get("episode_gate")))
    if classification != "current-regression" and request.get("impact_gate") is not None:
        errors.append("only current-regression promotion accepts an impact gate")
    if not _nonempty_text(request.get("rationale")):
        errors.append("promotion rationale must be non-empty")
    errors.extend(_evidence_errors(request.get("evidence"), "promotion evidence"))
    errors.extend(
        _evidence_errors(
            request.get("authority_evidence"),
            "promotion authority_evidence",
        )
    )
    return errors


def _merge_mappings(
    current: dict[str, list[str]],
    additions: dict[str, list[str]],
) -> dict[str, list[str]]:
    merged = copy.deepcopy(current)
    for key, values in additions.items():
        existing = merged.setdefault(key, [])
        for node_id in values:
            if node_id not in existing:
                existing.append(node_id)
    return merged


def _promotion_candidate(
    request: dict[str, Any],
    graph: dict[str, Any],
    run: dict[str, Any],
    parent: dict[str, Any],
) -> tuple[dict[str, Any] | None, list[str]]:
    errors: list[str] = []
    requested_nodes = request["node_ids"]
    requested_edges = request["edge_ids"]
    requested_node_set = set(requested_nodes)
    parent_node_ids = {item["id"] for item in parent["nodes"]}
    parent_edge_ids = {item["id"] for item in parent["edges"]}
    source_nodes = _node_definitions(graph)
    source_edges = {
        item["id"]: item
        for item in graph["edges"]
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    unknown_nodes = sorted(requested_node_set - set(source_nodes))
    unknown_edges = sorted(set(requested_edges) - set(source_edges))
    already_nodes = sorted(requested_node_set & parent_node_ids)
    already_edges = sorted(set(requested_edges) & parent_edge_ids)
    if unknown_nodes:
        errors.append("promotion references unknown durable nodes: " + ", ".join(unknown_nodes))
    if unknown_edges:
        errors.append("promotion references unknown durable edges: " + ", ".join(unknown_edges))
    if already_nodes:
        errors.append("promotion nodes are already projected: " + ", ".join(already_nodes))
    if already_edges:
        errors.append("promotion edges are already projected: " + ", ".join(already_edges))

    contract = run["outcome_contract"]
    if not set(request["requirement_mappings"]).issubset(
        set(contract["requirement_ids"])
    ):
        errors.append("promotion maps to requirements outside the outcome contract")
    if not set(request["acceptance_mappings"]).issubset(
        set(contract["acceptance_ids"])
    ):
        errors.append("promotion maps to acceptance outside the outcome contract")
    mapped_nodes = {
        node_id
        for mappings in (
            request["requirement_mappings"],
            request["acceptance_mappings"],
        )
        for values in mappings.values()
        for node_id in values
    }
    if mapped_nodes != requested_node_set:
        errors.append("every promoted node must map exactly to the existing outcome contract")
    combined_nodes = parent_node_ids | requested_node_set
    for edge_id in requested_edges:
        edge = source_edges.get(edge_id)
        if edge is None:
            continue
        if edge["from"] not in combined_nodes or edge["to"] not in combined_nodes:
            errors.append(f"promotion edge has an unenrolled endpoint: {edge_id}")
        if edge["from"] not in requested_node_set and edge["to"] not in requested_node_set:
            errors.append(f"promotion edge does not enroll requested work: {edge_id}")
    if errors:
        return None, errors

    candidate = copy.deepcopy(parent)
    candidate["source_graph_version"] = graph["graph_version"]
    candidate["source_graph_checksum"] = canonical_checksum(graph)
    candidate["projection_version"] = parent["projection_version"] + 1
    candidate["nodes"].extend(
        copy.deepcopy(item)
        for item in graph["nodes"]
        if item["id"] in requested_node_set
    )
    candidate["edges"].extend(
        copy.deepcopy(item)
        for item in graph["edges"]
        if item["id"] in set(requested_edges)
    )
    for node_id in graph["entry_nodes"]:
        if node_id in requested_node_set and node_id not in candidate["entry_nodes"]:
            candidate["entry_nodes"].append(node_id)
    candidate["requirement_mappings"] = _merge_mappings(
        parent["requirement_mappings"],
        request["requirement_mappings"],
    )
    candidate["acceptance_mappings"] = _merge_mappings(
        parent["acceptance_mappings"],
        request["acceptance_mappings"],
    )
    candidate["provenance"] = {
        "actor": request["actor"],
        "authority": request["authority"],
    }
    candidate["lineage"] = {
        "parent_path": run["projection"]["path"],
        "parent_checksum": run["projection"]["checksum"],
        "parent_version": parent["projection_version"],
        "promotion_id": request["promotion_id"],
        "added_node_ids": list(requested_nodes),
        "added_edge_ids": list(requested_edges),
    }
    errors.extend(
        projection_contract_errors(
            graph,
            contract,
            candidate,
            expected_run_id=run["run_id"],
            require_current_source=False,
        )
    )
    return candidate, errors



def _next_event_payload_candidate(
    candidate_run: dict[str, Any],
    event: dict[str, Any],
    previous_payload: bytes,
    previous_record: dict[str, Any],
    *,
    timestamp: str,
) -> tuple[dict[str, Any], bytes]:
    if event.get("run_version_before") != candidate_run.get("run_version"):
        raise RuntimeStateError("staged event run version does not match candidate run")
    if event.get("run_version_after") != candidate_run.get("run_version", 0) + 1:
        raise RuntimeStateError("staged event must advance the run version exactly once")
    record = copy.deepcopy(event)
    record["timestamp"] = timestamp
    record["sequence"] = int(previous_record["sequence"]) + 1
    record["previous_hash"] = previous_record["event_hash"]
    record.pop("event_hash", None)
    record["event_hash"] = sha256_json(record)
    separator = b"" if previous_payload.endswith(b"\n") else b"\n"
    payload = previous_payload + separator + canonical_bytes(record) + b"\n"
    candidate_run["run_version"] = record["run_version_after"]
    candidate_run["updated_at"] = timestamp
    candidate_run["last_event_sequence"] = record["sequence"]
    candidate_run["last_event_hash"] = record["event_hash"]
    return record, payload


def _event_payload_candidate(
    run_dir: Path,
    candidate_run: dict[str, Any],
    event: dict[str, Any],
    *,
    timestamp: str,
) -> tuple[dict[str, Any], bytes]:
    events_path = run_dir.resolve() / EVENTS_FILENAME
    errors = verify_event_chain(events_path)
    if errors:
        raise RuntimeStateError(
            "cannot append to an invalid event ledger: " + "; ".join(errors)
        )
    previous_payload = events_path.read_bytes()
    records = _event_records(run_dir)
    if not records:
        raise RuntimeStateError("event ledger is empty")
    return _next_event_payload_candidate(
        candidate_run,
        event,
        previous_payload,
        records[-1],
        timestamp=timestamp,
    )


def _project_relative(project_root: Path, path: Path) -> str:
    return serialize_relative(project_root, path, label="lifecycle artifact")



def _promotion_transaction(
    request: dict[str, Any],
    graph_path: Path,
    run_dir: Path,
    graph: dict[str, Any],
    run: dict[str, Any],
    candidate_projection: dict[str, Any],
    episode_transition: dict[str, Any],
    *,
    transaction_timestamp: str,
    semantic_impact: dict[str, Any] | None = None,
    impact_digest: str | None = None,
) -> dict[str, Any]:
    classification = request["classification"]
    if classification == "current-regression":
        if semantic_impact is None or impact_digest is None:
            raise RuntimeStateError(
                "current-regression transaction requires bound semantic impact"
            )
    elif semantic_impact is not None or impact_digest is not None:
        raise RuntimeStateError(
            "non-regression promotion cannot carry semantic invalidation impact"
        )

    project_root = project_root_for_graph(graph_path, graph)
    projection_relative = (
        f"projections/{run['run_id']}-v"
        f"{candidate_projection['projection_version']}.json"
    )
    projection_path = resolve_project_path(run_dir.resolve(), projection_relative)
    if projection_path.exists():
        raise RuntimeStateError(
            f"projection snapshot already exists: {projection_relative}"
        )
    before_checksum = run_execution_checksum(run)
    after_checksum = sha256_json(candidate_projection)
    candidate_run = copy.deepcopy(run)
    candidate_run["projection"] = {
        "version": candidate_projection["projection_version"],
        "path": projection_relative,
        "checksum": after_checksum,
    }
    for node in candidate_projection["nodes"]:
        node_id = node["id"]
        if node_id not in candidate_run["nodes"]:
            candidate_run["nodes"][node_id] = {
                "state": "planned",
                "attempts": 0,
                "generation": 1,
                "lease": None,
                "approval": None,
                "completed_idempotency_keys": [],
                "evidence": [],
                "last_result": None,
            }
    execution_graph = execution_graph_from_projection(graph, candidate_projection)
    _refresh_status(execution_graph, candidate_run)
    episode_details = apply_repair_cycle(
        candidate_run,
        episode_transition,
        timestamp=transaction_timestamp,
    )
    graph_checksum = canonical_checksum(graph)
    promotion_details = {
        "promotion_id": request["promotion_id"],
        "causal_episode_id": request["causal_episode_id"],
        "classification": classification,
        "node_ids": list(request["node_ids"]),
        "edge_ids": list(request["edge_ids"]),
        "requirement_mappings": copy.deepcopy(request["requirement_mappings"]),
        "acceptance_mappings": copy.deepcopy(request["acceptance_mappings"]),
        "projection_checksum_before": before_checksum,
        "projection_checksum_after": after_checksum,
        "graph_checksum_before": graph_checksum,
        "graph_checksum_after": graph_checksum,
        "semantic_invalidation": copy.deepcopy(
            request["semantic_invalidation"]
        ),
        "fanout_policy_result": copy.deepcopy(request["fanout_policy_result"]),
        "authority": request["authority"],
        "authority_evidence": list(request["authority_evidence"]),
        **episode_details,
    }
    if impact_digest is not None:
        promotion_details["impact_digest"] = impact_digest
    promotion_event = _event_base(
        candidate_run,
        "run-nodes-promoted",
        request["actor"],
        details=promotion_details,
    )
    promotion_record, events_payload = _event_payload_candidate(
        run_dir,
        candidate_run,
        promotion_event,
        timestamp=transaction_timestamp,
    )
    records = [promotion_record]
    invalidated_node_ids: list[str] = []

    if classification == "current-regression":
        assert semantic_impact is not None
        assert impact_digest is not None
        invalidated_node_ids = list(semantic_impact["verified_node_ids"])
        for node_id in invalidated_node_ids:
            state = candidate_run["nodes"][node_id]
            state["state"] = "invalidated"
            state["lease"] = None
            state["approval"] = None
            state["last_result"] = {
                "invalidated_reason": request["rationale"],
                "impact_digest": impact_digest,
            }
        _refresh_status(execution_graph, candidate_run)
        semantic = request["semantic_invalidation"]
        invalidation_event = _event_base(
            candidate_run,
            "semantic-nodes-invalidated",
            request["actor"],
            details={
                "promotion_id": request["promotion_id"],
                "causal_episode_id": request["causal_episode_id"],
                "classification": classification,
                "reason": request["rationale"],
                "requested_node_ids": list(
                    semantic_impact["requested_node_ids"]
                ),
                "outside_projection_node_ids": list(
                    semantic_impact["outside_projection_node_ids"]
                ),
                "artifact_changes": copy.deepcopy(
                    semantic_impact["artifact_changes"]
                ),
                "proven_artifact_consumer_node_ids": list(
                    semantic_impact["proven_artifact_consumer_node_ids"]
                ),
                "different_digest_consumer_node_ids": list(
                    semantic_impact["different_digest_consumer_node_ids"]
                ),
                "uncertain_artifact_consumer_node_ids": list(
                    semantic_impact["uncertain_artifact_consumer_node_ids"]
                ),
                "semantic_affected_node_ids": list(
                    semantic_impact["semantic_affected_node_ids"]
                ),
                "affected_nodes": invalidated_node_ids,
                "requirement_ids": list(semantic_impact["requirement_ids"]),
                "accepted_requirement_ids": list(
                    semantic_impact["accepted_requirement_ids"]
                ),
                "acceptance_ids": list(semantic_impact["acceptance_ids"]),
                "terminal_node_ids": list(
                    semantic_impact["terminal_node_ids"]
                ),
                "gate_reasons": list(semantic_impact["gate_reasons"]),
                "impact_digest": impact_digest,
                "authority": request["authority"],
                "authority_evidence": list(request["authority_evidence"]),
                "impact_gate": copy.deepcopy(request["impact_gate"]),
                "fanout_policy_result": copy.deepcopy(
                    request["fanout_policy_result"]
                ),
                "semantic_invalidation": copy.deepcopy(semantic),
                "projection_checksum_before": after_checksum,
                "projection_checksum_after": after_checksum,
                "graph_checksum_before": graph_checksum,
                "graph_checksum_after": graph_checksum,
            },
        )
        invalidation_record, events_payload = _next_event_payload_candidate(
            candidate_run,
            invalidation_event,
            events_payload,
            promotion_record,
            timestamp=transaction_timestamp,
        )
        records.append(invalidation_record)

    run_path, events_path, _ = _paths(run_dir)
    artifact_values = (
        (projection_path, _json_document_bytes(candidate_projection), 0o600),
        (run_path, _json_document_bytes(candidate_run), 0o600),
        (events_path, events_payload, 0o600),
    )
    artifacts = [
        {
            "path": _project_relative(project_root, artifact_path),
            "content": payload,
            "mode": mode,
            "expected_sha256": (
                hashlib.sha256(artifact_path.read_bytes()).hexdigest()
                if artifact_path.is_file()
                else None
            ),
        }
        for artifact_path, payload, mode in artifact_values
    ]
    metadata = {
        "entrypoint": "graphctl promote",
        "promotion_id": request["promotion_id"],
        "classification": classification,
        "expected_run_version": run["run_version"],
        "expected_graph_checksum": graph_checksum,
        "expected_contract_digest": outcome_contract_digest(
            run["outcome_contract"]
        ),
        "authority": request["authority"],
    }
    if impact_digest is not None:
        metadata["impact_digest"] = impact_digest
    lifecycle_request = {
        "schema_version": 1,
        "operation": "run-promotion",
        "project_root": project_root,
        "artifacts": artifacts,
        "metadata": metadata,
    }
    return {
        "request": lifecycle_request,
        "plan": preview_project(lifecycle_request),
        "candidate_run": candidate_run,
        "candidate_projection": candidate_projection,
        "event": records[-1],
        "events": records,
        "projection_path": projection_path,
        "invalidated_node_ids": invalidated_node_ids,
    }



def preview_promotion(
    request: dict[str, Any],
    graph_path: Path,
    run_dir: Path,
    *,
    transaction_timestamp: str | None = None,
) -> dict[str, Any]:
    errors = promotion_document_errors(request)
    try:
        graph = _load_graph(graph_path)
        run = load_run(run_dir)
    except (GraphControlError, ValueError) as exc:
        return {
            "valid": False,
            "apply_allowed": False,
            "dry_run": True,
            "errors": errors + [str(exc)],
            "gate_errors": [],
            "semantic_impact": None,
            "impact_digest": None,
        }
    if not isinstance(request, dict):
        return {
            "valid": False,
            "apply_allowed": False,
            "dry_run": True,
            "errors": errors,
            "gate_errors": [],
            "semantic_impact": None,
            "impact_digest": None,
        }
    classification = request.get("classification")
    errors.extend(_binding_errors(request, graph, run))
    if not errors:
        try:
            load_execution_graph(graph_path, run_dir, graph=graph, run=run)
        except (GraphControlError, ValueError) as exc:
            errors.append(str(exc))
    if run.get("status") == "cancelled":
        errors.append("promotion cannot change a cancelled run")
    elif (
        run.get("status") == "completed"
        and classification != "current-regression"
    ):
        errors.append("ordinary promotion requires a nonterminal run")
    running = sorted(
        node_id
        for node_id, state in run.get("nodes", {}).items()
        if state.get("state") == "running"
    )
    if running:
        errors.append(
            "promotion cannot change a run with active leases: "
            + ", ".join(running)
        )
    if classification == "approved-outcome-change":
        errors.append(
            "ordinary promotion cannot change the approved outcome; "
            "use migrate-outcome"
        )
    elif classification == "deferred":
        errors.append("deferred work cannot be promoted; use defer")

    parent: dict[str, Any] | None = None
    candidate: dict[str, Any] | None = None
    if not errors:
        _, parent = _bound_projection(run_dir, run)
        candidate, candidate_errors = _promotion_candidate(
            request,
            graph,
            run,
            parent,
        )
        errors.extend(candidate_errors)

    semantic_impact: dict[str, Any] | None = None
    impact_digest: str | None = None
    gate_errors: list[str] = []
    gate_required = False
    if (
        not errors
        and classification == "current-regression"
        and parent is not None
        and candidate is not None
    ):
        semantic = request["semantic_invalidation"]
        semantic_request = {
            "node_ids": list(semantic["nodes"]),
            "artifact_changes": copy.deepcopy(semantic["artifacts"]),
        }
        try:
            semantic_impact = _semantic_invalidation_impact(
                semantic_request,
                parent,
                run,
                _event_records(run_dir),
            )
        except (GraphControlError, ValueError) as exc:
            errors.append(str(exc))
        if semantic_impact is not None:
            if semantic_impact["running_node_ids"]:
                errors.append(
                    "cannot invalidate running nodes: "
                    + ", ".join(semantic_impact["running_node_ids"])
                )
            if not semantic_impact["verified_node_ids"]:
                errors.append(
                    "current-regression semantic impact selected no verified "
                    "enrolled nodes"
                )
            impact_digest = _promotion_impact_digest(
                request,
                candidate,
                semantic_impact,
            )
            fanout_reasons = {
                "verified-node-count",
                "verified-node-fraction",
            }
            expected_fanout_allowed = not bool(
                fanout_reasons & set(semantic_impact["gate_reasons"])
            )
            if (
                request["fanout_policy_result"]["allowed"]
                is not expected_fanout_allowed
            ):
                errors.append(
                    "promotion fanout policy result does not match computed "
                    "contract fanout"
                )
            gate_required = bool(semantic_impact["gate_reasons"])
            gate_errors = _impact_gate_errors(
                request.get("impact_gate"),
                run,
                impact_digest,
                required=gate_required,
            )

    episode_transition: dict[str, Any] | None = None
    episode_gate_errors: list[str] = []
    if not errors and candidate is not None:
        acceptance_scope = sorted(request["acceptance_mappings"])
        if (
            not acceptance_scope
            and semantic_impact is not None
            and semantic_impact["acceptance_ids"]
        ):
            acceptance_scope = list(semantic_impact["acceptance_ids"])
        if not acceptance_scope:
            acceptance_scope = list(
                run["outcome_contract"]["acceptance_ids"]
            )
        operation_commitment = copy.deepcopy(request)
        operation_commitment.pop("episode_gate", None)
        progress_node_ids = sorted(
            set(request["node_ids"])
            | set(
                semantic_impact["verified_node_ids"]
                if semantic_impact is not None
                else []
            )
        )
        episode_transition = preview_repair_cycle(
            run,
            candidate,
            operation_kind="promotion",
            operation_id=request["promotion_id"],
            causal_episode_id=request["causal_episode_id"],
            classification=classification,
            evidence=(
                list(request["evidence"])
                + list(request["authority_evidence"])
            ),
            operation_commitment=operation_commitment,
            episode_gate=request.get("episode_gate"),
            progress_node_ids=progress_node_ids,
            actor=request["actor"],
            authority=request["authority"],
            acceptance_ids=acceptance_scope,
        )
        errors.extend(episode_transition["errors"])
        episode_gate_errors = list(episode_transition["gate_errors"])

    valid = not errors
    apply_allowed = valid and not episode_gate_errors and (
        classification == "existing-requirement"
        or (
            classification == "current-regression"
            and semantic_impact is not None
            and impact_digest is not None
            and not gate_errors
        )
    )
    lifecycle: dict[str, Any] = {}
    if apply_allowed and candidate is not None:
        timestamp = transaction_timestamp or utc_now()
        try:
            transaction = _promotion_transaction(
                request,
                graph_path.resolve(),
                run_dir.resolve(),
                graph,
                run,
                candidate,
                episode_transition,
                transaction_timestamp=timestamp,
                semantic_impact=semantic_impact,
                impact_digest=impact_digest,
            )
        except (GraphControlError, LifecycleError, OSError, ValueError) as exc:
            errors.append(str(exc))
            apply_allowed = False
        else:
            plan = transaction["plan"]
            if not plan.valid:
                errors.extend(plan.errors)
                errors.extend(f"lifecycle blocked: {item}" for item in plan.blocked)
                errors.extend(
                    f"lifecycle conflict: {item}" for item in plan.conflicts
                )
                apply_allowed = False
            lifecycle = {
                "transaction_timestamp": timestamp,
                "lifecycle_base_digest": plan.base_digest,
                "lifecycle_plan_digest": plan.plan_digest,
                "lifecycle_candidate_digest": plan.candidate_digest,
            }
    valid = not errors
    route = (
        "apply-current-regression"
        if valid and classification == "current-regression"
        else "apply-promotion"
        if valid and classification == "existing-requirement"
        else None
    )
    return {
        "valid": valid,
        "apply_allowed": apply_allowed,
        "dry_run": True,
        "errors": errors,
        "gate_errors": gate_errors,
        "gate_required": gate_required,
        "episode_gate_errors": episode_gate_errors,
        "episode_gate_required": (
            episode_transition.get("gate_required")
            if episode_transition is not None
            else None
        ),
        "episode_operation_digest": (
            episode_transition.get("episode_operation_digest")
            if episode_transition is not None
            else None
        ),
        "expected_gate_evidence_digest": (
            episode_transition.get("expected_gate_evidence_digest")
            if episode_transition is not None
            else None
        ),
        "causal_episode_id": request.get("causal_episode_id"),
        "episode_transition": episode_transition,
        "promotion_id": request.get("promotion_id"),
        "classification": classification,
        "route": route,
        "added_node_ids": list(request.get("node_ids", [])),
        "added_edge_ids": list(request.get("edge_ids", [])),
        "candidate_projection_version": (
            candidate.get("projection_version") if candidate is not None else None
        ),
        "candidate_projection_checksum": (
            sha256_json(candidate) if candidate is not None else None
        ),
        "semantic_impact": semantic_impact,
        "impact_digest": impact_digest,
        **lifecycle,
    }



def apply_promotion(
    request: dict[str, Any],
    graph_path: Path,
    run_dir: Path,
    *,
    transaction_timestamp: str,
    expected_lifecycle_base_digest: str,
    expected_lifecycle_plan_digest: str,
) -> dict[str, Any]:
    graph_path = graph_path.resolve()
    run_dir = run_dir.resolve()
    _, _, lock_path = _paths(run_dir)
    with file_lock(lock_path):
        report = preview_promotion(
            request,
            graph_path,
            run_dir,
            transaction_timestamp=transaction_timestamp,
        )
        if not report["valid"] or not report["apply_allowed"]:
            failures = (
                list(report["errors"])
                + list(report["gate_errors"])
                + list(report["episode_gate_errors"])
            )
            raise RuntimeStateError(
                "promotion preview failed: " + "; ".join(failures)
            )
        if report["lifecycle_base_digest"] != expected_lifecycle_base_digest:
            raise StaleStateError("stale lifecycle base digest")
        if report["lifecycle_plan_digest"] != expected_lifecycle_plan_digest:
            raise StaleStateError("stale lifecycle plan digest")
        graph = _load_graph(graph_path)
        run = load_run(run_dir)
        _, parent = _bound_projection(run_dir, run)
        candidate, errors = _promotion_candidate(request, graph, run, parent)
        if errors or candidate is None:
            raise RuntimeStateError(
                "promotion candidate is invalid: " + "; ".join(errors)
            )
        transaction = _promotion_transaction(
            request,
            graph_path,
            run_dir,
            graph,
            run,
            candidate,
            report["episode_transition"],
            transaction_timestamp=transaction_timestamp,
            semantic_impact=report["semantic_impact"],
            impact_digest=report["impact_digest"],
        )
        receipt = apply_project(
            transaction["request"],
            expected_base_digest=expected_lifecycle_base_digest,
            expected_plan_digest=expected_lifecycle_plan_digest,
            actor=request["actor"],
            authority=request["authority"],
        )
        if not receipt.applied:
            raise RuntimeStateError(
                f"promotion transaction {receipt.state}: "
                f"{receipt.error or 'apply failed'}"
            )
        return {
            "valid": True,
            "applied": True,
            "promotion_id": request["promotion_id"],
            "causal_episode_id": request["causal_episode_id"],
            "run_version": transaction["candidate_run"]["run_version"],
            "projection_version": candidate["projection_version"],
            "projection_checksum": sha256_json(candidate),
            "added_node_ids": list(request["node_ids"]),
            "added_edge_ids": list(request["edge_ids"]),
            "invalidated_node_ids": list(
                transaction["invalidated_node_ids"]
            ),
            "impact_digest": report["impact_digest"],
            "transaction_id": receipt.transaction_id,
            "lifecycle_plan_digest": receipt.plan_digest,
        }


_INVALIDATION_FIELDS = {
    "schema_version", "invalidation_id", "causal_episode_id",
    "target_run_id", "expected_run_version",
    "expected_outcome_contract_digest", "expected_graph_version",
    "expected_graph_checksum", "expected_projection_checksum", "node_ids",
    "artifact_changes", "reason", "actor", "authority",
    "authority_evidence", "impact_gate", "episode_gate",
}
_IMPACT_GATE_FIELDS = {
    "decision", "run_id", "impact_digest", "approver", "gate_node_id",
    "evidence_digest", "approved_at",
}
_ARTIFACT_CHANGE_FIELDS = {
    "artifact_id", "previous_digest", "current_digest",
}


def _impact_gate_document_errors(gate: Any) -> list[str]:
    if gate is None:
        return []
    errors = _document_shape_errors(gate, _IMPACT_GATE_FIELDS, "impact gate")
    if not isinstance(gate, dict):
        return errors
    if gate.get("decision") != "approved":
        errors.append("impact gate decision must be approved")
    try:
        uuid.UUID(gate.get("run_id"))
    except (AttributeError, TypeError, ValueError):
        errors.append("impact gate run_id must be a UUID")
    if not _is_checksum(gate.get("impact_digest")):
        errors.append("impact gate impact_digest must be a SHA-256 digest")
    if not _is_checksum(gate.get("evidence_digest")):
        errors.append("impact gate evidence_digest must be a SHA-256 digest")
    if (
        not isinstance(gate.get("gate_node_id"), str)
        or not _SLUG.fullmatch(gate["gate_node_id"])
    ):
        errors.append("impact gate gate_node_id must be a lowercase slug")
    for field in ("approver", "approved_at"):
        if not _nonempty_text(gate.get(field)):
            errors.append(f"impact gate {field} must be non-empty")
    return errors



def _artifact_changes_errors(value: Any, label: str) -> list[str]:
    if not isinstance(value, list):
        return [f"{label} must be an array"]
    errors: list[str] = []
    artifact_ids: list[str] = []
    for index, change in enumerate(value):
        item_label = f"{label}[{index}]"
        if not isinstance(change, dict) or set(change) != _ARTIFACT_CHANGE_FIELDS:
            errors.append(f"{item_label} fields are invalid")
            continue
        artifact_id = change.get("artifact_id")
        if not _nonempty_text(artifact_id):
            errors.append(f"{item_label}.artifact_id must be non-empty")
        else:
            artifact_ids.append(artifact_id)
        for field in ("previous_digest", "current_digest"):
            if not _is_checksum(change.get(field)):
                errors.append(f"{item_label}.{field} must be a SHA-256 digest")
        if (
            _is_checksum(change.get("previous_digest"))
            and change.get("previous_digest") == change.get("current_digest")
        ):
            errors.append(f"{item_label} must change the artifact digest")
    if len(artifact_ids) != len(set(artifact_ids)):
        errors.append(f"{label} must have unique artifact IDs")
    return errors


def semantic_invalidation_document_errors(request: Any) -> list[str]:
    errors = _request_common_errors(
        request,
        _INVALIDATION_FIELDS,
        ("invalidation_id", "causal_episode_id"),
    )
    if not isinstance(request, dict):
        return errors
    errors.extend(_slug_list_errors(request.get("node_ids"), "invalidation node_ids"))
    changes = request.get("artifact_changes")
    errors.extend(
        _artifact_changes_errors(changes, "invalidation artifact_changes")
    )
    node_ids = request.get("node_ids")
    if isinstance(node_ids, list) and isinstance(changes, list) and not node_ids and not changes:
        errors.append("semantic invalidation requires at least one node or artifact change")
    if not _nonempty_text(request.get("reason")):
        errors.append("invalidation reason must be non-empty")
    errors.extend(
        _evidence_errors(
            request.get("authority_evidence"),
            "invalidation authority_evidence",
        )
    )
    errors.extend(_impact_gate_document_errors(request.get("impact_gate")))
    errors.extend(episode_gate_document_errors(request.get("episode_gate")))
    return errors


def _semantic_invalidation_impact(
    request: dict[str, Any],
    projection: dict[str, Any],
    run: dict[str, Any],
    records: list[dict[str, Any]],
) -> dict[str, Any]:
    order = [item["id"] for item in projection["nodes"]]
    enrolled = set(order)
    definitions = {item["id"]: item for item in projection["nodes"]}
    requested = set(request["node_ids"])
    outside = sorted(requested - enrolled)
    seeds = requested & enrolled
    proven: set[str] = set()
    different: set[str] = set()
    uncertain: set[str] = set()
    for change in request["artifact_changes"]:
        artifact_id = change["artifact_id"]
        previous_digest = change["previous_digest"]
        for node_id in order:
            definition = definitions[node_id]
            consumes = any(
                isinstance(item, dict)
                and item.get("artifact") == artifact_id
                for item in definition.get("inputs", [])
            )
            state = run["nodes"][node_id]
            if not consumes or state.get("state") != "verified":
                continue
            observations = _sealed_consumed_artifacts(records, node_id, state)
            if observations is None or artifact_id not in observations:
                uncertain.add(node_id)
            elif observations[artifact_id] == previous_digest:
                proven.add(node_id)
            else:
                different.add(node_id)
    seeds.update(proven)
    seeds.update(uncertain)
    semantic_nodes = semantic_affected_node_ids(projection, seeds)
    verified_nodes = [
        node_id
        for node_id in semantic_nodes
        if run["nodes"][node_id].get("state") == "verified"
    ]
    running_nodes = [
        node_id
        for node_id in semantic_nodes
        if run["nodes"][node_id].get("state") == "running"
    ]
    verified_set = set(verified_nodes)
    requirement_mappings = projection.get("requirement_mappings", {})
    acceptance_mappings = projection.get("acceptance_mappings", {})
    requirement_ids = sorted(
        requirement_id
        for requirement_id, node_ids in requirement_mappings.items()
        if set(node_ids) & verified_set
    )
    accepted_requirements = {
        requirement_id
        for requirement_id, node_ids in requirement_mappings.items()
        if node_ids
        and all(
            run["nodes"].get(node_id, {}).get("state") == "verified"
            for node_id in node_ids
        )
    }
    accepted_requirement_ids = sorted(
        set(requirement_ids) & accepted_requirements
    )
    acceptance_ids = sorted(
        acceptance_id
        for acceptance_id, node_ids in acceptance_mappings.items()
        if set(node_ids) & verified_set
    )
    terminal_node_ids = [
        node_id
        for node_id in projection["terminal_nodes"]
        if node_id in verified_set
    ]
    verified_enrolled = sum(
        1
        for node_id in order
        if run["nodes"][node_id].get("state") == "verified"
    )
    fraction = len(verified_nodes) / verified_enrolled if verified_enrolled else 0
    policy = run["outcome_contract"]["impact_policy"]
    gate_reasons: list[str] = []
    if accepted_requirement_ids:
        gate_reasons.append("accepted-requirement")
    if acceptance_ids:
        gate_reasons.append("acceptance")
    if terminal_node_ids:
        gate_reasons.append("terminal")
    if len(requirement_ids) > 1:
        gate_reasons.append("cross-requirement")
    if len(verified_nodes) > policy["max_verified_node_count"]:
        gate_reasons.append("verified-node-count")
    if fraction > policy["max_verified_node_fraction"]:
        gate_reasons.append("verified-node-fraction")
    if uncertain:
        gate_reasons.append("unproven-artifact-consumers")
    normalized_changes = sorted(
        copy.deepcopy(request["artifact_changes"]),
        key=lambda item: item["artifact_id"],
    )
    return {
        "requested_node_ids": sorted(requested),
        "outside_projection_node_ids": outside,
        "artifact_changes": normalized_changes,
        "proven_artifact_consumer_node_ids": [
            node_id for node_id in order if node_id in proven
        ],
        "different_digest_consumer_node_ids": [
            node_id for node_id in order if node_id in different
        ],
        "uncertain_artifact_consumer_node_ids": [
            node_id for node_id in order if node_id in uncertain
        ],
        "semantic_affected_node_ids": semantic_nodes,
        "verified_node_ids": verified_nodes,
        "running_node_ids": running_nodes,
        "requirement_ids": requirement_ids,
        "accepted_requirement_ids": accepted_requirement_ids,
        "acceptance_ids": acceptance_ids,
        "terminal_node_ids": terminal_node_ids,
        "verified_node_count": len(verified_nodes),
        "verified_enrolled_node_count": verified_enrolled,
        "verified_node_fraction": fraction,
        "gate_reasons": gate_reasons,
    }


def _semantic_impact_digest(
    request: dict[str, Any],
    impact: dict[str, Any],
) -> str:
    commitment = {
        "schema_version": request["schema_version"],
        "invalidation_id": request["invalidation_id"],
        "causal_episode_id": request["causal_episode_id"],
        "target_run_id": request["target_run_id"],
        "expected_run_version": request["expected_run_version"],
        "expected_outcome_contract_digest": request[
            "expected_outcome_contract_digest"
        ],
        "expected_graph_version": request["expected_graph_version"],
        "expected_graph_checksum": request["expected_graph_checksum"],
        "expected_projection_checksum": request[
            "expected_projection_checksum"
        ],
        "node_ids": sorted(request["node_ids"]),
        "artifact_changes": impact["artifact_changes"],
        "reason": request["reason"],
        "actor": request["actor"],
        "authority": request["authority"],
        "authority_evidence": sorted(request["authority_evidence"]),
    }
    return sha256_json(
        {
            "operation": "semantic-invalidation",
            "request": commitment,
            "impact": impact,
        }
    )



def _promotion_impact_digest(
    request: dict[str, Any],
    candidate_projection: dict[str, Any],
    impact: dict[str, Any],
) -> str:
    normalized_mappings = lambda mappings: {
        key: sorted(values)
        for key, values in sorted(mappings.items())
    }
    semantic = request["semantic_invalidation"]
    commitment = {
        "schema_version": request["schema_version"],
        "promotion_id": request["promotion_id"],
        "causal_episode_id": request["causal_episode_id"],
        "target_run_id": request["target_run_id"],
        "expected_run_version": request["expected_run_version"],
        "expected_outcome_contract_digest": request[
            "expected_outcome_contract_digest"
        ],
        "expected_graph_version": request["expected_graph_version"],
        "expected_graph_checksum": request["expected_graph_checksum"],
        "expected_projection_checksum": request[
            "expected_projection_checksum"
        ],
        "classification": request["classification"],
        "node_ids": sorted(request["node_ids"]),
        "edge_ids": sorted(request["edge_ids"]),
        "requirement_mappings": normalized_mappings(
            request["requirement_mappings"]
        ),
        "acceptance_mappings": normalized_mappings(
            request["acceptance_mappings"]
        ),
        "semantic_invalidation": {
            "nodes": sorted(semantic["nodes"]),
            "artifacts": copy.deepcopy(impact["artifact_changes"]),
        },
        "fanout_policy_result": copy.deepcopy(
            request["fanout_policy_result"]
        ),
        "actor": request["actor"],
        "authority": request["authority"],
        "authority_evidence": sorted(request["authority_evidence"]),
        "rationale": request["rationale"],
        "evidence": sorted(request["evidence"]),
    }
    return sha256_json(
        {
            "operation": "current-regression-promotion",
            "request": commitment,
            "candidate_projection": {
                "version": candidate_projection["projection_version"],
                "checksum": sha256_json(candidate_projection),
                "parent_checksum": candidate_projection["lineage"][
                    "parent_checksum"
                ],
            },
            "impact": impact,
        }
    )


def _impact_gate_errors(
    gate: Any,
    run: dict[str, Any],
    impact_digest: str,
    *,
    required: bool,
) -> list[str]:
    if gate is None:
        return ["impact gate is required"] if required else []
    errors = _impact_gate_document_errors(gate)
    if errors or not isinstance(gate, dict):
        return errors
    if gate.get("run_id") != run["run_id"]:
        errors.append("impact gate run ID does not match runtime")
    if gate.get("impact_digest") != impact_digest:
        errors.append("impact gate impact digest does not match preview")
    return errors


def _semantic_invalidation_transaction(
    request: dict[str, Any],
    run_dir: Path,
    execution_graph: dict[str, Any],
    run: dict[str, Any],
    impact: dict[str, Any],
    impact_digest: str,
    episode_transition: dict[str, Any],
    *,
    transaction_timestamp: str,
) -> dict[str, Any]:
    candidate_run = copy.deepcopy(run)
    for node_id in impact["verified_node_ids"]:
        state = candidate_run["nodes"][node_id]
        state["state"] = "invalidated"
        state["lease"] = None
        state["approval"] = None
        state["last_result"] = {
            "invalidated_reason": request["reason"],
            "impact_digest": impact_digest,
        }
    _refresh_status(execution_graph, candidate_run)
    episode_details = apply_repair_cycle(
        candidate_run,
        episode_transition,
        timestamp=transaction_timestamp,
    )
    event = _event_base(
        candidate_run,
        "semantic-nodes-invalidated",
        request["actor"],
        details={
            "invalidation_id": request["invalidation_id"],
            **episode_details,
            "causal_episode_id": request["causal_episode_id"],
            "reason": request["reason"],
            "requested_node_ids": list(impact["requested_node_ids"]),
            "outside_projection_node_ids": list(
                impact["outside_projection_node_ids"]
            ),
            "artifact_changes": copy.deepcopy(impact["artifact_changes"]),
            "proven_artifact_consumer_node_ids": list(
                impact["proven_artifact_consumer_node_ids"]
            ),
            "uncertain_artifact_consumer_node_ids": list(
                impact["uncertain_artifact_consumer_node_ids"]
            ),
            "affected_nodes": list(impact["verified_node_ids"]),
            "requirement_ids": list(impact["requirement_ids"]),
            "acceptance_ids": list(impact["acceptance_ids"]),
            "terminal_node_ids": list(impact["terminal_node_ids"]),
            "gate_reasons": list(impact["gate_reasons"]),
            "impact_digest": impact_digest,
            "authority": request["authority"],
            "authority_evidence": list(request["authority_evidence"]),
            "impact_gate": copy.deepcopy(request["impact_gate"]),
            "projection_checksum_before": run_execution_checksum(run),
            "projection_checksum_after": run_execution_checksum(run),
            "graph_checksum_before": run_observed_graph_checksum(run),
            "graph_checksum_after": run_observed_graph_checksum(run),
        },
    )
    record, events_payload = _event_payload_candidate(
        run_dir,
        candidate_run,
        event,
        timestamp=transaction_timestamp,
    )
    return {
        "candidate_run": candidate_run,
        "event": record,
        "events_payload": events_payload,
    }


def preview_semantic_invalidation(
    request: dict[str, Any],
    graph_path: Path,
    run_dir: Path,
    *,
    transaction_timestamp: str | None = None,
) -> dict[str, Any]:
    errors = semantic_invalidation_document_errors(request)
    try:
        graph = _load_graph(graph_path)
        run = load_run(run_dir)
    except (GraphControlError, ValueError) as exc:
        return {
            "valid": False,
            "apply_allowed": False,
            "dry_run": True,
            "errors": errors + [str(exc)],
            "gate_errors": [],
        }
    if not isinstance(request, dict):
        return {
            "valid": False,
            "apply_allowed": False,
            "dry_run": True,
            "errors": errors,
            "gate_errors": [],
        }
    errors.extend(_binding_errors(request, graph, run))
    projection: dict[str, Any] | None = None
    if run.get("status") == "cancelled":
        errors.append("semantic invalidation cannot change a cancelled run")
    if not errors:
        try:
            load_execution_graph(graph_path, run_dir, graph=graph, run=run)
            _, projection = _bound_projection(run_dir, run)
        except (GraphControlError, ValueError) as exc:
            errors.append(str(exc))
    impact: dict[str, Any] | None = None
    impact_digest: str | None = None
    gate_errors: list[str] = []
    gate_required = False
    if not errors and projection is not None:
        records = _event_records(run_dir)
        impact = _semantic_invalidation_impact(
            request,
            projection,
            run,
            records,
        )
        if impact["running_node_ids"]:
            errors.append(
                "cannot invalidate running nodes: "
                + ", ".join(impact["running_node_ids"])
            )
        if not impact["verified_node_ids"]:
            errors.append("semantic invalidation selected no verified enrolled nodes")
        impact_digest = _semantic_impact_digest(request, impact)
        gate_required = bool(impact["gate_reasons"])
        gate_errors = _impact_gate_errors(
            request.get("impact_gate"),
            run,
            impact_digest,
            required=gate_required,
        )
    episode_transition: dict[str, Any] | None = None
    episode_gate_errors: list[str] = []
    if not errors and impact is not None and projection is not None:
        acceptance_scope = list(impact["acceptance_ids"])
        if not acceptance_scope:
            acceptance_scope = list(
                run["outcome_contract"]["acceptance_ids"]
            )
        operation_commitment = copy.deepcopy(request)
        operation_commitment.pop("episode_gate", None)
        episode_transition = preview_repair_cycle(
            run,
            projection,
            operation_kind="invalidation",
            operation_id=request["invalidation_id"],
            causal_episode_id=request["causal_episode_id"],
            classification="current-regression",
            evidence=(
                list(request["authority_evidence"])
                + [
                    request["reason"],
                    sha256_json(
                        {
                            "node_ids": sorted(request["node_ids"]),
                            "artifact_changes": impact[
                                "artifact_changes"
                            ],
                        }
                    ),
                ]
            ),
            operation_commitment=operation_commitment,
            episode_gate=request.get("episode_gate"),
            progress_node_ids=list(impact["verified_node_ids"]),
            actor=request["actor"],
            authority=request["authority"],
            acceptance_ids=acceptance_scope,
        )
        errors.extend(episode_transition["errors"])
        episode_gate_errors = list(episode_transition["gate_errors"])

    timestamp = transaction_timestamp or utc_now()
    run_path, events_path, _ = _paths(run_dir)
    valid = not errors
    apply_allowed = (
        valid
        and impact is not None
        and not gate_errors
        and not episode_gate_errors
    )
    return {
        "valid": valid,
        "apply_allowed": apply_allowed,
        "dry_run": True,
        "errors": errors,
        "gate_errors": gate_errors,
        "invalidation_id": request.get("invalidation_id"),
        "causal_episode_id": request.get("causal_episode_id"),
        "impact": impact,
        "impact_digest": impact_digest,
        "gate_required": gate_required,
        "episode_gate_errors": episode_gate_errors,
        "episode_gate_required": (
            episode_transition.get("gate_required")
            if episode_transition is not None
            else None
        ),
        "episode_operation_digest": (
            episode_transition.get("episode_operation_digest")
            if episode_transition is not None
            else None
        ),
        "expected_gate_evidence_digest": (
            episode_transition.get("expected_gate_evidence_digest")
            if episode_transition is not None
            else None
        ),
        "episode_transition": episode_transition,
        "transaction_timestamp": timestamp if impact is not None else None,
        "expected_run_sha256": (
            hashlib.sha256(run_path.read_bytes()).hexdigest()
            if run_path.is_file()
            else None
        ),
        "expected_events_sha256": (
            hashlib.sha256(events_path.read_bytes()).hexdigest()
            if events_path.is_file()
            else None
        ),
    }


def apply_semantic_invalidation(
    request: dict[str, Any],
    graph_path: Path,
    run_dir: Path,
    *,
    transaction_timestamp: str,
    expected_impact_digest: str,
    expected_run_sha256: str,
    expected_events_sha256: str,
) -> dict[str, Any]:
    graph_path = graph_path.resolve()
    run_dir = run_dir.resolve()
    run_path, events_path, lock_path = _paths(run_dir)
    with file_lock(lock_path):
        if hashlib.sha256(run_path.read_bytes()).hexdigest() != expected_run_sha256:
            raise StaleStateError("stale run file")
        if hashlib.sha256(events_path.read_bytes()).hexdigest() != expected_events_sha256:
            raise StaleStateError("stale event ledger")
        report = preview_semantic_invalidation(
            request,
            graph_path,
            run_dir,
            transaction_timestamp=transaction_timestamp,
        )
        if report.get("impact_digest") != expected_impact_digest:
            raise StaleStateError("semantic impact digest is stale")
        if not report["valid"] or not report["apply_allowed"]:
            failures = (
                list(report["errors"])
                + list(report["gate_errors"])
                + list(report["episode_gate_errors"])
            )
            raise RuntimeStateError(
                "semantic invalidation preview failed: " + "; ".join(failures)
            )
        graph = _load_graph(graph_path)
        run = load_run(run_dir)
        execution_graph = load_execution_graph(
            graph_path,
            run_dir,
            graph=graph,
            run=run,
        )
        transaction = _semantic_invalidation_transaction(
            request,
            run_dir,
            execution_graph,
            run,
            report["impact"],
            report["impact_digest"],
            report["episode_transition"],
            transaction_timestamp=transaction_timestamp,
        )
        if hashlib.sha256(run_path.read_bytes()).hexdigest() != expected_run_sha256:
            raise StaleStateError("stale run file")
        if hashlib.sha256(events_path.read_bytes()).hexdigest() != expected_events_sha256:
            raise StaleStateError("stale event ledger")
        atomic_write_batch(
            [
                (events_path, transaction["events_payload"], 0o600),
                (
                    run_path,
                    _json_document_bytes(transaction["candidate_run"]),
                    0o600,
                ),
            ]
        )
        return {
            "valid": True,
            "applied": True,
            "invalidation_id": request["invalidation_id"],
            "causal_episode_id": request["causal_episode_id"],
            "impact_digest": report["impact_digest"],
            "invalidated_node_ids": list(
                report["impact"]["verified_node_ids"]
            ),
            "run_version": transaction["candidate_run"]["run_version"],
            "event_sequence": transaction["event"]["sequence"],
        }


_DEFERRAL_FIELDS = {
    "schema_version", "deferral_id", "causal_episode_id", "target_run_id",
    "expected_run_version", "expected_outcome_contract_digest",
    "expected_graph_version", "expected_graph_checksum",
    "expected_projection_checksum", "finding_id", "reason", "evidence",
    "actor", "authority",
}


def deferral_document_errors(request: Any) -> list[str]:
    errors = _request_common_errors(
        request,
        _DEFERRAL_FIELDS,
        ("deferral_id", "causal_episode_id", "finding_id"),
    )
    if not isinstance(request, dict):
        return errors
    if not _nonempty_text(request.get("reason")):
        errors.append("deferral reason must be non-empty")
    errors.extend(_evidence_errors(request.get("evidence"), "deferral evidence"))
    return errors


def _deferral_transaction(
    request: dict[str, Any],
    run_dir: Path,
    run: dict[str, Any],
    *,
    transaction_timestamp: str,
) -> dict[str, Any]:
    candidate_run = copy.deepcopy(run)
    event = _event_base(
        candidate_run,
        "finding-deferred",
        request["actor"],
        details={
            "deferral_id": request["deferral_id"],
            "causal_episode_id": request["causal_episode_id"],
            "finding_id": request["finding_id"],
            "reason": request["reason"],
            "evidence": list(request["evidence"]),
            "authority": request["authority"],
            "projection_checksum_before": run_execution_checksum(run),
            "projection_checksum_after": run_execution_checksum(run),
            "graph_checksum_before": run_observed_graph_checksum(run),
            "graph_checksum_after": run_observed_graph_checksum(run),
        },
    )
    record, events_payload = _event_payload_candidate(
        run_dir,
        candidate_run,
        event,
        timestamp=transaction_timestamp,
    )
    run_path, events_path, _ = _paths(run_dir)
    return {
        "candidate_run": candidate_run,
        "event": record,
        "events_payload": events_payload,
        "run_sha256": hashlib.sha256(run_path.read_bytes()).hexdigest(),
        "events_sha256": hashlib.sha256(events_path.read_bytes()).hexdigest(),
    }


def preview_defer(
    request: dict[str, Any],
    graph_path: Path,
    run_dir: Path,
    *,
    transaction_timestamp: str | None = None,
) -> dict[str, Any]:
    errors = deferral_document_errors(request)
    try:
        graph = _load_graph(graph_path)
        run = load_run(run_dir)
    except (GraphControlError, ValueError) as exc:
        return {"valid": False, "dry_run": True, "errors": errors + [str(exc)]}
    if not isinstance(request, dict):
        return {"valid": False, "dry_run": True, "errors": errors}
    errors.extend(_binding_errors(request, graph, run))
    if not errors:
        try:
            load_execution_graph(graph_path, run_dir, graph=graph, run=run)
        except (GraphControlError, ValueError) as exc:
            errors.append(str(exc))
    graph_nodes = {item["id"] for item in graph.get("nodes", [])}
    finding_id = request.get("finding_id")
    if finding_id not in graph_nodes:
        errors.append("deferral finding is not present in the durable graph")
    if finding_id in run.get("nodes", {}):
        errors.append("deferral finding is already enrolled in the run")
    timestamp = transaction_timestamp or utc_now()
    transaction: dict[str, Any] | None = None
    if not errors:
        transaction = _deferral_transaction(
            request,
            run_dir.resolve(),
            run,
            transaction_timestamp=timestamp,
        )
    return {
        "valid": not errors,
        "dry_run": True,
        "errors": errors,
        "deferral_id": request.get("deferral_id"),
        "finding_id": finding_id,
        "transaction_timestamp": timestamp if transaction is not None else None,
        "expected_run_sha256": (
            transaction["run_sha256"] if transaction is not None else None
        ),
        "expected_events_sha256": (
            transaction["events_sha256"] if transaction is not None else None
        ),
    }


def apply_defer(
    request: dict[str, Any],
    graph_path: Path,
    run_dir: Path,
    *,
    transaction_timestamp: str,
    expected_run_sha256: str,
    expected_events_sha256: str,
) -> dict[str, Any]:
    run_dir = run_dir.resolve()
    run_path, events_path, lock_path = _paths(run_dir)
    with file_lock(lock_path):
        if hashlib.sha256(run_path.read_bytes()).hexdigest() != expected_run_sha256:
            raise StaleStateError("stale run file")
        if hashlib.sha256(events_path.read_bytes()).hexdigest() != expected_events_sha256:
            raise StaleStateError("stale event ledger")
        report = preview_defer(
            request,
            graph_path,
            run_dir,
            transaction_timestamp=transaction_timestamp,
        )
        if not report["valid"]:
            raise RuntimeStateError(
                "deferral preview failed: " + "; ".join(report["errors"])
            )
        graph = _load_graph(graph_path)
        run = load_run(run_dir)
        errors = _binding_errors(request, graph, run)
        if errors:
            raise StaleStateError("; ".join(errors))
        transaction = _deferral_transaction(
            request,
            run_dir,
            run,
            transaction_timestamp=transaction_timestamp,
        )
        atomic_write_batch(
            [
                (events_path, transaction["events_payload"], 0o600),
                (
                    run_path,
                    _json_document_bytes(transaction["candidate_run"]),
                    0o600,
                ),
            ]
        )
        return {
            "valid": True,
            "applied": True,
            "deferral_id": request["deferral_id"],
            "finding_id": request["finding_id"],
            "run_version": transaction["candidate_run"]["run_version"],
        }


_MIGRATION_FIELDS = {
    "schema_version", "migration_id", "target_run_id", "expected_run_version",
    "expected_outcome_contract_digest", "expected_graph_version",
    "expected_graph_checksum", "expected_projection_checksum", "mode",
    "successor_run_id", "proposed_outcome_contract", "proposed_projection",
    "actor", "authority",
    "authority_evidence", "rationale", "evidence",
}


def migration_document_errors(request: Any) -> list[str]:
    required_fields = set(_MIGRATION_FIELDS)
    if not isinstance(request, dict) or request.get("mode") != "successor-run":
        required_fields.discard("successor_run_id")
    errors = _request_common_errors(
        request,
        required_fields,
        ("migration_id",),
    )
    if not isinstance(request, dict):
        return errors
    if request.get("mode") not in {"successor-run", "same-run"}:
        errors.append("migration mode must be successor-run or same-run")
    if not _nonempty_text(request.get("rationale")):
        errors.append("migration rationale must be non-empty")
    errors.extend(_evidence_errors(request.get("evidence"), "migration evidence"))
    errors.extend(
        _evidence_errors(
            request.get("authority_evidence"),
            "migration authority_evidence",
        )
    )
    if not isinstance(request.get("proposed_outcome_contract"), dict):
        errors.append("migration proposed outcome contract must be an object")
    if not isinstance(request.get("proposed_projection"), dict):
        errors.append("migration proposed projection must be an object")
    successor_run_id = request.get("successor_run_id")
    if request.get("mode") == "successor-run":
        if not isinstance(successor_run_id, str):
            errors.append("successor-run migration requires successor_run_id")
        else:
            try:
                parsed = uuid.UUID(successor_run_id)
            except (TypeError, ValueError, AttributeError):
                errors.append("successor_run_id must be a canonical UUID")
            else:
                if str(parsed) != successor_run_id:
                    errors.append("successor_run_id must be a canonical UUID")
    elif successor_run_id is not None:
        errors.append("successor_run_id is valid only for successor-run mode")
    return errors


def preview_outcome_migration(
    request: dict[str, Any],
    graph_path: Path,
    run_dir: Path,
    *,
    transaction_timestamp: str | None = None,
) -> dict[str, Any]:
    errors = migration_document_errors(request)
    try:
        graph = _load_graph(graph_path)
        run = load_run(run_dir)
    except (GraphControlError, ValueError) as exc:
        return {
            "valid": False,
            "dry_run": True,
            "apply_supported": False,
            "errors": errors + [str(exc)],
        }
    if not isinstance(request, dict):
        return {
            "valid": False,
            "dry_run": True,
            "apply_supported": False,
            "errors": errors,
        }
    transaction_timestamp = transaction_timestamp or utc_now()
    try:
        _parse_time(transaction_timestamp)
    except (TypeError, ValueError):
        errors.append("migration transaction_timestamp is invalid")
    if run.get("schema_version") != 2:
        errors.append("outcome migration requires a schema-v2 source run")
    else:
        errors.extend(_binding_errors(request, graph, run))
    active_nodes = sorted(
        node_id
        for node_id, state in run.get("nodes", {}).items()
        if state.get("state") in {"running", "verifying"}
        or state.get("lease") is not None
    )
    if active_nodes:
        errors.append(
            "outcome migration cannot create a successor with active leases: "
            + ", ".join(active_nodes)
        )
    if run.get("status") == "cancelled":
        errors.append("outcome migration cannot expand a cancelled run")
    if not errors:
        try:
            load_execution_graph(graph_path, run_dir, graph=graph, run=run)
        except (GraphControlError, ValueError) as exc:
            errors.append(str(exc))
    proposed_contract = request.get("proposed_outcome_contract")
    proposed_projection = request.get("proposed_projection")
    if isinstance(proposed_contract, dict):
        errors.extend(outcome_contract_errors(proposed_contract))
        if (
            proposed_contract.get("contract_version")
            == run.get("outcome_contract", {}).get("contract_version")
        ):
            errors.append("migration must create a new outcome contract version")
    expected_migration_run_id = (
        request.get("successor_run_id")
        if request.get("mode") == "successor-run"
        else run.get("run_id")
    )
    if isinstance(proposed_contract, dict) and isinstance(proposed_projection, dict):
        errors.extend(
            projection_contract_errors(
                graph,
                proposed_contract,
                proposed_projection,
                expected_graph_checksum=canonical_checksum(graph),
                expected_run_id=expected_migration_run_id,
                require_current_source=True,
            )
        )
    old_contract = run.get("outcome_contract", {})
    contract_diff = {}
    if isinstance(proposed_contract, dict):
        for field in sorted(set(old_contract) | set(proposed_contract)):
            if old_contract.get(field) != proposed_contract.get(field):
                contract_diff[field] = {
                    "before": copy.deepcopy(old_contract.get(field)),
                    "after": copy.deepcopy(proposed_contract.get(field)),
                }
    outcome_changed = (
        isinstance(proposed_contract, dict)
        and proposed_contract.get("outcome_digest") != old_contract.get("outcome_digest")
    )
    mode = request.get("mode")
    successor_run_id = request.get("successor_run_id")
    successor_run_path = (
        f"successors/{successor_run_id}"
        if mode == "successor-run" and isinstance(successor_run_id, str)
        else None
    )
    lifecycle_base_digest = None
    lifecycle_plan_digest = None
    source_run_sha256 = None
    source_events_sha256 = None
    source_projection_sha256 = None

    if mode == "successor-run":
        if not outcome_changed:
            errors.append("successor-run migration must change the approved outcome")
        if isinstance(proposed_contract, dict):
            if proposed_contract.get("contract_id") != old_contract.get("contract_id"):
                errors.append("successor contract_id must preserve contract lineage")
            if proposed_contract.get("contract_version") != (
                old_contract.get("contract_version", 0) + 1
            ):
                errors.append(
                    "successor contract_version must be exactly one greater"
                )
            for field in ("requirement_ids", "acceptance_ids"):
                old_values = old_contract.get(field)
                new_values = proposed_contract.get(field)
                if (
                    not isinstance(old_values, list)
                    or not isinstance(new_values, list)
                    or not set(old_values).issubset(set(new_values))
                ):
                    errors.append(
                        f"successor {field} must preserve the source contract"
                    )
            if proposed_contract.get("authority_evidence") != request.get(
                "authority_evidence"
            ):
                errors.append(
                    "successor contract authority_evidence must match the request"
                )
            if proposed_contract.get("authority_digest") == old_contract.get(
                "authority_digest"
            ):
                errors.append(
                    "successor expansion requires authority distinct from the source contract"
                )
            if proposed_contract.get("actor") != request.get("actor"):
                errors.append("successor contract actor must match the request")
            if proposed_contract.get("created_at") != transaction_timestamp:
                errors.append(
                    "successor contract created_at must match the transaction timestamp"
                )
        if isinstance(proposed_projection, dict):
            provenance = proposed_projection.get("provenance")
            if not isinstance(provenance, dict) or provenance != {
                "actor": request.get("actor"),
                "authority": request.get("authority"),
            }:
                errors.append(
                    "successor projection provenance must match request authority"
                )
            if proposed_projection.get("lineage") is not None:
                errors.append(
                    "successor projection must start a new run lineage; "
                    "the migration event binds its source"
                )
        if successor_run_path is not None:
            successor_dir = run_dir.resolve() / successor_run_path
            if successor_dir.exists():
                errors.append("successor run destination already exists")

    if not errors and mode == "successor-run":
        run_path, events_path, _ = _paths(run_dir)
        source_projection_path = resolve_project_path(
            run_dir,
            run["projection"]["path"],
        )
        source_run_sha256 = hashlib.sha256(run_path.read_bytes()).hexdigest()
        source_events_sha256 = hashlib.sha256(
            events_path.read_bytes()
        ).hexdigest()
        source_projection_sha256 = hashlib.sha256(
            source_projection_path.read_bytes()
        ).hexdigest()
        lifecycle_base_digest = sha256_json(
            {
                "graph_checksum": canonical_checksum(graph),
                "run_sha256": source_run_sha256,
                "events_sha256": source_events_sha256,
                "projection_sha256": source_projection_sha256,
                "source_run_id": run["run_id"],
                "source_run_version": run["run_version"],
            }
        )
        lifecycle_plan_digest = sha256_json(
            {
                "lifecycle_base_digest": lifecycle_base_digest,
                "request": request,
                "successor_run_path": successor_run_path,
                "transaction_timestamp": transaction_timestamp,
            }
        )
    apply_supported = (
        not errors
        and mode == "successor-run"
        and outcome_changed
    )
    return {
        "valid": not errors,
        "dry_run": True,
        "apply_supported": apply_supported,
        "errors": errors,
        "migration_id": request.get("migration_id"),
        "mode": request.get("mode"),
        "recommended_mode": "successor-run" if outcome_changed else request.get("mode"),
        "outcome_changed": outcome_changed,
        "contract_diff": contract_diff,
        "current_contract_digest": outcome_contract_digest(old_contract),
        "proposed_contract_digest": (
            outcome_contract_digest(proposed_contract)
            if isinstance(proposed_contract, dict)
            else None
        ),
        "transaction_timestamp": transaction_timestamp,
        "successor_run_id": successor_run_id,
        "successor_run_path": successor_run_path,
        "source_run_sha256": source_run_sha256,
        "source_events_sha256": source_events_sha256,
        "source_projection_sha256": source_projection_sha256,
        "lifecycle_base_digest": lifecycle_base_digest,
        "lifecycle_plan_digest": lifecycle_plan_digest,
        "message": (
            "Authorized successor-run migration is apply-capable."
            if apply_supported
            else "Migration is preview-only; same-run apply is unsupported."
        ),
    }


def _cleanup_failed_successor_scaffold(
    successor_dir: Path,
    source_run_dir: Path,
) -> None:
    lock_path = successor_dir / LOCK_FILENAME
    try:
        if lock_path.is_file() and lock_path.stat().st_size == 0:
            lock_path.unlink()
    except OSError:
        pass
    for path in (
        successor_dir / "projections",
        successor_dir / "snapshots",
        successor_dir / "evidence",
        successor_dir,
        source_run_dir / "successors",
    ):
        try:
            path.rmdir()
        except (FileNotFoundError, OSError):
            pass


def apply_outcome_migration(
    request: dict[str, Any],
    graph_path: Path,
    run_dir: Path,
    *,
    transaction_timestamp: str,
    expected_lifecycle_base_digest: str,
    expected_lifecycle_plan_digest: str,
) -> dict[str, Any]:
    run_dir = run_dir.resolve()
    run_path, events_path, lock_path = _paths(run_dir)
    with file_lock(lock_path):
        report = preview_outcome_migration(
            request,
            graph_path,
            run_dir,
            transaction_timestamp=transaction_timestamp,
        )
        if not report["valid"]:
            raise RuntimeStateError(
                "outcome migration preview failed: "
                + "; ".join(report["errors"])
            )
        if not report["apply_supported"]:
            raise RuntimeStateError(
                "outcome migration apply supports successor-run mode only"
            )
        if report["lifecycle_base_digest"] != expected_lifecycle_base_digest:
            raise StaleStateError("stale outcome migration lifecycle base digest")
        if report["lifecycle_plan_digest"] != expected_lifecycle_plan_digest:
            raise StaleStateError("stale outcome migration lifecycle plan digest")
        if (
            hashlib.sha256(run_path.read_bytes()).hexdigest()
            != report["source_run_sha256"]
            or hashlib.sha256(events_path.read_bytes()).hexdigest()
            != report["source_events_sha256"]
        ):
            raise StaleStateError("source runtime changed after migration preview")

        successor_run_id = report["successor_run_id"]
        successor_dir = run_dir / report["successor_run_path"]
        if successor_dir.exists():
            raise RuntimeStateError("successor run destination already exists")
        source_run = load_run(run_dir)
        try:
            successor = initialize_run(
                graph_path,
                successor_dir,
                request["actor"],
                outcome_contract=request["proposed_outcome_contract"],
                projection=request["proposed_projection"],
                run_id=successor_run_id,
                created_at=transaction_timestamp,
                initial_action="outcome-migration-applied",
                initial_details={
                    "timestamp": transaction_timestamp,
                    "migration_id": request["migration_id"],
                    "source_run_id": source_run["run_id"],
                    "source_run_version": source_run["run_version"],
                    "source_outcome_contract_digest": outcome_contract_digest(
                        source_run["outcome_contract"]
                    ),
                    "source_projection_checksum": source_run["projection"][
                        "checksum"
                    ],
                    "authority": request["authority"],
                    "authority_evidence": list(request["authority_evidence"]),
                    "rationale": request["rationale"],
                    "evidence": list(request["evidence"]),
                },
            )
        except BaseException:
            _cleanup_failed_successor_scaffold(successor_dir, run_dir)
            raise
        return {
            "valid": True,
            "applied": True,
            "migration_id": request["migration_id"],
            "source_run_id": source_run["run_id"],
            "successor_run_id": successor["run_id"],
            "successor_run_path": report["successor_run_path"],
            "successor_run_version": successor["run_version"],
            "lifecycle_base_digest": expected_lifecycle_base_digest,
            "lifecycle_plan_digest": expected_lifecycle_plan_digest,
        }


def _outstanding_deferred_findings(run_dir: Path) -> list[str]:
    outstanding: dict[str, None] = {}
    try:
        records = _event_records(run_dir)
    except RuntimeStateError:
        return []
    for record in records:
        details = record.get("details")
        details = details if isinstance(details, dict) else {}
        if record.get("action") == "finding-deferred":
            finding_id = details.get("finding_id")
            if isinstance(finding_id, str):
                outstanding[finding_id] = None
        elif record.get("action") == "run-nodes-promoted":
            for node_id in details.get("node_ids", []):
                outstanding.pop(node_id, None)
    return sorted(outstanding)

def status_report(graph_path: Path, run_dir: Path) -> dict[str, Any]:
    graph = _load_graph(graph_path)
    run = load_run(run_dir)
    load_execution_graph(graph_path, run_dir, graph=graph, run=run)
    checksum = canonical_checksum(graph)
    counts: dict[str, int] = {}
    for state in run["nodes"].values():
        counts[state["state"]] = counts.get(state["state"], 0) + 1
    backlog = [
        item["id"]
        for item in graph["nodes"]
        if item["id"] not in run["nodes"]
    ]
    report = {
        "run_id": run["run_id"],
        "run_version": run["run_version"],
        "status": run["status"],
        "outcome_status": run["status"],
        "graph_checksum_current": checksum,
        "graph_checksum_matches": checksum == run_observed_graph_checksum(run),
        "state_counts": dict(sorted(counts.items())),
        "ready_nodes": [node_id for node_id, state in run["nodes"].items() if state["state"] == "ready"],
        "needs_human_nodes": [node_id for node_id, state in run["nodes"].items() if state["state"] == "needs_human"],
        "dlg_backlog_nodes": backlog,
    }
    if run["schema_version"] == 2:
        report["projection_checksum"] = run_execution_checksum(run)
        report["outcome_digest"] = run["outcome_contract"]["outcome_digest"]
        report["deferred_findings"] = _outstanding_deferred_findings(run_dir)
        report["needs_human_episodes"] = sorted(
            episode_id
            for episode_id, episode in run["episodes"].items()
            if episode["status"] == "needs_human"
        )
    return report


def verify_runtime_integrity(graph_path: Path, run_dir: Path) -> list[str]:
    errors: list[str] = []
    try:
        graph = _load_graph(graph_path)
        run = load_run(run_dir)
    except (GraphControlError, ValueError) as exc:
        return [str(exc)]
    common_fields = {
        "schema_version", "run_id", "run_version", "graph_id", "status",
        "created_at", "updated_at", "last_event_sequence", "last_event_hash",
        "feedback_traversals", "nodes",
    }
    if run.get("schema_version") == 1:
        required_run_fields = common_fields | {"graph_version", "graph_checksum"}
    else:
        required_run_fields = common_fields | {
            "observed_graph_version",
            "observed_graph_checksum",
            "outcome_contract",
            "projection",
            "episodes",
        }
        errors.extend(_v2_runtime_errors(run))
    missing = sorted(required_run_fields - set(run))
    unknown = sorted(set(run) - required_run_fields)
    errors.extend(
        error
        for error in (f"run.json missing field: {field}" for field in missing)
        if error not in errors
    )
    errors.extend(
        error
        for error in (f"run.json unknown field: {field}" for field in unknown)
        if error not in errors
    )
    if run.get("graph_id") != graph.get("graph_id"):
        errors.append("run graph ID does not match definition")
    current_version = graph.get("graph_version")
    observed_version = (
        run.get("graph_version")
        if run.get("schema_version") == 1
        else run.get("observed_graph_version")
    )
    if observed_version != current_version:
        errors.append("run graph version does not match definition")
    if run_observed_graph_checksum(run) != canonical_checksum(graph):
        errors.append("run graph checksum does not match definition")

    execution_graph: dict[str, Any] | None = None
    try:
        execution_graph = load_execution_graph(
            graph_path,
            run_dir,
            graph=graph,
            run=run,
            verify_ledger=False,
        )
    except (GraphControlError, ValueError) as exc:
        errors.append(str(exc))
    if execution_graph is not None:
        execution_nodes = {item["id"] for item in execution_graph["nodes"]}
        run_nodes = set(run.get("nodes", {})) if isinstance(run.get("nodes"), dict) else set()
        if execution_nodes != run_nodes:
            label = "projection" if run["schema_version"] == 2 else "definition"
            errors.append(
                f"runtime node inventory differs from {label}: "
                f"missing={sorted(execution_nodes - run_nodes)} extra={sorted(run_nodes - execution_nodes)}"
            )

    errors.extend(
        error
        for error in _runtime_ledger_errors(run_dir, run)
        if error not in errors
    )
    return errors


def _json_argument(value: str) -> dict[str, Any]:
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise argparse.ArgumentTypeError("value must be a JSON object")
    return parsed


def _common_mutation(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--graph", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--expected-run-version", type=int, required=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="initialize durable runtime state")
    init.add_argument("--graph", type=Path, required=True)
    init.add_argument("--run-dir", type=Path, required=True)
    init.add_argument("--actor", default="operator")
    init.add_argument("--contract", type=Path)
    init.add_argument("--projection", type=Path)

    for name in ("status", "ready", "verify-ledger"):
        item = sub.add_parser(name)
        item.add_argument("--graph", type=Path, required=True)
        item.add_argument("--run-dir", type=Path, required=True)

    claim = sub.add_parser("claim")
    _common_mutation(claim)
    claim.add_argument("--node", required=True)
    claim.add_argument("--worker-id", required=True)
    claim.add_argument("--lease-seconds", type=int, default=300)
    claim.add_argument("--idempotency-key")

    complete = sub.add_parser("complete")
    _common_mutation(complete)
    complete.add_argument("--node", required=True)
    complete.add_argument("--worker-id", required=True)
    complete.add_argument("--evidence", type=_json_argument, required=True)

    fail = sub.add_parser("fail")
    _common_mutation(fail)
    fail.add_argument("--node", required=True)
    fail.add_argument("--worker-id", required=True)
    fail.add_argument("--result", type=_json_argument, required=True)

    for name in ("approve", "reject"):
        item = sub.add_parser(name)
        _common_mutation(item)
        item.add_argument("--node", required=True)
        item.add_argument("--approver", required=True)
        item.add_argument("--evidence" if name == "approve" else "--reason", required=True)

    invalidate = sub.add_parser("invalidate")
    _common_mutation(invalidate)
    invalidate.add_argument("--node", action="append", default=[])
    invalidate.add_argument("--artifact", action="append", default=[])
    invalidate.add_argument("--reason", required=True)
    invalidate.add_argument("--actor", default="operator")

    resume = sub.add_parser("resume")
    _common_mutation(resume)
    resume.add_argument("--node", action="append", required=True)
    resume.add_argument("--actor", default="operator")

    feedback = sub.add_parser("feedback")
    _common_mutation(feedback)
    feedback.add_argument("--edge", required=True)
    feedback.add_argument("--actor", default="operator")

    promote = sub.add_parser("promote")
    promote.add_argument("--graph", type=Path, required=True)
    promote.add_argument("--run-dir", type=Path, required=True)
    promote.add_argument("--request", type=Path, required=True)
    promote.add_argument("--apply", action="store_true")
    promote.add_argument("--transaction-timestamp")
    promote.add_argument("--expected-lifecycle-base-digest")
    promote.add_argument("--expected-lifecycle-plan-digest")

    defer = sub.add_parser("defer")
    defer.add_argument("--graph", type=Path, required=True)
    defer.add_argument("--run-dir", type=Path, required=True)
    defer.add_argument("--request", type=Path, required=True)
    defer.add_argument("--apply", action="store_true")
    defer.add_argument("--transaction-timestamp")
    defer.add_argument("--expected-run-sha256")
    defer.add_argument("--expected-events-sha256")

    semantic = sub.add_parser("semantic-invalidate")
    semantic.add_argument("--graph", type=Path, required=True)
    semantic.add_argument("--run-dir", type=Path, required=True)
    semantic.add_argument("--request", type=Path, required=True)
    semantic.add_argument("--apply", action="store_true")
    semantic.add_argument("--transaction-timestamp")
    semantic.add_argument("--expected-impact-digest")
    semantic.add_argument("--expected-run-sha256")
    semantic.add_argument("--expected-events-sha256")

    migrate = sub.add_parser("migrate-outcome")
    migrate.add_argument("--graph", type=Path, required=True)
    migrate.add_argument("--run-dir", type=Path, required=True)
    migrate.add_argument("--request", type=Path, required=True)
    migrate.add_argument("--apply", action="store_true")
    migrate.add_argument("--transaction-timestamp")
    migrate.add_argument("--expected-lifecycle-base-digest")
    migrate.add_argument("--expected-lifecycle-plan-digest")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "init":
            result = initialize_run(
                args.graph,
                args.run_dir,
                args.actor,
                outcome_contract=load_json(args.contract) if args.contract else None,
                projection=load_json(args.projection) if args.projection else None,
            )
        elif args.command == "status":
            result = status_report(args.graph, args.run_dir)
        elif args.command == "ready":
            result = {"ready_nodes": status_report(args.graph, args.run_dir)["ready_nodes"]}
        elif args.command == "verify-ledger":
            errors = verify_runtime_integrity(args.graph, args.run_dir)
            result = {"valid": not errors, "errors": errors}
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0 if not errors else 1
        elif args.command == "claim":
            result = claim_node(args.graph, args.run_dir, args.node, args.worker_id, args.expected_run_version, args.lease_seconds, idempotency_key=args.idempotency_key)
        elif args.command == "complete":
            result = complete_node(args.graph, args.run_dir, args.node, args.worker_id, args.expected_run_version, args.evidence)
        elif args.command == "fail":
            result = fail_node(args.graph, args.run_dir, args.node, args.worker_id, args.expected_run_version, args.result)
        elif args.command == "approve":
            result = approve_node(args.graph, args.run_dir, args.node, args.approver, args.expected_run_version, args.evidence)
        elif args.command == "reject":
            result = reject_node(args.graph, args.run_dir, args.node, args.approver, args.expected_run_version, args.reason)
        elif args.command == "invalidate":
            result = invalidate_nodes(args.graph, args.run_dir, args.node, args.artifact, args.reason, args.expected_run_version, actor=args.actor)
        elif args.command == "resume":
            result = resume_nodes(args.graph, args.run_dir, args.node, args.actor, args.expected_run_version)
        elif args.command == "feedback":
            result = follow_feedback(args.graph, args.run_dir, args.edge, args.actor, args.expected_run_version)
        elif args.command == "semantic-invalidate":
            request = load_json(args.request)
            if args.apply:
                if (
                    not args.transaction_timestamp
                    or not args.expected_impact_digest
                    or not args.expected_run_sha256
                    or not args.expected_events_sha256
                ):
                    raise RuntimeStateError(
                        "semantic-invalidate --apply requires "
                        "--transaction-timestamp, --expected-impact-digest, "
                        "--expected-run-sha256, and --expected-events-sha256"
                    )
                result = apply_semantic_invalidation(
                    request,
                    args.graph,
                    args.run_dir,
                    transaction_timestamp=args.transaction_timestamp,
                    expected_impact_digest=args.expected_impact_digest,
                    expected_run_sha256=args.expected_run_sha256,
                    expected_events_sha256=args.expected_events_sha256,
                )
            else:
                result = preview_semantic_invalidation(
                    request,
                    args.graph,
                    args.run_dir,
                )
        elif args.command == "promote":
            request = load_json(args.request)
            if args.apply:
                if (
                    not args.transaction_timestamp
                    or not args.expected_lifecycle_base_digest
                    or not args.expected_lifecycle_plan_digest
                ):
                    raise RuntimeStateError(
                        "promote --apply requires --transaction-timestamp, "
                        "--expected-lifecycle-base-digest, and "
                        "--expected-lifecycle-plan-digest"
                    )
                result = apply_promotion(
                    request,
                    args.graph,
                    args.run_dir,
                    transaction_timestamp=args.transaction_timestamp,
                    expected_lifecycle_base_digest=(
                        args.expected_lifecycle_base_digest
                    ),
                    expected_lifecycle_plan_digest=(
                        args.expected_lifecycle_plan_digest
                    ),
                )
            else:
                result = preview_promotion(request, args.graph, args.run_dir)
        elif args.command == "defer":
            request = load_json(args.request)
            if args.apply:
                if (
                    not args.transaction_timestamp
                    or not args.expected_run_sha256
                    or not args.expected_events_sha256
                ):
                    raise RuntimeStateError(
                        "defer --apply requires --transaction-timestamp, "
                        "--expected-run-sha256, and --expected-events-sha256"
                    )
                result = apply_defer(
                    request,
                    args.graph,
                    args.run_dir,
                    transaction_timestamp=args.transaction_timestamp,
                    expected_run_sha256=args.expected_run_sha256,
                    expected_events_sha256=args.expected_events_sha256,
                )
            else:
                result = preview_defer(request, args.graph, args.run_dir)
        elif args.command == "migrate-outcome":
            request = load_json(args.request)
            if args.apply:
                if (
                    not args.transaction_timestamp
                    or not args.expected_lifecycle_base_digest
                    or not args.expected_lifecycle_plan_digest
                ):
                    raise RuntimeStateError(
                        "migrate-outcome --apply requires "
                        "--transaction-timestamp, "
                        "--expected-lifecycle-base-digest, and "
                        "--expected-lifecycle-plan-digest"
                    )
                result = apply_outcome_migration(
                    request,
                    args.graph,
                    args.run_dir,
                    transaction_timestamp=args.transaction_timestamp,
                    expected_lifecycle_base_digest=(
                        args.expected_lifecycle_base_digest
                    ),
                    expected_lifecycle_plan_digest=(
                        args.expected_lifecycle_plan_digest
                    ),
                )
            else:
                result = preview_outcome_migration(
                    request,
                    args.graph,
                    args.run_dir,
                )
        else:  # pragma: no cover - argparse prevents this
            raise RuntimeStateError(f"unsupported command: {args.command}")
    except (GraphControlError, ValueError, PermissionError, OSError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("valid", True) else 1


if __name__ == "__main__":
    sys.exit(main())
