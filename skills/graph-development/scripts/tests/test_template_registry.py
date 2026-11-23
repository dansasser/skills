from __future__ import annotations

import json
import copy
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from new_graph import build_graph  # noqa: E402
from render_graph import render_markdown  # noqa: E402
from template_registry import (  # noqa: E402
    TemplateRegistryError,
    _validate_template,
    load_registry,
    load_template,
    project_files,
    template_ids,
)
from validate_graph import validate_graph  # noqa: E402
from validate_specifications import validate_specifications  # noqa: E402
try:
    from fixtures import apply_new_graph  # noqa: E402
except ModuleNotFoundError:
    from scripts.tests.fixtures import apply_new_graph  # noqa: E402


EXPECTED_TEMPLATE_IDS = {
    "change",
    "diagnosis-repair",
    "discovery-to-delivery",
    "migration-release",
    "specification-to-delivery",
}


class TemplateRegistryTests(unittest.TestCase):
    def test_registry_preserves_public_ids_and_versioned_relative_assets(self) -> None:
        registry = load_registry()

        self.assertEqual(set(template_ids()), EXPECTED_TEMPLATE_IDS)
        self.assertEqual(registry["schema_version"], 1)
        for record in registry["templates"]:
            self.assertEqual(record["current_version"], 1)
            self.assertFalse(Path(record["asset"]).is_absolute())
            self.assertNotIn("..", Path(record["asset"]).parts)
            self.assertEqual(
                record["asset"],
                Path(
                    "assets",
                    "templates",
                    record["id"],
                    "v1",
                    "template.json",
                ).as_posix(),
            )

    def test_registry_loads_from_relocated_skill_root_and_unrelated_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            relocated = (
                root
                / "arbitrary vendor"
                / "custom runtime"
                / "skills with spaces"
                / "graph-development"
            )
            caller = root / "unrelated caller cwd"
            (relocated / "scripts").mkdir(parents=True)
            caller.mkdir()
            shutil.copy2(SCRIPTS / "template_registry.py", relocated / "scripts")
            shutil.copy2(SCRIPTS / "path_contract.py", relocated / "scripts")
            shutil.copytree(ROOT / "assets", relocated / "assets")
            shutil.copytree(ROOT / "references", relocated / "references")

            result = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    (
                        "import json,sys;"
                        f"sys.path.insert(0,{str(relocated / 'scripts')!r});"
                        "from template_registry import template_ids,load_template;"
                        "print(json.dumps({'ids':template_ids(),"
                        "'entry':load_template('change')['entry_mode']}))"
                    ),
                ],
                cwd=caller,
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(set(payload["ids"]), EXPECTED_TEMPLATE_IDS)
            self.assertEqual(payload["entry"], "change")

    def test_every_template_is_portable_and_builds_a_valid_planned_graph(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project_root = Path(directory)
            for template_id in template_ids():
                with self.subTest(template=template_id):
                    template = load_template(template_id)
                    serialized = json.dumps(template, sort_keys=True)
                    self.assertNotIn(str(ROOT), serialized)
                    self.assertNotIn("/" + "opt" + "/", serialized)
                    self.assertNotIn("/" + "root" + "/", serialized)
                    for record in template.get("project_files", []):
                        self.assertFalse(Path(record["path"]).is_absolute())
                        self.assertFalse(Path(record["source"]).is_absolute())
                        self.assertNotIn("..", Path(record["path"]).parts)
                        self.assertNotIn("..", Path(record["source"]).parts)

                    graph = build_graph(
                        project_root,
                        f"{template_id}-fixture",
                        f"{template_id} fixture",
                        template_id,
                        "test:template-matrix",
                    )
                    report = validate_graph(graph, render_markdown(graph))
                    self.assertTrue(report["valid"], report["errors"])
                    self.assertEqual(graph["status"], "draft")
                    self.assertTrue(
                        all(node["state"] == "planned" for node in graph["nodes"])
                    )

    def test_specification_template_scaffolds_composite_valid_unresolved_package(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project_root = Path(directory)
            result = apply_new_graph(
                [
                    sys.executable,
                    str(SCRIPTS / "new_graph.py"),
                    "--project-root",
                    str(project_root),
                    "--output-dir",
                    str(project_root),
                    "--graph-id",
                    "unresolved-product",
                    "--title",
                    "Unresolved Product",
                    "--template",
                    "specification-to-delivery",
                    "--context-version",
                    "conversation:unresolved",
                ],
                cwd=project_root,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            graph = json.loads(
                (project_root / "development-graph.json").read_text(
                    encoding="utf-8"
                )
            )
            manifest = json.loads(
                (project_root / "specification-manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            report = validate_specifications(graph, manifest, project_root)
            self.assertTrue(report["valid"], report["errors"])
            self.assertEqual(manifest["decisions"], [])
            self.assertTrue(
                all(node["state"] == "planned" for node in graph["nodes"])
            )
            self.assertEqual(graph["status"], "draft")

            files = project_files("specification-to-delivery")
            self.assertEqual(
                set(files),
                {
                    "doc/product-spec.md",
                    "doc/constraints-and-risks.md",
                    "doc/architecture-spec.md",
                    "doc/acceptance-spec.md",
                    "doc/open-questions.md",
                    "doc/implementation-plan.md",
                },
            )
            for relative, expected in files.items():
                actual = (project_root / relative).read_bytes()
                self.assertEqual(actual, expected)
                text = actual.decode("utf-8").lower()
                self.assertIn("unresolved", text)
                self.assertNotIn("status: resolved", text)
                self.assertNotIn("status: verified", text)

    def test_project_template_schema_is_distribution_relative(self) -> None:
        schema_path = ROOT / "references" / "project-template.schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))

        self.assertEqual(schema["$schema"], "https://json-schema.org/draft/2020-12/schema")
        self.assertEqual(schema["title"], "Graph Development project template")
        self.assertNotIn(str(ROOT), json.dumps(schema, sort_keys=True))

    def test_runtime_validation_rejects_duplicate_topology_entries(self) -> None:
        cases = []
        duplicate_predecessor = copy.deepcopy(load_template("change"))
        duplicate_predecessor["nodes"][1]["predecessors"] = ["inspect", "inspect"]
        cases.append(duplicate_predecessor)
        duplicate_entry = copy.deepcopy(load_template("change"))
        duplicate_entry["entry_nodes"] = ["inspect", "inspect"]
        cases.append(duplicate_entry)
        duplicate_terminal = copy.deepcopy(load_template("change"))
        duplicate_terminal["terminal_nodes"] = ["finish", "finish"]
        cases.append(duplicate_terminal)

        for malformed in cases:
            with self.subTest(malformed=malformed):
                with self.assertRaises(TemplateRegistryError):
                    _validate_template(malformed, expected_id="change")

    def test_runtime_validation_rejects_malformed_feedback(self) -> None:
        cases = []
        for max_traversals in (0, True):
            malformed = copy.deepcopy(load_template("change"))
            malformed["feedback"][0]["max_traversals"] = max_traversals
            cases.append(malformed)
        empty_exit = copy.deepcopy(load_template("change"))
        empty_exit["feedback"][0]["exit_condition"] = " "
        cases.append(empty_exit)
        self_edge = copy.deepcopy(load_template("change"))
        self_edge["feedback"][0]["to"] = "verify"
        cases.append(self_edge)
        duplicate = copy.deepcopy(load_template("change"))
        duplicate["feedback"].append(copy.deepcopy(duplicate["feedback"][0]))
        cases.append(duplicate)

        for malformed in cases:
            with self.subTest(malformed=malformed):
                with self.assertRaises(TemplateRegistryError):
                    _validate_template(malformed, expected_id="change")

    def test_runtime_validation_rejects_malformed_approval_lineage(self) -> None:
        cases = []
        for mutation in ("empty-artifact", "mismatch", "missing-input", "self-edge"):
            malformed = copy.deepcopy(load_template("migration-release"))
            approval = malformed["approvals"][0]
            if mutation == "empty-artifact":
                approval["artifact"] = " "
            elif mutation == "mismatch":
                approval["artifact"] = "artifact:not-the-gate-output"
            elif mutation == "missing-input":
                malformed["nodes"][5]["predecessors"] = ["migration-dry-run"]
            else:
                approval["to"] = approval["from"]
            cases.append(malformed)
        duplicate = copy.deepcopy(load_template("migration-release"))
        duplicate["approvals"].append(copy.deepcopy(duplicate["approvals"][0]))
        cases.append(duplicate)

        for malformed in cases:
            with self.subTest(malformed=malformed):
                with self.assertRaises(TemplateRegistryError):
                    _validate_template(
                        malformed,
                        expected_id="migration-release",
                    )

    def test_cli_rejects_project_file_symlink_parent_escape_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            outside = root / "outside"
            project.mkdir()
            outside.mkdir()
            (project / "doc").symlink_to(outside, target_is_directory=True)

            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "new_graph.py"),
                    "--project-root",
                    str(project),
                    "--output-dir",
                    str(project),
                    "--graph-id",
                    "escape-attempt",
                    "--title",
                    "Escape Attempt",
                    "--template",
                    "specification-to-delivery",
                    "--context-version",
                    "test:escape",
                ],
                cwd=project,
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("escapes its root through a symlink", result.stderr)
            self.assertFalse((project / "development-graph.json").exists())
            self.assertFalse((project / "development-graph.md").exists())
            self.assertFalse((project / "specification-manifest.json").exists())
            self.assertEqual(list(outside.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
