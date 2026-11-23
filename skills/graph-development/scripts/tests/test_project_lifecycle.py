from __future__ import annotations

import hashlib
import json
import multiprocessing
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

from project_lifecycle import (  # noqa: E402
    LifecycleError,
    StaleLifecycleError,
    apply_project,
    preview_project,
    recover_project,
)


def _request(
    root: Path,
    operation: str,
    artifacts: list[tuple[str, bytes, str | None]],
) -> dict[str, object]:
    records: list[dict[str, object]] = []
    for path, payload, expected in artifacts:
        record: dict[str, object] = {
            "path": path,
            "content": payload,
            "mode": 0o644,
        }
        if expected is not None:
            record["expected_sha256"] = expected
        records.append(record)
    return {
        "schema_version": 1,
        "operation": operation,
        "project_root": root,
        "artifacts": records,
        "metadata": {"source": "test"},
    }


def _concurrent_apply(
    root: str,
    base_digest: str,
    plan_digest: str,
    start: multiprocessing.synchronize.Event,
    output: multiprocessing.queues.Queue,
) -> None:
    request = _request(
        Path(root), "bootstrap", [("graph.json", b"candidate\n", None)]
    )
    start.wait(5)
    try:
        receipt = apply_project(
            request,
            expected_base_digest=base_digest,
            expected_plan_digest=plan_digest,
            actor="concurrent-test",
            authority="test:concurrency",
        )
        output.put(("receipt", receipt.state))
    except StaleLifecycleError:
        output.put(("stale", None))
    except BaseException as exc:  # pragma: no cover - diagnostic transport
        output.put(("error", repr(exc)))


class ProjectLifecycleTests(unittest.TestCase):
    def test_receipt_schema_enforces_operation_digests_and_relative_paths(
        self,
    ) -> None:
        schema = json.loads(
            (
                SCRIPTS.parent
                / "references"
                / "project-lifecycle-receipt.schema.json"
            ).read_text(encoding="utf-8")
        )

        plan_schema = json.loads(
            (
                SCRIPTS.parent
                / "references"
                / "project-lifecycle-plan.schema.json"
            ).read_text(encoding="utf-8")
        )
        expected_operations = [
            "bootstrap",
            "update",
            "migration",
            "graph-mutation",
            "run-promotion",
        ]
        self.assertEqual(
            schema["properties"]["operation"]["enum"],
            expected_operations,
        )
        self.assertEqual(
            plan_schema["properties"]["operation"]["enum"],
            expected_operations,
        )
        for name in ("base_digest", "plan_digest", "candidate_digest"):
            self.assertEqual(
                schema["properties"][name],
                {"$ref": "#/$defs/digest"},
            )
        self.assertEqual(
            schema["$defs"]["digest"]["pattern"],
            "^[0-9a-f]{64}$",
        )
        path_pattern = re.compile(schema["$defs"]["path"]["pattern"])
        for valid in (
            "development-graph.json",
            ".development-graph/run.json",
            "snapshots/graph-v1.json",
        ):
            self.assertIsNotNone(path_pattern.search(valid), valid)
        for invalid in (
            "",
            "/absolute",
            "../escape",
            "nested/../escape",
            "C" + ":/drive-root",
            r"windows\path",
            "~/home",
        ):
            self.assertIsNone(path_pattern.search(invalid), invalid)


    def test_run_promotion_accepts_exact_runtime_and_projection_artifacts(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            current = {
                ".development-graph/run.json": b'{"run_version":1}\n',
                ".development-graph/events.jsonl": b'{"sequence":1}\n',
            }
            candidates = {
                ".development-graph/run.json": b'{"run_version":2}\n',
                ".development-graph/events.jsonl": (
                    b'{"sequence":1}\n{"sequence":2}\n'
                ),
                ".development-graph/projections/test-run-v2.json": (
                    b'{"projection_version":2}\n'
                ),
            }
            artifacts: list[tuple[str, bytes, str | None]] = []
            for path, payload in current.items():
                target = root / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(payload)
                artifacts.append(
                    (
                        path,
                        candidates[path],
                        hashlib.sha256(payload).hexdigest(),
                    )
                )
            projection_path = (
                ".development-graph/projections/test-run-v2.json"
            )
            artifacts.append(
                (projection_path, candidates[projection_path], None)
            )
            request = _request(root, "run-promotion", artifacts)
            request["metadata"] = {"promotion_id": "promotion-1"}

            plan = preview_project(request)

            self.assertTrue(plan.valid, plan)
            self.assertEqual(plan.operation, "run-promotion")
            self.assertEqual(plan.blocked, ())
            self.assertEqual(plan.conflicts, ())
            self.assertEqual(plan.changed, tuple(sorted(candidates)))
            receipt = apply_project(
                request,
                expected_base_digest=plan.base_digest,
                expected_plan_digest=plan.plan_digest,
                actor="tester",
                authority="test:run-promotion",
            )
            self.assertTrue(receipt.applied)
            self.assertEqual(receipt.operation, "run-promotion")
            for path, payload in candidates.items():
                self.assertEqual((root / path).read_bytes(), payload)

    def test_graph_mutation_transactions_existing_state_and_new_snapshot_without_inventory(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            control = root / ".development-graph"
            control.mkdir()
            current = {
                "development-graph.json": b'{"graph_version":1}\n',
                "development-graph.md": b"# Graph v1\n",
                ".development-graph/run.json": b'{"run_version":1}\n',
                ".development-graph/events.jsonl": b'{"sequence":1}\n',
            }
            candidates = {
                "development-graph.json": b'{"graph_version":2}\n',
                "development-graph.md": b"# Graph v2\n",
                ".development-graph/run.json": b'{"run_version":2}\n',
                ".development-graph/events.jsonl": (
                    b'{"sequence":1}\n{"sequence":2}\n'
                ),
                ".development-graph/snapshots/graph-v1-mutation.json": (
                    current["development-graph.json"]
                ),
            }
            artifacts: list[tuple[str, bytes, str | None]] = []
            for path, payload in current.items():
                target = root / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(payload)
                artifacts.append(
                    (
                        path,
                        candidates[path],
                        hashlib.sha256(payload).hexdigest(),
                    )
                )
            snapshot_path = ".development-graph/snapshots/graph-v1-mutation.json"
            artifacts.append((snapshot_path, candidates[snapshot_path], None))
            request = _request(root, "graph-mutation", artifacts)
            request["metadata"] = {
                "entrypoint": "rewrite_graph",
                "mutation_id": "mutation-1",
                "expected_graph_checksum": "a" * 64,
                "expected_run_version": 1,
            }

            plan = preview_project(request)

            self.assertTrue(plan.valid, plan)
            self.assertEqual(plan.blocked, ())
            self.assertEqual(plan.conflicts, ())
            self.assertEqual(plan.changed, tuple(sorted(candidates)))
            receipt = apply_project(
                request,
                expected_base_digest=plan.base_digest,
                expected_plan_digest=plan.plan_digest,
                actor="tester",
                authority="test:graph-mutation",
            )
            self.assertTrue(receipt.applied)
            self.assertEqual(receipt.operation, "graph-mutation")
            for path, payload in candidates.items():
                self.assertEqual((root / path).read_bytes(), payload)

    def test_graph_mutation_requires_mutation_id(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            request = _request(root, "graph-mutation", [])

            with self.assertRaisesRegex(
                LifecycleError,
                "requires a non-empty mutation_id",
            ):
                preview_project(request)

    def test_graph_mutation_blocks_unreviewed_existing_and_non_snapshot_additions(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            graph = root / "development-graph.json"
            graph.write_bytes(b'{"graph_version":1}\n')
            request = _request(
                root,
                "graph-mutation",
                [
                    (
                        "development-graph.json",
                        b'{"graph_version":2}\n',
                        None,
                    ),
                    (
                        ".development-graph/run.json",
                        b'{"run_version":2}\n',
                        None,
                    ),
                ],
            )
            request["metadata"] = {"mutation_id": "mutation-1"}

            plan = preview_project(request)

            self.assertFalse(plan.valid)
            self.assertEqual(
                plan.blocked,
                (
                    ".development-graph/run.json",
                    "development-graph.json",
                ),
            )

    def test_graph_mutation_never_overwrites_existing_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot = (
                root
                / ".development-graph"
                / "snapshots"
                / "graph-v1-mutation.json"
            )
            snapshot.parent.mkdir(parents=True)
            snapshot.write_bytes(b"user snapshot\n")
            request = _request(
                root,
                "graph-mutation",
                [
                    (
                        ".development-graph/snapshots/graph-v1-mutation.json",
                        b"candidate snapshot\n",
                        None,
                    )
                ],
            )
            request["metadata"] = {"mutation_id": "mutation-1"}

            plan = preview_project(request)

            self.assertFalse(plan.valid)
            self.assertEqual(
                plan.conflicts,
                (".development-graph/snapshots/graph-v1-mutation.json",),
            )
            self.assertEqual(snapshot.read_bytes(), b"user snapshot\n")

    def test_bootstrap_preview_is_stable_and_performs_zero_project_writes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            request = _request(
                root,
                "bootstrap",
                [
                    (
                        "development-graph.json",
                        b'{"project":{"root":"."}}\n',
                        None,
                    ),
                    ("doc/product.md", b"# Product\n", None),
                ],
            )
            before = sorted(
                path.relative_to(root).as_posix() for path in root.rglob("*")
            )

            first = preview_project(request)
            second = preview_project(request)

            self.assertTrue(first.valid)
            self.assertEqual(first.to_dict(), second.to_dict())
            self.assertEqual(
                first.changed, ("development-graph.json", "doc/product.md")
            )
            self.assertEqual(
                sorted(path.relative_to(root).as_posix() for path in root.rglob("*")),
                before,
            )
            serialized = json.dumps(first.to_dict(), sort_keys=True)
            self.assertNotIn(str(root), serialized)
            self.assertEqual(len(first.base_digest), 64)
            self.assertEqual(len(first.plan_digest), 64)
            self.assertEqual(len(first.candidate_digest), 64)

    def test_apply_requires_exact_reviewed_base_and_plan_digests(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            request = _request(
                root, "bootstrap", [("graph.json", b"candidate\n", None)]
            )
            plan = preview_project(request)

            with self.assertRaisesRegex(StaleLifecycleError, "base digest"):
                apply_project(
                    request,
                    expected_base_digest="0" * 64,
                    expected_plan_digest=plan.plan_digest,
                    actor="tester",
                    authority="test:exact-plan",
                )
            with self.assertRaisesRegex(StaleLifecycleError, "plan digest"):
                apply_project(
                    request,
                    expected_base_digest=plan.base_digest,
                    expected_plan_digest="0" * 64,
                    actor="tester",
                    authority="test:exact-plan",
                )

            receipt = apply_project(
                request,
                expected_base_digest=plan.base_digest,
                expected_plan_digest=plan.plan_digest,
                actor="tester",
                authority="test:exact-plan",
            )

            self.assertEqual(receipt.state, "committed")
            self.assertTrue(receipt.applied)
            self.assertEqual((root / "graph.json").read_bytes(), b"candidate\n")
            manifest = json.loads(
                (
                    root
                    / ".development-graph"
                    / "transactions"
                    / receipt.transaction_id
                    / "manifest.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["state"], "committed")
            self.assertEqual(manifest["actor"], "tester")
            self.assertEqual(manifest["authority"], "test:exact-plan")
            self.assertEqual(manifest["artifacts"][0]["path"], "graph.json")
            self.assertNotIn(str(root), json.dumps(manifest, sort_keys=True))

    def test_apply_rejects_mode_only_drift_from_reviewed_base(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bootstrap = _request(
                root,
                "bootstrap",
                [("managed.txt", b"before\n", None)],
            )
            initial = preview_project(bootstrap)
            apply_project(
                bootstrap,
                expected_base_digest=initial.base_digest,
                expected_plan_digest=initial.plan_digest,
                actor="tester",
                authority="test:bootstrap",
            )
            target = root / "managed.txt"
            before_sha = hashlib.sha256(target.read_bytes()).hexdigest()
            update = _request(
                root,
                "update",
                [("managed.txt", b"after\n", before_sha)],
            )
            plan = preview_project(update)
            target.chmod(0o600)

            with self.assertRaisesRegex(
                StaleLifecycleError,
                "base digest",
            ):
                apply_project(
                    update,
                    expected_base_digest=plan.base_digest,
                    expected_plan_digest=plan.plan_digest,
                    actor="tester",
                    authority="test:mode-cas",
                )

            self.assertEqual(target.read_bytes(), b"before\n")
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)

    def test_bootstrap_existing_different_content_is_a_conflict_and_is_untouched(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "graph.json"
            target.write_bytes(b"user content\n")
            request = _request(
                root, "bootstrap", [("graph.json", b"candidate\n", None)]
            )

            plan = preview_project(request)

            self.assertFalse(plan.valid)
            self.assertEqual(plan.conflicts, ("graph.json",))
            with self.assertRaisesRegex(LifecycleError, "conflicts"):
                apply_project(
                    request,
                    expected_base_digest=plan.base_digest,
                    expected_plan_digest=plan.plan_digest,
                    actor="tester",
                    authority="test:no-overwrite",
                )
            self.assertEqual(target.read_bytes(), b"user content\n")
            self.assertFalse((root / ".development-graph").exists())

    def test_caught_late_failure_restores_every_byte_and_retains_rollback_evidence(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            request = _request(
                root,
                "bootstrap",
                [("a.txt", b"new-a\n", None), ("b.txt", b"new-b\n", None)],
            )
            plan = preview_project(request)
            from project_lifecycle import _replace_artifact

            calls = 0

            def fail_second(path: Path, payload: bytes, mode: int) -> None:
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("injected late write failure")
                _replace_artifact(path, payload, mode)

            with mock.patch(
                "project_lifecycle._replace_artifact", side_effect=fail_second
            ):
                receipt = apply_project(
                    request,
                    expected_base_digest=plan.base_digest,
                    expected_plan_digest=plan.plan_digest,
                    actor="tester",
                    authority="test:rollback",
                )

            self.assertFalse(receipt.applied)
            self.assertEqual(receipt.state, "rolled-back")
            self.assertFalse((root / "a.txt").exists())
            self.assertFalse((root / "b.txt").exists())
            transaction = (
                root
                / ".development-graph"
                / "transactions"
                / receipt.transaction_id
            )
            manifest = json.loads(
                (transaction / "manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["state"], "rolled-back")
            self.assertIn("injected late write failure", manifest["error"])
            self.assertTrue((transaction / "receipt.json").is_file())

    def test_interrupted_transaction_blocks_apply_and_explicit_recovery_restores_prior_bytes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "managed.txt"
            target.write_bytes(b"before\n")
            transaction = (
                root
                / ".development-graph"
                / "transactions"
                / "interrupted-transaction"
            )
            (transaction / "prior").mkdir(parents=True)
            (transaction / "prior" / "managed.txt").write_bytes(b"before\n")
            target.write_bytes(b"partially-written\n")

            current_sha = hashlib.sha256(target.read_bytes()).hexdigest()
            current_digest = hashlib.sha256(
                json.dumps(
                    [
                        {
                            "exists": True,
                            "mode": target.stat().st_mode & 0o777,
                            "path": "managed.txt",
                            "sha256": current_sha,
                        }
                    ],
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode("utf-8")
            ).hexdigest()
            manifest = {
                "schema_version": 1,
                "transaction_id": "interrupted-transaction",
                "operation": "update",
                "state": "committing",
                "base_digest": "1" * 64,
                "plan_digest": "2" * 64,
                "candidate_digest": current_digest,
                "actor": "interrupted",
                "authority": "test:interrupted",
                "artifacts": [
                    {
                        "path": "managed.txt",
                        "mode": 0o644,
                        "prior_exists": True,
                        "prior_sha256": hashlib.sha256(b"before\n").hexdigest(),
                        "candidate_sha256": current_sha,
                    }
                ],
                "error": None,
            }
            (transaction / "manifest.json").write_text(
                json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            next_request = _request(
                root, "bootstrap", [("next.txt", b"next\n", None)]
            )
            blocked = preview_project(next_request)
            self.assertFalse(blocked.valid)
            self.assertEqual(blocked.blocked, ("interrupted-transaction",))

            receipt = recover_project(
                root,
                "interrupted-transaction",
                expected_current_digest=current_digest,
                actor="recovery-operator",
                authority="test:explicit-recovery",
            )

            self.assertEqual(receipt.state, "rolled-back")
            self.assertEqual(target.read_bytes(), b"before\n")
            self.assertTrue((transaction / "receipt.json").is_file())
            self.assertTrue(preview_project(next_request).valid)

    def test_manifestless_transaction_directory_blocks_later_plans(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            orphan = (
                root
                / ".development-graph"
                / "transactions"
                / "orphan-transaction"
                / "prior"
            )
            orphan.mkdir(parents=True)
            request = _request(
                root,
                "bootstrap",
                [("doc/product.md", b"# Product\n", None)],
            )

            plan = preview_project(request)

            self.assertFalse(plan.valid)
            self.assertEqual(plan.blocked, ("orphan-transaction",))

    def test_recovery_restores_managed_inventory_after_interrupted_commit(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bootstrap = _request(
                root,
                "bootstrap",
                [("managed.txt", b"before\n", None)],
            )
            initial = preview_project(bootstrap)
            apply_project(
                bootstrap,
                expected_base_digest=initial.base_digest,
                expected_plan_digest=initial.plan_digest,
                actor="tester",
                authority="test:bootstrap",
            )
            before_sha = hashlib.sha256(b"before\n").hexdigest()
            update = _request(
                root,
                "update",
                [("managed.txt", b"candidate\n", before_sha)],
            )
            update_plan = preview_project(update)
            committed = apply_project(
                update,
                expected_base_digest=update_plan.base_digest,
                expected_plan_digest=update_plan.plan_digest,
                actor="tester",
                authority="test:update",
            )
            transaction = (
                root
                / ".development-graph"
                / "transactions"
                / committed.transaction_id
            )
            manifest_path = transaction / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["state"] = "committing"
            manifest_path.write_text(
                json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            candidate_sha = hashlib.sha256(b"candidate\n").hexdigest()
            interrupted = _request(
                root,
                "update",
                [("managed.txt", b"candidate\n", candidate_sha)],
            )
            current_digest = preview_project(interrupted).base_digest

            receipt = recover_project(
                root,
                committed.transaction_id,
                expected_current_digest=current_digest,
                actor="recovery-operator",
                authority="test:explicit-recovery",
            )

            self.assertEqual(receipt.state, "rolled-back")
            self.assertEqual((root / "managed.txt").read_bytes(), b"before\n")
            inventory = json.loads(
                (
                    root / ".development-graph" / "managed-artifacts.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(
                inventory["artifacts"],
                [{"path": "managed.txt", "sha256": before_sha}],
            )
            next_update = _request(
                root,
                "update",
                [("managed.txt", b"after\n", before_sha)],
            )
            self.assertTrue(preview_project(next_update).valid)

    def test_terminal_manifest_failure_rolls_back_artifacts_and_inventory(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bootstrap = _request(
                root,
                "bootstrap",
                [("managed.txt", b"before\n", None)],
            )
            initial = preview_project(bootstrap)
            apply_project(
                bootstrap,
                expected_base_digest=initial.base_digest,
                expected_plan_digest=initial.plan_digest,
                actor="tester",
                authority="test:bootstrap",
            )
            before_sha = hashlib.sha256(b"before\n").hexdigest()
            update = _request(
                root,
                "update",
                [("managed.txt", b"candidate\n", before_sha)],
            )
            plan = preview_project(update)
            from project_lifecycle import _write_manifest

            failed = False

            def fail_terminal(path: Path, value: dict[str, object]) -> None:
                nonlocal failed
                if value.get("state") == "committed" and not failed:
                    failed = True
                    raise OSError("injected terminal manifest failure")
                _write_manifest(path, value)

            with mock.patch(
                "project_lifecycle._write_manifest",
                side_effect=fail_terminal,
            ):
                receipt = apply_project(
                    update,
                    expected_base_digest=plan.base_digest,
                    expected_plan_digest=plan.plan_digest,
                    actor="tester",
                    authority="test:terminal-rollback",
                )

            self.assertFalse(receipt.applied)
            self.assertEqual(receipt.state, "rolled-back")
            self.assertEqual((root / "managed.txt").read_bytes(), b"before\n")
            inventory = json.loads(
                (
                    root / ".development-graph" / "managed-artifacts.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(
                inventory["artifacts"],
                [{"path": "managed.txt", "sha256": before_sha}],
            )
            transaction = (
                root
                / ".development-graph"
                / "transactions"
                / receipt.transaction_id
            )
            manifest = json.loads(
                (transaction / "manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["state"], "rolled-back")
            self.assertTrue((transaction / "receipt.json").is_file())

    def test_parent_symlink_swap_after_review_cannot_write_outside_project(
        self,
    ) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            tempfile.TemporaryDirectory() as outside_directory,
        ):
            root = Path(directory)
            outside = Path(outside_directory)
            request = _request(
                root,
                "bootstrap",
                [("nested/artifact.txt", b"candidate\n", None)],
            )
            plan = preview_project(request)
            from project_lifecycle import _snapshot_managed_inventory

            def swap_parent(
                project_root: Path,
                prior_root: Path,
                candidate: dict[str, object],
            ) -> dict[str, object]:
                record = _snapshot_managed_inventory(
                    project_root,
                    prior_root,
                    candidate,
                )
                (root / "nested").symlink_to(
                    outside,
                    target_is_directory=True,
                )
                return record

            with mock.patch(
                "project_lifecycle._snapshot_managed_inventory",
                side_effect=swap_parent,
            ):
                with self.assertRaisesRegex(
                    LifecycleError,
                    "escapes its root through a symlink",
                ):
                    apply_project(
                        request,
                        expected_base_digest=plan.base_digest,
                        expected_plan_digest=plan.plan_digest,
                        actor="tester",
                        authority="test:path-swap",
                    )

            self.assertFalse((outside / "artifact.txt").exists())

    def test_update_classifies_changed_preserved_invalidated_blocked_and_conflicts(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bootstrap = _request(
                root,
                "bootstrap",
                [
                    ("changed.txt", b"old\n", None),
                    ("preserved.txt", b"same\n", None),
                    ("blocked.txt", b"old\n", None),
                ],
            )
            initial = preview_project(bootstrap)
            apply_project(
                bootstrap,
                expected_base_digest=initial.base_digest,
                expected_plan_digest=initial.plan_digest,
                actor="tester",
                authority="test:bootstrap",
            )
            managed = json.loads(
                (
                    root / ".development-graph" / "managed-artifacts.json"
                ).read_text(encoding="utf-8")
            )["artifacts"]
            expected = {item["path"]: item["sha256"] for item in managed}
            (root / "unmanaged.txt").write_bytes(b"user\n")
            update = _request(
                root,
                "update",
                [
                    ("changed.txt", b"new\n", expected["changed.txt"]),
                    ("preserved.txt", b"same\n", expected["preserved.txt"]),
                    ("blocked.txt", b"new\n", None),
                    ("unmanaged.txt", b"replacement\n", None),
                    ("added.txt", b"added\n", None),
                ],
            )

            plan = preview_project(update)

            self.assertFalse(plan.valid)
            self.assertEqual(plan.changed, ("added.txt", "changed.txt"))
            self.assertEqual(plan.preserved, ("preserved.txt",))
            self.assertEqual(plan.invalidated, ("changed.txt",))
            self.assertEqual(plan.blocked, ("blocked.txt",))
            self.assertEqual(plan.conflicts, ("unmanaged.txt",))

    def test_update_changes_only_reviewed_managed_artifacts_at_reviewed_hashes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bootstrap = _request(
                root, "bootstrap", [("managed.txt", b"old\n", None)]
            )
            initial = preview_project(bootstrap)
            apply_project(
                bootstrap,
                expected_base_digest=initial.base_digest,
                expected_plan_digest=initial.plan_digest,
                actor="tester",
                authority="test:bootstrap",
            )
            inventory = json.loads(
                (
                    root / ".development-graph" / "managed-artifacts.json"
                ).read_text(encoding="utf-8")
            )
            old_digest = inventory["artifacts"][0]["sha256"]
            request = _request(
                root,
                "update",
                [
                    ("managed.txt", b"new\n", old_digest),
                    ("new-managed.txt", b"new file\n", None),
                ],
            )
            plan = preview_project(request)

            receipt = apply_project(
                request,
                expected_base_digest=plan.base_digest,
                expected_plan_digest=plan.plan_digest,
                actor="tester",
                authority="test:update",
            )

            self.assertEqual(receipt.state, "committed")
            self.assertEqual((root / "managed.txt").read_bytes(), b"new\n")
            self.assertEqual(
                (root / "new-managed.txt").read_bytes(), b"new file\n"
            )

    def test_update_cannot_adopt_same_content_unmanaged_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "user-owned.txt"
            target.write_bytes(b"user-owned\n")
            target.chmod(0o644)
            request = _request(
                root,
                "update",
                [("user-owned.txt", b"user-owned\n", None)],
            )

            plan = preview_project(request)

            self.assertFalse(plan.valid)
            self.assertEqual(plan.preserved, ())
            self.assertEqual(plan.conflicts, ("user-owned.txt",))
            with self.assertRaisesRegex(LifecycleError, "conflicts"):
                apply_project(
                    request,
                    expected_base_digest=plan.base_digest,
                    expected_plan_digest=plan.plan_digest,
                    actor="tester",
                    authority="test:no-adoption",
                )
            self.assertEqual(target.read_bytes(), b"user-owned\n")
            self.assertFalse((root / ".development-graph").exists())

    def test_apply_preflights_before_lock_and_revalidates_under_lock(
        self,
    ) -> None:
        from contextlib import contextmanager
        import project_lifecycle

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            request = _request(
                root, "bootstrap", [("graph.json", b"candidate\n", None)]
            )
            plan = preview_project(request)
            entered_lock = False
            preview_lock_states: list[bool] = []
            real_lock = project_lifecycle.file_lock
            real_preview = project_lifecycle.preview_project

            @contextmanager
            def tracked_lock(path: Path):
                nonlocal entered_lock
                with real_lock(path):
                    entered_lock = True
                    try:
                        yield
                    finally:
                        entered_lock = False

            def tracked_preview(value: dict[str, object]):
                preview_lock_states.append(entered_lock)
                return real_preview(value)

            with mock.patch(
                "project_lifecycle.file_lock", side_effect=tracked_lock
            ), mock.patch(
                "project_lifecycle.preview_project", side_effect=tracked_preview
            ):
                receipt = apply_project(
                    request,
                    expected_base_digest=plan.base_digest,
                    expected_plan_digest=plan.plan_digest,
                    actor="tester",
                    authority="test:locked-revalidation",
                )

            self.assertTrue(receipt.applied)
            self.assertEqual(preview_lock_states, [False, True])
            self.assertEqual((root / "graph.json").read_bytes(), b"candidate\n")

    def test_concurrent_apply_attempts_produce_one_commit_and_one_stale_refusal(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            request = _request(
                root, "bootstrap", [("graph.json", b"candidate\n", None)]
            )
            plan = preview_project(request)
            context = multiprocessing.get_context("fork")
            start = context.Event()
            output = context.Queue()
            processes = [
                context.Process(
                    target=_concurrent_apply,
                    args=(
                        str(root),
                        plan.base_digest,
                        plan.plan_digest,
                        start,
                        output,
                    ),
                )
                for _ in range(2)
            ]
            for process in processes:
                process.start()
            start.set()
            for process in processes:
                process.join(10)
                self.assertFalse(process.is_alive())
                self.assertEqual(process.exitcode, 0)
            outcomes = [output.get(timeout=2) for _ in processes]
            results = sorted(item[0] for item in outcomes)

            self.assertEqual(results, ["receipt", "stale"], outcomes)
            self.assertEqual((root / "graph.json").read_bytes(), b"candidate\n")

    def test_migration_operation_requires_authenticated_schema_migration_builder(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            request = _request(root, "migration", [])

            with self.assertRaisesRegex(LifecycleError, "authenticated"):
                preview_project(request)


if __name__ == "__main__":
    unittest.main()
