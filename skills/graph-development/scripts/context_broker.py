"""Typed context and action request broker for Graph Development V3."""

from __future__ import annotations

import copy
import hashlib
from typing import Any

from context_core import (
    ContextControlError,
    Tokenizer,
    canonical_json_bytes,
    extend_envelope,
    seal_document,
    validate_envelope,
    validate_request,
)


GENERIC_CAPABILITIES = {
    "filesystem",
    "read-file",
    "write-file",
    "shell",
    "web",
    "graph",
    "graph-lineage",
    "transcript",
    "retrieval",
    "knowledge-store",
}


class ContextBroker:
    """Mediates bounded requests without exposing raw project or tool data."""

    def __init__(
        self,
        *,
        artifact_registry: dict[str, dict[str, Any]],
        capability_registry: dict[str, dict[str, Any]],
        tokenizer: Tokenizer,
        read_scope: set[str],
        external_scope: set[str],
        max_requests: int,
        approval: dict[str, Any] | None = None,
        seen_request_digests: set[str] | None = None,
    ) -> None:
        if max_requests < 0:
            raise ValueError("max_requests cannot be negative")
        self._artifacts = artifact_registry
        self._capabilities = capability_registry
        self._tokenizer = tokenizer
        self._read_scope = set(read_scope)
        self._external_scope = set(external_scope)
        self._max_requests = max_requests
        self._approval = copy.deepcopy(approval)
        self._seen = set(seen_request_digests or set())
        self._request_count = len(self._seen)
        self.records: list[dict[str, Any]] = []
        self._executed_actions: dict[str, dict[str, Any]] = {}

    @staticmethod
    def _base(
        envelope: dict[str, Any],
        request: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "request_id": request.get("request_id", "invalid-request"),
            "request_digest": request.get("digest", "0" * 64),
            "parent_envelope_id": envelope.get(
                "envelope_id",
                "invalid-envelope",
            ),
            "parent_envelope_digest": envelope.get("digest", "0" * 64),
            "task_node_id": envelope.get("task_node_id", "invalid-task"),
            "kind": request.get("kind", "invalid"),
        }

    def _record(
        self,
        envelope: dict[str, Any],
        request: dict[str, Any],
        decision: str,
        *,
        reason: str,
        **details: Any,
    ) -> dict[str, Any]:
        record = seal_document(
            {
                **self._base(envelope, request),
                "decision": decision,
                "reason": reason,
                "request_budget_identity": (
                    request.get("digest") in self._seen
                ),
                **details,
            }
        )
        self.records.append(copy.deepcopy(record))
        return record

    def resolve(
        self,
        envelope: dict[str, Any],
        request: dict[str, Any],
    ) -> dict[str, Any]:
        envelope_errors = validate_envelope(envelope)
        if envelope_errors:
            return self._record(
                envelope,
                request,
                "denied",
                reason="invalid parent envelope: " + "; ".join(envelope_errors),
            )
        request_errors = validate_request(request)
        if request_errors:
            return self._record(
                envelope,
                request,
                "denied",
                reason="invalid request: " + "; ".join(request_errors),
            )
        request_digest = request["digest"]
        if request_digest in self._seen:
            return self._record(
                envelope,
                request,
                "denied",
                reason="request replay is not allowed",
            )
        if self._request_count >= self._max_requests:
            return self._record(
                envelope,
                request,
                "denied",
                reason="request bound is exhausted",
            )
        self._seen.add(request_digest)
        self._request_count += 1
        if request["envelope_id"] != envelope["envelope_id"]:
            return self._record(
                envelope,
                request,
                "denied",
                reason="request envelope_id does not match parent envelope",
            )
        if request["task_node_id"] != envelope["task_node_id"]:
            return self._record(
                envelope,
                request,
                "denied",
                reason="request task_node_id does not match parent envelope",
            )
        policy = envelope["request_policy"]
        if request["kind"] not in policy["allowed_kinds"]:
            return self._record(
                envelope,
                request,
                "denied",
                reason="request kind is not declared by the envelope policy",
            )
        remaining = min(
            policy["max_additional_tokens"],
            envelope["budget"]["available_context"]
            - envelope["budget"]["used_context"],
        )
        if request["max_tokens"] > remaining:
            return self._record(
                envelope,
                request,
                "denied",
                reason="requested token bound exceeds remaining policy budget",
            )
        if request["kind"] in {"context", "artifact"}:
            return self._resolve_artifacts(envelope, request)
        return self._resolve_action(envelope, request)

    def _resolve_artifacts(
        self,
        envelope: dict[str, Any],
        request: dict[str, Any],
    ) -> dict[str, Any]:
        projections: list[dict[str, Any]] = []
        for artifact_id in request["requested_artifacts"]:
            if artifact_id not in self._read_scope:
                return self._record(
                    envelope,
                    request,
                    "denied",
                    reason=f"unauthorized artifact read: {artifact_id}",
                )
            record = self._artifacts.get(artifact_id)
            if record is None or record.get("reachable") is not True:
                return self._record(
                    envelope,
                    request,
                    "denied",
                    reason=f"unreachable artifact: {artifact_id}",
                )
            if record.get("authorized") is not True:
                return self._record(
                    envelope,
                    request,
                    "denied",
                    reason=f"unauthorized artifact: {artifact_id}",
                )
            if record.get("fresh") is not True:
                return self._record(
                    envelope,
                    request,
                    "denied",
                    reason=f"stale artifact: {artifact_id}",
                )
            if record.get("provenance_complete") is not True:
                return self._record(
                    envelope,
                    request,
                    "denied",
                    reason=f"artifact provenance is incomplete: {artifact_id}",
                )
            projection = record.get("projection")
            if not isinstance(projection, dict):
                return self._record(
                    envelope,
                    request,
                    "denied",
                    reason=f"artifact has no declared projection: {artifact_id}",
                )
            content = projection.get("content")
            source = projection.get("source")
            requirements = projection.get("requirements")
            if (
                not isinstance(content, str)
                or not isinstance(source, str)
                or not isinstance(requirements, list)
            ):
                return self._record(
                    envelope,
                    request,
                    "denied",
                    reason=f"artifact projection is malformed: {artifact_id}",
                )
            projections.append(
                {
                    "artifact_id": artifact_id,
                    "content": content,
                    "source": source,
                    "requirements": requirements,
                    "inclusion_class": "direct_input",
                    "version": record.get("version", "unversioned"),
                }
            )
        # Use the same complete serialization and budget checks as handoff,
        # before the caller can persist or consume an approved decision.
        try:
            extended = extend_envelope(
                envelope,
                projections,
                tokenizer=self._tokenizer,
                parent_request_digest=request["digest"],
            )
        except ContextControlError as exc:
            return self._record(envelope, request, "denied", reason=str(exc))
        projected_tokens = max(
            0,
            extended["budget"]["used_context"] - envelope["budget"]["used_context"],
        )
        if projected_tokens > request["max_tokens"]:
            return self._record(
                envelope,
                request,
                "denied",
                reason="projected artifact token count exceeds the request bound",
            )
        return self._record(
            envelope,
            request,
            "approved",
            reason="declared artifact projections are current and authorized",
            projected_tokens=projected_tokens,
            projection_items=projections,
        )

    def _resolve_action(
        self,
        envelope: dict[str, Any],
        request: dict[str, Any],
    ) -> dict[str, Any]:
        capability = request["capability"]
        if capability in GENERIC_CAPABILITIES:
            return self._record(
                envelope,
                request,
                "denied",
                reason="generic project, shell, web, graph, transcript, or retrieval capability is forbidden",
            )
        if capability not in envelope["request_policy"]["allowed_capabilities"]:
            return self._record(
                envelope,
                request,
                "denied",
                reason="capability is not declared by the envelope policy",
            )
        if capability not in self._external_scope:
            return self._record(
                envelope,
                request,
                "denied",
                reason="capability is outside the node external permission scope",
            )
        registered = self._capabilities.get(capability)
        if not isinstance(registered, dict):
            return self._record(
                envelope,
                request,
                "denied",
                reason="capability has no typed broker registration",
            )
        if registered.get("high_risk") is True and not (
            isinstance(self._approval, dict)
            and self._approval.get("decision") == "approved"
            and self._approval.get("envelope_digest") == envelope["digest"]
        ):
            return self._record(
                envelope,
                request,
                "denied",
                reason="high-risk capability requires approval bound to the exact envelope digest",
            )
        return self._record(
            envelope,
            request,
            "approved",
            reason="typed capability is declared and authorized",
            capability=capability,
            arguments=copy.deepcopy(request["arguments"]),
            max_tokens=request["max_tokens"],
        )

    def execute_action(self, decision: dict[str, Any]) -> dict[str, Any]:
        body = {key: value for key, value in decision.items() if key != "digest"}
        if seal_document(body).get("digest") != decision.get("digest"):
            raise ValueError("broker decision digest does not match its content")
        if decision.get("decision") != "approved" or decision.get("kind") != "action":
            raise PermissionError("action execution requires an approved action decision")
        decision_digest = decision.get("digest")
        if decision_digest in self._executed_actions:
            return copy.deepcopy(self._executed_actions[decision_digest])
        capability = decision.get("capability")
        registered = self._capabilities.get(capability)
        if not isinstance(registered, dict):
            raise ValueError("approved capability registration is unavailable")
        handler = registered.get("handler")
        projector = registered.get("projector")
        if not callable(handler) or not callable(projector):
            raise ValueError("capability handler and projector must be callable")
        raw_result = handler(copy.deepcopy(decision["arguments"]))
        raw_bytes = canonical_json_bytes(raw_result)
        projected_content = projector(copy.deepcopy(raw_result))
        if not isinstance(projected_content, str):
            raise ValueError("capability projector must return a string")
        projected_tokens = self._tokenizer.count_tokens(projected_content)
        if projected_tokens > decision["max_tokens"]:
            raise ValueError("action projection exceeds the approved token bound")
        request_id = decision["request_id"]
        projection_item = {
            "artifact_id": f"action-result:{capability}-{request_id}",
            "content": projected_content,
            "source": registered.get("source", f"capability:{capability}"),
            "requirements": copy.deepcopy(
                registered.get("requirements", [])
            ),
            "inclusion_class": "capability",
        }
        result = seal_document(
            {
                "schema_version": 1,
                "request_digest": decision["request_digest"],
                "parent_envelope_digest": decision["parent_envelope_digest"],
                "capability": capability,
                "raw_result_sha256": hashlib.sha256(raw_bytes).hexdigest(),
                "raw_result_bytes": len(raw_bytes),
                "projection_tokens": projected_tokens,
                "projection_item": projection_item,
            }
        )
        self._executed_actions[decision_digest] = copy.deepcopy(result)
        return result
