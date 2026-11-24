"""Preview, apply, and recover exact project artifact transactions."""

from __future__ import annotations

import copy
import hashlib
import json
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from graph_core import atomic_write_bytes, atomic_write_json, file_lock
from path_contract import PathContractError, resolve_project_path, validate_relative_path
from schema_migrations import validate_authenticated_migration_request


CONTROL_ROOT = ".development-graph"
TRANSACTIONS_DIRECTORY = "transactions"
MANAGED_FILENAME = "managed-artifacts.json"
LOCK_FILENAME = "lifecycle.lock"
NONTERMINAL_STATES = frozenset({"prepared", "committing", "recovery-required"})


class LifecycleError(RuntimeError):
    """Raised when a lifecycle request is invalid or unsafe."""


class StaleLifecycleError(LifecycleError):
    """Raised when the reviewed project base or plan is no longer current."""


@dataclass(frozen=True)
class LifecyclePlan:
    operation: str
    valid: bool
    dry_run: bool
    base_digest: str
    plan_digest: str
    candidate_digest: str
    changed: tuple[str, ...]
    preserved: tuple[str, ...]
    invalidated: tuple[str, ...]
    blocked: tuple[str, ...]
    conflicts: tuple[str, ...]
    errors: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "operation": self.operation,
            "valid": self.valid,
            "dry_run": self.dry_run,
            "base_digest": self.base_digest,
            "plan_digest": self.plan_digest,
            "candidate_digest": self.candidate_digest,
            "changed": list(self.changed),
            "preserved": list(self.preserved),
            "invalidated": list(self.invalidated),
            "blocked": list(self.blocked),
            "conflicts": list(self.conflicts),
            "errors": list(self.errors),
        }


@dataclass(frozen=True)
class LifecycleReceipt:
    transaction_id: str
    operation: str
    state: str
    applied: bool
    base_digest: str
    plan_digest: str
    candidate_digest: str
    changed: tuple[str, ...]
    actor: str
    authority: str
    error: str | None = None
    migration: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        value = {
            "schema_version": 1,
            "transaction_id": self.transaction_id,
            "operation": self.operation,
            "state": self.state,
            "applied": self.applied,
            "base_digest": self.base_digest,
            "plan_digest": self.plan_digest,
            "candidate_digest": self.candidate_digest,
            "changed": list(self.changed),
            "actor": self.actor,
            "authority": self.authority,
            "error": self.error,
        }
        if self.migration is not None:
            value["migration"] = copy.deepcopy(self.migration)
        return value


@dataclass(frozen=True)
class _Artifact:
    path: str
    target: Path
    content: bytes
    mode: int
    expected_sha256: str | None


@dataclass(frozen=True)
class _Request:
    operation: str
    project_root: Path
    artifacts: tuple[_Artifact, ...]
    metadata: dict[str, Any]


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _require_nonempty(value: str, *, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise LifecycleError(f"{label} must be non-empty text")
    return value


def _normalize_request(request: dict[str, Any]) -> _Request:
    if not isinstance(request, dict) or request.get("schema_version") != 1:
        raise LifecycleError("lifecycle request schema_version must equal 1")
    operation = request.get("operation")
    if operation not in {
        "bootstrap", "update", "migration", "graph-mutation", "run-promotion"
    }:
        raise LifecycleError(
            "lifecycle operation must be bootstrap, update, migration, "
            "graph-mutation, or run-promotion"
        )
    root_value = request.get("project_root")
    if not isinstance(root_value, (str, Path)):
        raise LifecycleError("lifecycle request requires project_root")
    project_root = Path(root_value).resolve()
    if not project_root.is_dir():
        raise LifecycleError(f"project root is not a directory: {project_root}")
    if operation == "migration":
        authentication_errors = validate_authenticated_migration_request(
            request,
            project_root,
            revalidate_sources=False,
        )
        if authentication_errors:
            raise LifecycleError("; ".join(authentication_errors))
    raw_artifacts = request.get("artifacts")
    if not isinstance(raw_artifacts, list):
        raise LifecycleError("lifecycle request artifacts must be an array")
    artifacts: list[_Artifact] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_artifacts):
        label = f"artifact {index}"
        if not isinstance(raw, dict):
            raise LifecycleError(f"{label} must be an object")
        try:
            relative = validate_relative_path(
                raw.get("path"),
                label=f"{label} path",
                allow_dot=False,
            )
            target = resolve_project_path(
                project_root,
                relative,
                label=f"{label} path",
            )
        except (PathContractError, TypeError) as exc:
            raise LifecycleError(str(exc)) from exc
        controlled_runtime_paths = {
            f"{CONTROL_ROOT}/run.json",
            f"{CONTROL_ROOT}/events.jsonl",
        }
        graph_snapshot_prefix = f"{CONTROL_ROOT}/snapshots/"
        projection_prefix = f"{CONTROL_ROOT}/projections/"
        controlled_path_allowed = (
            operation == "migration"
            and (
                relative in controlled_runtime_paths
                or relative.startswith(projection_prefix)
            )
        ) or (
            operation == "graph-mutation"
            and (
                relative in controlled_runtime_paths
                or relative.startswith(graph_snapshot_prefix)
            )
        ) or (
            operation == "run-promotion"
            and (
                relative in controlled_runtime_paths
                or relative.startswith(projection_prefix)
            )
        )
        if (
            relative.startswith(f"{CONTROL_ROOT}/") or relative == CONTROL_ROOT
        ) and not controlled_path_allowed:
            raise LifecycleError(f"{label} cannot manage lifecycle control state")
        if relative in seen:
            raise LifecycleError(f"duplicate lifecycle artifact path: {relative}")
        seen.add(relative)
        content = raw.get("content")
        if not isinstance(content, bytes):
            raise LifecycleError(f"{label} content must be bytes")
        mode = raw.get("mode", 0o644)
        if (
            isinstance(mode, bool)
            or not isinstance(mode, int)
            or mode < 0
            or mode > 0o777
        ):
            raise LifecycleError(f"{label} mode must be an integer from 0 to 0o777")
        expected = raw.get("expected_sha256")
        if expected is not None and (
            not isinstance(expected, str)
            or len(expected) != 64
            or any(character not in "0123456789abcdef" for character in expected)
        ):
            raise LifecycleError(f"{label} expected_sha256 must be lowercase SHA-256")
        artifacts.append(
            _Artifact(relative, target, content, mode, expected)
        )
    metadata = request.get("metadata", {})
    if not isinstance(metadata, dict):
        raise LifecycleError("lifecycle request metadata must be an object")
    try:
        copy.deepcopy(json.loads(json.dumps(metadata)))
    except (TypeError, ValueError) as exc:
        raise LifecycleError("lifecycle request metadata must be JSON-compatible") from exc
    if operation == "migration":
        receipt = metadata.get("migration_receipt")
        conversion = (
            isinstance(receipt, dict)
            and (
                "required_actor" in receipt
                or "required_authority" in receipt
                or "projection_path" in receipt
            )
        )
        if conversion:
            paths = tuple(artifact.path for artifact in artifacts)
            expected_projection = (
                f"{CONTROL_ROOT}/{receipt.get('projection_path')}"
            )
            expected = (
                expected_projection,
                f"{CONTROL_ROOT}/run.json",
                f"{CONTROL_ROOT}/events.jsonl",
            )
            if (
                not expected_projection.startswith(
                    f"{CONTROL_ROOT}/projections/"
                )
                or paths != expected
            ):
                raise LifecycleError(
                    "runtime conversion requires ordered projection, run.json, "
                    "and events.jsonl artifacts"
                )
    if operation == "graph-mutation":
        mutation_id = metadata.get("mutation_id")
        if (
            not isinstance(mutation_id, str)
            or not mutation_id.strip()
            or "\x00" in mutation_id
        ):
            raise LifecycleError(
                "graph-mutation metadata requires a non-empty mutation_id"
            )
    if operation == "run-promotion":
        promotion_id = metadata.get("promotion_id")
        if (
            not isinstance(promotion_id, str)
            or not promotion_id.strip()
            or "\x00" in promotion_id
        ):
            raise LifecycleError(
                "run-promotion metadata requires a non-empty promotion_id"
            )
        paths = {artifact.path for artifact in artifacts}
        runtime_paths = {
            f"{CONTROL_ROOT}/run.json",
            f"{CONTROL_ROOT}/events.jsonl",
        }
        projection_paths = {
            path for path in paths if path.startswith(f"{CONTROL_ROOT}/projections/")
        }
        if (
            not runtime_paths.issubset(paths)
            or len(projection_paths) != 1
            or paths != runtime_paths | projection_paths
        ):
            raise LifecycleError(
                "run-promotion requires exactly run.json, events.jsonl, "
                "and one projection snapshot"
            )
    return _Request(
        operation=operation,
        project_root=project_root,
        artifacts=(
            tuple(artifacts)
            if operation == "migration"
            else tuple(sorted(artifacts, key=lambda item: item.path))
        ),
        metadata=copy.deepcopy(metadata),
    )


def _file_record(path: str, target: Path) -> dict[str, Any]:
    if not target.exists():
        return {"exists": False, "mode": None, "path": path, "sha256": None}
    if target.is_symlink() or not target.is_file():
        return {
            "exists": True,
            "mode": None,
            "path": path,
            "sha256": "non-file",
        }
    return {
        "exists": True,
        "mode": stat.S_IMODE(target.stat().st_mode),
        "path": path,
        "sha256": _sha256(target.read_bytes()),
    }


def _artifact_state_digest(artifacts: tuple[_Artifact, ...]) -> str:
    return _digest([_file_record(item.path, item.target) for item in artifacts])


def _candidate_digest(artifacts: tuple[_Artifact, ...]) -> str:
    return _digest(
        [
            {
                "mode": item.mode,
                "path": item.path,
                "sha256": _sha256(item.content),
            }
            for item in artifacts
        ]
    )


def _control_paths(project_root: Path) -> tuple[Path, Path, Path]:
    root = project_root / CONTROL_ROOT
    return (
        root / MANAGED_FILENAME,
        root / TRANSACTIONS_DIRECTORY,
        root / LOCK_FILENAME,
    )


def _load_managed(project_root: Path) -> dict[str, str]:
    inventory_path, _, _ = _control_paths(project_root)
    if not inventory_path.exists():
        return {}
    try:
        value = json.loads(inventory_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise LifecycleError(f"managed artifact inventory is invalid: {exc}") from exc
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise LifecycleError("managed artifact inventory is invalid")
    records = value.get("artifacts")
    if not isinstance(records, list):
        raise LifecycleError("managed artifact inventory artifacts must be an array")
    managed: dict[str, str] = {}
    for record in records:
        if (
            not isinstance(record, dict)
            or not isinstance(record.get("path"), str)
            or not isinstance(record.get("sha256"), str)
        ):
            raise LifecycleError("managed artifact inventory contains an invalid record")
        managed[record["path"]] = record["sha256"]
    return managed


def _nonterminal_transactions(project_root: Path) -> tuple[str, ...]:
    _, transactions_root, _ = _control_paths(project_root)
    if not transactions_root.exists():
        return ()
    blocked: list[str] = []
    for directory in sorted(transactions_root.iterdir(), key=lambda item: item.name):
        if not directory.is_dir() or directory.is_symlink():
            continue
        manifest_path = directory / "manifest.json"
        if not manifest_path.is_file():
            blocked.append(directory.name)
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            blocked.append(directory.name)
            continue
        if not isinstance(manifest, dict) or manifest.get("state") in NONTERMINAL_STATES:
            blocked.append(directory.name)
    return tuple(blocked)


def preview_project(request: dict[str, Any]) -> LifecyclePlan:
    """Build a deterministic no-write lifecycle plan."""

    normalized = _normalize_request(request)
    base_digest = _artifact_state_digest(normalized.artifacts)
    candidate_digest = _candidate_digest(normalized.artifacts)
    changed: list[str] = []
    preserved: list[str] = []
    invalidated: list[str] = []
    blocked = list(_nonterminal_transactions(normalized.project_root))
    conflicts: list[str] = []
    errors: list[str] = []

    migration_plan = normalized.metadata.get("migration_plan")
    migration_ready = (
        normalized.operation == "migration"
        and isinstance(migration_plan, dict)
        and migration_plan.get("valid") is True
        and migration_plan.get("dry_run") is True
        and isinstance(migration_plan.get("source_digest"), str)
        and len(migration_plan["source_digest"]) == 64
        and isinstance(migration_plan.get("candidate_digest"), str)
        and len(migration_plan["candidate_digest"]) == 64
    )
    if normalized.operation in {"graph-mutation", "run-promotion"}:
        snapshot_prefix = (
            f"{CONTROL_ROOT}/snapshots/"
            if normalized.operation == "graph-mutation"
            else f"{CONTROL_ROOT}/projections/"
        )
        for artifact in normalized.artifacts:
            current = _file_record(artifact.path, artifact.target)
            candidate_sha = _sha256(artifact.content)
            exact_candidate = (
                current["sha256"] == candidate_sha
                and current["mode"] == artifact.mode
            )
            if artifact.path.startswith(snapshot_prefix):
                if current["exists"]:
                    conflicts.append(artifact.path)
                elif artifact.expected_sha256 is not None:
                    blocked.append(artifact.path)
                else:
                    changed.append(artifact.path)
                continue
            if not current["exists"]:
                blocked.append(artifact.path)
            elif (
                artifact.expected_sha256 is None
                or current["sha256"] != artifact.expected_sha256
            ):
                blocked.append(artifact.path)
            elif exact_candidate:
                preserved.append(artifact.path)
            else:
                changed.append(artifact.path)
                invalidated.append(artifact.path)
    elif normalized.operation == "migration" and not migration_ready:
        blocked.append("migration-not-implemented")
        errors.append(
            "Migration execution is unavailable until Task 6 installs the "
            "versioned schema migration registry and supplies a validated plan."
        )
    elif normalized.operation == "migration":
        for artifact in normalized.artifacts:
            current = _file_record(artifact.path, artifact.target)
            candidate_sha = _sha256(artifact.content)
            if (
                current["sha256"] == candidate_sha
                and current["mode"] == artifact.mode
            ):
                preserved.append(artifact.path)
            elif not current["exists"] and artifact.expected_sha256 is None:
                changed.append(artifact.path)
                invalidated.append(artifact.path)
            elif (
                current["exists"]
                and artifact.expected_sha256 is not None
                and current["sha256"] == artifact.expected_sha256
            ):
                changed.append(artifact.path)
                invalidated.append(artifact.path)
            elif current["exists"] and artifact.expected_sha256 is None:
                conflicts.append(artifact.path)
            else:
                blocked.append(artifact.path)
    else:
        managed = _load_managed(normalized.project_root)
        for artifact in normalized.artifacts:
            current = _file_record(artifact.path, artifact.target)
            candidate_sha = _sha256(artifact.content)
            exact_candidate = (
                current["sha256"] == candidate_sha
                and current["mode"] == artifact.mode
            )
            if normalized.operation == "bootstrap":
                if exact_candidate:
                    preserved.append(artifact.path)
                elif current["exists"]:
                    conflicts.append(artifact.path)
                else:
                    changed.append(artifact.path)
                continue
            # Update: a known managed file needs an exact reviewed hash.
            if artifact.path in managed:
                if (
                    artifact.expected_sha256 is None
                    or artifact.expected_sha256 != managed[artifact.path]
                    or current["sha256"] != artifact.expected_sha256
                ):
                    blocked.append(artifact.path)
                elif exact_candidate:
                    preserved.append(artifact.path)
                else:
                    changed.append(artifact.path)
                    invalidated.append(artifact.path)
            elif current["exists"]:
                conflicts.append(artifact.path)
            else:
                changed.append(artifact.path)

    changed_tuple = tuple(sorted(set(changed)))
    preserved_tuple = tuple(sorted(set(preserved)))
    invalidated_tuple = tuple(sorted(set(invalidated)))
    blocked_tuple = tuple(sorted(set(blocked)))
    conflicts_tuple = tuple(sorted(set(conflicts)))
    errors_tuple = tuple(errors)
    plan_material = {
        "schema_version": 1,
        "operation": normalized.operation,
        "base_digest": base_digest,
        "candidate_digest": candidate_digest,
        "changed": list(changed_tuple),
        "preserved": list(preserved_tuple),
        "invalidated": list(invalidated_tuple),
        "blocked": list(blocked_tuple),
        "conflicts": list(conflicts_tuple),
        "errors": list(errors_tuple),
        "artifacts": [
            {
                "expected_sha256": item.expected_sha256,
                "mode": item.mode,
                "path": item.path,
                "sha256": _sha256(item.content),
            }
            for item in normalized.artifacts
        ],
        "metadata": normalized.metadata,
    }
    return LifecyclePlan(
        operation=normalized.operation,
        valid=not blocked_tuple and not conflicts_tuple,
        dry_run=True,
        base_digest=base_digest,
        plan_digest=_digest(plan_material),
        candidate_digest=candidate_digest,
        changed=changed_tuple,
        preserved=preserved_tuple,
        invalidated=invalidated_tuple,
        blocked=blocked_tuple,
        conflicts=conflicts_tuple,
        errors=errors_tuple,
    )


def _replace_artifact(path: Path, payload: bytes, mode: int) -> None:
    atomic_write_bytes(path, payload, mode=mode)


def _project_artifact_target(project_root: Path, relative: str) -> Path:
    candidate = project_root.resolve()
    for part in Path(relative).parts:
        candidate /= part
        if candidate.is_symlink():
            raise LifecycleError(
                f"project artifact path escapes its root through a symlink: {relative}"
            )
    try:
        return resolve_project_path(
            project_root,
            relative,
            label="project artifact path",
        )
    except PathContractError as exc:
        raise LifecycleError(str(exc)) from exc


def _replace_project_artifact(
    project_root: Path,
    relative: str,
    payload: bytes,
    mode: int,
) -> None:
    _replace_artifact(
        _project_artifact_target(project_root, relative),
        payload,
        mode,
    )


def _remove_project_artifact(project_root: Path, relative: str) -> None:
    target = _project_artifact_target(project_root, relative)
    if target.exists():
        if not target.is_file():
            raise LifecycleError(f"project artifact is not a regular file: {relative}")
        target.unlink()


def _manifest_path(project_root: Path, transaction_id: str) -> Path:
    _, transactions_root, _ = _control_paths(project_root)
    return transactions_root / transaction_id / "manifest.json"


def _write_manifest(path: Path, value: dict[str, Any]) -> None:
    atomic_write_json(path, value)


def _write_receipt(
    project_root: Path,
    receipt: LifecycleReceipt,
) -> None:
    path = _manifest_path(project_root, receipt.transaction_id).parent / "receipt.json"
    atomic_write_json(path, receipt.to_dict())


def _managed_value(managed: dict[str, str]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "artifacts": [
            {"path": path, "sha256": digest}
            for path, digest in sorted(managed.items())
        ],
    }


def _json_payload(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
        + b"\n"
    )


def _snapshot_managed_inventory(
    project_root: Path,
    prior_root: Path,
    candidate: dict[str, Any],
) -> dict[str, Any]:
    inventory_path, _, _ = _control_paths(project_root)
    if inventory_path.exists() and (
        inventory_path.is_symlink() or not inventory_path.is_file()
    ):
        raise LifecycleError("managed artifact inventory is not a regular file")
    prior_exists = inventory_path.is_file()
    prior_payload = inventory_path.read_bytes() if prior_exists else b""
    prior_mode = (
        stat.S_IMODE(inventory_path.stat().st_mode) if prior_exists else None
    )
    if prior_exists:
        atomic_write_bytes(
            prior_root / ".managed-artifacts.json",
            prior_payload,
            mode=prior_mode,
        )
    return {
        "prior_exists": prior_exists,
        "prior_sha256": _sha256(prior_payload) if prior_exists else None,
        "prior_mode": prior_mode,
        "candidate_sha256": _sha256(_json_payload(candidate)),
    }


def _restore_managed_inventory(
    project_root: Path,
    transaction_root: Path,
    record: dict[str, Any] | None,
) -> None:
    if record is None:
        return
    inventory_path, _, _ = _control_paths(project_root)
    if inventory_path.exists() and (
        inventory_path.is_symlink() or not inventory_path.is_file()
    ):
        raise LifecycleError("managed artifact inventory is not a regular file")
    current_sha = (
        _sha256(inventory_path.read_bytes()) if inventory_path.is_file() else None
    )
    allowed = {record.get("prior_sha256"), record.get("candidate_sha256")}
    if current_sha not in allowed:
        raise LifecycleError("managed artifact inventory diverged during transaction")
    if record.get("prior_exists") is True:
        prior = transaction_root / "prior" / ".managed-artifacts.json"
        if not prior.is_file() or prior.is_symlink():
            raise LifecycleError("missing prior managed artifact inventory")
        payload = prior.read_bytes()
        if _sha256(payload) != record.get("prior_sha256"):
            raise LifecycleError("prior managed artifact inventory checksum mismatch")
        mode = record.get("prior_mode")
        if isinstance(mode, bool) or not isinstance(mode, int):
            raise LifecycleError("prior managed artifact inventory mode is invalid")
        atomic_write_bytes(inventory_path, payload, mode=mode)
    elif inventory_path.exists():
        inventory_path.unlink()


def _migration_receipt(metadata: dict[str, Any]) -> dict[str, Any] | None:
    receipt = metadata.get("migration_receipt")
    if not isinstance(receipt, dict):
        return None
    return copy.deepcopy(receipt)


def _require_conversion_authority(
    metadata: dict[str, Any],
    *,
    actor: str,
    authority: str,
) -> None:
    receipt = metadata.get("migration_receipt")
    if not isinstance(receipt, dict):
        return
    required_actor = receipt.get("required_actor")
    required_authority = receipt.get("required_authority")
    if required_actor is None and required_authority is None:
        return
    if not isinstance(required_actor, str) or not required_actor.strip():
        raise LifecycleError("conversion actor binding is invalid")
    if not isinstance(required_authority, str) or not required_authority.strip():
        raise LifecycleError("conversion authority binding is invalid")
    if actor != required_actor:
        raise LifecycleError("conversion actor does not match reviewed migration")
    if authority != required_authority:
        raise LifecycleError("conversion authority does not match reviewed migration")


def apply_project(
    request: dict[str, Any],
    *,
    expected_base_digest: str,
    expected_plan_digest: str,
    actor: str,
    authority: str,
) -> LifecycleReceipt:
    """Apply exactly one reviewed plan and retain rollback evidence."""

    normalized = _normalize_request(request)
    actor = _require_nonempty(actor, label="actor")
    authority = _require_nonempty(authority, label="authority")
    _require_conversion_authority(
        normalized.metadata,
        actor=actor,
        authority=authority,
    )
    initial = preview_project(request)
    if initial.base_digest != expected_base_digest:
        raise StaleLifecycleError("reviewed base digest is stale")
    if initial.plan_digest != expected_plan_digest:
        raise StaleLifecycleError("reviewed plan digest is stale")
    if initial.conflicts:
        raise LifecycleError("lifecycle plan has conflicts")
    if initial.blocked:
        raise LifecycleError("lifecycle plan is blocked")
    if not initial.valid:
        raise LifecycleError("lifecycle plan is no longer valid")

    _, _, lock_path = _control_paths(normalized.project_root)
    with file_lock(lock_path):
        if normalized.operation == "migration":
            authentication_errors = validate_authenticated_migration_request(
                request,
                normalized.project_root,
                revalidate_sources=True,
            )
            if authentication_errors:
                raise LifecycleError("; ".join(authentication_errors))
        current = preview_project(request)
        if current.base_digest != expected_base_digest:
            raise StaleLifecycleError("reviewed base digest is stale")
        if current.plan_digest != expected_plan_digest:
            raise StaleLifecycleError("reviewed plan digest is stale")
        if current.conflicts:
            raise LifecycleError("lifecycle plan has conflicts")
        if current.blocked:
            raise LifecycleError("lifecycle plan is blocked")
        if not current.valid:
            raise LifecycleError("lifecycle plan is no longer valid")

        transaction_id = str(uuid.uuid4())
        transaction_root = _manifest_path(
            normalized.project_root,
            transaction_id,
        ).parent
        prior_root = transaction_root / "prior"
        prior_root.mkdir(parents=True, exist_ok=False)
        manifest_path = transaction_root / "manifest.json"
        manifest: dict[str, Any] = {
            "schema_version": 1,
            "transaction_id": transaction_id,
            "operation": normalized.operation,
            "state": "prepared",
            "base_digest": current.base_digest,
            "plan_digest": current.plan_digest,
            "candidate_digest": current.candidate_digest,
            "actor": actor,
            "authority": authority,
            "artifacts": [],
            "managed_inventory": None,
            "metadata": copy.deepcopy(normalized.metadata),
            "error": None,
        }
        _write_manifest(manifest_path, manifest)
        managed = _load_managed(normalized.project_root)
        for artifact in normalized.artifacts:
            managed[artifact.path] = _sha256(artifact.content)
        managed_value = _managed_value(managed)
        inventory_record = _snapshot_managed_inventory(
            normalized.project_root,
            prior_root,
            managed_value,
        )
        records: list[dict[str, Any]] = []
        for artifact in normalized.artifacts:
            target = _project_artifact_target(
                normalized.project_root,
                artifact.path,
            )
            existed = target.is_file() and not target.is_symlink()
            prior_payload = target.read_bytes() if existed else b""
            if existed:
                prior = prior_root / artifact.path
                prior.parent.mkdir(parents=True, exist_ok=True)
                atomic_write_bytes(
                    prior,
                    prior_payload,
                    mode=stat.S_IMODE(target.stat().st_mode),
                )
            records.append(
                {
                    "path": artifact.path,
                    "mode": artifact.mode,
                    "prior_exists": existed,
                    "prior_sha256": _sha256(prior_payload) if existed else None,
                    "candidate_sha256": _sha256(artifact.content),
                }
            )
        manifest["artifacts"] = records
        manifest["managed_inventory"] = inventory_record
        _write_manifest(manifest_path, manifest)
        manifest["state"] = "committing"
        _write_manifest(manifest_path, manifest)

        attempted: list[_Artifact] = []
        inventory_attempted = False
        try:
            for artifact in normalized.artifacts:
                attempted.append(artifact)
                _replace_project_artifact(
                    normalized.project_root,
                    artifact.path,
                    artifact.content,
                    artifact.mode,
                )
            inventory_path, _, _ = _control_paths(normalized.project_root)
            inventory_attempted = True
            atomic_write_json(inventory_path, managed_value)
            receipt = LifecycleReceipt(
                transaction_id=transaction_id,
                operation=normalized.operation,
                state="committed",
                applied=True,
                base_digest=current.base_digest,
                plan_digest=current.plan_digest,
                candidate_digest=current.candidate_digest,
                changed=current.changed,
                actor=actor,
                authority=authority,
                migration=_migration_receipt(normalized.metadata),
            )
            manifest["state"] = "committed"
            _write_manifest(manifest_path, manifest)
            _write_receipt(normalized.project_root, receipt)
        except BaseException as exc:
            rollback_errors: list[str] = []
            for artifact in reversed(attempted):
                record = next(item for item in records if item["path"] == artifact.path)
                try:
                    if record["prior_exists"]:
                        prior = prior_root / artifact.path
                        _replace_project_artifact(
                            normalized.project_root,
                            artifact.path,
                            prior.read_bytes(),
                            stat.S_IMODE(prior.stat().st_mode),
                        )
                    else:
                        _remove_project_artifact(
                            normalized.project_root,
                            artifact.path,
                        )
                except BaseException as rollback_error:
                    rollback_errors.append(
                        f"{artifact.path}: {type(rollback_error).__name__}: {rollback_error}"
                    )
            if inventory_attempted:
                try:
                    _restore_managed_inventory(
                        normalized.project_root,
                        transaction_root,
                        inventory_record,
                    )
                except BaseException as rollback_error:
                    rollback_errors.append(
                        "managed-artifacts.json: "
                        f"{type(rollback_error).__name__}: {rollback_error}"
                    )
            manifest["state"] = (
                "recovery-required" if rollback_errors else "rolled-back"
            )
            manifest["error"] = f"{type(exc).__name__}: {exc}"
            if rollback_errors:
                manifest["error"] += "; rollback: " + "; ".join(rollback_errors)
            _write_manifest(manifest_path, manifest)
            receipt = LifecycleReceipt(
                transaction_id=transaction_id,
                operation=normalized.operation,
                state=manifest["state"],
                applied=False,
                base_digest=current.base_digest,
                plan_digest=current.plan_digest,
                candidate_digest=current.candidate_digest,
                changed=current.changed,
                actor=actor,
                authority=authority,
                error=manifest["error"],
                migration=_migration_receipt(normalized.metadata),
            )
            _write_receipt(normalized.project_root, receipt)
            return receipt

        return receipt


def _load_transaction(project_root: Path, transaction_id: str) -> tuple[Path, dict[str, Any]]:
    try:
        identifier = validate_relative_path(
            transaction_id,
            label="transaction id",
            allow_dot=False,
        )
    except PathContractError as exc:
        raise LifecycleError(str(exc)) from exc
    if "/" in identifier:
        raise LifecycleError("transaction id must not contain a path separator")
    path = _manifest_path(project_root, identifier)
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise LifecycleError(f"cannot load transaction manifest: {exc}") from exc
    if not isinstance(manifest, dict) or manifest.get("transaction_id") != identifier:
        raise LifecycleError("transaction manifest identity does not match")
    return path, manifest


def _manifest_artifacts(
    project_root: Path,
    manifest: dict[str, Any],
) -> tuple[_Artifact, ...]:
    raw_records = manifest.get("artifacts")
    if not isinstance(raw_records, list):
        raise LifecycleError("transaction artifact records are invalid")
    artifacts: list[_Artifact] = []
    for index, record in enumerate(raw_records):
        if not isinstance(record, dict) or not isinstance(record.get("path"), str):
            raise LifecycleError("transaction artifact record is invalid")
        relative = validate_relative_path(
            record["path"],
            label=f"transaction artifact {index}",
            allow_dot=False,
        )
        target = resolve_project_path(project_root, relative)
        artifacts.append(_Artifact(relative, target, b"", int(record.get("mode", 0o644)), None))
    return tuple(sorted(artifacts, key=lambda item: item.path))


def recover_project(
    project_root: Path,
    transaction_id: str,
    *,
    expected_current_digest: str,
    actor: str,
    authority: str,
) -> LifecycleReceipt:
    """Restore prior bytes for a nonterminal transaction at an exact current base."""

    root = Path(project_root).resolve()
    if not root.is_dir():
        raise LifecycleError(f"project root is not a directory: {root}")
    actor = _require_nonempty(actor, label="actor")
    authority = _require_nonempty(authority, label="authority")
    _, _, lock_path = _control_paths(root)
    with file_lock(lock_path):
        manifest_path, manifest = _load_transaction(root, transaction_id)
        if manifest.get("state") not in NONTERMINAL_STATES:
            raise LifecycleError("transaction is not recoverable")
        artifacts = _manifest_artifacts(root, manifest)
        current_digest = _artifact_state_digest(artifacts)
        if current_digest != expected_current_digest:
            raise StaleLifecycleError("recovery current digest is stale")
        transaction_root = manifest_path.parent
        errors: list[str] = []
        records = {
            record["path"]: record
            for record in manifest["artifacts"]
            if isinstance(record, dict) and isinstance(record.get("path"), str)
        }
        for artifact in reversed(artifacts):
            record = records[artifact.path]
            try:
                if record.get("prior_exists") is True:
                    prior = resolve_project_path(
                        transaction_root,
                        f"prior/{artifact.path}",
                        label="transaction prior path",
                    )
                    if not prior.is_file():
                        raise LifecycleError(f"missing prior bytes for {artifact.path}")
                    _replace_project_artifact(
                        root,
                        artifact.path,
                        prior.read_bytes(),
                        stat.S_IMODE(prior.stat().st_mode),
                    )
                else:
                    _remove_project_artifact(root, artifact.path)
            except BaseException as exc:
                errors.append(f"{artifact.path}: {type(exc).__name__}: {exc}")
        try:
            _restore_managed_inventory(
                root,
                transaction_root,
                (
                    manifest["managed_inventory"]
                    if isinstance(manifest.get("managed_inventory"), dict)
                    else None
                ),
            )
        except BaseException as exc:
            errors.append(
                "managed-artifacts.json: "
                f"{type(exc).__name__}: {exc}"
            )
        manifest["state"] = "recovery-required" if errors else "rolled-back"
        manifest["error"] = "; ".join(errors) if errors else None
        manifest["recovery"] = {"actor": actor, "authority": authority}
        _write_manifest(manifest_path, manifest)
        receipt = LifecycleReceipt(
            transaction_id=transaction_id,
            operation=str(manifest.get("operation", "recovery")),
            state=manifest["state"],
            applied=False,
            base_digest=str(manifest.get("base_digest", "")),
            plan_digest=str(manifest.get("plan_digest", "")),
            candidate_digest=current_digest,
            changed=tuple(item.path for item in artifacts),
            actor=actor,
            authority=authority,
            error=manifest["error"],
            migration=(
                _migration_receipt(manifest["metadata"])
                if isinstance(manifest.get("metadata"), dict)
                else None
            ),
        )
        _write_receipt(root, receipt)
        return receipt
