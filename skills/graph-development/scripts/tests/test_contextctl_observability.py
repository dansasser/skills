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

from context_core import DeterministicWordTokenizer, plan_context  # noqa: E402
from render_graph import render_markdown  # noqa: E402
from test_validate_graph import valid_graph  # noqa: E402


class ContextObservabilityCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.graph = valid_graph()
        self.graph["project"]["root"] = "."
        self.graph["project"]["context_version"] = "test:explain-cli"
        (self.root / "development-graph.json").write_text(
            json.dumps(self.graph, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        (self.root / "development-graph.md").write_text(
            render_markdown(self.graph, "development-graph.json"),
            encoding="utf-8",
        )
        (self.root / "context-plan.json").write_text(
            json.dumps(
                {
                    "artifact_registry": {
                        "artifact:context": {
                            "content": "PRIVATE DIRECT INPUT",
                            "source": "artifacts/context.json",
                            "requirements": ["context-inspected"],
                            "fresh": True,
                            "authorized": True,
                            "provenance_complete": True,
                        }
                    },
                    "mandatory_extras": [],
                    "optional_candidates": [
                        {
                            "artifact_id": "artifact:optional",
                            "content": "PRIVATE OPTIONAL INPUT",
                            "source": "docs/optional.md",
                            "requirements": ["optional"],
                        }
                    ],
                    "requirement_weights": {"optional": 1},
                    "budget": {
                        "input_limit": 1000,
                        "output_reserve": 20,
                        "safety_margin": 5,
                    },
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        self.command = [sys.executable, str(SCRIPTS / "contextctl.py")]

    def test_explain_plan_is_read_only_and_redacts_content(self) -> None:
        before = {
            path.relative_to(self.root).as_posix(): path.read_bytes()
            for path in self.root.rglob("*")
            if path.is_file()
        }

        result = subprocess.run(
            self.command
            + [
                "explain-plan",
                "--project-root",
                str(self.root),
                "--graph",
                "development-graph.json",
                "--input",
                "context-plan.json",
                "--node",
                "implement-change",
                "--mode",
                "executable",
                "--tokenizer",
                "deterministic-word-v1",
            ],
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["report_type"], "plan")
        self.assertNotIn("PRIVATE", result.stdout)
        after = {
            path.relative_to(self.root).as_posix(): path.read_bytes()
            for path in self.root.rglob("*")
            if path.is_file()
        }
        self.assertEqual(after, before)
        self.assertFalse((self.root / ".development-graph").exists())

    def test_explain_plan_reports_blocked_mandatory_input_with_stable_code(self) -> None:
        config = json.loads(
            (self.root / "context-plan.json").read_text(encoding="utf-8")
        )
        config["artifact_registry"]["artifact:context"]["authorized"] = False
        (self.root / "context-plan.json").write_text(
            json.dumps(config, indent=2) + "\n",
            encoding="utf-8",
        )

        result = subprocess.run(
            self.command
            + [
                "explain-plan",
                "--project-root",
                str(self.root),
                "--graph",
                "development-graph.json",
                "--input",
                "context-plan.json",
                "--node",
                "implement-change",
                "--mode",
                "executable",
                "--tokenizer",
                "deterministic-word-v1",
            ],
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["status"], "blocked")
        self.assertEqual(
            report["items"][0]["reason_code"],
            "mandatory-blocked-unauthorized",
        )
        self.assertFalse((self.root / ".development-graph").exists())

    def test_explain_envelope_is_read_only_and_redacts_payload(self) -> None:
        mandatory = [
            {
                "artifact_id": "artifact:required",
                "content": "PRIVATE ENVELOPE PAYLOAD",
                "source": "docs/required.md",
                "requirements": ["acceptance"],
                "inclusion_class": "direct_input",
            }
        ]
        envelope = plan_context(
            self.graph,
            "implement-change",
            mandatory,
            [],
            {},
            tokenizer=DeterministicWordTokenizer("deterministic-word-v1"),
            mode="executable",
            input_limit=100,
            output_reserve=20,
            safety_margin=5,
        )["envelope"]
        envelope_path = self.root / "envelope.json"
        envelope_path.write_text(json.dumps(envelope) + "\n", encoding="utf-8")
        before = envelope_path.read_bytes()

        result = subprocess.run(
            self.command
            + ["explain-envelope", "--input", str(envelope_path)],
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("PRIVATE", result.stdout)
        self.assertEqual(envelope_path.read_bytes(), before)
        self.assertEqual(json.loads(result.stdout)["report_type"], "envelope")

    def test_explain_reuse_miss_does_not_create_run_directory(self) -> None:
        run_dir = self.root / "missing-run"
        components = self.root / "components.json"
        components.write_text(
            json.dumps(
                {
                    "graph_checksum": "0" * 64,
                    "checkpoint": "run-one:1",
                    "node_contract_digest": "1" * 64,
                    "implementation_version": "v1",
                    "direct_input_versions": [],
                    "envelope_digest": "2" * 64,
                    "selector_version": "weighted-coverage-v1",
                    "compactor_version": "none",
                    "policy_versions": [],
                    "approval_versions": [],
                    "adapter_identity": "adapter-contract",
                    "tokenizer_identity": "deterministic-word-v1",
                    "runtime_config": {},
                }
            )
            + "\n",
            encoding="utf-8",
        )

        result = subprocess.run(
            self.command
            + [
                "explain-reuse",
                "--run-dir",
                str(run_dir),
                "--components",
                str(components),
            ],
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout)["reasons"][0]["code"],
            "cache-exact-key-miss",
        )
        self.assertFalse(run_dir.exists())

    def test_explain_invalidation_reports_nodes_without_writes(self) -> None:
        result = subprocess.run(
            self.command
            + [
                "explain-invalidation",
                "--graph",
                str(self.root / "development-graph.json"),
                "--changed-artifact",
                "artifact:context",
            ],
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["report_type"], "invalidation")
        self.assertIn("implement-change", report["affected_nodes"])


if __name__ == "__main__":
    unittest.main()
