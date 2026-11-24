"""Preview, apply, or recover a Graph Development project transaction."""

from __future__ import annotations

import argparse
import base64
import json
import sys
from pathlib import Path
from typing import Any

from project_lifecycle import (
    LifecycleError,
    StaleLifecycleError,
    apply_project,
    preview_project,
    recover_project,
)
from schema_migrations import (
    MigrationError,
    build_lifecycle_migration_request,
)


def _load_request(path: Path, project_root: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise LifecycleError(f"cannot load lifecycle request: {exc}") from exc
    if not isinstance(value, dict):
        raise LifecycleError("lifecycle request must be a JSON object")
    if value.get("operation") == "migration":
        raise LifecycleError(
            "migration requests require --migration so source bindings are authenticated"
        )
    artifacts = value.get("artifacts")
    if not isinstance(artifacts, list):
        raise LifecycleError("lifecycle request artifacts must be an array")
    decoded: list[dict[str, Any]] = []
    for index, record in enumerate(artifacts):
        if not isinstance(record, dict):
            raise LifecycleError(f"artifact {index} must be an object")
        candidate = dict(record)
        encoded = candidate.pop("content_base64", None)
        if not isinstance(encoded, str):
            raise LifecycleError(f"artifact {index} requires content_base64")
        try:
            candidate["content"] = base64.b64decode(encoded, validate=True)
        except (ValueError, base64.binascii.Error) as exc:
            raise LifecycleError(
                f"artifact {index} content_base64 is invalid"
            ) from exc
        decoded.append(candidate)
    result = dict(value)
    result["project_root"] = project_root
    result["artifacts"] = decoded
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--request", type=Path)
    parser.add_argument(
        "--migration",
        type=Path,
        help="relative-path migration bundle descriptor",
    )
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expected-base-digest")
    parser.add_argument("--expected-plan-digest")
    parser.add_argument("--actor")
    parser.add_argument("--authority")
    parser.add_argument("--recover")
    parser.add_argument("--expected-current-digest")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.recover is not None:
            if (
                not args.expected_current_digest
                or not args.actor
                or not args.authority
            ):
                raise LifecycleError(
                    "recovery requires --expected-current-digest, --actor, and --authority"
                )
            receipt = recover_project(
                args.project_root,
                args.recover,
                expected_current_digest=args.expected_current_digest,
                actor=args.actor,
                authority=args.authority,
            )
            print(json.dumps(receipt.to_dict(), indent=2, sort_keys=True))
            return 0 if receipt.state == "rolled-back" else 1
        if args.request is not None and args.migration is not None:
            raise LifecycleError("preview/apply accepts only one of --request or --migration")
        if args.migration is not None:
            try:
                descriptor = json.loads(
                    args.migration.resolve().read_text(encoding="utf-8")
                )
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise LifecycleError(
                    f"cannot load migration descriptor: {exc}"
                ) from exc
            request, _ = build_lifecycle_migration_request(
                args.project_root.resolve(),
                descriptor,
            )
        elif args.request is not None:
            request = _load_request(
                args.request.resolve(),
                args.project_root.resolve(),
            )
        else:
            raise LifecycleError("preview/apply requires --request or --migration")
        if not args.apply:
            plan = preview_project(request)
            print(json.dumps(plan.to_dict(), indent=2, sort_keys=True))
            return 0 if plan.valid else 1
        if (
            not args.expected_base_digest
            or not args.expected_plan_digest
            or not args.actor
            or not args.authority
        ):
            raise LifecycleError(
                "apply requires --expected-base-digest, --expected-plan-digest, "
                "--actor, and --authority"
            )
        receipt = apply_project(
            request,
            expected_base_digest=args.expected_base_digest,
            expected_plan_digest=args.expected_plan_digest,
            actor=args.actor,
            authority=args.authority,
        )
        print(json.dumps(receipt.to_dict(), indent=2, sort_keys=True))
        return 0 if receipt.state == "committed" else 1
    except (LifecycleError, MigrationError, StaleLifecycleError, OSError) as exc:
        print(f"Project lifecycle: FAIL\nERROR {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
