from __future__ import annotations

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class PublicReleaseContractTests(unittest.TestCase):
    def test_public_runtime_is_operator_neutral_and_opt_in(self) -> None:
        skill = (ROOT / "SKILL.md").read_text()
        metadata = (ROOT / "agents" / "openai.yaml").read_text()
        self.assertIn("allow_implicit_invocation: false", metadata)
        self.assertIn("explicit `$graph-development` invocation", skill)
        self.assertIn("operator-neutral project policy", skill)
        self.assertIn("Without explicit invocation or project opt-in", skill)

    def test_single_skill_folder_contains_verification_dependency_closure(self) -> None:
        for relative in ("scripts/verify_distribution.py", "scripts/tests/test_optional_implementation_plan.py",
                         "evals/context-control-v3/runner.py", "evals/context-control-v3/cases.json",
                         "evals/context-control-v4/runner.py", "evals/context-control-v4/cases.json"):
            self.assertTrue((ROOT / relative).is_file(), relative)
        for private in ("development-graph.json", "specification-manifest.json", "decisions.json", "reports"):
            self.assertFalse((ROOT / private).exists(), private)

    def test_license_and_metadata_are_complete(self) -> None:
        license_text = (ROOT / "LICENSE").read_text()
        self.assertIn("Apache License", license_text)
        self.assertIn("Version 2.0, January 2004", license_text)
        self.assertIn("license: Apache-2.0", (ROOT / "SKILL.md").read_text())

    def test_opt_in_preserves_the_current_outcome_scope(self) -> None:
        text = (ROOT / "references" / "project-opt-in.md").read_text()
        for phrase in ("explicitly opts this project into `$graph-development`",
                       "approved outcome and acceptance contract are the scope ceiling",
                       "Recording a graph mutation never promotes its nodes",
                       "return to the requested critical path",
                       "backlog cannot block current-run completion"):
            self.assertIn(phrase, text)

    def test_synthetic_activation_and_containment_criteria_are_preserved(self) -> None:
        spec = json.loads((ROOT / "evals/production-ready/evals.json").read_text())
        cases = {case["id"]: case for case in spec["cases"]}
        expected = {"explicit-invocation", "project-policy-opt-in", "implicit-lifecycle-request",
                    "graph-discussion-near-miss", "requested-dlg-growth", "bounded-run-with-backlog",
                    "mapped-acceptance-dependency", "adjacent-enhancement-deferred",
                    "outcome-complete-with-backlog"}
        self.assertTrue(expected.issubset(cases))
        self.assertTrue(spec["frozen"])
        self.assertEqual(cases["explicit-invocation"]["expected"], "trigger")
        self.assertEqual(cases["implicit-lifecycle-request"]["expected"], "do-not-trigger")
        self.assertTrue(all(case["assertions"] for case in cases.values()))

    def test_distribution_manifest_is_one_relative_file_inventory(self) -> None:
        from distribution_manifest import build_distribution_manifest
        manifest = build_distribution_manifest(ROOT)
        paths = {entry["path"] for entry in manifest["files"]}
        self.assertIn("LICENSE", paths)
        self.assertIn("scripts/graphctl.py", paths)
        self.assertTrue(all(not Path(path).is_absolute() for path in paths))
        self.assertFalse(any("__pycache__" in path for path in paths))

    def test_model_evaluation_credentials_are_not_bundled(self) -> None:
        source = (ROOT / "evals/outcome_first_runner.py").read_text()
        for forbidden in ("auth_link", "auth_file", "CODEX_HOME", "subprocess.run", "run_case("):
            self.assertNotIn(forbidden, source)
        self.assertIn("Provider-free synthetic", source)


if __name__ == "__main__":
    unittest.main()
