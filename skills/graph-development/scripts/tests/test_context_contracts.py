from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
ROOT = SCRIPTS.parent
sys.path.insert(0, str(SCRIPTS))

from context_core import (  # noqa: E402
    assert_valid_envelope,
    assert_valid_request,
    assert_valid_result,
    canonical_json_bytes,
    seal_document,
    validate_envelope,
    validate_request,
    validate_result,
)


ZERO = "0" * 64
ONE = "1" * 64


def envelope() -> dict[str, object]:
    value: dict[str, object] = {
        "schema_version": 1,
        "envelope_id": "envelope-fixture",
        "mode": "executable",
        "graph": {
            "graph_id": "fixture-graph",
            "graph_version": 3,
            "context_version": "commit:abc123",
            "canonical_sha256": ZERO,
        },
        "task_node_id": "implement-feature",
        "execution_context": {
            "checkpoint_id": "fixture-run:1",
            "node_contract_digest": ZERO,
            "adapter_identity": "fake/deterministic-capsule/1",
            "selector_version": "weighted-coverage-v1",
            "compactor_version": "none",
        },
        "budget": {
            "input_limit": 100,
            "output_reserve": 20,
            "safety_margin": 10,
            "available_context": 70,
            "used_context": 12,
            "fixed_tokens": 0,
            "capability_schema_tokens": 0,
            "mandatory_tokens": 12,
            "optional_tokens": 0,
            "dynamic_tokens": 0,
            "tokenizer": {"id": "fake-exact-v1", "exact": True},
        },
        "manifest": [
            {
                "artifact_id": "artifact:requirements",
                "artifact_version": "requirements-v1",
                "classification": "mandatory",
                "inclusion_class": "direct_input",
                "inclusion_reason": "required-direct-input",
                "token_count": 12,
                "sha256": ONE,
                "source": "docs/requirements.md",
                "requirements": ["acceptance-a"],
            }
        ],
        "payload": [
            {
                "artifact_id": "artifact:requirements",
                "content": "Required behavior.",
            }
        ],
        "selection": {
            "selector_version": "weighted-coverage-v1",
            "strategy": "marginal-density",
            "selected_ids": [],
            "omitted": [],
            "trace": [],
            "covered_requirements": [],
            "utility": 0,
            "used_tokens": 0,
            "budget": 58,
        },
        "request_policy": {
            "allowed_kinds": ["artifact"],
            "allowed_capabilities": [],
            "max_additional_tokens": 0,
        },
        "parent_request_digest": None,
    }
    value["manifest"][0]["sha256"] = __import__("hashlib").sha256(  # type: ignore[index]
        b"Required behavior."
    ).hexdigest()
    return seal_document(value)


def request(kind: str = "artifact") -> dict[str, object]:
    value: dict[str, object] = {
        "schema_version": 1,
        "request_id": "request-fixture",
        "envelope_id": "envelope-fixture",
        "task_node_id": "implement-feature",
        "kind": kind,
        "purpose": "Satisfy acceptance-a.",
        "max_tokens": 20,
    }
    if kind == "action":
        value.update({"capability": "lookup-record", "arguments": {"id": "42"}})
    else:
        value["requested_artifacts"] = ["artifact:missing-field"]
    return seal_document(value)


def result(status: str = "completed") -> dict[str, object]:
    value: dict[str, object] = {
        "schema_version": 1,
        "envelope_id": "envelope-fixture",
        "task_node_id": "implement-feature",
        "status": status,
        "usage": {"input_tokens": 12, "output_tokens": 3, "total_tokens": 15},
        "provenance": [{"source": "docs/requirements.md", "sha256": ONE}],
    }
    if status == "completed":
        value.update(
            {
                "output_artifacts": ["artifact:implementation"],
                "result_payload": {"summary": "done"},
            }
        )
    elif status == "context_request":
        value["request"] = request()
    elif status == "action_request":
        value["request"] = request("action")
    else:
        value["error"] = "bounded failure"
    return seal_document(value)


class ContextContractTests(unittest.TestCase):
    def test_distribution_schemas_match_approved_interfaces(self) -> None:
        # Frozen canonical digests preserve exact interface parity without
        # publishing a duplicate of the private development specification.
        import hashlib
        expected = {'context-envelope.schema.json': '75aaa103d4818c03c0dccee879d455fc315dcb4c2476a5cf8b8b9cc436f896f9', 'context-request.schema.json': '112b04100ad9a7c3dd33e48e0b8e6843060ee5a6d28ecf5bb38036a66ca5a8b1', 'context-result.schema.json': 'ab0db028c7c82a2201a8d931487582f31f623f3aed562a5a03c215543e1dc684'}
        for name, digest in expected.items():
            value = json.loads((ROOT / "references" / name).read_text())
            canonical = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
            self.assertEqual(hashlib.sha256(canonical.encode()).hexdigest(), digest, name)

    def test_canonical_json_and_digest_are_order_independent(self) -> None:
        first = seal_document({"b": 2, "a": 1})
        second = seal_document({"a": 1, "b": 2})

        self.assertEqual(canonical_json_bytes(first), canonical_json_bytes(second))
        self.assertEqual(first["digest"], second["digest"])

    def test_valid_envelope_request_and_result_pass(self) -> None:
        assert_valid_envelope(envelope())
        assert_valid_request(request())
        assert_valid_request(request("action"))
        assert_valid_result(result())
        assert_valid_result(result("context_request"))
        assert_valid_result(result("action_request"))

    def test_executable_envelope_requires_exact_tokenizer_and_budget_reconciliation(self) -> None:
        value = envelope()
        value["budget"]["tokenizer"]["exact"] = False  # type: ignore[index]
        value = seal_document({key: item for key, item in value.items() if key != "digest"})
        self.assertIn("$.budget.tokenizer.exact", "\n".join(validate_envelope(value)))

        value = envelope()
        value["budget"]["available_context"] = 69  # type: ignore[index]
        value = seal_document({key: item for key, item in value.items() if key != "digest"})
        self.assertIn("$.budget.available_context", "\n".join(validate_envelope(value)))

    def test_envelope_rejects_digest_payload_and_portability_drift(self) -> None:
        value = envelope()
        value["digest"] = ZERO
        self.assertIn("$.digest", "\n".join(validate_envelope(value)))

        value = envelope()
        value["payload"][0]["content"] = "changed"  # type: ignore[index]
        value = seal_document({key: item for key, item in value.items() if key != "digest"})
        self.assertIn("$.manifest[0].sha256", "\n".join(validate_envelope(value)))

        value = envelope()
        value["manifest"][0]["source"] = "/" + "private/requirements.md"  # type: ignore[index]
        value = seal_document({key: item for key, item in value.items() if key != "digest"})
        self.assertIn("$.manifest[0].source", "\n".join(validate_envelope(value)))

    def test_envelope_rejects_unknown_fields_secret_markers_and_order_drift(self) -> None:
        value = envelope()
        value["unknown"] = True
        value = seal_document({key: item for key, item in value.items() if key != "digest"})
        self.assertIn("$.unknown", "\n".join(validate_envelope(value)))

        value = envelope()
        value["payload"][0]["content"] = "token sk-secretfixturevalue"  # type: ignore[index]
        value["manifest"][0]["sha256"] = __import__("hashlib").sha256(  # type: ignore[index]
            b"token sk-secretfixturevalue"
        ).hexdigest()
        value = seal_document({key: item for key, item in value.items() if key != "digest"})
        self.assertIn("$.payload[0].content", "\n".join(validate_envelope(value)))

        value = envelope()
        optional = copy.deepcopy(value["manifest"][0])  # type: ignore[index]
        optional["artifact_id"] = "artifact:optional"
        optional["classification"] = "optional"
        optional["sha256"] = __import__("hashlib").sha256(b"optional").hexdigest()
        optional_payload = {"artifact_id": "artifact:optional", "content": "optional"}
        value["manifest"] = [optional, value["manifest"][0]]  # type: ignore[index]
        value["payload"] = [optional_payload, value["payload"][0]]  # type: ignore[index]
        value = seal_document({key: item for key, item in value.items() if key != "digest"})
        self.assertIn("$.manifest", "\n".join(validate_envelope(value)))

    def test_request_enforces_typed_exclusive_shape_and_digest(self) -> None:
        value = request()
        value["capability"] = "read-file"
        value["arguments"] = {"path": "secret.txt"}
        value = seal_document({key: item for key, item in value.items() if key != "digest"})
        self.assertIn("$.capability", "\n".join(validate_request(value)))

        value = request("action")
        del value["arguments"]
        value = seal_document({key: item for key, item in value.items() if key != "digest"})
        self.assertIn("$.arguments", "\n".join(validate_request(value)))

    def test_request_and_result_reject_nested_secret_markers(self) -> None:
        value = request("action")
        value["arguments"] = {
            "nested": {"credential": "sk-secretfixturevalue"}
        }
        value = seal_document(
            {key: item for key, item in value.items() if key != "digest"}
        )
        self.assertIn(
            "$.arguments.nested.credential",
            "\n".join(validate_request(value)),
        )

        value = result()
        value["result_payload"] = {
            "nested": ["safe", "ghp-secretfixturevalue"]
        }
        value = seal_document(
            {key: item for key, item in value.items() if key != "digest"}
        )
        self.assertIn(
            "$.result_payload.nested[1]",
            "\n".join(validate_result(value)),
        )

    def test_result_enforces_status_shape_nested_request_and_usage(self) -> None:
        value = result()
        value["usage"]["total_tokens"] = 14  # type: ignore[index]
        value = seal_document({key: item for key, item in value.items() if key != "digest"})
        self.assertIn("$.usage.total_tokens", "\n".join(validate_result(value)))

        value = result("context_request")
        value["request"] = request("action")
        value = seal_document({key: item for key, item in value.items() if key != "digest"})
        self.assertIn("$.request.kind", "\n".join(validate_result(value)))


if __name__ == "__main__":
    unittest.main()
