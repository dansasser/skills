"""Bounded, deterministic operations over specification workspace artifacts."""

from __future__ import annotations

import json
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from add_specification_decisions import (
    CATALOG_STATUSES,
    SUPPORTED_STATUSES,
    _build_specification_decision_candidates,
    _decision_record,
    _load_decision_specs,
)
from graph_core import (
    GraphControlError,
    load_json,
    resolve_within,
)
from render_graph import render_markdown
from validate_graph import canonical_checksum, validate_graph
from validate_specifications import validate_specifications


WorkflowProfile = Literal["starter", "standard", "assurance"]
MARKDOWN_LINK = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")


@dataclass(frozen=True)
class WorkspacePaths:
    project_root: Path
    graph: Path
    manifest: Path
    decisions: Path | None
    markdown: Path


@dataclass(frozen=True)
class WorkflowReport:
    operation: Literal["snapshot", "sync", "verify"]
    valid: bool
    changed_paths: tuple[str, ...]
    errors: tuple[str, ...]
    warnings: tuple[str, ...]
    summary: dict[str, object]


@dataclass(frozen=True)
class _WorkspaceCandidate:
    root: Path
    graph_path: Path
    manifest_path: Path
    decisions_path: Path | None
    markdown_path: Path
    graph: dict[str, Any]
    manifest: dict[str, Any]
    decisions: tuple[dict[str, Any], ...]
    markdown: str


def report_to_json(report: WorkflowReport) -> str:
    payload = {
        "operation": report.operation,
        "valid": report.valid,
        "changed_paths": list(report.changed_paths),
        "errors": list(report.errors),
        "warnings": list(report.warnings),
        "summary": report.summary,
    }
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def _contained_artifact(root: Path, path: Path, label: str) -> Path:
    try:
        return resolve_within(root, str(path))
    except ValueError as exc:
        raise GraphControlError(f"{label} {exc}") from exc


def _relative(root: Path, path: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def _document_count(manifest: dict[str, object]) -> int:
    documents = manifest.get("documents")
    count = len(documents) if isinstance(documents, list) else 0
    implementation_plan = manifest.get("implementation_plan")
    if isinstance(implementation_plan, dict):
        count += 1
    return count


def _decision_counts(decisions: list[dict[str, object]]) -> tuple[int, int]:
    resolved = sum(decision.get("status") == "resolved" for decision in decisions)
    return resolved, len(decisions) - resolved


def _load_workspace_candidate(
    paths: WorkspacePaths,
    *,
    allow_resolved_decisions: bool = False,
) -> _WorkspaceCandidate:
    root = paths.project_root.resolve()
    if not root.is_dir():
        raise GraphControlError(
            f"project root does not exist or is not a directory: {root}"
        )
    graph_path = _contained_artifact(root, paths.graph, "graph")
    manifest_path = _contained_artifact(root, paths.manifest, "manifest")
    markdown_path = _contained_artifact(root, paths.markdown, "Markdown")
    decisions_path = (
        _contained_artifact(root, paths.decisions, "decisions")
        if paths.decisions is not None
        else None
    )
    for label, path in (
        ("graph", graph_path),
        ("manifest", manifest_path),
        ("Markdown", markdown_path),
    ):
        if not path.is_file():
            raise GraphControlError(
                f"{label} artifact does not exist: {_relative(root, path)}"
            )
    if decisions_path is not None and not decisions_path.is_file():
        raise GraphControlError(
            f"decisions artifact does not exist: {_relative(root, decisions_path)}"
        )
    try:
        markdown = markdown_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise GraphControlError(
            f"Markdown: cannot load {_relative(root, markdown_path)}: {exc}"
        ) from exc
    try:
        graph = load_json(graph_path)
    except GraphControlError as exc:
        raise GraphControlError(
            f"graph: {_relative(root, graph_path)}: {exc}"
        ) from exc
    try:
        manifest = load_json(manifest_path)
    except GraphControlError as exc:
        raise GraphControlError(
            f"manifest: {_relative(root, manifest_path)}: {exc}"
        ) from exc
    try:
        decisions = (
            tuple(
                _load_decision_specs(
                    decisions_path,
                    allowed_statuses=(
                        CATALOG_STATUSES
                        if allow_resolved_decisions
                        else SUPPORTED_STATUSES
                    ),
                )
            )
            if decisions_path is not None
            else ()
        )
    except GraphControlError as exc:
        raise GraphControlError(
            f"decisions: {_relative(root, decisions_path)}: {exc}"
        ) from exc
    return _WorkspaceCandidate(
        root=root,
        graph_path=graph_path,
        manifest_path=manifest_path,
        decisions_path=decisions_path,
        markdown_path=markdown_path,
        graph=graph,
        manifest=manifest,
        decisions=decisions,
        markdown=markdown,
    )


def _pending_decisions(
    manifest: dict[str, Any],
    decisions: tuple[dict[str, Any], ...],
) -> list[dict[str, Any]]:
    raw_existing = manifest.get("decisions")
    if not isinstance(raw_existing, list):
        raise GraphControlError("manifest decisions must be an array")
    existing = {
        decision.get("id"): decision
        for decision in raw_existing
        if isinstance(decision, dict) and isinstance(decision.get("id"), str)
    }
    pending: list[dict[str, Any]] = []
    for decision in decisions:
        current = existing.get(decision["id"])
        if current is None:
            pending.append(decision)
            continue
        expected = {
            "id": decision["id"],
            "status": decision["status"],
            "source_path": decision["source_path"],
            "decision_node": f"resolve-{decision['id']}",
            "artifact": f"decision:{decision['id']}",
            "affected_nodes": decision["affected_nodes"],
        }
        if current != expected:
            raise GraphControlError(
                f"existing decision differs from catalog: {decision['id']}"
            )
    return pending


def _candidate_file_payloads(
    root: Path,
    manifest: dict[str, Any],
    overrides: dict[str, bytes],
) -> dict[str, bytes]:
    raw_paths: list[str] = []
    for document in manifest.get("documents", []):
        if isinstance(document, dict) and isinstance(document.get("path"), str):
            raw_paths.append(document["path"])
    plan = manifest.get("implementation_plan")
    if isinstance(plan, dict) and isinstance(plan.get("path"), str):
        raw_paths.append(plan["path"])
    for decision in manifest.get("decisions", []):
        if isinstance(decision, dict) and isinstance(
            decision.get("source_path"), str
        ):
            raw_paths.append(decision["source_path"])

    payloads: dict[str, bytes] = {}
    for raw_path in raw_paths:
        if raw_path in payloads:
            continue
        if raw_path in overrides:
            payloads[raw_path] = overrides[raw_path]
            continue
        source = resolve_within(root, raw_path)
        if not source.is_file():
            raise GraphControlError(
                f"candidate specification artifact does not exist: {raw_path}"
            )
        payloads[raw_path] = source.read_bytes()
    return payloads


def _validate_candidate(
    root: Path,
    graph: dict[str, Any],
    manifest: dict[str, Any],
    markdown: str,
    *,
    overrides: dict[str, bytes] | None = None,
) -> tuple[str, ...]:
    errors: list[str] = []
    graph_report = validate_graph(graph, markdown)
    errors.extend(f"graph: {error}" for error in graph_report["errors"])
    try:
        payloads = _candidate_file_payloads(root, manifest, overrides or {})
    except (GraphControlError, OSError, ValueError) as exc:
        errors.append(f"specifications: {exc}")
        return tuple(errors)

    with tempfile.TemporaryDirectory() as directory:
        validation_root = Path(directory)
        specification_root = manifest.get("specification_root")
        if isinstance(specification_root, str):
            resolve_within(validation_root, specification_root).mkdir(
                parents=True,
                exist_ok=True,
            )
        for raw_path, payload in payloads.items():
            target = resolve_within(validation_root, raw_path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
        specification_report = validate_specifications(
            graph,
            manifest,
            project_root=validation_root,
        )
    errors.extend(
        f"specifications: {error}"
        for error in specification_report["errors"]
    )
    return tuple(errors)


def _json_payload(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True).encode(
            "utf-8"
        )
        + b"\n"
    )


def _sync_summary(
    graph: dict[str, Any],
    manifest: dict[str, Any],
    profile: WorkflowProfile,
) -> dict[str, object]:
    return {
        "profile": profile,
        "graph": {
            "graph_id": graph.get("graph_id"),
            "version": graph.get("graph_version"),
            "canonical_sha256": canonical_checksum(graph),
            "nodes": len(graph.get("nodes", [])),
            "edges": len(graph.get("edges", [])),
        },
        "manifest": {
            "documents": _document_count(manifest),
            "decisions": len(manifest.get("decisions", [])),
        },
        "validation": "PASS",
    }


def apply_specification_decisions(
    graph: dict[str, Any],
    manifest: dict[str, Any],
    decisions: list[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build graph and manifest candidates for one validated decision batch."""

    return _build_specification_decision_candidates(graph, manifest, decisions)


def build_specification_scaffold(
    project_root: Path,
    *,
    graph_id: str,
    title: str,
    context_version: str,
    include_implementation_plan: bool = True,
) -> tuple[dict[str, Any], dict[str, Any], str]:
    """Build and validate the canonical specification scaffold in memory."""

    from new_graph import build_graph, build_specification_manifest

    graph = build_graph(
        project_root,
        graph_id,
        title,
        "specification-to-delivery",
        context_version,
        include_implementation_plan=include_implementation_plan,
    )
    manifest = build_specification_manifest(
        graph, include_implementation_plan=include_implementation_plan
    )
    markdown = render_markdown(graph)
    report = validate_graph(graph, markdown)
    if not report["valid"]:
        raise GraphControlError(
            "specification scaffold is invalid: " + "; ".join(report["errors"])
        )
    return graph, manifest, markdown


def _build_workspace_synchronization(
    paths: WorkspacePaths,
    *,
    profile: WorkflowProfile,
    _candidate_writes: list[tuple[Path, bytes, int]] | None = None,
) -> WorkflowReport:
    if profile not in {"starter", "standard", "assurance"}:
        return WorkflowReport(
            operation="sync",
            valid=False,
            changed_paths=(),
            errors=(f"unknown workflow profile: {profile}",),
            warnings=(),
            summary={"profile": profile},
        )
    try:
        workspace = _load_workspace_candidate(paths)
        pending = _pending_decisions(workspace.manifest, workspace.decisions)
        if not pending:
            errors = _validate_candidate(
                workspace.root,
                workspace.graph,
                workspace.manifest,
                workspace.markdown,
            )
            return WorkflowReport(
                operation="sync",
                valid=not errors,
                changed_paths=(),
                errors=errors,
                warnings=(),
                summary=_sync_summary(
                    workspace.graph,
                    workspace.manifest,
                    profile,
                ),
            )

        record_payloads: dict[str, bytes] = {}
        writes: list[tuple[Path, bytes, int]] = []
        for decision in pending:
            raw_path = decision["source_path"]
            record_path = resolve_within(workspace.root, raw_path)
            if record_path.exists():
                if not record_path.is_file() or not record_path.read_text(
                    encoding="utf-8"
                ).strip():
                    raise GraphControlError(
                        f"decision record exists but is not a nonempty file: {raw_path}"
                    )
            else:
                payload = _decision_record(decision)
                record_payloads[raw_path] = payload
                writes.append((record_path, payload, 0o644))

        candidate_graph, candidate_manifest = apply_specification_decisions(
            workspace.graph,
            workspace.manifest,
            pending,
        )
        candidate_markdown = render_markdown(
            candidate_graph,
            workspace.graph_path.name,
        )
        errors = _validate_candidate(
            workspace.root,
            candidate_graph,
            candidate_manifest,
            candidate_markdown,
            overrides=record_payloads,
        )
        if errors:
            return WorkflowReport(
                operation="sync",
                valid=False,
                changed_paths=(),
                errors=errors,
                warnings=(),
                summary={"profile": profile},
            )

        writes.extend(
            [
                (workspace.graph_path, _json_payload(candidate_graph), 0o600),
                (
                    workspace.manifest_path,
                    _json_payload(candidate_manifest),
                    0o600,
                ),
                (
                    workspace.markdown_path,
                    candidate_markdown.encode("utf-8"),
                    0o644,
                ),
            ]
        )
        if _candidate_writes is not None:
            _candidate_writes.extend(writes)
        changed_paths = tuple(
            _relative(workspace.root, path)
            for path, _, _ in writes
        )
        return WorkflowReport(
            operation="sync",
            valid=True,
            changed_paths=changed_paths,
            errors=(),
            warnings=(),
            summary=_sync_summary(candidate_graph, candidate_manifest, profile),
        )
    except (GraphControlError, OSError, UnicodeError, ValueError) as exc:
        return WorkflowReport(
            operation="sync",
            valid=False,
            changed_paths=(),
            errors=(str(exc),),
            warnings=(),
            summary={"profile": profile},
        )


def synchronize_workspace(
    paths: WorkspacePaths,
    *,
    profile: WorkflowProfile,
) -> WorkflowReport:
    """Preview a validated specification synchronization without writing."""

    return _build_workspace_synchronization(paths, profile=profile)


def plan_workspace_synchronization(
    paths: WorkspacePaths,
    *,
    profile: WorkflowProfile,
) -> tuple[WorkflowReport, tuple[tuple[Path, bytes, int], ...]]:
    """Build and validate a sync candidate without writing project state."""

    writes: list[tuple[Path, bytes, int]] = []
    report = _build_workspace_synchronization(
        paths,
        profile=profile,
        _candidate_writes=writes,
    )
    return report, tuple(writes)


def _validate_decision_catalog(
    graph: dict[str, Any],
    manifest: dict[str, Any],
    decisions: tuple[dict[str, Any], ...],
) -> tuple[str, ...]:
    if not decisions:
        return ()
    raw_manifest_decisions = manifest.get("decisions")
    if not isinstance(raw_manifest_decisions, list):
        return ("manifest decisions must be an array",)
    manifest_decisions = {
        decision.get("id"): decision
        for decision in raw_manifest_decisions
        if isinstance(decision, dict) and isinstance(decision.get("id"), str)
    }
    catalog_ids = {decision["id"] for decision in decisions}
    errors: list[str] = []
    for decision in decisions:
        decision_id = decision["id"]
        current = manifest_decisions.get(decision_id)
        if current is None:
            errors.append(f"{decision_id} is absent from the manifest")
            continue
        for field in ("status", "source_path", "affected_nodes"):
            if current.get(field) != decision[field]:
                errors.append(
                    f"{decision_id} {field} differs between catalog and manifest"
                )
        expected_node = f"resolve-{decision_id}"
        if current.get("decision_node") != expected_node:
            errors.append(
                f"{decision_id} decision_node is not {expected_node}"
            )
    extra = sorted(set(manifest_decisions) - catalog_ids)
    if extra:
        errors.append(
            "manifest decisions are absent from the catalog: "
            + ", ".join(extra)
        )
    graph_nodes = {
        node.get("id")
        for node in graph.get("nodes", [])
        if isinstance(node, dict)
    }
    for decision in decisions:
        node_id = f"resolve-{decision['id']}"
        if node_id not in graph_nodes:
            errors.append(f"{decision['id']} graph node is missing: {node_id}")
    return tuple(errors)


def _manifest_markdown_paths(manifest: dict[str, Any]) -> tuple[str, ...]:
    raw_paths: list[str] = []
    for document in manifest.get("documents", []):
        if (
            isinstance(document, dict)
            and isinstance(document.get("path"), str)
            and document["path"].lower().endswith((".md", ".markdown"))
        ):
            raw_paths.append(document["path"])
    plan = manifest.get("implementation_plan")
    if (
        isinstance(plan, dict)
        and isinstance(plan.get("path"), str)
        and plan["path"].lower().endswith((".md", ".markdown"))
    ):
        raw_paths.append(plan["path"])
    for decision in manifest.get("decisions", []):
        if (
            isinstance(decision, dict)
            and isinstance(decision.get("source_path"), str)
            and decision["source_path"].lower().endswith((".md", ".markdown"))
        ):
            raw_paths.append(decision["source_path"])
    return tuple(dict.fromkeys(raw_paths))


def _link_target(raw_target: str) -> str | None:
    target = raw_target.strip()
    if target.startswith("<") and ">" in target:
        target = target[1 : target.index(">")]
    else:
        target = target.split(maxsplit=1)[0]
    if (
        not target
        or target.startswith("#")
        or re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", target)
    ):
        return None
    return target.split("#", 1)[0].split("?", 1)[0]


def _validate_local_markdown_links(
    project_root: Path,
    manifest: dict[str, Any],
) -> tuple[tuple[str, ...], int]:
    root = project_root.resolve()
    errors: list[str] = []
    checked = 0
    for raw_path in _manifest_markdown_paths(manifest):
        try:
            document = resolve_within(root, raw_path)
        except ValueError as exc:
            errors.append(f"{raw_path}: {exc}")
            continue
        if not document.is_file():
            continue
        try:
            text = document.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            errors.append(f"{raw_path}: cannot read Markdown: {exc}")
            continue
        for match in MARKDOWN_LINK.finditer(text):
            target_text = _link_target(match.group(1))
            if target_text is None or not target_text:
                continue
            checked += 1
            target = (document.parent / target_text).resolve()
            try:
                target.relative_to(root)
            except ValueError:
                errors.append(
                    f"{raw_path}: local link escapes project root: {target_text}"
                )
                continue
            if not target.exists():
                errors.append(
                    f"{raw_path}: local link target does not exist: {target_text}"
                )
    return tuple(errors), checked


def verify_workspace(paths: WorkspacePaths) -> WorkflowReport:
    try:
        workspace = _load_workspace_candidate(
            paths,
            allow_resolved_decisions=True,
        )
    except (GraphControlError, OSError, UnicodeError, ValueError) as exc:
        return WorkflowReport(
            operation="verify",
            valid=False,
            changed_paths=(),
            errors=(str(exc),),
            warnings=(),
            summary={"validation": "FAIL"},
        )

    graph_report = validate_graph(workspace.graph, workspace.markdown)
    specification_report = validate_specifications(
        workspace.graph,
        workspace.manifest,
        project_root=workspace.root,
    )
    decision_errors = _validate_decision_catalog(
        workspace.graph,
        workspace.manifest,
        workspace.decisions,
    )
    link_errors, checked_links = _validate_local_markdown_links(
        workspace.root,
        workspace.manifest,
    )
    errors = tuple(
        [f"graph: {error}" for error in graph_report["errors"]]
        + [
            f"specifications: {error}"
            for error in specification_report["errors"]
        ]
        + [f"decisions: {error}" for error in decision_errors]
        + [f"links: {error}" for error in link_errors]
    )
    warnings = tuple(
        [f"graph: {warning}" for warning in graph_report["warnings"]]
        + [
            f"specifications: {warning}"
            for warning in specification_report["warnings"]
        ]
    )
    return WorkflowReport(
        operation="verify",
        valid=not errors,
        changed_paths=(),
        errors=errors,
        warnings=warnings,
        summary={
            "validation": "PASS" if not errors else "FAIL",
            "graph": {
                "graph_id": workspace.graph.get("graph_id"),
                "version": workspace.graph.get("graph_version"),
                "canonical_sha256": graph_report["canonical_sha256"],
                "nodes": graph_report["stats"]["nodes"],
                "edges": graph_report["stats"]["edges"],
            },
            "documents": _document_count(workspace.manifest),
            "decisions": len(workspace.manifest.get("decisions", [])),
            "local_links_checked": checked_links,
            "warning_count": len(warnings),
        },
    )


def snapshot_workspace(paths: WorkspacePaths) -> WorkflowReport:
    root = paths.project_root.resolve()
    errors: list[str] = []
    missing: list[str] = []
    summary: dict[str, object] = {
        "graph": {},
        "manifest": {},
        "decisions": {
            "path": None,
            "resolved": 0,
            "unresolved": 0,
        },
        "missing": missing,
    }
    if not root.is_dir():
        errors.append(f"project root does not exist or is not a directory: {root}")

    resolved: dict[str, Path] = {}
    artifacts = {
        "graph": paths.graph,
        "manifest": paths.manifest,
        "markdown": paths.markdown,
    }
    if paths.decisions is not None:
        artifacts["decisions"] = paths.decisions
    for label, path in artifacts.items():
        try:
            resolved[label] = _contained_artifact(root, path, label)
        except GraphControlError as exc:
            errors.append(str(exc))

    for label in ("graph", "manifest", "markdown"):
        path = resolved.get(label)
        if path is not None and not path.is_file():
            missing.append(_relative(root, path))
    decisions_path = resolved.get("decisions")
    if decisions_path is not None and not decisions_path.is_file():
        missing.append(_relative(root, decisions_path))
    if missing:
        errors.extend(f"missing required artifact: {path}" for path in missing)

    graph_path = resolved.get("graph")
    if graph_path is not None and graph_path.is_file():
        try:
            graph = load_json(graph_path)
        except GraphControlError as exc:
            errors.append(f"{_relative(root, graph_path)}: {exc}")
        else:
            nodes = graph.get("nodes")
            edges = graph.get("edges")
            summary["graph"] = {
                "path": _relative(root, graph_path),
                "graph_id": graph.get("graph_id"),
                "version": graph.get("graph_version"),
                "canonical_sha256": canonical_checksum(graph),
                "nodes": len(nodes) if isinstance(nodes, list) else 0,
                "edges": len(edges) if isinstance(edges, list) else 0,
            }

    manifest_path = resolved.get("manifest")
    if manifest_path is not None and manifest_path.is_file():
        try:
            manifest = load_json(manifest_path)
        except GraphControlError as exc:
            errors.append(f"{_relative(root, manifest_path)}: {exc}")
        else:
            decisions = manifest.get("decisions")
            summary["manifest"] = {
                "path": _relative(root, manifest_path),
                "documents": _document_count(manifest),
                "decisions": len(decisions) if isinstance(decisions, list) else 0,
            }

    if decisions_path is not None and decisions_path.is_file():
        try:
            decision_specs = _load_decision_specs(
                decisions_path,
                allowed_statuses=CATALOG_STATUSES,
            )
        except GraphControlError as exc:
            errors.append(f"{_relative(root, decisions_path)}: {exc}")
        else:
            resolved_count, unresolved_count = _decision_counts(decision_specs)
            summary["decisions"] = {
                "path": _relative(root, decisions_path),
                "resolved": resolved_count,
                "unresolved": unresolved_count,
            }

    return WorkflowReport(
        operation="snapshot",
        valid=not errors,
        changed_paths=(),
        errors=tuple(errors),
        warnings=(),
        summary=summary,
    )
