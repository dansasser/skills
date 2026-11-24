"""Bound causal repair episodes without governing outcome completion."""

from __future__ import annotations

import copy
import re
import uuid
from typing import Any

from graph_core import sha256_json


_SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_OPERATION_ID = re.compile(
    r"^(?:mutation|promotion|invalidation):"
    r"[a-z0-9]+(?:-[a-z0-9]+)*$"
)
_GATE_FIELDS = {
    "decision",
    "run_id",
    "causal_episode_id",
    "episode_operation_digest",
    "approver",
    "evidence_digest",
    "approved_at",
}
_EPISODE_FIELDS = {
    "episode_key",
    "classification",
    "status",
    "opened_at",
    "operation_ids",
    "acceptance_ids",
    "progress_node_ids",
    "seen_acceptance_witness_digests",
    "last_acceptance_digest",
    "last_progress_event_sequence",
    "progress_count",
    "repair_cycles",
    "no_progress_cycles",
    "causal_evidence_digest",
    "last_decision",
}
_CLASSIFICATIONS = {
    "repair-mutation",
    "existing-requirement",
    "current-regression",
}
_DECISIONS = {
    "authorize-next-cycle",
    "approve-independent-episode",
}


def _is_checksum(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _nonempty_text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _string_list_errors(
    value: Any,
    label: str,
    *,
    pattern: re.Pattern[str] | None = None,
    checksums: bool = False,
    require_one: bool = False,
) -> list[str]:
    if not isinstance(value, list):
        return [f"{label} must be an array"]
    errors: list[str] = []
    if require_one and not value:
        errors.append(f"{label} must not be empty")
    if all(isinstance(item, str) for item in value):
        if len(value) != len(set(value)):
            errors.append(f"{label} must contain unique values")
    for item in value:
        valid = _is_checksum(item) if checksums else _nonempty_text(item)
        if pattern is not None:
            valid = isinstance(item, str) and bool(pattern.fullmatch(item))
        if not valid:
            errors.append(f"{label} contains an invalid value")
            break
    return errors


def episode_gate_document_errors(gate: Any) -> list[str]:
    if gate is None:
        return []
    if not isinstance(gate, dict):
        return ["episode gate must be an object or null"]
    errors = [
        *(
            f"episode gate missing field: {field}"
            for field in sorted(_GATE_FIELDS - set(gate))
        ),
        *(
            f"episode gate unknown field: {field}"
            for field in sorted(set(gate) - _GATE_FIELDS)
        ),
    ]
    if gate.get("decision") not in _DECISIONS:
        errors.append("episode gate decision is invalid")
    try:
        uuid.UUID(gate.get("run_id"))
    except (AttributeError, TypeError, ValueError):
        errors.append("episode gate run_id must be a UUID")
    episode_id = gate.get("causal_episode_id")
    if not isinstance(episode_id, str) or not _SLUG.fullmatch(episode_id):
        errors.append("episode gate causal_episode_id must be a lowercase slug")
    for field in ("episode_operation_digest", "evidence_digest"):
        if not _is_checksum(gate.get(field)):
            errors.append(f"episode gate {field} must be a SHA-256 digest")
    for field in ("approver", "approved_at"):
        if not _nonempty_text(gate.get(field)):
            errors.append(f"episode gate {field} must be non-empty")
    return errors


def runtime_episodes_errors(
    episodes: Any,
    *,
    run_id: str | None = None,
    last_event_sequence: int | None = None,
) -> list[str]:
    if not isinstance(episodes, dict):
        return ["runtime episodes must be an object"]
    errors: list[str] = []
    for episode_id, episode in episodes.items():
        label = f"runtime episode {episode_id}"
        if not isinstance(episode_id, str) or not _SLUG.fullmatch(episode_id):
            errors.append("runtime episode IDs must be lowercase slugs")
            continue
        if not isinstance(episode, dict):
            errors.append(f"{label} must be an object")
            continue
        missing = sorted(_EPISODE_FIELDS - set(episode))
        unknown = sorted(set(episode) - _EPISODE_FIELDS)
        errors.extend(f"{label} missing field: {field}" for field in missing)
        errors.extend(f"{label} unknown field: {field}" for field in unknown)
        if missing or unknown:
            continue
        for field in (
            "episode_key",
            "last_acceptance_digest",
            "causal_evidence_digest",
        ):
            if not _is_checksum(episode.get(field)):
                errors.append(f"{label} {field} must be a SHA-256 digest")
        if episode.get("classification") not in _CLASSIFICATIONS:
            errors.append(f"{label} classification is invalid")
        if episode.get("status") not in {"active", "needs_human", "closed"}:
            errors.append(f"{label} status is invalid")
        if not _nonempty_text(episode.get("opened_at")):
            errors.append(f"{label} opened_at must be non-empty")
        errors.extend(
            _string_list_errors(
                episode.get("operation_ids"),
                f"{label} operation_ids",
                pattern=_OPERATION_ID,
            )
        )
        errors.extend(
            _string_list_errors(
                episode.get("acceptance_ids"),
                f"{label} acceptance_ids",
                require_one=True,
            )
        )
        errors.extend(
            _string_list_errors(
                episode.get("progress_node_ids"),
                f"{label} progress_node_ids",
                pattern=_SLUG,
            )
        )
        errors.extend(
            _string_list_errors(
                episode.get("seen_acceptance_witness_digests"),
                f"{label} seen_acceptance_witness_digests",
                checksums=True,
            )
        )
        for field in (
            "last_progress_event_sequence",
            "progress_count",
            "repair_cycles",
            "no_progress_cycles",
        ):
            item = episode.get(field)
            if not isinstance(item, int) or isinstance(item, bool) or item < 0:
                errors.append(f"{label} {field} must be a non-negative integer")
        sequence = episode.get("last_progress_event_sequence")
        if (
            isinstance(sequence, int)
            and isinstance(last_event_sequence, int)
            and sequence > last_event_sequence
        ):
            errors.append(f"{label} progress sequence exceeds the ledger")
        decision = episode.get("last_decision")
        decision_errors = episode_gate_document_errors(decision)
        errors.extend(f"{label}: {item}" for item in decision_errors)
        if isinstance(decision, dict):
            if decision.get("causal_episode_id") != episode_id:
                errors.append(f"{label} decision targets another episode")
            if run_id is not None and decision.get("run_id") != run_id:
                errors.append(f"{label} decision targets another run")
    return errors


def _acceptance_witnesses(
    run: dict[str, Any],
    projection: dict[str, Any],
    acceptance_ids: list[str],
    progress_node_ids: list[str],
) -> list[dict[str, Any]]:
    mappings = projection.get("acceptance_mappings", {})
    progress_nodes = set(progress_node_ids)
    witnesses: list[dict[str, Any]] = []
    for acceptance_id in sorted(acceptance_ids):
        for node_id in sorted(
            set(mappings.get(acceptance_id, [])) & progress_nodes
        ):
            state = run.get("nodes", {}).get(node_id)
            if (
                not isinstance(state, dict)
                or state.get("state") != "verified"
                or not isinstance(state.get("last_result"), dict)
            ):
                continue
            witness = {
                "acceptance_id": acceptance_id,
                "node_id": node_id,
                "generation": state.get("generation"),
                "evidence_digest": sha256_json(state["last_result"]),
            }
            witness["witness_digest"] = sha256_json(witness)
            witnesses.append(witness)
    return witnesses


def preview_repair_cycle(
    run: dict[str, Any],
    projection: dict[str, Any],
    *,
    operation_kind: str,
    operation_id: str,
    causal_episode_id: str,
    classification: str,
    evidence: list[str],
    operation_commitment: dict[str, Any],
    episode_gate: Any,
    progress_node_ids: list[str],
    actor: str,
    authority: str,
    acceptance_ids: list[str] | None = None,
) -> dict[str, Any]:
    errors = runtime_episodes_errors(
        run.get("episodes"),
        run_id=run.get("run_id"),
        last_event_sequence=run.get("last_event_sequence"),
    )
    if operation_kind not in {"mutation", "promotion", "invalidation"}:
        errors.append("repair episode operation kind is invalid")
    if not isinstance(operation_id, str) or not _SLUG.fullmatch(operation_id):
        errors.append("repair episode operation ID must be a lowercase slug")
    if (
        not isinstance(causal_episode_id, str)
        or not _SLUG.fullmatch(causal_episode_id)
    ):
        errors.append("causal episode ID must be a lowercase slug")
    if classification not in _CLASSIFICATIONS:
        errors.append("repair episode classification is invalid")
    if (
        not isinstance(evidence, list)
        or not evidence
        or any(not _nonempty_text(item) for item in evidence)
    ):
        errors.append("repair episode requires non-empty causal evidence")
    if not isinstance(operation_commitment, dict):
        errors.append("repair episode operation commitment must be an object")
    errors.extend(
        _string_list_errors(
            progress_node_ids,
            "repair episode progress_node_ids",
            pattern=_SLUG,
        )
    )
    if not _nonempty_text(actor):
        errors.append("repair episode actor must be non-empty")
    if not _nonempty_text(authority):
        errors.append("repair episode authority must be non-empty")

    normalized_evidence = sorted(set(evidence)) if isinstance(evidence, list) else []
    progress_scope = sorted(
        set(
            item
            for item in progress_node_ids
            if isinstance(progress_node_ids, list) and isinstance(item, str)
        )
    )
    available_node_ids = (
        set(run.get("nodes", {}))
        if isinstance(run.get("nodes"), dict)
        else set()
    )
    projection_nodes = projection.get("nodes", [])
    if isinstance(projection_nodes, list):
        available_node_ids.update(
            item["id"]
            for item in projection_nodes
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        )
    unknown_progress = sorted(set(progress_scope) - available_node_ids)
    if unknown_progress:
        errors.append(
            "repair episode progress nodes are outside the execution projection: "
            + ", ".join(unknown_progress)
        )
    scope = sorted(
        set(
            acceptance_ids
            if acceptance_ids
            else run.get("outcome_contract", {}).get("acceptance_ids", [])
        )
    )
    if not scope or any(not _nonempty_text(item) for item in scope):
        errors.append("repair episode requires contracted acceptance scope")
    causal_evidence_digest = sha256_json(normalized_evidence)
    episode_key = sha256_json(
        {
            "outcome_contract_digest": sha256_json(
                run.get("outcome_contract")
            ),
            "classification": classification,
            "acceptance_ids": scope,
            "progress_node_ids": progress_scope,
            "causal_evidence_digest": causal_evidence_digest,
        }
    )
    qualified_operation_id = f"{operation_kind}:{operation_id}"
    operation_digest = sha256_json(
        {
            "run_id": run.get("run_id"),
            "run_version": run.get("run_version"),
            "observed_graph_checksum": run.get("observed_graph_checksum"),
            "projection_checksum": run.get("projection", {}).get("checksum"),
            "causal_episode_id": causal_episode_id,
            "episode_key": episode_key,
            "causal_evidence_digest": causal_evidence_digest,
            "progress_node_ids": progress_scope,
            "actor": actor,
            "authority": authority,
            "operation_id": qualified_operation_id,
            "operation_commitment": copy.deepcopy(operation_commitment),
        }
    )
    witnesses = _acceptance_witnesses(
        run, projection, scope, progress_scope
    )
    witness_digests = [item["witness_digest"] for item in witnesses]
    acceptance_digest = sha256_json(witnesses)

    episodes = run.get("episodes")
    episodes = episodes if isinstance(episodes, dict) else {}
    existing = episodes.get(causal_episode_id)
    for other_id, other in episodes.items():
        if (
            isinstance(other, dict)
            and qualified_operation_id in other.get("operation_ids", [])
        ):
            errors.append(
                f"repair operation already belongs to causal episode: {other_id}"
            )
    matching_ids = sorted(
        episode_id
        for episode_id, episode in episodes.items()
        if isinstance(episode, dict)
        and episode.get("episode_key") == episode_key
        and episode_id != causal_episode_id
    )
    gate_required: str | None = None
    if isinstance(existing, dict):
        if existing.get("episode_key") != episode_key:
            errors.append(
                "causal episode ID cannot be rebound to different evidence or scope"
            )
        if existing.get("classification") != classification:
            errors.append("causal episode classification cannot change")
        if existing.get("acceptance_ids") != scope:
            errors.append("causal episode acceptance scope cannot change")
        if existing.get("progress_node_ids") != progress_scope:
            errors.append("causal episode progress scope cannot change")
        if existing.get("status") == "closed":
            errors.append("closed causal episode cannot accept another repair cycle")
        elif existing.get("status") == "needs_human":
            gate_required = "authorize-next-cycle"
    else:
        if matching_ids:
            errors.append(
                "equivalent causal episode already exists: "
                + ", ".join(matching_ids)
            )
        elif any(
            isinstance(episode, dict)
            and episode.get("status") != "closed"
            for episode in episodes.values()
        ):
            gate_required = "approve-independent-episode"

    expected_gate_evidence_digest = sha256_json(
        {
            "kind": "causal-episode-gate",
            "run_id": run.get("run_id"),
            "causal_episode_id": causal_episode_id,
            "decision": gate_required,
            "episode_operation_digest": operation_digest,
            "actor": actor,
            "authority": authority,
            "approver": authority,
        }
    )
    gate_errors = episode_gate_document_errors(episode_gate)
    if gate_required is None:
        if episode_gate is not None and not gate_errors:
            gate_errors.append("episode gate is not required for this cycle")
    elif episode_gate is None:
        gate_errors.append(
            f"episode gate decision {gate_required} is required"
        )
    elif isinstance(episode_gate, dict) and not gate_errors:
        if episode_gate.get("decision") != gate_required:
            gate_errors.append(
                f"episode gate decision must be {gate_required}"
            )
        if episode_gate.get("run_id") != run.get("run_id"):
            gate_errors.append("episode gate run ID does not match runtime")
        if episode_gate.get("causal_episode_id") != causal_episode_id:
            gate_errors.append("episode gate causal episode does not match")
        if episode_gate.get("episode_operation_digest") != operation_digest:
            gate_errors.append(
                "episode gate operation digest does not match preview"
            )
        if episode_gate.get("approver") != authority:
            gate_errors.append(
                "episode gate approver does not match operation authority"
            )
        if (
            episode_gate.get("evidence_digest")
            != expected_gate_evidence_digest
        ):
            gate_errors.append(
                "episode gate evidence digest does not match preview"
            )

    return {
        "valid": not errors,
        "apply_allowed": not errors and not gate_errors,
        "errors": errors,
        "gate_errors": gate_errors,
        "gate_required": gate_required,
        "causal_episode_id": causal_episode_id,
        "episode_key": episode_key,
        "classification": classification,
        "qualified_operation_id": qualified_operation_id,
        "episode_operation_digest": operation_digest,
        "causal_evidence_digest": causal_evidence_digest,
        "acceptance_ids": scope,
        "progress_node_ids": progress_scope,
        "expected_gate_evidence_digest": expected_gate_evidence_digest,
        "acceptance_witnesses": witnesses,
        "acceptance_witness_digests": witness_digests,
        "acceptance_digest": acceptance_digest,
        "episode_gate": copy.deepcopy(episode_gate),
    }


def apply_repair_cycle(
    run: dict[str, Any],
    preview: dict[str, Any],
    *,
    timestamp: str,
) -> dict[str, Any]:
    if not preview.get("valid") or not preview.get("apply_allowed"):
        raise ValueError("repair episode cycle is not applyable")
    episode_id = preview["causal_episode_id"]
    episode = run["episodes"].get(episode_id)
    if episode is None:
        episode = {
            "episode_key": preview["episode_key"],
            "classification": preview["classification"],
            "status": "active",
            "opened_at": timestamp,
            "operation_ids": [],
            "acceptance_ids": list(preview["acceptance_ids"]),
            "progress_node_ids": list(preview["progress_node_ids"]),
            "seen_acceptance_witness_digests": list(
                preview["acceptance_witness_digests"]
            ),
            "last_acceptance_digest": preview["acceptance_digest"],
            "last_progress_event_sequence": 0,
            "progress_count": 0,
            "repair_cycles": 0,
            "no_progress_cycles": 0,
            "causal_evidence_digest": preview["causal_evidence_digest"],
            "last_decision": None,
        }
        run["episodes"][episode_id] = episode
    operation_id = preview["qualified_operation_id"]
    if operation_id in episode["operation_ids"]:
        raise ValueError("repair operation was already consumed")
    episode["operation_ids"].append(operation_id)
    episode["repair_cycles"] += 1
    episode["no_progress_cycles"] += 1
    episode["status"] = "needs_human"
    episode["last_acceptance_digest"] = preview["acceptance_digest"]
    episode["last_decision"] = copy.deepcopy(preview["episode_gate"])
    return {
        "causal_episode_id": episode_id,
        "episode_key": episode["episode_key"],
        "episode_operation_id": operation_id,
        "episode_operation_digest": preview["episode_operation_digest"],
        "repair_cycles": episode["repair_cycles"],
        "no_progress_cycles": episode["no_progress_cycles"],
        "episode_status": episode["status"],
        "episode_gate": copy.deepcopy(preview["episode_gate"]),
        "episode_state_digest": sha256_json(episode),
    }


def record_acceptance_progress(
    run: dict[str, Any],
    projection: dict[str, Any],
    *,
    event_sequence: int,
) -> list[dict[str, Any]]:
    updates: list[dict[str, Any]] = []
    episodes = run.get("episodes")
    if not isinstance(episodes, dict):
        return updates
    for episode_id in sorted(episodes):
        episode = episodes[episode_id]
        if episode.get("status") == "closed":
            continue
        witnesses = _acceptance_witnesses(
            run,
            projection,
            list(episode["acceptance_ids"]),
            list(episode["progress_node_ids"]),
        )
        witness_digests = [item["witness_digest"] for item in witnesses]
        seen = set(episode["seen_acceptance_witness_digests"])
        fresh = [item for item in witness_digests if item not in seen]
        if not fresh:
            continue
        episode["seen_acceptance_witness_digests"].extend(fresh)
        episode["last_acceptance_digest"] = sha256_json(witnesses)
        episode["last_progress_event_sequence"] = event_sequence
        episode["progress_count"] += len(fresh)
        episode["no_progress_cycles"] = 0
        episode["status"] = "active"
        updates.append(
            {
                "causal_episode_id": episode_id,
                "acceptance_witness_digests": fresh,
                "last_acceptance_digest": episode[
                    "last_acceptance_digest"
                ],
                "progress_count": episode["progress_count"],
                "episode_status": episode["status"],
                "episode_state_digest": sha256_json(episode),
            }
        )
    return updates


def episode_ledger_errors(
    records: list[dict[str, Any]],
    episodes: Any,
) -> list[str]:
    errors: list[str] = []
    cursors: dict[str, str] = {}
    for record in records:
        details = record.get("details")
        if not isinstance(details, dict):
            continue
        episode_id = details.get("causal_episode_id")
        state_digest = details.get("episode_state_digest")
        if state_digest is not None:
            if (
                not isinstance(episode_id, str)
                or not _SLUG.fullmatch(episode_id)
                or not _is_checksum(state_digest)
            ):
                errors.append(
                    f"event {record.get('sequence', '?')} has invalid episode state binding"
                )
            else:
                cursors[episode_id] = state_digest
        progress = details.get("episode_progress", [])
        if not isinstance(progress, list):
            errors.append(
                f"event {record.get('sequence', '?')} episode progress is invalid"
            )
            continue
        for item in progress:
            if not isinstance(item, dict):
                errors.append(
                    f"event {record.get('sequence', '?')} episode progress item is invalid"
                )
                continue
            progress_id = item.get("causal_episode_id")
            progress_digest = item.get("episode_state_digest")
            if (
                not isinstance(progress_id, str)
                or not _SLUG.fullmatch(progress_id)
                or not _is_checksum(progress_digest)
            ):
                errors.append(
                    f"event {record.get('sequence', '?')} has invalid episode progress binding"
                )
            else:
                cursors[progress_id] = progress_digest
    if not isinstance(episodes, dict):
        return errors
    for episode_id, episode in episodes.items():
        expected = sha256_json(episode)
        observed = cursors.get(episode_id)
        if observed is None:
            errors.append(
                f"runtime episode is not sealed by the event ledger: {episode_id}"
            )
        elif observed != expected:
            errors.append(
                f"runtime episode digest does not match the event ledger: {episode_id}"
            )
    for episode_id in sorted(set(cursors) - set(episodes)):
        errors.append(
            f"event ledger references missing runtime episode: {episode_id}"
        )
    return errors
