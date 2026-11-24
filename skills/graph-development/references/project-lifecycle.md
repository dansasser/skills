# Project lifecycle transactions

Graph Development V4 writes project artifact groups through a preview-first,
project-local transaction. The same contract governs scaffolding, specification
synchronization, graph mutation, schema-v2 run promotion, and schema migration.

## Review contract

Preview is the default and performs no project write. It reports:

- exact base, plan, and candidate SHA-256 digests;
- changed, preserved, invalidated, blocked, and conflicting relative paths;
- typed errors; and
- the operation being reviewed.

The base binds each target's existence, content digest, and mode. Apply requires
the reviewed base digest, reviewed plan digest, actor, and authority reference.
Any drift is rejected under the lifecycle lock.

All serialized artifact paths are relative to the target project. Absolute
paths, parent traversal, home shortcuts, drive paths, backslashes, NULs, and
symlink escapes are invalid.

## Operations

- `bootstrap` creates missing artifacts, preserves identical artifacts, and
  reports different existing content as a conflict.
- `update` changes only artifacts already recorded in the project-local managed
  inventory at the reviewed hash. Unmanaged existing files are never adopted.
- `graph-mutation` commits the graph, generated Markdown, runtime state, event
  ledger, and rollback snapshot as one reviewed artifact group. Schema-v2
  mutation is record-only and does not write a projection.
- `run-promotion` commits exactly one new immutable projection snapshot plus
  existing `run.json` and `events.jsonl` as one recoverable artifact group; it
  cannot mutate the durable graph definition.
- `migration` commits an authenticated, validated migration bundle in its
  declared phase order.

`scripts/new_graph.py` and `scripts/specctl.py sync` expose the lifecycle
contract directly. `scripts/projectctl.py` accepts a portable request document
for lower-level bootstrap/update work and a validated migration descriptor for
schema evolution.

## Durable evidence and recovery

Transactions retain a manifest, prior bytes, prior modes, managed-inventory
snapshot, and receipt below the target project's lifecycle control directory.
States are `prepared`, `committing`, `committed`, `rolled-back`, and
`recovery-required`.

A minimal `prepared` manifest is persisted immediately after the transaction
directory is created and before prior-state snapshots are collected. A
transaction directory with a missing, malformed, or nonterminal manifest is
treated as nonterminal and blocks later work rather than being ignored.
Recovery is explicit and requires the transaction identifier, exact current
digest, actor, and authority. It restores both managed artifacts and their
inventory. Do not delete or edit transaction evidence to bypass recovery.

The lifecycle lock coordinates Graph Development writers. It cannot grant
authority to unrelated processes, so apply also re-resolves every target and
rejects a path whose containment changed after preview.
