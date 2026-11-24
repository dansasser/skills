# Schema history

This directory is the portable, ordered schema-migration registry for Graph
Development. `registry.json` is resolved from the executing skill root, not a
development checkout or caller working directory.

Version 0 denotes the frozen pre-registry compatibility fixtures. Version 1 is
the current graph, specification-manifest, context-successor, and runtime
contract. Every `0 -> 1` transformation is deterministic.

- Graph migration normalizes the portable project root and retains all other
  definition fields.
- Manifest migration normalizes the portable specification root and retains
  all other manifest fields.
- Context migration creates a sealed successor containing the exact predecessor
  record. It never rewrites the predecessor.
- Runtime migration is permitted only with an inactive run and a valid event
  chain. It binds the run to the migrated definition, clears checksum-bound
  approvals when the definition checksum changes, and appends one chained
  migration event.

Graph, manifest, and context migrations restore from the exact immutable source
bytes and file mode retained by the project lifecycle transaction, and restore
checks that those bytes reproduce the bound migration candidate. Runtime ledger
append is an explicit irreversible boundary recorded on the appended event;
recovery restores the complete prior runtime and ledger bytes from that
transaction instead of deleting history.

`projectctl --migration` is the only serialized CLI entrypoint. It builds an
in-process authenticated request, binds every source and candidate hash, and
commits graph, manifest, context successors, runtime, then ledger in that order.
The lifecycle layer rebuilds the migration from source while holding the
project lock. A generic lifecycle request cannot claim migration authority by
copying plan metadata.
