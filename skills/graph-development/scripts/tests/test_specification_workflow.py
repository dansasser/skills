from __future__ import annotations

import hashlib
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

from specification_workflow import (  # noqa: E402
    WorkflowReport,
    WorkspacePaths,
    plan_workspace_synchronization,
    snapshot_workspace,
    verify_workspace,
)
from project_lifecycle import apply_project, preview_project  # noqa: E402
from render_graph import render_markdown  # noqa: E402
from validate_graph import validate_graph  # noqa: E402
from validate_specifications import validate_specifications  # noqa: E402
try:
    from fixtures import apply_new_graph  # noqa: E402
except ModuleNotFoundError:
    from scripts.tests.fixtures import apply_new_graph  # noqa: E402


def synchronize_workspace(
    paths: WorkspacePaths,
    *,
    profile: str,
) -> WorkflowReport:
    """Exercise specification candidates through the reviewed lifecycle API."""

    report, writes = plan_workspace_synchronization(  # type: ignore[arg-type]
        paths,
        profile=profile,
    )
    if not report.valid or not writes:
        return report
    artifacts = [
        {
            "path": path.resolve().relative_to(paths.project_root.resolve()).as_posix(),
            "content": payload,
            "mode": mode,
            "expected_sha256": (
                hashlib.sha256(path.read_bytes()).hexdigest()
                if path.is_file()
                else None
            ),
        }
        for path, payload, mode in writes
    ]
    request = {
        "schema_version": 1,
        "operation": "update",
        "project_root": paths.project_root,
        "artifacts": artifacts,
        "metadata": {"entrypoint": "test-specification-sync", "profile": profile},
    }
    plan = preview_project(request)
    if not plan.valid:
        return WorkflowReport(
            operation="sync",
            valid=False,
            changed_paths=(),
            errors=tuple(plan.errors) + tuple(plan.blocked) + tuple(plan.conflicts),
            warnings=(),
            summary=report.summary,
        )
    receipt = apply_project(
        request,
        expected_base_digest=plan.base_digest,
        expected_plan_digest=plan.plan_digest,
        actor="test-specification-workflow",
        authority="test:specification-lifecycle",
    )
    if not receipt.applied:
        return WorkflowReport(
            operation="sync",
            valid=False,
            changed_paths=(),
            errors=(receipt.error or "lifecycle apply failed",),
            warnings=(),
            summary=report.summary,
        )
    return report


class SpecificationWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        result = apply_new_graph(
            [
                sys.executable,
                str(SCRIPTS / "new_graph.py"),
                "--project-root",
                str(self.root),
                "--output-dir",
                str(self.root),
                "--graph-id",
                "workflow-fixture",
                "--title",
                "Workflow Fixture",
                "--template",
                "specification-to-delivery",
                "--context-version",
                "test:workflow-fixture",
            ],
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.paths = WorkspacePaths(
            project_root=self.root,
            graph=self.root / "development-graph.json",
            manifest=self.root / "specification-manifest.json",
            decisions=None,
            markdown=self.root / "development-graph.md",
        )

    def _hashes(self) -> dict[str, str]:
        return {
            path.relative_to(self.root).as_posix(): hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
            for path in sorted(self.root.rglob("*"))
            if path.is_file()
        }

    def _write_required_specifications(self) -> dict[str, str]:
        manifest = json.loads(self.paths.manifest.read_text(encoding="utf-8"))
        paths = [
            self.root / document["path"]
            for document in manifest["documents"]
        ]
        paths.append(self.root / manifest["implementation_plan"]["path"])
        for path in paths:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                f"# {path.stem}\n\nEvidence-backed fixture content.\n",
                encoding="utf-8",
            )
        return {
            path.relative_to(self.root).as_posix(): hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
            for path in paths
        }

    def _decision_paths(self, decisions: list[dict[str, object]]) -> WorkspacePaths:
        decisions_path = self.root / "decisions.json"
        decisions_path.write_text(
            json.dumps(
                {"schema_version": 1, "decisions": decisions},
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        return WorkspacePaths(
            project_root=self.root,
            graph=self.paths.graph,
            manifest=self.paths.manifest,
            decisions=decisions_path,
            markdown=self.paths.markdown,
        )

    @staticmethod
    def _decisions() -> list[dict[str, object]]:
        return [
            {
                "id": "d1-loader",
                "title": "D1 Loader",
                "question": "Which project loader is authoritative?",
                "status": "open",
                "source_path": "doc/decisions/d1-loader.md",
                "affected_nodes": ["implement-loader"],
            },
            {
                "id": "d2-layout",
                "title": "D2 Layout",
                "question": "Which layout behavior is required?",
                "status": "deferred",
                "source_path": "doc/decisions/d2-layout.md",
                "affected_nodes": ["test-layout"],
            },
        ]

    def test_snapshot_is_bounded_valid_and_read_only(self) -> None:
        before = self._hashes()

        report = snapshot_workspace(self.paths)

        self.assertTrue(report.valid, report.errors)
        self.assertEqual(report.operation, "snapshot")
        self.assertEqual(report.summary["graph"]["nodes"], 13)
        self.assertEqual(report.summary["manifest"]["documents"], 6)
        self.assertNotIn("node_records", report.summary["graph"])
        self.assertNotIn("nodes_data", report.summary["graph"])
        self.assertEqual(report.changed_paths, ())
        self.assertEqual(report.summary["decisions"]["path"], None)
        self.assertEqual(report.summary["decisions"]["resolved"], 0)
        self.assertEqual(report.summary["decisions"]["unresolved"], 0)
        self.assertEqual(before, self._hashes())

    def test_snapshot_rejects_path_outside_project_without_writing(self) -> None:
        outside = self.root.parent / "outside-development-graph.json"
        outside.write_text("{}\n", encoding="utf-8")
        self.addCleanup(outside.unlink)
        paths = WorkspacePaths(
            project_root=self.root,
            graph=outside,
            manifest=self.paths.manifest,
            decisions=None,
            markdown=self.paths.markdown,
        )
        before = self._hashes()

        report = snapshot_workspace(paths)

        self.assertFalse(report.valid)
        self.assertTrue(
            any("escapes project root" in error for error in report.errors),
            report.errors,
        )
        self.assertEqual(before, self._hashes())

    def test_snapshot_reports_missing_and_malformed_files(self) -> None:
        self.paths.manifest.unlink()
        self.paths.graph.write_text("{not-json\n", encoding="utf-8")
        before = self._hashes()

        report = snapshot_workspace(self.paths)

        self.assertFalse(report.valid)
        self.assertIn("specification-manifest.json", report.summary["missing"])
        self.assertTrue(any("development-graph.json" in error for error in report.errors))
        self.assertEqual(before, self._hashes())

    def test_snapshot_rejects_duplicate_decisions(self) -> None:
        decisions_path = self.root / "decisions.json"
        decision = {
            "id": "d1-storage",
            "title": "Storage",
            "question": "Which storage?",
            "status": "open",
            "source_path": "doc/decisions/d1-storage.md",
            "affected_nodes": ["implement"],
        }
        decisions_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "decisions": [decision, {**decision, "source_path": "doc/decisions/d1-other.md"}],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        paths = WorkspacePaths(
            project_root=self.root,
            graph=self.paths.graph,
            manifest=self.paths.manifest,
            decisions=decisions_path,
            markdown=self.paths.markdown,
        )
        before = self._hashes()

        report = snapshot_workspace(paths)

        self.assertFalse(report.valid)
        self.assertTrue(any("duplicates" in error for error in report.errors), report.errors)
        self.assertEqual(before, self._hashes())

    def test_sync_updates_decisions_and_preserves_specification_prose(self) -> None:
        prose_before = self._write_required_specifications()
        paths = self._decision_paths(self._decisions())

        report = synchronize_workspace(paths, profile="standard")

        self.assertTrue(report.valid, report.errors)
        self.assertEqual(report.operation, "sync")
        self.assertEqual(
            set(report.changed_paths),
            {
                "development-graph.json",
                "development-graph.md",
                "specification-manifest.json",
                "doc/decisions/d1-loader.md",
                "doc/decisions/d2-layout.md",
            },
        )
        graph = json.loads(self.paths.graph.read_text(encoding="utf-8"))
        manifest = json.loads(self.paths.manifest.read_text(encoding="utf-8"))
        markdown = self.paths.markdown.read_text(encoding="utf-8")
        self.assertTrue(validate_graph(graph, markdown)["valid"])
        self.assertTrue(
            validate_specifications(graph, manifest, self.root)["valid"],
            validate_specifications(graph, manifest, self.root),
        )
        self.assertEqual(
            prose_before,
            {
                path: hashlib.sha256((self.root / path).read_bytes()).hexdigest()
                for path in prose_before
            },
        )

    def test_sync_is_idempotent(self) -> None:
        self._write_required_specifications()
        paths = self._decision_paths(self._decisions())
        first = synchronize_workspace(paths, profile="starter")
        self.assertTrue(first.valid, first.errors)
        before = self._hashes()

        second = synchronize_workspace(paths, profile="starter")

        self.assertTrue(second.valid, second.errors)
        self.assertEqual(second.changed_paths, ())
        self.assertEqual(before, self._hashes())

    def test_sync_invalid_decision_leaves_every_file_unchanged(self) -> None:
        self._write_required_specifications()
        decisions = self._decisions()
        decisions[0]["affected_nodes"] = ["Invalid Node"]
        paths = self._decision_paths(decisions)
        before = self._hashes()

        report = synchronize_workspace(paths, profile="assurance")

        self.assertFalse(report.valid)
        self.assertTrue(any("lowercase slug" in error for error in report.errors))
        self.assertEqual(before, self._hashes())

    def test_sync_rolls_back_after_late_write_failure(self) -> None:
        self._write_required_specifications()
        paths = self._decision_paths(self._decisions())
        before = {
            path: digest
            for path, digest in self._hashes().items()
            if not path.startswith(".development-graph/")
        }
        from graph_core import atomic_write_bytes as real_atomic_write_bytes

        calls = 0

        def fail_once_on_final_write(
            path: Path,
            payload: bytes,
            *,
            mode: int = 0o600,
        ) -> None:
            nonlocal calls
            calls += 1
            if calls == 5:
                raise OSError("simulated final write failure")
            real_atomic_write_bytes(path, payload, mode=mode)

        def fail_lifecycle_write(path: Path, payload: bytes, mode: int) -> None:
            fail_once_on_final_write(path, payload, mode=mode)

        with mock.patch(
            "project_lifecycle._replace_artifact",
            side_effect=fail_lifecycle_write,
        ):
            report = synchronize_workspace(paths, profile="standard")

        self.assertFalse(report.valid)
        self.assertTrue(any("simulated final write failure" in error for error in report.errors))
        self.assertEqual(
            before,
            {
                path: digest
                for path, digest in self._hashes().items()
                if not path.startswith(".development-graph/")
            },
        )
        receipts = list(
            (self.root / ".development-graph" / "transactions").glob(
                "*/receipt.json"
            )
        )
        self.assertTrue(receipts)
        self.assertTrue(
            any(
                json.loads(path.read_text(encoding="utf-8"))["state"]
                == "rolled-back"
                for path in receipts
            )
        )

    def test_sync_accepts_profiles_and_rejects_unknown_before_loading(self) -> None:
        self._write_required_specifications()
        for profile in ("starter", "standard", "assurance"):
            with self.subTest(profile=profile):
                report = synchronize_workspace(self.paths, profile=profile)
                self.assertTrue(report.valid, report.errors)
                self.assertEqual(report.changed_paths, ())

        with mock.patch(
            "specification_workflow._load_workspace_candidate"
        ) as load_candidate:
            report = synchronize_workspace(self.paths, profile="unknown")  # type: ignore[arg-type]

        self.assertFalse(report.valid)
        self.assertTrue(any("unknown workflow profile" in error for error in report.errors))
        load_candidate.assert_not_called()

    def test_verify_accepts_graph_manifest_decisions_and_local_links(self) -> None:
        self._write_required_specifications()
        product = self.root / "doc/product-spec.md"
        product.write_text(
            "# Product\n\nSee [architecture](architecture-spec.md).\n",
            encoding="utf-8",
        )
        paths = self._decision_paths(self._decisions())
        sync = synchronize_workspace(paths, profile="standard")
        self.assertTrue(sync.valid, sync.errors)

        report = verify_workspace(paths)

        self.assertTrue(report.valid, report.errors)
        self.assertEqual(report.operation, "verify")
        self.assertEqual(report.changed_paths, ())
        self.assertEqual(report.summary["validation"], "PASS")
        self.assertEqual(report.summary["decisions"], 2)

    def test_verify_and_snapshot_accept_resolved_catalog_decisions(self) -> None:
        self._write_required_specifications()
        paths = self._decision_paths(self._decisions())
        sync = synchronize_workspace(paths, profile="standard")
        self.assertTrue(sync.valid, sync.errors)

        catalog = json.loads(paths.decisions.read_text(encoding="utf-8"))
        catalog["decisions"][0]["status"] = "resolved"
        paths.decisions.write_text(
            json.dumps(catalog, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        manifest = json.loads(self.paths.manifest.read_text(encoding="utf-8"))
        manifest["decisions"][0]["status"] = "resolved"
        self.paths.manifest.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        graph = json.loads(self.paths.graph.read_text(encoding="utf-8"))
        next(
            node
            for node in graph["nodes"]
            if node["id"] == "resolve-d1-loader"
        )["state"] = "verified"
        self.paths.graph.write_text(
            json.dumps(graph, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        self.paths.markdown.write_text(
            render_markdown(graph, self.paths.graph.name),
            encoding="utf-8",
        )

        verification = verify_workspace(paths)
        snapshot = snapshot_workspace(paths)

        self.assertTrue(verification.valid, verification.errors)
        self.assertTrue(snapshot.valid, snapshot.errors)
        self.assertEqual(snapshot.summary["decisions"]["resolved"], 1)
        self.assertEqual(snapshot.summary["decisions"]["unresolved"], 1)

    def test_sync_rejects_uncataloged_resolved_decision(self) -> None:
        self._write_required_specifications()
        decisions = self._decisions()
        decisions[0]["status"] = "resolved"
        paths = self._decision_paths(decisions)

        report = synchronize_workspace(paths, profile="standard")

        self.assertFalse(report.valid)
        self.assertTrue(
            any("deferred, open" in error for error in report.errors),
            report.errors,
        )

    def test_verify_reports_malformed_graph_with_artifact_owner(self) -> None:
        self._write_required_specifications()
        self.paths.graph.write_text("{broken\n", encoding="utf-8")

        report = verify_workspace(self.paths)

        self.assertFalse(report.valid)
        self.assertTrue(
            any(error.startswith("graph:") for error in report.errors),
            report.errors,
        )

    def test_verify_rejects_graph_markdown_checksum_mismatch(self) -> None:
        self._write_required_specifications()
        self.paths.markdown.write_text(
            re.sub(
                r"development-graph-sha256: [0-9a-f]{64}",
                "development-graph-sha256: " + "0" * 64,
                self.paths.markdown.read_text(encoding="utf-8"),
                count=1,
            ),
            encoding="utf-8",
        )

        report = verify_workspace(self.paths)

        self.assertFalse(report.valid)
        self.assertTrue(
            any(
                error.startswith("graph:") and "checksum" in error
                for error in report.errors
            ),
            report.errors,
        )

    def test_verify_rejects_manifest_graph_version_mismatch(self) -> None:
        self._write_required_specifications()
        manifest = json.loads(self.paths.manifest.read_text(encoding="utf-8"))
        manifest["graph_version"] += 1
        self.paths.manifest.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        report = verify_workspace(self.paths)

        self.assertFalse(report.valid)
        self.assertTrue(
            any(
                error.startswith("specifications:")
                and "graph_version" in error
                for error in report.errors
            ),
            report.errors,
        )

    def test_verify_rejects_decision_catalog_manifest_mismatch(self) -> None:
        self._write_required_specifications()
        paths = self._decision_paths(self._decisions())
        sync = synchronize_workspace(paths, profile="standard")
        self.assertTrue(sync.valid, sync.errors)
        catalog = json.loads(paths.decisions.read_text(encoding="utf-8"))
        catalog["decisions"][0]["affected_nodes"] = ["different-consumer"]
        paths.decisions.write_text(
            json.dumps(catalog, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        report = verify_workspace(paths)

        self.assertFalse(report.valid)
        self.assertTrue(
            any(
                error.startswith("decisions:")
                and "affected_nodes" in error
                for error in report.errors
            ),
            report.errors,
        )

    def test_verify_rejects_missing_local_markdown_target(self) -> None:
        self._write_required_specifications()
        (self.root / "doc/product-spec.md").write_text(
            "# Product\n\nSee [missing contract](missing-contract.md).\n",
            encoding="utf-8",
        )

        report = verify_workspace(self.paths)

        self.assertFalse(report.valid)
        self.assertTrue(
            any(
                error.startswith("links:")
                and "missing-contract.md" in error
                for error in report.errors
            ),
            report.errors,
        )

    def test_verify_rejects_project_root_escape(self) -> None:
        self._write_required_specifications()
        outside = self.root.parent / "outside-verify-graph.json"
        outside.write_text("{}\n", encoding="utf-8")
        self.addCleanup(outside.unlink)
        paths = WorkspacePaths(
            project_root=self.root,
            graph=outside,
            manifest=self.paths.manifest,
            decisions=None,
            markdown=self.paths.markdown,
        )

        report = verify_workspace(paths)

        self.assertFalse(report.valid)
        self.assertTrue(any("escapes project root" in error for error in report.errors))


if __name__ == "__main__":
    unittest.main()
