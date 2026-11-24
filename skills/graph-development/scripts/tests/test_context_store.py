from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "tests"))

from context_core import (  # noqa: E402
    DeterministicWordTokenizer,
    build_result_key,
    delta_affected_nodes,
    early_cutoff,
    plan_context,
    seal_document,
)
from context_store import ContextStore  # noqa: E402
from test_validate_graph import valid_graph  # noqa: E402


def context_item(artifact_id: str, content: str) -> dict[str, object]:
    return {
        "artifact_id": artifact_id,
        "content": content,
        "source": f"docs/{artifact_id.replace(':', '-')}.md",
        "requirements": ["acceptance"],
        "inclusion_class": "direct_input",
    }


def result(envelope: dict[str, object], payload: object | None = None) -> dict[str, object]:
    return seal_document(
        {
            "schema_version": 1,
            "envelope_id": envelope["envelope_id"],
            "task_node_id": envelope["task_node_id"],
            "status": "completed",
            "output_artifacts": ["artifact:implementation"],
            "result_payload": payload if payload is not None else {"summary": "done"},
            "usage": {"input_tokens": 2, "output_tokens": 1, "total_tokens": 3},
            "provenance": [
                {
                    "source": envelope["manifest"][0]["source"],  # type: ignore[index]
                    "sha256": envelope["manifest"][0]["sha256"],  # type: ignore[index]
                }
            ],
        }
    )


def key_components(envelope: dict[str, object]) -> dict[str, object]:
    return {
        "graph_checksum": envelope["graph"]["canonical_sha256"],  # type: ignore[index]
        "checkpoint": "run-1:3",
        "node_contract_digest": "1" * 64,
        "implementation_version": "impl-v1",
        "direct_input_versions": [["artifact:required", "v1"]],
        "envelope_digest": envelope["digest"],
        "selector_version": "weighted-coverage-v1",
        "compactor_version": "none",
        "policy_versions": ["policy-v1"],
        "approval_versions": ["approval-v1"],
        "adapter_identity": "fake/deterministic-capsule/1",
        "tokenizer_identity": "store-word-v1",
        "runtime_config": {"temperature": 0},
    }


class ContextStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.run_dir = self.root / ".development-graph"
        graph = valid_graph()
        graph["project"]["root"] = "."
        graph["project"]["context_version"] = "test:store"
        self.tokenizer = DeterministicWordTokenizer("store-word-v1")
        self.envelope = plan_context(
            graph,
            "implement-change",
            [context_item("artifact:required", "PRIVATE REQUIRED INFORMATION")],
            [],
            {},
            tokenizer=self.tokenizer,
            mode="executable",
            input_limit=100,
            output_reserve=20,
            safety_margin=5,
        )["envelope"]
        self.store = ContextStore(self.run_dir)

    def test_envelope_retention_is_digest_first_by_default(self) -> None:
        path = self.store.store_envelope(self.envelope)
        retained = json.loads(path.read_text())
        text = path.read_text()

        self.assertFalse(retained["payload_retained"])
        self.assertNotIn("payload", retained)
        self.assertNotIn("PRIVATE REQUIRED INFORMATION", text)
        self.assertEqual(retained["envelope_digest"], self.envelope["digest"])
        self.assertFalse(path.is_absolute() and str(self.root) in text)
        self.assertTrue((self.run_dir / "context/context.lock").is_file())
        self.assertEqual(
            (self.run_dir / "context/context.lock").stat().st_mode & 0o777,
            0o600,
        )

    def test_explicit_payload_retention_is_separate_and_opt_in(self) -> None:
        path = self.store.store_envelope(self.envelope, retain_payload=True)
        retained = json.loads(path.read_text())

        self.assertTrue(retained["payload_retained"])
        self.assertEqual(retained["payload"], self.envelope["payload"])

    def test_exact_key_cache_hits_and_every_component_change_misses(self) -> None:
        components = key_components(self.envelope)
        completed = result(self.envelope)
        key = self.store.cache_put(
            components,
            completed,
            clean_recomputation=copy.deepcopy(completed),
        )

        hit = self.store.cache_inspect(components)
        self.assertTrue(hit["hit"], hit)
        self.assertEqual(hit["result"], completed)
        self.assertEqual(key, build_result_key(components))

        for field in components:
            changed = copy.deepcopy(components)
            changed[field] = f"changed-{field}"
            with self.subTest(field=field):
                miss = self.store.cache_inspect(changed)
                self.assertFalse(miss["hit"])
                self.assertEqual(miss["reason"], "exact-key-miss")

    def test_cache_rejects_non_equivalent_clean_recomputation_and_corruption(self) -> None:
        components = key_components(self.envelope)
        with self.assertRaisesRegex(ValueError, "clean recomputation"):
            self.store.cache_put(
                components,
                result(self.envelope),
                clean_recomputation=result(self.envelope, {"summary": "different"}),
            )

        completed = result(self.envelope)
        key = self.store.cache_put(
            components,
            completed,
            clean_recomputation=copy.deepcopy(completed),
        )
        cache_path = self.run_dir / "context/cache" / f"{key}.json"
        cache_path.write_text("{}\n")
        inspection = self.store.cache_inspect(components)
        self.assertFalse(inspection["hit"])
        self.assertEqual(inspection["reason"], "corrupt-cache-record")

    def test_same_run_replay_restores_only_accepted_matching_result(self) -> None:
        completed = result(self.envelope)
        self.store.record_attempt(
            run_id="run-1",
            node_id="implement-change",
            attempt=1,
            envelope_digest=self.envelope["digest"],
            result=completed,
            accepted=True,
        )
        replay = self.store.find_replay(
            run_id="run-1",
            node_id="implement-change",
            envelope_digest=self.envelope["digest"],
        )
        self.assertEqual(replay, completed)

        self.store.record_attempt(
            run_id="run-1",
            node_id="run-tests",
            attempt=1,
            envelope_digest=self.envelope["digest"],
            result=completed,
            accepted=False,
        )
        self.assertIsNone(
            self.store.find_replay(
                run_id="run-1",
                node_id="run-tests",
                envelope_digest=self.envelope["digest"],
            )
        )

    def test_delta_invalidation_and_early_cutoff_preserve_unaffected_work(self) -> None:
        graph = valid_graph()
        unrelated = copy.deepcopy(graph["nodes"][0])
        unrelated["id"] = "unrelated-review"
        unrelated["outputs"][0]["artifact"] = "artifact:unrelated"
        graph["nodes"].append(unrelated)
        affected = delta_affected_nodes(graph, {"artifact:context"})

        self.assertIn("inspect-context", affected)
        self.assertIn("implement-change", affected)
        self.assertIn("run-tests", affected)
        self.assertNotIn("unrelated-review", affected)
        self.assertTrue(early_cutoff("a" * 64, "a" * 64))
        self.assertFalse(early_cutoff("a" * 64, "b" * 64))

    def test_evidence_rejects_secret_markers_and_uses_relative_records(self) -> None:
        with self.assertRaisesRegex(ValueError, "secret"):
            self.store.record_evidence(
                run_id="run-1",
                node_id="implement-change",
                attempt=1,
                evidence={"token": "sk-secretfixturevalue"},
            )
        path = self.store.record_evidence(
            run_id="run-1",
            node_id="implement-change",
            attempt=2,
            evidence={"status": "verified"},
        )
        self.assertTrue(path.is_file())
        self.assertNotIn(str(self.root), path.read_text())

    def test_broker_history_restores_only_valid_counted_request_identities(self) -> None:
        counted = seal_document(
            {
                "schema_version": 1,
                "request_id": "request-counted",
                "request_digest": "a" * 64,
                "parent_envelope_id": self.envelope["envelope_id"],
                "parent_envelope_digest": self.envelope["digest"],
                "task_node_id": self.envelope["task_node_id"],
                "kind": "artifact",
                "decision": "approved",
                "reason": "accepted",
                "request_budget_identity": True,
            }
        )
        uncounted = seal_document(
            {
                "schema_version": 1,
                "request_id": "request-uncounted",
                "request_digest": "b" * 64,
                "parent_envelope_id": self.envelope["envelope_id"],
                "parent_envelope_digest": self.envelope["digest"],
                "task_node_id": self.envelope["task_node_id"],
                "kind": "artifact",
                "decision": "denied",
                "reason": "invalid",
                "request_budget_identity": False,
            }
        )
        self.store.store_broker_record(counted)
        self.store.store_broker_record(uncounted)

        self.assertEqual(
            self.store.broker_history(self.envelope["digest"]),
            {"a" * 64},
        )


if __name__ == "__main__":
    unittest.main()
