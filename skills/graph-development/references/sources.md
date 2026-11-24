# Sources and Research Notes

Accessed 2026-07-22. These sources support the framework-neutral synthesis in this skill; they do not make the skill dependent on any named runtime.

## Agent loops and orchestration

- [OpenAI Agents SDK: Running agents](https://openai.github.io/openai-agents-python/running_agents/) documents the model/tool/handoff loop, final-output termination, maximum turns, resumable state, and durable orchestration integrations.
- [OpenAI Agents SDK: Agent orchestration](https://openai.github.io/openai-agents-python/multi_agent/) distinguishes LLM-led orchestration from code-led orchestration and describes chaining, evaluator loops, and parallel execution.
- [OpenAI Agents SDK: Human-in-the-loop](https://openai.github.io/openai-agents-python/human_in_the_loop/) documents explicit approval interruptions, durable pause/resume, and tool-level authority boundaries.
- [Anthropic: Building Effective AI Agents](https://resources.anthropic.com/building-effective-ai-agents) surveys sequential, parallel, and evaluator-optimizer workflow patterns and recommends matching complexity to business value.

Synthesis: a loop is the right local primitive when the next action is intentionally delegated to the model. A graph is the right lifecycle primitive when dependencies, artifacts, authority, or recovery must remain explicit. A development graph can place bounded agent loops inside nodes.

## Graph workflow semantics and durability

- [LangGraph: Graph API overview](https://langchain-ai.github.io/langgraph/concepts/low_level/) defines graph state, nodes, edges, start/end semantics, cycles, checkpoint re-execution, idempotency, and graph migration concerns.
- [LangGraph reference](https://langchain-ai.github.io/langgraph/reference/) positions graphs for long-running stateful work that mixes deterministic and agentic workflows.
- [Temporal documentation](https://docs.temporal.io/) describes durable recovery after process, network, and infrastructure failures.
- [Temporal architecture](https://github.com/temporalio/temporal/blob/main/docs/architecture/README.md) separates deterministic workflow code from side-effecting activities and calls for idempotent or non-retryable activity behavior.
- [GitHub Actions workflow syntax](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax) provides a production example of dependency edges and downstream consumption of job outputs.

Synthesis: graph authoring should make state, dependency, artifacts, retries, and side effects visible. Re-execution means side effects need idempotency or explicit non-retry/rollback policy. Graph versioning and artifact invalidation are lifecycle equivalents of durable workflow state discipline.

## Development lifecycle and verification

- [NIST Secure Software Development Framework](https://csrc.nist.gov/projects/ssdf) provides outcome-based practices intended to be adapted to a project's lifecycle and risk profile rather than used as a fixed checklist.
- [NIST IR 8397: Minimum Standards for Developer Verification](https://csrc.nist.gov/pubs/ir/8397/final) recommends multiple verification techniques, including automated, structural, black-box, historical, and fuzz testing where applicable.
- [OMG BPMN](https://www.omg.org/bpmn/) describes a process notation intended to remain understandable to stakeholders while retaining executable process precision.

Synthesis: project-specific graphs should be instantiated from reusable templates, with verification chosen by risk and observable outcomes. Human-readable views and machine-readable contracts serve different audiences and must remain in parity.

## Graph data and representation

- [W3C RDF overview](https://www.w3.org/2001/sw/wiki/RDF) defines a directed labeled graph for statements about resources.
- [JSON Schema](https://json-schema.org/) defines a vocabulary for JSON consistency, validity, documentation, and interoperability.

Synthesis: a knowledge graph primarily records facts and semantic relationships; a development graph primarily coordinates state transitions, artifact flow, authority, and verification. Both use nodes and edges, but their contracts and validation questions differ.

## Design conclusions used by this skill

1. Build a reusable purpose and template library, then instantiate project-specific graphs.
2. Keep the project graph canonical in machine-readable JSON and generate the human view deterministically.
3. Use a DAG for normal scheduling and explicit bounded feedback edges for repair.
4. Put agent loops inside bounded nodes; do not let an opaque loop replace lifecycle governance.
5. Model artifacts, evidence, permissions, side effects, approval, rollback, and invalidation as first-class data.
6. Validate structure and graph semantics before reporting completion.
