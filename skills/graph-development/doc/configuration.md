# Configuration

Graph Development has no provider configuration, credential file, hosted endpoint or network-service requirement. Configuration is explicit project and run input.

## Project policy

Explicitly invoke `$graph-development` for a task, or adopt the bounded [project opt-in policy](../references/project-opt-in.md) in the project's applicable agent instructions. Project opt-in selects Graph Development for governed lifecycle mutations; it does not authorize every durable graph node or expand the approved outcome.

## Interpreter and paths

Use one selected Linux Python 3.12+ interpreter for helpers, tests and the example. Set `SKILL_DIR` to the skill root resolved from the loaded `SKILL.md`, `PYTHON` to that interpreter, and `PROJECT_ROOT` to the caller's project. Quote these variables. The [README](../README.md#requirements) describes runtime boundaries.

Bundled scripts, schemas, templates and references stay inside the complete skill folder. Reusable source does not embed personal or machine-specific paths. Runtime project locations are supplied explicitly; the generated local graph may record that project provenance. Transaction artifact paths remain project-relative. Symlink escapes, parent traversal, aliases and containment changes between preview and apply are rejected.

## Templates and specifications

The versioned registry is `assets/templates/registry.json`. Use `scripts/new_graph.py --help` to inspect supported templates. Templates are immutable inputs, not ready-to-run project commands; bind project-specific commands, evidence and adapters after inspection.

For `specification-to-delivery`, use `--no-implementation-plan` when the specification establishes interfaces, behavior, failure handling and observable acceptance sufficiently for direct implementation. Open build choices become decisions affecting named consumers. A short plan remains available when sequencing requires one; the legacy plan-bearing scaffold is retained.

## Outcome contract and projection

Schema-v2 runtime initialization accepts an outcome contract and execution projection together. The contract names the approved outcome, requirement/acceptance identifiers, terminal evidence, authority and impact policy. The projection freezes the permitted nodes, dependencies and requirement/acceptance mappings for that run.

Omitting both creates legacy schema-v1 whole-definition execution semantics. New bounded runs should prefer schema v2. The [runnable example](../README.md#quick-start) creates both files explicitly; see the [runtime contract](../references/runtime-contract.md) and [projection schema](../references/development-graph.projection.schema.json) for real project bindings.

## Context control

Context control is optional and provider-neutral. It binds project-relative plans, exact tokenization, sealed envelopes, typed broker requests and digest-first evidence. It does not grant generic filesystem, shell, web, transcript or graph-lineage access and cannot remove context already loaded by an outer agent. See [context control](../references/context-control.md).
