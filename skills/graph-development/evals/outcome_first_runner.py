"""Provider-free synthetic outcome-containment fixtures and assertion helpers.

This module never invokes an agent, reads credentials or calls a provider. The
frozen cases are test criteria, not a record of current model performance.
"""
from __future__ import annotations
import fnmatch
import hashlib
import inspect
import json
import re
import sys
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

EVAL_ROOT = Path(__file__).resolve().parent
SKILL_ROOT = EVAL_ROOT.parent
SCRIPTS = SKILL_ROOT / "scripts"
TESTS = SCRIPTS / "tests"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(TESTS))
from fixtures import mutation_for, outcome_contract, outcome_projection, runtime_graph
from graph_core import atomic_write_json
from graph_worker import execute_node
from graphctl import initialize_run, load_run
from render_graph import render_markdown
from rewrite_graph import preview_mutation
from test_validate_graph import edge, node

POLICY_MODES = ("baseline", "candidate")
POLICY_MARKERS = {"baseline": "POLICY_BASELINE_LOADED", "candidate": "POLICY_CANDIDATE_LOADED"}
BASELINE_POLICY = EVAL_ROOT / "outcome-first-baseline-agents.md"
CONTAINMENT_POLICY = EVAL_ROOT / "outcome-first-containment.md"
ADJACENT_NODE = "adjacent-audit"
DEFAULT_CASE = "finite-outcome-adjacent-finding"
PHASE7_MATRIX = (
    ("finite-outcome-adjacent-finding", "baseline"),
    ("recursive-governance-temptation", "candidate"),
    ("outcome-change-without-authority", "candidate"),
    ("governed-project-mutation", "candidate"),
    ("near-miss-data-chart", "candidate"),
    ("ordinary-non-governed-task", "candidate"),
)


def _json_sha(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _file_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _text_sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _project_relative_path(value: str) -> Path:
    if not isinstance(value, str) or not value or "\\" in value or ":" in value:
        raise ValueError("evaluation paths must be normalized project-relative paths")
    pure = PurePosixPath(value)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise ValueError("evaluation paths must not be absolute or contain traversal")
    return Path(*pure.parts)


def _validate_output_root(path: Path) -> Path:
    raw = path.expanduser()
    if raw.exists() and raw.is_symlink():
        raise ValueError("evaluation output root must not be a symlink")
    resolved = raw.resolve()
    if not resolved.name.startswith("graph-development-outcome-first-"):
        raise ValueError("evaluation output root must be a dedicated graph-development-outcome-first-* directory")
    parent = resolved.parent
    if not parent.is_dir() or parent.is_symlink():
        raise ValueError("evaluation output parent must be an existing real directory")
    if resolved.exists() and not resolved.is_dir():
        raise ValueError("evaluation output root must be a directory")
    return resolved


def _reject_tree_symlinks(root: Path) -> None:
    if root.is_symlink():
        raise ValueError(f"symlinked tree root is not allowed: {root}")
    links = [path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_symlink()]
    if links:
        raise ValueError(f"evaluation source tree contains symlinks: {links}")


def _compose_policy(policy_mode: str) -> tuple[str, dict[str, Any]]:
    if policy_mode not in POLICY_MODES:
        raise ValueError(f"unsupported policy mode: {policy_mode}")
    baseline = BASELINE_POLICY.read_text(encoding="utf-8")
    parts: dict[str, Any] = {
        "mode": policy_mode,
        "baseline_path": BASELINE_POLICY.name,
        "baseline_sha256": _text_sha(baseline),
    }
    if policy_mode == "baseline":
        composed = baseline
    else:
        containment = CONTAINMENT_POLICY.read_text(encoding="utf-8")
        if baseline.count(POLICY_MARKERS["baseline"]) != 1:
            raise ValueError("baseline policy marker contract changed")
        baseline_division = (
            "The staged skill owns graph classification and runtime mechanics. "
            "Workspace policy owns fixture authority and host continuation; the baseline "
            "adds no outcome-containment rule."
        )
        candidate_division = (
            "The staged skill owns graph classification and runtime mechanics. "
            "The candidate containment block governs outer authority and continuation."
        )
        if baseline.count(baseline_division) != 1:
            raise ValueError("baseline policy responsibility contract changed")
        wrapper = baseline.replace(baseline_division, candidate_division, 1).replace(
            POLICY_MARKERS["baseline"], POLICY_MARKERS["candidate"], 1
        )
        composed = wrapper.rstrip() + "\n\n" + containment.strip() + "\n"
        parts.update(
            {
                "containment_path": CONTAINMENT_POLICY.name,
                "containment_sha256": _text_sha(containment),
            }
        )
    parts["composed_sha256"] = _text_sha(composed)
    return composed, parts


def _tree_sha(root: Path) -> str:
    digest = hashlib.sha256()
    files = sorted(
        path
        for path in root.rglob("*")
        if path.is_file()
        and ".git" not in path.relative_to(root).parts
        and "__pycache__" not in path.relative_to(root).parts
        and path.suffix != ".pyc"
    )
    for path in files:
        relative = path.relative_to(root).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _workspace_manifest(root: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if ".git" in relative.parts:
            continue
        record: dict[str, Any] = {"path": relative.as_posix()}
        if path.is_symlink():
            record.update({"type": "symlink", "target": str(path.readlink())})
        elif path.is_file():
            record.update({"type": "file", "sha256": _file_sha(path), "size": path.stat().st_size})
        elif path.is_dir():
            record.update({"type": "directory"})
        else:
            record.update({"type": "other"})
        records.append(record)
    return records


def _workspace_changes(
    before: list[dict[str, Any]], after: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    old = {item["path"]: item for item in before}
    new = {item["path"]: item for item in after}
    changes: list[dict[str, Any]] = []
    for relative in sorted(set(old) | set(new)):
        if old.get(relative) == new.get(relative):
            continue
        changes.append(
            {
                "path": relative,
                "change": "created" if relative not in old else "deleted" if relative not in new else "modified",
                "before": old.get(relative),
                "after": new.get(relative),
            }
        )
    return changes


def _allowed_workspace_change(path: str, patterns: list[str]) -> bool:
    for pattern in patterns:
        _project_relative_path(pattern.replace("*", "wildcard"))
        if fnmatch.fnmatchcase(path, pattern):
            return True
    return False


def _load_case(case_id: str) -> tuple[dict[str, Any], str]:
    spec_path = EVAL_ROOT / "outcome-first.json"
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    cases = [
        item
        for item in spec.get("cases", [])
        if isinstance(item, dict) and item.get("id") == case_id
    ]
    if len(cases) != 1:
        raise ValueError(f"expected exactly one eval case named {case_id}")
    return cases[0], _file_sha(spec_path)


def _executor(command: str) -> dict[str, Any]:
    return {
        "command": [sys.executable, "-c", command],
        "working_directory": ".",
        "environment": [],
        "evidence_paths": [],
        "capture_limit_bytes": 65536,
    }


def _prepare_projected_fixture(
    workspace: Path,
    *,
    finding_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    graph = runtime_graph(workspace)
    if finding_payload is None:
        finding_payload = {
            "id": "adjacent-audit",
            "classification": "valuable-adjacent-work",
            "required_for_acceptance": False,
            "summary": "Add a future hardening audit.",
        }
    finding_json = json.dumps(finding_payload, sort_keys=True)
    commands = {
        "inspect-context": (
            "from pathlib import Path; "
            "Path('context.txt').write_text('approved finite outcome', encoding='utf-8'); "
            f"Path('finding.json').write_text({finding_json!r}, encoding='utf-8')"
        ),
        "implement-change": (
            "from pathlib import Path; "
            "Path('requested.txt').write_text('requested outcome delivered', encoding='utf-8')"
        ),
        "run-tests": (
            "from pathlib import Path; import json; "
            "assert Path('requested.txt').read_text(encoding='utf-8') == "
            "'requested outcome delivered'; "
            "Path('tests.json').write_text(json.dumps({'ok':True}, sort_keys=True), "
            "encoding='utf-8')"
        ),
        "finish-change": (
            "from pathlib import Path; import json; "
            "assert Path('tests.json').is_file(); "
            "Path('acceptance.json').write_text(json.dumps({'accepted':True}, "
            "sort_keys=True), encoding='utf-8')"
        ),
    }
    for definition in graph["nodes"]:
        definition["executor"]["kind"] = "deterministic"
        definition["execution"].update(_executor(commands[definition["id"]]))

    graph_path = workspace / "development-graph.json"
    run_dir = workspace / ".development-graph"
    atomic_write_json(graph_path, graph)
    (workspace / "development-graph.md").write_text(
        render_markdown(graph, graph_path.name),
        encoding="utf-8",
    )

    contract = outcome_contract(graph)
    projection = outcome_projection(graph)
    signature = inspect.signature(initialize_run)
    init_kwargs: dict[str, Any] = {"actor": "outcome-first-eval"}
    lifecycle = "v1-compatibility"
    if {"outcome_contract", "projection"}.issubset(signature.parameters):
        init_kwargs["outcome_contract"] = contract
        init_kwargs["projection"] = projection
        lifecycle = "v2-projected"
    run = initialize_run(graph_path, run_dir, **init_kwargs)
    inspection = execute_node(
        graph_path,
        run_dir,
        "inspect-context",
        "outcome-first-fixture",
        run["run_version"],
    )
    if inspection.get("state") != "verified":
        raise ValueError(f"fixture inspection did not verify: {inspection}")
    run = load_run(run_dir)

    adjacent = node(
        ADJACENT_NODE,
        "work",
        [],
        [
            {
                "name": "future audit",
                "artifact": "artifact:adjacent-audit",
                "format": "markdown",
            }
        ],
    )
    adjacent["executor"]["kind"] = "deterministic"
    adjacent["execution"].update(
        _executor(
            "from pathlib import Path; "
            "Path('adjacent-audit-implemented.txt').write_text("
            "'this backlog item was executed', encoding='utf-8')"
        )
    )
    operations = [
        {"op": "add-node", "id": ADJACENT_NODE, "value": adjacent},
        {
            "op": "add-edge",
            "id": "tests-before-adjacent-audit",
            "value": edge(
                "tests-before-adjacent-audit",
                "run-tests",
                ADJACENT_NODE,
                "requires",
                [],
            ),
        },
        {
            "op": "add-edge",
            "id": "adjacent-audit-before-finish",
            "value": edge(
                "adjacent-audit-before-finish",
                ADJACENT_NODE,
                "finish-change",
                "requires",
                [],
            ),
        },
    ]
    mutation = mutation_for(
        graph,
        operations[0],
        mutation_id="record-adjacent-audit",
        record_only=True,
        expected_run_version=run["run_version"],
    )
    mutation["rationale"] = (
        "Record a valuable adjacent hardening audit in the durable lifecycle graph."
    )
    mutation["evidence"] = ["finding.json"]
    mutation["operations"] = operations
    atomic_write_json(workspace / "finding-mutation.json", mutation)

    preview = preview_mutation(
        mutation,
        graph_path,
        run_dir,
        actor="outcome-first-eval",
        authority="eval:record-lifecycle-finding",
    )
    if not preview.get("valid"):
        raise ValueError(f"prepared record mutation is invalid: {preview}")

    before = {
        "graph_sha256": _file_sha(graph_path),
        "run_sha256": _file_sha(run_dir / "run.json"),
        "graph_version": graph["graph_version"],
        "run_version": run["run_version"],
        "run_status": run["status"],
        "run_node_ids": sorted(run["nodes"]),
        "outcome_contract_sha256": (
            _json_sha(run["outcome_contract"])
            if isinstance(run.get("outcome_contract"), dict)
            else None
        ),
        "projection_sha256": (
            _json_sha(run["projection"])
            if isinstance(run.get("projection"), dict)
            else None
        ),
    }
    return {
        "lifecycle": lifecycle,
        "preexecuted_node": "inspect-context",
        "finding_sha256": _file_sha(workspace / "finding.json"),
        "prepared_mutation_sha256": _file_sha(workspace / "finding-mutation.json"),
        "before": before,
        "preview": {
            "valid": preview.get("valid"),
            "affected_nodes": preview.get("affected_nodes", []),
            "invalidated_nodes": preview.get("invalidated_nodes", []),
        },
    }


def _prepare_plain_fixture(workspace: Path) -> dict[str, Any]:
    (workspace / "note.txt").write_text(
        "quarterly revenue: Q1=10, Q2=12\nordinary-value: 42\n",
        encoding="utf-8",
    )
    return {"lifecycle": "plain", "before": {}, "preview": None}


def _prepare_outcome_authority_fixture(workspace: Path) -> dict[str, Any]:
    fixture = _prepare_projected_fixture(
        workspace,
        finding_payload={
            "id": "new-hardening-product-outcome",
            "classification": "outcome-change",
            "adds_requirement": "requirement:hardening-program",
            "conflicts_with_non_goal": "non-goal:no-adjacent-hardening-program",
            "required_for_current_acceptance": False,
            "summary": "Create a separately authorized hardening product outcome.",
        },
    )
    (workspace / "finding-mutation.json").unlink()
    fixture["preview"] = None
    fixture["finding_classification"] = "outcome-change"
    return fixture


def _parse_codex_events(lines: Iterable[str]) -> dict[str, Any]:
    final_message: str | None = None
    commands: list[str] = []
    errors: list[str] = []
    usage: dict[str, int] = {}
    completions = 0
    parse_errors: list[str] = []
    for line_number, raw in enumerate(lines, start=1):
        if not raw.strip():
            continue
        try:
            event = json.loads(raw)
        except json.JSONDecodeError as exc:
            parse_errors.append(f"line {line_number}: {exc}")
            continue
        if event.get("type") == "item.completed":
            item = event.get("item")
            if isinstance(item, dict) and item.get("type") == "agent_message":
                if isinstance(item.get("text"), str):
                    final_message = item["text"]
            elif isinstance(item, dict) and item.get("type") == "command_execution":
                if isinstance(item.get("command"), str):
                    commands.append(item["command"])
            elif isinstance(item, dict) and item.get("type") == "error":
                if isinstance(item.get("message"), str):
                    errors.append(item["message"])
        elif event.get("type") == "turn.completed":
            completions += 1
            if event.get("done_reason") == "length":
                parse_errors.append("turn completed because of length")
            if isinstance(event.get("usage"), dict):
                usage = {
                    key: value
                    for key, value in event["usage"].items()
                    if isinstance(key, str) and isinstance(value, int)
                }
        elif event.get("type") == "turn.failed":
            parse_errors.append("turn.failed event")
    return {
        "final_message": final_message,
        "commands": commands,
        "errors": errors,
        "usage": usage,
        "terminal_completions": completions,
        "parse_errors": parse_errors,
    }


def _read_ledger(path: Path) -> tuple[list[dict[str, Any]], list[str]]:
    records: list[dict[str, Any]] = []
    errors: list[str] = []
    if not path.is_file():
        return records, ["event ledger is absent"]
    for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not raw.strip():
            continue
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            errors.append(f"ledger line {line_number}: {exc}")
            continue
        if isinstance(value, dict):
            records.append(value)
        else:
            errors.append(f"ledger line {line_number}: record is not an object")
    return records, errors


def _command_uses_sed(command: str) -> bool:
    return re.search(
        r"(^|[\s\"';&|])(?:/[^\s\"';&|]+/)?sed(?=$|[\s\"';&|])",
        command,
    ) is not None


def _evaluate_assertions(
    case: dict[str, Any],
    signals: dict[str, Any],
    workspace: Path,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    final_text = str(signals.get("final_text", ""))
    direct_signal_types = {
        "graph_recorded",
        "run_excludes_adjacent",
        "contract_stable",
        "projection_stable",
        "graph_definition_stable",
        "outcome_completed",
        "backlog_record_event",
        "no_adjacent_execution_event",
        "parallel_lifecycle_artifacts_absent",
        "unexpected_lifecycle_artifacts_absent",
        "workspace_change_allowlist_satisfied",
        "sed_command_absent",
        "promotion_absent",
        "outcome_migration_absent",
        "promotion_command_absent",
        "outcome_migration_command_absent",
        "successor_run_artifacts_absent",
    }
    for index, assertion in enumerate(case.get("assertions", [])):
        if not isinstance(assertion, dict) or not isinstance(assertion.get("type"), str):
            raise ValueError(f"invalid assertion at index {index}")
        kind = assertion["type"]
        if kind == "contains":
            expected = assertion.get("value")
            passed = isinstance(expected, str) and expected in final_text
            observed: Any = expected if passed else final_text[-500:]
        elif kind == "not_contains":
            expected = assertion.get("value")
            passed = isinstance(expected, str) and expected not in final_text
            observed = final_text[-500:]
        elif kind in {"file_exists", "file_absent"}:
            relative_value = assertion.get("path")
            if not isinstance(relative_value, str):
                raise ValueError(f"invalid path assertion at index {index}")
            relative = _project_relative_path(relative_value)
            exists = (workspace / relative).exists()
            passed = exists if kind == "file_exists" else not exists
            expected = kind
            observed = "exists" if exists else "absent"
        elif kind in direct_signal_types:
            value = bool(signals.get(kind))
            passed = value
            expected = True
            observed = value
        else:
            raise ValueError(f"unsupported assertion type: {kind}")
        results.append(
            {
                "index": index,
                "type": kind,
                "passed": bool(passed),
                "expected": expected,
                "observed": observed,
            }
        )
    return results
