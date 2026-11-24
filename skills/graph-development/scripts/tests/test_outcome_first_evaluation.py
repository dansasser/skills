from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
EVAL_ROOT = ROOT / "evals"


def load_runner():
    path = EVAL_ROOT / "outcome_first_runner.py"
    spec = importlib.util.spec_from_file_location("outcome_first_runner_contract", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("unable to load outcome-first evaluator")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class OutcomeFirstEvaluationContractTests(unittest.TestCase):
    def test_containment_policy_is_outer_authority_not_runtime_reimplementation(self) -> None:
        path = EVAL_ROOT / "outcome-first-containment.md"
        self.assertTrue(path.is_file(), "candidate containment policy is missing")
        text = path.read_text(encoding="utf-8")
        required = (
            "## Graph Development outcome containment",
            "For a governed software-lifecycle mutation, load Graph Development before acting.",
            "approved outcome",
            "current run projection",
            "durable backlog",
            "Do not create a parallel TLG, PLG, DLG, plan, or governance workflow.",
            "Record or defer unrelated findings.",
            "changing that outcome requires the user's explicit authority",
            "return to the requested critical path",
            "Do not recursively repair graph, specification, or governance state",
            "For excluded or ordinary non-governed work, do not require Graph Development",
        )
        for phrase in required:
            self.assertIn(phrase, text)
        for runtime_detail in ("graphctl.py", "rewrite_graph.py", "development-graph.projection.schema.json"):
            self.assertNotIn(runtime_detail, text)

    def test_case_registry_covers_phase7_activation_authority_and_exclusions(self) -> None:
        spec = json.loads((EVAL_ROOT / "outcome-first.json").read_text(encoding="utf-8"))
        cases = {item["id"]: item for item in spec["cases"]}
        expected = {
            "finite-outcome-adjacent-finding": "projected-runtime",
            "recursive-governance-temptation": "projected-runtime",
            "outcome-change-without-authority": "outcome-authority",
            "governed-project-mutation": "projected-runtime",
            "near-miss-data-chart": "plain",
            "ordinary-non-governed-task": "plain",
        }
        self.assertTrue(set(expected).issubset(cases))
        for case_id, fixture in expected.items():
            case = cases[case_id]
            self.assertEqual(case["fixture"], fixture)
            self.assertIn("candidate", case["policy_modes"])
            self.assertTrue(case["assertions"])
        self.assertIn("baseline", cases["finite-outcome-adjacent-finding"]["policy_modes"])

    def test_runner_composes_named_policy_modes_without_claiming_markers_are_behavior(self) -> None:
        runner = load_runner()
        self.assertEqual(tuple(runner.POLICY_MODES), ("baseline", "candidate"))
        baseline, baseline_parts = runner._compose_policy("baseline")
        candidate, candidate_parts = runner._compose_policy("candidate")
        self.assertIn("POLICY_BASELINE_LOADED", baseline)
        self.assertNotIn("Graph Development outcome containment", baseline)
        self.assertIn("POLICY_CANDIDATE_LOADED", candidate)
        self.assertIn("Graph Development outcome containment", candidate)
        self.assertEqual(baseline_parts["mode"], "baseline")
        self.assertEqual(candidate_parts["mode"], "candidate")
        self.assertNotEqual(baseline_parts["composed_sha256"], candidate_parts["composed_sha256"])

    def test_assertions_use_behavior_instead_of_diagnostic_markers(self) -> None:
        runner = load_runner()
        case = {"assertions": [{"type": "outcome_completed"}]}
        with tempfile.TemporaryDirectory() as directory:
            results = runner._evaluate_assertions(case, {"policy_marker_observed": True,
                                                         "skill_path_observed": True}, Path(directory))
        self.assertFalse(results[0]["passed"])
        with tempfile.TemporaryDirectory() as directory:
            results = runner._evaluate_assertions(case, {"outcome_completed": True}, Path(directory))
        self.assertTrue(results[0]["passed"])

    def test_phase7_matrix_is_closed_and_uses_exact_workspace_change_contracts(self) -> None:
        runner = load_runner()
        expected_matrix = (
            ("finite-outcome-adjacent-finding", "baseline"),
            ("recursive-governance-temptation", "candidate"),
            ("outcome-change-without-authority", "candidate"),
            ("governed-project-mutation", "candidate"),
            ("near-miss-data-chart", "candidate"),
            ("ordinary-non-governed-task", "candidate"),
        )
        self.assertEqual(tuple(runner.PHASE7_MATRIX), expected_matrix)
        spec = json.loads((EVAL_ROOT / "outcome-first.json").read_text(encoding="utf-8"))
        cases = {item["id"]: item for item in spec["cases"]}
        self.assertNotIn("necessary-in-scope-promotion", cases)
        deferred = {item["id"] for item in spec["deferred_cases"]}
        self.assertIn("necessary-in-scope-promotion", deferred)
        for case_id, mode in expected_matrix:
            case = cases[case_id]
            self.assertIn(mode, case["policy_modes"])
            self.assertIsInstance(case["allowed_workspace_changes"], list)
            assertion_types = {item["type"] for item in case["assertions"]}
            self.assertIn("workspace_change_allowlist_satisfied", assertion_types)
            self.assertIn("sed_command_absent", assertion_types)
            self.assertNotIn("policy_marker_observed", assertion_types)
            self.assertNotIn("skill_path_observed", assertion_types)
            self.assertNotIn("skill_path_not_observed", assertion_types)

    def test_outcome_authority_case_has_a_distinct_fixture_and_state_evidence(self) -> None:
        spec = json.loads((EVAL_ROOT / "outcome-first.json").read_text(encoding="utf-8"))
        case = next(
            item for item in spec["cases"]
            if item["id"] == "outcome-change-without-authority"
        )
        self.assertEqual(case["fixture"], "outcome-authority")
        assertion_types = {item["type"] for item in case["assertions"]}
        for required in (
            "graph_definition_stable",
            "contract_stable",
            "projection_stable",
            "promotion_absent",
            "outcome_migration_absent",
            "successor_run_artifacts_absent",
            "outcome_completed",
        ):
            self.assertIn(required, assertion_types)

    def test_evaluator_rejects_broad_roots_traversal_and_source_symlinks(self) -> None:
        runner = load_runner()
        output_parent = Path(tempfile.gettempdir())
        with self.assertRaises(ValueError):
            runner._validate_output_root(output_parent)
        accepted = runner._validate_output_root(
            output_parent / "graph-development-outcome-first-contract"
        )
        self.assertEqual(accepted.name, "graph-development-outcome-first-contract")
        for unsafe in ("../escape", "/absolute", "nested/../../escape", "bad\\path"):
            with self.assertRaises(ValueError):
                runner._project_relative_path(unsafe)
        source = (EVAL_ROOT / "outcome_first_runner.py").read_text(encoding="utf-8")
        self.assertIn("_reject_tree_symlinks", source)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "linked").symlink_to(root / "outside")
            with self.assertRaises(ValueError):
                runner._reject_tree_symlinks(root)

    def test_candidate_composition_removes_baseline_contradiction_and_markers_are_diagnostic(self) -> None:
        baseline_text = (EVAL_ROOT / "outcome-first-baseline-agents.md").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("proves this workspace policy was loaded", baseline_text)
        self.assertIn("acknowledges the staged policy", baseline_text)
        runner = load_runner()
        candidate, _ = runner._compose_policy("candidate")
        self.assertNotIn(
            "This policy intentionally does not decide whether a graph finding is current work",
            candidate,
        )
        self.assertIn(
            "The staged skill owns graph classification and runtime mechanics",
            candidate,
        )
        source = (EVAL_ROOT / "outcome_first_runner.py").read_text(encoding="utf-8")
        self.assertIn('"workspace_change_allowlist_satisfied"', source)
        changes = runner._workspace_changes([{"path": "one", "sha256": "before"}],
                                             [{"path": "one", "sha256": "after"}])
        self.assertTrue(changes)
        self.assertNotIn('if not signals["policy_marker_observed"]', source)


    def test_projected_fixture_prepares_current_immutable_mutation_and_bounded_reads(self) -> None:
        runner = load_runner()
        with tempfile.TemporaryDirectory(
            prefix="graph-development-outcome-first-unit-"
        ) as temporary:
            workspace = Path(temporary)
            fixture = runner._prepare_projected_fixture(workspace)
            run = json.loads(
                (workspace / ".development-graph" / "run.json").read_text(
                    encoding="utf-8"
                )
            )
            mutation_path = workspace / "finding-mutation.json"
            mutation = json.loads(mutation_path.read_text(encoding="utf-8"))
            self.assertEqual(
                run["nodes"]["inspect-context"]["state"], "verified"
            )
            self.assertTrue((workspace / "finding.json").is_file())
            self.assertEqual(mutation["expected_run_version"], run["run_version"])
            self.assertEqual(fixture["before"]["run_version"], run["run_version"])
            self.assertTrue(fixture["preview"]["valid"])
            self.assertEqual(
                fixture["prepared_mutation_sha256"],
                runner._file_sha(mutation_path),
            )

        policy = (EVAL_ROOT / "outcome-first-baseline-agents.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("do not invoke sed", policy)
        self.assertIn("never reread it once inspected", policy)
        self.assertTrue(runner._command_uses_sed("sed -n '1p' requested.txt"))
        absolute_sed = Path(
            Path(tempfile.gettempdir()).anchor, "usr", "bin", "sed"
        )
        self.assertTrue(runner._command_uses_sed(f"{absolute_sed} -n 1p file"))
        self.assertFalse(runner._command_uses_sed("rg -n requested requested.txt"))

        spec = json.loads((EVAL_ROOT / "outcome-first.json").read_text(encoding="utf-8"))
        cases = {item["id"]: item for item in spec["cases"]}
        mutation_cases = (
            "finite-outcome-adjacent-finding",
            "recursive-governance-temptation",
            "governed-project-mutation",
        )
        projected_cases = mutation_cases + ("outcome-change-without-authority",)
        for case_id in mutation_cases:
            allowed = cases[case_id]["allowed_workspace_changes"]
            self.assertIn(".development-graph/lifecycle.lock", allowed)
        for case_id in projected_cases:
            case = cases[case_id]
            self.assertNotIn("context.txt", case["allowed_workspace_changes"])
            self.assertNotIn("finding.json", case["allowed_workspace_changes"])
            self.assertIn("already verified", case["prompt"])

        ordinary = cases["ordinary-non-governed-task"]
        self.assertIn("exact full line", ordinary["prompt"])
        self.assertIn(
            {"type": "contains", "value": "ordinary-value: 42"},
            ordinary["assertions"],
        )

if __name__ == "__main__":
    unittest.main()
