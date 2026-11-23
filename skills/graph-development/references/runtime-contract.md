# Development Graph Runtime Contract

The runtime is a single-host, project-local coordinator for one validated
development lifecycle graph (DLG). It keeps mutable execution state out of
`development-graph.json`, so version control describes durable intent while
`.development-graph/` describes one run.

## Durable files

| Path | Contract |
|---|---|
| `run.json` | Current state, run schema, version, attempts, leases, approvals, evidence references, outcome/projection binding, episodes, and idempotency history |
| `events.jsonl` | Append-only canonical JSON events with sequence, previous hash, and event hash |
| `runtime.lock` | Advisory exclusive mutation lock |
| `evidence/<node>/` | Full stdout/stderr and per-attempt result records |
| `snapshots/` | Exact pre-mutation graph definitions |
| `projections/<run>-v<n>.json` | Immutable schema-v2 execution projections with parent lineage |
| `context/envelopes/` | Sealed-envelope metadata and payload digest; raw payload only under explicit retention |
| `context/requests/` | Typed request decisions and request digests |
| `context/results/` | Schema-valid capsule results and usage evidence |
| `context/cache/` | Exact-key cross-run reuse records |
| `context/evidence/<run-id>/<node-id>/` | Secret-safe context-control evidence |

`development-graph.run.schema.json` defines both persisted runtime shapes. Files
are written with restrictive defaults, same-directory temporary files, file
`fsync`, atomic replace, and directory `fsync`. The ledger is append-only and
fsynced per event.

## Runtime schemas, binding, and compare-and-swap

Legacy schema v1 binds execution directly to `graph_id`, `graph_version`, and
the canonical DLG checksum. Normal transitions stop on any definition drift.

Schema v2 separates durable knowledge from current execution:

- `observed_graph_version` and `observed_graph_checksum` identify the latest DLG
  revision recorded into the run.
- `outcome_contract` freezes approved outcome, requirements, non-goals,
  acceptance IDs, terminals, authority evidence, and impact policy.
- `projection` binds one immutable file by version, path, and checksum. Its
  lineage preserves prior projected definitions while mapped promotion may add
  exact current-DLG nodes.
- only projected nodes are executable; unprojected durable nodes are DLG
  backlog, even after a definition record advances the observed DLG binding.

Every mutating command supplies `expected-run-version`; the controller rejects a
stale value under the exclusive runtime lock. Events carry the applicable
outcome and projection digests. An event is appended before the new `run.json`
is replaced. If a crash creates a ledger event not reflected by run state,
preserve both files and reconcile from the last trusted state rather than
deleting history.

## Node states and readiness

| State | Meaning | Normal exits |
|---|---|---|
| `planned` | Eligible for readiness calculation | `ready`, `blocked`, `needs_human` |
| `ready` | All projected scheduling predecessors are verified | `running` |
| `blocked` | At least one projected scheduling predecessor is not verified | `ready` |
| `running` | Claimed under a live lease | `verified`, `failed`, `needs_human` |
| `verified` | Completion evidence is durably recorded | `invalidated` |
| `failed` | Attempt failed but a reviewed recovery remains | explicit resume or schema-specific repair |
| `invalidated` | Prior evidence is stale after a governed semantic change | explicit resume |
| `needs_human` | Authority, attempt, impact, or causal-episode boundary requires intervention | reviewed gate decision or recovery |
| `cancelled` | Operator ended the node | terminal unless an audited recovery policy changes it |

`verifying` remains graph vocabulary for adapters that split work from
verification. The bundled local worker records completion directly from
`running` to `verified`.

Legacy schema-v1 readiness uses the definition's scheduling edges. Schema-v2
readiness uses only the frozen projection's scheduling edges. Human-gate nodes
become `needs_human`, not locally executable. The projection's
`semantic_dependencies` are used for semantic impact; they are not scheduling
edges and cannot make durable backlog executable.

## Claims, leases, approvals, and idempotency

A claim records worker ID, issue/expiry times, attempt, generation, and an
idempotency key. Claims require projected `ready` state for schema v2, an
unexhausted attempt budget, a current compare-and-swap version, and a current
approval when required. A schema-v1 approval binds the graph checksum; a
schema-v2 approval binds the projection checksum. A later schema-v2 definition
record does not invalidate an otherwise current projection-bound approval.

The worker completes only its own lease. A duplicate completed idempotency key
is rejected. A live lease cannot be resumed or invalidated. An expired lease is
recovered only through an explicit audited `resume` operation. Remote adapters
must fence their own side effects with the idempotency key or an external
transactional mechanism.

## Execution boundary

The local worker executes only deterministic projected nodes with a non-empty
argv array. It uses `shell=False`, a project-contained working directory, a
minimal environment plus explicit allowlisted variable names, a bounded
timeout, a new process group, and durable output files. It does not serialize
environment values into state or events.

Graph `permissions` describe the authorized policy boundary but do not create an
OS sandbox. Run untrusted commands inside an approved container, VM, user
account, or sandbox. External API idempotency and target-system verification
remain the adapter's responsibility.

## V3 context claim boundary

For an opt-in `agent` or `hybrid` node, `graph_worker.py --context-plan` claims
the ready projected node and seals its context envelope before adapter handoff.
Planning uses the node's project-relative plan, current execution checksum, run
checkpoint, and declared tokenizer/adapter identity. Success leaves the node
`running` under the returned lease and idempotency key. Planning failure records
a failed attempt. Preview planning does not claim a node.

Context persistence is digest-first. Envelope metadata, accounting, identities,
and payload digest are retained by default; raw payload is not. Same-run replay
restores only an accepted matching attempt. Cross-run reuse requires the full
result key and clean-recomputation artifact/provenance equivalence. Replay and
cache are separate decisions, and any registered key-component change is a
miss.

Every executable envelope binds its runtime checkpoint, node-contract digest,
adapter identity, selector/compactor versions, artifact versions, inclusion and
omission reasons, parent request digest, and reconciled token categories.
Context writes use `context/context.lock`, durable atomic replacement, and
write-once identity checks. Broker decision history restores counted request
digests across CLI processes so request bounds do not reset on restart. The
controller governs only invocations created through this boundary; it cannot
remove context already loaded into an outer Codex conversation or another host
agent.

## Completion, backlog, and evidence

A zero exit code is necessary for a deterministic node but may not prove its
goal. Node acceptance and evidence paths must establish output-level behavior.
The worker retains full stdout/stderr in evidence files and stores only bounded
previews plus file metadata in run state.

Schema-v2 completion is pinned to `outcome_contract`, its acceptance mappings,
and terminal nodes in the frozen projection. Later DLG additions,
`dlg_backlog_nodes`, and deferred findings do not contribute to outcome completion. `status` reports backlog and `needs_human_episodes` separately so
visibility does not become scope authority. Legacy schema v1 retains
whole-definition terminal semantics.

## Retries, feedback, semantic invalidation, and causal episodes

Node attempts are bounded by `execution.max_attempts`. A failure remains
`failed` while another reviewed attempt is possible; exhaustion becomes
`needs_human`.

Legacy schema-v1 `graphctl feedback` and `graphctl invalidate` operate over the
whole-definition runtime and remain bounded by declared traversal/attempt
limits. Schema v2 rejects those paths. Its
`development-graph.invalidation.schema.json` request previews exact enrolled
impact and reopens semantic dependencies, not scheduling edges. Explicitly
requested enrolled nodes and proven artifact consumers may seed impact.
Uncertain enrolled artifact consumers require an impact gate. Unprojected
consumers are reported but never enrolled or invalidated. Fanout above the
outcome contract's impact policy also requires an authority-bound impact gate.

Schema-v2 repair-cycle definition records, promotions, and invalidations create
or update content-bound causal episodes; observe-only definition records do not create or update an episode. Each episode stores its acceptance scope,
`progress_node_ids`, accepted-evidence witnesses, operations, and status.
Equivalent new IDs or relabeling cannot reset the episode. A required episode gate authorizes the next operation, but applying that repair cycle leaves the episode in `needs_human`. Accepted mapped evidence progress returns the episode to `active`. Episode state does not govern or expand outcome completion.

## Definition evolution and recovery

A schema-v2 definition rewrite is record-only: it advances the observed DLG
binding and ledger while preserving the outcome contract, projection, node
inventory, node states, approvals, and completion. Promotion, deferral, semantic
invalidation, and outcome migration use separate request schemas and commands.
Outcome migration previews by default. An explicitly authorized successor-run may apply only against the exact reviewed digests; it preserves the source run, contract, projection, and ledger bytes and refuses stale bindings, active leases, or a duplicate successor destination. Same-run migration remains preview-only.

Ledger corruption, partial writes, projection/snapshot mismatch, or ungoverned
checksum drift is a stop condition. Preserve evidence, verify filesystem
durability assumptions, and restore from trusted version-control or snapshot
state. Advisory `flock` semantics are intended for a local filesystem; validate
locking before placing runtime state on network storage.

## Executable regression anchors

These instruction promises are exercised by the bundled tests:

- `test_completed_run_record_only_addition_preserves_completion`
- `test_repair_episode_survives_fresh_mutation_ids_and_gates_independence`
- `test_shared_acceptance_progress_reactivates_only_owning_episode`
- `test_scheduling_only_rewire_does_not_invalidate_verified_work`
- `test_current_regression_promotion_atomically_reopens_semantic_consumers`
