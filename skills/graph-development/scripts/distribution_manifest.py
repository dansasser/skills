"""Build and verify an exact clean-distribution file manifest."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Iterator

from path_contract import serialize_lexical_relative, serialize_relative, skill_root


EXCLUDED_DIRECTORY_NAMES = frozenset(
    {
        ".development-graph",
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "__pycache__",
        "results",
    }
)
EXCLUDED_FILE_SUFFIXES = frozenset({".pyc", ".pyo"})


class DistributionContractError(ValueError):
    """Raised when a source tree cannot be represented as a clean distribution."""


def _distribution_files(root: Path) -> Iterator[Path]:
    root = root.resolve()
    if not root.is_dir():
        raise DistributionContractError(f"distribution root is not a directory: {root}")
    for current, directory_names, file_names in os.walk(root, followlinks=False):
        current_path = Path(current)
        kept_directories: list[str] = []
        for name in sorted(directory_names):
            candidate = current_path / name
            if candidate.is_symlink():
                relative = serialize_lexical_relative(
                    root,
                    candidate,
                    label="distribution symlink",
                )
                raise DistributionContractError(
                    f"clean distribution contains symlink: {relative}"
                )
            if name in EXCLUDED_DIRECTORY_NAMES:
                continue
            kept_directories.append(name)
        directory_names[:] = kept_directories
        for name in sorted(file_names):
            candidate = current_path / name
            if candidate.is_symlink():
                relative = serialize_lexical_relative(
                    root,
                    candidate,
                    label="distribution symlink",
                )
                raise DistributionContractError(
                    f"clean distribution contains symlink: {relative}"
                )
            if candidate.suffix in EXCLUDED_FILE_SUFFIXES:
                continue
            yield candidate


def _file_digest(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return size, digest.hexdigest()


def build_distribution_manifest(root: Path | None = None) -> dict[str, object]:
    """Return a deterministic manifest containing only relative paths."""

    authority = (root or skill_root()).resolve()
    entries: list[dict[str, object]] = []
    for path in _distribution_files(authority):
        size, digest = _file_digest(path)
        entries.append(
            {
                "path": serialize_relative(authority, path, label="distribution file"),
                "size": size,
                "sha256": digest,
            }
        )
    entries.sort(key=lambda entry: str(entry["path"]))
    return {"schema_version": 1, "files": entries}


def _is_text_artifact(path: Path) -> bool:
    payload = path.read_bytes()
    if b"\x00" in payload:
        return False
    try:
        payload.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def text_artifact_paths(root: Path | None = None) -> list[str]:
    """Enumerate all UTF-8 text artifacts regardless of filename suffix."""

    authority = (root or skill_root()).resolve()
    return sorted(
        serialize_relative(authority, path, label="text artifact")
        for path in _distribution_files(authority)
        if _is_text_artifact(path)
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path)
    parser.add_argument("--text-artifacts", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.text_artifacts:
            result: object = text_artifact_paths(args.root)
        else:
            result = build_distribution_manifest(args.root)
    except (DistributionContractError, OSError, ValueError) as exc:
        print(f"Distribution verification: FAIL\nERROR {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
