from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class OutcomeFirstDocumentationTests(unittest.TestCase):
    def test_skill_sets_scope_ceiling_and_return_rule(self) -> None:
        skill = (ROOT / "SKILL.md").read_text(encoding="utf-8").lower()
        for required in [
            "## outcome-first execution contract",
            "approved outcome and acceptance contract are the scope ceiling",
            "durable dlg growth does not expand the current run",
            "return to the current projected critical path",
            "do not recursively repair graph, specification, or governance state",
            "mapped acceptance blocker",
            "outcome change requires explicit authority",
        ]:
            self.assertIn(required, skill)

    def test_operator_docs_distinguish_lifecycle_operations(self) -> None:
        readme = "\n".join(p.read_text(encoding="utf-8") for p in [ROOT / "README.md", *sorted((ROOT / "doc").glob("*.md"))]).lower()
        mutation = (
            ROOT / "references" / "mutation-contract.md"
        ).read_text(encoding="utf-8").lower()
        combined = f"{readme}\n{mutation}"
        for required in [
            "schema-v2 record",
            "graphctl.py promote",
            "graphctl.py defer",
            "graphctl.py semantic-invalidate",
            "graphctl.py migrate-outcome",
            "development-graph.projection.schema.json",
            "development-graph.promotion.schema.json",
            "development-graph.defer.schema.json",
            "development-graph.invalidation.schema.json",
            "development-graph.outcome-migration.schema.json",
            "explicitly authorized successor-run",
            "same-run remains preview-only",
            "preserves source-run bytes",
            "stale bindings",
            "active leases",
            "duplicate successor destinations",
        ]:
            self.assertIn(required, combined)

    def test_runtime_docs_pin_execution_completion_and_episode_progress(self) -> None:
        runtime = (
            ROOT / "references" / "runtime-contract.md"
        ).read_text(encoding="utf-8").lower()
        graph = (
            ROOT / "references" / "graph-contract.md"
        ).read_text(encoding="utf-8").lower()
        rules = (
            ROOT / "references" / "validation-rules.md"
        ).read_text(encoding="utf-8").lower()
        combined = f"{runtime}\n{graph}\n{rules}"
        for required in [
            "only projected nodes are executable",
            "frozen projection",
            "outcome_contract",
            "dlg_backlog_nodes",
            "needs_human_episodes",
            "progress_node_ids",
            "do not contribute to outcome completion",
            "semantic dependencies, not scheduling edges",
        ]:
            self.assertIn(required, combined)

    def test_instruction_promises_name_executable_regression_anchors(self) -> None:
        runtime = (
            ROOT / "references" / "runtime-contract.md"
        ).read_text(encoding="utf-8")
        mutation = (
            ROOT / "references" / "mutation-contract.md"
        ).read_text(encoding="utf-8")
        combined = f"{runtime}\n{mutation}"
        for required in [
            "test_completed_run_record_only_addition_preserves_completion",
            "test_repair_episode_survives_fresh_mutation_ids_and_gates_independence",
            "test_shared_acceptance_progress_reactivates_only_owning_episode",
            "test_scheduling_only_rewire_does_not_invalidate_verified_work",
            "test_current_regression_promotion_atomically_reopens_semantic_consumers",
        ]:
            self.assertIn(required, combined)

    def test_prose_matches_gated_uncertainty_episode_and_completion_boundaries(self) -> None:
        skill = (ROOT / "SKILL.md").read_text(encoding="utf-8").lower()
        runtime = (
            ROOT / "references" / "runtime-contract.md"
        ).read_text(encoding="utf-8").lower()
        rules = (
            ROOT / "references" / "validation-rules.md"
        ).read_text(encoding="utf-8").lower()
        combined = f"{skill}\n{runtime}\n{rules}"
        for required in [
            "uncertain enrolled artifact consumers require an impact gate",
            "observe-only definition records do not create or update an episode",
            "a required episode gate authorizes the next operation",
            "accepted mapped evidence progress returns the episode to `active`",
            "controller-computed",
            "operator success-claim audit",
        ]:
            self.assertIn(required, combined)

    def test_schema_v1_to_v2_conversion_is_documented(self) -> None:
        migration = (
            ROOT / "references" / "schema-migrations.md"
        ).read_text(encoding="utf-8").lower()
        for required in [
            "schema-v1 run stays readable",
            "schema-version 2 descriptor",
            "scripts/projectctl.py --migration",
            "preview before apply",
            "actor",
            "authority",
            "refuses active or leased nodes",
            "preserves the exact legacy ledger prefix",
            "restores the exact source bytes and modes",
            "removes a newly created projection",
        ]:
            self.assertIn(required, migration)


if __name__ == "__main__":
    unittest.main()
