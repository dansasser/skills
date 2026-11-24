from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

from causal_episodes import (  # noqa: E402
    apply_repair_cycle,
    preview_repair_cycle,
    record_acceptance_progress,
)


class CausalEpisodeTests(unittest.TestCase):
    def _case(self) -> tuple[dict, dict]:
        run = {
            "run_id": "00000000-0000-4000-8000-000000000001",
            "run_version": 1,
            "last_event_sequence": 1,
            "observed_graph_checksum": "a" * 64,
            "projection": {"checksum": "b" * 64},
            "outcome_contract": {
                "acceptance_ids": ["acceptance:done"],
                "outcome_digest": "c" * 64,
            },
            "episodes": {},
            "nodes": {
                "repair-a": {
                    "state": "planned",
                    "generation": 1,
                    "last_result": None,
                },
                "repair-b": {
                    "state": "planned",
                    "generation": 1,
                    "last_result": None,
                },
            },
        }
        projection = {
            "acceptance_mappings": {
                "acceptance:done": ["repair-a", "repair-b"],
            }
        }
        return run, projection

    def _preview(
        self,
        run: dict,
        projection: dict,
        *,
        episode_id: str,
        operation_id: str,
        evidence: list[str],
        progress_node_ids: list[str],
        gate: dict | None = None,
        authority: str = "authority:operator",
    ) -> dict:
        return preview_repair_cycle(
            run,
            projection,
            operation_kind="promotion",
            operation_id=operation_id,
            causal_episode_id=episode_id,
            classification="existing-requirement",
            evidence=evidence,
            operation_commitment={"operation": operation_id},
            episode_gate=gate,
            acceptance_ids=["acceptance:done"],
            progress_node_ids=progress_node_ids,
            actor="test-worker",
            authority=authority,
        )

    def _gate(
        self,
        run: dict,
        preview: dict,
        decision: str,
        *,
        approver: str = "authority:operator",
        evidence_digest: str | None = None,
    ) -> dict:
        return {
            "decision": decision,
            "run_id": run["run_id"],
            "causal_episode_id": preview["causal_episode_id"],
            "episode_operation_digest": preview[
                "episode_operation_digest"
            ],
            "approver": approver,
            "evidence_digest": (
                evidence_digest
                if evidence_digest is not None
                else preview["expected_gate_evidence_digest"]
            ),
            "approved_at": "2026-08-11T06:00:00Z",
        }

    def test_shared_acceptance_progress_reactivates_only_owning_episode(
        self,
    ) -> None:
        run, projection = self._case()
        first = self._preview(
            run,
            projection,
            episode_id="first-defect",
            operation_id="first-repair",
            evidence=["evidence:first"],
            progress_node_ids=["repair-a"],
        )
        self.assertTrue(first["apply_allowed"], first)
        apply_repair_cycle(run, first, timestamp="2026-08-11T06:00:00Z")

        second_preview = self._preview(
            run,
            projection,
            episode_id="second-defect",
            operation_id="second-repair",
            evidence=["evidence:second"],
            progress_node_ids=["repair-b"],
        )
        self.assertEqual(
            second_preview["gate_required"],
            "approve-independent-episode",
        )
        second = self._preview(
            run,
            projection,
            episode_id="second-defect",
            operation_id="second-repair",
            evidence=["evidence:second"],
            progress_node_ids=["repair-b"],
            gate=self._gate(
                run,
                second_preview,
                "approve-independent-episode",
            ),
        )
        self.assertTrue(second["apply_allowed"], second)
        apply_repair_cycle(run, second, timestamp="2026-08-11T06:01:00Z")

        run["nodes"]["repair-b"]["state"] = "verified"
        run["nodes"]["repair-b"]["last_result"] = {"accepted": "repair-b"}
        updates = record_acceptance_progress(
            run,
            projection,
            event_sequence=2,
        )

        self.assertEqual(
            [item["causal_episode_id"] for item in updates],
            ["second-defect"],
        )
        self.assertEqual(run["episodes"]["first-defect"]["status"], "needs_human")
        self.assertEqual(run["episodes"]["second-defect"]["status"], "active")

    def test_episode_gate_rejects_authority_and_evidence_substitution(
        self,
    ) -> None:
        run, projection = self._case()
        first = self._preview(
            run,
            projection,
            episode_id="first-defect",
            operation_id="first-repair",
            evidence=["evidence:first"],
            progress_node_ids=["repair-a"],
        )
        apply_repair_cycle(run, first, timestamp="2026-08-11T06:00:00Z")
        candidate = self._preview(
            run,
            projection,
            episode_id="second-defect",
            operation_id="second-repair",
            evidence=["evidence:second"],
            progress_node_ids=["repair-b"],
        )

        wrong_approver = self._preview(
            run,
            projection,
            episode_id="second-defect",
            operation_id="second-repair",
            evidence=["evidence:second"],
            progress_node_ids=["repair-b"],
            gate=self._gate(
                run,
                candidate,
                "approve-independent-episode",
                approver="authority:someone-else",
            ),
        )
        self.assertFalse(wrong_approver["apply_allowed"])
        self.assertTrue(
            any("approver" in item for item in wrong_approver["gate_errors"]),
            wrong_approver,
        )

        wrong_evidence = self._preview(
            run,
            projection,
            episode_id="second-defect",
            operation_id="second-repair",
            evidence=["evidence:second"],
            progress_node_ids=["repair-b"],
            gate=self._gate(
                run,
                candidate,
                "approve-independent-episode",
                evidence_digest="0" * 64,
            ),
        )
        self.assertFalse(wrong_evidence["apply_allowed"])
        self.assertTrue(
            any(
                "evidence digest" in item
                for item in wrong_evidence["gate_errors"]
            ),
            wrong_evidence,
        )

        accepted = self._preview(
            run,
            projection,
            episode_id="second-defect",
            operation_id="second-repair",
            evidence=["evidence:second"],
            progress_node_ids=["repair-b"],
            gate=self._gate(
                run,
                candidate,
                "approve-independent-episode",
            ),
        )
        self.assertTrue(accepted["apply_allowed"], accepted)

    def test_equivalent_relabel_is_rejected_and_authority_allows_one_cycle(
        self,
    ) -> None:
        run, projection = self._case()
        first = self._preview(
            run,
            projection,
            episode_id="same-defect",
            operation_id="first-repair",
            evidence=["evidence:same"],
            progress_node_ids=["repair-a"],
        )
        apply_repair_cycle(run, first, timestamp="2026-08-11T06:00:00Z")

        relabeled = self._preview(
            run,
            projection,
            episode_id="fresh-label",
            operation_id="second-repair",
            evidence=["evidence:same"],
            progress_node_ids=["repair-a"],
        )
        self.assertFalse(relabeled["valid"])
        self.assertTrue(
            any(
                "equivalent causal episode" in item
                for item in relabeled["errors"]
            ),
            relabeled,
        )

        blocked = self._preview(
            run,
            projection,
            episode_id="same-defect",
            operation_id="second-repair",
            evidence=["evidence:same"],
            progress_node_ids=["repair-a"],
        )
        self.assertEqual(blocked["gate_required"], "authorize-next-cycle")
        authorized = self._preview(
            run,
            projection,
            episode_id="same-defect",
            operation_id="second-repair",
            evidence=["evidence:same"],
            progress_node_ids=["repair-a"],
            gate=self._gate(
                run,
                blocked,
                "authorize-next-cycle",
            ),
        )
        self.assertTrue(authorized["apply_allowed"], authorized)
        apply_repair_cycle(
            run,
            authorized,
            timestamp="2026-08-11T06:01:00Z",
        )
        self.assertEqual(run["episodes"]["same-defect"]["repair_cycles"], 2)

        blocked_again = self._preview(
            run,
            projection,
            episode_id="same-defect",
            operation_id="third-repair",
            evidence=["evidence:same"],
            progress_node_ids=["repair-a"],
        )
        self.assertFalse(blocked_again["apply_allowed"])
        self.assertEqual(
            blocked_again["gate_required"],
            "authorize-next-cycle",
        )


if __name__ == "__main__":
    unittest.main()
