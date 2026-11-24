"""Aggregate portable efficiency evidence from isolated Codex evaluation runs."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from statistics import mean, median
from typing import Iterable


VERIFIER = re.compile(
    r"(?:specctl\.py\s+verify|validate_graph\.py|validate_specifications\.py)"
)
FULL_REFERENCE_READ = re.compile(
    r"(?:cat\s+|sed\s+-n\s+['\"]?1,(?:\$|\d+)p['\"]?\s+)"
    r"[^\n;&|]*references/"
)
WRITE_COMMAND = re.compile(
    r"(?:apply_patch|sed\s+-i|tee\s+|cat\s+[^;&|]*>|"
    r"\b(?:cp|mv|touch|mkdir)\s+)"
)
TOOL_OUTPUT_FIELDS = ("output", "aggregated_output", "result", "content")


def _events(run: dict[str, object]) -> list[dict[str, object]]:
    raw_run_dir = run.get("run_dir")
    if not isinstance(raw_run_dir, str):
        return []
    path = Path(raw_run_dir) / "events.jsonl"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    events: list[dict[str, object]] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def _field_bytes(value: object) -> int:
    if isinstance(value, str):
        return len(value.encode("utf-8"))
    return len(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )


def _trace_metrics(
    events: Iterable[dict[str, object]],
) -> tuple[int, dict[str, int], int, int, int]:
    model_cycles = 0
    tool_calls: Counter[str] = Counter()
    tool_result_bytes = 0
    repair_rounds = 0
    reference_reads = 0
    first_successful_verifier = False
    awaiting_repair = False
    repair_counted = False

    for event in events:
        if event.get("type") == "turn.completed":
            model_cycles += 1
        if event.get("type") != "item.completed":
            continue
        item = event.get("item")
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if not isinstance(item_type, str) or item_type == "agent_message":
            continue
        tool_calls[item_type] += 1
        for field in TOOL_OUTPUT_FIELDS:
            if field in item and item[field] is not None:
                tool_result_bytes += _field_bytes(item[field])

        if item_type == "file_change" and awaiting_repair and not repair_counted:
            repair_rounds += 1
            repair_counted = True
        if item_type != "command_execution":
            continue
        command = item.get("command")
        exit_code = item.get("exit_code")
        if not isinstance(command, str):
            continue
        if not first_successful_verifier and FULL_REFERENCE_READ.search(command):
            reference_reads += 1
        if awaiting_repair and not repair_counted and WRITE_COMMAND.search(command):
            repair_rounds += 1
            repair_counted = True
        if VERIFIER.search(command):
            if exit_code == 0:
                first_successful_verifier = True
                awaiting_repair = False
                repair_counted = False
            else:
                awaiting_repair = True
                repair_counted = False

    return (
        model_cycles,
        dict(sorted(tool_calls.items())),
        tool_result_bytes,
        repair_rounds,
        reference_reads,
    )


def _api_usage(
    run: dict[str, object],
    events: list[dict[str, object]],
) -> dict[str, int | None]:
    usage: object = None
    for event in reversed(events):
        if event.get("type") == "turn.completed" and isinstance(
            event.get("usage"), dict
        ):
            usage = event["usage"]
            break
    if usage is None:
        usage = run.get("usage")
    if not isinstance(usage, dict):
        usage = {}

    def value(name: str) -> int | None:
        candidate = usage.get(name)
        return candidate if isinstance(candidate, int) else None

    input_tokens = value("input_tokens")
    cached_input_tokens = value("cached_input_tokens")
    output_tokens = value("output_tokens")
    total_tokens = (
        input_tokens + output_tokens
        if input_tokens is not None and output_tokens is not None
        else None
    )
    return {
        "input_tokens": input_tokens,
        "cached_input_tokens": cached_input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
    }


def _artifact_hashes(run: dict[str, object]) -> dict[str, str]:
    raw_run_dir = run.get("run_dir")
    if not isinstance(raw_run_dir, str):
        return {}
    workspace = Path(raw_run_dir) / "workspace"
    if not workspace.is_dir():
        return {}
    hashes: dict[str, str] = {}
    for path in sorted(workspace.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(workspace)
        if relative.parts[:1] == (".git",):
            continue
        hashes[relative.as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return hashes


def _assertion_metrics(
    run: dict[str, object],
) -> tuple[int, int, bool | None]:
    assertions = run.get("assertions")
    if not isinstance(assertions, list):
        return 0, 0, None
    passed = 0
    total = 0
    validation_values: list[bool] = []
    for assertion in assertions:
        if not isinstance(assertion, dict) or not isinstance(
            assertion.get("passed"), bool
        ):
            continue
        total += 1
        passed += int(assertion["passed"])
        value = assertion.get("value")
        if isinstance(value, str) and value in {
            "Graph validation: PASS",
            "Specification validation: PASS",
        }:
            validation_values.append(assertion["passed"])
    deterministic_validation = (
        all(validation_values) if validation_values else None
    )
    return passed, total, deterministic_validation


def analyze_run(run: dict[str, object]) -> dict[str, object]:
    events = _events(run)
    (
        model_cycles,
        tool_calls,
        tool_result_bytes,
        repair_rounds,
        reference_reads,
    ) = _trace_metrics(events)
    assertion_passed, assertion_total, deterministic_validation = (
        _assertion_metrics(run)
    )
    artifact_sha256 = _artifact_hashes(run)
    goal_accounted = run.get("goal_accounted_tokens")
    return {
        "case_id": run.get("case_id"),
        "kind": run.get("kind"),
        "variant": run.get("variant"),
        "assertions_passed": assertion_passed,
        "assertions_total": assertion_total,
        "deterministic_validation_passed": deterministic_validation,
        "duration_seconds": run.get("duration_seconds"),
        "api_usage": _api_usage(run, events),
        "goal_accounted_tokens": (
            goal_accounted if isinstance(goal_accounted, int) else None
        ),
        "model_cycles": model_cycles,
        "tool_calls": tool_calls,
        "tool_result_bytes": tool_result_bytes,
        "repair_rounds": repair_rounds,
        "pre_validation_full_reference_reads": reference_reads,
        "timed_out": run.get("timed_out") is True,
        "parse_error": isinstance(run.get("parse_error"), str),
        "nonzero_exit": (
            isinstance(run.get("exit_code"), int)
            and run.get("exit_code") != 0
        ),
        "artifact_count": len(artifact_sha256),
        "artifact_sha256": artifact_sha256,
    }


def _number_summary(values: Iterable[int | float | None]) -> dict[str, object]:
    present = [
        float(value)
        for value in values
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    ]
    return {
        "available": len(present),
        "median": median(present) if present else None,
        "mean": mean(present) if present else None,
        "minimum": min(present) if present else None,
        "maximum": max(present) if present else None,
    }


def _group_summary(runs: list[dict[str, object]]) -> dict[str, object]:
    assertion_total = sum(int(run["assertions_total"]) for run in runs)
    assertion_passed = sum(int(run["assertions_passed"]) for run in runs)
    validation = [
        run["deterministic_validation_passed"]
        for run in runs
        if isinstance(run["deterministic_validation_passed"], bool)
    ]
    tool_calls: Counter[str] = Counter()
    for run in runs:
        raw_calls = run.get("tool_calls")
        if isinstance(raw_calls, dict):
            tool_calls.update(
                {
                    name: count
                    for name, count in raw_calls.items()
                    if isinstance(name, str) and isinstance(count, int)
                }
            )
    return {
        "runs": len(runs),
        "assertions": assertion_total,
        "assertion_pass_rate": (
            assertion_passed / assertion_total if assertion_total else None
        ),
        "deterministic_validation_runs": len(validation),
        "deterministic_validation_pass_rate": (
            sum(validation) / len(validation) if validation else None
        ),
        "goal_accounted_tokens": _number_summary(
            run["goal_accounted_tokens"] for run in runs
        ),
        "api_input_tokens": _number_summary(
            run["api_usage"]["input_tokens"] for run in runs
        ),
        "api_cached_input_tokens": _number_summary(
            run["api_usage"]["cached_input_tokens"] for run in runs
        ),
        "api_output_tokens": _number_summary(
            run["api_usage"]["output_tokens"] for run in runs
        ),
        "api_total_tokens": _number_summary(
            run["api_usage"]["total_tokens"] for run in runs
        ),
        "model_cycles": _number_summary(run["model_cycles"] for run in runs),
        "tool_result_bytes": _number_summary(
            run["tool_result_bytes"] for run in runs
        ),
        "duration_seconds": _number_summary(
            run["duration_seconds"] for run in runs
        ),
        "repair_rounds": _number_summary(run["repair_rounds"] for run in runs),
        "pre_validation_full_reference_reads": _number_summary(
            run["pre_validation_full_reference_reads"] for run in runs
        ),
        "artifact_count": _number_summary(run["artifact_count"] for run in runs),
        "tool_calls": dict(sorted(tool_calls.items())),
        "timeouts": sum(run["timed_out"] is True for run in runs),
        "parse_errors": sum(run["parse_error"] is True for run in runs),
        "nonzero_exits": sum(run["nonzero_exit"] is True for run in runs),
    }


def summarize_variant(runs: list[dict[str, object]]) -> dict[str, object]:
    return {
        "runs": len(runs),
        "all_runs": _group_summary(runs),
        "output_runs": _group_summary(
            [run for run in runs if run.get("kind") == "output"]
        ),
        "trigger_runs": _group_summary(
            [run for run in runs if run.get("kind") == "trigger"]
        ),
    }


def _reduction_percent(
    candidate: int | float | None,
    baseline: int | float | None,
) -> float | None:
    if not isinstance(candidate, (int, float)):
        return None
    if not isinstance(baseline, (int, float)) or baseline == 0:
        return None
    return (float(baseline) - float(candidate)) / float(baseline) * 100.0


def analyze_runs(runs: list[dict[str, object]]) -> dict[str, object]:
    analyzed = [analyze_run(run) for run in runs]
    grouped = {
        variant: [run for run in analyzed if run.get("variant") == variant]
        for variant in ("candidate", "baseline")
    }
    if not grouped["candidate"] or not grouped["baseline"]:
        raise ValueError("candidate and baseline runs are required")
    variants = {
        variant: summarize_variant(variant_runs)
        for variant, variant_runs in grouped.items()
    }
    candidate = variants["candidate"]["output_runs"]
    baseline = variants["baseline"]["output_runs"]

    reductions = {
        "goal_accounted_tokens": _reduction_percent(
            candidate["goal_accounted_tokens"]["median"],
            baseline["goal_accounted_tokens"]["median"],
        ),
        "api_total_tokens": _reduction_percent(
            candidate["api_total_tokens"]["median"],
            baseline["api_total_tokens"]["median"],
        ),
        "model_cycles": _reduction_percent(
            candidate["model_cycles"]["median"],
            baseline["model_cycles"]["median"],
        ),
        "tool_result_bytes": _reduction_percent(
            candidate["tool_result_bytes"]["median"],
            baseline["tool_result_bytes"]["median"],
        ),
        "duration_seconds": _reduction_percent(
            candidate["duration_seconds"]["median"],
            baseline["duration_seconds"]["median"],
        ),
        "repair_rounds": _reduction_percent(
            candidate["repair_rounds"]["median"],
            baseline["repair_rounds"]["median"],
        ),
        "pre_validation_full_reference_reads": _reduction_percent(
            candidate["pre_validation_full_reference_reads"]["median"],
            baseline["pre_validation_full_reference_reads"]["median"],
        ),
    }
    max_repairs = candidate["repair_rounds"]["maximum"]
    gates = {
        "deterministic_assertions_100_percent": (
            variants["candidate"]["all_runs"]["assertion_pass_rate"] == 1.0
        ),
        "goal_accounted_token_reduction_at_least_50_percent": (
            reductions["goal_accounted_tokens"] >= 50.0
            if reductions["goal_accounted_tokens"] is not None
            else None
        ),
        "model_cycle_reduction_at_least_60_percent": (
            reductions["model_cycles"] >= 60.0
            if reductions["model_cycles"] is not None
            else None
        ),
        "tool_result_byte_reduction_at_least_70_percent": (
            reductions["tool_result_bytes"] >= 70.0
            if reductions["tool_result_bytes"] is not None
            else None
        ),
        "no_output_run_exceeds_two_repairs": (
            max_repairs <= 2 if isinstance(max_repairs, (int, float)) else None
        ),
        "blind_human_preference_no_worse": None,
    }
    return {
        "schema_version": 1,
        "runs": analyzed,
        "variants": variants,
        "output_median_reduction_percent": reductions,
        "acceptance_gates": gates,
        "accepted": all(value is True for value in gates.values()),
        "limitations": [
            "Goal-accounted token usage is unavailable when the isolated runner does not emit it.",
            "turn.completed counts isolated execution turns and may not expose internal model cycles.",
            "A genuine blind human preference result requires an external reviewer.",
        ],
    }


def _format_metric(value: object, suffix: str = "") -> str:
    if not isinstance(value, (int, float)):
        return "unavailable"
    return f"{value:,.1f}{suffix}"


def _benchmark_markdown(report: dict[str, object]) -> str:
    variants = report["variants"]
    reductions = report["output_median_reduction_percent"]
    gates = report["acceptance_gates"]
    lines = [
        "# Graph Development V2 Token-Efficiency Benchmark",
        "",
        "## Output-case medians",
        "",
        "| Metric | V2 candidate | V1 baseline | Reduction |",
        "|---|---:|---:|---:|",
    ]
    metrics = [
        ("API total tokens", "api_total_tokens"),
        ("Model cycles", "model_cycles"),
        ("Tool-result bytes", "tool_result_bytes"),
        ("Duration seconds", "duration_seconds"),
        ("Repair rounds", "repair_rounds"),
        (
            "Full reference reads before validation",
            "pre_validation_full_reference_reads",
        ),
    ]
    for label, key in metrics:
        candidate = variants["candidate"]["output_runs"][key]["median"]
        baseline = variants["baseline"]["output_runs"][key]["median"]
        reduction = reductions.get(key)
        lines.append(
            f"| {label} | {_format_metric(candidate)} | "
            f"{_format_metric(baseline)} | "
            f"{_format_metric(reduction, '%')} |"
        )
    lines.extend(
        [
            "",
            "## Acceptance gates",
            "",
        ]
    )
    for name, value in gates.items():
        status = "PASS" if value is True else "FAIL" if value is False else "UNAVAILABLE"
        lines.append(f"- `{name}`: {status}")
    lines.extend(
        [
            "",
            f"Overall acceptance: {'PASS' if report['accepted'] else 'NOT PROVEN'}",
            "",
            "## Limitations",
            "",
        ]
    )
    lines.extend(f"- {limitation}" for limitation in report["limitations"])
    lines.append("")
    return "\n".join(lines)


def write_benchmark(
    report: dict[str, object],
    output_dir: str | Path,
) -> tuple[Path, Path]:
    output = Path(output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    json_path = output / "benchmark.json"
    markdown_path = output / "benchmark.md"
    json_path.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    markdown_path.write_text(_benchmark_markdown(report), encoding="utf-8")
    return json_path, markdown_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("runs_json")
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args(argv)
    try:
        runs = json.loads(Path(args.runs_json).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"invalid runs JSON: {exc}") from exc
    if not isinstance(runs, list):
        raise SystemExit("runs JSON must contain a list")
    report = analyze_runs(runs)
    json_path, markdown_path = write_benchmark(report, args.output_dir)
    print(json_path)
    print(markdown_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
