"""Run the frozen provider-neutral Graph Development V4 acceptance corpus."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any


EVAL_ROOT = Path(__file__).resolve().parent
SKILL_ROOT = EVAL_ROOT.parents[1]


def _load_registry() -> dict[str, Any]:
    value = json.loads((EVAL_ROOT / "cases.json").read_text(encoding="utf-8"))
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != 1
        or value.get("frozen") is not True
        or value.get("comparison_policy") != "no-hard-gate-regression"
    ):
        raise ValueError("V4 evaluation registry is invalid or not frozen")
    return value


def _run_test(test_id: str) -> dict[str, str]:
    environment = {
        "PATH": os.environ.get("PATH", os.defpath),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONHASHSEED": "0",
    }
    completed = subprocess.run(
        [sys.executable, "-m", "unittest", "-q", test_id],
        cwd=SKILL_ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    record = {
        "test_id": test_id,
        "status": "PASS" if completed.returncode == 0 else "FAIL",
    }
    if completed.returncode != 0:
        record["diagnostic"] = (completed.stderr or completed.stdout).strip()[-4000:]
    return record


def run_frozen_corpus() -> dict[str, Any]:
    registry = _load_registry()
    tests = sorted(
        {
            test_id
            for case in registry["cases"]
            for test_id in case["test_ids"]
        }
    )
    test_records = {record["test_id"]: record for record in map(_run_test, tests)}
    case_records: list[dict[str, Any]] = []
    failed_hard_gates: set[str] = set()
    for case in registry["cases"]:
        failed_tests = [
            test_id
            for test_id in case["test_ids"]
            if test_records[test_id]["status"] != "PASS"
        ]
        status = "FAIL" if failed_tests else "PASS"
        if failed_tests:
            failed_hard_gates.update(case["hard_gates"])
        case_records.append(
            {
                "id": case["id"],
                "family": case["family"],
                "status": status,
                "hard_gates": case["hard_gates"],
                "test_ids": case["test_ids"],
            }
        )
    counts = {
        "total": len(case_records),
        "pass": sum(case["status"] == "PASS" for case in case_records),
        "fail": sum(case["status"] == "FAIL" for case in case_records),
    }
    return {
        "schema_version": 1,
        "evaluation": registry["evaluation"],
        "comparison_policy": registry["comparison_policy"],
        "status": "PASS" if not failed_hard_gates else "FAIL",
        "failed_hard_gates": sorted(failed_hard_gates),
        "live_provider_calls": 0,
        "network_used": False,
        "case_counts": counts,
        "cases": case_records,
        "tests": [test_records[test_id] for test_id in tests],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        report = run_frozen_corpus()
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        print(f"V4 frozen evaluation: FAIL\nERROR {exc}", file=sys.stderr)
        return 1
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    print(
        json.dumps(
            {
                "status": report["status"],
                "case_counts": report["case_counts"],
                "failed_hard_gates": report["failed_hard_gates"],
                "live_provider_calls": 0,
            },
            sort_keys=True,
        )
    )
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
