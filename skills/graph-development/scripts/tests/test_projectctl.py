from __future__ import annotations

import base64
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]


class ProjectControlCliTests(unittest.TestCase):
    def _request_path(self, root: Path) -> Path:
        request = {
            "schema_version": 1,
            "operation": "bootstrap",
            "artifacts": [
                {
                    "path": "development-graph.json",
                    "content_base64": base64.b64encode(
                        b'{"project":{"root":"."}}\n'
                    ).decode("ascii"),
                    "mode": 0o600,
                }
            ],
            "metadata": {"source": "cli-test"},
        }
        path = root.parent / "request.json"
        path.write_text(json.dumps(request), encoding="utf-8")
        return path

    def test_preview_is_default_and_writes_nothing_to_project(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            root.mkdir()
            request = self._request_path(root)

            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "projectctl.py"),
                    "--project-root",
                    str(root),
                    "--request",
                    str(request),
                ],
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            plan = json.loads(result.stdout)
            self.assertTrue(plan["valid"])
            self.assertTrue(plan["dry_run"])
            self.assertFalse((root / "development-graph.json").exists())
            self.assertFalse((root / ".development-graph").exists())

    def test_apply_requires_review_digests_actor_and_authority(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            root.mkdir()
            request = self._request_path(root)
            base_command = [
                sys.executable,
                str(SCRIPTS / "projectctl.py"),
                "--project-root",
                str(root),
                "--request",
                str(request),
            ]
            preview = subprocess.run(
                base_command,
                check=False,
                capture_output=True,
                text=True,
            )
            plan = json.loads(preview.stdout)

            missing = subprocess.run(
                base_command + ["--apply"],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(missing.returncode, 0)
            self.assertIn("requires", missing.stderr)

            applied = subprocess.run(
                base_command
                + [
                    "--apply",
                    "--expected-base-digest",
                    plan["base_digest"],
                    "--expected-plan-digest",
                    plan["plan_digest"],
                    "--actor",
                    "cli-tester",
                    "--authority",
                    "test:cli-authority",
                ],
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(applied.returncode, 0, applied.stderr)
            receipt = json.loads(applied.stdout)
            self.assertTrue(receipt["applied"])
            self.assertEqual(receipt["state"], "committed")
            self.assertEqual(
                (root / "development-graph.json").read_bytes(),
                b'{"project":{"root":"."}}\n',
            )

    def test_recovery_requires_current_digest_actor_and_authority(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / "projectctl.py"),
                "--project-root",
                ".",
                "--recover",
                "transaction-id",
            ],
            cwd=tempfile.gettempdir(),
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("requires", result.stderr)


if __name__ == "__main__":
    unittest.main()
