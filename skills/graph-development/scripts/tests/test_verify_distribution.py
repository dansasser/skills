from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "verify_distribution.py"


class VerifyDistributionTests(unittest.TestCase):
    def test_provider_neutral_verification_composes_all_v4_stages(self) -> None:
        result = subprocess.run(
            [sys.executable, str(SCRIPT)],
            cwd=ROOT.parent,
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["status"], "PASS")
        self.assertEqual(report["provider_calls"], 0)
        self.assertFalse(report["network_used"])
        self.assertEqual(
            report["network_enforcement"],
            {
                "guard": "python-audit-hook",
                "guarded_stages": 8,
                "network_attempts": 0,
                "status": "PASS",
            },
        )
        self.assertEqual(
            set(report["stages"]),
            {
                "graph-specification",
                "portability-provider-boundary",
                "templates",
                "lifecycle",
                "migrations",
                "context-observability",
                "frozen-v3",
                "frozen-v4",
            },
        )
        self.assertTrue(
            all(stage["status"] == "PASS" for stage in report["stages"].values())
        )
        self.assertTrue(
            all(
                stage["network_guard"] == "PASS"
                for stage in report["stages"].values()
            )
        )
        serialized = json.dumps(report, sort_keys=True)
        self.assertNotIn(str(ROOT), serialized)
        self.assertNotIn("http://", serialized)
        self.assertNotIn("https://", serialized)


if __name__ == "__main__":
    unittest.main()
