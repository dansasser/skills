from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "assets" / "examples" / "run_example.py"


class PublicExampleTests(unittest.TestCase):
    def invoke(self, project: Path, *extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(EXAMPLE), "--project-root", str(project), *extra],
            cwd=project.parent,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            capture_output=True,
            text=True,
            check=False,
        )

    def test_preview_is_read_only_and_apply_requires_exact_review(self) -> None:
        with tempfile.TemporaryDirectory(prefix="public example ") as temporary:
            project = Path(temporary)
            preview = self.invoke(project)
            self.assertEqual(preview.returncode, 0, preview.stderr)
            plan = json.loads(preview.stdout)
            self.assertTrue(plan["dry_run"])
            self.assertTrue(plan["valid"])
            self.assertEqual(list(project.iterdir()), [])
            missing_review = self.invoke(project, "--apply")
            self.assertNotEqual(missing_review.returncode, 0)
            self.assertEqual(list(project.iterdir()), [])
            applied = self.invoke(
                project, "--apply", "--expected-base-digest", plan["base_digest"],
                "--expected-plan-digest", plan["plan_digest"], "--actor", "example-operator",
                "--authority", "local:requested-example",
            )
            self.assertEqual(applied.returncode, 0, applied.stderr)
            for name in (
                "development-graph.json", "development-graph.md",
                "outcome-contract.json", "development-graph.projection.json",
            ):
                self.assertTrue((project / name).is_file(), name)
            self.assertFalse((project / "result.txt").exists())
            self.assertFalse((project / ".development-graph" / "run.json").exists())

    def test_documented_schema_v2_commands_produce_verified_output(self) -> None:
        with tempfile.TemporaryDirectory(prefix="schema two example ") as temporary:
            project = Path(temporary)
            preview = self.invoke(project)
            self.assertEqual(preview.returncode, 0, preview.stderr)
            plan = json.loads(preview.stdout)
            applied = self.invoke(
                project, "--apply", "--expected-base-digest", plan["base_digest"],
                "--expected-plan-digest", plan["plan_digest"], "--actor", "example-operator",
                "--authority", "local:requested-example",
            )
            self.assertEqual(applied.returncode, 0, applied.stderr)
            common = ["--graph", str(project / "development-graph.json"),
                      "--run-dir", str(project / ".development-graph")]
            commands = [
                ["graphctl.py", "init", *common, "--contract", str(project / "outcome-contract.json"),
                 "--projection", str(project / "development-graph.projection.json"), "--actor", "example-operator"],
                ["graph_worker.py", *common, "--node", "write-result", "--worker-id", "example-worker",
                 "--expected-run-version", "1"],
                ["graphctl.py", "status", *common],
                ["graphctl.py", "verify-ledger", *common],
            ]
            for command in commands:
                result = subprocess.run(
                    [sys.executable, str(ROOT / "scripts" / command[0]), *command[1:]],
                    cwd=project, capture_output=True, text=True, check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
            run = json.loads((project / ".development-graph" / "run.json").read_text())
            self.assertEqual(run["schema_version"], 2)
            self.assertEqual(run["status"], "completed")
            self.assertEqual(run["nodes"]["write-result"]["state"], "verified")
            self.assertEqual((project / "result.txt").read_text(), "Graph Development example complete\n")

    def test_example_refuses_the_bundled_skill_directory(self) -> None:
        result = self.invoke(ROOT)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("outside the installed skill", result.stderr)

    def test_existing_result_is_not_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            target = project / "result.txt"
            target.write_text("existing work\n")
            result = self.invoke(project)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("already exists", result.stderr)
            self.assertEqual(target.read_text(), "existing work\n")


if __name__ == "__main__":
    unittest.main()
