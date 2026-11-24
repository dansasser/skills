# Development Graph Contract

The canonical artifact is JSON conforming to `development-graph.schema.json` plus the semantic rules enforced by `scripts/validate_graph.py`.

When the graph governs specification development, `specification-manifest.json` is a version-bound catalog conforming to `specification-manifest.schema.json`. It indexes document and decision lineage but does not replace `development-graph.json` as the lifecycle source of truth.

## Top-level fields

- `schema_version`: integer `1`.
- `graph_id`: stable lowercase slug.
- `graph_version`: positive integer incremented for material updates.
- `title`: human-readable project/change name.
- `purpose`: one bounded outcome statement.
- `project`: project identity and context provenance.
- `status`: `draft`, `validated`, `approved`, or `archived`.
- `templates`: template identifiers actually composed into the graph.
- `entry_nodes`: nodes enabled by external input rather than graph predecessors.
- `terminal_nodes`: nodes representing the durable DLG's allowed completed outcomes; a schema-v2 run completes against its frozen projection and outcome contract.
- `nodes`: lifecycle responsibility definitions.
- `edges`: typed relationships.
- `assumptions`: material facts not yet established as verified evidence.
- `risks`: known failure, safety, delivery, or uncertainty risks.

## Project provenance

`project` contains:

- `name`: project name.
- `root`: absolute project root when known, or a stable project identifier in portable examples.
- `instructions`: instruction or policy files read while creating the graph.
- `context_version`: Git commit, artifact hash, timestamped snapshot identifier, or explicit `unversioned` value.

Do not claim current project grounding without real provenance.

## Node contract

Every node requires:

- `id`: unique lowercase slug.
- `type`: `work`, `decision`, `verification`, `human_gate`, `operation`, or `observation`.
- `title`: short human-readable label.
- `goal`: bounded result the node owns.
- `state`: one lifecycle state.
- `executor`: who or what performs the work.
- `inputs`: named required or optional artifacts.
- `outputs`: named artifacts created by the node.
- `acceptance`: evidence required before verification.
- `permissions`: declared read, write, and external capability scope.
- `execution`: retry, timeout, and completion boundaries.
- `side_effects`: reversibility, rollback, and approval requirements.
- `resources`: concurrency-sensitive file areas, interfaces, environments, or capacity domains used for conflict analysis.
- `tags`: project-defined classification labels.

An `agent` or `hybrid` node may also declare an optional `context_control`
binding:

- `version`: integer `3`.
- `plan_path`: project-relative, project-contained context-plan path.
- `mode`: `preview` or `executable`.
- `tokenizer`: stable tokenizer identity.
- `adapter`: stable capsule-adapter slug.
- `retention`: `digest-only` or deliberately selected `explicit-payload`.

The binding makes context planning part of the node claim boundary. It does not
grant capabilities or change scheduling. Nodes without the field preserve V2
behavior. Deterministic and human nodes cannot declare it.

### Node types

- `work`: creates or changes a project artifact.
- `decision`: resolves an explicit question or selects a branch.
- `verification`: produces evidence about upstream artifacts.
- `human_gate`: records human approval or rejection.
- `operation`: performs a deterministic lifecycle or environment action.
- `observation`: measures runtime, rollout, or external behavior.

### Executor kinds

- `deterministic`: ordinary code, tool, CI job, compiler, test, or policy engine.
- `agent`: bounded model-directed loop.
- `human`: human judgment or authorization.
- `hybrid`: declared combination whose completion rule identifies the authoritative evidence.

`executor` fields:

- `kind`: one value above.
- `name`: stable executor or role name.
- `instructions`: bounded task instructions, not a hidden lifecycle.

### Lifecycle states

- `planned`: defined but not enabled.
- `ready`: all current prerequisites satisfied.
- `blocked`: a declared prerequisite or external condition prevents execution.
- `running`: executor is currently working.
- `verifying`: output exists and acceptance evidence is being evaluated.
- `verified`: required evidence passed.
- `invalidated`: previously usable work or evidence became stale.
- `failed`: execution or verification exhausted its policy.
- `needs_human`: explicit human decision is required.
- `cancelled`: intentionally stopped and not eligible to enable descendants.

State is evidence-backed run information. A newly authored graph normally uses `planned`, `blocked`, `needs_human`, or preserved states from a grounded existing graph. Do not invent `verified` states for work not actually checked.

## Artifact contract

Inputs contain:

- `name`: stable local input name.
- `artifact`: project-relative path, URI, contract ID, or durable artifact identifier.
- `required`: boolean.

Outputs contain:

- `name`: stable local output name.
- `artifact`: durable artifact identifier.
- `format`: format or media type.

One artifact has one producer within a graph definition unless the graph explicitly versions the identifiers. `consumes`, `validates`, `approves`, and `invalidates` edges list relevant produced artifact identifiers.

An approval artifact follows the same explicit lineage as other scheduling evidence: the human-gate source declares it as an output, the protected target declares it as an input, and the `approves` edge lists it in `artifacts`.

Specification documents and decision records use the same artifact rules. The specification manifest names their producer and consumers so `validate_specifications.py` can verify graph lineage. Content stays in the documents; scheduling, verification, approval, feedback, and invalidation stay in the graph.

## Acceptance evidence

Each node has one or more acceptance entries:

- `id`: node-local unique slug.
- `kind`: `artifact`, `test`, `schema`, `review`, `policy`, `probe`, or `manual`.
- `criterion`: precise pass condition.
- `evidence`: durable path, report, status check, approval record, or observation expected.

Prefer deterministic evidence. Use model or human review only when semantic judgment is necessary. Producer self-report alone is not acceptance evidence.

## Permissions and side effects

`permissions` contains arrays `read`, `write`, and `external`, plus boolean `destructive`.

`side_effects` contains:

- `level`: `none`, `reversible`, or `destructive`.
- `description`: exact effect or `none`.
- `rollback`: exact rollback/compensation or `none`.
- `approval_required`: boolean.

Rules:

- `destructive` effects require an incoming `approves` edge from a `human_gate`, a nonempty rollback, and `approval_required: true`.
- `reversible` effects require a concrete rollback or compensation.
- If `permissions.destructive` is true, `side_effects.level` must be `destructive`.
- A node that writes or calls a mutating external capability cannot declare `side_effects.level: none`.

### Conflict resources

Permissions and conflict resources answer different questions:

- `permissions.read`, `permissions.write`, and `permissions.external` declare what the executor may access.
- `resources` declares only domains whose concurrent use could be unsafe or exceed a deliberately exclusive capacity.

Shared read-only dependencies belong in `permissions.read`, not `resources`. For example, several provider-verification nodes may all read official documentation without conflicting. A shared writable file, single-writer workspace, exclusive deployment target, or deliberately serialized local-model capacity belongs in `resources`.

The semantic validator treats matching `resources` on unordered mutating nodes as an unsafe overlap. Such nodes must be ordered or connected by one `conflicts` edge. Do not copy every permission entry into `resources`; doing so invents false conflicts.

## Execution boundary

`execution` contains:

- `max_attempts`: positive integer.
- `timeout_minutes`: positive integer.
- `completion_condition`: explicit evidence-backed condition.

Deterministic nodes may additionally declare:

- `command`: nonempty argv array; shell strings are invalid.
- `working_directory`: project-root-relative directory.
- `environment`: names of environment variables an adapter may inherit; values never belong in the graph.
- `evidence_paths`: project-root-relative outputs to digest after execution.
- `capture_limit_bytes`: bounded stdout/stderr preview stored in run state.
- `timeout_seconds`: optional precise local-worker timeout; otherwise `timeout_minutes` is used.

Agent nodes must remain bounded by attempts, time, and acceptance. Graph feedback has a separate traversal bound.

The local worker executes only deterministic nodes with `command`. An absent command means an external deterministic adapter must perform the node. Declared permissions are a policy contract, not an operating-system sandbox.

## Definition and runtime planes

`development-graph.json` records versioned durable DLG intent. It is changed only through normal authoring before a run or the governed mutation contract during a run. Node `state` in the definition describes authored/bootstrap status and does not become a mutable execution log.

`.development-graph/run.json` records one run: live node states, attempts, generations, leases, approvals, idempotency history, evidence references, and compare-and-swap version. Legacy schema v1 binds that state to the whole definition. Schema v2 stores an immutable `outcome_contract`, observed DLG revision, causal episodes, and a checksum-bound frozen projection. Only projected nodes are executable; durable nodes outside it remain backlog.

Controllers and adapters must not copy mutable run state back into the definition. Ungoverned checksum drift stops transitions. A governed schema-v2 definition record may advance the observed DLG revision without changing the frozen projection or current completion.

## Edge contract

Every edge requires:

- `id`: globally unique lowercase slug.
- `from`: source node ID.
- `to`: target node ID.
- `type`: one allowed relationship.
- `condition`: condition enabling the relationship, or `always`.
- `artifacts`: relevant artifact identifiers, possibly empty.
- `max_traversals`: positive integer for feedback, otherwise `null`.
- `exit_condition`: nonempty feedback exit rule, otherwise `null`.

All directed control edges point in forward execution order.

### Scheduling edge types

- `requires`: source completion is a prerequisite for target readiness.
- `consumes`: target consumes named artifacts produced by source.
- `validates`: source verification evidence enables a downstream target.
- `approves`: source human gate authorizes protected target work through a named approval artifact produced by the gate and declared as a target input.
- `blocks`: target remains blocked until the source condition resolves successfully.

These edges form the scheduling graph and must be acyclic.

### Non-scheduling edge types

- `invalidates`: a material source change makes target output or evidence stale.
- `feedback`: failed evidence routes bounded correction from source to target.
- `conflicts`: nodes cannot run concurrently because they share an unsafe resource.

Feedback may close a cycle only when `max_traversals` and `exit_condition` are explicit. Conflict is semantically symmetric even though represented once.

## Entry and terminal rules

- Durable DLG entry nodes have no incoming scheduling edges.
- Durable DLG terminal nodes have no outgoing scheduling edges.
- Every DLG node is reachable from at least one entry through scheduling or feedback flow.
- Every DLG terminal is reachable from at least one entry.
- A graph may have success, rejected, rolled-back, or cancelled terminals when those outcomes are explicit.
- A schema-v2 run has its own projected entry and terminal sets. Completion uses
  those projected terminals plus the `outcome_contract` acceptance mappings;
  unrelated durable terminals do not contribute to outcome completion.

## Change and invalidation rules

A durable source-contract change increments `graph_version`, updates context
when required, and regenerates Markdown. Recording that change into a schema-v2
run does not itself reopen work.

Legacy schema v1 may traverse definition scheduling and declared invalidation
relationships forward. Schema v2 computes current-run impact only from explicit
enrolled semantic dependencies, artifact/evidence lineage, and mapped
acceptance. It does not infer semantic impact from scheduling edges. It preserves
unrelated projected evidence and reports outside-projection consumers without
enrolling them. Reopening projected work requires a previewed, gate-compliant
semantic-invalidation request or current-regression promotion.
