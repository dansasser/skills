# Graph Development

Build, validate and operate executable software-development lifecycle graphs. Graph Development combines an opt-in agent workflow with a Python control plane for specifications, decisions, approvals, implementation, verification and recovery. The approved outcome bounds each run while the durable graph can retain unrelated backlog.

Use it when a project needs inspectable artifact lineage, durable state and controlled change across more than a checklist. For a plain PRD, architecture-only diagram, knowledge graph or small understood change without project opt-in, use a narrower workflow.

## Requirements

- Linux and Python 3.12 or newer. The current verification baseline is Python 3.12; this does not certify every newer Python release.
- Python's standard library, POSIX file locking, process groups and signals. Native Windows and macOS execution have not been verified.
- An agent that can load the complete skill folder and run local Python commands. Codex and Claude Code installation/invocation conventions are documented below.
- A target project directory writable within the user's authorized scope.

No third-party Python package, provider account, credentials, endpoint, hosted forge, background daemon or network service is a core runtime dependency. Installing from the remote collection requires the installer's normal Git/network access. Optional model adapters and tokenizers are separately operated host tooling.

The local command worker runs trusted argv commands with the user's permissions. Graph permissions and resource declarations are policy contracts; they are not an OS sandbox. Use an appropriate sandbox for untrusted commands.

## Install

Install from a collection revision containing `skills/graph-development`. Use the same Skills CLI and independent folder-copy convention as the other skills in `dansasser/skills`.

With Node.js 22.20 or newer, Git and network access:

```sh
# Codex project installation
npx skills@1.9.0 add dansasser/skills --skill graph-development --agent codex --copy

# Claude Code project installation
npx skills@1.9.0 add dansasser/skills --skill graph-development --agent claude-code --copy
```

For manual installation, clone the shared collection outside the agent's discovery directory:

```sh
git clone https://github.com/dansasser/skills.git
```

Copy the complete `skills/graph-development` folder from that checkout into the agent's supported location. Preserve scripts, references, templates, examples, verification support and license together. Installing only `SKILL.md` omits required resources; placing the whole collection inside a single skill directory creates the wrong layout.

| Agent | Project location | User location | Explicit invocation |
| --- | --- | --- | --- |
| Codex | `.agents/skills/graph-development/` | `~/.agents/skills/graph-development/` | `$graph-development` |
| Claude Code | `.claude/skills/graph-development/` | `~/.claude/skills/graph-development/` | `/graph-development` |

Start a fresh session and invoke the skill explicitly, or adopt the bounded [project opt-in policy](references/project-opt-in.md). The [Codex metadata](https://learn.chatgpt.com/docs/build-skills) sets `allow_implicit_invocation: false` to disable implicit invocation in Codex. Claude Code may load skills automatically under its own [invocation controls](https://code.claude.com/docs/en/skills#control-who-invokes-a-skill); the OpenAI metadata does not configure that host policy. This portable skill's instructions require explicit invocation or project opt-in, and do not impose governance on every software project.

## Runtime and paths

Resolve `SKILL_DIR` from the loaded `SKILL.md`, and choose one Linux Python 3.12+ interpreter as `PYTHON`. Quote both. Keep authored graphs, runtime state and generated output in the caller's project.

For example, after a Codex project copy, while in that project:

```sh
SKILL_DIR="$PWD/.agents/skills/graph-development"
PYTHON=python3
"$PYTHON" --version
```

Confirm the interpreter meets the requirements; select the project's interpreter instead if needed. A virtual environment is optional because the core has no third-party dependencies. When using an interpreter path, resolve it in the caller's workspace. The commands below work without changing into the skill directory.

## Quick start

The [bundled example](assets/examples/run_example.py) creates a synthetic one-node lifecycle that writes an expected result file. It supplies every schema-v2 prerequisite: canonical graph, checksum-bound Markdown, outcome contract and frozen execution projection. It is a control-plane demonstration, not a scaffold of the user's real implementation.

Create a new disposable target outside the installed skill:

```sh
mkdir -p workspace/graph-example
PROJECT_ROOT="$PWD/workspace/graph-example"
```

Preview the exact artifact transaction. Preview writes nothing in the project:

```sh
"$PYTHON" "$SKILL_DIR/assets/examples/run_example.py" \
  --project-root "$PROJECT_ROOT"
```

Review `changed`, `preserved`, `blocked`, `conflicts`, `base_digest` and `plan_digest`. Set the two digest variables to the exact values from that reviewed preview, then apply:

```sh
BASE_DIGEST=the_reviewed_base_digest
PLAN_DIGEST=the_reviewed_plan_digest

"$PYTHON" "$SKILL_DIR/assets/examples/run_example.py" \
  --project-root "$PROJECT_ROOT" \
  --apply \
  --expected-base-digest "$BASE_DIGEST" \
  --expected-plan-digest "$PLAN_DIGEST" \
  --actor example-operator \
  --authority local:requested-example
```

Apply creates `development-graph.json`, `development-graph.md`, `outcome-contract.json` and `development-graph.projection.json`, plus transaction evidence. It does not initialize a run or execute the task.

Validate graph/view parity and initialize the bounded schema-v2 run:

```sh
"$PYTHON" "$SKILL_DIR/scripts/validate_graph.py" \
  "$PROJECT_ROOT/development-graph.json" \
  --markdown "$PROJECT_ROOT/development-graph.md" --json

"$PYTHON" "$SKILL_DIR/scripts/graphctl.py" init \
  --graph "$PROJECT_ROOT/development-graph.json" \
  --run-dir "$PROJECT_ROOT/.development-graph" \
  --contract "$PROJECT_ROOT/outcome-contract.json" \
  --projection "$PROJECT_ROOT/development-graph.projection.json" \
  --actor example-operator
```

Execute its ready deterministic node with the initial run version. For later operations, read the current version from `status`; a stale value is rejected.

```sh
"$PYTHON" "$SKILL_DIR/scripts/graph_worker.py" \
  --graph "$PROJECT_ROOT/development-graph.json" \
  --run-dir "$PROJECT_ROOT/.development-graph" \
  --node write-result \
  --worker-id example-worker \
  --expected-run-version 1
```

Inspect the completed run, output and event chain:

```sh
"$PYTHON" "$SKILL_DIR/scripts/graphctl.py" status \
  --graph "$PROJECT_ROOT/development-graph.json" \
  --run-dir "$PROJECT_ROOT/.development-graph"

"$PYTHON" "$SKILL_DIR/scripts/graphctl.py" verify-ledger \
  --graph "$PROJECT_ROOT/development-graph.json" \
  --run-dir "$PROJECT_ROOT/.development-graph"

cat "$PROJECT_ROOT/result.txt"
```

The expected result is `Graph Development example complete` followed by a newline, a verified `write-result` node, a completed schema-v2 run and successful ledger verification. The task refuses to overwrite an existing result. Keep its output and transaction/runtime evidence together when reviewing or diagnosing the run.

These observations verify the local example. They do not establish fresh outer-agent activation, a real project's acceptance or external adapter behavior.

## Use with a real project

1. Invoke the skill or adopt project opt-in. Establish the project, applicable instructions, approved outcome, non-goals, acceptance and mutation authority.
2. Preview the smallest suitable lifecycle template, bind real commands/evidence/adapters, apply only reviewed digests, and validate the canonical graph plus generated view.
3. For schema v2, review the outcome contract and freeze the execution projection. Only projected nodes execute. An outcome change requires explicit authority and the successor-run path.
4. Run the projected critical path through legal controller transitions. Verify actual output and acceptance evidence; process existence, submission and zero exit alone are insufficient.
5. Record unrelated discoveries as durable backlog or deferrals. Graph recording never promotes work or changes the current run's completion boundary.
6. Complete the approved outcome and preserve its evidence, ledger, snapshots and recovery records.

Templates are `change`, `discovery-to-delivery`, `diagnosis-repair`, `migration-release` and `specification-to-delivery`; see the [template catalog](references/template-catalog.md).

### Specifications and optional implementation plans

Use specification-to-delivery when product/technical documents, unresolved decisions, approval and implementation need executable lineage. Preview a direct handoff with:

```sh
"$PYTHON" "$SKILL_DIR/scripts/new_graph.py" \
  --project-root "$PROJECT_ROOT" \
  --output-dir "$PROJECT_ROOT" \
  --graph-id product-delivery \
  --title "Product delivery" \
  --template specification-to-delivery \
  --no-implementation-plan \
  --context-version local:reviewed-input
```

Use `--no-implementation-plan` when the specification provides interfaces, behavior, failure handling and observable acceptance sufficiently for implementation. Unresolved build choices become dedicated decisions blocking only their named consumers. Use a short targeted plan for genuine sequencing gaps; omitting the flag retains the legacy plan-bearing scaffold.

`specctl.py snapshot`, preview/apply `specctl.py sync`, and `specctl.py verify` provide bounded document/decision synchronization and composite validation. A structurally valid scaffold is not semantic approval. See [specification development](references/specification-development.md) for exact artifact and authority contracts.

## Architecture and documentation

| Concern | Guide |
| --- | --- |
| Definition, specification, execution, runtime and context ownership | [Architecture](doc/architecture.md) |
| First use and workflow choice | [Getting started](doc/getting-started.md) |
| Opt-in, paths, templates, outcome and context inputs | [Configuration](doc/configuration.md) |
| Status, legal transitions, evolution and recovery | [Operations](doc/operations.md) |
| Tests, collection checks and public-content review | [Development](doc/development.md) |

Deep contracts: [graph](references/graph-contract.md), [runtime](references/runtime-contract.md), [validation](references/validation-rules.md), [project lifecycle](references/project-lifecycle.md), [mutations](references/mutation-contract.md), [schema migrations](references/schema-migrations.md), and [context control](references/context-control.md).

The runtime is a single-host coordinator and provider-neutral adapter contract. It does not provide a distributed cluster, remote-agent transport, daemon supervisor, secrets manager or OS sandbox. Context controls govern inner invocations created through their boundary and cannot remove information already present in an outer agent.

## Verification

Run the provider-neutral distribution gate and complete existing regression suite with the selected interpreter, from the caller's workspace:

```sh
"$PYTHON" "$SKILL_DIR/scripts/verify_distribution.py"
"$PYTHON" -m unittest discover -s "$SKILL_DIR/scripts/tests" -p 'test_*.py' -v
```

The gate covers graph/specification parity, relocation/provider absence, templates, lifecycle transactions, migrations, context observability and frozen V3/V4 cases. Each guarded stage must load its Python audit-hook socket guard; a controlled canary proves denial and recorded network attempts fail the gate. This is bounded verification, not a general sandbox or universal proof of network isolation.

Verification targets Linux/Python 3.12. Run both commands for the exact skill revision being reviewed and retain their actual output; a historical test count is not a current result. Other runtime combinations and external agents require their own verification.

Metadata/file validation, deterministic tests and direct example execution are separate from agent discovery, invocation/near-miss behavior and real project acceptance. Frozen synthetic cases specify expected contract behavior; they do not establish provider performance or every supported client workflow. See [development](doc/development.md).

## Troubleshooting and recovery

| Symptom | Action |
| --- | --- |
| Preview reports conflicts or a stale base/plan | Preserve the files, inspect the difference and make a fresh preview; do not force overwrite |
| Import fails on locking/process primitives | Use a Linux execution environment meeting the requirements; native Windows/macOS are unverified |
| Schema-v2 initialization cannot find contract/projection | Create and review both files; the bundled quick start supplies them, ordinary topology scaffolding alone does not |
| Transition rejects a run version or lease | Inspect current status; use the current version or explicit recovery for an expired lease |
| Graph checksum or ledger verification fails | Stop transitions, preserve state/evidence and use governed rewrite or trusted-state recovery |
| Lifecycle transaction is nonterminal | Use the documented transaction recovery with exact current digest; retain prior bytes and receipts |
| Example result already exists | Inspect its run/output; choose a new disposable target for a fresh example |

Never edit `run.json`, immutable projections or old ledger events by hand. A schema-v2 definition record preserves the approved outcome, projection and node inventory. Promotion, deferral, semantic invalidation and outcome migration have separate preview-first contracts. See [operations](doc/operations.md) and [project lifecycle](references/project-lifecycle.md).

## License

Published by **Dan Sasser** under the [Apache License 2.0](LICENSE), SPDX identifier `Apache-2.0`. Copyright 2026 Dan Sasser. The license covers this folder's code, instructions, documentation and bundled examples. Third-party dependencies and referenced works retain their own licenses.
