from __future__ import annotations

import copy
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(TESTS))

from fixtures import (
    backlog_node,
    graph_with_gate,
    outcome_contract,
    outcome_projection,
    runtime_graph,
)  # noqa: E402
from graph_core import RuntimeStateError, atomic_write_json  # noqa: E402
from graph_core import StaleStateError  # noqa: E402
import graph_worker  # noqa: E402
import graphctl  # noqa: E402
from graph_worker import (  # noqa: E402
    _minimal_environment,
    claim_context_node,
    execute_node,
)
from graphctl import approve_node, claim_node, initialize_run, invalidate_nodes, load_run, resume_nodes, status_report  # noqa: E402
from context_core import DeterministicWordTokenizer, validate_envelope  # noqa: E402
from fake_capsule_adapter import FakeCapsuleAdapter  # noqa: E402


class GraphWorkerTests(unittest.TestCase):
    def _setup(self, root: Path, graph: dict) -> tuple[Path, Path, dict]:
        graph_path = root / "development-graph.json"
        run_dir = root / ".development-graph"
        atomic_write_json(graph_path, graph)
        run = initialize_run(graph_path, run_dir, actor="test")
        return graph_path, run_dir, run

    def test_executes_argv_and_records_output_level_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            graph_path, run_dir, run = self._setup(root, runtime_graph(root))
            unrelated = root / "unrelated caller cwd"
            unrelated.mkdir()
            previous = Path.cwd()
            try:
                os.chdir(unrelated)
                result = execute_node(
                    graph_path,
                    run_dir,
                    "inspect-context",
                    "worker-1",
                    run["run_version"],
                )
            finally:
                os.chdir(previous)
            self.assertEqual(result["exit_code"], 0)
            self.assertEqual(result["state"], "verified")
            self.assertEqual((root / "context.txt").read_text(encoding="utf-8"), "context")
            evidence_path = (
                run_dir / "evidence" / "inspect-context" / "attempt-1.json"
            )
            self.assertTrue(evidence_path.exists())
            evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
            self.assertEqual(evidence["cwd"], ".")
            self.assertEqual(
                result["evidence"],
                "evidence/inspect-context/attempt-1.json",
            )

    def test_new_generation_preserves_previous_result_and_logs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            graph = runtime_graph(root)
            graph["nodes"][0]["execution"]["command"] = [
                sys.executable, "-c",
                "from pathlib import Path; import sys; p=Path('executions'); "
                "n=int(p.read_text())+1 if p.exists() else 1; p.write_text(str(n)); "
                "print('stdout-'+str(n)); print('stderr-'+str(n), file=sys.stderr)",
            ]
            graph_path, run_dir, run = self._setup(root, graph)
            first = execute_node(graph_path, run_dir, "inspect-context", "worker", run["run_version"])
            first_path = run_dir / first["evidence"]
            first_evidence = json.loads(first_path.read_text())
            first_files = {
                path: path.read_bytes()
                for path in (first_path, run_dir / first_evidence["stdout"]["path"], run_dir / first_evidence["stderr"]["path"])
            }
            invalidated = invalidate_nodes(graph_path, run_dir, ["inspect-context"], [], "fixture input changed", first["run_version"])
            resumed = resume_nodes(graph_path, run_dir, ["inspect-context"], "operator", invalidated["run_version"])
            self.assertEqual(resumed["nodes"]["inspect-context"]["generation"], 2)
            second = execute_node(graph_path, run_dir, "inspect-context", "worker", resumed["run_version"])
            self.assertEqual((first["attempt"], second["attempt"]), (1, 1))
            self.assertNotEqual(first["evidence"], second["evidence"])
            for path, original in first_files.items():
                self.assertEqual(path.read_bytes(), original, f"previous-generation evidence overwritten: {path.name}")
            second_evidence = json.loads((run_dir / second["evidence"]).read_text())
            self.assertEqual((run_dir / second_evidence["stdout"]["path"]).read_text(), "stdout-2\n")
            self.assertEqual((run_dir / second_evidence["stderr"]["path"]).read_text(), "stderr-2\n")
            self.assertEqual((root / "executions").read_text(), "2")

    def test_claim_lease_covers_command_timeout_and_cleanup(self) -> None:
        for timeout_field, configured in (("timeout_seconds", 600), ("timeout_minutes", 11)):
            with self.subTest(timeout_field=timeout_field), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                graph = runtime_graph(root)
                execution = graph["nodes"][0]["execution"]
                execution.pop("timeout_seconds", None)
                execution[timeout_field] = configured
                timeout = configured if timeout_field == "timeout_seconds" else configured * 60
                execution["command"] = [
                    sys.executable, "-c",
                    "from pathlib import Path; import time; Path('ready').touch();\n"
                    "while not Path('release').exists(): time.sleep(.01)\n"
                    "Path('executions').write_text('once')",
                ]
                graph_path, run_dir, run = self._setup(root, graph)
                started = datetime.now(timezone.utc)
                clock = {"elapsed": 0}

                class Clock(datetime):
                    @classmethod
                    def now(cls, tz=None):
                        return started + timedelta(seconds=clock["elapsed"])

                real_popen = subprocess.Popen

                def spawn(*args, **kwargs):
                    process = real_popen(*args, **kwargs)
                    try:
                        deadline = time.monotonic() + 3
                        while not (root / "ready").exists() and process.poll() is None and time.monotonic() < deadline:
                            time.sleep(.01)
                        self.assertTrue((root / "ready").exists())
                        claimed = load_run(run_dir)
                        lease = claimed["nodes"]["inspect-context"]["lease"]
                        expires = datetime.fromisoformat(lease["expires_at"].replace("Z", "+00:00"))
                        self.assertGreater((expires - started).total_seconds(), timeout + 4)
                        clock["elapsed"] = 301
                        self.assertIsNone(process.poll(), "fixture command ended before attempted recovery")
                        with self.assertRaisesRegex(RuntimeStateError, "live lease"):
                            resume_nodes(graph_path, run_dir, ["inspect-context"], "operator", claimed["run_version"])
                    except BaseException:
                        process.kill()
                        process.wait()
                        raise
                    finally:
                        (root / "release").touch()
                    return process

                with mock.patch.object(graphctl, "datetime", Clock), mock.patch.object(graph_worker.subprocess, "Popen", side_effect=spawn):
                    result = execute_node(graph_path, run_dir, "inspect-context", "worker", run["run_version"])
                self.assertEqual(result["state"], "verified")
                self.assertEqual((root / "executions").read_text(), "once")

    def test_minimal_environment_uses_runtime_default_path(self) -> None:
        with (
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch.object(os, "defpath", "runtime-default-path"),
        ):
            environment = _minimal_environment([])
        self.assertEqual(environment["PATH"], "runtime-default-path")

    def test_timeout_fails_node_without_duplicate_execution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            graph = runtime_graph(root)
            node = graph["nodes"][0]
            node["execution"]["command"] = [sys.executable, "-c", "import time; time.sleep(2)"]
            node["execution"]["timeout_seconds"] = 1
            graph_path, run_dir, run = self._setup(root, graph)
            result = execute_node(graph_path, run_dir, node["id"], "worker-1", run["run_version"])
            self.assertTrue(result["timed_out"])
            self.assertEqual(result["state"], "failed")

    def _timeout_with_descendant(self, *, cleanup_delay: float | None = None) -> None:
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        previous = ctypes.c_int()
        self.assertEqual(libc.prctl(37, ctypes.byref(previous), 0, 0, 0), 0)
        self.assertEqual(libc.prctl(36, 1, 0, 0, 0), 0)
        child = None
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                graph = runtime_graph(root)
                child_code = (
                    "import os,signal,time; from pathlib import Path; "
                    "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                    "Path('child.pid').write_text(str(os.getpid())); time.sleep(30)"
                )
                if cleanup_delay is not None:
                    child_code = (
                        "import os,signal,time; from pathlib import Path\n"
                        "def cleanup(signum, frame):\n"
                        f"    time.sleep({cleanup_delay!r})\n"
                        "    Path('cleanup-complete').touch()\n"
                        "    raise SystemExit(0)\n"
                        "signal.signal(signal.SIGTERM, cleanup)\n"
                        "Path('child.pid').write_text(str(os.getpid()))\n"
                        "time.sleep(30)"
                    )
                parent_code = (
                    "import subprocess,sys,time; from pathlib import Path; "
                    f"subprocess.Popen([sys.executable, '-c', {child_code!r}]);\n"
                    "while not Path('child.pid').exists(): time.sleep(.01)\n"
                    "time.sleep(30)"
                )
                graph["nodes"][0]["execution"].update(
                    command=[sys.executable, "-c", parent_code], timeout_seconds=1
                )
                graph_path, run_dir, run = self._setup(root, graph)
                try:
                    result = execute_node(graph_path, run_dir, "inspect-context", "worker", run["run_version"])
                    child = int((root / "child.pid").read_text())
                    deadline = time.monotonic() + 2
                    child_exited = False
                    while time.monotonic() < deadline:
                        try:
                            child_exited = os.waitid(os.P_PID, child, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None
                        except ChildProcessError:
                            child_exited = True
                        if child_exited:
                            break
                        time.sleep(.01)
                    self.assertTrue(result["timed_out"])
                    self.assertEqual(result["state"], "failed")
                    self.assertTrue(child_exited, "timed-out descendant survived its parent")
                    if cleanup_delay is not None:
                        self.assertTrue((root / "cleanup-complete").exists(), "descendant lost its TERM cleanup grace")
                finally:
                    if child is None and (root / "child.pid").exists():
                        child = int((root / "child.pid").read_text())
        finally:
            if child is not None:
                try:
                    os.kill(child, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                try:
                    os.waitpid(child, 0)
                except ChildProcessError:
                    pass
            libc.prctl(36, previous.value, 0, 0, 0)

    @unittest.skipUnless(sys.platform.startswith("linux"), "requires Linux child reaping")
    def test_timeout_kills_descendant_after_parent_exits(self) -> None:
        self._timeout_with_descendant()

    @unittest.skipUnless(sys.platform.startswith("linux"), "requires Linux child reaping")
    def test_timeout_preserves_descendant_term_cleanup_grace(self) -> None:
        self._timeout_with_descendant(cleanup_delay=.2)

    def _execute_with_unrelated_approval(self, exit_code: int, *, race_finalization: bool = False) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            graph = graph_with_gate(root)
            command = (
                "from pathlib import Path; import sys,time; "
                "Path('executions').open('a').write('executed\\n'); "
                "Path('ready').touch();\n"
                + ("while not Path('release').exists(): time.sleep(.01)\n" if not race_finalization else "")
                + f"sys.exit({exit_code})"
            )
            graph["nodes"][0]["execution"].update(
                command=[sys.executable, "-c", command], timeout_seconds=5
            )
            graph_path, run_dir, run = self._setup_v2(root, graph)
            real_popen = subprocess.Popen
            finalizer_name = "complete_node" if exit_code == 0 else "fail_node"
            real_finalizer = getattr(graph_worker, finalizer_name)
            approvals = []

            def approve() -> None:
                current = load_run(run_dir)
                approvals.append(approve_node(graph_path, run_dir, "approve-change", "human", current["run_version"], "fixture approval"))

            def start(*args, **kwargs):
                process = real_popen(*args, **kwargs)
                if not race_finalization:
                    try:
                        deadline = time.monotonic() + 3
                        while not (root / "ready").exists() and process.poll() is None and time.monotonic() < deadline:
                            time.sleep(.01)
                        self.assertTrue((root / "ready").exists(), "command did not reach handshake")
                        approve()
                        (root / "release").touch()
                    except BaseException:
                        process.kill()
                        process.wait()
                        raise
                return process

            calls = []

            def finalize(*args, **kwargs):
                calls.append(args[4])
                if race_finalization and len(calls) == 1:
                    approve()
                return real_finalizer(*args, **kwargs)

            with mock.patch.object(graph_worker.subprocess, "Popen", side_effect=start), mock.patch.object(graph_worker, finalizer_name, side_effect=finalize):
                result = execute_node(graph_path, run_dir, "inspect-context", "worker", run["run_version"])
            self.assertEqual(result["state"], "verified" if exit_code == 0 else "failed")
            self.assertEqual(result["exit_code"], exit_code)
            self.assertEqual((root / "executions").read_text(), "executed\n")
            self.assertEqual(len(approvals), 1)
            self.assertEqual(len(calls), 2 if race_finalization else 1)
            self.assertEqual(load_run(run_dir)["nodes"]["approve-change"]["state"], "verified")

    def _setup_v2(self, root: Path, graph: dict) -> tuple[Path, Path, dict]:
        graph_path = root / "development-graph.json"
        run_dir = root / ".development-graph"
        atomic_write_json(graph_path, graph)
        run = initialize_run(graph_path, run_dir, actor="test", outcome_contract=outcome_contract(graph), projection=outcome_projection(graph))
        return graph_path, run_dir, run

    def test_completion_accepts_unrelated_approval_without_reexecution(self) -> None:
        self._execute_with_unrelated_approval(0)

    def test_failure_accepts_unrelated_approval_without_reexecution(self) -> None:
        self._execute_with_unrelated_approval(7)

    def test_finalization_retries_concurrent_update_without_reexecution(self) -> None:
        for exit_code in (0, 7):
            with self.subTest(exit_code=exit_code):
                self._execute_with_unrelated_approval(exit_code, race_finalization=True)

    def test_finalization_rejects_replaced_claim_or_execution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            graph_path, run_dir, run = self._setup_v2(root, runtime_graph(root))
            claimed = claim_node(graph_path, run_dir, "inspect-context", "worker", run["run_version"])
            changes = {
                "run": lambda value: value.update(run_id="different-run"),
                "graph": lambda value: value.update(graph_id="different-graph"),
                "schema": lambda value: value.update(schema_version=1),
                "execution": lambda value: value["projection"].update(checksum="0" * 64),
                "outcome": lambda value: value["outcome_contract"].update(outcome_digest="0" * 64),
                "generation": lambda value: value["nodes"]["inspect-context"].update(generation=2),
                "attempt": lambda value: value["nodes"]["inspect-context"].update(attempts=2),
                "state": lambda value: value["nodes"]["inspect-context"].update(state="verified"),
                "lease-owner": lambda value: value["nodes"]["inspect-context"]["lease"].update(owner="someone-else"),
                "lease-key": lambda value: value["nodes"]["inspect-context"]["lease"].update(idempotency_key="different-attempt"),
            }
            for label, change in changes.items():
                for success in (False, True):
                    with self.subTest(change=label, success=success):
                        current = copy.deepcopy(claimed)
                        change(current)
                        with mock.patch.object(graph_worker, "load_run", return_value=current), mock.patch.object(graph_worker, "complete_node") as complete, mock.patch.object(graph_worker, "fail_node") as fail:
                            with self.assertRaisesRegex(StaleStateError, "claim changed"):
                                graph_worker._finalize_node(graph_path, run_dir, "inspect-context", "worker", claimed, {}, success=success)
                        complete.assert_not_called()
                        fail.assert_not_called()

    def test_finalization_stops_after_bounded_stale_retries(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            graph_path, run_dir, run = self._setup(root, runtime_graph(root))
            claimed = claim_node(graph_path, run_dir, "inspect-context", "worker", run["run_version"])
            with mock.patch.object(graph_worker, "complete_node", side_effect=StaleStateError("concurrent update")) as complete:
                with self.assertRaises(StaleStateError):
                    graph_worker._finalize_node(graph_path, run_dir, "inspect-context", "worker", claimed, {}, success=True)
            self.assertGreater(complete.call_count, 1)
            self.assertLessEqual(complete.call_count, 10)

    def test_finalization_allows_observed_definition_change_with_same_execution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            graph_path, run_dir, run = self._setup_v2(root, runtime_graph(root))
            claimed = claim_node(graph_path, run_dir, "inspect-context", "worker", run["run_version"])
            current = copy.deepcopy(claimed)
            current["observed_graph_checksum"] = "0" * 64
            current["observed_graph_version"] += 1
            current["run_version"] += 1
            with mock.patch.object(graph_worker, "load_run", return_value=current), mock.patch.object(graph_worker, "complete_node", return_value=current) as complete:
                graph_worker._finalize_node(graph_path, run_dir, "inspect-context", "worker", claimed, {}, success=True)
            self.assertEqual(complete.call_args.args[4], current["run_version"])

    def test_preview_reads_only_prefix_and_reports_full_size(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "stdout.log"
            with path.open("wb") as handle:
                handle.write(b"sample output")
                handle.truncate(1024 * 1024)
            with path.open("rb") as handle:
                guarded = mock.MagicMock(wraps=handle)
                guarded.__enter__.return_value = guarded
                guarded.__exit__.return_value = False

                def read(size=-1):
                    self.assertGreaterEqual(size, 0, "preview attempted an unbounded read")
                    self.assertLessEqual(size, 6)
                    return handle.read(size)

                guarded.read.side_effect = read
                with mock.patch.object(Path, "open", return_value=guarded):
                    preview, truncated, size = graph_worker._read_preview(path, 6)
            self.assertEqual((preview, truncated, size), ("sample", True, 1024 * 1024))
            self.assertEqual(graph_worker._read_preview(path.parent / "missing", 6), ("", False, 0))

    def test_rejects_shell_string_and_unapproved_destructive_node(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            graph = runtime_graph(root)
            graph["nodes"][0]["execution"]["command"] = "echo unsafe"
            graph_path = root / "development-graph.json"
            atomic_write_json(graph_path, graph)
            with self.assertRaises(ValueError):
                initialize_run(graph_path, root / ".development-graph", actor="test")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            graph = graph_with_gate(root)
            graph_path, run_dir, run = self._setup(root, graph)
            inspect = execute_node(graph_path, run_dir, "inspect-context", "worker", run["run_version"])
            current_version = inspect["run_version"]
            with self.assertRaises(PermissionError):
                execute_node(graph_path, run_dir, "implement-change", "worker", current_version)

    def test_claim_context_node_seals_digest_first_envelope_and_leaves_lease_running(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            graph = runtime_graph(root)
            target = next(
                node for node in graph["nodes"] if node["id"] == "implement-change"
            )
            target["executor"]["kind"] = "agent"
            for field in (
                "command",
                "working_directory",
                "environment",
                "evidence_paths",
                "capture_limit_bytes",
                "timeout_seconds",
            ):
                target["execution"].pop(field, None)
            target["context_control"] = {
                "version": 3,
                "plan_path": "context/implement-change.json",
                "mode": "executable",
                "tokenizer": "deterministic-word-v1",
                "adapter": "fake-deterministic",
                "retention": "digest-only",
            }
            plan_path = root / "context/implement-change.json"
            plan_path.parent.mkdir(parents=True)
            plan_path.write_text(
                __import__("json").dumps(
                    {
                        "artifact_registry": {
                            "artifact:context": {
                                "content": "PRIVATE RUNTIME CONTEXT",
                                "source": "artifacts/context.json",
                                "requirements": ["evidence-exists"],
                                "authorized": True,
                                "fresh": True,
                                "provenance_complete": True,
                            }
                        },
                        "mandatory_extras": [],
                        "optional_candidates": [],
                        "requirement_weights": {},
                        "budget": {
                            "input_limit": 1000,
                            "output_reserve": 100,
                            "safety_margin": 20,
                        },
                    }
                )
                + "\n"
            )
            graph_path, run_dir, run = self._setup(root, graph)
            inspect = execute_node(
                graph_path,
                run_dir,
                "inspect-context",
                "worker",
                run["run_version"],
            )

            planned = claim_context_node(
                graph_path,
                run_dir,
                "implement-change",
                "context-worker",
                inspect["run_version"],
            )

            self.assertEqual(planned["state"], "running")
            self.assertEqual(planned["adapter"], "fake-deterministic")
            self.assertNotIn("sealed_envelope", planned)
            self.assertNotIn("PRIVATE RUNTIME CONTEXT", json.dumps(planned))
            self._assert_digest_only_run(run_dir)
            record = (
                run_dir
                / "context/envelopes"
                / f"{planned['envelope_digest']}.json"
            )
            self.assertTrue(record.is_file())
            retained = __import__("json").loads(record.read_text())
            self.assertNotIn("PRIVATE RUNTIME CONTEXT", record.read_text())
            checkpoint = retained["execution_context"]["checkpoint_id"]
            self.assertTrue(checkpoint.startswith(f"{run['run_id']}:"))
            self.assertEqual(
                retained["execution_context"]["adapter_identity"],
                "fake/deterministic-capsule/1",
            )
            self.assertEqual(
                sum(
                    retained["budget"][field]
                    for field in (
                        "fixed_tokens",
                        "capability_schema_tokens",
                        "mandatory_tokens",
                        "optional_tokens",
                        "dynamic_tokens",
                    )
                ),
                retained["budget"]["used_context"],
            )
            status = status_report(graph_path, run_dir)
            self.assertEqual(status["state_counts"]["running"], 1)

    def _context_plan_fixture(self, root: Path, *, input_limit: int = 1000) -> tuple:
        graph = runtime_graph(root)
        target = next(node for node in graph["nodes"] if node["id"] == "implement-change")
        target["executor"]["kind"] = "agent"
        for field in ("command", "working_directory", "environment", "evidence_paths", "capture_limit_bytes", "timeout_seconds"):
            target["execution"].pop(field, None)
        target["context_control"] = {
            "version": 3, "plan_path": "context/claim-plan.json", "mode": "executable",
            "tokenizer": "deterministic-word-v1", "adapter": "fake-deterministic", "retention": "digest-only",
        }
        plan = {
            "artifact_registry": {"artifact:context": {
                "content": "PRIVATE RUNTIME CONTEXT", "source": "artifacts/context.json",
                "requirements": ["evidence-exists"], "authorized": True, "fresh": True, "provenance_complete": True,
            }},
            "mandatory_extras": [], "optional_candidates": [], "requirement_weights": {},
            "budget": {"input_limit": input_limit, "output_reserve": 100, "safety_margin": 20},
        }
        plan_path = root / "context/claim-plan.json"
        plan_path.parent.mkdir()
        atomic_write_json(plan_path, plan)
        graph_path, run_dir, run = self._setup_v2(root, graph)
        inspected = execute_node(graph_path, run_dir, "inspect-context", "context-worker", run["run_version"])
        return graph_path, run_dir, inspected

    def _assert_digest_only_run(self, run_dir: Path) -> None:
        for path in run_dir.rglob("*"):
            if path.is_file():
                self.assertNotIn(b"PRIVATE RUNTIME CONTEXT", path.read_bytes(), str(path.relative_to(run_dir)))

    def _assert_transient_claim(self, planned: dict, run_dir: Path) -> None:
        envelope = planned["sealed_envelope"]
        self.assertEqual(validate_envelope(envelope), [])
        self.assertIn("PRIVATE RUNTIME CONTEXT", json.dumps(envelope))
        self.assertEqual(envelope["digest"], planned["envelope_digest"])
        self.assertEqual(envelope["envelope_id"], planned["envelope_id"])
        run = load_run(run_dir)
        state = run["nodes"]["implement-change"]
        self.assertEqual(planned["idempotency_key"], state["lease"]["idempotency_key"])
        self.assertEqual(envelope["execution_context"]["checkpoint_id"], f"{run['run_id']}:{planned['run_version']}:{state['generation']}:{state['attempts']}")
        adapter = FakeCapsuleAdapter(
            tokenizer=DeterministicWordTokenizer("deterministic-word-v1"), context_limit=1000,
            handler=lambda received: {"status": "completed", "output_artifacts": ["artifact:implementation"], "result_payload": {"accepted": True}},
        )
        result = adapter.invoke(envelope, planned["idempotency_key"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(adapter.invocation_count, 1)
        retained = json.loads((run_dir / planned["envelope_record"]).read_text())
        self.assertFalse(retained["payload_retained"])
        self.assertNotIn("payload", retained)
        self._assert_digest_only_run(run_dir)

    def _context_claim_command(self, graph_path: Path, run_dir: Path, run_version: int) -> list[str]:
        return [sys.executable, "-B", str(SCRIPTS / "graph_worker.py"), "--graph", str(graph_path), "--run-dir", str(run_dir), "--node", "implement-change", "--worker-id", "context-worker", "--expected-run-version", str(run_version)]

    def test_context_claim_api_can_return_exact_envelope_without_retaining_payload(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            graph_path, run_dir, inspected = self._context_plan_fixture(Path(directory))
            planned = claim_context_node(graph_path, run_dir, "implement-change", "context-worker", inspected["run_version"], include_envelope=True)
            self._assert_transient_claim(planned, run_dir)

    def test_context_claim_cli_envelope_output_is_explicit_and_digest_only_on_disk(self) -> None:
        for include in (False, True):
            with self.subTest(include=include), tempfile.TemporaryDirectory() as directory:
                graph_path, run_dir, inspected = self._context_plan_fixture(Path(directory))
                command = self._context_claim_command(graph_path, run_dir, inspected["run_version"]) + ["--context-plan"]
                if include:
                    command.append("--include-envelope")
                result = subprocess.run(command, capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                planned = json.loads(result.stdout)
                if include:
                    self._assert_transient_claim(planned, run_dir)
                else:
                    self.assertNotIn("sealed_envelope", planned)
                    self.assertNotIn("PRIVATE RUNTIME CONTEXT", result.stdout)
                    self._assert_digest_only_run(run_dir)

    def test_include_envelope_without_context_plan_rejects_before_claim(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            graph_path, run_dir, inspected = self._context_plan_fixture(Path(directory))
            before = {path.relative_to(run_dir): path.read_bytes() for path in run_dir.rglob("*") if path.is_file()}
            result = subprocess.run(self._context_claim_command(graph_path, run_dir, inspected["run_version"]) + ["--include-envelope"], capture_output=True, text=True, timeout=10)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("--include-envelope requires --context-plan", result.stderr)
            after = {path.relative_to(run_dir): path.read_bytes() for path in run_dir.rglob("*") if path.is_file()}
            self.assertEqual(after, before)

    def test_context_claim_failures_never_emit_raw_context(self) -> None:
        for input_limit in (0, 1):
            for cli in (False, True):
                with self.subTest(input_limit=input_limit, cli=cli), tempfile.TemporaryDirectory() as directory:
                    graph_path, run_dir, inspected = self._context_plan_fixture(Path(directory), input_limit=input_limit)
                    if cli:
                        result = subprocess.run(self._context_claim_command(graph_path, run_dir, inspected["run_version"]) + ["--context-plan", "--include-envelope"], capture_output=True, text=True, timeout=10)
                        self.assertNotEqual(result.returncode, 0)
                        self.assertNotIn("PRIVATE RUNTIME CONTEXT", result.stdout + result.stderr)
                        self.assertEqual(load_run(run_dir)["nodes"]["implement-change"]["state"], "failed")
                        if result.stdout:
                            summary = json.loads(result.stdout)
                            self.assertEqual(summary["status"], "mandatory_overflow")
                            self.assertNotIn("sealed_envelope", summary)
                        else:
                            self.assertIn("context planning failed", result.stderr)
                    elif input_limit == 0:
                        with self.assertRaisesRegex(RuntimeStateError, "context planning failed") as error:
                            claim_context_node(graph_path, run_dir, "implement-change", "context-worker", inspected["run_version"], include_envelope=True)
                        self.assertNotIn("PRIVATE RUNTIME CONTEXT", str(error.exception))
                    else:
                        planned = claim_context_node(graph_path, run_dir, "implement-change", "context-worker", inspected["run_version"], include_envelope=True)
                        self.assertEqual(planned["status"], "mandatory_overflow")
                        self.assertNotIn("sealed_envelope", planned)
                        self.assertNotIn("PRIVATE RUNTIME CONTEXT", json.dumps(planned))
                    self._assert_digest_only_run(run_dir)


    def test_evidence_collection_errors_fail_once_and_preserve_result(self) -> None:
        for failure in ("escape", "unreadable", "preview"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                parent = Path(directory)
                root = parent / "project"
                root.mkdir()
                outside = parent / "outside.txt"
                outside.write_text("OUTSIDE CONTENT MUST NOT BE READ")
                graph = runtime_graph(root)
                node = graph["nodes"][0]
                command = (
                    "from pathlib import Path; import sys; "
                    "Path('executions').open('a').write('once\\n'); "
                    + ("Path('evidence').symlink_to(sys.argv[1])" if failure == "escape"
                       else "Path('evidence').write_text('output')")
                )
                node["execution"].update(command=[sys.executable, "-c", command, str(outside)], evidence_paths=["evidence"])
                graph_path, run_dir, run = self._setup_v2(root, graph)
                original_digest = graph_worker._digest_path

                def digest(path, project_root):
                    if failure == "unreadable" and path.name == "evidence":
                        raise PermissionError("fixture evidence is unreadable")
                    return original_digest(path, project_root)

                original_preview = graph_worker._read_preview

                def preview(path, limit):
                    if failure == "preview" and path.name.endswith(".stdout.txt"):
                        raise PermissionError("fixture output is unreadable")
                    return original_preview(path, limit)

                with mock.patch.object(graph_worker, "_digest_path", side_effect=digest), mock.patch.object(graph_worker, "_read_preview", side_effect=preview):
                    result = execute_node(graph_path, run_dir, node["id"], "worker", run["run_version"])
                self.assertEqual(result["exit_code"], 0)
                self.assertEqual(result["state"], "failed")
                self.assertEqual((root / "executions").read_text(), "once\n")
                state = load_run(run_dir)["nodes"][node["id"]]
                self.assertIsNone(state["lease"])
                self.assertEqual(state["attempts"], 1)
                evidence = json.loads((run_dir / result["evidence"]).read_text())
                self.assertTrue(evidence["evidence_errors"])
                if failure == "preview":
                    self.assertTrue(evidence["evidence_errors"][0]["path"].endswith(".stdout.txt"))
                else:
                    self.assertEqual(evidence["evidence_errors"][0]["path"], "evidence")
                self.assertNotIn("OUTSIDE CONTENT MUST NOT BE READ", json.dumps(evidence))
                self.assertEqual(state["last_result"], evidence)

    def test_v2_worker_executes_projection_and_rejects_dlg_backlog(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            graph = runtime_graph(root)
            projected_ids = [item["id"] for item in graph["nodes"]]
            future = backlog_node()
            future["executor"]["kind"] = "deterministic"
            future["execution"].update(
                {
                    "command": [
                        sys.executable,
                        "-c",
                        "from pathlib import Path; Path('future.txt').write_text('ran')",
                    ],
                    "working_directory": ".",
                    "environment": [],
                    "evidence_paths": [],
                    "capture_limit_bytes": 65536,
                }
            )
            graph["nodes"].insert(0, future)
            graph["entry_nodes"].insert(0, future["id"])
            graph["terminal_nodes"].append(future["id"])
            graph_path = root / "development-graph.json"
            run_dir = root / ".development-graph"
            atomic_write_json(graph_path, graph)
            run = initialize_run(
                graph_path,
                run_dir,
                actor="test",
                outcome_contract=outcome_contract(graph),
                projection=outcome_projection(graph, projected_ids),
            )
            with self.assertRaisesRegex(RuntimeStateError, "unknown node"):
                execute_node(
                    graph_path,
                    run_dir,
                    "future-debt",
                    "worker",
                    run["run_version"],
                )
            self.assertFalse((root / "future.txt").exists())
            result = execute_node(
                graph_path,
                run_dir,
                "inspect-context",
                "worker",
                run["run_version"],
            )
            self.assertEqual(result["state"], "verified")
            self.assertEqual(
                status_report(graph_path, run_dir)["dlg_backlog_nodes"],
                ["future-debt"],
            )


    def test_v2_worker_rejects_contract_tamper_before_execution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            graph = runtime_graph(root)
            graph_path = root / "development-graph.json"
            run_dir = root / ".development-graph"
            atomic_write_json(graph_path, graph)
            run = initialize_run(
                graph_path,
                run_dir,
                actor="test",
                outcome_contract=outcome_contract(graph),
                projection=outcome_projection(graph),
            )
            run["outcome_contract"]["non_goals"] = ["Tampered non-goal."]
            atomic_write_json(run_dir / "run.json", run)
            with self.assertRaisesRegex(RuntimeStateError, "ledger integrity"):
                execute_node(
                    graph_path,
                    run_dir,
                    "inspect-context",
                    "worker",
                    run["run_version"],
                )
            self.assertFalse((root / "context.txt").exists())


if __name__ == "__main__":
    unittest.main()
