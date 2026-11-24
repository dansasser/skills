"""Run the provider-neutral preflight for the frozen V3 context cases."""

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


def _load_cases() -> dict[str, Any]:
    return json.loads((EVAL_ROOT / "cases.json").read_text(encoding="utf-8"))


def _run_test(test_id: str) -> dict[str, Any]:
    environment = {
        "PATH": os.environ.get("PATH", os.defpath),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    completed = subprocess.run(
        [sys.executable, "-m", "unittest", "-q", test_id],
        cwd=SKILL_ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    record: dict[str, Any] = {
        "test_id": test_id,
        "status": "PASS" if completed.returncode == 0 else "FAIL",
    }
    if completed.returncode != 0:
        diagnostic = (completed.stderr or completed.stdout).strip()
        record["diagnostic"] = diagnostic[-4000:]
    return record


def run_provider_neutral() -> dict[str, Any]:
    registry = _load_cases()
    unique_test_ids = sorted(
        {
            test_id
            for case in registry["cases"]
            if case["provider_neutral_status"] != "pending-d5"
            for test_id in case["test_ids"]
        }
    )
    test_records = {
        record["test_id"]: record
        for record in (_run_test(test_id) for test_id in unique_test_ids)
    }
    case_records: list[dict[str, Any]] = []
    for case in registry["cases"]:
        configured = case["provider_neutral_status"]
        if configured == "pending-d5":
            status = "PENDING_D5"
        else:
            failed = [
                test_id
                for test_id in case["test_ids"]
                if test_records[test_id]["status"] != "PASS"
            ]
            if failed:
                status = "FAIL"
            elif configured == "disabled-by-design":
                status = "DISABLED_BY_DESIGN"
            else:
                status = "PASS"
        record: dict[str, Any] = {
            "number": case["number"],
            "id": case["id"],
            "family": case["family"],
            "status": status,
            "test_ids": case["test_ids"],
        }
        if case.get("reason"):
            record["reason"] = case["reason"]
        case_records.append(record)

    counts = {
        "total": len(case_records),
        "pass": sum(record["status"] == "PASS" for record in case_records),
        "fail": sum(record["status"] == "FAIL" for record in case_records),
        "disabled_by_design": sum(
            record["status"] == "DISABLED_BY_DESIGN"
            for record in case_records
        ),
        "pending_d5": sum(
            record["status"] == "PENDING_D5"
            for record in case_records
        ),
    }
    provider_neutral_status = "PASS" if counts["fail"] == 0 else "FAIL"
    return {
        "schema_version": 1,
        "evaluation": "context-control-v3-provider-neutral-preflight",
        "contract": registry["contract"],
        "provider_neutral_status": provider_neutral_status,
        "overall_status": (
            "FAIL"
            if provider_neutral_status == "FAIL"
            else (
                "PENDING_D5"
                if counts["pending_d5"]
                else "PASS"
            )
        ),
        "live_provider_calls": 0,
        "live_evaluation_claimed": False,
        "retained_fidelity_corpus": registry["retained_fidelity_corpus"],
        "case_counts": counts,
        "cases": case_records,
        "tests": [test_records[test_id] for test_id in unique_test_ids],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = run_provider_neutral()
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    print(
        json.dumps(
            {
                "provider_neutral_status": report["provider_neutral_status"],
                "overall_status": report["overall_status"],
                "case_counts": report["case_counts"],
                "live_provider_calls": 0,
            },
            sort_keys=True,
        )
    )
    return 0 if report["provider_neutral_status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
