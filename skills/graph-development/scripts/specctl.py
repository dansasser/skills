"""Compact command interface for specification workspace operations."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

from specification_workflow import (
    WorkspacePaths,
    plan_workspace_synchronization,
    report_to_json,
    snapshot_workspace,
    verify_workspace,
)
from project_lifecycle import (
    LifecycleError,
    StaleLifecycleError,
    apply_project,
    preview_project,
)


def _artifact_path(project_root: Path, raw_path: str) -> Path:
    path = Path(raw_path)
    return path if path.is_absolute() else project_root / path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="operation", required=True)
    snapshot = subparsers.add_parser(
        "snapshot",
        help="emit a bounded read-only specification workspace summary",
    )
    snapshot.add_argument("--project-root", type=Path, required=True)
    snapshot.add_argument("--graph", required=True)
    snapshot.add_argument("--manifest", required=True)
    snapshot.add_argument("--decisions")
    snapshot.add_argument("--markdown", required=True)
    sync = subparsers.add_parser(
        "sync",
        help="synchronize a validated specification artifact group",
    )
    sync.add_argument("--project-root", type=Path, required=True)
    sync.add_argument("--graph", required=True)
    sync.add_argument("--manifest", required=True)
    sync.add_argument("--decisions")
    sync.add_argument("--markdown", required=True)
    sync.add_argument(
        "--profile",
        choices=("starter", "standard", "assurance"),
        default="standard",
    )
    sync.add_argument("--apply", action="store_true")
    sync.add_argument("--expected-base-digest")
    sync.add_argument("--expected-plan-digest")
    sync.add_argument("--actor")
    sync.add_argument("--authority")
    verify = subparsers.add_parser(
        "verify",
        help="run composite graph and specification verification",
    )
    verify.add_argument("--project-root", type=Path, required=True)
    verify.add_argument("--graph", required=True)
    verify.add_argument("--manifest", required=True)
    verify.add_argument("--decisions")
    verify.add_argument("--markdown", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    project_root = args.project_root.resolve()
    paths = WorkspacePaths(
        project_root=project_root,
        graph=_artifact_path(project_root, args.graph),
        manifest=_artifact_path(project_root, args.manifest),
        decisions=(
            _artifact_path(project_root, args.decisions)
            if args.decisions is not None
            else None
        ),
        markdown=_artifact_path(project_root, args.markdown),
    )
    if args.operation == "snapshot":
        report = snapshot_workspace(paths)
    elif args.operation == "sync":
        report, writes = plan_workspace_synchronization(
            paths,
            profile=args.profile,
        )
        if not report.valid or not writes:
            sys.stdout.write(report_to_json(report))
            if not report.valid:
                for error in report.errors:
                    print(f"ERROR {error}", file=sys.stderr)
            return 0 if report.valid else 1
        artifacts = []
        for path, payload, mode in writes:
            relative = path.resolve().relative_to(project_root).as_posix()
            expected = (
                hashlib.sha256(path.read_bytes()).hexdigest()
                if path.is_file()
                else None
            )
            artifacts.append(
                {
                    "path": relative,
                    "content": payload,
                    "mode": mode,
                    "expected_sha256": expected,
                }
            )
        request = {
            "schema_version": 1,
            "operation": "update",
            "project_root": project_root,
            "artifacts": artifacts,
            "metadata": {
                "entrypoint": "specctl sync",
                "profile": args.profile,
            },
        }
        try:
            if not args.apply:
                plan = preview_project(request)
                sys.stdout.write(
                    json.dumps(plan.to_dict(), indent=2, sort_keys=True) + "\n"
                )
                return 0 if plan.valid else 1
            if (
                not args.expected_base_digest
                or not args.expected_plan_digest
                or not args.actor
                or not args.authority
            ):
                raise LifecycleError(
                    "apply requires --expected-base-digest, "
                    "--expected-plan-digest, --actor, and --authority"
                )
            receipt = apply_project(
                request,
                expected_base_digest=args.expected_base_digest,
                expected_plan_digest=args.expected_plan_digest,
                actor=args.actor,
                authority=args.authority,
            )
            sys.stdout.write(
                json.dumps(receipt.to_dict(), indent=2, sort_keys=True) + "\n"
            )
            return 0 if receipt.applied else 1
        except (LifecycleError, StaleLifecycleError, OSError) as exc:
            print(f"Specification sync: FAIL\nERROR {exc}", file=sys.stderr)
            return 1
    else:
        report = verify_workspace(paths)
    sys.stdout.write(report_to_json(report))
    if not report.valid:
        for error in report.errors:
            print(f"ERROR {error}", file=sys.stderr)
    return 0 if report.valid else 1


if __name__ == "__main__":
    raise SystemExit(main())
    plan_workspace_synchronization,
