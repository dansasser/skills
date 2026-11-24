from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path

try:
    from test_validate_graph import edge, node, valid_graph
except ModuleNotFoundError:  # Package import from the skill root.
    from .test_validate_graph import edge, node, valid_graph


def apply_new_graph(
    command: list[str],
    *,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """Preview and apply a new_graph lifecycle request for test fixtures."""

    preview = subprocess.run(
        command,
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
    )
    if preview.returncode != 0:
        return preview
    plan = json.loads(preview.stdout)
    return subprocess.run(
        command
        + [
            "--apply",
            "--expected-base-digest",
            plan["base_digest"],
            "--expected-plan-digest",
            plan["plan_digest"],
            "--actor",
            "test-fixture",
            "--authority",
            "test:new-graph-fixture",
        ],
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
    )


def runtime_graph(project_root: Path) -> dict:
    graph = copy.deepcopy(valid_graph())
    graph["project"]["root"] = "."
    graph["project"]["instructions"] = []
    graph["project"]["context_version"] = "test:runtime-v1"
    commands = {
        "inspect-context": "from pathlib import Path; Path('context.txt').write_text('context', encoding='utf-8')",
        "implement-change": "from pathlib import Path; Path('result.txt').write_text('implemented', encoding='utf-8')",
        "run-tests": "from pathlib import Path; assert Path('result.txt').read_text(encoding='utf-8') == 'implemented'; Path('tests.json').write_text('{\"ok\":true}', encoding='utf-8')",
        "finish-change": "from pathlib import Path; Path('handoff.md').write_text('# complete', encoding='utf-8')",
    }
    for item in graph["nodes"]:
        item["executor"]["kind"] = "deterministic"
        item["execution"].update({
            "command": [sys.executable, "-c", commands[item["id"]]],
            "working_directory": ".",
            "environment": [],
            "evidence_paths": [],
            "capture_limit_bytes": 65536,
        })
    return graph


def graph_with_gate(project_root: Path) -> dict:
    graph = runtime_graph(project_root)
    approval = {"name": "approval", "artifact": "artifact:approval", "format": "approval-record"}
    gate = node("approve-change", "human_gate", [], [approval])
    gate["executor"]["kind"] = "human"
    target = next(item for item in graph["nodes"] if item["id"] == "implement-change")
    target["inputs"].append({"name": "approval", "artifact": "artifact:approval", "required": True})
    target["permissions"]["destructive"] = True
    target["side_effects"] = {
        "level": "destructive",
        "description": "Replaces the test result.",
        "rollback": "Restore result.txt from the runtime snapshot.",
        "approval_required": True,
    }
    graph["nodes"].insert(1, gate)
    graph["entry_nodes"].append("approve-change")
    graph["edges"].extend([
        edge("approval-to-implementation-consumption", "approve-change", "implement-change", "consumes", ["artifact:approval"]),
        edge("approval-authorizes-implementation", "approve-change", "implement-change", "approves", ["artifact:approval"]),
    ])
    return graph


def outcome_contract(
    graph: dict,
    *,
    impact_policy: dict | None = None,
) -> dict:
    from graph_core import sha256_json

    outcome = "Complete the approved requested change."
    requirement_ids = ["requirement:requested-change"]
    acceptance_ids = ["acceptance:finish-change"]
    authority_evidence = ["test:owner-approved-outcome"]
    return {
        "contract_id": "deliver-requested-change",
        "contract_version": 1,
        "outcome": outcome,
        "outcome_digest": sha256_json(outcome),
        "requirement_ids": requirement_ids,
        "non_goals": ["Execute unrelated durable backlog."],
        "acceptance_ids": acceptance_ids,
        "acceptance_digest": sha256_json(acceptance_ids),
        "terminal_node_ids": ["finish-change"],
        "authority_evidence": authority_evidence,
        "authority_digest": sha256_json(authority_evidence),
        "impact_policy": copy.deepcopy(
            impact_policy
            if impact_policy is not None
            else {
                "max_verified_node_count": 3,
                "max_verified_node_fraction": 0.75,
            }
        ),
        "created_at": "2026-08-09T00:00:00Z",
        "actor": "test-owner",
    }


def outcome_projection(
    graph: dict,
    enrolled_node_ids: list[str] | None = None,
) -> dict:
    from validate_graph import canonical_checksum

    enrolled = set(
        enrolled_node_ids
        if enrolled_node_ids is not None
        else [item["id"] for item in graph["nodes"]]
    )
    nodes = [copy.deepcopy(item) for item in graph["nodes"] if item["id"] in enrolled]
    edges = [
        copy.deepcopy(item)
        for item in graph["edges"]
        if item["from"] in enrolled and item["to"] in enrolled
    ]
    return {
        "schema_version": 1,
        "graph_id": graph["graph_id"],
        "source_graph_version": graph["graph_version"],
        "source_graph_checksum": canonical_checksum(graph),
        "contract_id": "deliver-requested-change",
        "contract_version": 1,
        "projection_version": 1,
        "nodes": nodes,
        "edges": edges,
        "entry_nodes": [item for item in graph["entry_nodes"] if item in enrolled],
        "terminal_nodes": ["finish-change"],
        "semantic_dependencies": [],
        "requirement_mappings": {
            "requirement:requested-change": [item["id"] for item in nodes]
        },
        "acceptance_mappings": {
            "acceptance:finish-change": ["finish-change"]
        },
        "provenance": {
            "actor": "test-owner",
            "authority": "test:owner-approved-outcome",
        },
    }


def backlog_node(node_id: str = "future-debt") -> dict:
    return node(
        node_id,
        "work",
        [],
        [
            {
                "name": "durable backlog record",
                "artifact": f"artifact:{node_id}",
                "format": "markdown",
            }
        ],
    )


def mutation_for(
    graph: dict,
    operation: dict,
    *,
    mutation_id: str = "add-review",
    record_only: bool = False,
    expected_run_version: int | None = None,
) -> dict:
    from validate_graph import canonical_checksum

    mutation = {
        "schema_version": 1,
        "mutation_id": mutation_id,
        "rationale": "Runtime evidence requires a more precise lifecycle contract.",
        "evidence": ["event:1"],
        "expected_graph_version": graph["graph_version"],
        "expected_graph_checksum": canonical_checksum(graph),
        "new_context_version": f"mutation:{mutation_id}",
        "operations": [operation],
        "invalidate": {"nodes": [], "artifacts": []},
        "approval": None,
    }
    if record_only:
        if expected_run_version is None:
            raise ValueError("record-only mutation requires expected_run_version")
        mutation.update(
            {
                "schema_version": 2,
                "runtime_effect": "record-only",
                "expected_run_version": expected_run_version,
                "causal_episode_id": "record-definition-episode",
                "episode_effect": "observe-only",
                "episode_gate": None,
            }
        )
    return mutation
