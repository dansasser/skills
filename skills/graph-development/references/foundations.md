# Graph Development Foundations

## Operational definition

A development graph is a versioned, executable, and queryable model of a software-development lifecycle. Nodes represent bounded work, decisions, verification, approvals, operations, or observations. Typed edges represent forward control, artifact flow, approval, invalidation, correction, or resource conflict.

The graph must answer operational questions:

- What is ready, blocked, invalidated, or verified?
- Which work can run in parallel without conflict?
- Which artifacts and evidence enable downstream work?
- What becomes stale when a contract changes?
- Where should failed verification route rework?
- What requires deterministic or human approval?

A diagram that cannot answer these questions is documentation, not a development graph.

## Agent loop versus development graph

An agent loop adapts locally:

`observe → decide → act → inspect feedback → repeat or stop`

A development graph governs globally:

`enabled nodes → bounded execution → evidence → state transition → downstream enablement or targeted feedback`

The recommended architecture is **graph outside, bounded loops inside**:

- The graph owns dependencies, state, budgets, permissions, evidence, checkpoints, approval, invalidation, and termination.
- A node may invoke deterministic code, one agent loop, a human, or a nested subgraph.
- Deterministic operations remain deterministic. Do not use an LLM for schema validation, compilation, test execution, graph traversal, or checksum comparison when ordinary code is sufficient.

Agents are not necessarily nodes. A node is a lifecycle responsibility. One agent may execute several nodes, several workers may contribute to one node, and many nodes should contain no LLM.

## Development graph versus knowledge graph

A knowledge graph represents what is known: entities, facts, documents, concepts, and semantic relationships. A development graph governs what happens: work, artifacts, prerequisites, verification, state transitions, and recovery.

Knowledge graphs are typically queried. Development graphs are scheduled, gated, invalidated, and executed. A project may link them, but a request for people/topic/document relationships does not activate this skill.

## Four layers

Use four linked layers rather than one enormous graph:

1. **Purpose graph:** stable outcome and universal lifecycle invariants.
2. **Template subgraphs:** reusable feature, migration, security, verification, release, and observation patterns.
3. **Project graph:** project-specific nodes, artifacts, dependencies, gates, and exceptions composed from templates.
4. **Run/evidence graph:** executions, artifact versions, test results, approvals, observations, and failures. Version one of this skill defines the project graph; a later runtime may maintain run history.

## Core principles

### Standardize invariants, specialize projects

Templates supply vocabulary and safe sequencing. They do not decide the project's actual module boundaries, deployment constraints, acceptance tests, or approval owners. Preserve project-specific evidence.

### Evidence controls state

An output existing is not equivalent to verification. `verified` means declared acceptance evidence has been produced and accepted by the required deterministic or human authority.

### Edge semantics are executable

Use edge types that change readiness, artifact flow, invalidation, or concurrency. Avoid generic `connected_to` relationships.

### Localize failure

A failed verifier should route to the responsible producer through a bounded feedback edge. Do not restart the whole graph or invalidate unrelated verified branches.

### Control complexity

Model meaningful responsibilities, not every command. Nested detail belongs inside a node or a deliberately selected subgraph. Add graph complexity only when it improves scheduling, safety, recovery, or traceability.

### Preserve uncertainty

Unknown dependencies and unresolved decisions are not successful nodes. Represent them as assumptions, risks, decision nodes, or `needs_human` state.

## When a graph is warranted

Use a graph when work spans sessions, services, repositories, environments, parallel workers, approval boundaries, reversible migrations, or evidence-producing stages. A single bounded coding task with obvious dependencies may be better served by a normal agent loop and test cycle.
