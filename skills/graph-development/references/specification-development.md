# Specification Development with an Executable Graph

Use this workflow when specification work and delivery need one inspectable lifecycle covering source evidence, documents, unresolved decisions, verification, stakeholder approval, implementation, and delivery proof.

The specification documents contain product and technical meaning. `development-graph.json` is the lifecycle source of truth. `specification-manifest.json` binds each specification and decision to the graph nodes that produce, verify, approve, and consume it. An optional implementation plan records technical sequencing for the work that needs it.

## Entry modes

Start from any one of these minimum inputs:

- a **product conversation** that establishes the intended outcome, users, or problem;
- **minimum specifications** that are incomplete but concrete enough to expose what remains unknown;
- a **strong prompt** containing the product, constraints, acceptance expectations, or named decisions; or
- an **existing project at any stage**, from an empty repository to a partially implemented or mature system.

These are entry modes, not claims of completeness. Missing facts become explicit open questions, assumptions, risks, or decision nodes. Never invent a verified requirement, project fact, approval, or resolved decision to make the artifact set look complete.

## Optional conversational intake

`$graph-intake` is an optional, read-only pre-step for a product conversation
that is too loose to support a truthful first-pass scaffold. Route to it only
when the intended outcome or user context cannot be stated without guessing,
or when an ambiguity could materially change scope, constraints, or acceptance
direction.

Skip the companion for a strong prompt or adequate minimum specifications.
Adequate means Graph Development can author the minimum viable artifact set
while classifying gaps as open questions, assumptions, risks, or unresolved
decisions; it does not mean every question is resolved.

Accept only a `user-confirmed` handoff. It is conversation input only. Graph
Development remains the sole owner of project evidence classification,
specifications, stable decision identities and statuses, decision records,
manifests, canonical graphs, runtime state, approval, validation, and every
filesystem write. Recheck its claims against current project evidence. User
confirmation is not specification approval or mutation authority. Keep this
request-level intake separate from PLG and DLG state and terminology.

When the companion is unavailable, do not install it or block. Continue this
workflow directly: inspect discoverable facts, ask exactly one truly blocking
question with a recommended answer, wait for the response, and record every
other gap explicitly without invention. Start the response with the exact text
`built-in fallback` and use the labels `Question:`, `Recommended answer:`, and
`Why:`; do not claim Graph Intake ran.

Generic plan or design critique remains outside this route.

## Trigger boundary

Use the `specification-to-delivery` template when the request needs specification lineage, unresolved decisions connected to affected work, a specification approval boundary, or later invalidation and repair.

Do not use this workflow for a plain PRD with no executable lifecycle, an architecture-only diagram, or a small understood change that is clearer as the `change` template or a short checklist.

## Workflow profiles

Select one profile before elaboration. Profiles change evidence depth and the
amount of research, not truthfulness, required lineage, deterministic validity,
or approval safety.

| Profile | Use | Goal-accounted ceiling | Model-cycle ceiling | Repair rounds |
|---|---|---:|---:|---:|
| `starter` | Small greenfield project with bounded scope and few decisions | 100,000 | 30 | 1 |
| `standard` | Normal product foundation with research, decisions, and implementation-ready lineage | 300,000 | 75 | 2 |
| `assurance` | Mature reconstruction, policy-sensitive system, or unusually broad evidence set | 500,000 initially; justify increases in writing | 120 initially | 2 |

Use `standard` when the prompt does not justify another profile. Every profile
preserves explicit unknowns, material decision lineage, verification before
approval, implementation and runtime boundaries, valid graph and manifest
contracts, and honest completion reporting.

An additional material decision should normally use no more than 15,000 to
25,000 goal-accounted tokens of targeted research and integration. If it needs
more, record why the existing evidence was insufficient.

## Stopping and budget rules

Stop researching a material decision when:

1. its question and affected consumers are explicit;
2. one authoritative source supports the selected path;
3. material counterevidence or compatibility risk is recorded;
4. the decision is resolved, deferred, or explicitly open; and
5. no current acceptance criterion requires another fact.

Do not broaden research merely because more libraries, patterns, or articles
exist.

Stop specification elaboration when:

1. every in-scope requirement has a status;
2. every requirement maps to an owning module or documented external seam;
3. every requirement maps to observable acceptance and evidence;
4. every material decision has exact downstream consumers;
5. risks and implementation discoveries are classified;
6. no first-slice decision remains silently implicit; and
7. graph and manifest semantic verification pass.

Word count, document count, and graph size are not completion criteria. Run the
composite verifier once after each coherent mutation batch, repair only
reported failures, and permit no more than two model-driven repair rounds. If
the same failure remains after two rounds, record a skill or tool defect
instead of continuing ad hoc edits. Do not reload successful generated output
into context.

Budget checkpoints are:

- 50 percent: record a compact progress and evidence checkpoint;
- 75 percent: switch to completion-only work;
- 90 percent: prohibit new artifact categories and unrelated research;
- 100 percent: complete the smallest valid in-scope package and report
  uncompleted elaboration honestly.

Budget pressure never marks a goal blocked.

Static research, semantic specification review, runtime behavior, and delivery
proof are separate evidence tiers. A missing future runtime observation cannot
invalidate completed research or structurally valid specification work.

## Required artifact set

Respect the project's instructions and existing documentation conventions. The scaffold defaults to `doc/` only when no stronger project convention has been established. If another root is required, update the graph artifacts, node permissions, and manifest paths together before authoring.

| Artifact | Purpose |
|---|---|
| `doc/product-spec.md` | Product problem, actors, outcomes, scope, non-goals, and requirement status |
| `doc/constraints-and-risks.md` | Technical, operational, policy, schedule, migration, and uncertainty boundaries |
| `doc/architecture-spec.md` | System boundaries, interfaces, data ownership, behavior, and compatibility |
| `doc/acceptance-spec.md` | Observable acceptance scenarios and required evidence |
| `doc/open-questions.md` | Unknowns, assumptions, risks, and decision inventory |
| `doc/decisions/*.md` | One durable record per material cataloged decision |
| `doc/implementation-plan.md` | Optional sequencing detail for the implementation nodes that need it |
| `specification-manifest.json` | Graph-to-document and decision-to-consumer lineage |
| `development-graph.json` | Canonical lifecycle, gates, feedback, and invalidation topology |
| `development-graph.md` | Generated, checksum-bound human view |

Existing projects may already contain equivalent files under other names. Preserve and catalog them rather than rewriting them into the defaults without cause.

## Minimum-spec fast path

Create the **minimum viable artifact set before elaboration**:

1. Read this reference plus project instructions and only the project evidence needed to classify supplied facts, observed facts, and unknowns.
2. Scaffold the `specification-to-delivery` graph. Use `--no-implementation-plan` for a direct handoff or for work where only named build decisions remain. Bind resources and acceptance to the real project before execution.
3. Run `scripts/specctl.py snapshot` and retain its bounded counts, versions, and hashes instead of loading complete generated artifacts.
4. Write concise first-pass product, constraints, architecture, acceptance, and open-question documents. Add a short implementation plan for a genuine sequencing gap.
5. Put all named material decisions in one small `decisions.json` file and
   preview `scripts/specctl.py sync --profile <profile>`. Review its lifecycle
   base/plan digests, then apply that exact plan with actor and authority. Let
   the workflow create decision nodes, records, missing affected-consumer
    nodes, dependency and approval bindings, the graph-version update, and regenerated
   Markdown in one validated transaction.
6. Run `scripts/specctl.py verify`.
7. Only after both validators have run should you elaborate evidence or repair the exact reported gaps.

Do not read entire generated graphs, schemas, or validator source before the first scaffold-and-validate cycle. Use targeted JSON queries and validator errors when customization is required. Always reserve time for both validators and the final handoff.

Example decision input:

```json
{
  "schema_version": 1,
  "decisions": [
    {
      "id": "d1-helper-wire-framing",
      "title": "D1 Helper Wire Framing",
      "question": "Which helper wire framing is authoritative?",
      "status": "open",
      "source_path": "doc/decisions/d1-helper-wire-framing.md",
      "affected_nodes": ["scaffold-helper", "implement-helper"]
    }
  ]
}
```

Run the bounded workflow from the skill root:

```bash
PROJECT_ROOT="${PROJECT_ROOT:?set PROJECT_ROOT to the target project directory}"
python3 scripts/specctl.py snapshot \
  --project-root "$PROJECT_ROOT" \
  --graph development-graph.json \
  --manifest specification-manifest.json \
  --decisions decisions.json \
  --markdown development-graph.md

python3 scripts/specctl.py sync \
  --project-root "$PROJECT_ROOT" \
  --graph development-graph.json \
  --manifest specification-manifest.json \
  --decisions decisions.json \
  --markdown development-graph.md \
  --profile standard

python3 scripts/specctl.py verify \
  --project-root "$PROJECT_ROOT" \
  --graph development-graph.json \
  --manifest specification-manifest.json \
  --decisions decisions.json \
  --markdown development-graph.md
```

The `sync` command above is a zero-write preview. Apply requires `--apply`, the
returned `--expected-base-digest` and `--expected-plan-digest`, plus `--actor`
and `--authority`.

The workflow accepts explicit `open` or `deferred` decisions. It never fabricates a resolved choice. Both `open` and `deferred` remain unresolved and block their declared affected nodes; `deferred` records an accountable postponement, not permission to bypass lineage or approval. Every cataloged decision keeps a durable record of its current status. For an already resolved decision, author and verify that record from real evidence before cataloging it.

Adding previously uncataloged decisions from the already-bound source context is
a definition-only elaboration: the helper increments `graph_version` and leaves
`project.context_version` unchanged. If the source evidence changes or a
decision is resolved or revised, use the governed mutation flow with a new
context version and targeted invalidation.

The scaffold and synchronization workflow use durable project lifecycle
transactions for the graph, manifest, Markdown, starter documents, and newly
created decision records. Prior bytes, modes, and managed-inventory state are
retained. A nonterminal transaction blocks another apply until explicit,
exact-current recovery.

## Lifecycle topology

The direct handoff branches around unresolved work:

```text
required specification artifacts -> verification -> human approval
               |                                           |
               +-----------------> implementation <--------+
affected decision or plan ------> /        |
                                           +-> delivery verification -> finish
```

The exact graph remains project-specific. Remove unjustified nodes, add material branches, and bind real executors and evidence after inspecting the project.

For a direct handoff, specify the implementation node IDs in the manifest's
top-level `implementation_nodes`. Each direct node consumes the required
specification documents and the human approval as graph inputs. The validator
checks the declared inputs and edges. Specification verification evaluates
whether interfaces, expected behavior, failure handling, and observable
acceptance are sufficient for the slice before stakeholder approval.

A plan-bearing graph can list every implementation node at the top level and
list only the plan's consumers under `implementation_plan.implementation_nodes`.
Bind the plan node to the required specification documents and approval, and
connect its artifact to the listed implementation nodes. The remaining nodes
consume the specification directly. Use the governed graph update workflow
to change an existing topology. Existing manifests with only
`implementation_plan.implementation_nodes` retain the original plan-bearing
contract. The CLI produces that original scaffold when the flag is omitted.

## Authoring procedure

1. Read project instructions, manifests, existing specifications, ADRs, source boundaries, tests, CI, deployment configuration, and current delivery evidence.
2. Classify each relevant claim as observed, supplied, inferred, assumed, unresolved, or verified. Cite project evidence inside the appropriate document.
3. Scaffold with `--template specification-to-delivery`, then adapt its paths, graph nodes, artifacts, permissions, acceptance criteria, and executor bindings to the actual project.
4. Author or catalog the product, constraints, architecture, acceptance, and open-question documents. Preserve conflicts instead of silently picking one account.
5. Give every material unresolved choice its own decision identity. If the input names choices such as D1, D2, D3, and D4, preserve them as distinct decisions rather than collapsing them into prose.
6. For every cataloged decision, create a durable decision record, a dedicated graph decision node and artifact, and explicit consumers for every affected downstream node. Keep an `open` or `deferred` decision unresolved until an accountable choice and its evidence update the record.
7. Make the specification verifier consume every required document and every resolved decision. Inventory unresolved decisions through the open-question document and manifest. Verify semantic completeness, internal consistency, project evidence, acceptance testability, and remaining blockers.
8. Put stakeholder approval after specification verification. Record applicable existing approval evidence against the verified specification. Bind the approval artifact to implementation and to the plan node when present.
9. Wire direct slices to required specification artifacts. Wire a short plan only to slices with sequencing that needs to be recorded. Give affected nodes explicit decision inputs.
10. Validate the graph, render and check Markdown parity, validate the specification manifest, and report remaining warnings or blockers.

## Decision-to-implementation lineage

Every material decision entry in `specification-manifest.json` declares:

- a stable decision ID and current status;
- the project-contained decision record path;
- the graph decision node and its output artifact; and
- all graph nodes whose work can change when that decision changes.

Each affected node declares the decision artifact as an input and has an incoming `consumes` edge from the decision node. The specification verifier consumes resolved decisions. In manifests that declare top-level implementation nodes, an `open` or `deferred` decision directly blocks only its declared affected nodes. Name a shared verifier or plan as affected when the choice applies to that shared node. Legacy plan-bearing manifests retain their shared decision bindings and require the plan to consume every required specification and resolved decision. When a decision changes, use the graph mutation and invalidation contracts to update the definition and reopen affected work with current evidence.

Path enforcement is a trusted single-host project boundary, not an adversarial
filesystem sandbox. The helpers and validator use resolved-path containment to
reject existing symlink escapes and paths outside the project root. They do not
defend against a privileged actor swapping directory entries between validation
and write.

## Existing-project reconstruction

For a project already in progress:

1. inventory actual behavior and durable evidence before describing intended behavior;
2. map existing documents and decisions into the manifest;
3. mark already completed graph nodes complete only through runtime evidence, never from file presence alone;
4. expose undocumented but implementation-significant choices as decision candidates;
5. record mismatches between implementation, tests, and documents as explicit risks or repair work; and
6. preserve unrelated verified branches when a specification change invalidates downstream work.

When reviewing an immutable or protected source snapshot, do not run tools that
write caches into it. Run Python checks with `PYTHONDONTWRITEBYTECODE=1` (or
`python3 -B`) and direct all coverage, test, build, and temporary outputs outside
the protected source snapshot. Capture sorted hashes before and after the review
and treat any unexpected file, including `__pycache__` or `*.pyc`, as a failed
preservation gate.

The workflow can therefore begin before implementation, during active development, or after a mature project needs its specification lineage reconstructed.

## Validation and completion

The graph validator proves graph shape and artifact semantics. The specification validator proves the manifest's graph binding, project-contained nonempty document paths, producer/consumer lineage, verifier coverage, approval edges, decision consumers, and the binding from specification or plan to implementation.

Run both. Only print:

```text
Specification validation: PASS
```

after a fresh successful `validate_specifications.py` run against the current graph, manifest, and project root. This marker proves deterministic lineage checks, not stakeholder approval or semantic quality by itself. Keep verification evidence and approval as separate graph artifacts.

The final handoff must:

- print `Graph validation: PASS` only when current graph and Markdown parity passed;
- print `Specification validation: PASS` only when the current manifest passed;
- list every named decision ID separately with its status and affected nodes; do not compress D1, D2, D3, and D4 into a range such as “D1-D4”;
- for reconstruction work, include the exact sentence `Protected files were preserved.` and name the evidence used to prove it; and
- state the lifecycle boundary reached, including whether implementation, runtime initialization, or approval was intentionally not performed.
