from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

from path_contract import (  # noqa: E402
    PathContractError,
    project_root_for_graph,
    resolve_project_path,
    skill_root,
    validate_relative_path,
)


class PathContractTests(unittest.TestCase):
    def test_rejects_nonportable_relative_paths(self) -> None:
        invalid = (
            "/" + "etc/passwd",
            "C" + r":\Users\someone\file.txt",
            "D" + ":/data/file.txt",
            "C" + ":relative/file.txt",
            "~/notes.txt",
            "../outside.txt",
            "inside/../../outside.txt",
            "bad\x00name.txt",
        )
        for value in invalid:
            with self.subTest(value=value):
                with self.assertRaises(PathContractError):
                    validate_relative_path(value)

    def test_accepts_normalized_project_relative_paths(self) -> None:
        self.assertEqual(validate_relative_path("."), ".")
        self.assertEqual(
            validate_relative_path("docs/specification.txt"),
            "docs/specification.txt",
        )

    def test_skill_root_is_derived_from_module_location_not_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            previous = Path.cwd()
            try:
                os.chdir(directory)
                self.assertEqual(skill_root(), SCRIPTS.parent)
            finally:
                os.chdir(previous)

    def test_graph_root_resolves_from_graph_directory_or_explicit_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project with spaces"
            graph_dir = project / "graph"
            explicit = root / "explicit project"
            graph_dir.mkdir(parents=True)
            explicit.mkdir()
            graph_path = graph_dir / "development-graph.json"
            graph_path.write_text("{}\n", encoding="utf-8")
            graph = {"project": {"root": "."}}

            previous = Path.cwd()
            try:
                os.chdir(root)
                self.assertEqual(
                    project_root_for_graph(graph_path, graph),
                    graph_dir.resolve(),
                )
                self.assertEqual(
                    project_root_for_graph(
                        graph_path,
                        graph,
                        explicit_project_root=explicit,
                    ),
                    explicit.resolve(),
                )
            finally:
                os.chdir(previous)

    def test_project_path_rejects_symlink_escape(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            outside = root / "outside"
            project.mkdir()
            outside.mkdir()
            (project / "link").symlink_to(outside, target_is_directory=True)

            with self.assertRaises(PathContractError):
                resolve_project_path(project, "link/escaped.txt")


if __name__ == "__main__":
    unittest.main()
