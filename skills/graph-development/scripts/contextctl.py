"""Bounded CLI for Graph Development V3 context planning and evidence."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from context_broker import ContextBroker
from context_core import (
    ContextControlError,
    DeterministicWordTokenizer,
    PreviewTokenizer,
    Tokenizer,
    build_mandatory_frontier,
    extend_envelope,
    plan_context,
    validate_envelope,
    validate_request,
    validate_result,
)
from context_observability import (
    explain_cache,
    explain_envelope_record,
    explain_invalidation,
    explain_plan,
    inspect_replay,
    validate_context_explanation,
)
from context_store import ContextStore
from graph_core import (
    GraphControlError,
    atomic_write_bytes,
    load_json,
    resolve_within,
)
from validate_graph import validate_graph


def _json_output(value: object) -> None:
    sys.stdout.write(json.dumps(value, indent=2, sort_keys=True) + "\n")


def _contained(root: Path, raw: str) -> Path:
    return resolve_within(root, raw)


def _tokenizer(name: str) -> Tokenizer:
    if name == "deterministic-word-v1":
        return DeterministicWordTokenizer(name)
    if name == "preview":
        return PreviewTokenizer()
    raise ValueError(f"unsupported tokenizer: {name}")


def _write_json(path: Path, value: object) -> None:
    atomic_write_bytes(
        path,
        json.dumps(
            value,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ).encode("utf-8")
        + b"\n",
        mode=0o600,
    )


def _plan_candidate(args: argparse.Namespace) -> tuple[Path, dict[str, Any]]:
    root = args.project_root.resolve()
    graph_path = _contained(root, args.graph)
    input_path = _contained(root, args.input)
    graph = load_json(graph_path)
    graph_report = validate_graph(graph)
    if not graph_report["valid"]:
        raise ValueError(
            "graph validation failed: " + "; ".join(graph_report["errors"])
        )
    config = load_json(input_path)
    tokenizer = _tokenizer(args.tokenizer)
    mandatory = build_mandatory_frontier(
        graph,
        args.node,
        config.get("artifact_registry", {}),
        extras=config.get("mandatory_extras", []),
    )
    budget = config["budget"]
    planned = plan_context(
        graph,
        args.node,
        mandatory,
        config.get("optional_candidates", []),
        config.get("requirement_weights", {}),
        tokenizer=tokenizer,
        mode=args.mode,
        input_limit=budget["input_limit"],
        output_reserve=budget["output_reserve"],
        safety_margin=budget["safety_margin"],
        request_policy=config.get("request_policy"),
    )
    return root, planned


def _plan(args: argparse.Namespace) -> int:
    root, planned = _plan_candidate(args)
    run_dir = _contained(root, args.run_dir)
    store = ContextStore(run_dir)
    if planned["status"] == "mandatory_overflow":
        result_path = store.store_result(planned["result"])
        _json_output(
            {
                "status": "mandatory_overflow",
                "result_digest": planned["result"]["digest"],
                "mandatory_tokens": planned["mandatory_tokens"],
                "available_context": planned["available_context"],
                "result_record": result_path.relative_to(root).as_posix(),
            }
        )
        return 2
    envelope = planned["envelope"]
    record_path = store.store_envelope(envelope)
    output_path: Path | None = None
    if args.envelope_output:
        output_path = _contained(root, args.envelope_output)
        _write_json(output_path, envelope)
    classifications = [
        item["classification"] for item in envelope["manifest"]
    ]
    _json_output(
        {
            "status": "planned",
            "envelope_id": envelope["envelope_id"],
            "envelope_digest": envelope["digest"],
            "mode": envelope["mode"],
            "budget": envelope["budget"],
            "mandatory_items": classifications.count("mandatory"),
            "optional_items": classifications.count("optional"),
            "selected_optional": planned["selection"]["selected_ids"],
            "omitted_optional": planned["selection"]["omitted"],
            "record": record_path.relative_to(root).as_posix(),
            "envelope_output": (
                output_path.relative_to(root).as_posix()
                if output_path is not None
                else None
            ),
        }
    )
    return 0


def _emit_explanation(report: dict[str, Any]) -> int:
    errors = validate_context_explanation(report)
    if errors:
        raise ValueError(
            "context explanation validation failed: " + "; ".join(errors)
        )
    _json_output(report)
    return 0


def _explain_plan(args: argparse.Namespace) -> int:
    try:
        _, planned = _plan_candidate(args)
    except ContextControlError as exc:
        if exc.reason_code is None or exc.artifact_id is None:
            raise
        planned = {
            "status": "blocked",
            "task_node_id": args.node,
            "blocked": [
                {
                    "artifact_id": exc.artifact_id,
                    "reason": exc.reason_code,
                }
            ],
        }
    return _emit_explanation(explain_plan(planned))


def _explain_envelope(args: argparse.Namespace) -> int:
    return _emit_explanation(
        explain_envelope_record(load_json(args.input.resolve()))
    )


def _explain_reuse(args: argparse.Namespace) -> int:
    store = ContextStore(args.run_dir.resolve(), create=False)
    if args.components is not None:
        inspection = store.cache_inspect(load_json(args.components.resolve()))
        return _emit_explanation(explain_cache(inspection))
    missing = [
        option
        for option, value in (
            ("--run-id", args.run_id),
            ("--node", args.node),
            ("--envelope-digest", args.envelope_digest),
        )
        if value is None
    ]
    if missing:
        raise ValueError(
            "replay explanation requires " + ", ".join(missing)
        )
    return _emit_explanation(
        inspect_replay(
            store,
            run_id=args.run_id,
            node_id=args.node,
            envelope_digest=args.envelope_digest,
        )
    )


def _explain_invalidation(args: argparse.Namespace) -> int:
    graph = load_json(args.graph.resolve())
    graph_report = validate_graph(graph)
    if not graph_report["valid"]:
        raise ValueError(
            "graph validation failed: " + "; ".join(graph_report["errors"])
        )
    return _emit_explanation(
        explain_invalidation(graph, set(args.changed_artifact))
    )


def _verify(args: argparse.Namespace) -> int:
    value = load_json(args.input.resolve())
    validators = {
        "envelope": validate_envelope,
        "request": validate_request,
        "result": validate_result,
    }
    errors = validators[args.kind](value)
    _json_output(
        {
            "kind": args.kind,
            "valid": not errors,
            "errors": errors,
            "digest": value.get("digest") if isinstance(value, dict) else None,
        }
    )
    return 0 if not errors else 1


def _resolve_request(args: argparse.Namespace) -> int:
    root = args.project_root.resolve()
    envelope = load_json(_contained(root, args.envelope))
    request = load_json(_contained(root, args.request))
    config = load_json(_contained(root, args.broker_config))
    run_dir = _contained(root, args.run_dir)
    tokenizer = _tokenizer(args.tokenizer)
    store = ContextStore(run_dir)
    broker = ContextBroker(
        artifact_registry=config.get("artifact_registry", {}),
        capability_registry=config.get("capability_registry", {}),
        tokenizer=tokenizer,
        read_scope=set(config.get("read_scope", [])),
        external_scope=set(config.get("external_scope", [])),
        max_requests=config.get("max_requests", 1),
        approval=config.get("approval"),
        seen_request_digests=store.broker_history(envelope.get("digest", "")),
    )
    decision = broker.resolve(envelope, request)
    store.store_request(request)
    store.store_broker_record(decision)
    resumed: dict[str, Any] | None = None
    output_path: Path | None = None
    if decision["decision"] == "approved" and decision["kind"] in {
        "context",
        "artifact",
    }:
        resumed = extend_envelope(
            envelope,
            decision["projection_items"],
            tokenizer=tokenizer,
            parent_request_digest=decision["request_digest"],
        )
        store.store_envelope(resumed)
        if args.envelope_output:
            output_path = _contained(root, args.envelope_output)
            _write_json(output_path, resumed)
    _json_output(
        {
            "decision": decision["decision"],
            "reason": decision["reason"],
            "request_id": decision["request_id"],
            "request_digest": decision["request_digest"],
            "parent_envelope_digest": decision["parent_envelope_digest"],
            "new_envelope_id": (
                resumed["envelope_id"] if resumed is not None else None
            ),
            "new_envelope_digest": (
                resumed["digest"] if resumed is not None else None
            ),
            "projected_tokens": decision.get("projected_tokens"),
            "envelope_output": (
                output_path.relative_to(root).as_posix()
                if output_path is not None
                else None
            ),
        }
    )
    return 0 if decision["decision"] == "approved" else 3


def _cache_inspect(args: argparse.Namespace) -> int:
    components = load_json(args.components.resolve())
    report = ContextStore(args.run_dir.resolve()).cache_inspect(components)
    _json_output(report)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    plan = sub.add_parser("plan", help="plan and persist a sealed envelope")
    plan.add_argument("--project-root", type=Path, required=True)
    plan.add_argument("--graph", required=True)
    plan.add_argument("--input", required=True)
    plan.add_argument("--run-dir", required=True)
    plan.add_argument("--node", required=True)
    plan.add_argument("--mode", choices=("preview", "executable"), required=True)
    plan.add_argument(
        "--tokenizer",
        choices=("preview", "deterministic-word-v1"),
        required=True,
    )
    plan.add_argument("--envelope-output")

    verify = sub.add_parser("verify", help="validate one context contract")
    verify.add_argument(
        "--kind",
        choices=("envelope", "request", "result"),
        required=True,
    )
    verify.add_argument("--input", type=Path, required=True)

    resolve = sub.add_parser(
        "resolve-request",
        help="apply one bounded broker decision and optionally reseal",
    )
    resolve.add_argument("--project-root", type=Path, required=True)
    resolve.add_argument("--envelope", required=True)
    resolve.add_argument("--request", required=True)
    resolve.add_argument("--broker-config", required=True)
    resolve.add_argument("--run-dir", required=True)
    resolve.add_argument(
        "--tokenizer",
        choices=("preview", "deterministic-word-v1"),
        required=True,
    )
    resolve.add_argument("--envelope-output")

    cache = sub.add_parser(
        "cache-inspect",
        help="explain an exact-key cache hit or miss",
    )
    cache.add_argument("--run-dir", type=Path, required=True)
    cache.add_argument("--components", type=Path, required=True)

    explain_plan_parser = sub.add_parser(
        "explain-plan",
        help="explain a context plan without persisting context state",
    )
    explain_plan_parser.add_argument("--project-root", type=Path, required=True)
    explain_plan_parser.add_argument("--graph", required=True)
    explain_plan_parser.add_argument("--input", required=True)
    explain_plan_parser.add_argument("--node", required=True)
    explain_plan_parser.add_argument(
        "--mode",
        choices=("preview", "executable"),
        required=True,
    )
    explain_plan_parser.add_argument(
        "--tokenizer",
        choices=("preview", "deterministic-word-v1"),
        required=True,
    )

    explain_envelope_parser = sub.add_parser(
        "explain-envelope",
        help="explain a full or digest-first envelope record",
    )
    explain_envelope_parser.add_argument("--input", type=Path, required=True)

    explain_reuse_parser = sub.add_parser(
        "explain-reuse",
        help="explain an exact cache decision or same-run replay decision",
    )
    explain_reuse_parser.add_argument("--run-dir", type=Path, required=True)
    explain_reuse_parser.add_argument("--components", type=Path)
    explain_reuse_parser.add_argument("--run-id")
    explain_reuse_parser.add_argument("--node")
    explain_reuse_parser.add_argument("--envelope-digest")

    explain_invalidation_parser = sub.add_parser(
        "explain-invalidation",
        help="explain forward invalidation from changed artifact identities",
    )
    explain_invalidation_parser.add_argument("--graph", type=Path, required=True)
    explain_invalidation_parser.add_argument(
        "--changed-artifact",
        action="append",
        required=True,
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "plan":
            return _plan(args)
        if args.command == "verify":
            return _verify(args)
        if args.command == "resolve-request":
            return _resolve_request(args)
        if args.command == "explain-plan":
            return _explain_plan(args)
        if args.command == "explain-envelope":
            return _explain_envelope(args)
        if args.command == "explain-reuse":
            return _explain_reuse(args)
        if args.command == "explain-invalidation":
            return _explain_invalidation(args)
        return _cache_inspect(args)
    except (
        GraphControlError,
        KeyError,
        OSError,
        TypeError,
        ValueError,
    ) as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": str(exc),
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
