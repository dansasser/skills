from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import json
from pathlib import Path

TESTS = Path(__file__).resolve().parent
SCRIPTS = TESTS.parent
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(TESTS))

from test_validate_graph import valid_graph  # noqa: E402
from distribution_manifest import (  # noqa: E402
    DistributionContractError,
    build_distribution_manifest,
    text_artifact_paths,
)
try:
    from fixtures import apply_new_graph  # noqa: E402
except ModuleNotFoundError:
    from scripts.tests.fixtures import apply_new_graph  # noqa: E402


SKILL_ROOT = Path(__file__).resolve().parents[2]
ABSOLUTE_POSIX_PATH = re.compile(
    r"(?<![A-Za-z0-9.:#<~])/(?!/)[A-Za-z0-9._~-]+"
    r"/[A-Za-z0-9._~*${}-]+"
)
HOST_ROOT_NAMES = (
    "opt",
    "root",
    "home",
    "Users",
    "tmp",
    "var",
    "etc",
    "usr",
    "bin",
    "absolute",
)


def _without_urls(text: str) -> str:
    return re.sub(r"https?://[^\s)\]}>]+", "", text)


class DistributionPortabilityTests(unittest.TestCase):
    def test_portability_scan_distinguishes_home_aliases_from_absolute_paths(self) -> None:
        self.assertIsNone(ABSOLUTE_POSIX_PATH.search("~" + "/" + ".agents/skills"))
        self.assertIsNone(ABSOLUTE_POSIX_PATH.search("$" + "SKILL_DIR/scripts/tool.py"))
        self.assertIsNotNone(ABSOLUTE_POSIX_PATH.search("/" + "host/private/tool.py"))

    def test_manifest_is_exact_relative_and_excludes_runtime_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "scripts").mkdir()
            (root / "scripts" / "tool.py").write_text("print('ok')\n")
            (root / "NOTICE").write_text("portable\n")
            (root / "notes.txt").write_text("portable text\n")
            (root / ".development-graph").mkdir()
            (root / ".development-graph" / "run.json").write_text("{}\n")
            (root / "__pycache__").mkdir()
            (root / "__pycache__" / "tool.pyc").write_bytes(b"cache")

            first = build_distribution_manifest(root)
            second = build_distribution_manifest(root)

            self.assertEqual(first, second)
            self.assertEqual(
                [entry["path"] for entry in first["files"]],
                ["NOTICE", "notes.txt", "scripts/tool.py"],
            )
            self.assertEqual(
                text_artifact_paths(root),
                ["NOTICE", "notes.txt", "scripts/tool.py"],
            )
            self.assertTrue(
                all(
                    set(entry) == {"path", "sha256", "size"}
                    for entry in first["files"]
                )
            )

    def test_manifest_rejects_distribution_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "target.txt").write_text("target\n")
            (root / "link.txt").symlink_to(root / "target.txt")
            with self.assertRaises(DistributionContractError):
                build_distribution_manifest(root)

    def test_manifest_rejects_directory_symlinks_with_excluded_names(self) -> None:
        for excluded_name in (".development-graph", "results"):
            with self.subTest(excluded_name=excluded_name):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    target = root / "target"
                    target.mkdir()
                    (root / excluded_name).symlink_to(
                        target,
                        target_is_directory=True,
                    )
                    with self.assertRaises(DistributionContractError):
                        build_distribution_manifest(root)

    def test_source_tree_satisfies_clean_distribution_contract(self) -> None:
        manifest = build_distribution_manifest(SKILL_ROOT)
        self.assertGreater(len(manifest["files"]), 0)
        self.assertTrue(
            all(
                not Path(entry["path"]).is_absolute()
                for entry in manifest["files"]
            )
        )

    def test_distribution_contains_no_host_specific_paths(self) -> None:
        forbidden = ["/".join(("", name, "")) for name in HOST_ROOT_NAMES]
        forbidden.extend(
            [
                "~" + "/.codex",
                "$" + "CODEX_HOME",
            ]
        )
        violations: list[str] = []
        for relative in text_artifact_paths(SKILL_ROOT):
            path = SKILL_ROOT / relative
            text = _without_urls(path.read_text(encoding="utf-8"))
            for marker in forbidden:
                if marker in text:
                    violations.append(
                        f"{relative} contains {marker!r}"
                    )
            if re.search(r"(?i)\b[a-z]:[\\/]", text):
                violations.append(
                    f"{relative} contains a drive-root path"
                )
            for match in ABSOLUTE_POSIX_PATH.finditer(text):
                violations.append(
                    f"{relative} contains absolute path "
                    f"{match.group(0)!r}"
                )
        self.assertEqual(violations, [])

    def test_helper_invocations_are_relative_to_the_skill_root(self) -> None:
        contract_paths = [
            SKILL_ROOT / "README.md",
            SKILL_ROOT / "references" / "specification-development.md",
            SKILL_ROOT / "scripts" / "render_graph.py",
        ]
        violations: list[str] = []
        for path in contract_paths:
            text = path.read_text(encoding="utf-8")
            for marker in ("<skill-root>/scripts", "GRAPH_SKILL"):
                if marker in text:
                    violations.append(
                        f"{path.relative_to(SKILL_ROOT)} contains {marker!r}"
                    )
        self.assertEqual(violations, [])

    def test_relocated_distribution_runs_without_original_location(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            relocated = (
                root
                / "arbitrary vendor"
                / "custom agent runtime"
                / "skills"
                / "graph-development"
            )
            project = root / "unrelated project"
            shutil.copytree(
                SKILL_ROOT,
                relocated,
                ignore=shutil.ignore_patterns(
                    ".development-graph",
                    "__pycache__",
                    "*.pyc",
                    "results",
                ),
            )
            project.mkdir()

            unrelated_cwd = root / "caller cwd with spaces"
            unrelated_cwd.mkdir()
            scaffold = apply_new_graph(
                [
                    sys.executable,
                    str(relocated / "scripts/new_graph.py"),
                    "--project-root",
                    str(project),
                    "--output-dir",
                    str(project),
                    "--graph-id",
                    "relocated-distribution",
                    "--title",
                    "Relocated Distribution",
                    "--template",
                    "change",
                    "--context-version",
                    "portable",
                ],
                cwd=unrelated_cwd,
            )
            self.assertEqual(scaffold.returncode, 0, scaffold.stderr)
            generated = json.loads(
                (project / "development-graph.json").read_text(encoding="utf-8")
            )
            self.assertEqual(generated["project"]["root"], ".")
            self.assertEqual(generated["project"]["instructions"], [])
            scaffold_report = json.loads(scaffold.stdout)
            self.assertTrue(scaffold_report["applied"])
            self.assertEqual(scaffold_report["state"], "committed")

            validation = subprocess.run(
                [
                    sys.executable,
                    str(relocated / "scripts/validate_graph.py"),
                    str(project / "development-graph.json"),
                    "--markdown",
                    str(project / "development-graph.md"),
                ],
                cwd=unrelated_cwd,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(validation.returncode, 0, validation.stderr)
            self.assertIn("Graph validation: PASS", validation.stdout)

    def test_relocated_distribution_plans_v3_context_without_source_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            relocated = (
                root
                / "arbitrary vendor"
                / "custom agent runtime"
                / "skills"
                / "graph-development"
            )
            project = root / "relocated context project"
            shutil.copytree(
                SKILL_ROOT,
                relocated,
                ignore=shutil.ignore_patterns(
                    ".development-graph",
                    "__pycache__",
                    "*.pyc",
                    "results",
                ),
            )
            project.mkdir()
            graph = valid_graph()
            graph["project"]["root"] = "."
            graph["project"]["instructions"] = []
            graph["project"]["context_version"] = "relocated:v3"
            target = next(
                node for node in graph["nodes"] if node["id"] == "implement-change"
            )
            target["executor"]["kind"] = "agent"
            target["context_control"] = {
                "version": 3,
                "plan_path": "context/implement-change.json",
                "mode": "executable",
                "tokenizer": "deterministic-word-v1",
                "adapter": "fake-deterministic",
                "retention": "digest-only",
            }
            (project / "development-graph.json").write_text(
                json.dumps(graph, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            plan = project / "context/implement-change.json"
            plan.parent.mkdir()
            plan.write_text(
                json.dumps(
                    {
                        "artifact_registry": {
                            "artifact:context": {
                                "content": "portable direct input",
                                "source": "artifacts/context.json",
                                "requirements": ["evidence-exists"],
                                "authorized": True,
                                "fresh": True,
                                "provenance_complete": True,
                            }
                        },
                        "mandatory_extras": [],
                        "optional_candidates": [],
                        "requirement_weights": {},
                        "budget": {
                            "input_limit": 1000,
                            "output_reserve": 100,
                            "safety_margin": 20,
                        },
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )

            result = subprocess.run(
                [
                    sys.executable,
                    "scripts/contextctl.py",
                    "plan",
                    "--project-root",
                    str(project),
                    "--graph",
                    "development-graph.json",
                    "--input",
                    "context/implement-change.json",
                    "--run-dir",
                    ".development-graph",
                    "--node",
                    "implement-change",
                    "--mode",
                    "executable",
                    "--tokenizer",
                    "deterministic-word-v1",
                ],
                cwd=relocated,
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual(report["status"], "planned")
            self.assertNotIn(str(SKILL_ROOT), result.stdout)
            self.assertTrue(
                (project / report["record"]).is_file(),
                report,
            )


if __name__ == "__main__":
    unittest.main()
