# Context observability

Graph Development V4 explains graph-owned context decisions without invoking a
model or exposing the sealed payload.

## Reports

`scripts/context_observability.py` produces schema-validated explanations for:

- mandatory, selected, omitted, blocked, and overflow plan inputs;
- sealed envelope identity and digests;
- same-run replay and cross-run exact-key cache decisions; and
- affected and preserved nodes after artifact invalidation.

Reason codes come from a closed vocabulary shared by the planner and report
schema. Reports retain only the identifiers, relative source references,
digests, token/byte evidence, and decision reasons needed to reproduce the
outcome. Raw payloads, result bodies, credentials, endpoints, and absolute paths
are rejected.

## Read-only commands

The following `scripts/contextctl.py` operations are read-only:

- `explain-plan`
- `explain-envelope`
- `explain-reuse`
- `explain-invalidation`

Explanation commands open context storage with creation disabled. A missing run,
envelope, replay, or cache record is reported without creating directories or
placeholder state. No explanation path imports a concrete provider adapter or
makes a provider/network call.

Use these reports when a node is blocked, an optional item was omitted, a budget
overflowed, reuse was accepted/refused, or invalidation affected an unexpected
branch. The explanation is evidence about controller behavior, not permission
to widen the context or invoke a model.
