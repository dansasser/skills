# Development Graph Template Catalog

Templates are reusable topology patterns, not finished project graphs. Instantiate only the nodes and edges justified by project evidence, then replace every placeholder with project-specific artifacts, commands, permissions, and acceptance criteria.

## Selection rule

Start with the smallest topology that exposes the real dependencies and gates:

| Situation | Start with | Add when needed |
|---|---|---|
| Small, understood change | `change` | `feedback` for bounded repair |
| New feature with uncertain design | `discovery-to-delivery` | `decision`, `human-gate` |
| Product or project needing specification lineage through delivery | `specification-to-delivery` | project-specific decision and implementation branches |
| Risky migration or release | `migration-release` | `rollback`, `observation` |
| Defect with unknown cause | `diagnosis-repair` | `decision`, `feedback` |
| Many independent components | Any base template | `parallel-fanout-fanin` |

Do not create a graph for a task that is clearer as a short linear checklist.

## Template: change

Purpose: a bounded implementation whose requirements and architecture are already understood.

```text
inspect -> specify -> implement -> verify -> finish
```

Required artifacts:

- `inspect`: repository evidence and applicable instructions
- `specify`: acceptance contract
- `implement`: changed files or patch
- `verify`: fresh test, lint, build, or probe evidence
- `finish`: handoff or release record

Use a `feedback` edge from `verify` to `implement` only with a traversal limit and explicit exit condition.

## Template: discovery-to-delivery

Purpose: a project-specific feature where the correct implementation path must be discovered.

```text
context -> constraints -> design decision -> plan -> implement -> verify -> review -> deliver
```

Best-practice gates:

- Put a `decision` node where competing approaches materially change cost, risk, or interfaces.
- Require the decision record as an input to implementation.
- Separate verification evidence from review judgment.
- Add a human gate before an irreversible external release.

## Template: diagnosis-repair

Purpose: a failure whose cause is not yet established.

```text
reproduce -> collect evidence -> test hypotheses -> diagnosis gate -> repair -> regression verify
                               ^                         |
                               +------ bounded feedback--+
```

Rules:

- Diagnosis and repair are different nodes with different outputs.
- The diagnosis node must emit evidence, not merely a theory.
- A failed regression check invalidates the repair output and returns to implementation through a bounded feedback edge.
- Project instructions may require multiple independent confirmations before the diagnosis gate can pass.

## Template: specification-to-delivery

Purpose: develop or reconstruct a product and technical specification set whose documents, decisions, approval, implementation, and delivery evidence remain linked.

```text
specification artifacts -> spec verification -> specification approval
        -> implementation -> delivery verification -> finish
```

Use it when starting from a product conversation, minimum specifications, a strong prompt, or an existing project at any stage and the request needs specification lineage or executable delivery control.

Use `--no-implementation-plan` for the direct scaffold. Its implementation node
consumes required specification artifacts and approval. Add decision nodes for
named choices and connect them to the affected work. For sequencing gaps, bind
a targeted plan to the implementation nodes that need it. The default CLI
scaffold retains the legacy plan path.

Select a workflow profile before elaboration:

- `starter` for a small bounded greenfield project;
- `standard` for a normal product foundation and as the default; or
- `assurance` for mature reconstruction, policy-sensitive work, or unusually
  broad evidence.

Profiles change research and evidence depth, not the required graph, manifest,
decision, verification, or approval contracts.

Rules:

- Keep the documents as specification content and the graph as lifecycle control; do not duplicate the graph schedule in prose.
- Preserve incomplete information as assumptions, risks, open questions, or unresolved decision nodes.
- Every material cataloged decision has a durable record, dedicated graph node and artifact, and explicit affected consumers; unresolved decision nodes continue to block those consumers.
- Use `scripts/specctl.py snapshot`, `scripts/specctl.py sync`, and
  `scripts/specctl.py verify` as the default bounded workflow. The standalone
  decision and validator helpers remain compatibility and diagnostic tools.
- `new_graph.py` and `specctl.py sync` preview by default. Apply only the exact
  reviewed lifecycle base/plan digests with actor and authority; no template
  authorizes overwrite of different existing content.
- Specification verification consumes every required document and resolved decision before the human approval boundary.
- A plan, when present, consumes the required specification artifacts and approval. The implementation nodes that need it consume its artifact.
- Use bounded feedback for specification repair and delivery repair.
- Do not select this template for a plain PRD, architecture-only diagram, or small understood change.

## Template: migration-release

Purpose: a stateful, externally visible, or difficult-to-reverse change.

```text
inventory -> compatibility -> backup/rollback proof -> migrate dry run -> approval -> migrate -> canary -> observe -> close
```

Rules:

- Model the dry run, approval, operation, and observation as separate nodes.
- Destructive operations require a human-gate predecessor, concrete rollback, and approval metadata.
- A process, port, deployment job, or command exit code alone is not a canary. The canary must prove correct output or target-system side effects.
- Keep rollback artifacts addressable after successful completion.

## Modifier: parallel-fanout-fanin

Purpose: independent work can proceed concurrently and later converge.

```text
prepare -> [work-a, work-b, work-c] -> integration verify
```

Apply only when:

- branches do not use the same concurrency-sensitive resource without coordination;
- each branch has distinct outputs;
- the fan-in node explicitly consumes all required artifacts;
- failure or cancellation semantics are stated.

Add `conflicts` edges between branches that cannot safely overlap. `conflicts` documents exclusion; it does not create a scheduling dependency.

Ordinary shared read-only sources belong in `permissions.read` and do not require `conflicts`. Put a file area, interface, environment, or capacity domain in `resources` only when overlapping execution would be unsafe or intentionally serialized.

## Modifier: decision

Purpose: select among alternatives using explicit criteria.

Minimum output:

- alternatives considered;
- decision criteria;
- evidence used;
- chosen option;
- consequences and revisit trigger.

The chosen decision artifact must be consumed by affected downstream nodes.

## Modifier: human-gate

Purpose: pause before a judgment or authority boundary that should not be delegated.

Use for:

- destructive or difficult-to-reverse effects;
- external publication, sending, spending, or production changes;
- material scope expansion;
- acceptance where only a stakeholder can judge success.

The gate must name the reviewer role, the evidence presented, approve/reject outcomes, and what rejection invalidates. It must also produce a named approval artifact. Every protected target declares that artifact as an input, and each `approves` edge carries the same artifact identifier.

## Modifier: bounded feedback

Purpose: repair work after a verification or observation failure.

Every feedback edge requires:

- a positive `max_traversals` limit;
- an objective `exit_condition`;
- a destination that can actually change the failed artifact;
- an escalation path when the limit is exhausted.

Never model “repeat until good” as an unbounded cycle.

## Modifier: rollback and observation

Purpose: protect changes whose immediate command result cannot establish correctness.

Split the concern into:

1. rollback preparation and proof;
2. the operation itself;
3. an output-level canary;
4. an observation window or target-system verification;
5. closeout or rollback decision.

This prevents an alive process or successful command from being mistaken for a working system.
