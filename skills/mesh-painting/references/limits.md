# Engine limits and common defects

## Scene limitations

| Feature | What the engine supports |
| --- | --- |
| Lighting | Shadows receive sky light. Surfaces in shadow can look blue because nearby sunlit surfaces do not contribute bounce light. Point-light shadows cover a cone pointing downwards. |
| Water and foam | Water lies on the plane at z = 0. Waves perturb the surface shading and blur reflections. Foam follows the shoreline. Open crests, spray and transparent shallows require additional modelling. |
| Terrain | A height field can form a steep slope. Use separate triangles or quads for overhangs, caves and arches. |
| Fine detail | Brushstrokes can smear parts narrower than about three pixels on the full working canvas. Widen features that must survive painting. |
| Sky | Clouds occupy a flat layer and can stretch towards the horizon. The sky model is tuned for sun elevations of roughly 1–10 degrees. Inspect previews carefully outside that range. |
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
| Subject appears out of scale | Check real dimensions and distance. On-screen height in pixels is approximately focal length × object height ÷ distance. |
| Sky below the horizon | In a scene with `sea=False`, extend the terrain throughout the view. |
| Small features disappear in the painting | Enlarge those features or increase the working scale within the chosen resource level. |

## Time and size

`--out` is a filename prefix; its parent directory is created when needed. Each stage replaces its own finished files only after writing temporary images successfully. Tiles cover the entire working canvas without overlap. [Output names and sizes](../README.md#outputs)

Measured on a six-core x86-64 Linux CPU with native C and BLAS/OMP threads set to 1, standard bundled runs took about 9 seconds for preview, 45 seconds for render and roughly three minutes for paint. Paint includes another render. These are condition-bound measurements. Give the calling tool enough time for the chosen stage and profile; high-scale timing is not guaranteed by a standard-scale measurement.

The Python core is much slower. A verified lighthouse fallback uses `--scale 0.25 --tiles 0`, producing a 400 × 250 working canvas and an upscaled 1600 × 1000 delivery image. It loses fine detail. Standard-size Python painting may exceed its time ceiling. [Fallback command](../README.md#performance-and-the-native-core)

## Resource levels

A level caps geometry, canvas size, stroke count, address space and wall-clock time. The defaults below use the example scenes’ 1600 × 1000 delivery size.

| Level | Default scale | Vertices | Triangles | Canvas pixels | Brushstrokes | Address space | Time |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `draft` | 1.2 | 1,000,000 | 1,000,000 | 2,304,000 | 750,000 | 2,048 MiB | 120 s |
| `standard` | 2.4 | 4,000,000 | 4,000,000 | 9,216,000 | 3,000,000 | 5,120 MiB | 600 s |
| `high` | 3.6 | 8,000,000 | 8,000,000 | 20,736,000 | 6,750,000 | 10,240 MiB | 1,800 s |

- Geometry counts accumulate across the whole scene. Mesh builders check requested counts before allocating. For `grid_mesh`, the check covers the grid before `drop_below` removes submerged triangles.
- Preview builds the complete scene and renders at scale 1. Render and paint scale both canvas dimensions. At Standard the example canvas is 3840 × 2400.
- At a delivery width of 1600, the seven passes plan 749,000 strokes at scale 1.2, 2,996,000 at 2.4 and 6,741,000 at 3.6.
- The launcher applies process limits before loading the scene. The address-space ceiling includes virtual memory reservations. Use `--time-limit` or `--memory-limit` to lower the chosen level’s limits.
- Standard is the default, including when only `--scale` is specified. Draft suits smaller scenes. The bundled moor has about 2.5 million vertices and needs Standard. Lowering image scale does not reduce geometry counts.
- Scale must be positive and finite; each working dimension must be at least 16 pixels. The pixel/stroke caps still apply when scale exceeds the profile default for a smaller scene.
- `--memory-limit` cannot be below 768 MiB or above the profile ceiling. `--time-limit` must be positive, finite and no greater than the profile ceiling.
- Regions 0 and 1 are reserved; scene regions must be integer IDs 2–127. Prefer dense low IDs: mask allocation extends through the highest rendered ID. [Region memory cost](scene-api.md#paint-regions)

Scene Python runs with your account's permissions. Limits supervise a trusted workload; they are not a security sandbox. Geometry helpers check counts before covered allocations, but cannot predict every array a scene callback will create. `RLIMIT_AS` bounds process address space, including virtual reservations; it does not reserve physical RAM or impose a total host-memory quota. The supervisor monitors the renderer, requests termination on a wall-clock overrun, escalates if it ignores termination and checks descendant cleanup.

## Stopped or failed runs

Read the exit status and `PREFIX_usage.json` together. A startup error or an unwritable report directory may prevent a new report; an existing file may be from an earlier run. Do not infer success from an old image or report.

| Outcome | How to respond |
| --- | --- |
| `stopped`, exit 3 | Report the stage, limiting resource, requested or observed use, and ceiling. For geometry limits, use coarser distant meshes or fewer scattered copies. For pixels or strokes, reduce scale or scene dimensions. If the user wants a higher level, obtain their approval before changing it. |
| Memory or time limit below the geometry caps | Inspect the last stage, peak memory and elapsed time. Check scene-created arrays, active renderer core, point lights and system load where relevant. Report observable figures and identify any open question. |
| `interrupted`, exit 130 or 143 | A stop was requested. Use the report to identify finished outputs. To stop a background job, invoke the launcher with `--stop` and the same `--out` prefix. |
| `failed`, exit 1 or another small code | Inspect the traceback or reported exit and last stage. Correct the identified error before retrying. |
| `failed`, signal exit | Record the signal and last stage. Inspect memory use, the traceback/log and the signal before deciding whether to retry. Preserve the original evidence and report both attempts if a retry is justified. |

If a paint stops after rendering, a completed `PREFIX_render.png` can survive. When at least one whole brush pass has completed and the process can save it, `PREFIX_partial.png` records the painting so far, before relief shading and grain. Label it as a partial painting.


## Prefix ownership and cleanup

A held `PREFIX.lock` identifies ownership across the launcher, supervisor and renderer. Another job with the same prefix exits 2 without replacing the active PID, images or usage report. Different prefixes have independent locks. The lock file remains after release; do not unlink it to force a competing run to start. Reusing a prefix after completion is allowed.

Use Ctrl+C, or invoke `--stop --out PREFIX` from the same caller directory with the selected interpreter and launcher. The stop path checks process identity and start time to avoid signalling a reused PID. It may report that no matching active process remains. Normal cancellation returns 130 for SIGINT or 143 for SIGTERM; the stop command itself has a separate status from the original job.

The supervisor attempts to clean up descendants and reports any it could not stop in `processes_left_running`. Check that field and the process state before starting more work. A persistent `.lock` alone does not mean a process is running.

Image writes are temporary until committed. A failed or interrupted stage discards its unfinished image writes and preserves previously completed files. Paint commits its underlying render before brushwork, so that image can survive a later failure. If a resource limit or memory error occurs after a whole paint pass and there is enough memory to save it, `_partial.png` records the completed-pass checkpoint at delivery size. Saving a partial is best effort; cancellation or a hard kill does not guarantee one. A successful paint removes a stale partial from an earlier attempt. Final image preservation does not imply that every image matches the current scene: check ownership in the report.

Changing a tile grid or disabling tiles does not remove older tiles with the same prefix. Use a fresh revision prefix to avoid confusing them with current outputs.

## Usage report

| Field | Interpretation |
| --- | --- |
| `result` | `completed`, `stopped`, `interrupted` or `failed` |
| `level`, `stage`, `scale`, `scene` | Selected profile, requested stage, effective scale and caller-relative scene path |
| `allowed` | Geometry, pixels, strokes, address-space MiB and wall-clock seconds ceilings |
| `usage` | Counts, planned/completed strokes, elapsed seconds, last stage, timeout flag and available sampled/OS memory figures in KiB |
| `stop` | Limit description: stage, resource, allowed amount and requested or observed use; null without a reported limit stop |
| `failure` | Failure summary when supplied; inspect the log/traceback too |
| `memory_ceiling_enforced` | Whether the renderer reported successful address-space-limit enforcement |
| `images_kept` | Primary images still present, with `written_by_this_run`; this list omits tiles |
| `images_written_by_this_run` | Newly written image paths, including tiles |
| `processes_left_running` | Descendant PIDs the supervisor could not stop |

Memory sampling may miss short peaks; some early-failure fields may be absent. Address-space and resident-memory figures mean different things. Paths are relative to the launch directory and can include `..` when output is elsewhere. Reports contain local scene/output paths; review them before sharing. They do not contain a scene hash or all CLI settings, so retain the source and command separately for reproducibility.

## Runtime troubleshooting

| Failure | Check |
| --- | --- |
| Dependency import fails | Use the same interpreter for `-m pip install -r requirements.txt` and the launcher |
| procfs monitoring unavailable | Ensure `findmnt` and procfs for the current PID namespace are available; check the environment permits required Linux process operations |
| Native compiler or cache rejected | Read the fallback reason; check compiler availability, ownership, permissions, symlink/link checks and trusted directory ancestors. A private temporary native build is attempted before Python fallback |
| Region validation fails | Use non-boolean Python/NumPy integers 2–127 for mapping values and `default_region` |
| Region masks exhaust memory | Reassign sparse region IDs densely; reduce scale or scene size if necessary |
| Geometry stop | Coarsen grids or reduce scattered copies; image scale does not change geometry counts |
| Pixel or stroke stop | Lower scale or delivery dimensions; a resource profile is an explicit choice |
| Non-finite heights or image values | Inspect the scene's math, clip square-root/fractional-power inputs, and keep material/bump outputs finite. The image writer rejects NaN/inf instead of silently converting it |
| Time or memory stop | Inspect the limiting stage and active core, reduce the workload, and account for other host activity |
| Cancellation or stale output | Check outcome, ownership and cleanup; use a new prefix for a retry |

[Native cache and environment controls](../README.md#cache-and-environment) describe compiler selection, `CC`, `XDG_CACHE_HOME`, `TMPDIR` and `MESHPAINT_NO_NATIVE`.
