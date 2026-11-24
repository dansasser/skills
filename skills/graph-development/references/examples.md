# Development Graph Examples

These examples are fragments for reasoning. Use the schema and project evidence when authoring a complete graph.

## Specification decision lineage

A material storage decision is represented once as a durable decision artifact and consumed everywhere it can change work:

```json
{
  "id": "storage-backend",
  "status": "resolved",
  "source_path": "doc/decisions/storage-backend.md",
  "decision_node": "resolve-storage-backend",
  "artifact": "decision:storage-backend",
  "affected_nodes": ["define-architecture-and-interfaces", "implement"]
}
```

The decision node produces `decision:storage-backend`. Both affected nodes declare it as a required input and receive a `consumes` edge. Connect it to a targeted implementation plan when the plan depends on that choice. The specification verifier consumes the decision once it is resolved. If the decision changes, graph mutation increments the binding and invalidates the affected paths.

## Typed artifact handoff

```json
{
  "nodes": [
    {
      "id": "implement-parser",
      "outputs": [{"name": "parser patch", "artifact": "artifact:parser-patch", "format": "git-diff"}]
    },
    {
      "id": "run-parser-tests",
      "inputs": [{"name": "parser patch", "artifact": "artifact:parser-patch", "required": true}]
    }
  ],
  "edges": [
    {
      "id": "implement-parser-to-run-parser-tests",
      "from": "implement-parser",
      "to": "run-parser-tests",
      "type": "consumes",
      "condition": "parser patch exists",
      "artifacts": ["artifact:parser-patch"],
      "max_traversals": null,
      "exit_condition": null
    }
  ]
}
```

Why it works: the edge identifies both control order and the exact artifact crossing the boundary.

## Verification with bounded repair

```json
[
  {
    "id": "tests-validate-finish",
    "from": "run-tests",
    "to": "finish-change",
    "type": "validates",
    "condition": "all required tests pass",
    "artifacts": ["artifact:test-results"],
    "max_traversals": null,
    "exit_condition": null
  },
  {
    "id": "tests-feedback-to-implementation",
    "from": "run-tests",
    "to": "implement-change",
    "type": "feedback",
    "condition": "a required test fails",
    "artifacts": ["artifact:test-results"],
    "max_traversals": 3,
    "exit_condition": "all required tests pass, or three repair traversals exhaust and the graph moves to needs_human"
  }
]
```

Why it works: validation meaning and repair control are separate. The feedback path cannot loop forever.

## Destructive operation gate

```json
{
  "gate": {
    "id": "approve-production-migration",
    "type": "human_gate",
    "executor": {"kind": "human", "name": "service owner", "instructions": "Review the dry-run and rollback evidence."},
    "outputs": [
      {"name": "migration approval", "artifact": "artifact:migration-approval", "format": "approval-record"}
    ]
  },
  "operation": {
    "id": "run-production-migration",
    "type": "operation",
    "inputs": [
      {"name": "migration approval", "artifact": "artifact:migration-approval", "required": true}
    ],
    "permissions": {"read": ["production database"], "write": ["production schema"], "external": ["database API"], "destructive": true},
    "side_effects": {
      "level": "destructive",
      "description": "Changes the production schema.",
      "rollback": "Restore snapshot snapshot:pre-migration and redeploy the previous application release.",
      "approval_required": true
    }
  },
  "edge": {
    "id": "approval-to-production-migration",
    "from": "approve-production-migration",
    "to": "run-production-migration",
    "type": "approves",
    "condition": "service owner approves the exact migration and rollback evidence",
    "artifacts": ["artifact:migration-approval"],
    "max_traversals": null,
    "exit_condition": null
  }
}
```

Why it works: the gate produces the approval artifact, the protected operation declares it as an input, and the `approves` edge carries it. Authority, evidence, effect, and rollback are explicit rather than hidden in a generic “deploy” step.

## Shared reads versus conflict resources

Two parallel provider checks may read the same documentation without conflicting:

```json
[
  {
    "id": "verify-provider-a",
    "permissions": {
      "read": ["official provider documentation"],
      "write": ["doc/providers/a.md"],
      "external": [],
      "destructive": false
    },
    "resources": ["doc/providers/a.md"]
  },
  {
    "id": "verify-provider-b",
    "permissions": {
      "read": ["official provider documentation"],
      "write": ["doc/providers/b.md"],
      "external": [],
      "destructive": false
    },
    "resources": ["doc/providers/b.md"]
  }
]
```

Why it works: the common documentation is a shared read permission, while each node's writable artifact is its concurrency-sensitive resource. Add a shared `resources` entry and a `conflicts` edge only when simultaneous use of that domain is actually unsafe or deliberately serialized.

## Invalidation after a changed decision

Suppose `artifact:api-decision` changes after implementation and tests were verified.

```text
select-api-design --consumes--> implement-api --consumes--> run-api-tests
select-api-design --invalidates--> implement-api
implement-api      --invalidates--> run-api-tests
```

Increment the durable graph and context versions and collect fresh evidence for any affected current work. In legacy schema v1, the controller may invalidate `implement-api` and `run-api-tests` through whole-definition traversal. In schema v2, the definition rewrite is record-only: preview `graphctl.py semantic-invalidate` with a `development-graph.invalidation.schema.json` request. Explicitly requested or proven enrolled consumers may reopen; uncertain enrolled artifact consumers require an impact gate. Apply only after every impact and causal gate passes. Do not retain the former test result as proof of the revised design.

## Wrong graph: vague work breakdown

```text
research -> code -> test -> deploy
```

It lacks named artifacts, acceptance evidence, permissions, side effects, executor boundaries, rollback, and bounded failure handling. It is a sketch, not an executable development contract.

## Wrong graph: knowledge graph request

```text
Person --works-at--> Company
Company --located-in--> City
```

This models facts and relationships. It belongs to knowledge-graph tooling, not this skill.

## Wrong graph: unbounded evaluator loop

```text
draft -> evaluate -> draft -> evaluate -> ...
```

Add a traversal limit, objective exit condition, escalation state, and artifact invalidation semantics—or use a local agent loop inside one node if the repeated work does not need lifecycle-level visibility.

## Runnable deterministic binding

After inspecting a Python project, a verification node can bind to a concrete local command:

```json
{
  "id": "run-focused-tests",
  "executor": {
    "kind": "deterministic",
    "name": "Python unittest",
    "instructions": "Run the focused suite and preserve output and the JSON report."
  },
  "execution": {
    "max_attempts": 2,
    "timeout_minutes": 10,
    "timeout_seconds": 600,
    "completion_condition": "The suite exits zero and reports/tests.json proves all required cases passed.",
    "command": ["python3", "-m", "unittest", "discover", "-s", "tests", "-v"],
    "working_directory": ".",
    "environment": ["PYTHONPATH"],
    "evidence_paths": ["reports/tests.json"],
    "capture_limit_bytes": 65536
  }
}
```

The command is an argv array, so shell metacharacters are not interpreted. The adapter records stdout/stderr, exit state, timing, and declared evidence. A zero exit still does not override the node's acceptance contract.

## Operated repair path

In legacy schema v1, a recorded `run-tests` failure may traverse a declared,
bounded feedback edge through `graphctl feedback`; the counter prevents an
unbounded loop. Schema v2 rejects that whole-definition path. It uses a
previewed `graphctl.py semantic-invalidate` request or a
`current-regression` `graphctl.py promote` request. The request names a stable
causal episode, proven enrolled impact, and acceptance-linked
`progress_node_ids`. Equivalent IDs or relabeling cannot create another repair
cycle. Accepted-evidence progress or explicit next-cycle/independence authority
is required before the episode can reopen again.

## Governed rewrite

A runtime observation may justify adding a canary node to the durable DLG. First
create a mutation document whose base checksum matches the current graph and
preview it with `rewrite_graph.py`. For schema v2 the document is a
`runtime_effect: record-only` mutation with empty invalidation seeds. Apply
snapshots the old JSON, validates and replaces the definition, regenerates
Markdown, advances only the observed DLG binding, and appends
`graph-definition-recorded`. The new canary remains non-executable backlog; the
frozen projection and completion do not change.

If the canary is mapped to an existing requirement, preview
`graphctl.py promote` with `development-graph.promotion.schema.json`. If it is
uncertain or adjacent, keep it outside the run and optionally record
`graphctl.py defer` with `development-graph.defer.schema.json`. If it changes
the approved outcome, `graphctl.py migrate-outcome` with
`development-graph.outcome-migration.schema.json` first previews the change and
requires explicit authority. Only an explicitly authorized successor-run may
apply with exact reviewed digests; it preserves source-run bytes and refuses
stale bindings, active leases, or a duplicate successor destination. Same-run
remains preview-only. The graph's recommendation is evidence, never authority to
add mandatory current work.
