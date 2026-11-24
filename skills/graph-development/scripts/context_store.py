"""Digest-first persistence, replay, and exact-key cache for V3 context control."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

from context_core import (
    SECRET_MARKER,
    SLUG,
    assert_valid_envelope,
    assert_valid_request,
    assert_valid_result,
    build_result_key,
    canonical_json_bytes,
    seal_document,
    sha256_json,
    validate_result,
)
from graph_core import GraphControlError, atomic_write_bytes, file_lock, load_json
from context_broker import ContextBroker


class ContextStore:
    """Project-local context evidence store rooted under one run directory."""

    def __init__(self, run_dir: Path, *, create: bool = True) -> None:
        self.run_dir = run_dir.resolve()
        self.root = self.run_dir / "context"
        self.lock_path = self.root / "context.lock"
        if create:
            for relative in (
                "envelopes",
                "requests",
                "results",
                "cache",
                "evidence",
            ):
                path = self.root / relative
                path.mkdir(parents=True, exist_ok=True, mode=0o700)

    @staticmethod
    def _slug(value: str, label: str) -> str:
        if not isinstance(value, str) or not SLUG.fullmatch(value):
            raise ValueError(f"{label} must be a lowercase slug")
        return value

    def _write_once(self, path: Path, record: dict[str, Any]) -> Path:
        with file_lock(self.lock_path):
            return self._write_once_locked(path, record)

    def _write_once_locked(self, path: Path, record: dict[str, Any]) -> Path:
        """Write an immutable record while the caller owns context.lock."""
        if path.exists():
            current = load_json(path)
            if current != record:
                raise GraphControlError(
                    f"content-addressed record differs at {path.name}"
                )
            return path
        atomic_write_bytes(path, canonical_json_bytes(record) + b"\n", mode=0o600)
        return path

    def resolve_broker_request(
        self,
        envelope: dict[str, Any],
        request: dict[str, Any],
        *,
        broker_options: dict[str, Any],
    ) -> dict[str, Any]:
        """Reserve the durable budget and decision before returning approval.

        Resolution only authorizes a typed request; no action handler executes
        under this lock. Action execution remains a separate adapter operation.
        """
        assert_valid_request(request)
        with file_lock(self.lock_path):
            broker = ContextBroker(
                **broker_options,
                seen_request_digests=self.broker_history(envelope.get("digest", "")),
            )
            decision = broker.resolve(envelope, request)
            request_path = self.root / "requests" / f"{request['request_id']}.json"
            decision_path = self.root / "requests" / f"{request['request_id']}-decision-{decision['digest']}.json"
            self._write_once_locked(request_path, copy.deepcopy(request))
            self._write_once_locked(decision_path, copy.deepcopy(decision))
            return decision

    def broker_history(self, parent_envelope_digest: str) -> set[str]:
        """Return durable request-budget identities for one parent envelope."""

        seen: set[str] = set()
        for path in sorted((self.root / "requests").glob("*-decision-*.json")):
            try:
                record = load_json(path)
            except (GraphControlError, OSError):
                continue
            body = {
                key: value
                for key, value in record.items()
                if key != "digest"
            }
            if seal_document(body) != record:
                continue
            request_digest = record.get("request_digest")
            if (
                record.get("parent_envelope_digest")
                == parent_envelope_digest
                and record.get("request_budget_identity") is True
                and isinstance(request_digest, str)
            ):
                seen.add(request_digest)
        return seen

    def store_envelope(
        self,
        envelope: dict[str, Any],
        *,
        retain_payload: bool = False,
    ) -> Path:
        assert_valid_envelope(envelope)
        record: dict[str, Any] = {
            "schema_version": 1,
            "envelope_id": envelope["envelope_id"],
            "envelope_digest": envelope["digest"],
            "graph": copy.deepcopy(envelope["graph"]),
            "task_node_id": envelope["task_node_id"],
            "execution_context": copy.deepcopy(
                envelope["execution_context"]
            ),
            "budget": copy.deepcopy(envelope["budget"]),
            "manifest": copy.deepcopy(envelope["manifest"]),
            "selection": copy.deepcopy(envelope["selection"]),
            "request_policy": copy.deepcopy(envelope["request_policy"]),
            "parent_request_digest": envelope["parent_request_digest"],
            "payload_digest": sha256_json(envelope["payload"]),
            "payload_retained": retain_payload,
        }
        if retain_payload:
            record["payload"] = copy.deepcopy(envelope["payload"])
        path = self.root / "envelopes" / f"{envelope['digest']}.json"
        return self._write_once(path, record)

    def store_request(self, request: dict[str, Any]) -> Path:
        assert_valid_request(request)
        path = self.root / "requests" / f"{request['request_id']}.json"
        return self._write_once(path, copy.deepcopy(request))

    def store_broker_record(self, record: dict[str, Any]) -> Path:
        request_id = self._slug(record.get("request_id"), "request_id")
        digest = record.get("digest")
        if not isinstance(digest, str) or len(digest) != 64:
            raise ValueError("broker record digest must be a SHA-256 digest")
        path = (
            self.root
            / "requests"
            / f"{request_id}-decision-{digest}.json"
        )
        return self._write_once(path, copy.deepcopy(record))

    def store_result(self, result: dict[str, Any]) -> Path:
        assert_valid_result(result)
        path = self.root / "results" / f"{result['digest']}.json"
        return self._write_once(path, copy.deepcopy(result))

    @staticmethod
    def results_equivalent(
        cached: dict[str, Any],
        clean: dict[str, Any],
    ) -> bool:
        assert_valid_result(cached)
        assert_valid_result(clean)
        if cached["status"] != "completed" or clean["status"] != "completed":
            return cached == clean
        return (
            cached.get("output_artifacts") == clean.get("output_artifacts")
            and sha256_json(cached.get("result_payload"))
            == sha256_json(clean.get("result_payload"))
            and sha256_json(cached.get("provenance"))
            == sha256_json(clean.get("provenance"))
        )

    def cache_put(
        self,
        components: dict[str, Any],
        result: dict[str, Any],
        *,
        clean_recomputation: dict[str, Any],
    ) -> str:
        assert_valid_result(result)
        if not self.results_equivalent(result, clean_recomputation):
            raise ValueError(
                "cache result differs from clean recomputation artifact or provenance digests"
            )
        result_key = build_result_key(components)
        record = seal_document(
            {
                "schema_version": 1,
                "result_key": result_key,
                "components": copy.deepcopy(components),
                "result_digest": result["digest"],
                "result": copy.deepcopy(result),
            }
        )
        self._write_once(
            self.root / "cache" / f"{result_key}.json",
            record,
        )
        return result_key

    def cache_inspect(self, components: dict[str, Any]) -> dict[str, Any]:
        result_key = build_result_key(components)
        path = self.root / "cache" / f"{result_key}.json"
        if not path.is_file():
            return {
                "hit": False,
                "reason": "exact-key-miss",
                "result_key": result_key,
                "components": copy.deepcopy(components),
            }
        try:
            record = load_json(path)
            body = {
                key: value
                for key, value in record.items()
                if key != "digest"
            }
            if seal_document(body) != record:
                raise ValueError("record digest mismatch")
            if record.get("result_key") != result_key:
                raise ValueError("record key mismatch")
            if record.get("components") != components:
                raise ValueError("record components mismatch")
            result = record.get("result")
            if validate_result(result):
                raise ValueError("cached result is invalid")
            if record.get("result_digest") != result.get("digest"):
                raise ValueError("cached result digest mismatch")
        except (GraphControlError, OSError, ValueError, AttributeError):
            return {
                "hit": False,
                "reason": "corrupt-cache-record",
                "result_key": result_key,
            }
        return {
            "hit": True,
            "reason": "exact-key-hit",
            "result_key": result_key,
            "components": copy.deepcopy(components),
            "result": copy.deepcopy(record["result"]),
        }

    def record_attempt(
        self,
        *,
        run_id: str,
        node_id: str,
        attempt: int,
        envelope_digest: str,
        result: dict[str, Any],
        accepted: bool,
    ) -> Path:
        run_id = self._slug(run_id, "run_id")
        node_id = self._slug(node_id, "node_id")
        if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
            raise ValueError("attempt must be a positive integer")
        assert_valid_result(result)
        record = seal_document(
            {
                "schema_version": 1,
                "record_type": "capsule-attempt",
                "run_id": run_id,
                "node_id": node_id,
                "attempt": attempt,
                "envelope_digest": envelope_digest,
                "result_digest": result["digest"],
                "result": copy.deepcopy(result),
                "accepted": bool(accepted),
            }
        )
        directory = self.root / "evidence" / run_id / node_id
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        return self._write_once(
            directory / f"attempt-{attempt}.json",
            record,
        )

    def find_replay(
        self,
        *,
        run_id: str,
        node_id: str,
        envelope_digest: str,
    ) -> dict[str, Any] | None:
        run_id = self._slug(run_id, "run_id")
        node_id = self._slug(node_id, "node_id")
        directory = self.root / "evidence" / run_id / node_id
        if not directory.is_dir():
            return None
        for path in sorted(directory.glob("attempt-*.json"), reverse=True):
            try:
                record = load_json(path)
            except (GraphControlError, OSError):
                continue
            if (
                record.get("record_type") == "capsule-attempt"
                and record.get("accepted") is True
                and record.get("envelope_digest") == envelope_digest
                and record.get("result_digest")
                == record.get("result", {}).get("digest")
                and not validate_result(record.get("result"))
            ):
                return copy.deepcopy(record["result"])
        return None

    def record_evidence(
        self,
        *,
        run_id: str,
        node_id: str,
        attempt: int,
        evidence: dict[str, Any],
    ) -> Path:
        run_id = self._slug(run_id, "run_id")
        node_id = self._slug(node_id, "node_id")
        if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
            raise ValueError("attempt must be a positive integer")
        serialized = canonical_json_bytes(evidence).decode("utf-8")
        if SECRET_MARKER.search(serialized):
            raise ValueError("evidence contains a credential-like secret marker")
        record = seal_document(
            {
                "schema_version": 1,
                "record_type": "evidence",
                "run_id": run_id,
                "node_id": node_id,
                "attempt": attempt,
                "evidence": copy.deepcopy(evidence),
            }
        )
        directory = self.root / "evidence" / run_id / node_id
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        return self._write_once(
            directory / f"attempt-{attempt}-evidence.json",
            record,
        )
