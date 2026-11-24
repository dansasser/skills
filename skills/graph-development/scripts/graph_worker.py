"""Execute one eligible deterministic development-graph node safely."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from context_core import (
    DeterministicWordTokenizer,
    build_mandatory_frontier,
    plan_context,
)
from context_store import ContextStore
from graph_core import GraphControlError, RuntimeStateError, StaleStateError, atomic_write_json, load_json, resolve_within
from graphctl import (
    claim_node,
    complete_node,
    fail_node,
    load_execution_graph,
    load_run,
    run_execution_checksum,
    run_observed_graph_checksum,
    status_report,
)
from validate_graph import canonical_checksum, validate_graph
from path_contract import (
    project_root_for_graph,
    serialize_relative,
)

_TERMINATION_GRACE_SECONDS = 2
_REAP_TIMEOUT_SECONDS = 2
_FINALIZATION_MARGIN_SECONDS = 30


def _graph(path: Path) -> dict[str, Any]:
    graph = load_json(path)
    report = validate_graph(graph)
    if not report["valid"]:
        raise ValueError("graph validation failed: " + "; ".join(report["errors"]))
    return graph


def _node(graph: dict[str, Any], node_id: str) -> dict[str, Any]:
    for item in graph["nodes"]:
        if item["id"] == node_id:
            return item
    raise RuntimeStateError(f"unknown node: {node_id}")


def _approval_is_current(run: dict[str, Any], node_id: str) -> bool:
    approval = run["nodes"][node_id].get("approval")
    return bool(
        isinstance(approval, dict)
        and approval.get("decision") == "approved"
        and approval.get("run_id") == run["run_id"]
        and approval.get("graph_checksum") == run_execution_checksum(run)
        and approval.get("node_id") == node_id
    )


def _safe_command(node: dict[str, Any]) -> list[str]:
    if node["executor"]["kind"] != "deterministic":
        raise RuntimeStateError(
            f"node {node['id']} uses {node['executor']['kind']} execution and requires an external adapter or human"
        )
    command = node["execution"].get("command")
    if not isinstance(command, list) or not command or not all(isinstance(item, str) and item and "\x00" not in item for item in command):
        raise ValueError(f"node {node['id']} must declare a non-empty command argument array")
    return command


def _minimal_environment(allowlist: list[str]) -> dict[str, str]:
    environment: dict[str, str] = {
        "PATH": os.environ.get("PATH", os.defpath),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"),
    }
    for name in allowlist:
        if name in os.environ:
            environment[name] = os.environ[name]
    return environment


def _digest_path(path: Path, project_root: Path) -> dict[str, Any]:
    relative = path.relative_to(project_root).as_posix()
    if path.is_file():
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
                size += len(chunk)
        return {"path": relative, "kind": "file", "size": size, "sha256": digest.hexdigest()}
    if path.is_dir():
        members = sorted(
            item.relative_to(project_root).as_posix()
            for item in path.rglob("*")
            if item.is_file()
        )
        return {"path": relative, "kind": "directory", "members": members}
    return {"path": relative, "kind": "missing"}


def _read_preview(path: Path, limit: int) -> tuple[str, bool, int]:
    if not path.exists():
        return "", False, 0
    with path.open("rb") as handle:
        payload = handle.read(limit)
        size = os.fstat(handle.fileno()).st_size
    return payload.decode("utf-8", errors="replace"), size > limit, size


def _finalize_node(
    graph_path: Path,
    run_dir: Path,
    node_id: str,
    worker_id: str,
    claimed: dict[str, Any],
    evidence: dict[str, Any],
    *,
    success: bool,
) -> dict[str, Any]:
    """Record an already executed result without adopting a replacement claim."""
    original = claimed["nodes"][node_id]
    original_lease = original.get("lease")
    if (
        original.get("state") != "running"
        or not isinstance(original_lease, dict)
        or original_lease.get("owner") != worker_id
        or not original_lease.get("idempotency_key")
    ):
        raise RuntimeStateError("original execution claim has no owned lease")
    for attempt in range(5):
        current = load_run(run_dir)
        state = current["nodes"].get(node_id, {})
        lease = state.get("lease")
        if (
            any(current.get(key) != claimed.get(key) for key in ("run_id", "graph_id", "schema_version"))
            or run_execution_checksum(current) != run_execution_checksum(claimed)
            or current.get("projection") != claimed.get("projection")
            or current.get("outcome_contract") != claimed.get("outcome_contract")
            or state.get("state") != "running"
            or state.get("generation") != original["generation"]
            or state.get("attempts") != original["attempts"]
            or not isinstance(lease, dict)
            or lease.get("owner") != original_lease["owner"]
            or lease.get("idempotency_key") != original_lease["idempotency_key"]
        ):
            raise StaleStateError(f"execution claim changed for node {node_id}; result evidence is retained for recovery")
        try:
            finalizer = complete_node if success else fail_node
            return finalizer(graph_path, run_dir, node_id, worker_id, current["run_version"], evidence)
        except StaleStateError as exc:
            # CAS fences a change after the identity check; retry only this result.
            if attempt == 4:
                raise StaleStateError(
                    f"could not finalize node {node_id} after 5 concurrent updates; "
                    "result evidence is retained for recovery without rerunning the command"
                ) from exc
    raise AssertionError("unreachable finalization retry state")


def claim_context_node(
    graph_path: Path,
    run_dir: Path,
    node_id: str,
    worker_id: str,
    expected_run_version: int,
    *,
    include_envelope: bool = False,
) -> dict[str, Any]:
    """Claim an opt-in V3 model node and persist its sealed context envelope."""

    graph_path = graph_path.resolve()
    run_dir = run_dir.resolve()
    graph = _graph(graph_path)
    before = load_run(run_dir)
    if before["run_version"] != expected_run_version:
        from graph_core import StaleStateError

        raise StaleStateError(
            f"stale run version: expected {expected_run_version}, current {before['run_version']}"
        )
    if run_observed_graph_checksum(before) != canonical_checksum(graph):
        from graph_core import StaleStateError

        raise StaleStateError("graph checksum differs from initialized runtime state")
    execution_graph = load_execution_graph(
        graph_path,
        run_dir,
        graph=graph,
        run=before,
    )
    definition = _node(execution_graph, node_id)
    binding = definition.get("context_control")
    if not isinstance(binding, dict) or binding.get("version") != 3:
        raise RuntimeStateError(
            f"node {node_id} has no Graph Development V3 context binding"
        )
    if definition["executor"]["kind"] not in {"agent", "hybrid"}:
        raise RuntimeStateError(
            f"node {node_id} context binding requires an agent or hybrid executor"
        )
    if binding["mode"] != "executable":
        raise RuntimeStateError(
            "graph worker can claim only executable context bindings; use contextctl for preview"
        )
    if binding["tokenizer"] != "deterministic-word-v1":
        raise RuntimeStateError(
            f"provider-neutral worker does not recognize tokenizer {binding['tokenizer']!r}"
        )
    project_root = project_root_for_graph(graph_path, graph)
    if not project_root.is_dir():
        raise RuntimeStateError(f"project root does not exist: {project_root}")
    plan_path = resolve_within(project_root, binding["plan_path"])
    config = load_json(plan_path)
    tokenizer = DeterministicWordTokenizer(binding["tokenizer"])
    claimed = claim_node(
        graph_path,
        run_dir,
        node_id,
        worker_id,
        expected_run_version,
    )
    try:
        mandatory = build_mandatory_frontier(
            execution_graph,
            node_id,
            config.get("artifact_registry", {}),
            extras=config.get("mandatory_extras", []),
        )
        budget = config["budget"]
        planned = plan_context(
            execution_graph,
            node_id,
            mandatory,
            config.get("optional_candidates", []),
            config.get("requirement_weights", {}),
            tokenizer=tokenizer,
            mode=binding["mode"],
            input_limit=budget["input_limit"],
            output_reserve=budget["output_reserve"],
            safety_margin=budget["safety_margin"],
            request_policy=config.get("request_policy"),
            checkpoint_id=(
                f"{claimed['run_id']}:{claimed['run_version']}:"
                f"{claimed['nodes'][node_id]['generation']}:"
                f"{claimed['nodes'][node_id]['attempts']}"
            ),
            adapter_identity=(
                "fake/deterministic-capsule/1"
                if binding["adapter"] == "fake-deterministic"
                else binding["adapter"]
            ),
        )
        store = ContextStore(run_dir)
        if planned["status"] == "mandatory_overflow":
            store.store_result(planned["result"])
            failed = fail_node(
                graph_path,
                run_dir,
                node_id,
                worker_id,
                claimed["run_version"],
                {
                    "status": "mandatory_overflow",
                    "result_digest": planned["result"]["digest"],
                    "mandatory_tokens": planned["mandatory_tokens"],
                    "available_context": planned["available_context"],
                },
            )
            return {
                "node_id": node_id,
                "state": failed["nodes"][node_id]["state"],
                "run_version": failed["run_version"],
                "status": "mandatory_overflow",
                "result_digest": planned["result"]["digest"],
            }
        envelope = planned["envelope"]
        record = store.store_envelope(
            envelope,
            retain_payload=binding["retention"] == "explicit-payload",
        )
    except Exception as exc:
        failed = fail_node(
            graph_path,
            run_dir,
            node_id,
            worker_id,
            claimed["run_version"],
            {
                "status": "context_planning_failed",
                "error": f"{type(exc).__name__}: {exc}",
            },
        )
        raise RuntimeStateError(
            f"context planning failed and node is {failed['nodes'][node_id]['state']}: {exc}"
        ) from exc
    lease = claimed["nodes"][node_id]["lease"]
    result = {
        "node_id": node_id,
        "state": "running",
        "run_version": claimed["run_version"],
        "adapter": binding["adapter"],
        "tokenizer": binding["tokenizer"],
        "envelope_id": envelope["envelope_id"],
        "envelope_digest": envelope["digest"],
        "envelope_record": record.relative_to(run_dir).as_posix(),
        "idempotency_key": lease["idempotency_key"],
    }
    if include_envelope:
        result["sealed_envelope"] = envelope
    return result


def execute_node(
    graph_path: Path,
    run_dir: Path,
    node_id: str,
    worker_id: str,
    expected_run_version: int,
) -> dict[str, Any]:
    graph_path = graph_path.resolve()
    run_dir = run_dir.resolve()
    graph = _graph(graph_path)
    before = load_run(run_dir)
    if before["run_version"] != expected_run_version:
        from graph_core import StaleStateError

        raise StaleStateError(
            f"stale run version: expected {expected_run_version}, current {before['run_version']}"
        )
    if run_observed_graph_checksum(before) != canonical_checksum(graph):
        from graph_core import StaleStateError

        raise StaleStateError("graph checksum differs from initialized runtime state")
    execution_graph = load_execution_graph(
        graph_path,
        run_dir,
        graph=graph,
        run=before,
    )
    definition = _node(execution_graph, node_id)
    command = _safe_command(definition)
    if definition["side_effects"]["approval_required"] and not _approval_is_current(before, node_id):
        raise PermissionError(f"node {node_id} requires a current approval")

    timeout_seconds = definition["execution"].get(
        "timeout_seconds", definition["execution"]["timeout_minutes"] * 60
    )
    lease_seconds = max(
        300,
        timeout_seconds + _TERMINATION_GRACE_SECONDS + _REAP_TIMEOUT_SECONDS + _FINALIZATION_MARGIN_SECONDS,
    )
    claimed = claim_node(graph_path, run_dir, node_id, worker_id, expected_run_version, lease_seconds=lease_seconds)
    node_state = claimed["nodes"][node_id]
    attempt = node_state["attempts"]
    project_root = project_root_for_graph(graph_path, graph)
    if not project_root.is_dir():
        result = {"exit_code": None, "timed_out": False, "error": f"project root does not exist: {project_root}"}
        failed = _finalize_node(graph_path, run_dir, node_id, worker_id, claimed, result, success=False)
        return {**result, "state": failed["nodes"][node_id]["state"], "run_version": failed["run_version"]}

    working_directory = resolve_within(project_root, definition["execution"].get("working_directory", "."))
    if not working_directory.is_dir():
        result = {"exit_code": None, "timed_out": False, "error": f"working directory does not exist: {working_directory}"}
        failed = _finalize_node(graph_path, run_dir, node_id, worker_id, claimed, result, success=False)
        return {**result, "state": failed["nodes"][node_id]["state"], "run_version": failed["run_version"]}

    evidence_dir = run_dir / "evidence" / node_id
    evidence_dir.mkdir(parents=True, exist_ok=True)
    evidence_name = f"attempt-{attempt}"
    # Preserve generation-one paths while keeping later executions append-only.
    if node_state["generation"] != 1:
        evidence_name = f"generation-{node_state['generation']}-{evidence_name}"
    stdout_path = evidence_dir / f"{evidence_name}.stdout.txt"
    stderr_path = evidence_dir / f"{evidence_name}.stderr.txt"
    result_path = evidence_dir / f"{evidence_name}.json"
    environment = _minimal_environment(definition["execution"].get("environment", []))
    started = time.monotonic()
    timed_out = False
    exit_code: int | None = None
    spawn_error: str | None = None
    with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
        try:
            process = subprocess.Popen(
                command,
                cwd=working_directory,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
                shell=False,
                start_new_session=True,
            )
            try:
                exit_code = process.wait(timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                timed_out = True
                termination_deadline = time.monotonic() + _TERMINATION_GRACE_SECONDS
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    process.wait(timeout=_TERMINATION_GRACE_SECONDS)
                except subprocess.TimeoutExpired:
                    pass
                # A reaped leader does not prove its process group has exited.
                while (remaining := termination_deadline - time.monotonic()) > 0:
                    try:
                        os.killpg(process.pid, 0)
                    except ProcessLookupError:
                        break
                    time.sleep(min(.05, remaining))
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                exit_code = process.wait(timeout=_REAP_TIMEOUT_SECONDS)
        except OSError as exc:
            spawn_error = str(exc)
    duration = round(time.monotonic() - started, 6)
    capture_limit = definition["execution"].get("capture_limit_bytes", 65536)
    evidence_errors: list[dict[str, str]] = []

    def preview(path: Path) -> tuple[str, bool, int | None]:
        try:
            return _read_preview(path, capture_limit)
        except OSError as exc:
            evidence_errors.append({"path": path.relative_to(run_dir).as_posix(), "error": type(exc).__name__})
            return "", False, None

    stdout_preview, stdout_truncated, stdout_size = preview(stdout_path)
    stderr_preview, stderr_truncated, stderr_size = preview(stderr_path)
    declared_evidence = []
    for relative in definition["execution"].get("evidence_paths", []):
        try:
            declared_evidence.append(_digest_path(resolve_within(project_root, relative), project_root))
        except (OSError, ValueError) as exc:
            evidence_errors.append({"path": relative, "error": type(exc).__name__})
    evidence: dict[str, Any] = {
        "attempt": attempt,
        "command_digest": hashlib.sha256(json.dumps(command, separators=(",", ":")).encode("utf-8")).hexdigest(),
        "cwd": serialize_relative(
            project_root,
            working_directory,
            label="worker cwd",
        ),
        "duration_seconds": duration,
        "exit_code": exit_code,
        "timed_out": timed_out,
        "spawn_error": spawn_error,
        "stdout": {"path": str(stdout_path.relative_to(run_dir)), "size": stdout_size, "truncated": stdout_truncated, "preview": stdout_preview},
        "stderr": {"path": str(stderr_path.relative_to(run_dir)), "size": stderr_size, "truncated": stderr_truncated, "preview": stderr_preview},
        "declared_evidence": declared_evidence,
        "evidence_errors": evidence_errors,
    }
    atomic_write_json(result_path, evidence)
    success = exit_code == 0 and not timed_out and spawn_error is None and not evidence_errors
    after = _finalize_node(graph_path, run_dir, node_id, worker_id, claimed, evidence, success=success)
    return {
        "node_id": node_id,
        "attempt": attempt,
        "exit_code": exit_code,
        "timed_out": timed_out,
        "spawn_error": spawn_error,
        "state": after["nodes"][node_id]["state"],
        "run_version": after["run_version"],
        "evidence": serialize_relative(
            run_dir,
            result_path,
            label="worker evidence",
        ),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--graph", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--node")
    selection.add_argument("--next", action="store_true", dest="next_node")
    parser.add_argument("--worker-id", required=True)
    parser.add_argument("--expected-run-version", type=int, required=True)
    parser.add_argument(
        "--context-plan",
        action="store_true",
        help="claim an opt-in V3 agent/hybrid node and seal its context",
    )
    parser.add_argument(
        "--include-envelope",
        action="store_true",
        help="include the full sealed envelope and raw context in context-plan JSON output",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.include_envelope and not args.context_plan:
            raise RuntimeStateError("--include-envelope requires --context-plan")
        if args.context_plan:
            if args.next_node or not args.node:
                raise RuntimeStateError(
                    "--context-plan requires an explicit --node"
                )
            result = claim_context_node(
                args.graph,
                args.run_dir,
                args.node,
                args.worker_id,
                args.expected_run_version,
                include_envelope=args.include_envelope,
            )
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0 if result["state"] == "running" else 1
        node_id = args.node
        if args.next_node:
            graph = _graph(args.graph)
            run = load_run(args.run_dir)
            execution_graph = load_execution_graph(
                args.graph,
                args.run_dir,
                graph=graph,
                run=run,
            )
            definitions = {item["id"]: item for item in execution_graph["nodes"]}
            report = status_report(args.graph, args.run_dir)
            eligible = [
                item for item in report["ready_nodes"]
                if definitions[item]["executor"]["kind"] == "deterministic"
                and isinstance(definitions[item]["execution"].get("command"), list)
            ]
            if not eligible:
                raise RuntimeStateError("no ready deterministic node")
            node_id = eligible[0]
        result = execute_node(args.graph, args.run_dir, node_id, args.worker_id, args.expected_run_version)
    except (GraphControlError, ValueError, PermissionError, OSError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["state"] == "verified" else 1


if __name__ == "__main__":
    sys.exit(main())
