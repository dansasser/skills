# Development Graph Validation Rules

The JSON Schema validates document shape. `scripts/validate_graph.py` adds graph semantics that JSON Schema alone cannot express. A graph is valid only when both layers pass.

## Structural rules

- The root object and every node and edge contain all fields required by `development-graph.schema.json`.
- Unknown fields are rejected where the schema sets `additionalProperties: false`.
- Identifiers use lowercase slugs and are unique within their scope.
- Enumerated node types, states, executor kinds, acceptance kinds, side-effect levels, and edge types use only contract values.
- Collections that define evidence, entry nodes, terminal nodes, nodes, and edges are non-empty where required.

## Referential rules

- Every edge endpoint names an existing node.
- Every entry and terminal identifier names an existing node and appears only once.
- Self-edges are rejected.
- Node acceptance IDs are unique within the node.
- Edge IDs and node IDs are globally unique in their respective collections.

## Control-flow rules

Scheduling edge types are `requires`, `consumes`, `validates`, `approves`, and `blocks`.

- The scheduling subgraph must be acyclic.
- Entry nodes have no incoming scheduling edges.
- Terminal nodes have no outgoing scheduling edges.
- Every node is reachable from at least one entry through scheduling or feedback edges.
- At least one declared terminal is reachable from an entry.
- Non-scheduling edges (`invalidates`, `feedback`, `conflicts`) express semantics without silently changing normal scheduling order.

## Typed-edge rules

- A `validates` edge starts at a `verification` node.
- An `approves` edge starts at a `human_gate` node.
- A `feedback` edge starts at `verification`, `human_gate`, or `observation`, includes `max_traversals >= 1`, has an objective `exit_condition`, and does not target a terminal node.
- Non-feedback edges use `max_traversals: null` and `exit_condition: null`.
- An `invalidates` edge lists the affected artifact or artifacts.
- A `consumes` edge lists artifacts produced by its source and declared as inputs by its target.
- A `validates` edge lists verification evidence produced by its source and declared as input by its downstream target.
- An `approves` edge lists an approval artifact produced by its human-gate source and declared as an input by its protected target.
- An `invalidates` edge lists artifacts produced by its target.

## Artifact rules

- Each output artifact has one producer. Multiple producers make lineage ambiguous and are rejected.
- Each named input artifact is produced by an upstream graph node unless `required` is false or it uses the `external:` prefix.
- Every required input has an incoming `consumes` edge from its producer.
- Every artifact named on an edge is declared in the appropriate source or target node.
- A node in `invalidated` state has at least one incoming `invalidates` edge.

## Resource conflict rules

- `permissions.read` records ordinary shared read-only dependencies.
- `resources` records only concurrency-sensitive domains whose overlap could be unsafe or intentionally serialized.
- Unordered mutating nodes with a matching `resources` entry require a `conflicts` edge.
- Ordered mutating nodes may share a conflict resource because scheduling already prevents overlap.
- Do not mirror every permission into `resources`; shared documentation and concurrency-safe read APIs do not create a conflict merely because multiple nodes read them.

## Gate and side-effect rules

- A `human_gate` node uses a `human` executor.
- A node declaring destructive permissions also declares `side_effects.level: destructive`.
- A destructive node has `approval_required: true`, a concrete rollback instruction, and an incoming `approves` edge from a human gate.
- A reversible node has a concrete rollback instruction.
- A node with write or external permissions cannot declare `side_effects.level: none`.
- Acceptance evidence for a node must establish its actual goal. Process existence, port listening, or command success is insufficient when the goal is behavioral.

## Update and invalidation rules

When an input, assumption, decision, instruction, or artifact changes, update the
durable DLG version/context as required, re-run definition validation, and
regenerate the Markdown view. Do not erase old evidence to make a graph look
current; record supersession in project history or version control.

Legacy schema v1 then follows definition scheduling and invalidation edges to
rebind its whole-graph runtime. A schema-v2 definition record does not enroll or
invalidate current work. Schema-v2 invalidation is a separate
`development-graph.invalidation.schema.json` request: explicitly requested or
proven enrolled consumers may seed semantic dependencies, not scheduling edges.
Uncertain enrolled artifact consumers require an impact gate. The request
preserves unrelated verified nodes, reports unprojected consumers without
enrolling them, and enforces all impact and causal-episode gates before apply.

## Markdown parity rule

The JSON document is canonical. Generated Markdown contains exactly one marker in this format:

```text
<!-- development-graph-sha256: 64_lowercase_hex_characters -->
```

The marker is derived from canonicalized JSON. Validation with `--markdown` fails if it is absent or differs, proving the human view was regenerated from the same graph version.

For integrations, run `validate_graph.py --json` and read `canonical_sha256` and `markdown_marker`. Do not scrape Markdown with a guessed marker name.

## Completion rule

Only a fresh successful validator run may be reported with the exact marker:

```text
Graph validation: PASS
```

On failure, report `Graph validation: FAIL`, preserve the validator errors, and do not imply that authoring alone completed the graph.

## Executable binding rules

- A local command is allowed only on a deterministic executor.
- `command` is a nonempty array of nonempty arguments and never a shell string.
- Command arguments cannot contain NUL bytes.
- Working directories and declared evidence paths remain within the project root.
- Environment entries are variable names, not values or secrets.
- Capture and timeout limits are positive and explicit.
- A valid definition with no local command is schedulable through an external adapter; it is not locally executable.

## V3 context-control binding rules

- `context_control` is optional; its absence preserves V2 validation and
  runtime behavior.
- Only `agent` and `hybrid` executors may declare the binding.
- `version` equals `3`; `plan_path` is nonempty, project-relative, and contains
  no parent traversal; `mode`, `retention`, adapter, and tokenizer use the
  public schema contract.
- Executable planning requires the exact tokenizer for the selected adapter.
  Preview token counts cannot authorize model invocation.
- Every required direct input plus applicable policy, authority, approval,
  safety, invalidation, and capability item is mandatory. Missing, stale,
  unauthorized, or provenance-incomplete mandatory context fails closed.
- Optional selection never removes a mandatory item. Mandatory overflow returns
  a typed result before adapter invocation.
- A sealed capsule has no generic filesystem, shell, web, graph-lineage,
  transcript, retrieval, or knowledge-store capability. Additional context and
  actions require typed, bounded broker requests.
- Digest-first retention is the default. Explicit payload retention must be
  selected in the graph and handled as sensitive project runtime state.
- A cache hit requires the complete result key and clean-recomputation artifact
  and provenance equivalence. Same-run replay is evaluated separately.
- The deterministic fake adapter is test evidence only and cannot satisfy the
  real-adapter or live-evaluation gate.

## Specification-manifest validation rules

`references/specification-manifest.schema.json` defines the canonical structural
shape and basic relative-path syntax. For the combined schema, graph-lineage,
filesystem, containment, and nonempty-file result, the manual validator is authoritative.
`scripts/validate_specifications.py` enforces:

- graph ID, graph version, and context version match the validated graph;
- every document path stays within the project root, exists, and is nonempty;
- every document artifact is produced by its declared graph node;
- every declared consumer has the document as an input and an incoming `consumes` edge;
- the specification verification node consumes every required document and every resolved decision, while unresolved decisions remain inventoried through the manifest and open-question document;
- the approval node consumes current specification-verification evidence and produces the approval artifact;
- every approval target declares that artifact and has an incoming `approves` edge;
- every implementation node in the top-level inventory is an approval target. A direct implementation node consumes every required specification artifact;
- a declared implementation plan consumes required specification artifacts and is an approval target when the manifest declares a top-level implementation inventory. Each listed plan consumer consumes the plan artifact; and
- legacy manifests keep their original plan and resolved-decision binding checks.

### Decision-to-implementation lineage

Every material decision has a manifest entry, unique project-contained nonempty
decision record, dedicated graph decision node, unique output artifact, and at
least one affected node. Every affected node consumes that artifact through a
`consumes` edge. Both `open` and `deferred` decisions remain unresolved. In a
manifest with a top-level implementation inventory, they directly block only
their declared affected nodes. Cataloging a decision from the current
bound context is a definition-only elaboration and increments only
`graph_version`; changed evidence or a resolved/revised decision requires a new
context version and targeted downstream invalidation.

Only a fresh successful run may be reported with the exact marker:

```text
Specification validation: PASS
```

This deterministic marker does not establish semantic quality or stakeholder approval. Those remain evidence-producing graph nodes.

### Composite specification verification

`scripts/specctl.py verify` is the default bounded verification entrypoint for
specification workspaces. It composes the graph and specification validators;
those validators remain authoritative, and the composite command preserves
every underlying error instead of weakening or replacing it.

- Graph, specification, decision-catalog, containment, and local Markdown-link
  failures are grouped by their owning artifact surface.
- Successful output contains only counts, versions, checksums, warning totals,
  and validation status so the result is safe to retain in agent context.
- Detailed output is failure-only and remains actionable without printing
  complete graphs, documents, manifests, or decision records.
- Generated artifacts are not read back into model context after a successful
  result; the concise report is the evidence surface.
- Deterministic verification proves structural and lineage contracts.
  Semantic quality, human approval, runtime behavior, and delivery proof remain
  distinct evidence-producing lifecycle nodes.

## Runtime validation rules

- Initialization requires a semantically valid definition. A schema-v2 run also
  requires a valid `outcome_contract` and exact frozen projection supplied
  together; initialization without both is legacy schema v1.
- Every state mutation holds the runtime lock and matches
  `expected-run-version`.
- Legacy schema v1 rejects definition checksum drift and executes its whole
  definition. Schema v2 separately verifies the observed DLG binding and
  immutable projection lineage; only projected nodes are executable.
- Only ready enrolled nodes can be claimed, only the lease owner can finish,
  and completed idempotency keys cannot be reused.
- Human gates are never locally executed. Schema-v2 destructive approvals bind
  run ID, projection checksum, target node, and declared gate.
- Running nodes cannot be invalidated or structurally rewritten. Expired leases
  recover through an explicit ledger event, never a silent state edit.
- Causal `episodes`, their `progress_node_ids`, and ledger witnesses must agree;
  equivalent relabeling cannot reset a repair cycle.
- Event sequence, previous hashes, event hashes, outcome/projection digests, and
  the `run.json` last-event pointer must agree.
- Schema-v2 completion is derived from the outcome contract and projected
  acceptance/terminal mappings. `dlg_backlog_nodes`, deferred findings, and
  `needs_human_episodes` are separately reported and do not contribute to
  outcome completion.

Runtime status is not proven by a lock file, run file, process, or zero exit.
Verify the applicable definition/projection binding, legal outcome terminal
state, ledger integrity, accepted evidence, and actual target behavior.

## Mutation validation rules

- Mutation shape must match `development-graph.mutation.schema.json` and the
  semantic validator.
- Expected graph version/checksum and apply-time run version must match under the
  runtime lock; v2 also checks the outcome, projection, and causal bindings.
- Preview validates the complete candidate without writing. Node and edge
  removals require approval bound to the current DLG checksum.
- A candidate cannot orphan required artifacts, break definition entry/terminal
  rules, introduce a scheduling cycle, weaken destructive gates, or add unsafe
  command bindings.
- Legacy schema-v1 apply writes a rollback snapshot, replaces graph/Markdown,
  rebinds runtime inventory, invalidates affected nodes, and appends an event.
- Schema-v2 record apply writes the same durable definition artifacts but
  preserves projection, runtime inventory, states, approvals, and completion;
  enrollment or invalidation requires its separate request schema.
- Event history is never truncated or rewritten during apply or rollback.
