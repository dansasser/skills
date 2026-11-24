"""Preview or apply a complete, synthetic, one-output schema-v2 example.

This helper creates a toy definition, outcome contract and projection. It does
not approve a real project's specification, initialize a run or execute work.
Use it only in a caller-owned demonstration directory outside the skill.
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path
from typing import Any


SKILL_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

from graph_core import sha256_json  # noqa: E402
from new_graph import build_graph  # noqa: E402
from project_lifecycle import LifecycleError, apply_project, preview_project  # noqa: E402
from render_graph import render_markdown  # noqa: E402
from validate_graph import canonical_checksum, validate_graph  # noqa: E402


RESULT = "Graph Development example complete\n"


def example_artifacts(project_root: Path) -> dict[str, str]:
    graph = build_graph(
        project_root, "local-example", "Local deterministic example", "change", "example:v1"
    )
    graph["purpose"] = "Create and verify one new demonstration result.txt file."
    graph["project"]["root"] = "."
    graph["project"]["instructions"] = []
    task = copy.deepcopy(graph["nodes"][0])
    task.update({
        "id": "write-result", "title": "Write the demonstration result", "type": "work",
        "goal": graph["purpose"], "inputs": [],
        "outputs": [{"name": "demonstration result", "artifact": "result.txt", "format": "text/plain"}],
        "acceptance": [{"id": "exact-result", "kind": "test",
                        "criterion": "result.txt contains the exact demonstration message.",
                        "evidence": "result.txt"}],
        "executor": {"kind": "deterministic", "name": "local example writer",
                     "instructions": "Exclusively create result.txt, then assert its exact content."},
        "permissions": {"read": ["result.txt"], "write": ["result.txt"],
                        "external": [], "destructive": False},
        "side_effects": {"level": "reversible", "description": "Create a new result.txt only.",
                         "rollback": "Remove only the result.txt created by this example.",
                         "approval_required": False},
        "resources": ["result.txt"], "tags": ["synthetic-example"],
    })
    code = (
        "from pathlib import Path; p=Path('result.txt'); "
        f"expected={RESULT!r}; "
        "f=p.open('x', encoding='utf-8'); f.write(expected); f.close(); "
        "assert p.read_text(encoding='utf-8') == expected"
    )
    task["execution"].update({
        "command": [sys.executable, "-c", code], "working_directory": ".",
        "environment": [], "evidence_paths": ["result.txt"], "timeout_seconds": 30,
        "capture_limit_bytes": 4096, "max_attempts": 1,
    })
    graph.update({"nodes": [task], "edges": [], "entry_nodes": ["write-result"],
                  "terminal_nodes": ["write-result"]})
    markdown = render_markdown(graph, "development-graph.json")
    validation = validate_graph(graph, markdown)
    if not validation["valid"]:
        raise ValueError("synthetic example definition is invalid: " + "; ".join(validation["errors"]))

    # These are explicit synthetic fixture identities, not actual project approvals.
    outcome = graph["purpose"]
    acceptance = ["acceptance:example-result"]
    authority = ["example:synthetic-local-demonstration-only"]
    contract = {
        "contract_id": "local-demonstration", "contract_version": 1,
        "outcome": outcome, "outcome_digest": sha256_json(outcome),
        "requirement_ids": ["requirement:example-result"],
        "non_goals": ["Approve or execute a real project's work.", "Overwrite any existing result."],
        "acceptance_ids": acceptance, "acceptance_digest": sha256_json(acceptance),
        "terminal_node_ids": ["write-result"], "authority_evidence": authority,
        "authority_digest": sha256_json(authority),
        "impact_policy": {"max_verified_node_count": 1, "max_verified_node_fraction": 1.0},
        "created_at": "2000-01-01T00:00:00Z", "actor": "synthetic-example",
    }
    projection = {
        "schema_version": 1, "graph_id": graph["graph_id"],
        "source_graph_version": graph["graph_version"],
        "source_graph_checksum": canonical_checksum(graph),
        "contract_id": contract["contract_id"], "contract_version": 1, "projection_version": 1,
        "nodes": [copy.deepcopy(task)], "edges": [], "entry_nodes": ["write-result"],
        "terminal_nodes": ["write-result"], "semantic_dependencies": [],
        "requirement_mappings": {"requirement:example-result": ["write-result"]},
        "acceptance_mappings": {acceptance[0]: ["write-result"]},
        "provenance": {"actor": "synthetic-example", "authority": authority[0]},
    }
    values: dict[str, Any] = {
        "development-graph.json": graph,
        "outcome-contract.json": contract,
        "development-graph.projection.json": projection,
    }
    artifacts = {name: json.dumps(value, indent=2, sort_keys=True) + "\n"
                 for name, value in values.items()}
    artifacts["development-graph.md"] = markdown
    return artifacts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expected-base-digest")
    parser.add_argument("--expected-plan-digest")
    parser.add_argument("--actor")
    parser.add_argument("--authority")
    args = parser.parse_args()
    root = args.project_root.resolve()
    try:
        if root == SKILL_ROOT or root.is_relative_to(SKILL_ROOT):
            raise ValueError("choose a demonstration directory outside the installed skill")
        if not root.is_dir():
            raise ValueError("the project-root must be an existing demonstration directory")
        if (root / "result.txt").exists():
            raise ValueError("result.txt already exists; choose a fresh demonstration directory")
        request = {
            "schema_version": 1, "operation": "bootstrap", "project_root": root,
            "artifacts": [{"path": name, "content": text.encode("utf-8"), "mode": 0o644}
                          for name, text in sorted(example_artifacts(root).items())],
            "metadata": {"entrypoint": "synthetic-local-example", "example_version": 1},
        }
        if not args.apply:
            plan = preview_project(request)
            print(json.dumps(plan.to_dict(), indent=2, sort_keys=True))
            return 0 if plan.valid else 1
        if not all((args.expected_base_digest, args.expected_plan_digest, args.actor, args.authority)):
            raise ValueError("apply requires both reviewed digests, --actor, and --authority")
        receipt = apply_project(
            request, expected_base_digest=args.expected_base_digest,
            expected_plan_digest=args.expected_plan_digest, actor=args.actor, authority=args.authority,
        )
        print(json.dumps(receipt.to_dict(), indent=2, sort_keys=True))
        return 0 if receipt.applied else 1
    except (ValueError, OSError, LifecycleError) as exc:
        print(f"Example failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
