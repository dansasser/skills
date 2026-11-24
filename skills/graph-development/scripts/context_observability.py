"""Provider-neutral, payload-free explanations for V3/V4 context decisions."""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Any, Protocol

from context_core import (
    OPTIONAL_OMISSION_REASONS,
    SECRET_MARKER,
    SHA256,
    delta_affected_nodes,
)


REPORT_FIELDS = {
    "schema_version",
    "report_type",
    "status",
    "subject",
    "reasons",
    "items",
    "budget",
    "affected_nodes",
    "unaffected_nodes",
    "changed_artifacts",
    "digests",
}
REPORT_TYPES = {"plan", "envelope", "replay", "cache", "invalidation"}
REPORT_STATUSES = {
    "planned",
    "blocked",
    "mandatory-overflow",
    "sealed",
    "hit",
    "miss",
    "corrupt",
    "affected",
    "unchanged",
}
REASON_CODES = {
    "mandatory-required",
    "mandatory-overflow",
    "mandatory-blocked-unauthorized",
    "mandatory-blocked-stale",
    "mandatory-blocked-provenance-incomplete",
    "mandatory-blocked-conflict",
    "mandatory-blocked-unavailable",
    "plan-blocked",
    "optional-selected",
    "optional-blocked-unauthorized",
    "optional-blocked-stale",
    "optional-blocked-provenance-incomplete",
    "optional-blocked-conflict",
    "optional-omitted-no-coverage",
    "optional-omitted-no-positive-coverage",
    "optional-omitted-redundant",
    "optional-omitted-budget",
    "optional-omitted-not-selected",
    "optional-omitted-serialization-overflow",
    "envelope-sealed",
    "replay-accepted-match",
    "replay-no-accepted-match",
    "cache-exact-key-hit",
    "cache-exact-key-miss",
    "cache-corrupt-record",
    "invalidation-artifact-changed",
    "invalidation-node-affected",
    "invalidation-node-preserved",
}
_BLOCKING_REASONS = {
    "unauthorized",
    "stale",
    "provenance-incomplete",
    "conflict",
    "unavailable",
}
_FORBIDDEN_KEYS = {
    "arguments",
    "content",
    "credentials",
    "payload",
    "raw_content",
    "raw_payload",
    "result",
    "result_payload",
}
_DRIVE_ROOT = re.compile(r"^[A-Za-z]:")


class ReplayStore(Protocol):
    def find_replay(
        self,
        *,
        run_id: str,
        node_id: str,
        envelope_digest: str,
    ) -> dict[str, Any] | None:
        """Return an accepted matching result or None."""


def _reason(
    code: str,
    *,
    artifact_id: str | None = None,
    token_count: int | None = None,
) -> dict[str, Any]:
    return {
        "code": code,
        "artifact_id": artifact_id,
        "token_count": token_count,
    }


def _item(
    artifact_id: str,
    classification: str,
    reason_code: str,
    *,
    token_count: int | None = None,
    digest: str | None = None,
    source: str | None = None,
) -> dict[str, Any]:
    return {
        "artifact_id": artifact_id,
        "classification": classification,
        "reason_code": reason_code,
        "token_count": token_count,
        "digest": digest,
        "source": source,
    }


def _report(
    report_type: str,
    status: str,
    *,
    subject: dict[str, Any] | None = None,
    reasons: list[dict[str, Any]] | None = None,
    items: list[dict[str, Any]] | None = None,
    budget: dict[str, int] | None = None,
    affected_nodes: list[str] | None = None,
    unaffected_nodes: list[str] | None = None,
    changed_artifacts: list[str] | None = None,
    digests: dict[str, str] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "report_type": report_type,
        "status": status,
        "subject": subject or {},
        "reasons": reasons or [],
        "items": items or [],
        "budget": budget or {},
        "affected_nodes": affected_nodes or [],
        "unaffected_nodes": unaffected_nodes or [],
        "changed_artifacts": changed_artifacts or [],
        "digests": digests or {},
    }


def _omission_reason(reason: object, *, mandatory: bool = False) -> str:
    normalized = reason if isinstance(reason, str) else "unavailable"
    if mandatory:
        if normalized not in _BLOCKING_REASONS:
            return "plan-blocked"
        return f"mandatory-blocked-{normalized}"
    if normalized in _BLOCKING_REASONS:
        return f"optional-blocked-{normalized}"
    known = OPTIONAL_OMISSION_REASONS - _BLOCKING_REASONS
    return (
        f"optional-omitted-{normalized}"
        if normalized in known
        else "optional-omitted-not-selected"
    )


def explain_plan(planned: dict[str, Any]) -> dict[str, Any]:
    """Explain a planning result without retaining any model-facing content."""

    status = planned.get("status")
    if status == "mandatory_overflow":
        result = planned.get("result", {})
        result = result if isinstance(result, dict) else {}
        report = _report(
            "plan",
            "mandatory-overflow",
            subject={
                "task_node_id": result.get("task_node_id"),
                "envelope_id": result.get("envelope_id"),
            },
            reasons=[_reason("mandatory-overflow")],
            budget={
                "mandatory_tokens": int(planned.get("mandatory_tokens", 0)),
                "available_context": int(planned.get("available_context", 0)),
            },
            digests={
                "result": result["digest"]
                for _ in (0,)
                if isinstance(result.get("digest"), str)
            },
        )
        return report
    if status == "blocked":
        blocked = planned.get("blocked", [])
        blocked = blocked if isinstance(blocked, list) else []
        items = []
        reasons = []
        for raw in blocked:
            if not isinstance(raw, dict):
                continue
            artifact_id = raw.get("artifact_id")
            if not isinstance(artifact_id, str) or not artifact_id:
                continue
            code = _omission_reason(raw.get("reason"), mandatory=True)
            items.append(_item(artifact_id, "blocked", code))
            reasons.append(_reason(code, artifact_id=artifact_id))
        if not reasons:
            reasons.append(_reason("plan-blocked"))
        return _report(
            "plan",
            "blocked",
            subject={"task_node_id": planned.get("task_node_id")},
            reasons=reasons,
            items=items,
        )
    if status != "planned":
        raise ValueError("context plan status must be planned, blocked, or mandatory_overflow")
    envelope = planned.get("envelope")
    if not isinstance(envelope, dict):
        raise ValueError("planned context result must contain an envelope")
    items: list[dict[str, Any]] = []
    reasons: list[dict[str, Any]] = []
    manifest = envelope.get("manifest", [])
    if isinstance(manifest, list):
        for raw in manifest:
            if not isinstance(raw, dict):
                continue
            artifact_id = raw.get("artifact_id")
            if not isinstance(artifact_id, str):
                continue
            classification = raw.get("classification")
            code = (
                "mandatory-required"
                if classification == "mandatory"
                else "optional-selected"
            )
            items.append(
                _item(
                    artifact_id,
                    str(classification),
                    code,
                    token_count=raw.get("token_count")
                    if isinstance(raw.get("token_count"), int)
                    else None,
                    digest=raw.get("sha256")
                    if isinstance(raw.get("sha256"), str)
                    else None,
                    source=raw.get("source")
                    if isinstance(raw.get("source"), str)
                    else None,
                )
            )
            reasons.append(
                _reason(
                    code,
                    artifact_id=artifact_id,
                    token_count=raw.get("token_count")
                    if isinstance(raw.get("token_count"), int)
                    else None,
                )
            )
    selection = envelope.get("selection", {})
    omitted = selection.get("omitted", []) if isinstance(selection, dict) else []
    if isinstance(omitted, list):
        for raw in omitted:
            if not isinstance(raw, dict):
                continue
            artifact_id = raw.get("artifact_id")
            if not isinstance(artifact_id, str):
                continue
            code = _omission_reason(raw.get("reason"))
            classification = "blocked" if "blocked" in code else "omitted"
            tokens = (
                raw.get("token_count")
                if isinstance(raw.get("token_count"), int)
                else None
            )
            items.append(
                _item(
                    artifact_id,
                    classification,
                    code,
                    token_count=tokens,
                )
            )
            reasons.append(
                _reason(code, artifact_id=artifact_id, token_count=tokens)
            )
    budget_value = envelope.get("budget", {})
    budget = {
        key: value
        for key, value in budget_value.items()
        if isinstance(key, str)
        and isinstance(value, int)
        and not isinstance(value, bool)
    } if isinstance(budget_value, dict) else {}
    return _report(
        "plan",
        "planned",
        subject={
            "task_node_id": envelope.get("task_node_id"),
            "envelope_id": envelope.get("envelope_id"),
        },
        reasons=reasons,
        items=sorted(items, key=lambda item: item["artifact_id"]),
        budget=budget,
        digests={
            "envelope": envelope["digest"]
            for _ in (0,)
            if isinstance(envelope.get("digest"), str)
        },
    )


def explain_envelope_record(record: dict[str, Any]) -> dict[str, Any]:
    """Explain a full or digest-first envelope record without exposing payload."""

    envelope_digest = record.get("envelope_digest", record.get("digest"))
    items: list[dict[str, Any]] = []
    manifest = record.get("manifest", [])
    if isinstance(manifest, list):
        for raw in manifest:
            if not isinstance(raw, dict):
                continue
            artifact_id = raw.get("artifact_id")
            if not isinstance(artifact_id, str):
                continue
            classification = raw.get("classification")
            code = (
                "mandatory-required"
                if classification == "mandatory"
                else "optional-selected"
            )
            items.append(
                _item(
                    artifact_id,
                    str(classification),
                    code,
                    token_count=raw.get("token_count")
                    if isinstance(raw.get("token_count"), int)
                    else None,
                    digest=raw.get("sha256")
                    if isinstance(raw.get("sha256"), str)
                    else None,
                    source=raw.get("source")
                    if isinstance(raw.get("source"), str)
                    else None,
                )
            )
    digests = {}
    for name, value in (
        ("envelope", envelope_digest),
        ("payload", record.get("payload_digest")),
    ):
        if isinstance(value, str):
            digests[name] = value
    graph = record.get("graph")
    if isinstance(graph, dict) and isinstance(graph.get("canonical_sha256"), str):
        digests["graph"] = graph["canonical_sha256"]
    budget_value = record.get("budget")
    budget = {
        key: value
        for key, value in budget_value.items()
        if isinstance(key, str)
        and isinstance(value, int)
        and not isinstance(value, bool)
    } if isinstance(budget_value, dict) else {}
    return _report(
        "envelope",
        "sealed",
        subject={
            "task_node_id": record.get("task_node_id"),
            "envelope_id": record.get("envelope_id"),
            "envelope_digest": envelope_digest,
        },
        reasons=[_reason("envelope-sealed")],
        items=items,
        budget=budget,
        digests=digests,
    )


def inspect_replay(
    store: ReplayStore,
    *,
    run_id: str,
    node_id: str,
    envelope_digest: str,
) -> dict[str, Any]:
    """Explain same-run replay eligibility without returning the stored result."""

    result = store.find_replay(
        run_id=run_id,
        node_id=node_id,
        envelope_digest=envelope_digest,
    )
    hit = isinstance(result, dict)
    digests = {"envelope": envelope_digest}
    if hit and isinstance(result.get("digest"), str):
        digests["result"] = result["digest"]
    return _report(
        "replay",
        "hit" if hit else "miss",
        subject={
            "run_id": run_id,
            "task_node_id": node_id,
            "envelope_digest": envelope_digest,
        },
        reasons=[
            _reason(
                "replay-accepted-match"
                if hit
                else "replay-no-accepted-match"
            )
        ],
        digests=digests,
    )


def explain_cache(inspection: dict[str, Any]) -> dict[str, Any]:
    """Explain an exact-key cache inspection without returning result content."""

    hit = inspection.get("hit") is True
    raw_reason = inspection.get("reason")
    if hit:
        status = "hit"
        code = "cache-exact-key-hit"
    elif raw_reason == "corrupt-cache-record":
        status = "corrupt"
        code = "cache-corrupt-record"
    else:
        status = "miss"
        code = "cache-exact-key-miss"
    digests = {}
    result = inspection.get("result")
    if isinstance(result, dict) and isinstance(result.get("digest"), str):
        digests["result"] = result["digest"]
    result_key = inspection.get("result_key")
    if isinstance(result_key, str):
        digests["result_key"] = result_key
    return _report(
        "cache",
        status,
        subject={"result_key": result_key},
        reasons=[_reason(code)],
        digests=digests,
    )


def explain_invalidation(
    graph: dict[str, Any],
    changed_artifacts: set[str],
) -> dict[str, Any]:
    """Explain deterministic forward invalidation from artifact identities."""

    if not isinstance(changed_artifacts, set) or not all(
        isinstance(item, str) and item for item in changed_artifacts
    ):
        raise ValueError("changed_artifacts must be a set of non-empty identifiers")
    affected = delta_affected_nodes(graph, changed_artifacts)
    node_ids = [
        node.get("id")
        for node in graph.get("nodes", [])
        if isinstance(node, dict) and isinstance(node.get("id"), str)
    ]
    affected_set = set(affected)
    unaffected = [node_id for node_id in node_ids if node_id not in affected_set]
    reasons = [
        _reason("invalidation-artifact-changed", artifact_id=artifact)
        for artifact in sorted(changed_artifacts)
    ]
    reasons.extend(
        _reason("invalidation-node-affected", artifact_id=node_id)
        for node_id in affected
    )
    reasons.extend(
        _reason("invalidation-node-preserved", artifact_id=node_id)
        for node_id in unaffected
    )
    return _report(
        "invalidation",
        "affected" if affected else "unchanged",
        subject={
            "graph_id": graph.get("graph_id"),
            "graph_version": graph.get("graph_version"),
        },
        reasons=reasons,
        affected_nodes=affected,
        unaffected_nodes=unaffected,
        changed_artifacts=sorted(changed_artifacts),
    )


def _walk(value: Any, path: str, errors: list[str]) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key in _FORBIDDEN_KEYS and not (
                path == "$.digests" and key in {"payload", "result"}
            ):
                errors.append(f"{path}.{key}: is not allowed in an explanation")
            _walk(item, f"{path}.{key}", errors)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _walk(item, f"{path}[{index}]", errors)
    elif isinstance(value, str) and SECRET_MARKER.search(value):
        errors.append(f"{path}: contains a credential-like secret marker")


def _portable_source(value: str, path: str, errors: list[str]) -> None:
    pure = PurePosixPath(value)
    if (
        pure.is_absolute()
        or value.startswith("~")
        or _DRIVE_ROOT.match(value)
        or "\\" in value
        or ".." in pure.parts
    ):
        errors.append(f"{path}: must be a portable project-relative source")


def validate_context_explanation(value: Any) -> list[str]:
    """Validate the bounded report shape and secret/path safety invariants."""

    errors: list[str] = []
    if not isinstance(value, dict):
        return ["$: explanation must be an object"]
    for field in sorted(REPORT_FIELDS - set(value)):
        errors.append(f"$.{field}: is required")
    for field in sorted(set(value) - REPORT_FIELDS):
        errors.append(f"$.{field}: is not allowed")
    if value.get("schema_version") != 1:
        errors.append("$.schema_version: must equal 1")
    if value.get("report_type") not in REPORT_TYPES:
        errors.append("$.report_type: unsupported report type")
    if value.get("status") not in REPORT_STATUSES:
        errors.append("$.status: unsupported report status")
    reasons = value.get("reasons")
    if not isinstance(reasons, list):
        errors.append("$.reasons: must be an array")
    else:
        for index, reason in enumerate(reasons):
            path = f"$.reasons[{index}]"
            if not isinstance(reason, dict) or set(reason) != {
                "code",
                "artifact_id",
                "token_count",
            }:
                errors.append(f"{path}: must contain code, artifact_id, and token_count")
                continue
            if reason.get("code") not in REASON_CODES:
                errors.append(f"{path}.code: unsupported reason code")
    items = value.get("items")
    if not isinstance(items, list):
        errors.append("$.items: must be an array")
    else:
        for index, item in enumerate(items):
            path = f"$.items[{index}]"
            if not isinstance(item, dict) or set(item) != {
                "artifact_id",
                "classification",
                "reason_code",
                "token_count",
                "digest",
                "source",
            }:
                errors.append(f"{path}: has invalid item fields")
                continue
            if item.get("reason_code") not in REASON_CODES:
                errors.append(f"{path}.reason_code: unsupported reason code")
            digest = item.get("digest")
            if digest is not None and (
                not isinstance(digest, str) or not SHA256.fullmatch(digest)
            ):
                errors.append(f"{path}.digest: must be a SHA-256 digest or null")
            source = item.get("source")
            if isinstance(source, str):
                _portable_source(source, f"{path}.source", errors)
            elif source is not None:
                errors.append(f"{path}.source: must be a string or null")
    digests = value.get("digests")
    if not isinstance(digests, dict):
        errors.append("$.digests: must be an object")
    else:
        for name, digest in digests.items():
            if not isinstance(name, str) or not isinstance(digest, str) or not SHA256.fullmatch(digest):
                errors.append(f"$.digests.{name}: must be a SHA-256 digest")
    for field in (
        "subject",
        "budget",
        "affected_nodes",
        "unaffected_nodes",
        "changed_artifacts",
    ):
        expected = dict if field in {"subject", "budget"} else list
        if not isinstance(value.get(field), expected):
            errors.append(f"$.{field}: must be a {'object' if expected is dict else 'array'}")
    _walk(value, "$", errors)
    return errors
