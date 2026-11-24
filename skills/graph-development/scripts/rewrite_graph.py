"""Preview or apply a governed mutation to a development-graph definition."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

from graph_core import (
    GraphControlError,
    RuntimeStateError,
    StaleStateError,
    affected_node_ids,
    artifact_affected_nodes,
    canonical_bytes,
    file_lock,
    load_json,
    outcome_contract_digest,
    sha256_json,
    utc_now,
    verify_event_chain,
)
from causal_episodes import (
    apply_repair_cycle,
    episode_gate_document_errors,
    preview_repair_cycle,
)
from graphctl import (
    load_execution_graph,
    load_run,
    run_execution_checksum,
    run_observed_graph_checksum,
)
from project_lifecycle import (
    LifecycleError,
    apply_project,
    preview_project,
)
from render_graph import render_markdown
from validate_graph import SLUG, canonical_checksum, validate_graph


OPERATIONS = {
    "add-node", "replace-node", "remove-node",
    "add-edge", "replace-edge", "remove-edge", "update-root",
}
ROOT_UPDATES_V1 = {"title", "purpose", "status", "templates", "assumptions", "risks"}
ROOT_UPDATES_V2 = ROOT_UPDATES_V1 | {"entry_nodes", "terminal_nodes"}
DESTRUCTIVE_OPERATIONS = {"remove-node", "remove-edge"}


def validate_mutation(mutation: Any) -> list[str]:
    errors: list[str] = []
    common_required = {
        "schema_version", "mutation_id", "rationale", "evidence",
        "expected_graph_version", "expected_graph_checksum", "new_context_version",
        "operations", "invalidate", "approval",
    }
    if not isinstance(mutation, dict):
        return ["$: mutation must be an object"]
    schema_version = mutation.get("schema_version")
    required = set(common_required)
    if schema_version == 2:
        required.update(
            {
                "runtime_effect",
                "expected_run_version",
                "causal_episode_id",
                "episode_effect",
                "episode_gate",
            }
        )
    unknown = sorted(set(mutation) - required)
    missing = sorted(required - set(mutation))
    errors.extend(f"$.{name}: is required" for name in missing)
    errors.extend(f"$.{name}: is not allowed" for name in unknown)
    if schema_version not in {1, 2}:
        errors.append("$.schema_version: must equal 1 or 2")
    if schema_version == 2:
        if mutation.get("runtime_effect") != "record-only":
            errors.append("$.runtime_effect: must equal record-only")
        episode_id = mutation.get("causal_episode_id")
        if not isinstance(episode_id, str) or not SLUG.fullmatch(episode_id):
            errors.append("$.causal_episode_id: must be a lowercase slug")
        if mutation.get("episode_effect") not in {
            "observe-only",
            "repair-cycle",
        }:
            errors.append(
                "$.episode_effect: must equal observe-only or repair-cycle"
            )
        errors.extend(episode_gate_document_errors(mutation.get("episode_gate")))
        if (
            mutation.get("episode_effect") == "observe-only"
            and mutation.get("episode_gate") is not None
        ):
            errors.append(
                "$.episode_gate: observe-only mutation cannot consume a repair gate"
            )
        expected_run_version = mutation.get("expected_run_version")
        if (
            not isinstance(expected_run_version, int)
            or isinstance(expected_run_version, bool)
            or expected_run_version < 1
        ):
            errors.append("$.expected_run_version: must be a positive integer")
    mutation_id = mutation.get("mutation_id")
    if not isinstance(mutation_id, str) or not SLUG.fullmatch(mutation_id):
        errors.append("$.mutation_id: must be a lowercase slug")
    for field in ("rationale", "new_context_version"):
        if not isinstance(mutation.get(field), str) or not mutation[field].strip():
            errors.append(f"$.{field}: must be a non-empty string")
    evidence = mutation.get("evidence")
    if (
        not isinstance(evidence, list)
        or not evidence
        or not all(isinstance(item, str) and item.strip() for item in evidence)
    ):
        errors.append(
            "$.evidence: must contain at least one non-empty evidence reference"
        )
    expected_graph_version = mutation.get("expected_graph_version")
    if (
        not isinstance(expected_graph_version, int)
        or isinstance(expected_graph_version, bool)
        or expected_graph_version < 1
    ):
        errors.append("$.expected_graph_version: must be a positive integer")
    checksum = mutation.get("expected_graph_checksum")
    if (
        not isinstance(checksum, str)
        or len(checksum) != 64
        or any(char not in "0123456789abcdef" for char in checksum)
    ):
        errors.append(
            "$.expected_graph_checksum: must be a lowercase SHA-256 digest"
        )
    operations = mutation.get("operations")
    root_updates = ROOT_UPDATES_V2 if schema_version == 2 else ROOT_UPDATES_V1
    if not isinstance(operations, list) or not operations:
        errors.append("$.operations: must contain at least one operation")
    else:
        for index, operation in enumerate(operations):
            if not isinstance(operation, dict):
                errors.append(f"$.operations[{index}]: must be an object")
                continue
            op = operation.get("op")
            if op not in OPERATIONS:
                errors.append(
                    f"$.operations[{index}].op: unsupported operation {op!r}"
                )
            if op == "update-root":
                if set(operation) != {"op", "field", "value"}:
                    errors.append(
                        f"$.operations[{index}]: update-root requires only "
                        "op, field, and value"
                    )
                if operation.get("field") not in root_updates:
                    errors.append(
                        f"$.operations[{index}].field: field is not mutable"
                    )
            elif op in {"remove-node", "remove-edge"}:
                if set(operation) != {"op", "id"}:
                    errors.append(
                        f"$.operations[{index}]: remove operations require only op and id"
                    )
            elif op in OPERATIONS:
                if set(operation) != {"op", "id", "value"}:
                    errors.append(
                        f"$.operations[{index}]: add/replace operations require "
                        "op, id, and value"
                    )
                if not isinstance(operation.get("value"), dict):
                    errors.append(
                        f"$.operations[{index}].value: must be an object"
                    )
                elif operation["value"].get("id") != operation.get("id"):
                    errors.append(
                        f"$.operations[{index}].value.id: must match operation id"
                    )
    invalidate = mutation.get("invalidate")
    if (
        not isinstance(invalidate, dict)
        or set(invalidate) != {"nodes", "artifacts"}
    ):
        errors.append(
            "$.invalidate: must contain only nodes and artifacts arrays"
        )
    else:
        for field in ("nodes", "artifacts"):
            values = invalidate.get(field)
            if (
                not isinstance(values, list)
                or not all(
                    isinstance(item, str) and item.strip() for item in values
                )
            ):
                errors.append(
                    f"$.invalidate.{field}: must be an array of non-empty strings"
                )
            elif schema_version == 2 and values:
                errors.append(
                    f"$.invalidate.{field}: record-only mutation must be empty"
                )
    approval = mutation.get("approval")
    if approval is not None:
        approval_fields = {
            "decision", "approver", "evidence", "graph_checksum",
        }
        if not isinstance(approval, dict) or set(approval) != approval_fields:
            errors.append(
                "$.approval: must be null or contain decision, approver, "
                "evidence, and graph_checksum"
            )
        elif (
            approval.get("decision") != "approved"
            or not all(
                isinstance(approval.get(field), str) and approval[field].strip()
                for field in ("approver", "evidence")
            )
        ):
            errors.append(
                "$.approval: must record an approved decision with approver "
                "and evidence"
            )
    return errors

def _replace(items: list[dict[str, Any]], item_id: str, value: dict[str, Any], *, add: bool) -> None:
    positions = [index for index, item in enumerate(items) if item.get("id") == item_id]
    if add:
        if positions:
            raise ValueError(f"id already exists: {item_id}")
        items.append(copy.deepcopy(value))
    else:
        if len(positions) != 1:
            raise ValueError(f"id does not exist exactly once: {item_id}")
        items[positions[0]] = copy.deepcopy(value)


def _remove(items: list[dict[str, Any]], item_id: str) -> None:
    positions = [index for index, item in enumerate(items) if item.get("id") == item_id]
    if len(positions) != 1:
        raise ValueError(f"id does not exist exactly once: {item_id}")
    del items[positions[0]]


def _candidate(
    mutation: dict[str, Any],
    graph: dict[str, Any],
    *,
    track_invalidation: bool = True,
) -> tuple[dict[str, Any], set[str], set[str], list[str]]:
    candidate = copy.deepcopy(graph)
    seeds: set[str] = (
        set(mutation["invalidate"]["nodes"]) if track_invalidation else set()
    )
    removed_nodes: set[str] = set()
    changes: list[str] = []
    for operation in mutation["operations"]:
        op, item_id = operation["op"], operation.get("id")
        if op == "add-node":
            _replace(candidate["nodes"], item_id, operation["value"], add=True)
            if track_invalidation:
                seeds.add(item_id)
        elif op == "replace-node":
            _replace(candidate["nodes"], item_id, operation["value"], add=False)
            if track_invalidation:
                seeds.add(item_id)
        elif op == "remove-node":
            _remove(candidate["nodes"], item_id)
            removed_nodes.add(item_id)
        elif op == "add-edge":
            _replace(candidate["edges"], item_id, operation["value"], add=True)
            if track_invalidation:
                seeds.add(operation["value"]["to"])
        elif op == "replace-edge":
            _replace(candidate["edges"], item_id, operation["value"], add=False)
            if track_invalidation:
                seeds.add(operation["value"]["to"])
        elif op == "remove-edge":
            old = next(
                item for item in candidate["edges"] if item.get("id") == item_id
            )
            if track_invalidation:
                seeds.add(old["to"])
            _remove(candidate["edges"], item_id)
        elif op == "update-root":
            candidate[operation["field"]] = copy.deepcopy(operation["value"])
            if track_invalidation:
                seeds.update(item["id"] for item in candidate["nodes"])
        changes.append(f"{op}:{item_id or operation.get('field')}")
    candidate["graph_version"] = graph["graph_version"] + 1
    candidate["project"]["context_version"] = mutation["new_context_version"]
    if track_invalidation:
        seeds |= artifact_affected_nodes(
            candidate,
            set(mutation["invalidate"]["artifacts"]),
        )
    return candidate, seeds, removed_nodes, changes

def _destructive_approval_valid(mutation: dict[str, Any], checksum: str) -> bool:
    approval = mutation.get("approval")
    return bool(
        isinstance(approval, dict)
        and approval.get("decision") == "approved"
        and approval.get("graph_checksum") == checksum
    )


def _refresh_run_status(graph: dict[str, Any], run: dict[str, Any]) -> None:
    terminal_states = [run["nodes"][node_id]["state"] for node_id in graph["terminal_nodes"]]
    if terminal_states and all(state == "verified" for state in terminal_states):
        run["status"] = "completed"
    elif any(item["state"] == "running" for item in run["nodes"].values()):
        run["status"] = "running"
    elif any(item["state"] == "needs_human" for item in run["nodes"].values()):
        run["status"] = "needs_human"
    else:
        run["status"] = "active"


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _json_payload(value: dict[str, Any]) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True).encode(
            "utf-8"
        )
        + b"\n"
    )


def _relative_project_path(project_root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(project_root.resolve()).as_posix()
    except ValueError as exc:
        raise RuntimeStateError(
            f"mutation artifact is outside the project root: {path}"
        ) from exc


def _next_event_candidate(
    previous_payload: bytes,
    previous: dict[str, Any],
    event: dict[str, Any],
    *,
    timestamp: str,
) -> tuple[dict[str, Any], bytes]:
    record = copy.deepcopy(event)
    record["timestamp"] = timestamp
    record["sequence"] = int(previous["sequence"]) + 1
    record["previous_hash"] = previous["event_hash"]
    record.pop("event_hash", None)
    record["event_hash"] = sha256_json(record)
    separator = b"" if previous_payload.endswith(b"\n") else b"\n"
    return record, previous_payload + separator + canonical_bytes(record) + b"\n"


def _event_candidate(
    path: Path,
    event: dict[str, Any],
    *,
    timestamp: str,
) -> tuple[dict[str, Any], bytes]:
    errors = verify_event_chain(path)
    if errors:
        raise RuntimeStateError(
            "cannot mutate with an invalid event ledger: " + "; ".join(errors)
        )
    previous_payload = path.read_bytes()
    lines = previous_payload.decode("utf-8").splitlines()
    previous = json.loads(lines[-1])
    return _next_event_candidate(
        previous_payload,
        previous,
        event,
        timestamp=timestamp,
    )


def _mutation_transaction(
    mutation: dict[str, Any],
    graph_path: Path,
    run_dir: Path,
    graph: dict[str, Any],
    candidate: dict[str, Any],
    run: dict[str, Any],
    affected: list[str],
    removed_nodes: set[str],
    *,
    actor: str,
    authority: str,
    transaction_timestamp: str,
) -> dict[str, Any]:
    project_root = graph_path.resolve().parent
    run_dir = run_dir.resolve()
    _relative_project_path(project_root, run_dir)
    previous_graph_bytes = graph_path.read_bytes()
    snapshot = (
        run_dir
        / "snapshots"
        / f"graph-v{graph['graph_version']}-{mutation['mutation_id']}.json"
    )
    if snapshot.exists():
        raise RuntimeStateError(f"rollback snapshot already exists: {snapshot}")
    markdown_path = graph_path.with_name("development-graph.md")
    markdown = render_markdown(candidate, graph_path.name)
    parity = validate_graph(candidate, markdown)
    if not parity["valid"]:
        raise ValueError(
            "candidate Markdown parity failed: " + "; ".join(parity["errors"])
        )

    candidate_run = copy.deepcopy(run)
    old_nodes = set(candidate_run["nodes"])
    new_nodes = {item["id"]: item for item in candidate["nodes"]}
    for removed in removed_nodes:
        candidate_run["nodes"].pop(removed, None)
    for node_id in new_nodes.keys() - old_nodes:
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
    invalidated: list[str] = []
    for node_id in affected:
        if node_id in old_nodes and node_id in candidate_run["nodes"]:
            state = candidate_run["nodes"][node_id]
            state["state"] = "invalidated"
            state["lease"] = None
            state["approval"] = None
            state["last_result"] = {
                "invalidated_by_mutation": mutation["mutation_id"]
            }
            invalidated.append(node_id)
    run_version_before = candidate_run["run_version"]
    candidate_run["graph_version"] = candidate["graph_version"]
    candidate_run["graph_checksum"] = canonical_checksum(candidate)
    _refresh_run_status(candidate, candidate_run)
    candidate_run["run_version"] += 1
    candidate_run["updated_at"] = transaction_timestamp
    events_path = run_dir / "events.jsonl"
    event, events_payload = _event_candidate(
        events_path,
        {
            "run_id": candidate_run["run_id"],
            "graph_id": candidate_run["graph_id"],
            "graph_checksum": candidate_run["graph_checksum"],
            "run_version_before": run_version_before,
            "run_version_after": candidate_run["run_version"],
            "actor": actor,
            "action": "graph-mutated",
            "details": {
                "mutation_id": mutation["mutation_id"],
                "previous_graph_checksum": canonical_checksum(graph),
                "new_graph_checksum": candidate_run["graph_checksum"],
                "snapshot": _relative_project_path(project_root, snapshot),
                "invalidated_nodes": invalidated,
                "removed_nodes": sorted(removed_nodes),
            },
        },
        timestamp=transaction_timestamp,
    )
    candidate_run["last_event_sequence"] = event["sequence"]
    candidate_run["last_event_hash"] = event["event_hash"]

    artifact_values = (
        (graph_path, _json_payload(candidate), 0o600),
        (markdown_path, markdown.encode("utf-8"), 0o644),
        (run_dir / "run.json", _json_payload(candidate_run), 0o600),
        (events_path, events_payload, 0o600),
        (snapshot, previous_graph_bytes, 0o600),
    )
    artifacts = []
    for path, payload, mode in artifact_values:
        artifacts.append(
            {
                "path": _relative_project_path(project_root, path),
                "content": payload,
                "mode": mode,
                "expected_sha256": (
                    _sha256(path.read_bytes()) if path.is_file() else None
                ),
            }
        )
    request = {
        "schema_version": 1,
        "operation": "graph-mutation",
        "project_root": project_root,
        "artifacts": artifacts,
        "metadata": {
            "entrypoint": "rewrite_graph",
            "mutation_id": mutation["mutation_id"],
            "expected_graph_checksum": canonical_checksum(graph),
            "expected_run_version": run_version_before,
            "authority": authority,
        },
    }
    plan = preview_project(request)
    return {
        "request": request,
        "plan": plan,
        "candidate_run": candidate_run,
        "event": event,
        "snapshot": snapshot,
        "previous_graph_bytes": previous_graph_bytes,
        "invalidated_nodes": invalidated,
    }


def _record_transaction(
    mutation: dict[str, Any],
    graph_path: Path,
    run_dir: Path,
    graph: dict[str, Any],
    candidate: dict[str, Any],
    run: dict[str, Any],
    changes: list[str],
    removed_nodes: set[str],
    episode_transition: dict[str, Any] | None,
    *,
    actor: str,
    authority: str,
    transaction_timestamp: str,
) -> dict[str, Any]:
    if run.get("schema_version") != 2:
        raise RuntimeStateError("record-only mutation requires a schema-v2 run")
    project_root = graph_path.resolve().parent
    run_dir = run_dir.resolve()
    _relative_project_path(project_root, run_dir)
    previous_graph_bytes = graph_path.read_bytes()
    snapshot = (
        run_dir
        / "snapshots"
        / f"graph-v{graph['graph_version']}-{mutation['mutation_id']}.json"
    )
    if snapshot.exists():
        raise RuntimeStateError(f"rollback snapshot already exists: {snapshot}")
    markdown_path = graph_path.with_name("development-graph.md")
    markdown = render_markdown(candidate, graph_path.name)
    parity = validate_graph(candidate, markdown)
    if not parity["valid"]:
        raise ValueError(
            "candidate Markdown parity failed: " + "; ".join(parity["errors"])
        )

    graph_checksum_before = canonical_checksum(graph)
    graph_checksum_after = canonical_checksum(candidate)
    projection_checksum = run_execution_checksum(run)
    if not isinstance(projection_checksum, str):
        raise RuntimeStateError("runtime projection checksum is unavailable")
    candidate_run = copy.deepcopy(run)
    run_version_before = candidate_run["run_version"]
    candidate_run["observed_graph_version"] = candidate["graph_version"]
    candidate_run["observed_graph_checksum"] = graph_checksum_after
    candidate_run["updated_at"] = transaction_timestamp

    contract = candidate_run["outcome_contract"]
    contract_digest = outcome_contract_digest(contract)
    event, events_payload = _event_candidate(
        run_dir / "events.jsonl",
        {
            "run_id": candidate_run["run_id"],
            "graph_id": candidate_run["graph_id"],
            "graph_checksum": graph_checksum_after,
            "projection_checksum": projection_checksum,
            "outcome_contract_digest": contract_digest,
            "outcome_digest": contract["outcome_digest"],
            "run_version_before": run_version_before,
            "run_version_after": run_version_before + 1,
            "actor": actor,
            "action": "graph-definition-recorded",
            "details": {
                "mutation_id": mutation["mutation_id"],
                "runtime_effect": "record-only",
                "graph_checksum_before": graph_checksum_before,
                "graph_checksum_after": graph_checksum_after,
                "projection_checksum_before": projection_checksum,
                "projection_checksum_after": projection_checksum,
                "outcome_contract_digest": contract_digest,
                "outcome_digest": contract["outcome_digest"],
                "snapshot": _relative_project_path(project_root, snapshot),
                "changes": list(changes),
                "invalidated_nodes": [],
                "removed_nodes": sorted(removed_nodes),
                "semantic_invalidation": {"nodes": [], "artifacts": []},
                "authority": authority,
                "evidence": list(mutation["evidence"]),
                "rationale": mutation["rationale"],
            },
        },
        timestamp=transaction_timestamp,
    )
    candidate_run["run_version"] = event["run_version_after"]
    candidate_run["last_event_sequence"] = event["sequence"]
    candidate_run["last_event_hash"] = event["event_hash"]

    if episode_transition is not None:
        episode_details = apply_repair_cycle(
            candidate_run,
            episode_transition,
            timestamp=transaction_timestamp,
        )
        episode_event = {
            "run_id": candidate_run["run_id"],
            "graph_id": candidate_run["graph_id"],
            "graph_checksum": graph_checksum_after,
            "projection_checksum": projection_checksum,
            "outcome_contract_digest": contract_digest,
            "outcome_digest": contract["outcome_digest"],
            "run_version_before": candidate_run["run_version"],
            "run_version_after": candidate_run["run_version"] + 1,
            "actor": actor,
            "action": "repair-episode-needs-human",
            "details": episode_details,
        }
        episode_record, events_payload = _next_event_candidate(
            events_payload,
            event,
            episode_event,
            timestamp=transaction_timestamp,
        )
        event = episode_record
        candidate_run["run_version"] = event["run_version_after"]
        candidate_run["last_event_sequence"] = event["sequence"]
        candidate_run["last_event_hash"] = event["event_hash"]

    artifact_values = (
        (graph_path, _json_payload(candidate), 0o600),
        (markdown_path, markdown.encode("utf-8"), 0o644),
        (run_dir / "run.json", _json_payload(candidate_run), 0o600),
        (run_dir / "events.jsonl", events_payload, 0o600),
        (snapshot, previous_graph_bytes, 0o600),
    )
    artifacts = [
        {
            "path": _relative_project_path(project_root, path),
            "content": payload,
            "mode": mode,
            "expected_sha256": (
                _sha256(path.read_bytes()) if path.is_file() else None
            ),
        }
        for path, payload, mode in artifact_values
    ]
    request = {
        "schema_version": 1,
        "operation": "graph-mutation",
        "project_root": project_root,
        "artifacts": artifacts,
        "metadata": {
            "entrypoint": "rewrite_graph",
            "mutation_id": mutation["mutation_id"],
            "runtime_effect": "record-only",
            "expected_graph_checksum": graph_checksum_before,
            "expected_run_version": run_version_before,
            "expected_contract_digest": contract_digest,
            "authority": authority,
        },
    }
    plan = preview_project(request)
    return {
        "request": request,
        "plan": plan,
        "candidate_run": candidate_run,
        "event": event,
        "snapshot": snapshot,
        "previous_graph_bytes": previous_graph_bytes,
        "invalidated_nodes": [],
        "episode_transition": episode_transition,
    }

def preview_mutation(
    mutation: dict[str, Any],
    graph_path: Path,
    run_dir: Path,
    *,
    actor: str | None = None,
    authority: str | None = None,
    transaction_timestamp: str | None = None,
) -> dict[str, Any]:
    errors = validate_mutation(mutation)
    try:
        graph = load_json(graph_path)
        run = load_run(run_dir)
    except GraphControlError as exc:
        return {
            "valid": False,
            "errors": errors + [str(exc)],
            "changes": [],
            "affected_nodes": [],
            "preserved_nodes": [],
        }
    current_checksum = canonical_checksum(graph)
    if mutation.get("expected_graph_version") != graph.get("graph_version"):
        errors.append(
            "$.expected_graph_version: does not match the current graph"
        )
    if mutation.get("expected_graph_checksum") != current_checksum:
        errors.append(
            "$.expected_graph_checksum: does not match the current graph"
        )

    runtime_schema = run.get("schema_version")
    mutation_schema = mutation.get("schema_version")
    execution_graph: dict[str, Any] | None = None
    if runtime_schema == 1:
        if mutation_schema != 1:
            errors.append(
                "$.schema_version: schema-v1 runtime requires a schema-v1 mutation"
            )
        if run.get("graph_checksum") != current_checksum:
            errors.append(
                "$runtime.graph_checksum: runtime is not bound to the current graph"
            )
    elif runtime_schema == 2:
        if mutation_schema != 2:
            errors.append(
                "$.schema_version: schema-v2 runtime requires a schema-v2 "
                "record-only mutation"
            )
        if run.get("observed_graph_version") != graph.get("graph_version"):
            errors.append(
                "$runtime.observed_graph_version: runtime is not bound to "
                "the current graph"
            )
        if run_observed_graph_checksum(run) != current_checksum:
            errors.append(
                "$runtime.observed_graph_checksum: runtime is not bound to "
                "the current graph"
            )
        if mutation.get("expected_run_version") != run.get("run_version"):
            errors.append(
                "$.expected_run_version: does not match the current run"
            )
        if not errors:
            try:
                execution_graph = load_execution_graph(
                    graph_path,
                    run_dir,
                    graph=graph,
                    run=run,
                )
            except (GraphControlError, ValueError) as exc:
                errors.append(str(exc))
    else:
        errors.append("$runtime.schema_version: must equal 1 or 2")

    requires_approval = any(
        item.get("op") in DESTRUCTIVE_OPERATIONS
        for item in mutation.get("operations", [])
        if isinstance(item, dict)
    )
    if requires_approval and not _destructive_approval_valid(
        mutation,
        current_checksum,
    ):
        errors.append(
            "$.approval: destructive topology changes require approval bound "
            "to the current graph checksum"
        )

    candidate: dict[str, Any] | None = None
    seeds: set[str] = set()
    removed_nodes: set[str] = set()
    changes: list[str] = []
    if not errors:
        try:
            candidate, seeds, removed_nodes, changes = _candidate(
                mutation,
                graph,
                track_invalidation=runtime_schema == 1,
            )
        except (KeyError, StopIteration, ValueError, TypeError) as exc:
            errors.append(f"$.operations: {exc}")
    if candidate is not None:
        report = validate_graph(candidate)
        errors.extend(report["errors"])

    current_order = [item["id"] for item in graph.get("nodes", [])]
    if runtime_schema == 1:
        affected = affected_node_ids(candidate or graph, seeds)
        affected = [item for item in affected if item not in removed_nodes]
        preserved = [
            item
            for item in current_order
            if item not in set(affected) | removed_nodes
        ]
    else:
        affected = []
        preserved = list(run.get("nodes", {}))

    episode_transition: dict[str, Any] | None = None
    episode_gate_errors: list[str] = []
    if (
        not errors
        and runtime_schema == 2
        and mutation.get("episode_effect") == "repair-cycle"
        and execution_graph is not None
    ):
        operation_commitment = copy.deepcopy(mutation)
        operation_commitment.pop("episode_gate", None)
        episode_transition = preview_repair_cycle(
            run,
            execution_graph,
            operation_kind="mutation",
            operation_id=mutation["mutation_id"],
            causal_episode_id=mutation["causal_episode_id"],
            classification="repair-mutation",
            evidence=list(mutation["evidence"]),
            operation_commitment=operation_commitment,
            episode_gate=mutation.get("episode_gate"),
            progress_node_ids=[],
            actor=actor,
            authority=authority,
        )
        errors.extend(episode_transition["errors"])
        episode_gate_errors = list(episode_transition["gate_errors"])

    apply_allowed = not errors and not episode_gate_errors
    lifecycle: dict[str, Any] = {}
    if (
        apply_allowed
        and candidate is not None
        and (actor is not None or authority is not None)
    ):
        if not actor or not authority:
            errors.append("transaction preview requires both actor and authority")
        else:
            active: list[str] = []
            if runtime_schema == 1:
                active = [
                    item
                    for item in set(affected) | removed_nodes
                    if item in run["nodes"]
                    and run["nodes"][item]["state"] == "running"
                ]
            if active:
                errors.append(
                    "cannot mutate active running nodes: "
                    + ", ".join(sorted(active))
                )
            else:
                timestamp = transaction_timestamp or utc_now()
                try:
                    if runtime_schema == 2:
                        transaction = _record_transaction(
                            mutation,
                            graph_path.resolve(),
                            run_dir.resolve(),
                            graph,
                            candidate,
                            run,
                            changes,
                            removed_nodes,
                            episode_transition,
                            actor=actor,
                            authority=authority,
                            transaction_timestamp=timestamp,
                        )
                    else:
                        transaction = _mutation_transaction(
                            mutation,
                            graph_path.resolve(),
                            run_dir.resolve(),
                            graph,
                            candidate,
                            run,
                            affected,
                            removed_nodes,
                            actor=actor,
                            authority=authority,
                            transaction_timestamp=timestamp,
                        )
                except (
                    GraphControlError,
                    LifecycleError,
                    RuntimeStateError,
                    OSError,
                    UnicodeError,
                    ValueError,
                ) as exc:
                    errors.append(str(exc))
                else:
                    plan = transaction["plan"]
                    if not plan.valid:
                        errors.extend(plan.errors)
                        errors.extend(
                            f"lifecycle blocked: {item}" for item in plan.blocked
                        )
                        errors.extend(
                            f"lifecycle conflict: {item}"
                            for item in plan.conflicts
                        )
                    lifecycle = {
                        "transaction_timestamp": timestamp,
                        "lifecycle_base_digest": plan.base_digest,
                        "lifecycle_plan_digest": plan.plan_digest,
                        "lifecycle_candidate_digest": plan.candidate_digest,
                    }
    return {
        "valid": not errors,
        "apply_allowed": apply_allowed,
        "errors": errors,
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
        "mutation_id": mutation.get("mutation_id"),
        "runtime_effect": (
            "record-only" if runtime_schema == 2 else "runtime-rebind"
        ),
        "dry_run": True,
        "current_graph_checksum": current_checksum,
        "candidate_graph_checksum": (
            canonical_checksum(candidate)
            if candidate is not None and not errors
            else None
        ),
        "candidate_graph_version": (
            candidate.get("graph_version") if candidate is not None else None
        ),
        "changes": changes,
        "affected_nodes": affected,
        "preserved_nodes": preserved,
        "removed_nodes": sorted(removed_nodes),
        "requires_approval": requires_approval,
        "approval_present": _destructive_approval_valid(
            mutation,
            current_checksum,
        ),
        "run_version": run.get("run_version"),
        **lifecycle,
    }

def apply_mutation(
    mutation: dict[str, Any],
    graph_path: Path,
    run_dir: Path,
    *,
    expected_graph_checksum: str,
    expected_run_version: int,
    actor: str,
    authority: str,
    transaction_timestamp: str,
    expected_lifecycle_base_digest: str,
    expected_lifecycle_plan_digest: str,
) -> dict[str, Any]:
    graph_path = graph_path.resolve()
    run_dir = run_dir.resolve()
    lock_path = run_dir / "runtime.lock"
    with file_lock(lock_path):
        graph = load_json(graph_path)
        current_checksum = canonical_checksum(graph)
        run = load_run(run_dir)
        if (
            expected_graph_checksum != current_checksum
            or mutation.get("expected_graph_checksum") != current_checksum
        ):
            raise StaleStateError("stale graph checksum")
        if expected_run_version != run["run_version"]:
            raise StaleStateError(
                f"stale run version: expected {expected_run_version}, "
                f"current {run['run_version']}"
            )
        if (
            mutation.get("schema_version") == 2
            and mutation.get("expected_run_version") != run["run_version"]
        ):
            raise StaleStateError("stale mutation run version")

        report = preview_mutation(
            mutation,
            graph_path,
            run_dir,
            actor=actor,
            authority=authority,
            transaction_timestamp=transaction_timestamp,
        )
        if not report["valid"] or not report["apply_allowed"]:
            failures = (
                list(report["errors"])
                + list(report["episode_gate_errors"])
            )
            raise ValueError(
                "mutation preview failed: " + "; ".join(failures)
            )

        runtime_schema = run["schema_version"]
        candidate, seeds, removed_nodes, changes = _candidate(
            mutation,
            graph,
            track_invalidation=runtime_schema == 1,
        )
        if runtime_schema == 1:
            affected = affected_node_ids(candidate, seeds)
            active = [
                item
                for item in set(affected) | removed_nodes
                if item in run["nodes"]
                and run["nodes"][item]["state"] == "running"
            ]
            if active:
                raise RuntimeStateError(
                    "cannot mutate active running nodes: "
                    + ", ".join(sorted(active))
                )
            transaction = _mutation_transaction(
                mutation,
                graph_path,
                run_dir,
                graph,
                candidate,
                run,
                affected,
                removed_nodes,
                actor=actor,
                authority=authority,
                transaction_timestamp=transaction_timestamp,
            )
        else:
            transaction = _record_transaction(
                mutation,
                graph_path,
                run_dir,
                graph,
                candidate,
                run,
                changes,
                removed_nodes,
                report["episode_transition"],
                actor=actor,
                authority=authority,
                transaction_timestamp=transaction_timestamp,
            )

        plan = transaction["plan"]
        if plan.base_digest != expected_lifecycle_base_digest:
            raise StaleStateError("stale lifecycle base digest")
        if plan.plan_digest != expected_lifecycle_plan_digest:
            raise StaleStateError("stale lifecycle plan digest")
        receipt = apply_project(
            transaction["request"],
            expected_base_digest=expected_lifecycle_base_digest,
            expected_plan_digest=expected_lifecycle_plan_digest,
            actor=actor,
            authority=authority,
        )
        if not receipt.applied:
            raise RuntimeStateError(
                "graph mutation transaction "
                f"{receipt.state}: {receipt.error or 'apply failed'}"
            )
        candidate_run = transaction["candidate_run"]
        return {
            "valid": True,
            "applied": True,
            "mutation_id": mutation["mutation_id"],
            "causal_episode_id": mutation.get("causal_episode_id"),
            "runtime_effect": report["runtime_effect"],
            "graph_checksum": canonical_checksum(candidate),
            "graph_version": candidate["graph_version"],
            "run_version": candidate_run["run_version"],
            "invalidated_nodes": transaction["invalidated_nodes"],
            "preserved_nodes": report["preserved_nodes"],
            "snapshot": transaction["snapshot"],
            "previous_graph_bytes": transaction["previous_graph_bytes"],
            "transaction_id": receipt.transaction_id,
            "lifecycle_plan_digest": receipt.plan_digest,
        }

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mutation", type=Path)
    parser.add_argument("--graph", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expected-graph-checksum")
    parser.add_argument("--expected-run-version", type=int)
    parser.add_argument("--actor")
    parser.add_argument("--authority")
    parser.add_argument("--transaction-timestamp")
    parser.add_argument("--expected-lifecycle-base-digest")
    parser.add_argument("--expected-lifecycle-plan-digest")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        mutation = load_json(args.mutation)
        if args.apply:
            if (
                args.expected_graph_checksum is None
                or args.expected_run_version is None
                or not args.actor
                or not args.authority
                or not args.transaction_timestamp
                or not args.expected_lifecycle_base_digest
                or not args.expected_lifecycle_plan_digest
            ):
                raise ValueError(
                    "--apply requires --expected-graph-checksum, "
                    "--expected-run-version, --actor, --authority, "
                    "--transaction-timestamp, "
                    "--expected-lifecycle-base-digest, and "
                    "--expected-lifecycle-plan-digest"
                )
            result = apply_mutation(
                mutation,
                args.graph,
                args.run_dir,
                expected_graph_checksum=args.expected_graph_checksum,
                expected_run_version=args.expected_run_version,
                actor=args.actor,
                authority=args.authority,
                transaction_timestamp=args.transaction_timestamp,
                expected_lifecycle_base_digest=(
                    args.expected_lifecycle_base_digest
                ),
                expected_lifecycle_plan_digest=(
                    args.expected_lifecycle_plan_digest
                ),
            )
            serializable = {key: str(value) if isinstance(value, Path) else value for key, value in result.items() if key != "previous_graph_bytes"}
        else:
            serializable = preview_mutation(
                mutation,
                args.graph,
                args.run_dir,
                actor=args.actor,
                authority=args.authority,
                transaction_timestamp=args.transaction_timestamp,
            )
    except (
        GraphControlError,
        LifecycleError,
        ValueError,
        PermissionError,
        OSError,
    ) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 1
    print(json.dumps(serializable, indent=2, sort_keys=True))
    return 0 if serializable.get("valid", False) else 1


if __name__ == "__main__":
    sys.exit(main())
