# Future Direction

The current system deliberately stops at a trustworthy single-host, project-local control plane. Its contracts are designed so stronger orchestration can be added without replacing project graphs or weakening auditability.

## Non-negotiable direction

**No silent self-rewriting.** A graph may emit evidence and a proposed mutation, but the canonical definition changes only through a schema-valid mutation document, explicit authority, a dry run, compare-and-swap checks, full validation, a rollback snapshot, and an audit event.

Future automation must preserve:

- immutable historical events;
- definition/runtime separation;
- explicit authority boundaries;
- bounded attempts and feedback;
- graph- and run-bound approvals;
- deterministic affected-branch calculation;
- output-level verification; and
- reversible deployment of the control plane itself.

## Extension seams

| Seam | Current contract | Forward use |
|---|---|---|
| Scheduler adapter | `status`, `ready`, `claim`, `complete`, `fail` | CI queues, agent pools, or service schedulers |
| Agent transport | Node executor instructions, lease, idempotency key, evidence JSON | Codex tasks, remote workers, or specialized agents |
| Policy engine | Permissions, side effects, approval, actor, event stream | Organization policy-as-code and scoped credentials |
| Event sink | Canonical hash-chained JSONL | Signed transparency logs, metrics, or external audit stores |
| Evidence store | Stable evidence references and digests | Object storage, attestations, provenance systems |
| Lease store | Run version, lease owner/expiry, fencing key | Transactional distributed coordination |
| Mutation recommender | Explicit mutation schema and dry-run report | Evidence-driven graph improvement proposals |
| Efficiency trace analyzer | Isolated run events, usage, artifacts, and assertions | Profile calibration and regression detection without exposing raw private traces |

Adapters must call the same transition rules rather than editing `run.json` directly.

## Maturity gates

### Remote agent adapters

Ready when adapter conformance tests prove claim fencing, idempotent completion, timeout/cancellation behavior, secret redaction, evidence upload integrity, and safe recovery after duplicate delivery.

### External event and evidence stores

Ready when records are content-addressed, event order is durable, partial upload recovery is tested, retention and access controls are explicit, and a local verification tool can reconstruct the graph/run binding.

### Distributed scheduling

Ready when local `flock` is replaced by a transactional compare-and-swap store with monotonically increasing fencing tokens; failover, partitions, clock skew, duplicate workers, and stale leaders have repeatable fault-injection tests. A database row or leader process merely existing is not proof that scheduling is correct.

### Policy and credential brokers

Ready when node permissions map to short-lived scoped credentials, deny decisions are auditable, approvals cannot be replayed across graph checksums, and secrets never enter definitions, run state, events, logs, or evaluation artifacts.

### Graph recommendation

Ready when recommendations are separately evaluated for validity, minimality, affected-branch accuracy, risk, and human preference. Recommendation generation remains read-only. Application continues through the governed mutation contract.

### Adaptive specification profiles

Ready when frozen candidate-versus-baseline evaluations show that any automatic
profile recommendation preserves deterministic correctness and human-rated
quality while reducing median model cycles, token use, and tool-result bytes.
Budget pressure may narrow elaboration but must never mark a goal blocked,
fabricate completeness, or weaken graph and manifest contracts.

### Autonomous operation

Autonomous claiming of pre-approved deterministic nodes may expand after sandboxing, idempotency, observability, and recovery are proven. Autonomous authority expansion, destructive approval, external publication, spending, or canonical graph rewriting remains out of scope.

## Evaluation direction

Maintain three independent proof layers:

1. deterministic unit, transition, concurrency, corruption, and fault-injection tests;
2. isolated behavioral evaluations for graph quality, trigger boundaries, and operator workflow fidelity; and
3. installed regular-Codex canaries that prove real files, state transitions, evidence, rollback, and audit integrity.

New features are mature only when all applicable layers pass and the limitations are documented. Process existence, installed folders, successful command submission, or schema validity alone never establishes correct operation.

Efficiency evaluations additionally retain API usage, model cycles, tool calls
by type, tool-result bytes, duration, repair rounds, timeouts, parse failures,
assertions, and final artifact hashes. Aggregate reports exclude raw prompts,
credentials, machine paths, generated workspaces, and private trace content.
Harness-level compaction, filtered tool results, and stable prompt-prefix
experiments remain evaluation concerns until controlled trials prove both
quality preservation and a material efficiency gain.
