---
name: graph-development
description: Build, validate, operate, and safely evolve executable software-development lifecycle graphs. Use after an explicit $graph-development request or an operator-neutral project policy opts a project into graph governance, including specification-to-delivery lineage, unresolved decisions and downstream consumers, preview-first bootstrap or update, schema migration, durable run state, ready-node scheduling, targeted invalidation, controlled rewrites, or graph-owned context envelopes. It can start from a product conversation, minimum specifications, strong prompt, or existing project at any stage. Do not use for a plain PRD, architecture-only diagram, small understood change without project opt-in, knowledge graph, generic task runner, or distributed workflow-engine deployment.
license: Apache-2.0
metadata:
  version: "4.1.0"
---

# Graph Development

Create, validate, operate, and safely evolve project-specific software-development lifecycle graphs. The canonical definition exposes dependencies, artifact handoffs, evidence, permissions, approvals, invalidation, and bounded repair; the runtime records a particular execution without rewriting intent.

## Invocation and project opt-in

Activate this skill after an explicit `$graph-development` invocation or when an operator-neutral project policy opts the project into Graph Development. Once activated for a project turn, load the applicable graph contract before project mutation. Without explicit invocation or project opt-in, do not make Graph Development mandatory and do not create lifecycle artifacts merely because the conversation concerns software.

## Runtime and paths

Read [setup, examples and verification](README.md). Use Linux and Python 3.12 or newer. Runtime helpers use the Python standard library and POSIX locking/process controls; a model provider, credentials, background service and network access are not core requirements. Other operating systems are unverified.

Resolve the skill root from the loaded `SKILL.md`. In command patterns, `SKILL_DIR` is that resolved root and `PYTHON` is the selected project interpreter; quote both. Locate scripts, schemas, templates and examples relative to the skill root. Supply the caller's target project at runtime and keep authored graphs, run state and output there. Do not assume the current directory is the skill directory or edit bundled assets in place.

Graph permissions describe policy; they do not sandbox the caller's filesystem, subprocesses or network. Bind project commands and external adapters only within the user's authorized scope.

## Boundary check

Use this skill for development lifecycle definitions and their project-local control state.

Use the specification workflow when product or technical specifications must remain connected to decisions, approval, implementation, delivery, and later invalidation.

Do not use it for:

- knowledge graphs of facts, people, documents, or concepts;
- a plain PRD or architecture-only diagram without lifecycle execution semantics;
- a small understood change that is clearer as a short checklist or the `change` template;
- flat plans, issue decomposition, or generic shell task running;
- explaining a codebase without creating or operating a lifecycle graph;
- deploying Temporal, Airflow, Kubernetes, or another distributed workflow engine.

The bundled runtime is a single-host coordinator and adapter contract, not a distributed scheduler or OS sandbox.

## Project policy contract

A project may opt into Graph Development through an operator-neutral project
policy. While that policy applies, use this skill before every project mutation,
even when another skill also governs bounded work. The project policy supplies
the mandate; this public skill does not assume standing authority over every
software project.

Before writing code, tests, documentation, configuration, plans,
specifications, lifecycle artifacts, or other project files, or performing
branch, commit, push, deployment, or other mutating project operations:

1. Read the applicable project instructions and load the canonical
   `development-graph.json`.
2. Load the applicable specification manifest, decision records, and durable
   runtime state when they exist.
3. Establish the exact graph lifecycle node, authority, inputs, evidence, and
   acceptance contract that govern the requested mutation.
4. If graph or specification lineage is absent, stale, or incomplete, classify
   the gap against the approved outcome. Repair it before project mutation only
   when required to establish authority or when it is a mapped acceptance
   blocker; otherwise record or defer it without expanding the current run.
5. Perform the requested work only inside that graph-governed lifecycle. Other
   skills may execute bounded work within it but may not replace it.

Do not substitute an ad hoc plan, ADR, checklist, standalone specification, or
ordinary direct edit for the canonical graph and specification workflow.
Read-only orientation may precede lifecycle loading, but no project mutation
may.

### Fast path precedence

For specification creation, read only the specification-development quick path
and a bounded project snapshot before the first scaffold and validation.
General graph, template, schema, and validation references load only when the
first result reports a gap or the project requires custom topology.

## Required routing

- Read [specification development](references/specification-development.md) first when turning a product conversation, minimum specifications, strong prompt, or existing project at any stage into a specification-to-delivery graph. Its fast path takes precedence over the general routing below until the first composite verification result.
- Read [graph contract](references/graph-contract.md) and [template catalog](references/template-catalog.md) before hand-authoring a topology or changing the bundled scaffold beyond explicit specification decisions and named consumers.
- Read [validation rules](references/validation-rules.md) before repairing validation failures, executing, rewriting, or claiming readiness. Run the validators before reading their implementation.
- Read [runtime contract](references/runtime-contract.md) before initializing state, claiming/completing nodes, approving gates, following feedback, invalidating, resuming, or recovering leases.
- Read [V3 context control](references/context-control.md) before adding a `context_control` binding, planning or extending an envelope, brokering a context/action request, using context replay/cache, or claiming a model node through the worker.
- Read [context observability](references/context-observability.md) before explaining a plan, envelope, replay/cache decision, or invalidation.
- Read [project lifecycle](references/project-lifecycle.md) before applying or recovering bootstrap, update, graph-mutation, or migration artifact groups.
- Read [schema migrations](references/schema-migrations.md) before planning, applying, restoring, or extending a schema migration.
- Read [mutation contract](references/mutation-contract.md) before proposing, previewing, applying, or rolling back a definition rewrite.
- Read `README.md` before using runtime or mutation helpers, installing, or rolling back this skill. The specification fast path and its commands are self-contained in the specification-development reference.
- Read [foundations](references/foundations.md) when graph-versus-loop scope or terminology is unclear.
- Read [examples](references/examples.md) when binding a new project type or executor.
- Read [future direction](references/future-direction.md) only for roadmap or extension work.
- Read [project opt-in](references/project-opt-in.md) when a repository needs an operator-neutral policy that makes Graph Development mandatory without granting it authority to expand the current run.
- Read [sources](references/sources.md) only when researching or updating the knowledge base.

The canonical schemas are [definition](references/development-graph.schema.json), [specification manifest](references/specification-manifest.schema.json), [runtime state](references/development-graph.run.schema.json), [mutation](references/development-graph.mutation.schema.json), [projection](references/development-graph.projection.schema.json), [promotion](references/development-graph.promotion.schema.json), [deferral](references/development-graph.defer.schema.json), [semantic invalidation](references/development-graph.invalidation.schema.json), and [outcome migration](references/development-graph.outcome-migration.schema.json). Do not invent project-specific replacements.

## Pre-write contract

Before writing project artifacts, establish:

1. project root and applicable instructions;
2. purpose, scope, and explicit non-goals;
3. create, operate, validate, rewrite, or recover operation;
4. exact definition, runtime, evidence, and output paths;
5. current graph checksum/version, run version, lifecycle base digest, and
   lifecycle plan digest when present;
6. template plus project-specific executor/evidence bindings;
7. external effects, destructive actions, and human gates;
8. validation, installed/runtime proof, and rollback procedure; and
9. authority for the exact mutation group.

If authority is absent, present the contract and wait. Never silently choose an output location or treat a read-only request as authorization to write.

## Create

1. Inspect project instructions, manifests, architecture decisions, tests, delivery configuration, and existing graph/plan evidence.
2. Define the overall purpose graph, then compose the smallest applicable topology. Do not graph tiny implementation thoughts.
3. Separate deterministic commands, agent/hybrid work, human gates, verification, and observation through executor/node types.
4. Preview the scaffold, review changed/preserved/blocked/conflicting paths, and
   apply only the exact base and plan digests with actor and authority.
   `development-graph.json` remains the only definition source of truth.
5. Keep normal scheduling acyclic. Use explicit bounded feedback for repair and invalidation edges for staleness propagation.
6. Bind acceptance and output-level evidence before execution. An executor reporting completion is not verification.
7. Validate JSON, render Markdown, then validate checksum parity.

Use `scripts/new_graph.py`, `scripts/validate_graph.py`, and `scripts/render_graph.py`; command examples are in the README.

## Develop specifications

For a loose product conversation only, optionally use `$graph-intake` before
authoring when the intended outcome or user context cannot be stated without
guessing, or an ambiguity could materially change scope, constraints, or
acceptance direction. Skip the companion for a strong prompt or adequate
minimum specifications. Accept only its `user-confirmed` handoff, and treat it
as conversation input only, never as specification, decision, approval, graph,
TLG, PLG, DLG, or lifecycle state. Graph Development remains the sole owner of
every project artifact and durable lifecycle state.

When the companion is unavailable, do not install it or block. Continue this
workflow directly: inspect discoverable facts, ask exactly one truly blocking
question with a recommended answer, wait for the response, and preserve every
other gap explicitly without invention. Start the response with the exact text
`built-in fallback` and use the labels `Question:`, `Recommended answer:`, and
`Why:`; do not claim Graph Intake ran. Generic plan or design critique remains
outside this route.

1. Classify the entry as product conversation, minimum specifications, strong prompt, or existing project at any stage.
2. Inspect project evidence and documentation conventions before choosing paths. Treat absent information as open questions, assumptions, risks, or unresolved decisions, not verified facts.
3. Use the `specification-to-delivery` scaffold when the specification needs executable lineage, approval, implementation, and repair.
4. Keep specification content in project documents, lifecycle truth in `development-graph.json`, and their binding in `specification-manifest.json`.
5. Give every material unresolved decision a stable manifest entry, dedicated decision node and artifact, and explicit affected consumers. Put named decisions in `decisions.json`; preview `scripts/specctl.py sync`, then apply only its reviewed lifecycle digests instead of manually rewriting generated artifacts.
6. Have specification verification assess interfaces, expected behavior, failure handling, and observable acceptance. When the spec gives an implementation slice enough detail, use the direct handoff (`--no-implementation-plan` for a new scaffold) and bind the slice to the spec artifacts and stakeholder approval.
7. Give unresolved build choices decision nodes with explicit affected consumers. Use a short, targeted implementation plan for work with a genuine sequencing gap. Keep each dependency, approval, and evidence binding in the graph.
8. Create the minimum viable artifact set, run `scripts/specctl.py verify`, then elaborate or repair only its reported gaps. Successful compact output replaces rereading generated artifacts.

Do not use this path for a plain PRD, architecture-only diagram, or small understood change with no need for executable lifecycle control.

## Outcome-first execution contract

The approved outcome and acceptance contract are the scope ceiling for the
current run. `development-graph.json` remains the durable DLG and may evolve as
knowledge changes, but durable DLG growth does not expand the current run. For a
schema-v2 run, the frozen projection is the execution boundary: only projected
nodes are executable, ready, claimable, or relevant to completion. Unprojected
DLG nodes remain visible backlog and cannot block the approved outcome.

Keep these operations distinct:

- A schema-v2 definition mutation records durable DLG knowledge through
  `rewrite_graph.py`; `runtime_effect: record-only` cannot enroll or invalidate
  projected work.
- `graphctl.py promote` may enroll named durable nodes only through a previewed,
  authority-bound mapping to an existing requirement or a current regression.
- `graphctl.py defer` records an unenrolled finding without changing readiness,
  projection inventory, or completion.
- `graphctl.py semantic-invalidate` reopens explicitly requested or proven
  enrolled consumers. Uncertain enrolled artifact consumers require an impact
  gate, and every repair-cycle apply remains causal-episode bounded.
- `graphctl.py migrate-outcome` previews by default. Outcome change requires explicit authority. An explicitly authorized successor-run may apply only with exact reviewed digests; it preserves source-run bytes and refuses stale bindings, active leases, or duplicate successor destinations. Same-run remains preview-only. Ordinary promotion cannot change the approved outcome.

After a required graph transaction, return to the current projected critical path. Do not recursively repair graph, specification, or governance state unless
that repair is the requested outcome or a mapped acceptance blocker. Record
adjacent, uncertain, or unrelated findings as backlog or deferrals; do not make
them completion conditions for the current run.

## Operate

1. Initialize schema-v2 outcome-governed state only from a valid definition plus
   a reviewed outcome contract and projection. Initialization without both is
   legacy schema v1.
2. Read status and current `run_version`, outcome digest, projection checksum,
   `dlg_backlog_nodes`, deferred findings, and `needs_human_episodes` before a
   transition.
3. For schema v2, claim only a `ready` node in the frozen projection; durable DLG
   backlog is never claimable. Deterministic local commands must be argv arrays;
   other executor kinds require an adapter or human.
4. Record evidence on `complete` or `fail`. Preserve full outputs in the evidence directory.
5. Require a current projection-bound approval for a destructive schema-v2 node;
   legacy schema v1 retains graph-checksum binding.
6. Use legacy `feedback` and `invalidate` only for schema v1. For schema v2, use
   a previewed semantic invalidation or current-regression promotion and satisfy
   its impact and causal-episode gates.
7. Recover invalidated, failed, or expired-lease projected work through explicit
   audited operations without enrolling backlog.
8. Verify projection lineage, outcome-bound completion, event-chain integrity,
   accepted evidence, and actual target output before claiming runtime success.

Never edit `run.json`, projection snapshots, or `events.jsonl` by hand.

## Control model context

1. Add `context_control` only to agent or hybrid nodes. Nodes without it retain V2 behavior.
2. Resolve the node contract, every required direct input, and applicable policy, authority, approval, safety, invalidation, and capability record before considering optional context.
3. Refuse executable planning without the exact adapter tokenizer. Approximate counting is preview-only and cannot invoke a model.
4. Never rank away mandatory context. Return typed `mandatory_overflow` before adapter invocation when the complete mandatory payload cannot fit.
5. Seal the ordered payload and manifest; persist payload digest rather than raw payload by default.
6. Give the capsule no generic filesystem, shell, web, graph-lineage, transcript, retrieval, or knowledge-store capability.
7. Route every additional context or action through a typed broker request, permission and authority checks, a bounded projection, and a new envelope digest.
8. Keep same-run replay separate from cross-run exact-key cache reuse. Require clean-recomputation artifact and provenance equivalence before accepting a cache record.
9. Record envelope, request, result, usage, replay/cache, invalidation, and output/evidence digests without credentials.
10. Treat the fake adapter under test support as deterministic evidence only. Core ships only the provider-neutral adapter protocol; real adapters, tokenizers, endpoints, credentials, and provider authority belong to separately operated host tooling.
11. Use read-only explanation commands for plan, envelope, reuse/cache, and
    invalidation questions. Do not expose raw payloads or create state merely
    to explain a decision.

This controls only inner invocations created through the V3 boundary. Never claim it removes information already present in the outer Codex or host-agent context.

## Evolve

1. Express a durable definition change as a versioned mutation document with
   rationale, evidence, expected base, typed operations, and the required
   schema-specific runtime fields.
2. Preview first and review candidate validity, removals, authority, causal
   episode, and exact transaction digests.
3. In schema v2, apply only a `record-only` mutation with empty invalidation
   seeds. It updates the observed DLG binding while preserving the outcome
   contract, frozen projection, runtime node inventory, node states, and
   completion.
4. Use the separate preview-first `promote`, `defer`, and
   `semantic-invalidate` request contracts for current-run effects. Never infer
   promotion from a definition rewrite.
5. Require explicit user authority and preview `migrate-outcome` for a proposed
   outcome change. Apply only an explicitly authorized successor-run with its
   exact reviewed digests; preserve source-run bytes, refuse stale bases, active
   leases, and duplicate successor destinations, and keep same-run preview-only.
   Do not apply outcome change through ordinary promotion or mutation.
6. Reject stale bases, invalid candidates, active-node conflicts, ungated causal
   repetition, and unapproved destructive topology changes.
7. Preserve the exact rollback snapshot and append-only event history, then
   return to the projected critical path.

For a preview-only handoff, include the literal lowercase marker `dry run` and state explicitly that apply was not performed.

A graph may recommend a mutation; it never silently applies one to itself or makes its backlog mandatory current work.

## Non-negotiable invariants

- Definition intent and mutable run state remain separate.
- JSON is canonical; Markdown is generated and checksum-bound.
- Shared read-only dependencies belong in `permissions.read`; list only concurrency-sensitive conflict domains in `resources`.
- Every artifact on an `approves` edge is produced by the human-gate source and declared as an input by the protected target.
- Read `canonical_sha256` and `markdown_marker` from `validate_graph.py --json`; never guess or scrape an invented Markdown marker.
- Runtime and definition mutations are locked and compare-and-swap guarded.
- Event history is append-only and hash-chained.
- Commands use `shell=False`, bounded timeouts, project-contained working directories, and minimal environments.
- Leases and idempotency keys prevent controller duplicates; external adapters fence their own side effects.
- Attempts and feedback are bounded; exhaustion requires human intervention.
- Destructive effects require explicit gates, current approval, and concrete rollback.
- A folder, process, port, zero exit, or successful submission is not proof of behavioral correctness.
- A smaller envelope is rejected if mandatory recall, authority, provenance, isolation, cache equivalence, or output fidelity regresses.
- Project artifact previews write nothing; apply and recovery are exact-base,
  actor/authority-bound, relative-path transactions.
- Core verification must pass without a concrete model provider, provider
  package, credentials, endpoint, network, or hosted forge.

## Completion gates

Write `Graph validation: PASS` only after fresh semantic validation, generated Markdown, and checksum-parity validation succeed.

For specification development, write `Specification validation: PASS` only after fresh graph validation and specification-manifest validation succeed. Deterministic validation does not substitute for the graph's semantic verification or human approval nodes.

In the final handoff, list every named decision ID separately with status and affected nodes. For reconstruction, include the exact sentence `Protected files were preserved.` and cite the byte-level or version-control evidence. State whether implementation, runtime initialization, and approval were intentionally not performed.

For schema v2, `run.status == completed` is controller-computed from projected terminal and acceptance-mapped node states; unrelated DLG backlog and deferred findings are reported separately and do not block that status. Legacy schema v1 retains whole-definition completion semantics. Before claiming overall success, perform a separate operator success-claim audit of projection/event lineage, accepted evidence, verified target behavior, authorized file scope, and rollback. Runtime status alone does not enforce those outer success conditions. Report every failed condition without implying readiness.

Run the [verification commands](README.md#verification) from the complete skill folder before claiming source readiness. Runtime helpers, tests and reusable synthetic evaluation fixtures travel together in this collection. A smaller or cheaper result cannot pass by weakening fidelity, authority, provenance, isolation, or safety.
