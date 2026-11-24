# Architecture

Graph Development is a single-host, project-local coordinator. It represents software delivery as explicit artifact producers, consumers, verification, approvals and bounded repair. It is not a distributed scheduler, daemon supervisor, remote-agent transport or OS sandbox.

## Planes and artifacts

| Plane | Artifacts | Ownership |
| --- | --- | --- |
| Durable lifecycle intent | `development-graph.json` and generated `development-graph.md` | The JSON definition is canonical; Markdown carries its checksum |
| Specification lineage | Project documents and `specification-manifest.json` | Content stays in documents; the manifest binds producers, consumers, decisions and approvals to graph nodes |
| Approved execution | Outcome contract and frozen projection | Scope, requirements, acceptance and authority bound one run; only projected nodes execute |
| Mutable runtime | `.development-graph/run.json`, events, evidence, projection snapshots and recovery records | Controllers perform locked, version-checked transitions; the event chain is append-only |
| Optional model context | Sealed envelopes, broker requests/results and digest-first context evidence | The provider-neutral adapter boundary controls inner invocations only |

Bundled scripts, references and template assets resolve relative to the skill folder. Project paths are supplied by the caller. All lifecycle transaction outputs remain inside that declared project; path traversal, symlink escapes and changed preview/apply containment are rejected.

## Modules

- `new_graph.py`, template assets and `template_registry.py` scaffold a lifecycle.
- `validate_graph.py`, `render_graph.py`, `validate_specifications.py` and `specctl.py` check graph/view parity and specification lineage.
- `graphctl.py`, `graph_worker.py` and `graph_core.py` manage run state, legal transitions, deterministic commands, leases, approvals, evidence and ledger integrity.
- `project_lifecycle.py`, [projectctl.py](../scripts/projectctl.py) and `schema_migrations.py` provide preview-first artifact transactions, receipts, recovery and ordered migrations.
- [rewrite_graph.py](../scripts/rewrite_graph.py) records governed definition changes; current-run promotion, deferral, semantic invalidation and outcome migration remain separate operations.
- `context_core.py`, `context_broker.py`, `context_store.py`, `context_observability.py` and [contextctl.py](../scripts/contextctl.py) implement optional bounded context planning and explanation.

See the [graph contract](../references/graph-contract.md), [runtime contract](../references/runtime-contract.md), [project lifecycle](../references/project-lifecycle.md) and [context control](../references/context-control.md).

## Execution and safety

A deterministic node runs an argument array with `shell=False`, bounded time, a project-contained working directory and a minimal environment. Its exit status, retained output and target effect supply evidence. A successful submission or a file/process existing does not prove acceptance.

Graph permissions are policy declarations. They do not prevent a trusted command from accessing files or network resources outside the project. Use an appropriate execution sandbox for untrusted code. Provider credentials, transport, tokenizers and side-effect fencing belong to separately operated adapters; the core ships no concrete provider client.

## Completion and evolution

The approved outcome is the scope ceiling. Schema-v2 completion follows projected terminal nodes and acceptance mappings. Durable backlog can grow without enrolling new current work. A definition rewrite does not imply promotion. Changing the approved outcome requires explicit authority and a successor-run transaction.

Legacy schema-v1 runs execute the whole definition and retain their compatibility operations. They must not be described as having schema-v2 projection semantics. See [operations](operations.md).

## Extension boundary

The [future direction and maturity gates](../references/future-direction.md) describe proposed seams and evidence required for broader deployment. A roadmap item does not establish distributed execution or grant authority for self-rewriting.
