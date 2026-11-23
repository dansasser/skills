from __future__ import annotations

import json
import copy
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "tests"))

from context_core import (  # noqa: E402
    DeterministicWordTokenizer,
    OPTIONAL_OMISSION_REASONS,
    plan_context,
    seal_document,
)
from context_observability import (  # noqa: E402
    REASON_CODES,
    explain_cache,
    explain_envelope_record,
    explain_invalidation,
    explain_plan,
    inspect_replay,
    validate_context_explanation,
)
from context_store import ContextStore  # noqa: E402
from test_validate_graph import valid_graph  # noqa: E402


def _item(
    artifact_id: str,
    content: str,
    *,
    requirements: list[str],
    inclusion_class: str = "direct_input",
    **flags: object,
) -> dict[str, object]:
    return {
        "artifact_id": artifact_id,
        "content": content,
        "source": f"docs/{artifact_id.replace(':', '-')}.md",
        "requirements": requirements,
        "inclusion_class": inclusion_class,
        "version": f"version:{artifact_id}",
        **flags,
    }


class ContextObservabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.graph = valid_graph()
        self.graph["project"]["root"] = "."
        self.graph["project"]["context_version"] = "test:observability"
        self.tokenizer = DeterministicWordTokenizer("observability-word-v1")

    def _planned(self) -> dict[str, object]:
        return plan_context(
            self.graph,
            "implement-change",
            [
                _item(
                    "artifact:required",
                    "PRIVATE MANDATORY PAYLOAD",
                    requirements=["acceptance"],
                )
            ],
            [
                _item(
                    "artifact:selected",
                    "PRIVATE SELECTED",
                    requirements=["optional-a"],
                    inclusion_class="optional",
                ),
                _item(
                    "artifact:omitted",
                    "PRIVATE OMITTED",
                    requirements=["optional-b"],
                    inclusion_class="optional",
                ),
                _item(
                    "artifact:blocked",
                    "PRIVATE BLOCKED",
                    requirements=["optional-c"],
                    inclusion_class="optional",
                    authorized=False,
                ),
            ],
            {"optional-a": 10, "optional-b": 0, "optional-c": 10},
            tokenizer=self.tokenizer,
            mode="executable",
            input_limit=100,
            output_reserve=20,
            safety_margin=5,
        )

    def test_core_exposes_closed_optional_omission_vocabulary(self) -> None:
        self.assertEqual(
            OPTIONAL_OMISSION_REASONS,
            frozenset(
                {
                    "unauthorized",
                    "stale",
                    "provenance-incomplete",
                    "conflict",
                    "no-coverage",
                    "no-positive-coverage",
                    "redundant",
                    "budget",
                    "not-selected",
                    "serialization-overflow",
                }
            ),
        )

    def test_report_schema_publishes_the_same_closed_reason_vocabulary(self) -> None:
        schema = json.loads(
            (
                SCRIPTS.parent
                / "references"
                / "context-observability-report.schema.json"
            ).read_text(encoding="utf-8")
        )
        published = set(
            schema["properties"]["reasons"]["items"]["properties"]["code"]["enum"]
        )
        item_codes = set(
            schema["properties"]["items"]["items"]["properties"]["reason_code"][
                "enum"
            ]
        )
        self.assertEqual(published, REASON_CODES)
        self.assertEqual(item_codes, REASON_CODES)

    def test_plan_explanation_uses_stable_reason_codes_without_raw_payload(self) -> None:
        report = explain_plan(self._planned())

        self.assertEqual(validate_context_explanation(report), [])
        reasons = {
            (item["artifact_id"], item["reason_code"])
            for item in report["items"]
        }
        self.assertIn(("artifact:required", "mandatory-required"), reasons)
        self.assertIn(("artifact:selected", "optional-selected"), reasons)
        self.assertIn(
            ("artifact:omitted", "optional-omitted-no-positive-coverage"),
            reasons,
        )
        self.assertIn(
            ("artifact:blocked", "optional-blocked-unauthorized"),
            reasons,
        )
        serialized = json.dumps(report, sort_keys=True)
        self.assertNotIn("PRIVATE", serialized)
        self.assertNotIn('"payload"', serialized)
        self.assertNotIn('"content"', serialized)

    def test_overflow_and_blocked_plan_have_stable_reasons(self) -> None:
        overflow = plan_context(
            self.graph,
            "implement-change",
            [
                _item(
                    "artifact:large",
                    "one two three four five six",
                    requirements=["acceptance"],
                )
            ],
            [],
            {},
            tokenizer=self.tokenizer,
            mode="executable",
            input_limit=3,
            output_reserve=0,
            safety_margin=0,
        )
        overflow_report = explain_plan(overflow)
        blocked_report = explain_plan(
            {
                "status": "blocked",
                "task_node_id": "implement-change",
                "blocked": [
                    {
                        "artifact_id": "artifact:required",
                        "reason": "provenance-incomplete",
                    }
                ],
            }
        )

        self.assertEqual(
            overflow_report["reasons"][0]["code"],
            "mandatory-overflow",
        )
        self.assertEqual(
            blocked_report["items"][0]["reason_code"],
            "mandatory-blocked-provenance-incomplete",
        )
        self.assertEqual(validate_context_explanation(overflow_report), [])
        self.assertEqual(validate_context_explanation(blocked_report), [])

    def test_envelope_explanation_retains_identity_and_digests_not_payload(self) -> None:
        envelope = self._planned()["envelope"]

        report = explain_envelope_record(envelope)
        retained_report = explain_envelope_record(
            {
                "schema_version": 1,
                "envelope_id": envelope["envelope_id"],
                "envelope_digest": envelope["digest"],
                "task_node_id": envelope["task_node_id"],
                "payload_digest": "f" * 64,
                "manifest": envelope["manifest"],
                "budget": envelope["budget"],
                "payload_retained": False,
            }
        )

        self.assertEqual(report["subject"]["envelope_id"], envelope["envelope_id"])
        self.assertEqual(
            report["subject"]["envelope_digest"],
            envelope["digest"],
        )
        self.assertTrue(report["digests"])
        serialized = json.dumps(report, sort_keys=True)
        self.assertNotIn("PRIVATE", serialized)
        self.assertNotIn("payload", serialized)
        self.assertEqual(validate_context_explanation(report), [])
        self.assertEqual(retained_report["digests"]["payload"], "f" * 64)
        self.assertEqual(validate_context_explanation(retained_report), [])

    def test_replay_and_cache_explanations_return_only_reuse_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory) / ".development-graph"
            store = ContextStore(run_dir)
            envelope = self._planned()["envelope"]
            result = seal_document(
                {
                    "schema_version": 1,
                    "envelope_id": envelope["envelope_id"],
                    "task_node_id": envelope["task_node_id"],
                    "status": "completed",
                    "output_artifacts": ["artifact:implementation"],
                    "result_payload": {"private": "PRIVATE RESULT"},
                    "usage": {
                        "input_tokens": 2,
                        "output_tokens": 1,
                        "total_tokens": 3,
                    },
                    "provenance": [
                        {
                            "source": envelope["manifest"][0]["source"],
                            "sha256": envelope["manifest"][0]["sha256"],
                        }
                    ],
                }
            )
            store.record_attempt(
                run_id="run-one",
                node_id="implement-change",
                attempt=1,
                envelope_digest=envelope["digest"],
                result=result,
                accepted=True,
            )

            replay = inspect_replay(
                store,
                run_id="run-one",
                node_id="implement-change",
                envelope_digest=envelope["digest"],
            )
            miss = explain_cache(
                {
                    "hit": False,
                    "reason": "exact-key-miss",
                    "result_key": "a" * 64,
                    "components": {"private": "PRIVATE COMPONENT"},
                }
            )
            hit = explain_cache(
                {
                    "hit": True,
                    "reason": "exact-key-hit",
                    "result_key": "b" * 64,
                    "result": result,
                    "components": {"private": "PRIVATE COMPONENT"},
                }
            )

        self.assertEqual(replay["reasons"][0]["code"], "replay-accepted-match")
        self.assertEqual(miss["reasons"][0]["code"], "cache-exact-key-miss")
        self.assertEqual(hit["reasons"][0]["code"], "cache-exact-key-hit")
        for report in (replay, miss, hit):
            self.assertEqual(validate_context_explanation(report), [])
            self.assertNotIn("PRIVATE", json.dumps(report, sort_keys=True))

    def test_invalidation_explanation_identifies_affected_and_preserved_nodes(self) -> None:
        unrelated = copy.deepcopy(self.graph["nodes"][0])
        unrelated["id"] = "unrelated-review"
        unrelated["outputs"][0]["artifact"] = "artifact:unrelated"
        self.graph["nodes"].append(unrelated)
        report = explain_invalidation(self.graph, {"artifact:context"})

        self.assertIn("inspect-context", report["affected_nodes"])
        self.assertIn("implement-change", report["affected_nodes"])
        self.assertNotIn("unrelated-review", report["affected_nodes"])
        self.assertIn("unrelated-review", report["unaffected_nodes"])
        self.assertEqual(
            report["reasons"][0]["code"],
            "invalidation-artifact-changed",
        )
        self.assertEqual(validate_context_explanation(report), [])

    def test_validator_rejects_raw_payload_credentials_and_absolute_paths(self) -> None:
        report = explain_plan(self._planned())
        report["raw_payload"] = "sk-secretfixturevalue"
        report["items"][0]["source"] = (
            Path.cwd().anchor + "host/private/file"
        )

        errors = validate_context_explanation(report)

        self.assertTrue(any("not allowed" in error for error in errors))
        self.assertTrue(any("credential" in error for error in errors))
        self.assertTrue(any("project-relative" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
