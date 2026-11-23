# Provider-neutral V4 verification

`cases.json` contains frozen deterministic criteria and test bindings. `runner.py` executes those bindings against the bundled tests without a model provider, credentials or network service. Expected results are criteria, not a claim about every agent or platform.

Run from the skill root, writing optional output into your project rather than this folder:

```sh
python3 evals/context-control-v4/runner.py
```

The full `scripts/verify_distribution.py` command also runs these cases with a Python socket audit guard. Fresh provider-backed behavior, discovery and trigger classification are separate checks.
