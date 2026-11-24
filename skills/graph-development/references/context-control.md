# V3 Context Control

Graph Development V3 adds a provider-neutral boundary for model-executed graph
nodes. The lifecycle graph still decides which node may run. The context
controller decides which project information may enter that node's isolated
model capsule.

This subsystem controls only invocations created through its controller and
adapter protocol. It cannot remove information already present in the outer
Codex conversation or another host agent's context.

## Opt-in node binding

Legacy nodes without `context_control` keep V2 behavior. An agent or hybrid node
opts in with:

```json
{
  "context_control": {
    "version": 3,
    "plan_path": "context/implement-feature.json",
    "mode": "executable",
    "tokenizer": "deterministic-word-v1",
    "adapter": "fake-deterministic",
    "retention": "digest-only"
  }
}
```

`plan_path` is project-relative. Executable planning requires an exact tokenizer
whose identity matches the adapter. `digest-only` is the default safe retention
mode; `explicit-payload` must be a deliberate graph choice.

The deterministic word tokenizer is provider-neutral. A fake adapter exists
only under test support for contract, fault, and integration evidence. The
installable core contains the capsule protocol but no concrete model adapter,
network client, tokenizer asset, provider requirement, or live invocation
executable.

## Plan input

The project-local plan file contains:

```json
{
  "artifact_registry": {
    "artifact:accepted-spec": {
      "content": "The bounded consumer projection.",
      "source": "artifacts/accepted-spec.json",
      "requirements": ["acceptance-a"],
      "version": "sha256:record-version",
      "authorized": true,
      "fresh": true,
      "provenance_complete": true
    }
  },
  "mandatory_extras": [],
  "optional_candidates": [],
  "requirement_weights": {},
  "budget": {
    "input_limit": 100000,
    "output_reserve": 10000,
    "safety_margin": 2000
  },
  "request_policy": {
    "allowed_kinds": ["artifact"],
    "allowed_capabilities": [],
    "max_additional_tokens": 5000
  }
}
```

Every required graph input must resolve to a fresh, authorized,
provenance-complete registry projection. The controller adds the node contract,
then mandatory policy, authority, approval, safety, invalidation, and declared
capability records. A missing or invalid mandatory item fails closed.

The sealed envelope binds the current checkpoint, node-contract digest,
adapter identity, tokenizer and selector versions, every artifact version,
inclusion class and reason, omitted candidates, parent request digest, and
reconciled fixed/capability/mandatory/optional/dynamic token categories.
Changing a bound artifact version or checkpoint changes the envelope identity
even when projected text is byte-identical.

Optional candidates are eligible only after mandatory closure. The V3 selector
uses deterministic marginal weighted coverage per token, then greater absolute
coverage, lower cost, and stable artifact ID. It compares the greedy result
with the best feasible singleton. It makes no approximation-ratio claim.

## CLI

Run helpers from the skill root and pass the target project at runtime:

```bash
: "${PROJECT_ROOT:?set PROJECT_ROOT to the target project directory}"

python3 scripts/contextctl.py plan \
  --project-root "$PROJECT_ROOT" \
  --graph development-graph.json \
  --input context/implement-feature.json \
  --run-dir .development-graph \
  --node implement-feature \
  --mode executable \
  --tokenizer deterministic-word-v1
```

The command prints a bounded summary and stores a digest-first envelope record.
It does not invoke a model. Use `--envelope-output` only when an authorized
adapter needs the full sealed payload.

Validate one contract:

```bash
python3 scripts/contextctl.py verify \
  --kind envelope \
  --input "$PROJECT_ROOT/context-envelope.json"
```

Resolve one typed artifact request:

```bash
python3 scripts/contextctl.py resolve-request \
  --project-root "$PROJECT_ROOT" \
  --envelope context-envelope.json \
  --request context-request.json \
  --broker-config context/broker.json \
  --run-dir .development-graph \
  --tokenizer deterministic-word-v1
```

Inspect an exact cache key without printing retained payload:

```bash
python3 scripts/contextctl.py cache-inspect \
  --run-dir "$PROJECT_ROOT/.development-graph" \
  --components "$PROJECT_ROOT/context/result-key-components.json"
```

## Provider boundary

A host may supply an adapter implementing
`scripts/context_adapter_protocol.py`. Executable planning still requires the
adapter's exact tokenizer and matching identity, but core does not select,
install, configure, import, or invoke a provider on its own.

Completed V3 provider evaluation remains historical evidence. Concrete
adapters, tokenizer fetchers, live corpora, ablations, and blind-review tools
belong to optional development tooling outside an installed skill. Their
existence never authorizes a provider call.

## Worker integration

An external model worker can claim and seal an opt-in node in one audited
boundary:

```bash
python3 scripts/graph_worker.py \
  --graph "$PROJECT_ROOT/development-graph.json" \
  --run-dir "$PROJECT_ROOT/.development-graph" \
  --node implement-feature \
  --worker-id capsule-worker \
  --expected-run-version 7 \
  --context-plan
```

Success leaves the node `running` under the returned lease and idempotency key,
and persists the envelope record before handing control to an adapter. Planning
failure records a failed attempt. Preview planning belongs in `contextctl` and
never claims a node.

## Broker boundary

A capsule can return only a typed context or action request. The broker checks
the parent envelope, task node, request digest, request bound, read/external
scope, reachability, freshness, provenance, token budget, capability
registration, and exact-envelope approval.

Generic filesystem, shell, web, graph-lineage, transcript, retrieval, and
knowledge-store capabilities are forbidden. A typed action handler runs only
after authorization. Its raw result is hashed and measured outside the capsule;
only the declared bounded projection may enter a newly sealed envelope.

## Persistence, replay, and reuse

The project-local layout is:

```text
.development-graph/context/
  envelopes/
  requests/
  results/
  cache/
  evidence/<run-id>/<node-id>/
```

Envelope records retain manifest, budget, identities, and payload digest by
default, not raw payload content. Same-run replay restores only accepted
matching attempts. Cross-run cache reuse uses the complete result key and must
match clean recomputation artifact and provenance digests. Any component change
is a miss. Recomputed identical output enables early cutoff.

Context records use a project-local advisory lock plus atomic replacement.
Broker request identities are restored from durable decision records, so a new
CLI process cannot reset the parent envelope's request bound or replay an
already counted request. Approved typed actions are idempotent per exact broker
decision within an executor instance.

## Authorization boundary

D5 remains a historical V3 evaluation decision. It does not bind the portable
core to a provider or authorize a provider call in another project or session.
Never treat model availability, process existence, or port availability as
conformance; require output-level evidence from separately authorized external
tooling.

## Deferred selection features

Compaction and semantic retrieval remain disabled in this core. Optional selection uses the deterministic evidence and priority contract described above; generic retrieval is not an implicit capability. Provider-backed compaction would need a separately specified, authorized adapter and fresh fidelity evidence.
