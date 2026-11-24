# Governed Graph Mutation Contract

Graph evolution is an operator-directed transaction, not autonomous
self-modification. Runtime evidence may motivate durable DLG evolution, but an
explicit request and operator command are required. Recording a definition and
changing the current run are separate operations.

`development-graph.mutation.schema.json` and `scripts/rewrite_graph.py` define
durable definition rewrites. The schema preserves legacy v1 behavior and adds a
schema-v2 record variant.

## Common mutation document

Every mutation names a lowercase `mutation_id`, concrete rationale and durable
evidence, exact expected graph version/checksum, typed operations, and an
approval bound to the current checksum for destructive topology operations.
Supported operations are `add-node`, `replace-node`, `remove-node`, `add-edge`,
`replace-edge`, `remove-edge`, and `update-root`. Graph and context versions are
advanced by the tool.

Preview is the default. It applies operations in memory, validates the complete
candidate, identifies destructive changes, and returns the candidate
checksum/version without writing graph, Markdown, runtime, ledger, or snapshot
state.

## Legacy schema-v1 mutation

A schema-v1 request carries explicit invalidation seeds. Apply rechecks the
reviewed graph and run versions, writes an exact rollback snapshot, replaces the
canonical graph and Markdown, rebinds the whole-definition runtime inventory,
initializes added nodes, invalidates affected existing nodes, appends an event,
and atomically replaces run state. Scheduling-forward invalidation in this
section is legacy v1 only.

## Schema-v2 record-only mutation

A schema-v2 record requires `runtime_effect: record-only`, exact
`expected_run_version`, `causal_episode_id`, `episode_effect`, and
`episode_gate`. Its node and artifact invalidation lists must be empty. Preview
classifies new durable nodes as backlog and evaluates the causal-episode gate;
it does not calculate scheduling invalidation or infer promotion.

Apply rechecks graph, run, outcome, projection, causal, lifecycle-base, and
lifecycle-plan bindings under their locks. It atomically commits the candidate
DLG, generated Markdown, pre-mutation snapshot, `graph-definition-recorded`
event, and the minimally updated run. Only the observed DLG version/checksum,
run version, timestamps, episode record when applicable, and ledger pointer may
change. The frozen projection, runtime node inventory, node states, approvals,
and outcome completion remain unchanged. New DLG nodes remain backlog.

## Distinct schema-v2 current-run operations

Definition recording never substitutes for these request workflows:

- `graphctl.py promote` uses
  `development-graph.promotion.schema.json`. Preview proves exact observed/run/
  outcome/projection bases and maps named durable nodes to contracted
  requirements and acceptance. `existing-requirement` promotion enrolls only
  mapped work. `current-regression` promotion additionally requires proven
  semantic impact plus impact and causal-episode gates. Apply atomically writes
  one immutable successor projection, run state, and ledger events.
- `graphctl.py defer` uses `development-graph.defer.schema.json`. It records an
  already-durable, unenrolled finding without changing projection inventory,
  readiness, or completion.
- `graphctl.py semantic-invalidate` uses
  `development-graph.invalidation.schema.json`. It may reopen explicitly
  requested or proven enrolled semantic consumers. Uncertain enrolled artifact
  consumers require an impact gate; scheduling-only rewires do not invalidate
  verified work.
- `graphctl.py migrate-outcome` uses
  `development-graph.outcome-migration.schema.json`. It previews by default and
  requires explicit outcome authority. Only a successor-run may apply, with
  exact reviewed digests; it preserves source-run bytes and fails closed on stale
  bindings, active leases, or duplicate successor destinations. Same-run remains
  preview-only. Ordinary mutation or promotion cannot apply an outcome change.

The immutable projection shape is defined by
`development-graph.projection.schema.json`. Every apply-capable command is
preview-first, exact-base, authority-bound, append-only in history, and
fail-closed on an active lease or stale digest.

## Destructive topology changes

Removing a node or edge requires an approval object containing
`decision: approved`, approver, evidence, and the current DLG checksum. Approval
does not excuse an invalid candidate or stale base. A node with destructive
project side effects still requires a human gate, incoming approval edge,
concrete rollback, destructive permission, and `approval_required: true`.

## Rollback

A definition snapshot contains exact pre-mutation graph bytes. Rollback is a
reviewed operation: stop claims, preserve graph/Markdown/run/events/evidence,
verify the selected snapshot and ledger record, restore through a new governed
mutation or explicit version-control authority, and verify definition,
projection/runtime binding, event chain, and output-level behavior. Do not
truncate the ledger or copy an old `run.json` over newer events.

## Executable regression anchors

The bundled suite proves record-only completion preservation, semantic-only
impact, mapped regression promotion, and causal independence through:

- `test_completed_run_record_only_addition_preserves_completion`
- `test_repair_episode_survives_fresh_mutation_ids_and_gates_independence`
- `test_shared_acceptance_progress_reactivates_only_owning_episode`
- `test_scheduling_only_rewire_does_not_invalidate_verified_work`
- `test_current_regression_promotion_atomically_reopens_semantic_consumers`
