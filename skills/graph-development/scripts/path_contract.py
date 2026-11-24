"""Portable path resolution for Graph Development skill and project artifacts."""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath
from typing import Any


_DRIVE_ROOT = re.compile(r"^[A-Za-z]:")


class PathContractError(ValueError):
    """Raised when a serialized path is not portable or escapes its authority."""


def skill_root() -> Path:
    """Return the executing distribution root without consulting caller CWD."""

    return Path(__file__).resolve().parent.parent


def validate_relative_path(
    value: str,
    *,
    label: str = "path",
    allow_dot: bool = True,
) -> str:
    """Validate and normalize one serialized POSIX-style relative path."""

    if not isinstance(value, str) or not value.strip():
        raise PathContractError(f"{label} must be a non-empty relative path")
    if "\x00" in value:
        raise PathContractError(f"{label} must not contain NUL characters")
    if value.startswith("~"):
        raise PathContractError(f"{label} must not use a home shortcut")
    if _DRIVE_ROOT.match(value):
        raise PathContractError(f"{label} must not use a drive-root path")
    if "\\" in value:
        raise PathContractError(f"{label} must use portable '/' separators")
    pure = PurePosixPath(value)
    if pure.is_absolute():
        raise PathContractError(f"{label} must be relative")
    if any(part == ".." for part in pure.parts):
        raise PathContractError(f"{label} must not traverse a parent directory")
    normalized = pure.as_posix()
    if normalized == "." and not allow_dot:
        raise PathContractError(f"{label} must name an artifact")
    return normalized


def _resolve_within(root: Path, relative: str, *, label: str) -> Path:
    base = root.resolve()
    normalized = validate_relative_path(relative, label=label)
    candidate = (base / Path(*PurePosixPath(normalized).parts)).resolve()
    try:
        candidate.relative_to(base)
    except ValueError as exc:
        raise PathContractError(f"{label} escapes its root through a symlink") from exc
    return candidate


def resolve_skill_asset(relative: str) -> Path:
    """Resolve a distribution asset relative to the executing skill root."""

    return _resolve_within(skill_root(), relative, label="skill asset path")


def resolve_project_path(
    project_root: Path,
    relative: str,
    *,
    label: str = "project artifact path",
) -> Path:
    """Resolve a serialized project path and reject traversal/symlink escape."""

    return _resolve_within(project_root, relative, label=label)


def project_root_for_graph(
    graph_path: Path,
    graph: dict[str, Any],
    *,
    explicit_project_root: Path | None = None,
) -> Path:
    """Resolve project authority from an explicit root or graph-file directory."""

    if explicit_project_root is not None:
        return explicit_project_root.resolve()
    project = graph.get("project")
    raw_root = project.get("root") if isinstance(project, dict) else None
    if not isinstance(raw_root, str):
        raise PathContractError("graph project.root must be a relative path")
    return _resolve_within(
        graph_path.resolve().parent,
        raw_root,
        label="graph project.root",
    )


def serialize_relative(root: Path, target: Path, *, label: str = "path") -> str:
    """Serialize a path relative to its owning project/run authority."""

    base = root.resolve()
    resolved = target.resolve()
    try:
        relative = resolved.relative_to(base).as_posix()
    except ValueError as exc:
        raise PathContractError(f"{label} is outside its serialization root") from exc
    return relative or "."


def serialize_lexical_relative(
    root: Path,
    target: Path,
    *,
    label: str = "path",
) -> str:
    """Serialize an untrusted directory entry without following a symlink."""

    try:
        relative = target.absolute().relative_to(root.resolve()).as_posix()
    except ValueError as exc:
        raise PathContractError(f"{label} is outside its serialization root") from exc
    return validate_relative_path(relative, label=label, allow_dot=False)
