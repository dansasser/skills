# Painting policies and configuration

Mesh Painting first renders an editable Python scene, then paints that rendered target with brushstrokes. `--stage paint` rebuilds and renders the scene before painting; it does not load an earlier PNG as its input. Use [the scene API](scene-api.md) for geometry, materials, camera, light and region controls.

The default painting policy is **`legacy-seven`**. **`adaptive-v1` is opt-in**. Both run inside the selected [resource level](limits.md), and neither retries at a higher level automatically.

Commands below run from your project workspace. `PYTHON` is your selected interpreter with the required dependencies; `SKILL_DIR` is the resolved directory of this skill. Keep your scene, configuration and outputs in the project rather than inside the installed skill.

```bash
# Default compatibility policy.
"$PYTHON" "$SKILL_DIR/scripts/run.py" scene.py --out out/painting --stage paint

# Image-dependent refinement, explicitly selected.
"$PYTHON" "$SKILL_DIR/scripts/run.py" scene.py --out out/adaptive --stage paint \
  --paint-policy adaptive-v1 --finish photographic
```

## Choose a policy

| Policy | Behavior | Completion |
| --- | --- | --- |
| `legacy-seven` | Seven ordered coarse-to-fine passes. Later passes place strokes using image error, but their counts and the pass count are fixed by the preset and effective working width. | `preset_complete` after the full preset and output publication. |
| `adaptive-v1` | Chooses region, brush tier and subsequent work from measured residual, coverage and improvement. Smooth and detailed targets can use different amounts of work. | `quality_target` or `diminishing_returns`; a budget stop is reported separately. |

Seven passes identify the compatibility preset, not a universal requirement of mesh painting. Adaptive batches are work units, not seven fixed brushstroke layers. More work does not guarantee a better picture: geometry, shading, resolution, represented detail and the chosen finish still constrain the result.

### Compatibility policy

`legacy-seven` preserves the preset's stroke mathematics and seeded draw order for valid inputs. Input, rendering and output-integrity fixes still apply. It rejects `--finish`, `--paint-config`, `--max-strokes` and `--max-passes`; select `adaptive-v1` to use those controls. Resource-level selection and lowering time/memory overrides remain available.

Its allocation follows effective working width, with `S = working_width / 1600` and count scaling proportional to `(S / 2.4)²`, subject to per-pass integer and batch rounding. The two finest brush tiers retain their pixel sizes; earlier tiers scale their sizes. At a 3840 × 2400 working canvas, the preset plans 2,996,000 strokes. This is a specific preset/dimension result, not the count for every scene size. Equal-area portrait and landscape canvases can receive different legacy allocations.

### Adaptive policy

Adaptive painting uses compact masks for the regions actually present in the render. It establishes coverage, refines through geometric brush tiers and combines residual-guided placement with uniform coverage. Region scheduling includes work for under-refined small regions. Scene `stroke_angle` values guide areas without coherent image structure; water strokes retain a predominantly horizontal direction.

The policy evaluates against one fixed rendered reference. Its weighted error combines RGB residual, low-frequency colour residual and edge residual. Quality completion also checks regional error, regional coverage and low-frequency error, with an allowance tied to target texture. The evaluation grid is bounded by `evaluation_pixels`; small regions missed by that grid have representative samples.

Stop metrics are measured **before** the final relief and grain treatment. They are algorithmic stopping criteria, not a photorealism score or a substitute for viewing the final image and every emitted tile.

## Painting CLI options

| Option | Default | Contract |
| --- | --- | --- |
| `--paint-policy legacy-seven\|adaptive-v1` | `legacy-seven` | Select the compatibility preset or opt into adaptive painting. |
| `--finish photographic\|painterly` | `photographic` within adaptive painting | Adaptive finish defaults; rejected by `legacy-seven`. |
| `--paint-config FILE` | Finish defaults | A regular JSON file no larger than 64 KiB; partial configuration overrides are accepted. |
| `--max-strokes N` | Area-based admission | Positive integer that can only lower the adaptive allocation for this working canvas. |
| `--max-passes N` | Resolved `max_batches` | Positive integer that can only lower the configured adaptive **batch/work-unit guard**. It does not select a fixed number of brush passes. |

Preview and render do not paint the image. Selecting an adaptive policy does not make those stages execute brush batches. The common CLI controls, dimensions and resource profiles are described in [limits.md](limits.md); output states and partials are described in [outputs.md](outputs.md).

### Area-based allocation

For adaptive painting, the admitted upper stroke count is:

```text
floor(profile_stroke_cap × min(working_width × working_height, profile_pixel_cap)
      / profile_pixel_cap)
```

`--max-strokes` may reduce that value. A value above the area-based ceiling is rejected even when it is below the profile's full stroke cap. An area admitting no useful stroke is rejected. Delivery and working dimensions must independently satisfy the pixel limits.

Equal working area gives the same adaptive ceiling under the same profile. Actual use can differ with detail, regions, finish and convergence. This is an upper bound, not an allocation that the painter promises to consume.

Attempted work is charged before each native/Python stroke call. Rejected batches restore both colour and relief state, but their attempted strokes still consume the ceiling. A completed final batch is evaluated for quality before an exhausted budget is labeled a budget stop.

## Configuration schema version 1

The file must contain a JSON object. Unknown fields, unsupported schema versions, nonfinite numbers and booleans in numeric fields are rejected. Integer fields require integers. All ranges below include both endpoints.

Defaults start from the selected finish, then explicit JSON fields override them. `painterly` changes only the three defaults noted in the table; a configuration can explicitly override those too.

| Field | Photographic default | Accepted range | Meaning |
| --- | --- | --- | --- |
| `schema_version` | `1` | Integer `1` | Configuration format. |
| `ratio` | `0.55` | `0.1`–`0.9` | Geometric width reduction between brush tiers. |
| `max_width_fraction` | `0.055` | `0.005`–`0.3` | Largest nominal tier width relative to the smaller working dimension. |
| `min_width` | `1.2` | `0.5`–`64` | Nominal minimum width in working pixels; painterly default `2.8`. |
| `max_tiers` | `12` | Integer `1`–`32` | Finite brush-tier count guard. |
| `batch_size` | `4096` | Integer `1`–`65536` | Upper bound on strokes generated per adaptive batch. |
| `max_batches` | `2048` | Integer `1`–`8192` | Maximum attempted adaptive work units. |
| `evaluation_pixels` | `131072` | Integer `64`–`262144` | Upper bound for the regular metric-evaluation grid. |
| `target_error` | `0.020` | `0`–`1` | Global and regional weighted-error target; painterly default `0.030`. |
| `low_error` | `0.006` | `0`–`1` | Low-frequency error threshold before texture allowance; painterly default `0.025`. |
| `texture_tolerance` | `0.25` | `0`–`1` | Adds target-texture-dependent allowance to low-frequency thresholds. |
| `min_gain` | `0.000025` | `0`–`1` | Absolute improvement threshold for plateau detection. |
| `relative_gain` | `0.001` | `0`–`1` | Improvement threshold relative to the prior objective. |
| `patience` | `4` | Integer `1`–`64` | Consecutive low-gain batches before tier progression or a final plateau stop. |
| `min_coverage` | `0.97` | `0.1`–`1` | Required coverage fraction in each rendered region. |
| `uniform_fraction` | `0.25` | `0.05`–`1` | Minimum uniform component of stroke placement within the chosen region. |
| `rejection_tolerance` | `0.0001` | `0`–`0.1` | Permitted objective increase before a candidate batch is rolled back. |

The effective batch size is the smaller of configured `batch_size` and `max(16, ceil(working_area / 256))`. The remaining stroke allowance can reduce it further. `--max-passes` can lower `max_batches`; it cannot raise it. Increasing a configuration guard within its validated range does not raise the profile's stroke, pixel, memory or time caps.

For painterly finish, the nominal width floor is `min_width × max(1, S)`, where `S = working_width / 1600`. Photographic finish uses `min_width` directly. Both retain brushstroke rendering; neither finish name guarantees photographic fidelity.

This partial configuration illustrates valid syntax and smaller batch/guard settings. It is **configuration, not execution evidence or a calibrated quality preset**:

```json
{
  "schema_version": 1,
  "batch_size": 2048,
  "max_batches": 1024
}
```

Save it as a project file such as `painting.json`, then pass it explicitly:

```bash
"$PYTHON" "$SKILL_DIR/scripts/run.py" scene.py --out out/custom --stage paint \
  --paint-policy adaptive-v1 --finish painterly --paint-config painting.json
```

## Stops and completion

| Painting reason | Meaning | CLI outcome when outputs also complete |
| --- | --- | --- |
| `preset_complete` | The full legacy recipe finished. | Exit `0`, `result: completed`. |
| `quality_target` | Adaptive coverage and global/regional quality criteria were met. | Exit `0`, `result: completed`. |
| `diminishing_returns` | Coverage is established and refinement has plateaued at the final available tier. The quality target need not have been met. | Exit `0`, `result: completed`. |
| `stroke_budget` | Adaptive attempted-work ceiling was exhausted before convergence. | Exit `3`, `result: stopped`; a checkpoint partial is best effort. |
| `pass_guard` | The configured/CLI adaptive batch guard was reached. | Exit `3`, `result: stopped`; a checkpoint partial is best effort. |
| `time`, `memory` | The effective process resource allowance stopped work. | Exit `3`, `result: stopped`. |
| `cancelled` | A recorded stop/interrupt ended work. | Normally exit `143` for stop or `130` for interrupt. |
| `input_error`, `output_error` | Input or output processing failed. | Nonzero failure; inspect the usage report and diagnostic. |

Earlier admission can also stop on resource keys such as `vertices`, `triangles`, `pixels` or `strokes`. An adaptive `pass_guard` uses the existing stroke-limit exception channel; read the painting/top-level `terminal_reason` to distinguish it from `stroke_budget`.

Image publication and the supervisor's completion receipt must also succeed. A convergence message, an existing PNG or a scene calling `SystemExit(0)` does not establish a completed CLI run. See [output authority and receipts](outputs.md#authoritative-generations-and-completion).

## Painting reports and reproducibility

Once painting reporting is initialized, the usage report's `usage.painting` object has `schema_version: 1`. Preview, render and early admission failures can leave it empty. Both painting policies record `policy`, `terminal_reason`, `admitted_strokes`, `attempted_strokes`, `completed_strokes`, actual `region_ids` and `mask_planes`.

Adaptive reports additionally include `finish`, resolved `config`, `config_sha256`, `seed`, `stream_derivation`, `batches_complete`, `initial_metrics`, `final_metrics` and one `decisions` entry per attempted batch. Decisions record the tier, external region ID, attempted count, acceptance, cumulative attempted/accepted totals, gain and metric values. `batches_complete` counts accepted batches; a batch ordinal can be larger when earlier batches were rejected.

For adaptive runs, the general `usage.strokes_done` counter tracks attempted work. Use `usage.painting.completed_strokes` for accepted strokes. Counts must be interpreted with the policy, dimensions and terminal reason.

Adaptive streams use the reported `sha256-json-seedsequence-v1` derivation from seed, resolved configuration/finish digest, stable region ID, tier, batch and stream purpose. They do not depend on Python's randomized `hash()`. Keep the scene bytes, seed, rendering settings, policy/configuration and numerical/native runtime fixed for a meaningful repeatability comparison.

Cross-version, cross-platform and native-versus-Python bitwise identity is not promised. Wall-clock or memory stops can occur at different boundaries on different runs. The recorded decisions describe the completed attempt; they are not a resumable painter state. Inspect full-resolution and delivery output separately, especially thin structures, flat fields, sky/water and noisy vegetation. A numerical improvement can still change the preferred brushwork.
