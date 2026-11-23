from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
ROOT = SCRIPTS.parent
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "tests"))

from context_core import (  # noqa: E402
    ContextControlError,
    DeterministicWordTokenizer,
    PreviewTokenizer,
    build_mandatory_frontier,
    build_result_key,
    plan_context,
    select_optional_context,
    validate_envelope,
    validate_result,
)
from test_validate_graph import valid_graph  # noqa: E402


def item(
    artifact_id: str,
    content: str,
    requirements: list[str],
    *,
    inclusion_class: str = "direct_input",
    **flags: object,
) -> dict[str, object]:
    value: dict[str, object] = {
        "artifact_id": artifact_id,
        "content": content,
        "source": f"docs/{artifact_id.replace(':', '-')}.md",
        "requirements": requirements,
        "inclusion_class": inclusion_class,
    }
    value.update(flags)
    return value


class ContextControllerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.graph = valid_graph()
        self.graph["project"]["context_version"] = "test:v3-controller"
        self.node_id = "implement-change"
        self.tokenizer = DeterministicWordTokenizer("word-exact-v1")

    def test_mandatory_frontier_has_complete_class_recall_and_stable_order(self) -> None:
        registry = {
            "artifact:spec": item("artifact:spec", "direct input", ["acceptance"]),
        }
        node = next(item for item in self.graph["nodes"] if item["id"] == self.node_id)
        node["inputs"] = [
            {"name": "spec", "artifact": "artifact:spec", "required": True}
        ]
        extras = [
            item("policy:rare", "rare policy", ["policy"], inclusion_class="policy"),
            item("authority:owner", "owner only", ["authority"], inclusion_class="authority"),
            item("approval:bound", "approved", ["approval"], inclusion_class="approval"),
            item("safety:no-delete", "never delete", ["safety"], inclusion_class="safety"),
            item("delta:spec", "spec changed", ["delta"], inclusion_class="invalidation"),
            item("capability:lookup", "lookup schema", ["capability"], inclusion_class="capability"),
        ]

        frontier = build_mandatory_frontier(
            self.graph,
            self.node_id,
            registry,
            extras=extras,
        )

        self.assertEqual(
            [entry["inclusion_class"] for entry in frontier],
            [
                "node_contract",
                "direct_input",
                "policy",
                "authority",
                "approval",
                "safety",
                "invalidation",
                "capability",
            ],
        )
        self.assertEqual(
            {entry["artifact_id"] for entry in frontier},
            {
                "node-contract:implement-change",
                "artifact:spec",
                "policy:rare",
                "authority:owner",
                "approval:bound",
                "safety:no-delete",
                "delta:spec",
                "capability:lookup",
            },
        )

    def test_missing_or_stale_mandatory_input_fails_closed(self) -> None:
        node = next(item for item in self.graph["nodes"] if item["id"] == self.node_id)
        node["inputs"] = [
            {"name": "spec", "artifact": "artifact:spec", "required": True}
        ]
        with self.assertRaisesRegex(ContextControlError, "artifact:spec"):
            build_mandatory_frontier(self.graph, self.node_id, {})

        registry = {
            "artifact:spec": item(
                "artifact:spec",
                "stale input",
                ["acceptance"],
                fresh=False,
            )
        }
        with self.assertRaisesRegex(ContextControlError, "not fresh"):
            build_mandatory_frontier(self.graph, self.node_id, registry)

    def test_optional_selector_is_deterministic_coverage_aware_and_filters_ineligible(self) -> None:
        candidates = [
            item("optional:a", "one two", ["a"]),
            item("optional:ab", "one two three four five", ["a", "b"]),
            item("optional:b", "one two", ["b"]),
            item("optional:conflict", "one", ["c"], conflicting=True),
            item("optional:stale", "one", ["c"], fresh=False),
            item("optional:unauthorized", "one", ["c"], authorized=False),
        ]
        weights = {"a": 3, "b": 3, "c": 10}

        first = select_optional_context(
            candidates,
            weights,
            budget=4,
            tokenizer=self.tokenizer,
        )
        second = select_optional_context(
            list(reversed(candidates)),
            weights,
            budget=4,
            tokenizer=self.tokenizer,
        )

        self.assertEqual(first["selected_ids"], ["optional:a", "optional:b"])
        self.assertEqual(first["selected_ids"], second["selected_ids"])
        reasons = {
            entry["artifact_id"]: entry["reason"]
            for entry in first["omitted"]
        }
        self.assertEqual(reasons["optional:stale"], "stale")
        self.assertEqual(reasons["optional:unauthorized"], "unauthorized")
        self.assertEqual(reasons["optional:conflict"], "conflict")
        self.assertEqual(reasons["optional:ab"], "redundant")

    def test_optional_selector_compares_greedy_with_best_singleton(self) -> None:
        candidates = [
            item("optional:dense", "one two", ["a"]),
            item("optional:singleton", "one two three four five", ["b", "c"]),
        ]
        selection = select_optional_context(
            candidates,
            {"a": 5, "b": 6, "c": 6},
            budget=5,
            tokenizer=self.tokenizer,
        )

        self.assertEqual(selection["strategy"], "best-singleton")
        self.assertEqual(selection["selected_ids"], ["optional:singleton"])

    def test_executable_planning_refuses_approximate_tokenizer(self) -> None:
        with self.assertRaisesRegex(ContextControlError, "exact tokenizer"):
            plan_context(
                self.graph,
                self.node_id,
                [item("artifact:required", "must include", ["acceptance"])],
                [],
                {},
                tokenizer=PreviewTokenizer(),
                mode="executable",
                input_limit=100,
                output_reserve=10,
                safety_margin=5,
            )

    def test_mandatory_overflow_returns_typed_result_without_envelope(self) -> None:
        plan = plan_context(
            self.graph,
            self.node_id,
            [item("artifact:required", "one two three four five", ["acceptance"])],
            [],
            {},
            tokenizer=self.tokenizer,
            mode="executable",
            input_limit=8,
            output_reserve=2,
            safety_margin=2,
        )

        self.assertEqual(plan["status"], "mandatory_overflow")
        self.assertNotIn("envelope", plan)
        self.assertEqual(validate_result(plan["result"]), [])
        self.assertEqual(plan["result"]["usage"]["total_tokens"], 0)

    def test_plan_seals_exact_deterministic_envelope_and_never_drops_mandatory(self) -> None:
        mandatory = [
            item("artifact:required", "one two", ["acceptance"]),
            item(
                "policy:rare",
                "rare",
                ["policy"],
                inclusion_class="policy",
            ),
        ]
        optional = [
            item("optional:a", "one", ["a"]),
            item("optional:b", "two", ["b"]),
        ]
        arguments = dict(
            graph=self.graph,
            task_node_id=self.node_id,
            mandatory_items=mandatory,
            optional_candidates=optional,
            requirement_weights={"a": 2, "b": 1},
            tokenizer=self.tokenizer,
            mode="executable",
            input_limit=30,
            output_reserve=5,
            safety_margin=2,
            request_policy={
                "allowed_kinds": ["artifact"],
                "allowed_capabilities": [],
                "max_additional_tokens": 5,
            },
        )

        first = plan_context(**arguments)
        second = plan_context(**copy.deepcopy(arguments))

        self.assertEqual(first["status"], "planned")
        self.assertEqual(first["envelope"], second["envelope"])
        self.assertEqual(validate_envelope(first["envelope"]), [])
        self.assertEqual(
            [
                entry["artifact_id"]
                for entry in first["envelope"]["manifest"][:2]
            ],
            ["artifact:required", "policy:rare"],
        )
        self.assertTrue(first["envelope"]["budget"]["tokenizer"]["exact"])

    def test_complete_interface_excludes_unrelated_branch_and_deep_ancestry(self) -> None:
        node = next(item for item in self.graph["nodes"] if item["id"] == self.node_id)
        node["inputs"] = [
            {"name": "accepted interface", "artifact": "artifact:interface", "required": True}
        ]
        registry = {
            "artifact:interface": item(
                "artifact:interface",
                "complete consumer projection",
                ["acceptance"],
                version="interface-v2",
            ),
            "artifact:deep-ancestor": item(
                "artifact:deep-ancestor",
                "large historical implementation transcript",
                ["history"],
            ),
            "artifact:sibling-branch": item(
                "artifact:sibling-branch",
                "unrelated parallel branch",
                ["other-goal"],
            ),
        }

        frontier = build_mandatory_frontier(
            self.graph,
            self.node_id,
            registry,
        )
        identifiers = {entry["artifact_id"] for entry in frontier}

        self.assertIn("artifact:interface", identifiers)
        self.assertNotIn("artifact:deep-ancestor", identifiers)
        self.assertNotIn("artifact:sibling-branch", identifiers)
        interface = next(
            entry for entry in frontier if entry["artifact_id"] == "artifact:interface"
        )
        self.assertEqual(interface["version"], "interface-v2")
        self.assertEqual(interface["source"], "docs/artifact-interface.md")

    def test_mandatory_only_envelope_is_feasible_when_optional_items_do_not_fit(self) -> None:
        planned = plan_context(
            self.graph,
            self.node_id,
            [item("artifact:required", "one two", ["acceptance"])],
            [item("optional:large", "optional " * 100, ["optional"])],
            {"optional": 10},
            tokenizer=self.tokenizer,
            mode="executable",
            input_limit=20,
            output_reserve=5,
            safety_margin=2,
        )

        self.assertEqual(planned["status"], "planned")
        self.assertEqual(planned["selection"]["selected_ids"], [])
        self.assertEqual(
            [entry["classification"] for entry in planned["envelope"]["manifest"]],
            ["mandatory"],
        )

    def test_serialization_overflow_reconciles_selection_evidence(self) -> None:
        planned = plan_context(
            self.graph,
            self.node_id,
            [item("artifact:required", "one", ["acceptance"])],
            [item("optional:selected", "two", ["optional"])],
            {"optional": 10},
            tokenizer=self.tokenizer,
            mode="executable",
            input_limit=5,
            output_reserve=1,
            safety_margin=1,
        )

        self.assertEqual(planned["status"], "planned")
        self.assertEqual(planned["selection"]["selected_ids"], [])
        self.assertEqual(planned["selection"]["covered_requirements"], [])
        self.assertEqual(planned["selection"]["utility"], 0)
        self.assertEqual(planned["selection"]["used_tokens"], 0)
        self.assertEqual(
            planned["selection"]["omitted"],
            [
                {
                    "artifact_id": "optional:selected",
                    "reason": "serialization-overflow",
                    "token_count": 1,
                }
            ],
        )
        envelope = planned["envelope"]
        self.assertEqual(envelope["selection"], {
            key: value
            for key, value in planned["selection"].items()
            if key != "selected"
        })
        self.assertEqual(envelope["budget"]["optional_tokens"], 0)
        self.assertEqual(validate_envelope(envelope), [])

    def test_tokenizer_identity_changes_envelope_digest_and_recounts_budget(self) -> None:
        arguments = {
            "graph": self.graph,
            "task_node_id": self.node_id,
            "mandatory_items": [
                item("artifact:required", "one two three", ["acceptance"])
            ],
            "optional_candidates": [],
            "requirement_weights": {},
            "mode": "executable",
            "input_limit": 100,
            "output_reserve": 10,
            "safety_margin": 5,
        }
        first = plan_context(
            **arguments,
            tokenizer=DeterministicWordTokenizer("model-a-tokenizer"),
        )["envelope"]
        second = plan_context(
            **arguments,
            tokenizer=DeterministicWordTokenizer("model-b-tokenizer"),
        )["envelope"]

        self.assertNotEqual(first["digest"], second["digest"])
        self.assertEqual(first["budget"]["used_context"], second["budget"]["used_context"])
        self.assertNotEqual(
            first["budget"]["tokenizer"]["id"],
            second["budget"]["tokenizer"]["id"],
        )

    def test_artifact_version_and_checkpoint_change_envelope_identity(self) -> None:
        def planned(version: str, checkpoint: str) -> dict[str, object]:
            return plan_context(
                self.graph,
                self.node_id,
                [
                    item(
                        "artifact:required",
                        "same projected bytes",
                        ["acceptance"],
                        version=version,
                    )
                ],
                [],
                {},
                tokenizer=self.tokenizer,
                mode="executable",
                input_limit=100,
                output_reserve=10,
                safety_margin=5,
                checkpoint_id=checkpoint,
            )["envelope"]

        baseline = planned("artifact-v1", "run-a:1")
        new_version = planned("artifact-v2", "run-a:1")
        new_checkpoint = planned("artifact-v1", "run-a:2")

        self.assertNotEqual(baseline["digest"], new_version["digest"])
        self.assertNotEqual(baseline["digest"], new_checkpoint["digest"])
        self.assertEqual(
            baseline["manifest"][0]["artifact_version"],  # type: ignore[index]
            "artifact-v1",
        )
        self.assertEqual(
            baseline["execution_context"]["checkpoint_id"],  # type: ignore[index]
            "run-a:1",
        )

    def test_envelope_is_byte_identical_across_fresh_processes(self) -> None:
        program = """
import json
import sys
sys.path.insert(0, "scripts")
sys.path.insert(0, "scripts/tests")
from context_core import DeterministicWordTokenizer, canonical_json_bytes, plan_context
from test_validate_graph import valid_graph
graph = valid_graph()
graph["project"]["context_version"] = "test:cross-process"
item = {
    "artifact_id": "artifact:required",
    "content": "stable projected content",
    "source": "docs/required.md",
    "requirements": ["acceptance"],
    "inclusion_class": "direct_input",
    "version": "artifact-v1",
}
planned = plan_context(
    graph,
    "implement-change",
    [item],
    [],
    {},
    tokenizer=DeterministicWordTokenizer("word-exact-v1"),
    mode="executable",
    input_limit=100,
    output_reserve=10,
    safety_margin=5,
    checkpoint_id="run-fixture:1",
)
sys.stdout.buffer.write(canonical_json_bytes(planned["envelope"]))
"""
        environment = {
            "PATH": os.environ.get("PATH", os.defpath),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        outputs = [
            subprocess.run(
                [sys.executable, "-c", program],
                cwd=ROOT,
                env=environment,
                check=True,
                capture_output=True,
            ).stdout
            for _ in range(2)
        ]

        self.assertEqual(outputs[0], outputs[1])
        envelope = json.loads(outputs[0])
        self.assertEqual(validate_envelope(envelope), [])
        self.assertEqual(
            sum(
                envelope["budget"][field]
                for field in (
                    "fixed_tokens",
                    "capability_schema_tokens",
                    "mandatory_tokens",
                    "optional_tokens",
                    "dynamic_tokens",
                )
            ),
            envelope["budget"]["used_context"],
        )

    def test_result_key_changes_for_every_registered_component(self) -> None:
        components = {
            "graph_checksum": "0" * 64,
            "checkpoint": "run-1:3",
            "node_contract_digest": "1" * 64,
            "implementation_version": "impl-v1",
            "direct_input_versions": [["artifact:a", "v1"]],
            "envelope_digest": "2" * 64,
            "selector_version": "coverage-greedy-v1",
            "compactor_version": "none",
            "policy_versions": ["policy-v1"],
            "approval_versions": ["approval-v1"],
            "adapter_identity": "fake/model/v1",
            "tokenizer_identity": "word-exact-v1",
            "runtime_config": {"temperature": 0},
        }
        baseline = build_result_key(components)
        for field in components:
            changed = copy.deepcopy(components)
            changed[field] = f"changed-{field}"
            with self.subTest(field=field):
                self.assertNotEqual(build_result_key(changed), baseline)


if __name__ == "__main__":
    unittest.main()
