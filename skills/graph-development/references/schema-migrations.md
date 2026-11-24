# Schema migrations

Graph Development V4 provides deterministic, ordered migrations for the graph,
specification manifest, sealed context history, legacy runtime binding, and
event ledger. The portable registry is
`references/schema-history/registry.json`.

Registry schema migration is distinct from outcome migration. A legacy
schema-v1 run stays readable with whole-graph semantics until an explicit
conversion. Convert it only with a reviewed schema-version 2 descriptor passed
to `scripts/projectctl.py --migration`; preview before apply. The descriptor
binds the exact graph, manifest, context, run, ledger, and evidence sources; the
paired outcome contract and projection; the projection output; actor; and
authority. Conversion refuses active or leased nodes, requires the canonical run
and ledger paths, preserves the exact legacy ledger prefix, and appends one
hash-chained schema boundary event. Apply revalidates every source binding under
the lifecycle lock. Any write failure restores the exact source bytes and modes
and removes a newly created projection.

For an existing schema-v2 run, `graphctl.py migrate-outcome` previews an
explicitly authorized proposed contract/projection. An explicitly authorized
successor-run may then apply only with exact reviewed digests, preserving
source-run bytes and refusing stale bindings, active leases, or a duplicate
successor destination; same-run migration remains preview-only.

## Planning

`scripts/projectctl.py --migration` loads a descriptor whose paths are relative
to the target project. Planning:

1. validates the complete source graph, manifest, runtime, context history, and
   ledger;
2. migrates the graph candidate;
3. migrates and validates the manifest against that graph;
4. creates successor context records while preserving sealed predecessors;
5. rebinds runtime state last and clears checksum-bound approvals when graph
   semantics changed;
6. appends one hash-chained migration event;
7. validates the complete candidate, ordered steps, and receipt; and
8. produces lifecycle artifacts without writing them.

Unknown future versions, missing migration steps, active or leased runtime
nodes, invalid event chains, cross-run ledger records, and stale source digests
fail before apply.

## Apply and recovery

Apply uses the project lifecycle base/plan digest, actor, and authority contract.
The reviewed migration phase order is retained in the transaction manifest.
Prior bytes and file modes are retained for recovery.

Reversible boundaries must restore the exact source bytes and mode, including
formatting and key order. An appended event is an explicit irreversible history
boundary: recovery can restore the prior file bytes, but a successful migration
receipt never claims that append-only history was semantically erased.

The V4 registry intentionally contains only migrations backed by frozen
fixtures. Adding a future schema version requires a consecutive registry step,
source/candidate fixtures, round-trip evidence where reversible, and a new
frozen acceptance case.
