from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "tests"))

from render_graph import render_markdown  # noqa: E402
from test_validate_graph import valid_graph  # noqa: E402


class ContextControlCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        graph = valid_graph()
        graph["project"]["root"] = "."
        graph["project"]["context_version"] = "test:contextctl"
        (self.root / "development-graph.json").write_text(
            json.dumps(graph, indent=2, sort_keys=True) + "\n"
        )
        (self.root / "development-graph.md").write_text(
            render_markdown(graph, "development-graph.json")
        )
        self.plan_input = self.root / "context-plan.json"
        self.plan_input.write_text(
            json.dumps(
                {
                    "artifact_registry": {
                        "artifact:context": {
                            "content": "current graph context",
                            "source": "artifacts/context.json",
                            "requirements": ["context-inspected"],
                            "fresh": True,
                            "authorized": True,
                            "provenance_complete": True
                        }
                    },
                    "mandatory_extras": [
                        {
                            "artifact_id": "artifact:required",
                            "content": "PRIVATE CONTEXT PAYLOAD",
                            "source": "docs/requirements.md",
                            "requirements": ["acceptance"],
                            "inclusion_class": "direct_input",
                        }
                    ],
                    "optional_candidates": [],
                    "requirement_weights": {},
                    "budget": {
                        "input_limit": 1000,
                        "output_reserve": 20,
                        "safety_margin": 5,
                    },
                    "request_policy": {
                        "allowed_kinds": ["artifact"],
                        "allowed_capabilities": [],
                        "max_additional_tokens": 10,
                    },
                },
                indent=2,
            )
            + "\n"
        )
        self.envelope_path = self.root / "envelope.json"
        self.command = [
            sys.executable,
            str(SCRIPTS / "contextctl.py"),
        ]

    def test_plan_writes_explicit_envelope_and_digest_first_store(self) -> None:
        result = subprocess.run(
            self.command
            + [
                "plan",
                "--project-root",
                str(self.root),
                "--graph",
                "development-graph.json",
                "--input",
                "context-plan.json",
                "--run-dir",
                ".development-graph",
                "--node",
                "implement-change",
                "--mode",
                "executable",
                "--tokenizer",
                "deterministic-word-v1",
                "--envelope-output",
                "envelope.json",
            ],
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["status"], "planned")
        self.assertNotIn("PRIVATE CONTEXT PAYLOAD", result.stdout)
        envelope = json.loads(self.envelope_path.read_text())
        retained = (
            self.root
            / ".development-graph/context/envelopes"
            / f"{envelope['digest']}.json"
        ).read_text()
        self.assertNotIn("PRIVATE CONTEXT PAYLOAD", retained)
        self.assertEqual(report["envelope_digest"], envelope["digest"])

    def test_verify_returns_nonzero_for_tampered_envelope(self) -> None:
        self.test_plan_writes_explicit_envelope_and_digest_first_store()
        envelope = json.loads(self.envelope_path.read_text())
        envelope["payload"][0]["content"] = "tampered"
        self.envelope_path.write_text(json.dumps(envelope) + "\n")

        result = subprocess.run(
            self.command
            + [
                "verify",
                "--kind",
                "envelope",
                "--input",
                str(self.envelope_path),
            ],
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertNotEqual(result.returncode, 0)
        report = json.loads(result.stdout)
        self.assertFalse(report["valid"])
        self.assertTrue(report["errors"])

    def test_resolve_request_stores_new_envelope_without_raw_content(self) -> None:
        self.test_plan_writes_explicit_envelope_and_digest_first_store()
        envelope = json.loads(self.envelope_path.read_text())
        request_path = self.root / "request.json"
        from context_core import seal_document

        request_path.write_text(
            json.dumps(
                seal_document(
                    {
                        "schema_version": 1,
                        "request_id": "request-missing",
                        "envelope_id": envelope["envelope_id"],
                        "task_node_id": envelope["task_node_id"],
                        "kind": "artifact",
                        "purpose": "Satisfy acceptance.",
                        "max_tokens": 5,
                        "requested_artifacts": ["artifact:missing"],
                    }
                )
            )
            + "\n"
        )
        broker_path = self.root / "broker.json"
        broker_path.write_text(
            json.dumps(
                {
                    "artifact_registry": {
                        "artifact:missing": {
                            "raw_content": "RAW PRIVATE CONTENT",
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
                    },
                    "capability_registry": {},
                    "read_scope": ["artifact:missing"],
                    "external_scope": [],
                    "max_requests": 1,
                }
            )
            + "\n"
        )
        resumed_path = self.root / "resumed-envelope.json"

        result = subprocess.run(
            self.command
            + [
                "resolve-request",
                "--project-root",
                str(self.root),
                "--envelope",
                "envelope.json",
                "--request",
                "request.json",
                "--broker-config",
                "broker.json",
                "--run-dir",
                ".development-graph",
                "--tokenizer",
                "deterministic-word-v1",
                "--envelope-output",
                "resumed-envelope.json",
            ],
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["decision"], "approved")
        self.assertNotIn("RAW PRIVATE CONTENT", result.stdout)
        resumed = json.loads(resumed_path.read_text())
        self.assertNotEqual(resumed["digest"], envelope["digest"])

        replay = subprocess.run(
            self.command
            + [
                "resolve-request",
                "--project-root",
                str(self.root),
                "--envelope",
                "envelope.json",
                "--request",
                "request.json",
                "--broker-config",
                "broker.json",
                "--run-dir",
                ".development-graph",
                "--tokenizer",
                "deterministic-word-v1",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(replay.returncode, 3, replay.stderr)
        replay_report = json.loads(replay.stdout)
        self.assertEqual(replay_report["decision"], "denied")
        self.assertIn("replay", replay_report["reason"])

    def test_cache_inspect_reports_exact_key_miss(self) -> None:
        components = self.root / "components.json"
        components.write_text(
            json.dumps(
                {
                    "graph_checksum": "0" * 64,
                    "checkpoint": "run-1:1",
                    "node_contract_digest": "1" * 64,
                    "implementation_version": "v1",
                    "direct_input_versions": [],
                    "envelope_digest": "2" * 64,
                    "selector_version": "weighted-coverage-v1",
                    "compactor_version": "none",
                    "policy_versions": [],
                    "approval_versions": [],
                    "adapter_identity": "fake",
                    "tokenizer_identity": "deterministic-word-v1",
                    "runtime_config": {},
                }
            )
            + "\n"
        )

        result = subprocess.run(
            self.command
            + [
                "cache-inspect",
                "--run-dir",
                str(self.root / ".development-graph"),
                "--components",
                str(components),
            ],
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertFalse(report["hit"])
        self.assertEqual(report["reason"], "exact-key-miss")


if __name__ == "__main__":
    unittest.main()
