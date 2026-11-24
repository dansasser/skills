"""Deterministic capsule adapter used only by core tests and frozen evaluations."""

from __future__ import annotations

import copy
from typing import Any, Callable

from context_core import (
    ContextValidationError,
    Tokenizer,
    assert_valid_result,
    canonical_json_bytes,
    count_adapter_input,
    seal_document,
    validate_envelope,
)


class FakeCapsuleAdapter:
    """In-memory adapter that has no project or generic tool capabilities."""

    def __init__(
        self,
        *,
        tokenizer: Tokenizer,
        context_limit: int,
        handler: Callable[[dict[str, Any]], dict[str, Any]],
    ) -> None:
        if context_limit < 1:
            raise ValueError("context_limit must be positive")
        self._tokenizer = tokenizer
        self._context_limit = context_limit
        self._handler = handler
        self._completed: dict[str, dict[str, Any]] = {}
        self._cancelled: set[str] = set()
        self.invocation_count = 0

    def identity(self) -> dict[str, Any]:
        return {
            "provider": "fake",
            "model": "deterministic-capsule",
            "version": "1",
            "tokenizer": self._tokenizer.tokenizer_id,
            "capabilities": [],
        }

    def context_limit(self) -> int:
        return self._context_limit

    def count_tokens(self, serialized_payload: str) -> int:
        return self._tokenizer.count_tokens(serialized_payload)

    def cancel(self, idempotency_key: str) -> None:
        if not isinstance(idempotency_key, str) or not idempotency_key:
            raise ValueError("idempotency_key must be nonempty")
        self._cancelled.add(idempotency_key)

    @staticmethod
    def _identity(envelope: dict[str, Any]) -> tuple[str, str]:
        envelope_id = envelope.get("envelope_id")
        task_node_id = envelope.get("task_node_id")
        return (
            envelope_id
            if isinstance(envelope_id, str) and envelope_id
            else "invalid-envelope",
            task_node_id
            if isinstance(task_node_id, str) and task_node_id
            else "invalid-task",
        )

    @staticmethod
    def _provenance(envelope: dict[str, Any]) -> list[dict[str, str]]:
        manifest = envelope.get("manifest")
        if not isinstance(manifest, list):
            return []
        records: list[dict[str, str]] = []
        for item in manifest:
            if (
                isinstance(item, dict)
                and isinstance(item.get("source"), str)
                and isinstance(item.get("sha256"), str)
            ):
                records.append(
                    {
                        "source": item["source"],
                        "sha256": item["sha256"],
                    }
                )
        return records

    def _failure(
        self,
        envelope: dict[str, Any],
        status: str,
        error: str,
    ) -> dict[str, Any]:
        envelope_id, task_node_id = self._identity(envelope)
        return seal_document(
            {
                "schema_version": 1,
                "envelope_id": envelope_id,
                "task_node_id": task_node_id,
                "status": status,
                "usage": {
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "total_tokens": 0,
                },
                "provenance": self._provenance(envelope),
                "error": error,
            }
        )

    def _preflight(self, envelope: dict[str, Any]) -> list[str]:
        errors = validate_envelope(envelope)
        if errors:
            return errors
        if envelope["mode"] != "executable":
            errors.append("$.mode: fake capsule invocation requires executable mode")
        tokenizer = envelope["budget"]["tokenizer"]
        if tokenizer["exact"] is not True:
            errors.append("$.budget.tokenizer.exact: exact tokenizer is required")
        if tokenizer["id"] != self._tokenizer.tokenizer_id:
            errors.append("$.budget.tokenizer.id: does not match adapter tokenizer")
        identity = self.identity()
        expected_adapter_identity = (
            f"{identity['provider']}/{identity['model']}/{identity['version']}"
        )
        if (
            envelope["execution_context"]["adapter_identity"]
            != expected_adapter_identity
        ):
            errors.append(
                "$.execution_context.adapter_identity: does not match adapter"
            )
        if envelope["budget"]["input_limit"] > self._context_limit:
            errors.append("$.budget.input_limit: exceeds adapter context limit")
        actual = count_adapter_input(self._tokenizer, envelope["payload"])
        if actual != envelope["budget"]["used_context"]:
            errors.append(
                "$.budget.used_context: does not match exact serialized adapter payload"
            )
        return errors

    def invoke(
        self,
        sealed_envelope: dict[str, Any],
        idempotency_key: str,
    ) -> dict[str, Any]:
        if not isinstance(idempotency_key, str) or not idempotency_key:
            raise ValueError("idempotency_key must be nonempty")
        if idempotency_key in self._completed:
            return copy.deepcopy(self._completed[idempotency_key])

        envelope = copy.deepcopy(sealed_envelope)
        preflight_errors = self._preflight(envelope)
        if preflight_errors:
            return self._failure(
                envelope,
                "invalid_envelope",
                "; ".join(preflight_errors),
            )
        if idempotency_key in self._cancelled:
            result = self._failure(
                envelope,
                "cancelled",
                "invocation was cancelled before execution",
            )
            self._completed[idempotency_key] = result
            return copy.deepcopy(result)

        self.invocation_count += 1
        try:
            response = self._handler(copy.deepcopy(envelope))
            result = self._build_result(envelope, response)
        except Exception as exc:
            result = self._failure(
                envelope,
                "adapter_failure",
                f"fake adapter handler failed: {type(exc).__name__}: {exc}",
            )
        self._completed[idempotency_key] = result
        return copy.deepcopy(result)

    def _build_result(
        self,
        envelope: dict[str, Any],
        response: dict[str, Any],
    ) -> dict[str, Any]:
        if not isinstance(response, dict):
            raise ValueError("fake adapter handler must return an object")
        status = response.get("status")
        if status not in {"completed", "context_request", "action_request"}:
            raise ValueError("fake adapter handler returned an unsupported status")
        input_tokens = envelope["budget"]["used_context"]
        body: dict[str, Any] = {
            "schema_version": 1,
            "envelope_id": envelope["envelope_id"],
            "task_node_id": envelope["task_node_id"],
            "status": status,
            "provenance": self._provenance(envelope),
        }
        if status == "completed":
            body["output_artifacts"] = response.get("output_artifacts")
            body["result_payload"] = response.get("result_payload")
            output_value = {
                "output_artifacts": body["output_artifacts"],
                "result_payload": body["result_payload"],
            }
        else:
            body["request"] = response.get("request")
            output_value = body["request"]
        output_tokens = self.count_tokens(
            canonical_json_bytes(output_value).decode("utf-8")
        )
        body["usage"] = {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        }
        result = seal_document(body)
        try:
            assert_valid_result(result)
        except ContextValidationError as exc:
            raise ValueError(
                "fake adapter handler produced an invalid result: "
                + "; ".join(exc.errors)
            ) from exc
        return result
