from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

try:
    from fixtures import apply_new_graph  # noqa: E402
except ModuleNotFoundError:
    from scripts.tests.fixtures import apply_new_graph  # noqa: E402


class SpecificationControlCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        scaffold = apply_new_graph(
            [
                sys.executable,
                str(SCRIPTS / "new_graph.py"),
                "--project-root",
                str(self.root),
                "--output-dir",
                str(self.root),
                "--graph-id",
                "specctl-fixture",
                "--title",
                "Specctl Fixture",
                "--template",
                "specification-to-delivery",
                "--context-version",
                "test:specctl-fixture",
            ],
        )
        self.assertEqual(scaffold.returncode, 0, scaffold.stderr)

    def _command(self) -> list[str]:
        return [
            sys.executable,
            str(SCRIPTS / "specctl.py"),
            "snapshot",
            "--project-root",
            str(self.root),
            "--graph",
            "development-graph.json",
            "--manifest",
            "specification-manifest.json",
            "--markdown",
            "development-graph.md",
        ]

    def _write_required_specifications(self) -> None:
        manifest = json.loads(
            (self.root / "specification-manifest.json").read_text(encoding="utf-8")
        )
        paths = [
            self.root / document["path"]
            for document in manifest["documents"]
        ]
        paths.append(self.root / manifest["implementation_plan"]["path"])
        for path in paths:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"# {path.stem}\n\nCLI fixture.\n", encoding="utf-8")

    def _write_decisions(self) -> None:
        (self.root / "decisions.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "decisions": [
                        {
                            "id": "d1-cli",
                            "title": "D1 CLI",
                            "question": "Which CLI behavior is required?",
                            "status": "open",
                            "source_path": "doc/decisions/d1-cli.md",
                            "affected_nodes": ["implement-cli"],
                        }
                    ],
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    def test_snapshot_cli_emits_bounded_json(self) -> None:
        result = subprocess.run(
            self._command(),
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertTrue(report["valid"])
        self.assertEqual(report["operation"], "snapshot")
        self.assertLess(len(result.stdout.encode("utf-8")), 2048)
        self.assertEqual(result.stderr, "")

    def test_snapshot_cli_returns_nonzero_and_diagnostics_for_missing_file(self) -> None:
        (self.root / "specification-manifest.json").unlink()

        result = subprocess.run(
            self._command(),
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertNotEqual(result.returncode, 0)
        report = json.loads(result.stdout)
        self.assertFalse(report["valid"])
        self.assertIn("specification-manifest.json", report["summary"]["missing"])
        self.assertIn("specification-manifest.json", result.stderr)

    def test_sync_cli_emits_only_bounded_change_summary(self) -> None:
        self._write_required_specifications()
        self._write_decisions()
        command = [
            sys.executable,
            str(SCRIPTS / "specctl.py"),
            "sync",
            "--project-root",
            str(self.root),
            "--graph",
            "development-graph.json",
            "--manifest",
            "specification-manifest.json",
            "--decisions",
            "decisions.json",
            "--markdown",
            "development-graph.md",
            "--profile",
            "standard",
        ]
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        plan = json.loads(result.stdout)
        self.assertTrue(plan["valid"])
        self.assertTrue(plan["dry_run"])
        self.assertIn("doc/decisions/d1-cli.md", plan["changed"])
        self.assertFalse((self.root / "doc/decisions/d1-cli.md").exists())
        self.assertNotIn('"nodes": [', result.stdout)
        self.assertNotIn("Which CLI behavior is required?", result.stdout)
        self.assertLess(len(result.stdout.encode("utf-8")), 2048)

        applied = subprocess.run(
            command
            + [
                "--apply",
                "--expected-base-digest",
                plan["base_digest"],
                "--expected-plan-digest",
                plan["plan_digest"],
                "--actor",
                "specctl-test",
                "--authority",
                "test:specctl-sync",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(applied.returncode, 0, applied.stderr)
        receipt = json.loads(applied.stdout)
        self.assertTrue(receipt["applied"])
        self.assertTrue((self.root / "doc/decisions/d1-cli.md").is_file())

    def test_verify_cli_emits_bounded_success(self) -> None:
        self._write_required_specifications()
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / "specctl.py"),
                "verify",
                "--project-root",
                str(self.root),
                "--graph",
                "development-graph.json",
                "--manifest",
                "specification-manifest.json",
                "--markdown",
                "development-graph.md",
            ],
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertTrue(report["valid"])
        self.assertEqual(report["operation"], "verify")
        self.assertEqual(report["summary"]["validation"], "PASS")
        self.assertLess(len(result.stdout.encode("utf-8")), 2048)
        self.assertEqual(result.stderr, "")

    def test_verify_cli_groups_failure_by_owner(self) -> None:
        self._write_required_specifications()
        (self.root / "doc/product-spec.md").write_text(
            "# Product\n\nSee [missing](missing.md).\n",
            encoding="utf-8",
        )
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / "specctl.py"),
                "verify",
                "--project-root",
                str(self.root),
                "--graph",
                "development-graph.json",
                "--manifest",
                "specification-manifest.json",
                "--markdown",
                "development-graph.md",
            ],
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertNotEqual(result.returncode, 0)
        report = json.loads(result.stdout)
        self.assertFalse(report["valid"])
        self.assertTrue(any(error.startswith("links:") for error in report["errors"]))
        self.assertIn("ERROR links:", result.stderr)


if __name__ == "__main__":
    unittest.main()
