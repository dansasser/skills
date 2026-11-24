from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1]


class NewGraphLifecycleTests(unittest.TestCase):
    def test_preview_writes_nothing_and_apply_requires_exact_review_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            command = [
                sys.executable,
                str(SCRIPTS / "new_graph.py"),
                "--project-root",
                str(root),
                "--output-dir",
                str(root),
                "--graph-id",
                "lifecycle-scaffold",
                "--title",
                "Lifecycle Scaffold",
                "--template",
                "change",
                "--context-version",
                "test:lifecycle",
            ]

            preview = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(preview.returncode, 0, preview.stderr)
            plan = json.loads(preview.stdout)
            self.assertTrue(plan["dry_run"])
            self.assertTrue(plan["valid"])
            self.assertFalse((root / "development-graph.json").exists())
            self.assertFalse((root / ".development-graph").exists())

            missing = subprocess.run(
                command + ["--apply"],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(missing.returncode, 0)
            self.assertIn("requires", missing.stderr)

            applied = subprocess.run(
                command
                + [
                    "--apply",
                    "--expected-base-digest",
                    plan["base_digest"],
                    "--expected-plan-digest",
                    plan["plan_digest"],
                    "--actor",
                    "test-operator",
                    "--authority",
                    "test:new-graph-lifecycle",
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(applied.returncode, 0, applied.stderr)
            receipt = json.loads(applied.stdout)
            self.assertTrue(receipt["applied"])
            self.assertEqual(receipt["state"], "committed")
            self.assertTrue((root / "development-graph.json").is_file())
            serialized = json.dumps(receipt, sort_keys=True)
            self.assertNotIn(str(root), serialized)

    def test_unrestricted_force_option_is_removed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "new_graph.py"),
                    "--project-root",
                    str(root),
                    "--output-dir",
                    str(root),
                    "--graph-id",
                    "no-force",
                    "--title",
                    "No Force",
                    "--template",
                    "change",
                    "--context-version",
                    "test:no-force",
                    "--force",
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("unrecognized arguments: --force", result.stderr)


if __name__ == "__main__":
    unittest.main()
