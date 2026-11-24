from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
EVAL_ROOT = ROOT / "evals" / "context-control-v3"


class ContextEvaluationTests(unittest.TestCase):
    def test_frozen_core_registry_has_all_26_provider_neutral_cases(self) -> None:
        registry = json.loads((EVAL_ROOT / "cases.json").read_text(encoding="utf-8"))
        cases = registry["cases"]

        self.assertEqual(registry["schema_version"], 1)
        self.assertTrue(registry["frozen"])
        self.assertEqual(len(cases), 26)
        self.assertEqual(
            [case["number"] for case in cases],
            list(range(1, 27)),
        )
        self.assertEqual(len({case["id"] for case in cases}), 26)
        self.assertEqual(
            registry["retained_fidelity_corpus"]["case_count"],
            9,
        )
        for case in cases:
            self.assertTrue(case["family"])
            self.assertTrue(case["title"])
            self.assertIn(
                case["provider_neutral_status"],
                {"testable", "disabled-by-design", "pending-d5"},
            )
            self.assertIsInstance(case["test_ids"], list)

    def test_compaction_and_semantic_retrieval_are_explicitly_disabled(self) -> None:
        context = (ROOT / "references/context-control.md").read_text(encoding="utf-8")
        self.assertIn("Compaction and semantic retrieval remain disabled", context)
        self.assertIn("semantic retrieval", context.lower())

        self.assertIn(
            "Generic filesystem, shell, web, graph-lineage, transcript, retrieval",
            context,
        )

    def test_provider_neutral_runner_passes_without_network_or_live_claim(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            report_path = Path(directory) / "report.json"
            result = subprocess.run(
                [
                    sys.executable,
                    str(EVAL_ROOT / "runner.py"),
                    "--output",
                    str(report_path),
                ],
                cwd=ROOT,
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(report["provider_neutral_status"], "PASS")
            self.assertEqual(report["overall_status"], "PASS")
            self.assertEqual(report["case_counts"]["total"], 26)
            self.assertGreater(report["case_counts"]["pass"], 0)
            self.assertEqual(report["case_counts"]["pending_d5"], 0)
            self.assertGreater(
                report["case_counts"]["disabled_by_design"],
                0,
            )
            serialized = json.dumps(report, sort_keys=True)
            self.assertNotIn(str(ROOT), serialized)
            self.assertNotIn("http://", serialized)
            self.assertNotIn("https://", serialized)


if __name__ == "__main__":
    unittest.main()
