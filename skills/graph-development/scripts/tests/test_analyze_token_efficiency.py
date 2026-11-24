from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

from analyze_token_efficiency import analyze_run, analyze_runs  # noqa: E402


class TokenEfficiencyAnalysisTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def _run(
        self,
        *,
        variant: str,
        timed_out: bool = False,
        parse_error: str | None = None,
        exit_code: int | None = 0,
        usage: dict[str, int] | None = None,
    ) -> dict[str, object]:
        run_dir = self.root / variant
        workspace = run_dir / "workspace"
        workspace.mkdir(parents=True)
        artifact = workspace / "report.txt"
        artifact.write_text("durable output\n", encoding="utf-8")
        final_usage = (
            {
                "input_tokens": 100,
                "cached_input_tokens": 40,
                "output_tokens": 20,
            }
            if usage is None
            else usage
        )
        events = [
            {
                "type": "item.completed",
                "item": {
                    "type": "command_execution",
                    "command": "sed -n '1,320p' references/validation-rules.md",
                    "exit_code": 0,
                    "aggregated_output": "reference text",
                },
            },
            {
                "type": "item.completed",
                "item": {
                    "type": "command_execution",
                    "command": "python3 scripts/specctl.py verify",
                    "exit_code": 1,
                    "aggregated_output": "invalid",
                },
            },
            {
                "type": "item.completed",
                "item": {
                    "type": "file_change",
                    "changes": [{"path": "doc/spec.md", "kind": "update"}],
                },
            },
            {
                "type": "item.completed",
                "item": {
                    "type": "command_execution",
                    "command": "apply_patch doc/spec.md",
                    "exit_code": 0,
                    "aggregated_output": "patched",
                },
            },
            {
                "type": "item.completed",
                "item": {
                    "type": "command_execution",
                    "command": "python3 scripts/specctl.py verify",
                    "exit_code": 0,
                    "aggregated_output": "valid",
                },
            },
            {
                "type": "item.completed",
                "item": {
                    "type": "agent_message",
                    "text": "raw private response",
                },
            },
            {
                "type": "turn.completed",
                "usage": {
                    "input_tokens": 80,
                    "cached_input_tokens": 30,
                    "output_tokens": 10,
                },
            },
            {
                "type": "turn.completed",
                "usage": final_usage,
            },
        ]
        (run_dir / "events.jsonl").write_text(
            "\n".join(json.dumps(event) for event in events) + "\n",
            encoding="utf-8",
        )
        return {
            "case_id": "case-output",
            "kind": "output",
            "variant": variant,
            "run_id": f"{variant}-run",
            "run_dir": str(run_dir),
            "exit_code": exit_code,
            "timed_out": timed_out,
            "duration_seconds": 12.5,
            "final_message": "raw private response",
            "usage": final_usage,
            "errors": ["secret diagnostic"],
            "commands": ["secret command"],
            "parse_error": parse_error,
            "assertions": [
                {"type": "contains", "value": "Graph validation: PASS", "passed": True},
                {
                    "type": "contains",
                    "value": "Specification validation: PASS",
                    "passed": True,
                },
            ],
            "files": ["report.txt"],
        }

    def test_analyze_run_extracts_stable_metrics_and_artifact_hashes(self) -> None:
        run = self._run(variant="candidate")
        run["usage"] = {
            "input_tokens": 999,
            "cached_input_tokens": 998,
            "output_tokens": 997,
        }

        result = analyze_run(run)

        self.assertEqual(result["model_cycles"], 2)
        self.assertEqual(
            result["api_usage"],
            {
                "input_tokens": 100,
                "cached_input_tokens": 40,
                "output_tokens": 20,
                "total_tokens": 120,
            },
        )
        self.assertEqual(
            result["tool_calls"],
            {"command_execution": 4, "file_change": 1},
        )
        self.assertEqual(
            result["tool_result_bytes"],
            sum(len(value.encode("utf-8")) for value in ("reference text", "invalid", "patched", "valid")),
        )
        self.assertEqual(result["repair_rounds"], 1)
        self.assertEqual(result["pre_validation_full_reference_reads"], 1)
        self.assertIsNone(result["goal_accounted_tokens"])
        self.assertEqual(result["artifact_count"], 1)
        self.assertEqual(
            result["artifact_sha256"],
            {
                "report.txt": hashlib.sha256(
                    b"durable output\n"
                ).hexdigest()
            },
        )

    def test_analyze_runs_preserves_null_usage_and_counts_failures(self) -> None:
        candidate = self._run(
            variant="candidate",
            timed_out=True,
            parse_error="bad JSONL",
            exit_code=None,
            usage={},
        )
        baseline = self._run(variant="baseline")

        report = analyze_runs([candidate, baseline])

        candidate_summary = report["variants"]["candidate"]["all_runs"]
        self.assertEqual(candidate_summary["goal_accounted_tokens"]["available"], 0)
        self.assertIsNone(candidate_summary["goal_accounted_tokens"]["median"])
        self.assertIsNone(candidate_summary["api_total_tokens"]["median"])
        self.assertEqual(candidate_summary["timeouts"], 1)
        self.assertEqual(candidate_summary["parse_errors"], 1)
        self.assertEqual(candidate_summary["nonzero_exits"], 0)
        self.assertEqual(
            report["output_median_reduction_percent"][
                "pre_validation_full_reference_reads"
            ],
            0.0,
        )

    def test_public_report_excludes_private_trace_and_machine_paths(self) -> None:
        candidate = self._run(variant="candidate")
        baseline = self._run(variant="baseline")

        report = analyze_runs([candidate, baseline])
        serialized = json.dumps(report, sort_keys=True)

        self.assertNotIn(str(self.root), serialized)
        self.assertNotIn("run_dir", serialized)
        self.assertNotIn("raw private response", serialized)
        self.assertNotIn("secret diagnostic", serialized)
        self.assertNotIn("secret command", serialized)
        self.assertEqual(report["variants"]["candidate"]["runs"], 1)
        self.assertEqual(report["variants"]["baseline"]["runs"], 1)


if __name__ == "__main__":
    unittest.main()
