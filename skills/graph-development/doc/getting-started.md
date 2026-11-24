# Getting started

## Install and opt in

Install the complete `skills/graph-development` folder from a revision of the shared collection that contains it. The [README installation commands](../README.md#install) cover Codex and Claude Code. Keep scripts, templates, references, tests, examples and license together.

Invoke `$graph-development` in Codex or `/graph-development` in Claude Code. A project can also adopt the bounded [project opt-in policy](../references/project-opt-in.md). Ordinary software discussion does not authorize mandatory lifecycle artifacts.

State the approved outcome, non-goals, acceptance evidence and mutation authority before executing project work. Those values bound the run even as durable DLG knowledge grows.

## Complete a first schema-v2 run

Follow the [README quick start](../README.md#quick-start). Its bundled synthetic example creates all prerequisites: graph definition, generated Markdown, outcome contract and frozen projection. It performs a real deterministic task in a disposable project and exposes the runtime state and ledger for inspection.

Confirm that preview leaves the target unchanged; apply uses the reviewed transaction values; graph/view validation passes; the deterministic task produces its expected output; the schema-v2 run completes against its mapped acceptance; and ledger verification passes. This example verifies the local control plane. It does not establish that an outer agent discovered the skill or that an external adapter ran correctly.

## Choose a real project workflow

Use `change` for a small bounded lifecycle, `discovery-to-delivery` for discovery through delivery, `diagnosis-repair` for defect investigation, and `migration-release` for stateful release work. Use `specification-to-delivery` when documents, decisions, approval and implementation need executable lineage. See the [template catalog](../references/template-catalog.md).

Preview scaffolding before apply. Templates contain no fake success commands; bind real project-specific commands and evidence after inspecting the project. Use `--no-implementation-plan` for a direct specification handoff when implementation readiness is established. Open decisions block only their declared consumers. Use a short targeted plan for genuine sequencing gaps.

Run `specctl.py snapshot`, preview/apply `specctl.py sync` for named decision changes, and run `specctl.py verify`. The [specification workflow](../references/specification-development.md) defines the exact contracts and commands.

## Continue operating

The [architecture](architecture.md), [configuration](configuration.md) and [operations](operations.md) guides explain state ownership, runtime inputs, legal transitions and recovery. Keep private state and output in the caller's project. Completion follows the approved run projection; unrelated backlog remains outside current work.
