"""Run the complete provider-neutral Graph Development distribution gate."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


SKILL_ROOT = Path(__file__).resolve().parents[1]
NETWORK_GUARD_ROOT = Path(__file__).resolve().parent / "network_guard"
NETWORK_GUARD_VARIABLE = "GRAPH_DEVELOPMENT_NETWORK_GUARD_LOG"

STAGES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "graph-specification",
        (
            "scripts.tests.test_validate_graph",
            "scripts.tests.test_validate_specifications",
            "scripts.tests.test_specification_workflow",
        ),
    ),
    (
        "portability-provider-boundary",
        (
            "scripts.tests.test_distribution_portability",
            "scripts.tests.test_provider_boundary",
        ),
    ),
    ("templates", ("scripts.tests.test_template_registry",)),
    (
        "lifecycle",
        (
            "scripts.tests.test_project_lifecycle",
            "scripts.tests.test_projectctl",
            "scripts.tests.test_new_graph_lifecycle",
            "scripts.tests.test_specctl",
        ),
    ),
    ("migrations", ("scripts.tests.test_schema_migrations",)),
    (
        "context-observability",
        (
            "scripts.tests.test_context_observability",
            "scripts.tests.test_contextctl_observability",
        ),
    ),
)


def _environment(log_path: Path) -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", os.defpath),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONHASHSEED": "0",
        "PYTHONPATH": str(NETWORK_GUARD_ROOT),
        NETWORK_GUARD_VARIABLE: str(log_path),
    }


def _diagnostic(completed: subprocess.CompletedProcess[str]) -> str:
    text = (completed.stderr or completed.stdout).strip()[-4000:]
    return text.replace(str(SKILL_ROOT), "<skill-root>")


def _read_guard_events(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    events: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            events.append(value)
    return events


def _run_guarded(command: list[str]) -> tuple[subprocess.CompletedProcess[str], dict[str, Any]]:
    with tempfile.TemporaryDirectory(prefix="graph-v4-network-guard-") as directory:
        log_path = Path(directory) / "events.jsonl"
        completed = subprocess.run(
            command,
            cwd=SKILL_ROOT,
            env=_environment(log_path),
            check=False,
            capture_output=True,
            text=True,
        )
        events = _read_guard_events(log_path)
    loaded = sum(event.get("kind") == "loaded" for event in events)
    denied = [
        str(event.get("event"))
        for event in events
        if event.get("kind") == "denied"
    ]
    return completed, {
        "loaded_processes": loaded,
        "network_attempts": len(denied),
        "denied_events": denied,
    }


def _stage_result(
    completed: subprocess.CompletedProcess[str],
    guard: dict[str, Any],
    *,
    checks: int,
) -> dict[str, Any]:
    guard_passed = (
        guard["loaded_processes"] >= 1
        and guard["network_attempts"] == 0
    )
    passed = completed.returncode == 0 and guard_passed
    result: dict[str, Any] = {
        "status": "PASS" if passed else "FAIL",
        "checks": checks,
        "network_guard": "PASS" if guard_passed else "FAIL",
    }
    if not passed:
        result["diagnostic"] = _diagnostic(completed)
        result["network_attempts"] = guard["network_attempts"]
    return result


def _run_unittest(test_ids: tuple[str, ...]) -> dict[str, Any]:
    completed, guard = _run_guarded(
        [sys.executable, "-m", "unittest", "-q", *test_ids]
    )
    return _stage_result(completed, guard, checks=len(test_ids))


def _run_frozen(relative_runner: str) -> dict[str, Any]:
    completed, guard = _run_guarded([sys.executable, relative_runner])
    return _stage_result(completed, guard, checks=1)


def _verify_network_guard() -> bool:
    completed, guard = _run_guarded(
        [
            sys.executable,
            "-c",
            "import socket; socket.socket()",
        ]
    )
    return (
        completed.returncode != 0
        and guard["loaded_processes"] >= 1
        and guard["network_attempts"] >= 1
        and "network access denied" in completed.stderr
    )


def verify_distribution() -> dict[str, Any]:
    stages = {
        name: _run_unittest(test_ids)
        for name, test_ids in STAGES
    }
    stages["frozen-v3"] = _run_frozen("evals/context-control-v3/runner.py")
    stages["frozen-v4"] = _run_frozen("evals/context-control-v4/runner.py")
    stage_network_attempts = sum(
        int(stage.get("network_attempts", 0))
        for stage in stages.values()
    )
    guarded_stages = sum(
        stage.get("network_guard") == "PASS"
        for stage in stages.values()
    )
    network_enforced = (
        _verify_network_guard()
        and guarded_stages == len(stages)
        and stage_network_attempts == 0
    )
    provider_boundary_passed = (
        stages["portability-provider-boundary"]["status"] == "PASS"
    )
    status = (
        "PASS"
        if (
            network_enforced
            and all(stage["status"] == "PASS" for stage in stages.values())
        )
        else "FAIL"
    )
    return {
        "schema_version": 1,
        "verification": "graph-development-v4-distribution",
        "status": status,
        "provider_calls": (
            0
            if network_enforced and provider_boundary_passed
            else None
        ),
        "network_used": False if network_enforced else None,
        "network_enforcement": {
            "status": "PASS" if network_enforced else "FAIL",
            "guard": "python-audit-hook",
            "guarded_stages": guarded_stages,
            "network_attempts": stage_network_attempts,
        },
        "forge_required": False,
        "stages": stages,
    }


def main() -> int:
    report = verify_distribution()
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
