# Development and verification

Graph Development is one independently usable skill folder in the shared skills collection. Runtime helpers, templates, schemas, tests and reusable synthetic evaluation cases travel together. Working graphs, runtime state, private authority, transcripts, credentials, caches and generated verification output belong outside the published folder.

## Verify the skill

Resolve the skill root and choose the Linux project interpreter as described in the [README](../README.md#requirements). From any working directory:

```sh
(cd "$SKILL_DIR" && "$PYTHON" scripts/verify_distribution.py)
(cd "$SKILL_DIR" && "$PYTHON" -m unittest discover -s scripts/tests -p 'test_*.py' -v)
```

The provider-neutral V4 release gate covers graph/specification parity, portability/provider absence, templates, lifecycle transactions, migrations, context observability and frozen V3/V4 contract cases. Its Python audit-hook network guard proves its own socket-denial canary and rejects recorded socket attempts in guarded stages. It is not a general OS sandbox.

Use focused regressions for behavior changes, then the complete suite. Preserve runtime assertions, failure visibility and the distinction between deterministic fixtures and real provider/agent behavior. Do not rewrite frozen expectations merely to hide a regression.

## Verify the collection

The collection root provides `tools/validate_collection.py`, collection tests and its CI workflow. Follow its README for the pinned validator dependency and commands. Collection rules check metadata, licenses, relative resources and common privacy/portability mistakes; they supplement manual source/history review.

Collection policies are maintained centrally: [contributing](https://github.com/dansasser/skills/blob/main/CONTRIBUTING.md) and [security](https://github.com/dansasser/skills/blob/main/SECURITY.md). This skill's [Apache-2.0 license](../LICENSE) remains distinct from the collection documentation license.

## Public-readiness review

Review the complete candidate tree and proposed public history. Keep resources self-contained; verify every local documentation target and documented command. Inspect synthetic fixtures as well as prose for personal information, private records, machine-specific paths, credentials and hidden generated files. Do not import private parent history.

Copy the complete folder into an unrelated real directory, including a path with spaces. Follow the documented example, inspect output and ledger evidence, and run the suite there. Keep fresh verification results outside the published folder. Fresh agent discovery/invocation, near-miss classification and external adapter behavior remain separate checks from metadata, direct CLI execution and deterministic regression tests.
