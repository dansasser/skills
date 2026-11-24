from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

from validate_graph import validate_graph  # noqa: E402
from graph_core import GraphControlError  # noqa: E402
from new_graph import main  # noqa: E402
try:
    from fixtures import apply_new_graph  # noqa: E402
except ModuleNotFoundError:
    from scripts.tests.fixtures import apply_new_graph  # noqa: E402


class NewGraphTests(unittest.TestCase):
    def test_specification_scaffold_reports_deep_module_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stderr = StringIO()
            with mock.patch(
                "specification_workflow.build_specification_scaffold",
                side_effect=GraphControlError("candidate rejected"),
            ), redirect_stderr(stderr):
                result = main(
                    [
                        "--project-root",
                        str(root),
                        "--output-dir",
                        str(root),
                        "--graph-id",
                        "failed-scaffold",
                        "--title",
                        "Failed Scaffold",
                        "--template",
                        "specification-to-delivery",
                        "--context-version",
                        "test:failure",
                    ]
                )

            self.assertEqual(result, 1)
            self.assertIn("candidate rejected", stderr.getvalue())

    def test_scaffold_is_valid_deterministic_and_refuses_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root
            command = [
                sys.executable,
                str(SCRIPTS / "new_graph.py"),
                "--project-root", str(root),
                "--output-dir", str(output),
                "--graph-id", "sample-change",
                "--title", "Sample Change",
                "--template", "change",
                "--context-version", "commit:abc123",
            ]
            first = apply_new_graph(command)
            self.assertEqual(first.returncode, 0, first.stderr)
            graph_path = output / "development-graph.json"
            markdown_path = output / "development-graph.md"
            graph = json.loads(graph_path.read_text(encoding="utf-8"))
            self.assertEqual(graph["project"]["root"], ".")
            self.assertEqual(graph["project"]["instructions"], [])
            self.assertTrue(validate_graph(graph, markdown_path.read_text(encoding="utf-8"))["valid"])
            self.assertNotIn("TODO", graph_path.read_text(encoding="utf-8"))
            before = graph_path.read_bytes()
            second = subprocess.run(command, check=False, capture_output=True, text=True)
            self.assertEqual(second.returncode, 0, second.stderr)
            second_plan = json.loads(second.stdout)
            self.assertEqual(second_plan["changed"], [])
            self.assertEqual(
                set(second_plan["preserved"]),
                {"development-graph.json", "development-graph.md"},
            )
            self.assertEqual(graph_path.read_bytes(), before)

    def test_specification_to_delivery_scaffold_is_branching_and_writes_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root
            command = [
                sys.executable,
                str(SCRIPTS / "new_graph.py"),
                "--project-root", str(root),
                "--output-dir", str(output),
                "--graph-id", "sample-product",
                "--title", "Sample Product",
                "--template", "specification-to-delivery",
                "--context-version", "conversation:approved-seed",
            ]

            first = apply_new_graph(command)

            self.assertEqual(first.returncode, 0, first.stderr)
            graph_path = output / "development-graph.json"
            markdown_path = output / "development-graph.md"
            manifest_path = output / "specification-manifest.json"
            self.assertTrue(manifest_path.is_file())
            graph = json.loads(graph_path.read_text(encoding="utf-8"))
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertTrue(validate_graph(graph, markdown_path.read_text(encoding="utf-8"))["valid"])
            self.assertEqual(manifest["graph_id"], graph["graph_id"])
            self.assertEqual(manifest["graph_version"], graph["graph_version"])
            self.assertEqual(manifest["context_version"], graph["project"]["context_version"])
            self.assertEqual(manifest["specification_root"], "doc")

            edges = {
                (edge["from"], edge["to"], edge["type"], tuple(edge["artifacts"]))
                for edge in graph["edges"]
            }
            self.assertIn(
                (
                    "capture-project-context",
                    "define-product-and-scope",
                    "consumes",
                    ("artifact:project-context",),
                ),
                edges,
            )
            self.assertIn(
                (
                    "capture-project-context",
                    "define-constraints-and-risks",
                    "consumes",
                    ("artifact:project-context",),
                ),
                edges,
            )
            self.assertNotIn(
                (
                    "define-product-and-scope",
                    "define-constraints-and-risks",
                    "consumes",
                    ("doc/product-spec.md",),
                ),
                edges,
            )
            approval_artifact = "artifact:specification-approval"
            self.assertIn(
                (
                    "approve-specification",
                    "author-implementation-plan",
                    "approves",
                    (approval_artifact,),
                ),
                edges,
            )
            self.assertIn(
                (
                    "approve-specification",
                    "implement",
                    "approves",
                    (approval_artifact,),
                ),
                edges,
            )

            before = {
                path.name: path.read_bytes()
                for path in (graph_path, markdown_path, manifest_path)
            }
            second = subprocess.run(command, check=False, capture_output=True, text=True)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(json.loads(second.stdout)["changed"], [])
            self.assertEqual(
                before,
                {
                    path.name: path.read_bytes()
                    for path in (graph_path, markdown_path, manifest_path)
                },
            )

    def test_rejects_output_directory_outside_project_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            outside = root / "outside"
            project.mkdir()

            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "new_graph.py"),
                    "--project-root", str(project),
                    "--output-dir", str(outside),
                    "--graph-id", "outside-output",
                    "--title", "Outside Output",
                    "--template", "specification-to-delivery",
                    "--context-version", "test:outside-output",
                ],
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("outside project root", result.stderr)
            self.assertFalse(outside.exists())

    def test_plan_free_flag_rejects_an_unrelated_template_without_writes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "new_graph.py"),
                    "--project-root", str(root),
                    "--output-dir", str(root),
                    "--graph-id", "scaffold-option",
                    "--title", "Scaffold Option",
                    "--template", "change",
                    "--context-version", "test:scaffold-option",
                    "--no-implementation-plan",
                ],
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(result.returncode, 2)
            self.assertIn(
                "--no-implementation-plan requires --template specification-to-delivery",
                result.stderr,
            )
            self.assertEqual(list(root.iterdir()), [])

    def test_rejects_nested_output_directory_that_would_change_root_authority(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "nested graph"

            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "new_graph.py"),
                    "--project-root", str(root),
                    "--output-dir", str(output),
                    "--graph-id", "nested-output",
                    "--title", "Nested Output",
                    "--template", "change",
                    "--context-version", "test:nested-output",
                ],
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("must equal project root", result.stderr)
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
