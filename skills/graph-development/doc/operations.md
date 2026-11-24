# Operations

Read the [README](../README.md) first and select the caller's interpreter and skill/project roots. Command patterns below use the same quoted `PYTHON`, `SKILL_DIR` and `PROJECT_ROOT` values.

## Preview before apply

Scaffolding, specification synchronization, project lifecycle changes, graph rewrites, promotion, deferral, semantic invalidation and outcome migration preview by default. Review changed, preserved, blocked and conflicting paths. Apply only the exact base, plan, graph, run, timestamp, actor and authority values required by that operation.

If a bound value changes, create a fresh preview. There is no unrestricted overwrite path. See [project lifecycle](../references/project-lifecycle.md).

## Run the approved projection

Only projected schema-v2 nodes are executable. Inspect status before each transition and use its current `run_version`; stale versions are rejected.

```sh
"$PYTHON" "$SKILL_DIR/scripts/graphctl.py" status \
  --graph "$PROJECT_ROOT/development-graph.json" \
  --run-dir "$PROJECT_ROOT/.development-graph"
```

Deterministic local commands are argument arrays. Other executor kinds require a declared agent/host adapter or human. A claim binds a worker, lease, generation and idempotency key; only that worker can complete its live lease. Human gates are never locally executed. Destructive nodes require current approval and rollback evidence.

Use the [runtime contract](../references/runtime-contract.md) for initialization, claims, completion, failure, approvals, leases and recovery.

## Evolve without expanding scope

| Operation | Current-run effect |
| --- | --- |
| Definition recording | Records durable DLG knowledge; does not enroll work |
| Promotion | Enrolls named work mapped to approved requirements, regression or acceptance gaps |
| Deferral | Records non-enrolling backlog |
| Semantic invalidation | Reopens explicitly requested or proven enrolled consumers; uncertain impact needs a gate |
| Outcome migration | Previews by default; explicitly authorized successor-run apply preserves its source run |

After bounded graph work, return to the projected critical path. Backlog does not prevent current-run completion. See the [mutation contract](../references/mutation-contract.md).

Outcome migration is an explicitly authorized successor-run operation. It preserves source-run bytes and refuses stale bindings, active leases and duplicate successor destinations; same-run remains preview-only. An outcome change must not be applied through ordinary promotion or a definition rewrite.

Request surfaces stay distinct:

| Command | Request contract |
| --- | --- |
| `graphctl.py promote` | [Promotion](../references/development-graph.promotion.schema.json) |
| `graphctl.py defer` | [Deferral](../references/development-graph.defer.schema.json) |
| `graphctl.py semantic-invalidate` | [Semantic invalidation](../references/development-graph.invalidation.schema.json) |
| `graphctl.py migrate-outcome` | [Outcome migration](../references/development-graph.outcome-migration.schema.json) |

Legacy `feedback` and forward `invalidate` operations apply only to schema-v1 runs. Schema-v2 runs use their promotion and semantic-impact contracts; do not substitute the legacy commands.

## Validate graph and specification lineage

Run `validate_graph.py --json` against the graph and its generated Markdown. Its `canonical_sha256` and `markdown_marker` fields provide the exact checksum and marker; do not guess or scrape an invented marker. The generated view carries this form:

```text
<!-- development-graph-sha256: 64_lowercase_hex_characters -->
```

For specification-to-delivery work, `specification-manifest.json` binds project documents, decisions, producers, consumers and approvals to the graph. Its [schema](../references/specification-manifest.schema.json) and [specification validator](../scripts/validate_specifications.py) define the machine-checked lineage. The default bounded workflow uses `specctl.py snapshot`, reviewed `specctl.py sync`, and `specctl.py verify`. The standalone validator prints `Specification validation: PASS` only after its structural and lineage checks pass; semantic verification and human approval remain separate.

The [batch decision helper](../scripts/add_specification_decisions.py) remains a compatibility/diagnostic interface. Prefer the composite workflow for routine synchronization and consult [specification development](../references/specification-development.md) before choosing document or decision paths.

## Verify and recover

```sh
"$PYTHON" "$SKILL_DIR/scripts/graphctl.py" verify-ledger \
  --graph "$PROJECT_ROOT/development-graph.json" \
  --run-dir "$PROJECT_ROOT/.development-graph"
```

Pair ledger integrity with actual output evidence and acceptance. A lock file, process, run status or zero exit alone is insufficient.

Preserve current files, prior bytes, snapshots, receipts and event evidence before repair. Use exact-current lifecycle recovery for a nonterminal transaction and explicit runtime resume for an expired lease. Never edit `run.json`, immutable projections or historical events by hand. On checksum drift, follow the governed rewrite path; on ledger corruption, stop transitions and recover from trusted evidence.

See [validation rules](../references/validation-rules.md), [schema migrations](../references/schema-migrations.md) and [troubleshooting](../README.md#troubleshooting-and-recovery).
