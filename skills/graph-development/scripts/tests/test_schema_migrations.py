from __future__ import annotations

import copy
import dataclasses
import hashlib
import json
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
TESTS = Path(__file__).resolve().parent
DATA = TESTS / "data" / "migrations"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(TESTS))

from fixtures import backlog_node, runtime_graph  # noqa: E402
from graph_core import projection_contract_errors, sha256_json  # noqa: E402
from graphctl import load_execution_graph, status_report, verify_runtime_integrity  # noqa: E402
from new_graph import build_graph, build_specification_manifest  # noqa: E402
from project_lifecycle import (  # noqa: E402
    LifecycleError,
    apply_project,
    preview_project,
)
from schema_migrations import (  # noqa: E402
    MigrationError,
    build_lifecycle_migration_request,
    migrate_document,
    plan_migration,
    restore_document,
    validate_migration_plan,
)
from validate_graph import canonical_checksum  # noqa: E402


def _load(name: str) -> dict:
    return json.loads((DATA / name).read_text(encoding="utf-8"))


def _event(
    *,
    sequence: int,
    previous_hash: str | None,
    run_id: str,
    graph_id: str,
    graph_checksum: str,
    run_version_before: int,
    run_version_after: int,
    action: str,
) -> dict:
    record = {
        "sequence": sequence,
        "previous_hash": previous_hash,
        "timestamp": "2026-01-01T00:00:00.000000Z",
        "run_id": run_id,
        "graph_id": graph_id,
        "graph_checksum": graph_checksum,
        "run_version_before": run_version_before,
        "run_version_after": run_version_after,
        "actor": "fixture",
        "action": action,
    }
    record["event_hash"] = sha256_json(record)
    return record


def _sealed_context() -> dict:
    source = {
        "schema_version": 0,
        "record_id": "context-fixture",
        "content": {"summary": "sealed source"},
    }
    source["digest"] = sha256_json(source)
    return source


def _bundle() -> dict:
    graph = build_graph(
        Path("migration-project"),
        "migration-product",
        "Migration Product",
        "specification-to-delivery",
        "test:migration-v0",
    )
    manifest = build_specification_manifest(graph)
    graph["schema_version"] = 0
    graph["project"]["root"] = "./"
    manifest["schema_version"] = 0
    manifest["specification_root"] = "./doc"
    old_checksum = canonical_checksum(graph)
    run_id = "00000000-0000-4000-8000-000000000001"
    event = _event(
        sequence=1,
        previous_hash=None,
        run_id=run_id,
        graph_id=graph["graph_id"],
        graph_checksum=old_checksum,
        run_version_before=0,
        run_version_after=1,
        action="run-initialized",
    )
    nodes = {
        node["id"]: {
            "state": "planned",
            "attempts": 0,
            "generation": 1,
            "lease": None,
            "approval": None,
            "completed_idempotency_keys": [],
            "evidence": [],
            "last_result": None,
        }
        for node in graph["nodes"]
    }
    first_node = graph["nodes"][0]["id"]
    nodes[first_node]["approval"] = {
        "decision": "approved",
        "run_id": run_id,
        "graph_checksum": old_checksum,
        "node_id": first_node,
        "gate_node_id": first_node,
        "approver": "fixture",
        "evidence_digest": "a" * 64,
        "approved_at": "2026-01-01T00:00:00.000000Z",
    }
    runtime = {
        "schema_version": 0,
        "run_id": run_id,
        "run_version": 1,
        "graph_id": graph["graph_id"],
        "graph_version": graph["graph_version"],
        "graph_checksum": old_checksum,
        "status": "active",
        "created_at": "2026-01-01T00:00:00.000000Z",
        "updated_at": "2026-01-01T00:00:00.000000Z",
        "last_event_sequence": 1,
        "last_event_hash": event["event_hash"],
        "feedback_traversals": {},
        "nodes": nodes,
    }
    return {
        "graph": graph,
        "manifest": manifest,
        "contexts": [_sealed_context()],
        "runtime": runtime,
        "ledger": [event],
    }


def _write_migration_project(directory: str) -> tuple[Path, dict]:
    root = Path(directory) / "authenticated-project"
    root.mkdir()
    source = _bundle()
    (root / "context").mkdir()
    control = root / ".development-graph"
    control.mkdir()
    (root / "development-graph.json").write_text(
        json.dumps(source["graph"], indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (root / "specification-manifest.json").write_text(
        json.dumps(source["manifest"], indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (root / "context" / "source.json").write_text(
        json.dumps(source["contexts"][0], indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (control / "run.json").write_text(
        json.dumps(source["runtime"], indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (control / "events.jsonl").write_text(
        "".join(
            json.dumps(record, separators=(",", ":"), sort_keys=True) + "\n"
            for record in source["ledger"]
        ),
        encoding="utf-8",
    )
    descriptor = {
        "schema_version": 1,
        "bundle": {
            "graph": "development-graph.json",
            "manifest": "specification-manifest.json",
            "contexts": ["context/source.json"],
            "runtime": ".development-graph/run.json",
            "ledger": ".development-graph/events.jsonl",
        },
        "outputs": {
            "graph": "development-graph.json",
            "manifest": "specification-manifest.json",
            "context_successors": ["context/source-v1.json"],
            "runtime": ".development-graph/run.json",
            "ledger": ".development-graph/events.jsonl",
        },
        "target_versions": {
            "graph": 1,
            "manifest": 1,
            "context": 1,
            "runtime": 1,
        },
    }
    return root, descriptor


def _legacy_v1_bundle() -> dict:
    graph = runtime_graph(Path("migration-project"))
    graph["nodes"].append(backlog_node("future-debt"))
    graph["entry_nodes"].append("future-debt")
    manifest = build_specification_manifest(graph)
    context = migrate_document("context", _sealed_context(), 0, 1, {})
    graph_checksum = canonical_checksum(graph)
    run_id = "00000000-0000-4000-8000-000000000021"
    initialized = _event(
        sequence=1,
        previous_hash=None,
        run_id=run_id,
        graph_id=graph["graph_id"],
        graph_checksum=graph_checksum,
        run_version_before=0,
        run_version_after=1,
        action="run-initialized",
    )
    nodes = {
        node["id"]: {
            "state": (
                "ready"
                if node["id"] in {"inspect-context", "future-debt"}
                else "blocked"
            ),
            "attempts": 0,
            "generation": 1,
            "lease": None,
            "approval": None,
            "completed_idempotency_keys": [],
            "evidence": [],
            "last_result": None,
        }
        for node in graph["nodes"]
    }
    legacy_result = {
        "status": "verified",
        "evidence_path": "evidence/inspect-context/attempt-1.json",
    }
    nodes["inspect-context"].update(
        {
            "state": "verified",
            "attempts": 1,
            "completed_idempotency_keys": ["legacy:inspect-context:1"],
            "evidence": [copy.deepcopy(legacy_result)],
            "last_result": copy.deepcopy(legacy_result),
        }
    )
    nodes["implement-change"]["state"] = "ready"
    nodes["implement-change"]["approval"] = {
        "decision": "approved",
        "run_id": run_id,
        "graph_checksum": graph_checksum,
        "node_id": "implement-change",
        "gate_node_id": "inspect-context",
        "approver": "legacy-owner",
        "evidence_digest": "b" * 64,
        "approved_at": "2026-01-01T00:00:00.000000Z",
    }
    completed = _event(
        sequence=2,
        previous_hash=initialized["event_hash"],
        run_id=run_id,
        graph_id=graph["graph_id"],
        graph_checksum=graph_checksum,
        run_version_before=1,
        run_version_after=2,
        action="node-completed",
    )
    runtime = {
        "schema_version": 1,
        "run_id": run_id,
        "run_version": 2,
        "graph_id": graph["graph_id"],
        "graph_version": graph["graph_version"],
        "graph_checksum": graph_checksum,
        "status": "active",
        "created_at": "2026-01-01T00:00:00.000000Z",
        "updated_at": "2026-01-01T00:00:00.000000Z",
        "last_event_sequence": 2,
        "last_event_hash": completed["event_hash"],
        "feedback_traversals": {},
        "nodes": nodes,
    }
    return {
        "graph": graph,
        "manifest": manifest,
        "contexts": [context],
        "runtime": runtime,
        "ledger": [initialized, completed],
    }


def _runtime_conversion(
    source: dict,
    *,
    enrolled_node_ids: list[str] | None = None,
) -> dict:
    graph = source["graph"]
    runtime = source["runtime"]
    authority = "test:approved-v1-to-v2-conversion"
    actor = "legacy-owner"
    enrolled = set(
        enrolled_node_ids
        if enrolled_node_ids is not None
        else [
            node["id"]
            for node in graph["nodes"]
            if node["id"] != "future-debt"
        ]
    )
    node_ids = [node["id"] for node in graph["nodes"] if node["id"] in enrolled]
    outcome = "Complete the approved legacy run outcome."
    contract = {
        "contract_id": "deliver-legacy-outcome",
        "contract_version": 1,
        "outcome": outcome,
        "outcome_digest": sha256_json(outcome),
        "requirement_ids": ["requirement:legacy-outcome"],
        "non_goals": ["Execute unrelated durable backlog."],
        "acceptance_ids": ["acceptance:legacy-finish"],
        "acceptance_digest": sha256_json(["acceptance:legacy-finish"]),
        "terminal_node_ids": list(graph["terminal_nodes"]),
        "authority_evidence": [authority],
        "authority_digest": sha256_json([authority]),
        "impact_policy": {
            "max_verified_node_count": 3,
            "max_verified_node_fraction": 0.75,
        },
        "created_at": "2026-01-01T00:00:00.000000Z",
        "actor": actor,
    }
    projection = {
        "schema_version": 1,
        "run_id": runtime["run_id"],
        "graph_id": graph["graph_id"],
        "source_graph_version": graph["graph_version"],
        "source_graph_checksum": canonical_checksum(graph),
        "contract_id": contract["contract_id"],
        "contract_version": contract["contract_version"],
        "projection_version": 1,
        "nodes": [
            copy.deepcopy(node) for node in graph["nodes"] if node["id"] in enrolled
        ],
        "edges": [
            copy.deepcopy(edge)
            for edge in graph["edges"]
            if edge["from"] in enrolled and edge["to"] in enrolled
        ],
        "entry_nodes": [node for node in graph["entry_nodes"] if node in enrolled],
        "terminal_nodes": list(graph["terminal_nodes"]),
        "semantic_dependencies": [],
        "requirement_mappings": {"requirement:legacy-outcome": node_ids},
        "acceptance_mappings": {
            "acceptance:legacy-finish": list(graph["terminal_nodes"])
        },
        "provenance": {"actor": actor, "authority": authority},
    }
    return {
        "outcome_contract": contract,
        "projection": projection,
        "projection_path": (
            f"projections/{runtime['run_id']}-v{projection['projection_version']}.json"
        ),
        "actor": actor,
        "authority": authority,
    }


def _conversion_targets() -> dict[str, int]:
    return {"graph": 1, "manifest": 1, "context": 1, "runtime": 2}


def _write_v1_conversion_project(
    directory: str,
) -> tuple[Path, dict, dict, dict, bytes]:
    root = Path(directory) / "v1-conversion-project"
    root.mkdir()
    source = _legacy_v1_bundle()
    conversion = _runtime_conversion(source)
    (root / "context").mkdir()
    (root / "conversion").mkdir()
    control = root / ".development-graph"
    evidence_dir = control / "evidence" / "inspect-context"
    evidence_dir.mkdir(parents=True)
    (root / "development-graph.json").write_text(
        json.dumps(source["graph"], indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (root / "specification-manifest.json").write_text(
        json.dumps(source["manifest"], indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (root / "context" / "source-v1.json").write_text(
        json.dumps(source["contexts"][0], indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (control / "run.json").write_text(
        json.dumps(source["runtime"], indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    ledger_bytes = "".join(
        json.dumps(record, separators=(", ", ": "), sort_keys=False) + "\n"
        for record in source["ledger"]
    ).encode("utf-8")
    (control / "events.jsonl").write_bytes(ledger_bytes)
    evidence_path = evidence_dir / "attempt-1.json"
    evidence_path.write_bytes(b'{"legacy": true}\n')
    evidence_path.chmod(0o640)
    (root / "conversion" / "outcome-contract.json").write_text(
        json.dumps(conversion["outcome_contract"], indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (root / "conversion" / "projection.json").write_text(
        json.dumps(conversion["projection"], indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    descriptor = {
        "schema_version": 2,
        "bundle": {
            "graph": "development-graph.json",
            "manifest": "specification-manifest.json",
            "contexts": ["context/source-v1.json"],
            "runtime": ".development-graph/run.json",
            "ledger": ".development-graph/events.jsonl",
        },
        "outputs": {
            "projection": f".development-graph/{conversion['projection_path']}",
            "runtime": ".development-graph/run.json",
            "ledger": ".development-graph/events.jsonl",
        },
        "target_versions": _conversion_targets(),
        "runtime_conversion": {
            "outcome_contract": "conversion/outcome-contract.json",
            "projection": "conversion/projection.json",
            "actor": conversion["actor"],
            "authority": conversion["authority"],
        },
    }
    return root, descriptor, source, conversion, ledger_bytes


class SchemaMigrationTests(unittest.TestCase):
    def test_frozen_graph_and_manifest_v0_fixtures_migrate_to_v1_deterministically(
        self,
    ) -> None:
        for kind in ("graph", "manifest"):
            before = _load(f"{kind}-v0.json")
            expected = _load(f"{kind}-v1.json")

            first = migrate_document(kind, before, 0, 1, {})
            second = migrate_document(kind, before, 0, 1, {})

            self.assertEqual(first, expected)
            self.assertEqual(first, second)
            self.assertEqual(before, _load(f"{kind}-v0.json"))

    def test_missing_steps_and_unknown_future_versions_fail_closed(self) -> None:
        with self.assertRaisesRegex(MigrationError, "missing migration step"):
            migrate_document("graph", {"schema_version": -1}, -1, 1, {})
        with self.assertRaisesRegex(MigrationError, "future"):
            migrate_document("graph", {"schema_version": 2}, 2, 2, {})
        plan = plan_migration(
            _bundle(),
            {"graph": 2, "manifest": 1, "context": 1, "runtime": 1},
        )
        self.assertFalse(plan.valid)
        self.assertTrue(any("future" in error for error in plan.errors))

    def test_context_migration_creates_successor_and_preserves_sealed_history(
        self,
    ) -> None:
        source = _sealed_context()
        before = copy.deepcopy(source)

        successor = migrate_document("context", source, 0, 1, {})

        self.assertEqual(source, before)
        self.assertEqual(successor["predecessor_digest"], source["digest"])
        self.assertEqual(successor["sealed_source"], source)
        body = {key: value for key, value in successor.items() if key != "digest"}
        self.assertEqual(successor["digest"], sha256_json(body))

    def test_runtime_migration_refuses_active_nodes_and_invalid_ledgers(self) -> None:
        active = _bundle()
        node_id = next(iter(active["runtime"]["nodes"]))
        active["runtime"]["nodes"][node_id]["state"] = "running"
        active["runtime"]["nodes"][node_id]["lease"] = {
            "owner": "worker",
            "issued_at": "2026-01-01T00:00:00.000000Z",
            "expires_at": "2026-01-01T00:05:00.000000Z",
            "idempotency_key": "migration-active-fixture",
        }

        active_plan = plan_migration(
            active,
            {"graph": 1, "manifest": 1, "context": 1, "runtime": 1},
        )

        self.assertFalse(active_plan.valid)
        self.assertTrue(any("active node" in error for error in active_plan.errors))

        corrupt = _bundle()
        corrupt["ledger"][0]["event_hash"] = "0" * 64
        corrupt_plan = plan_migration(
            corrupt,
            {"graph": 1, "manifest": 1, "context": 1, "runtime": 1},
        )
        self.assertFalse(corrupt_plan.valid)
        self.assertTrue(any("event hash" in error for error in corrupt_plan.errors))

    def test_runtime_preserves_chain_and_invalidates_checksum_bound_approvals(
        self,
    ) -> None:
        source = _bundle()
        source_copy = copy.deepcopy(source)

        plan = plan_migration(
            source,
            {"graph": 1, "manifest": 1, "context": 1, "runtime": 1},
        )

        self.assertTrue(plan.valid, plan.errors)
        self.assertEqual(validate_migration_plan(plan, source), ())
        candidate = plan.candidate_bundle
        self.assertEqual(source, source_copy)
        self.assertEqual(candidate["ledger"][:-1], source["ledger"])
        self.assertEqual(
            candidate["ledger"][-1]["previous_hash"],
            source["ledger"][-1]["event_hash"],
        )
        self.assertEqual(
            candidate["runtime"]["last_event_hash"],
            candidate["ledger"][-1]["event_hash"],
        )
        self.assertNotEqual(
            source["runtime"]["graph_checksum"],
            candidate["runtime"]["graph_checksum"],
        )
        self.assertTrue(
            all(
                state["approval"] is None
                for state in candidate["runtime"]["nodes"].values()
            )
        )

    def test_restore_returns_exact_source_bytes_and_mode_at_candidate_binding(
        self,
    ) -> None:
        source_bytes = b'{\n "schema_version": 0, "project": {"root": "./"}\n}\n\n'
        before = json.loads(source_bytes)
        migrated = migrate_document("graph", before, 0, 1, {})
        candidate_bytes = (
            json.dumps(migrated, indent=2, sort_keys=True).encode("utf-8") + b"\n"
        )

        restored = restore_document(
            "graph",
            migrated,
            {
                "source_bytes": source_bytes,
                "source_mode": 0o640,
                "candidate_sha256": hashlib.sha256(candidate_bytes).hexdigest(),
                "candidate_bytes": candidate_bytes,
                "from_version": 0,
                "to_version": 1,
            },
        )
        plan = plan_migration(
            _bundle(),
            {"graph": 1, "manifest": 1, "context": 1, "runtime": 1},
        )

        self.assertEqual(restored.content, source_bytes)
        self.assertEqual(restored.mode, 0o640)
        wrong_source = b'{"schema_version":0,"project":{"root":"other"}}\n'
        with self.assertRaisesRegex(MigrationError, "source bytes"):
            restore_document(
                "graph",
                migrated,
                {
                    "source_bytes": wrong_source,
                    "source_mode": 0o640,
                    "candidate_sha256": hashlib.sha256(
                        candidate_bytes
                    ).hexdigest(),
                    "candidate_bytes": candidate_bytes,
                    "from_version": 0,
                    "to_version": 1,
                },
            )
        self.assertIn("runtime-ledger-append", plan.receipt["irreversible_boundaries"])
        self.assertEqual(
            plan.receipt["restore_strategy"],
            "exact-source-bytes-and-mode",
        )

    def test_source_and_candidate_manifest_and_runtime_schemas_are_complete(
        self,
    ) -> None:
        malformed_manifest = _bundle()
        malformed_manifest["manifest"]["unknown"] = True
        manifest_plan = plan_migration(
            malformed_manifest,
            {"graph": 1, "manifest": 1, "context": 1, "runtime": 1},
        )
        self.assertFalse(manifest_plan.valid)
        self.assertTrue(
            any(
                "manifest" in error and "unknown" in error
                for error in manifest_plan.errors
            ),
            manifest_plan.errors,
        )

        malformed_runtime = _bundle()
        malformed_runtime["runtime"]["nodes"].pop(
            next(iter(malformed_runtime["runtime"]["nodes"]))
        )
        runtime_plan = plan_migration(
            malformed_runtime,
            {"graph": 1, "manifest": 1, "context": 1, "runtime": 1},
        )
        self.assertFalse(runtime_plan.valid)
        self.assertTrue(
            any(
                "runtime node inventory" in error
                for error in runtime_plan.errors
            ),
            runtime_plan.errors,
        )
        malformed_lease = _bundle()
        lease_node = next(iter(malformed_lease["runtime"]["nodes"].values()))
        lease_node["lease"] = {"owner": "incomplete"}
        lease_plan = plan_migration(
            malformed_lease,
            {"graph": 1, "manifest": 1, "context": 1, "runtime": 1},
        )
        self.assertFalse(lease_plan.valid)
        self.assertTrue(
            any("lease schema" in error for error in lease_plan.errors),
            lease_plan.errors,
        )

    def test_ledger_binds_identity_and_run_version_continuity(self) -> None:
        for field, value, expected in (
            ("run_id", "other-run", "run_id"),
            ("graph_id", "other-graph", "graph_id"),
            ("run_version_before", 7, "run version"),
        ):
            source = _bundle()
            source["ledger"][0][field] = value
            body = {
                key: item
                for key, item in source["ledger"][0].items()
                if key != "event_hash"
            }
            source["ledger"][0]["event_hash"] = sha256_json(body)
            source["runtime"]["last_event_hash"] = source["ledger"][0]["event_hash"]
            plan = plan_migration(
                source,
                {"graph": 1, "manifest": 1, "context": 1, "runtime": 1},
            )
            self.assertFalse(plan.valid)
            self.assertTrue(
                any(expected in error for error in plan.errors),
                (field, plan.errors),
            )

    def test_runtime_at_target_version_is_rebound_and_approvals_are_invalidated(
        self,
    ) -> None:
        source = _bundle()
        source["runtime"]["schema_version"] = 1

        plan = plan_migration(
            source,
            {"graph": 1, "manifest": 1, "context": 1, "runtime": 1},
        )

        self.assertTrue(plan.valid, plan.errors)
        self.assertEqual(
            plan.candidate_bundle["runtime"]["graph_checksum"],
            canonical_checksum(plan.candidate_bundle["graph"]),
        )
        self.assertTrue(
            all(
                node["approval"] is None
                for node in plan.candidate_bundle["runtime"]["nodes"].values()
            )
        )

    def test_all_current_bundle_is_a_valid_noop_without_context_self_successor(
        self,
    ) -> None:
        migrated = plan_migration(
            _bundle(),
            {"graph": 1, "manifest": 1, "context": 1, "runtime": 1},
        )
        self.assertTrue(migrated.valid, migrated.errors)
        current = copy.deepcopy(migrated.candidate_bundle)
        current["contexts"] = copy.deepcopy(current["context_successors"])
        current.pop("context_successors")

        plan = plan_migration(
            current,
            {"graph": 1, "manifest": 1, "context": 1, "runtime": 1},
        )

        self.assertTrue(plan.valid, plan.errors)
        self.assertEqual(plan.steps, ())
        self.assertNotIn("context_successors", plan.candidate_bundle)
        self.assertEqual(plan.candidate_bundle, current)
        self.assertEqual(plan.receipt["irreversible_boundaries"], [])

    def test_appended_event_names_irreversible_boundary(self) -> None:
        plan = plan_migration(
            _bundle(),
            {"graph": 1, "manifest": 1, "context": 1, "runtime": 1},
        )
        self.assertTrue(plan.valid, plan.errors)
        event = plan.candidate_bundle["ledger"][-1]
        self.assertEqual(event["action"], "schema-migrated")
        self.assertEqual(
            event["irreversible_boundary"],
            "runtime-ledger-append",
        )

    def test_plan_validation_binds_steps_and_receipt(self) -> None:
        source = _bundle()
        plan = plan_migration(
            source,
            {"graph": 1, "manifest": 1, "context": 1, "runtime": 1},
        )
        self.assertTrue(plan.valid, plan.errors)

        changed_steps = dataclasses.replace(plan, steps=())
        changed_receipt = dataclasses.replace(
            plan,
            receipt={**plan.receipt, "restore_strategy": "forged"},
        )

        self.assertIn(
            "migration steps differ",
            validate_migration_plan(changed_steps, source),
        )
        self.assertIn(
            "migration receipt differs",
            validate_migration_plan(changed_receipt, source),
        )


    def test_v1_to_v2_plan_is_explicit_deterministic_and_preserves_retained_state(
        self,
    ) -> None:
        source = _legacy_v1_bundle()
        conversion = _runtime_conversion(source)
        before = copy.deepcopy(source)
        legacy = plan_migration(
            source,
            {"graph": 1, "manifest": 1, "context": 1, "runtime": 1},
        )
        missing_conversion = plan_migration(source, _conversion_targets())
        first = plan_migration(
            source,
            _conversion_targets(),
            runtime_conversion=conversion,
        )
        second = plan_migration(
            source,
            _conversion_targets(),
            runtime_conversion=conversion,
        )
        self.assertTrue(legacy.valid, legacy.errors)
        self.assertEqual(legacy.candidate_bundle, source)
        self.assertFalse(missing_conversion.valid)
        self.assertTrue(
            any("explicit runtime conversion" in error for error in missing_conversion.errors),
            missing_conversion.errors,
        )
        self.assertTrue(first.valid, first.errors)
        self.assertEqual(first, second)
        self.assertEqual(source, before)
        self.assertEqual(
            validate_migration_plan(first, source, runtime_conversion=conversion),
            (),
        )
        candidate = first.candidate_bundle
        self.assertEqual(candidate["projection"], conversion["projection"])
        self.assertEqual(
            projection_contract_errors(
                source["graph"],
                conversion["outcome_contract"],
                candidate["projection"],
                expected_graph_checksum=canonical_checksum(source["graph"]),
                expected_run_id=source["runtime"]["run_id"],
            ),
            [],
        )
        self.assertEqual(candidate["runtime"]["schema_version"], 2)
        self.assertNotIn("future-debt", candidate["runtime"]["nodes"])
        for node_id in candidate["runtime"]["nodes"]:
            self.assertEqual(
                json.dumps(candidate["runtime"]["nodes"][node_id], sort_keys=True),
                json.dumps(source["runtime"]["nodes"][node_id], sort_keys=True),
            )
        legacy_approval = candidate["runtime"]["nodes"]["implement-change"]["approval"]
        self.assertEqual(
            legacy_approval,
            source["runtime"]["nodes"]["implement-change"]["approval"],
        )
        self.assertNotEqual(
            legacy_approval["graph_checksum"],
            candidate["runtime"]["projection"]["checksum"],
        )
        self.assertEqual(candidate["ledger"][:-1], source["ledger"])
        boundary = candidate["ledger"][-1]
        self.assertEqual(boundary["action"], "schema-migrated")
        self.assertEqual(boundary["source_versions"]["runtime"], 1)
        self.assertEqual(boundary["target_versions"]["runtime"], 2)
        self.assertEqual(
            boundary["projection_checksum"],
            candidate["runtime"]["projection"]["checksum"],
        )

    def test_v1_to_v2_refuses_active_or_nonpristine_excluded_nodes(self) -> None:
        cases = (
            ("running", "active node"),
            ("verifying", "active node"),
            ("lease", "active node"),
            ("attempts", "excluded node"),
            ("generation", "excluded node"),
            ("approval", "excluded node"),
            ("idempotency", "excluded node"),
            ("evidence", "excluded node"),
            ("last_result", "excluded node"),
        )
        for case, expected in cases:
            with self.subTest(case=case):
                source = _legacy_v1_bundle()
                state = source["runtime"]["nodes"]["future-debt"]
                if case in {"running", "verifying"}:
                    state["state"] = case
                elif case == "lease":
                    state["lease"] = {
                        "owner": "legacy-worker",
                        "issued_at": "2000-01-01T00:00:00Z",
                        "expires_at": "2000-01-01T00:01:00Z",
                        "idempotency_key": "expired-but-still-bound",
                    }
                elif case == "attempts":
                    state["attempts"] = 1
                elif case == "generation":
                    state["generation"] = 2
                elif case == "approval":
                    state["approval"] = {
                        "decision": "approved",
                        "run_id": source["runtime"]["run_id"],
                        "graph_checksum": source["runtime"]["graph_checksum"],
                        "node_id": "future-debt",
                        "gate_node_id": "inspect-context",
                        "approver": "legacy-owner",
                        "evidence_digest": "c" * 64,
                        "approved_at": "2026-01-01T00:00:00Z",
                    }
                elif case == "idempotency":
                    state["completed_idempotency_keys"] = ["legacy:key"]
                elif case == "evidence":
                    state["evidence"] = [{"legacy": True}]
                else:
                    state["last_result"] = {"legacy": True}
                before = copy.deepcopy(source)
                plan = plan_migration(
                    source,
                    _conversion_targets(),
                    runtime_conversion=_runtime_conversion(source),
                )
                self.assertFalse(plan.valid)
                self.assertTrue(any(expected in error for error in plan.errors), plan.errors)
                self.assertEqual(source, before)

    def test_v1_to_v2_binds_authority_run_identity_and_projection_closure(
        self,
    ) -> None:
        source = _legacy_v1_bundle()
        baseline = _runtime_conversion(source)
        variants: list[tuple[str, dict, str]] = []
        actor_mismatch = copy.deepcopy(baseline)
        actor_mismatch["actor"] = "different-actor"
        variants.append(("actor", actor_mismatch, "actor"))
        authority_mismatch = copy.deepcopy(baseline)
        authority_mismatch["projection"]["provenance"]["authority"] = "other"
        variants.append(("authority", authority_mismatch, "authority"))
        run_mismatch = copy.deepcopy(baseline)
        run_mismatch["projection"]["run_id"] = "00000000-0000-4000-8000-000000000099"
        variants.append(("run", run_mismatch, "run ID"))
        missing_prerequisite = _runtime_conversion(
            source,
            enrolled_node_ids=[
                node["id"]
                for node in source["graph"]["nodes"]
                if node["id"] not in {"future-debt", "run-tests"}
            ],
        )
        variants.append(("closure", missing_prerequisite, "prerequisite"))
        for case, conversion, expected in variants:
            with self.subTest(case=case):
                plan = plan_migration(
                    source,
                    _conversion_targets(),
                    runtime_conversion=conversion,
                )
                self.assertFalse(plan.valid)
                self.assertTrue(any(expected in error for error in plan.errors), plan.errors)

    def test_conversion_preview_binds_ledger_evidence_and_apply_authority(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, descriptor, _, conversion, ledger_bytes = _write_v1_conversion_project(directory)
            evidence_path = root / ".development-graph" / "evidence" / "inspect-context" / "attempt-1.json"
            protected_before = {
                path.relative_to(root).as_posix(): (
                    path.read_bytes(), stat.S_IMODE(path.stat().st_mode)
                )
                for path in root.rglob("*")
                if path.is_file()
            }
            request, migration = build_lifecycle_migration_request(root, descriptor)
            preview = preview_project(request)
            self.assertTrue(migration.valid, migration.errors)
            self.assertTrue(preview.valid, preview.errors)
            self.assertEqual(
                [artifact["path"] for artifact in request["artifacts"]],
                [
                    f".development-graph/{conversion['projection_path']}",
                    ".development-graph/run.json",
                    ".development-graph/events.jsonl",
                ],
            )
            ledger_candidate = request["artifacts"][-1]["content"]
            self.assertTrue(ledger_candidate.startswith(ledger_bytes))
            self.assertEqual(
                ledger_candidate[len(ledger_bytes):],
                json.dumps(
                    migration.candidate_bundle["ledger"][-1],
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode("utf-8") + b"\n",
            )
            receipt = request["metadata"]["migration_receipt"]
            self.assertEqual(receipt["required_actor"], conversion["actor"])
            self.assertEqual(receipt["required_authority"], conversion["authority"])
            evidence_binding = next(
                item for item in receipt["evidence_bindings"]
                if item["path"].endswith("attempt-1.json")
            )
            self.assertEqual(
                evidence_binding["sha256"],
                hashlib.sha256(evidence_path.read_bytes()).hexdigest(),
            )
            self.assertEqual(evidence_binding["mode"], 0o640)
            self.assertFalse(any(
                artifact["path"].startswith(".development-graph/evidence/")
                for artifact in request["artifacts"]
            ))
            self.assertEqual(
                protected_before,
                {
                    path.relative_to(root).as_posix(): (
                        path.read_bytes(), stat.S_IMODE(path.stat().st_mode)
                    )
                    for path in root.rglob("*")
                    if path.is_file()
                },
            )
            with self.assertRaisesRegex(LifecycleError, "conversion actor"):
                apply_project(
                    request,
                    expected_base_digest=preview.base_digest,
                    expected_plan_digest=preview.plan_digest,
                    actor="different-actor",
                    authority=conversion["authority"],
                )
            evidence_path.write_bytes(b'{"legacy": "drifted"}\n')
            with self.assertRaisesRegex(LifecycleError, "authenticated migration"):
                apply_project(
                    request,
                    expected_base_digest=preview.base_digest,
                    expected_plan_digest=preview.plan_digest,
                    actor=conversion["actor"],
                    authority=conversion["authority"],
                )

    def test_v1_to_v2_apply_keeps_legacy_prefix_and_runtime_is_operable(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, descriptor, source, conversion, ledger_bytes = _write_v1_conversion_project(directory)
            protected_paths = [
                root / "development-graph.json",
                root / "specification-manifest.json",
                root / "context" / "source-v1.json",
                root / ".development-graph" / "evidence" / "inspect-context" / "attempt-1.json",
            ]
            protected_before = {
                path.relative_to(root).as_posix(): (
                    path.read_bytes(), stat.S_IMODE(path.stat().st_mode)
                )
                for path in protected_paths
            }
            request, _ = build_lifecycle_migration_request(root, descriptor)
            preview = preview_project(request)
            receipt = apply_project(
                request,
                expected_base_digest=preview.base_digest,
                expected_plan_digest=preview.plan_digest,
                actor=conversion["actor"],
                authority=conversion["authority"],
            )
            self.assertEqual(receipt.state, "committed")
            self.assertEqual(
                protected_before,
                {
                    path.relative_to(root).as_posix(): (
                        path.read_bytes(), stat.S_IMODE(path.stat().st_mode)
                    )
                    for path in protected_paths
                },
            )
            control = root / ".development-graph"
            projection_path = control / conversion["projection_path"]
            self.assertEqual(
                json.loads(projection_path.read_text(encoding="utf-8")),
                conversion["projection"],
            )
            self.assertTrue((control / "events.jsonl").read_bytes().startswith(ledger_bytes))
            run = json.loads((control / "run.json").read_text(encoding="utf-8"))
            self.assertEqual(run["schema_version"], 2)
            self.assertEqual(verify_runtime_integrity(root / "development-graph.json", control), [])
            execution = load_execution_graph(root / "development-graph.json", control)
            self.assertEqual({node["id"] for node in execution["nodes"]}, set(run["nodes"]))
            self.assertNotIn("future-debt", run["nodes"])
            self.assertEqual(
                run["nodes"]["inspect-context"]["evidence"],
                source["runtime"]["nodes"]["inspect-context"]["evidence"],
            )
            self.assertNotEqual(
                run["nodes"]["implement-change"]["approval"]["graph_checksum"],
                run["projection"]["checksum"],
            )

    def test_v1_to_v2_write_failures_restore_bytes_modes_and_remove_projection(
        self,
    ) -> None:
        for failed_path in (
            "projection",
            ".development-graph/run.json",
            ".development-graph/events.jsonl",
        ):
            with self.subTest(failed_path=failed_path), tempfile.TemporaryDirectory() as directory:
                root, descriptor, _, conversion, _ = _write_v1_conversion_project(directory)
                projection_relative = f".development-graph/{conversion['projection_path']}"
                selected_failure = projection_relative if failed_path == "projection" else failed_path
                request, _ = build_lifecycle_migration_request(root, descriptor)
                preview = preview_project(request)
                protected = [
                    root / "development-graph.json",
                    root / "specification-manifest.json",
                    root / "context" / "source-v1.json",
                    root / ".development-graph" / "run.json",
                    root / ".development-graph" / "events.jsonl",
                    root / ".development-graph" / "evidence" / "inspect-context" / "attempt-1.json",
                ]
                before = {
                    path.relative_to(root).as_posix(): (
                        path.read_bytes(), stat.S_IMODE(path.stat().st_mode)
                    )
                    for path in protected
                }
                import project_lifecycle
                original_replace = project_lifecycle._replace_project_artifact
                failed = False
                def fail_once(
                    project_root: Path,
                    relative: str,
                    payload: bytes,
                    mode: int,
                ) -> None:
                    nonlocal failed
                    if relative == selected_failure and not failed:
                        failed = True
                        raise OSError(f"injected conversion failure: {relative}")
                    original_replace(project_root, relative, payload, mode)
                with mock.patch(
                    "project_lifecycle._replace_project_artifact",
                    side_effect=fail_once,
                ):
                    receipt = apply_project(
                        request,
                        expected_base_digest=preview.base_digest,
                        expected_plan_digest=preview.plan_digest,
                        actor=conversion["actor"],
                        authority=conversion["authority"],
                    )
                self.assertEqual(receipt.state, "rolled-back")
                self.assertEqual(
                    before,
                    {
                        path.relative_to(root).as_posix(): (
                            path.read_bytes(), stat.S_IMODE(path.stat().st_mode)
                        )
                        for path in protected
                    },
                )
                self.assertFalse((root / projection_relative).exists())

    def test_schema_v1_runtime_remains_whole_graph_without_conversion(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, _, source, _, _ = _write_v1_conversion_project(directory)
            control = root / ".development-graph"
            execution = load_execution_graph(root / "development-graph.json", control)
            report = status_report(root / "development-graph.json", control)
            self.assertEqual(
                {node["id"] for node in execution["nodes"]},
                {node["id"] for node in source["graph"]["nodes"]},
            )
            self.assertIn("future-debt", report["ready_nodes"])
            self.assertNotIn("projection_checksum", report)
            self.assertEqual(verify_runtime_integrity(root / "development-graph.json", control), [])

    def test_generic_or_mutated_migration_requests_cannot_forge_authentication(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, descriptor = _write_migration_project(directory)
            request, _ = build_lifecycle_migration_request(root, descriptor)
            forged = dict(request)
            with self.assertRaisesRegex(LifecycleError, "authenticated"):
                preview_project(forged)

            request["artifacts"][0]["content"] += b" "
            with self.assertRaisesRegex(LifecycleError, "binding"):
                preview_project(request)

    def test_apply_revalidates_nonoutput_sources_under_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, descriptor = _write_migration_project(directory)
            request, _ = build_lifecycle_migration_request(root, descriptor)
            plan = preview_project(request)
            (root / "context" / "source.json").write_text(
                '{"schema_version":0}\n',
                encoding="utf-8",
            )

            with self.assertRaisesRegex(LifecycleError, "authenticated migration"):
                apply_project(
                    request,
                    expected_base_digest=plan.base_digest,
                    expected_plan_digest=plan.plan_digest,
                    actor="migration-test",
                    authority="test:locked-revalidation",
                )

    def test_migration_apply_preserves_authenticated_phase_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, descriptor = _write_migration_project(directory)
            request, _ = build_lifecycle_migration_request(root, descriptor)
            plan = preview_project(request)
            writes: list[str] = []
            from project_lifecycle import _replace_artifact

            def record_write(path: Path, payload: bytes, mode: int) -> None:
                try:
                    writes.append(path.relative_to(root).as_posix())
                except ValueError:
                    pass
                _replace_artifact(path, payload, mode)

            with mock.patch(
                "project_lifecycle._replace_artifact",
                side_effect=record_write,
            ):
                receipt = apply_project(
                    request,
                    expected_base_digest=plan.base_digest,
                    expected_plan_digest=plan.plan_digest,
                    actor="migration-test",
                    authority="test:phase-order",
                )

            self.assertEqual(receipt.state, "committed")
            self.assertEqual(
                writes[:5],
                [
                    "development-graph.json",
                    "specification-manifest.json",
                    "context/source-v1.json",
                    ".development-graph/run.json",
                    ".development-graph/events.jsonl",
                ],
            )

    def test_projectctl_rejects_generic_migration_request_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, descriptor = _write_migration_project(directory)
            request, _ = build_lifecycle_migration_request(root, descriptor)
            serializable = {
                **request,
                "project_root": str(root),
                "artifacts": [
                    {
                        key: (
                            __import__("base64").b64encode(value).decode("ascii")
                            if key == "content"
                            else value
                        )
                        for key, value in artifact.items()
                        if key != "content"
                    }
                    | {
                        "content_base64": __import__("base64")
                        .b64encode(artifact["content"])
                        .decode("ascii")
                    }
                    for artifact in request["artifacts"]
                ],
            }
            request_path = Path(directory) / "forged-request.json"
            request_path.write_text(json.dumps(serializable), encoding="utf-8")
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "projectctl.py"),
                    "--project-root",
                    str(root),
                    "--request",
                    str(request_path),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("--migration", result.stderr)

    def test_projectctl_previews_then_applies_an_exact_migration_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            root.mkdir()
            source = _bundle()
            (root / "context").mkdir()
            control = root / ".development-graph"
            control.mkdir()
            (root / "development-graph.json").write_text(
                json.dumps(source["graph"], indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            (root / "specification-manifest.json").write_text(
                json.dumps(source["manifest"], indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            (root / "context" / "source.json").write_text(
                json.dumps(source["contexts"][0], indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            (control / "run.json").write_text(
                json.dumps(source["runtime"], indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            (control / "events.jsonl").write_text(
                "".join(
                    json.dumps(record, separators=(",", ":"), sort_keys=True)
                    + "\n"
                    for record in source["ledger"]
                ),
                encoding="utf-8",
            )
            descriptor = {
                "schema_version": 1,
                "bundle": {
                    "graph": "development-graph.json",
                    "manifest": "specification-manifest.json",
                    "contexts": ["context/source.json"],
                    "runtime": ".development-graph/run.json",
                    "ledger": ".development-graph/events.jsonl",
                },
                "outputs": {
                    "graph": "development-graph.json",
                    "manifest": "specification-manifest.json",
                    "context_successors": ["context/source-v1.json"],
                    "runtime": ".development-graph/run.json",
                    "ledger": ".development-graph/events.jsonl",
                },
                "target_versions": {
                    "graph": 1,
                    "manifest": 1,
                    "context": 1,
                    "runtime": 1,
                },
            }
            descriptor_path = Path(directory) / "migration.json"
            descriptor_path.write_text(
                json.dumps(descriptor),
                encoding="utf-8",
            )
            command = [
                sys.executable,
                str(SCRIPTS / "projectctl.py"),
                "--project-root",
                str(root),
                "--migration",
                str(descriptor_path),
            ]
            before = {
                path.relative_to(root).as_posix(): path.read_bytes()
                for path in root.rglob("*")
                if path.is_file()
            }

            preview = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(preview.returncode, 0, preview.stderr)
            lifecycle_plan = json.loads(preview.stdout)
            self.assertTrue(lifecycle_plan["valid"])
            self.assertTrue(lifecycle_plan["dry_run"])
            self.assertEqual(
                before,
                {
                    path.relative_to(root).as_posix(): path.read_bytes()
                    for path in root.rglob("*")
                    if path.is_file()
                },
            )
            applied = subprocess.run(
                command
                + [
                    "--apply",
                    "--expected-base-digest",
                    lifecycle_plan["base_digest"],
                    "--expected-plan-digest",
                    lifecycle_plan["plan_digest"],
                    "--actor",
                    "migration-test",
                    "--authority",
                    "test:migration",
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(applied.returncode, 0, applied.stderr)
            receipt = json.loads(applied.stdout)
            self.assertEqual(receipt["state"], "committed")
            self.assertIn(
                "runtime-ledger-append",
                receipt["migration"]["irreversible_boundaries"],
            )
            self.assertEqual(
                json.loads(
                    (root / "development-graph.json").read_text(encoding="utf-8")
                )["schema_version"],
                1,
            )
            self.assertTrue((root / "context" / "source-v1.json").is_file())
            self.assertEqual(
                len(
                    (control / "events.jsonl")
                    .read_text(encoding="utf-8")
                    .splitlines()
                ),
                2,
            )


if __name__ == "__main__":
    unittest.main()
