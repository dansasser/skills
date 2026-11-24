from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
EVAL_ROOT = ROOT / "evals" / "context-control-v4"


class ContextEvaluationV4Tests(unittest.TestCase):
    def test_frozen_registry_covers_every_v4_hard_gate(self) -> None:
        registry = json.loads((EVAL_ROOT / "cases.json").read_text(encoding="utf-8"))
        cases = registry["cases"]

        self.assertEqual(registry["schema_version"], 1)
        self.assertTrue(registry["frozen"])
        self.assertEqual(
            set(registry["hard_gates"]),
            {"fidelity", "authority", "provenance", "isolation", "safety"},
        )
        self.assertEqual(registry["comparison_policy"], "no-hard-gate-regression")
        self.assertGreaterEqual(len(cases), 12)
        self.assertEqual(len({case["id"] for case in cases}), len(cases))
        families = {case["family"] for case in cases}
        self.assertTrue(
            {
                "observability",
                "blocked-input",
                "overflow",
                "replay-cache",
                "invalidation",
                "relocation",
                "provider-absent",
                "lifecycle",
                "migration",
                "template",
            }.issubset(families)
        )
        for case in cases:
            self.assertTrue(case["test_ids"])
            self.assertTrue(case["hard_gates"])
            self.assertTrue(set(case["hard_gates"]).issubset(registry["hard_gates"]))

    def test_runner_passes_without_provider_or_network_claim(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "v4-report.json"
            result = subprocess.run(
                [
                    sys.executable,
                    str(EVAL_ROOT / "runner.py"),
                    "--output",
                    str(output),
                ],
                cwd=ROOT,
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "PASS")
            self.assertEqual(report["failed_hard_gates"], [])
            self.assertEqual(report["live_provider_calls"], 0)
            self.assertFalse(report["network_used"])
            self.assertEqual(
                report["case_counts"]["pass"],
                report["case_counts"]["total"],
            )
            serialized = json.dumps(report, sort_keys=True)
            self.assertNotIn(str(ROOT), serialized)
            self.assertNotIn("http://", serialized)
            self.assertNotIn("https://", serialized)


if __name__ == "__main__":
    unittest.main()
