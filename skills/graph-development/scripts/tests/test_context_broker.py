from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "tests"))

from context_broker import ContextBroker  # noqa: E402
from context_core import (  # noqa: E402
    DeterministicWordTokenizer,
    extend_envelope,
    plan_context,
    seal_document,
    validate_envelope,
)
from test_validate_graph import valid_graph  # noqa: E402


def context_item(artifact_id: str, content: str) -> dict[str, object]:
    return {
        "artifact_id": artifact_id,
        "content": content,
        "source": f"docs/{artifact_id.replace(':', '-')}.md",
        "requirements": ["acceptance"],
        "inclusion_class": "direct_input",
    }


class ContextBrokerTests(unittest.TestCase):
    def test_artifact_approval_accounts_for_headers_and_adapter_framing(self) -> None:
        class FramedTokenizer(DeterministicWordTokenizer):
            def serialize_prompt(self, payload: str) -> str:
                return "frame " * payload.count("[artifact:") + payload

        registry = {"artifact:missing": {
            "projection": {"content": "bounded projection", "source": "artifacts/missing.json", "requirements": ["acceptance"]},
            "authorized": True, "fresh": True, "provenance_complete": True, "reachable": True,
        }}
        for tokenizer, cost in [(DeterministicWordTokenizer("broker-word-v1"), 3), (FramedTokenizer("broker-word-v1"), 4)]:
            for limit in (cost - 1, cost):
                for bound in ("request", "policy", "remaining"):
                    with self.subTest(framed=cost == 4, limit=limit, bound=bound):
                        self.envelope = plan_context(
                            valid_graph(), "implement-change",
                            [context_item("artifact:required", "required information")], [], {},
                            tokenizer=tokenizer, mode="executable", input_limit=100,
                            output_reserve=20, safety_margin=5,
                            request_policy={"allowed_kinds": ["artifact"], "allowed_capabilities": [], "max_additional_tokens": limit if bound == "policy" else 10},
                        )["envelope"]
                        if bound == "remaining":
                            budget = self.envelope["budget"]
                            budget["available_context"] = budget["used_context"] + limit
                            budget["input_limit"] = budget["available_context"] + budget["output_reserve"] + budget["safety_margin"]
                            self.envelope = seal_document(self.envelope)
                        broker = ContextBroker(artifact_registry=registry, capability_registry={}, tokenizer=tokenizer, read_scope={"artifact:missing"}, external_scope=set(), max_requests=1)
                        decision = broker.resolve(self.envelope, self.request(max_tokens=limit))
                        if limit < cost:
                            self.assertEqual(decision["decision"], "denied")
                            self.assertNotIn("projection_items", decision)
                        else:
                            self.assertEqual(decision["decision"], "approved")
                            extended = extend_envelope(self.envelope, decision["projection_items"], tokenizer=tokenizer, parent_request_digest=decision["request_digest"])
                            self.assertEqual(validate_envelope(extended), [])
                            self.assertEqual(decision["projected_tokens"], cost)
                            self.assertEqual(extended["budget"]["used_context"] - self.envelope["budget"]["used_context"], cost)

    def setUp(self) -> None:
        graph = valid_graph()
        graph["project"]["context_version"] = "test:broker"
        self.tokenizer = DeterministicWordTokenizer("broker-word-v1")
        self.envelope = plan_context(
            graph,
            "implement-change",
            [context_item("artifact:required", "required information")],
            [],
            {},
            tokenizer=self.tokenizer,
            mode="executable",
            input_limit=100,
            output_reserve=20,
            safety_margin=5,
            request_policy={
                "allowed_kinds": ["artifact", "action"],
                "allowed_capabilities": ["lookup-record", "read-file"],
                "max_additional_tokens": 10,
            },
        )["envelope"]

    def request(
        self,
        *,
        artifact: str = "artifact:missing",
        max_tokens: int = 5,
    ) -> dict[str, object]:
        return seal_document(
            {
                "schema_version": 1,
                "request_id": "request-missing",
                "envelope_id": self.envelope["envelope_id"],
                "task_node_id": self.envelope["task_node_id"],
                "kind": "artifact",
                "purpose": "Satisfy acceptance.",
                "max_tokens": max_tokens,
                "requested_artifacts": [artifact],
            }
        )

    def action_request(
        self,
        capability: str,
        *,
        request_id: str = "request-action",
    ) -> dict[str, object]:
        return seal_document(
            {
                "schema_version": 1,
                "request_id": request_id,
                "envelope_id": self.envelope["envelope_id"],
                "task_node_id": self.envelope["task_node_id"],
                "kind": "action",
                "purpose": "Satisfy acceptance.",
                "max_tokens": 5,
                "capability": capability,
                "arguments": {"id": "42"},
            }
        )

    def test_valid_artifact_request_returns_projection_and_new_digest(self) -> None:
        registry = {
            "artifact:missing": {
                "raw_content": "raw private data that must stay outside",
                "projection": {
                    "content": "bounded projection",
                    "source": "artifacts/missing.json",
                    "requirements": ["acceptance"],
                },
                "authorized": True,
                "fresh": True,
                "provenance_complete": True,
                "reachable": True,
            }
        }
        broker = ContextBroker(
            artifact_registry=registry,
            capability_registry={},
            tokenizer=self.tokenizer,
            read_scope={"artifact:missing"},
            external_scope=set(),
            max_requests=2,
        )

        decision = broker.resolve(self.envelope, self.request())
        resumed = extend_envelope(
            self.envelope,
            decision["projection_items"],
            tokenizer=self.tokenizer,
            parent_request_digest=decision["request_digest"],
        )

        self.assertEqual(decision["decision"], "approved")
        self.assertNotIn("raw private data", repr(decision))
        self.assertNotEqual(resumed["digest"], self.envelope["digest"])
        self.assertEqual(validate_envelope(resumed), [])
        self.assertIn(
            "artifact:missing",
            [item["artifact_id"] for item in resumed["manifest"]],
        )

    def test_denies_unreachable_stale_unauthorized_and_overbudget_without_exposure(self) -> None:
        cases = {
            "unreachable": {"reachable": False},
            "stale": {"fresh": False},
            "unauthorized": {"authorized": False},
        }
        for reason, flag in cases.items():
            record = {
                "raw_content": "DO-NOT-EXPOSE",
                "projection": {
                    "content": "projection",
                    "source": "artifacts/missing.json",
                    "requirements": ["acceptance"],
                },
                "authorized": True,
                "fresh": True,
                "provenance_complete": True,
                "reachable": True,
                **flag,
            }
            broker = ContextBroker(
                artifact_registry={"artifact:missing": record},
                capability_registry={},
                tokenizer=self.tokenizer,
                read_scope={"artifact:missing"},
                external_scope=set(),
                max_requests=1,
            )

            decision = broker.resolve(self.envelope, self.request())

            with self.subTest(reason=reason):
                self.assertEqual(decision["decision"], "denied")
                self.assertIn(reason, decision["reason"])
                self.assertNotIn("DO-NOT-EXPOSE", repr(decision))

        broker = ContextBroker(
            artifact_registry={
                "artifact:missing": {
                    "raw_content": "DO-NOT-EXPOSE",
                    "projection": {
                        "content": "one two three four five six",
                        "source": "artifacts/missing.json",
                        "requirements": ["acceptance"],
                    },
                    "authorized": True,
                    "fresh": True,
                    "provenance_complete": True,
                    "reachable": True,
                }
            },
            capability_registry={},
            tokenizer=self.tokenizer,
            read_scope={"artifact:missing"},
            external_scope=set(),
            max_requests=1,
        )
        decision = broker.resolve(self.envelope, self.request(max_tokens=5))
        self.assertEqual(decision["decision"], "denied")
        self.assertIn("token", decision["reason"])

    def test_generic_capabilities_are_denied_even_if_declared(self) -> None:
        called = 0

        def handler(arguments: dict[str, object]) -> object:
            nonlocal called
            called += 1
            return {"secret": "should not run"}

        broker = ContextBroker(
            artifact_registry={},
            capability_registry={
                "read-file": {
                    "handler": handler,
                    "projector": lambda raw: "projection",
                    "source": "capability:read-file",
                }
            },
            tokenizer=self.tokenizer,
            read_scope=set(),
            external_scope={"read-file"},
            max_requests=1,
        )

        decision = broker.resolve(
            self.envelope,
            self.action_request("read-file"),
        )

        self.assertEqual(decision["decision"], "denied")
        self.assertIn("generic", decision["reason"])
        self.assertEqual(called, 0)

    def test_typed_action_executes_only_after_authorization_and_projects_raw_result(self) -> None:
        calls: list[dict[str, object]] = []

        def handler(arguments: dict[str, object]) -> object:
            calls.append(arguments)
            return {"id": arguments["id"], "private": "RAW-SECRET", "name": "Record"}

        broker = ContextBroker(
            artifact_registry={},
            capability_registry={
                "lookup-record": {
                    "handler": handler,
                    "projector": lambda raw: f"id={raw['id']} name={raw['name']}",
                    "source": "capabilities/lookup-record",
                    "requirements": ["acceptance"],
                    "high_risk": False,
                }
            },
            tokenizer=self.tokenizer,
            read_scope=set(),
            external_scope={"lookup-record"},
            max_requests=1,
        )

        decision = broker.resolve(
            self.envelope,
            self.action_request("lookup-record"),
        )
        action_result = broker.execute_action(decision)
        replayed = broker.execute_action(decision)

        self.assertEqual(decision["decision"], "approved")
        self.assertEqual(len(calls), 1)
        self.assertEqual(replayed, action_result)
        self.assertNotIn("RAW-SECRET", repr(action_result))
        self.assertEqual(action_result["raw_result_bytes"] > 0, True)
        self.assertEqual(len(action_result["raw_result_sha256"]), 64)
        self.assertEqual(
            action_result["projection_item"]["content"],
            "id=42 name=Record",
        )

    def test_high_risk_approval_is_bound_to_exact_envelope_digest(self) -> None:
        registry = {
            "lookup-record": {
                "handler": lambda arguments: {"id": arguments["id"]},
                "projector": lambda raw: f"id={raw['id']}",
                "source": "capabilities/lookup-record",
                "requirements": ["acceptance"],
                "high_risk": True,
            }
        }
        request = self.action_request("lookup-record")
        broker = ContextBroker(
            artifact_registry={},
            capability_registry=registry,
            tokenizer=self.tokenizer,
            read_scope=set(),
            external_scope={"lookup-record"},
            max_requests=2,
            approval={
                "decision": "approved",
                "envelope_digest": "0" * 64,
            },
        )
        denied = broker.resolve(self.envelope, request)
        self.assertEqual(denied["decision"], "denied")
        self.assertIn("approval", denied["reason"])

        request = copy.deepcopy(request)
        request["request_id"] = "request-action-approved"
        request = seal_document(
            {key: value for key, value in request.items() if key != "digest"}
        )
        broker = ContextBroker(
            artifact_registry={},
            capability_registry=registry,
            tokenizer=self.tokenizer,
            read_scope=set(),
            external_scope={"lookup-record"},
            max_requests=1,
            approval={
                "decision": "approved",
                "envelope_digest": self.envelope["digest"],
            },
        )
        approved = broker.resolve(self.envelope, request)
        self.assertEqual(approved["decision"], "approved")

    def test_replay_and_request_bound_are_denied(self) -> None:
        registry = {
            "artifact:missing": {
                "projection": {
                    "content": "bounded",
                    "source": "artifacts/missing.json",
                    "requirements": ["acceptance"],
                },
                "authorized": True,
                "fresh": True,
                "provenance_complete": True,
                "reachable": True,
            }
        }
        broker = ContextBroker(
            artifact_registry=registry,
            capability_registry={},
            tokenizer=self.tokenizer,
            read_scope={"artifact:missing"},
            external_scope=set(),
            max_requests=1,
        )
        request = self.request()

        self.assertEqual(
            broker.resolve(self.envelope, request)["decision"],
            "approved",
        )
        denied = broker.resolve(self.envelope, request)
        self.assertEqual(denied["decision"], "denied")
        self.assertIn("replay", denied["reason"])


if __name__ == "__main__":
    unittest.main()
