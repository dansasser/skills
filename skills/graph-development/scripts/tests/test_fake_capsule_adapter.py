from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
SCRIPTS = TESTS.parent
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(TESTS))

from context_core import (  # noqa: E402
    DeterministicWordTokenizer,
    plan_context,
    seal_document,
    validate_result,
)
from fake_capsule_adapter import FakeCapsuleAdapter  # noqa: E402
from test_validate_graph import valid_graph  # noqa: E402


def _item() -> dict[str, object]:
    return {
        "artifact_id": "artifact:required",
        "content": "required information",
        "source": "docs/required.md",
        "requirements": ["acceptance"],
        "inclusion_class": "direct_input",
    }


class FakeCapsuleAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        graph = valid_graph()
        graph["project"]["context_version"] = "test:fake-adapter"
        self.tokenizer = DeterministicWordTokenizer("fake-word-v1")
        self.envelope = plan_context(
            graph,
            "implement-change",
            [_item()],
            [],
            {},
            tokenizer=self.tokenizer,
            mode="executable",
            input_limit=100,
            output_reserve=20,
            safety_margin=5,
        )["envelope"]

    def test_invocation_is_typed_idempotent_and_capability_free(self) -> None:
        observed: dict[str, object] = {}

        def handler(envelope: dict[str, object]) -> dict[str, object]:
            observed["keys"] = sorted(envelope)
            return {
                "status": "completed",
                "output_artifacts": ["artifact:implementation"],
                "result_payload": {"summary": "done"},
            }

        adapter = FakeCapsuleAdapter(
            tokenizer=self.tokenizer,
            context_limit=100,
            handler=handler,
        )
        first = adapter.invoke(self.envelope, "same-key")
        second = adapter.invoke(copy.deepcopy(self.envelope), "same-key")

        self.assertEqual(first, second)
        self.assertEqual(validate_result(first), [])
        self.assertEqual(adapter.invocation_count, 1)
        self.assertNotIn("filesystem", observed["keys"])
        self.assertEqual(adapter.identity()["capabilities"], [])

    def test_invalid_and_cancelled_envelopes_never_invoke_handler(self) -> None:
        adapter = FakeCapsuleAdapter(
            tokenizer=self.tokenizer,
            context_limit=100,
            handler=lambda envelope: self.fail("handler must not run"),
        )
        tampered = copy.deepcopy(self.envelope)
        tampered["payload"][0]["content"] = "changed"
        self.assertEqual(
            adapter.invoke(tampered, "tampered")["status"],
            "invalid_envelope",
        )

        adapter.cancel("cancelled")
        self.assertEqual(
            adapter.invoke(self.envelope, "cancelled")["status"],
            "cancelled",
        )
        self.assertEqual(adapter.invocation_count, 0)

    def test_handler_can_return_typed_context_request(self) -> None:
        request = seal_document(
            {
                "schema_version": 1,
                "request_id": "request-more-context",
                "envelope_id": self.envelope["envelope_id"],
                "task_node_id": self.envelope["task_node_id"],
                "kind": "artifact",
                "purpose": "Satisfy acceptance.",
                "max_tokens": 5,
                "requested_artifacts": ["artifact:missing"],
            }
        )
        adapter = FakeCapsuleAdapter(
            tokenizer=self.tokenizer,
            context_limit=100,
            handler=lambda envelope: {
                "status": "context_request",
                "request": request,
            },
        )
        result = adapter.invoke(self.envelope, "request-key")
        self.assertEqual(result["status"], "context_request")
        self.assertEqual(validate_result(result), [])


if __name__ == "__main__":
    unittest.main()
