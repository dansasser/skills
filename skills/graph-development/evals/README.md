# Verification support

The bundled V3/V4 registries and runners verify deterministic control-plane
contracts against the tests in this skill folder. `outcome-first.json` and its
provider-free fixture helper preserve finite-outcome, authority, containment
and near-miss criteria. The production-ready matrix is a synthetic criteria
registry; it is not a published model-results report.

Run `python3 scripts/verify_distribution.py` from the skill root for the
provider-neutral gate. Write generated evidence into the caller's project.
Live model evaluation, credentials, host-agent launchers and historical private
results are outside this distribution. Passing deterministic cases does not
establish fresh agent activation or universal client compatibility.
