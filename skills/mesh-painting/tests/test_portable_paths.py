"""Portable public command-line paths and process monitoring."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import common


class PortablePaths(unittest.TestCase):
    def test_display_paths_preserve_relative_arguments_and_symlink_traversal(self):
        from meshpaint.__main__ import _display_path

        caller_path = "./scenes/../lighthouse.py"
        self.assertEqual(_display_path(caller_path), caller_path)
        self.assertEqual(_display_path(str(Path.cwd() / "link" / ".." / "view.png")), "link/../view.png")

    def test_discovery_selects_the_mount_for_the_current_pid_namespace(self):
        from meshpaint import procfs

        with tempfile.TemporaryDirectory() as work:
            foreign = Path(work, "other namespace")
            current = Path(work, "active namespace")
            for root, pid in ((foreign, os.getpid() + 1), (current, os.getpid())):
                root.mkdir()
                (root / "self").symlink_to(str(pid))
            entries = [{"target": str(root)} for root in (foreign, current)]
            reply = subprocess.CompletedProcess([], 0, json.dumps({"filesystems": entries}).encode())
            with patch.object(procfs.subprocess, "run", return_value=reply):
                self.assertEqual(procfs.mount.__wrapped__(), current)

    def test_absolute_arguments_produce_relative_report_and_log_paths(self):
        with tempfile.TemporaryDirectory(prefix="painting project ") as work:
            project = Path(work)
            scene = project / "lighthouse.py"
            shutil.copyfile(common.LIGHTHOUSE, scene)
            prefix = project / "pictures" / "coast"
            result = common.run_engine([str(scene), "--out", str(prefix), "--stage", "preview"], cwd=work)
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads((project / "pictures" / "coast_usage.json").read_text())
            image = "pictures/coast_preview.png"
            self.assertEqual(report["scene"], "lighthouse.py")
            self.assertEqual(report["images_kept"], [{"path": image, "written_by_this_run": True}])
            self.assertEqual(report["images_written_by_this_run"], [image])
            self.assertNotIn(work, result.stdout + result.stderr)
            self.assertTrue((project / image).is_file())
            self.assertGreater(report["usage"]["peak_address_space_kb"], 0)

    def test_unavailable_process_monitor_preserves_existing_pid_file(self):
        with tempfile.TemporaryDirectory() as work:
            pid_file = Path(work, "output.pid")
            pid_file.write_text(str(os.getpid()))
            result = common.run_engine(["--stop", "--out", "output"], cwd=work, env={"PATH": work})
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("findmnt", result.stderr)
            self.assertEqual(pid_file.read_text(), str(os.getpid()))
