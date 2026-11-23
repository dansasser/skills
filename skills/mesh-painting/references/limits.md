# Engine limits and common defects

This reference covers scene limitations, workload admission and process supervision. See [painting policies](painting.md) for stroke decisions and [outputs and recovery](outputs.md) for manifests, checkpoints and file ownership.

## Scene limitations

| Feature | What the engine supports |
| --- | --- |
| Lighting | Shadows receive sky light. Surfaces in shadow can look blue because nearby sunlit surfaces do not contribute bounce light. Point-light shadows cover a cone pointing downwards. |
| Water and foam | Water lies on the plane at z = 0. Waves perturb surface shading and blur reflections. Foam follows the shoreline. Open crests, spray and transparent shallows require additional modelling. |
| Terrain | A height field can form a steep slope. Use separate triangles or quads for overhangs, caves and arches. |
| Fine detail | Brushstrokes can smear parts narrower than about three pixels on the working canvas. Widen features that must survive painting and inspect the delivery image after resizing. |
| Sky | Clouds occupy a flat layer and can stretch towards the horizon. The sky model is tuned for sun elevations of roughly 1–10 degrees. Inspect previews outside that range. |
| Figures and vegetation | People, animals and trees require custom meshes. Blade tufts work for grass and scrub. [Ground detail](scene-api.md#ground-detail) |

## Defects and fixes

| What you see | What to check or change |
| --- | --- |
| A straight wall at the edge of a cliff | Extend the terrain patch past the frame, sink its boundary below the water or hide the edge behind another patch. |
| Foreground is blurred | Lower camera aperture or move the focus distance closer. |
| Coarse triangles appear through fine ground | Lower the coarse patch inside the fine patch's footprint. |
| Black patch or non-finite height error | Clip values before taking square roots or fractional powers. [Mesh builders](scene-api.md#meshes) |
| Faint discs near the subject | Set `lens_ghosts=False` or move the subject away from the line between the sun and image centre. |
| Streaks or shimmer on distant surfaces | Fade bump and colour detail with `g.fade(feature_size)`. [Material shading and bump](scene-api.md#materials-shade-and-bump) |
| Excessive waves or foam | Lower `sea_state` or `foam`. If foam fills a sheltered pool, check the surrounding rock height and opening to the sea. |
| Floating or buried object | Evaluate the ground height at the object's position and seat its base slightly below it. |
| Smooth plastic-looking rock | Shape ridges and ledges in the mesh, then layer the material colour. |
| Visible triangular facets | Reduce mesh step near the camera or build a separate fine patch. |
| Dark subject under a low sun | The `sunset` look backlights the subject. Choose `golden_hour` or override the sun direction for side lighting. |
| Subject appears out of scale | Check dimensions, forward depth and the normalized camera focal length. [Camera](scene-api.md#camera) |
| Sky below the horizon | With `sea=False`, extend terrain throughout the view. |
| Small features disappear in the painting | Enlarge those features or increase working scale within the selected resource level. Compare both working and delivery images. |

## Time and size

Preview builds the complete scene and renders at scale 1. Render and paint use the selected scale; paint also performs a render. Reducing scale lowers image work but does not reduce the geometry built by the scene.

Runtime depends on geometry, image dimensions, regions, point lights, painting policy, numerical dependencies, active core and host load. The optional native C core accelerates rasterization and brushwork; the Python fallback follows the same workload limits and can take substantially longer. Start fallback work with a small scene and canvas. A larger tool timeout does not raise the launcher's own time ceiling, and raising a resource level does not guarantee completion. There is no automatic retry or profile escalation.

`--out` is a filename prefix, resolved from the caller's working directory. Its parent is created when needed. The authoritative output is the generation selected by `PREFIX_manifest.json`; flat PNG names are compatibility exports. See [outputs and recovery](outputs.md) before treating an existing image as the result of the latest command.

## Resource levels

A level caps original and generated geometry, pixel areas, painting strokes, renderer address space and elapsed time. The default scales below assume a 1600 × 1000 delivery image.

| Level | Default scale | Vertices | Triangles | Canvas pixels | Brushstrokes | Address space | Time |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `draft` | 1.2 | 1,000,000 | 1,000,000 | 2,304,000 | 750,000 | 2,048 MiB | 120 s |
| `standard` | 2.4 | 4,000,000 | 4,000,000 | 9,216,000 | 3,000,000 | 5,120 MiB | 600 s |
| `high` | 3.6 | 8,000,000 | 8,000,000 | 20,736,000 | 6,750,000 | 10,240 MiB | 1,800 s |

`standard` is the default, including when only `--scale` is supplied. A profile is a ceiling, not a promised workload size or hardware qualification. A scene can meet the individual geometry and pixel caps and still fail the combined memory estimate or time ceiling.

- Geometry counts accumulate across the whole scene. Covered helpers check requested geometry before allocation; `grid_mesh` checks the grid before `drop_below` removes triangles. Scene-created intermediate arrays remain subject to the process memory limit.
- Near-plane clipping has separate generated-buffer checks against the same vertex and triangle ceilings. It does not add generated counts to the original scene totals. [Clipping and report counts](scene-api.md#near-plane-clipping)
- Both delivery and working pixel areas must fit the profile. Auxiliary maps also have bounded dimensions and pixel budgets.
- `--memory-limit MIB` can only lower the profile ceiling and must be at least 768 MiB. An inherited stricter address-space limit remains effective.
- `--time-limit SECONDS` must be finite, positive and no greater than the profile ceiling. It limits the whole requested stage, including import, scene building, render, painting and output work.
- Region IDs 0 and 1 are reserved for sky and sea. Scene region values must be non-boolean integers 2–127. Sparse IDs use compact mask slots. [Paint regions](scene-api.md#paint-regions)

### Dimensions and optical scale

For delivery size `(dw, dh) = Scene.size` and effective scale `s`:

```text
working_width  = int(dw * s)
working_height = int(dh * s)
A = working_width * working_height
D = dw * dh
camera_scale  = working_width / 1600
working_focal = Camera.focal * camera_scale
```

Preview sets `s = 1`; render and paint use `--scale` or the profile default. Delivery dimensions must be positive integers, scale must be finite and positive, and each working dimension must be at least 16 pixels. Dimensions must fit the PNG/native integer representation. Both `A` and `D` must be at most the profile's canvas-pixel cap; lowering scale cannot make an oversized delivery image admissible.

At Standard, a 1600 × 1000 scene at scale 2.4 uses a 3840 × 2400 working canvas. A scale below 1 makes a smaller working canvas that is upscaled for delivery; it cannot restore detail absent from that canvas. `Camera.focal` is normalized to a 1600-wide frame, so resizing preserves horizontal field of view. Optical scale and painting area have distinct roles.

### Painting work ceilings

The default `legacy-seven` policy retains its seven-pass schedule and historical width-based stroke planning. At delivery width 1600, the default Draft, Standard and High scales plan 749,000, 2,996,000 and 6,741,000 strokes respectively. Those figures describe that policy and width; they are not universal counts for every scene. A tall or narrow canvas can have a different area without changing that width-based plan.

The opt-in `adaptive-v1` policy admits an area-based maximum:

```text
admitted_strokes = floor(profile_strokes * min(A, profile_pixels) / profile_pixels)
```

`--max-strokes` may only lower this admitted count. An area that admits fewer than one stroke is rejected. `--max-passes` lowers the adaptive batch guard; it does not select a fixed number of brush passes. Attempted work, including rejected trial batches, consumes the stroke budget. A resource or batch guard is a stopped outcome rather than a claim that the quality target was reached. [Painting policy details](painting.md)

### Memory and auxiliary maps

The renderer performs a conservative address-space admission check after assembling the mesh and before allocating render buffers. It includes imported process address space, mesh/render/paint lifetime groups, an output reserve, and simultaneous sun, point-light and shoreline maps. Actual arrays made by arbitrary callbacks and dependency-specific allocator behaviour are not fully predictable.

The current estimate uses `A` working pixels, `D` delivery pixels, `V` vertices, `T` triangles and `K` possible paint regions:

```text
B = 48*V + 32*T                         # estimated mesh bytes
mesh_group   = 3*B
render_group = B + (204 if sea else 168)*A
paint_group  = B + (160 + 8*K)*A
output_reserve = max(16*1024**2, 20*A + 8*D)
```

`K` conservatively includes the mapped regions of assembled materials, sky and, when enabled, sea. Unused material mappings do not add mask planes to this assembled-scene estimate. The painter allocates masks only for IDs actually rendered: two float32 mask sets cost approximately `8*K_actual*A` bytes. Changing ID 2 to 127 does not by itself increase that count.

Auxiliary maps share the remaining estimated address-space allowance, with a reservation of 32 bytes per admitted map pixel. Each map also obeys the profile pixel cap and an 8192-pixel maximum dimension. Preferred map dimensions are retained when they fit; otherwise they are reduced deterministically. More point lights share this finite allowance rather than receiving unbounded full-size maps. The planned dimensions and estimate appear in `usage.resource_plan`.

These formulas describe the current admission model, not a physical-memory reservation or an exact measured peak. The effective ceiling is the lower of the selected renderer limit and an inherited address-space limit. `RLIMIT_AS` includes virtual reservations; it does not reserve RAM or impose a combined memory quota on the whole host. The operating-system limit remains authoritative if allocation exceeds the estimate. Sampled memory can miss short peaks; resident memory and address space are different measurements.

## Stopped or failed runs

Read the exit status, `PREFIX_usage.json` and selected manifest together. A startup error or unwritable report directory may prevent a new report. An old image or report does not establish success for a new run. A completed CLI stage requires a receipt for the current run and stage with verified image files, dimensions and hashes.

| Outcome | Interpretation and next step |
| --- | --- |
| `completed`, exit 0 | The requested stage produced and verified its required outputs. Check `terminal_reason` and the selected generation. |
| `stopped`, exit 3 | A reported resource or adaptive work guard ended the job. Inspect `stop`, the last stage, requested or observed use, and the ceiling. Coarsen geometry or reduce image work as appropriate. |
| `interrupted`, exit 130 or 143 | A real interrupt or stop request was observed. Inspect the selected generation for completed render or partial-paint recovery. |
| Startup rejection, exit 2 | The invocation, output ownership or initial environment checks were refused. A new usage report may not exist. |
| `failed`, exit 1 or another code | Inspect `failure` and the log/traceback. Invalid scene data, malformed callback results and output errors require correction before retrying. |
| `failed`, signal exit | Record the signal and last stage. Preserve the log and memory figures; a signal alone does not identify its cause. |

`terminal_reason` distinguishes painting completion (`preset_complete`, `quality_target` or `diminishing_returns`) from endings such as `stroke_budget`, `pass_guard`, `time`, `memory`, `cancelled`, `input_error` and `output_error`. Geometry and pixel stops can name their limiting resource. A completed preview or render uses `preset_complete`; legacy paint completion also uses that reason. Adaptive convergence decisions describe its image metrics, not a guarantee of subjective quality.

Paint selects a completed render before brushwork. If later work stops, that render can remain available. A recoverable partial contains the last completed legacy pass or accepted adaptive batch, before final relief shading and grain. Partial saving is best effort; an early stop, hard kill, memory shortage or disk failure may prevent it. Label it as a partial painting. There is no automatic resume from that PNG. [Recovery details](outputs.md)

## Prefix ownership and cleanup

A held `PREFIX.lock` coordinates the launcher, supervisor and renderer. Another active job with the same prefix exits 2. The lock file persists after release; its existence alone does not mean a job is active. Do not unlink it to force a competing run to start.

Different prefixes may run concurrently when their stage-specific output filenames are disjoint. Different prefix strings can still claim the same filename; overlapping destinations are refused. Output names must not alias the scene source, painting configuration or control files. The directory coordinator serializes reservation metadata updates, not the whole render. Lock ownership survives a killed launcher while the supervisor or renderer still holds the job's descriptor. See [output ownership](outputs.md).

Use Ctrl+C, or run the stop command with the same selected interpreter, launcher, caller directory and output prefix:

```sh
"$PYTHON" "$SKILL_DIR/scripts/run.py" --stop --out workspace/out/name
```

`SKILL_DIR` and `PYTHON` follow the [README setup](../README.md). The stop path checks process identity and start time before signalling, and may report that no matching active process remains. The stop command's status is separate from the original job's exit status. The supervisor normally gives a termination request 20 seconds before forced cleanup; Ctrl+C uses a 10-second grace period. It also enforces the stage deadline externally with a 20-second grace period. Native work may delay a cooperative checkpoint, so these are supervision bounds rather than a promise of an immediately saved partial.

The supervisor attempts to stop descendants and reports remaining PIDs in `processes_left_running`. Inspect that field and actual process state before assuming cleanup completed.

The manifest selects an immutable generation atomically. Compatibility PNGs are exported individually and can be stale or mixed after an export fault. Managed retention protects the selected generation and a prior successful generation, with at most two recognized retained generations after successful pruning. Unknown user entries are preserved and reported; they may prevent safe admission. This does not bound unrelated disk usage. Changing or disabling tiles can leave old flat tile exports; the manifest defines the current set. [Generation retention and compatibility exports](outputs.md)

## Usage report

| Field | Interpretation |
| --- | --- |
| `schema_version`, `run_id` | Report format and identity of the current invocation |
| `result`, `terminal_reason` | Overall outcome and the reason the stage or policy ended |
| `level`, `stage`, `scale`, `scene` | Selected profile, requested stage, effective scale and caller-relative scene path |
| `allowed` | Geometry, pixels, strokes, effective address-space MiB and elapsed-time ceilings |
| `usage` | Original geometry, admitted/planned and completed work, last stage, elapsed time and available memory measurements |
| `usage.job_plan` | Resolved delivery and working dimensions, area and optical scale |
| `usage.resource_plan` | Conservative address-space estimate and bounded auxiliary-map plans |
| `usage.projection_totals` | Most recently checked clipping-buffer upper bounds, when clipping required a check |
| `usage.painting` | Policy, admitted/attempted/accepted work, region IDs and adaptive decisions when available |
| `stop`, `failure` | Resource-stop details or a failure summary; inspect the log too |
| `provenance` | Captured scene-source hash, resolved settings, runtime/core information and available painting configuration |
| `stage_complete` | Current-run completion receipt when supplied |
| `authoritative_generation`, `manifest_error` | Selected manifest and any failure to read it |
| `disk` | Managed generation storage, retention and unknown-entry accounting when available |
| `compatibility_exports`, `export_errors` | Whether flat images match the selected generation and any export errors |
| `memory_ceiling_enforced` | Whether the renderer reported successful address-space-limit enforcement |
| `images_kept` | Primary flat images still present, with `written_by_this_run`; this list omits tiles |
| `images_written_by_this_run` | Newly written flat image paths, including tiles |
| `processes_left_running` | Descendant PIDs the supervisor could not stop |

Some fields are absent after early failures. Paths are relative to the caller directory and may contain `..`. Provenance records the scene bytes captured for execution, but it is not a bundle of imported modules, external assets or every source dependency. Keep those inputs when reproducing a run. Reports can contain local paths and environment/runtime details; review them before sharing. Cross-version numerical differences can change pixels even with the same seed.

## Runtime troubleshooting

The supervised CLI requires Linux process operations, enforceable address-space limits, `fork`, signals, file locking, `findmnt`, and procfs visibility for the current PID namespace. Scene Python executes with the caller's permissions; resource supervision is not a security sandbox. Numerical thread settings remain caller-controlled and are recorded when available; resource profiles are not CPU quotas.

| Failure | Check |
| --- | --- |
| Dependency import fails | Install `requirements.txt` with the same selected interpreter used by the launcher. |
| procfs monitoring unavailable | Check `findmnt`, the procfs mount and visibility of the current PID namespace. The launcher refuses to run without the required monitoring. |
| Native compiler or cache rejected | Read the fallback reason; check compiler availability, ownership, permissions and trusted cache ancestors. A private temporary native build is attempted before Python fallback. |
| Scene or helper validation fails | Use finite arrays of the documented shape, integral in-range indices and materials, and valid callback results. [Scene API](scene-api.md) |
| Region validation fails | Use non-boolean Python/NumPy integers 2–127 for mapping values and `default_region`. |
| Region memory estimate exceeds the ceiling | Reduce the number of distinct regions, scale or scene size. Renumbering sparse IDs densely does not save mask planes. |
| Geometry or generated-clipping stop | Coarsen grids, reduce scattered copies or move near-plane crossings. Image scale does not reduce original geometry counts. |
| Pixel or stroke stop | Lower working scale or delivery dimensions as appropriate to the named check. Choose a different resource profile explicitly if needed. |
| Non-finite height, material, bump or image value | Inspect the scene's math, clip square-root/fractional-power inputs, and keep callback results finite. The image writer rejects NaN/inf. |
| Time or memory stop | Inspect the limiting stage, memory estimate, active core and actual work. Reduce the workload before retrying. |
| Cancellation, stale image or export failure | Check current-run outcome, manifest selection, compatibility-export hashes and cleanup. Use a new prefix when preserving separate revisions. |

### Native cache and environment controls

The engine compiles its bundled `core.c` on first native use. It tries the compiler command in `CC`, then `cc`, `gcc` and `clang`. `CC` is split into command arguments without a shell; compilation uses a bounded subprocess timeout. A usable existing native library avoids recompilation.

The persistent cache is under `XDG_CACHE_HOME`, or the current user's home cache directory when that variable is unset, in `meshpaint/native-v2/<source-hash>/`. The engine checks directory ancestors, ownership, permissions, file type, links and source/library digest receipts before loading it. Cache files are generated outside the skill source. If the primary cache is unavailable, it attempts a private temporary build in Python's temporary directory selection, which honours `TMPDIR` when usable. If no native build can be loaded, the engine prints the reason and uses the Python core.

Set `MESHPAINT_NO_NATIVE=1` on a command to force Python fallback for that run. Compiler/cache controls do not alter resource caps. `OPENBLAS_NUM_THREADS`, `OMP_NUM_THREADS`, `MKL_NUM_THREADS` and `NUMEXPR_NUM_THREADS` remain caller environment settings; the launcher records available values but does not impose a CPU quota. Select compatible dependency builds with the same interpreter used for the launcher. See the [README setup](../README.md) for installation commands.
