# Outputs, manifests and recovery

`--out` is a filename prefix, defaulting to `out/painting` relative to the caller's working directory. Keep scene/configuration inputs and generated outputs in your project. Every invocation rebuilds the scene and starts a new run; `--stage paint` includes its own render.

Use the latest **`<prefix>_manifest.json`** to identify the authoritative image set. The familiar flat PNGs are compatibility exports. After a failed rename, a changed tile grid or another stage, old flat files can remain beside new ones; a filename glob does not establish ownership or completeness.

Commands below use the interpreter and resolved skill directory established in the [README](../README.md):

```bash
"$PYTHON" "$SKILL_DIR/scripts/run.py" scene.py --out out/painting --stage preview
"$PYTHON" "$SKILL_DIR/scripts/run.py" scene.py --out out/painting --stage render --tiles 3x2
"$PYTHON" "$SKILL_DIR/scripts/run.py" scene.py --out out/painting --stage paint --tiles 3x2
```

## Image names and dimensions

`D` below is the requested delivery size (`Scene.size`); `W` is the working size after scale resolution. Preview uses scale `1`. Render and paint use `--scale` or the resource level's default. Both sizes are checked independently against the pixel limits.

| Stage/output | Compatibility name | Dimensions/content |
| --- | --- | --- |
| Preview | `<prefix>_preview.png` | The rendered preview at `D = W`. |
| Render | `<prefix>_render.png` | Render downsampled/resized to `D`. |
| Render tiles | `<prefix>_render_tile_rRcC.png` | Crops of the working render. |
| Paint delivery | `<prefix>.png` | Finished painting resized to `D`. |
| Paint full | `<prefix>_full.png` | Finished painting at `W`. |
| Paint's kept render | `<prefix>_render.png` | That paint invocation's own render at `D`. |
| Paint tiles | `<prefix>_tile_rRcC.png` | Crops of the full working painting. |
| Best-effort partial | `<prefix>_partial.png` | Last safe painting checkpoint resized to `D`; not a finished painting. |

Images are RGB PNGs. `--tiles COLSxROWS` defaults to `3x2`; `--tiles 0` disables tiles. Rows and columns are positive integers, each no larger than the corresponding working dimension, and the product cannot exceed **1024 tile images**. Preview emits no tiles, although the option is still validated.

Tile indices start at `r1c1` in the top-left. Boundaries use integer subdivisions of the working image, so sizes can differ by one pixel when dimensions are not evenly divisible. Enumerate the current manifest's entries to review every tile emitted by that run. Render tiles and painted tiles are different sets.

## Authoritative generations and completion

For each publication, the engine:

1. Encodes and flushes sibling staging PNGs through exclusive file descriptors.
2. Records generation ownership before creating its directories.
3. Copies and verifies the full set into an independent immutable generation, recording hashes, sizes and dimensions.
4. Seals the generation's manifest, then replaces compatibility exports.
5. Atomically selects the generation through `<prefix>_manifest.json`.

If a compatibility export fails partway through, the prior selected manifest remains authoritative. Some flat PNGs may already have changed; the report identifies reported exports and whether their bytes match the selected generation. A failed publication is not repaired by assuming those flat files form a complete set.

Generation storage has this relative layout:

```text
out/
  painting_manifest.json
  painting_usage.json
  painting_generations/
    <run_id>/
      <generation_id>/
        manifest.json
        painting_render.png
        ...other images listed by this generation...
```

The per-generation `manifest.json` remains with the retained images. Paths inside an image entry are relative to the output prefix's parent directory (`out/` in this example).

The renderer sends an explicit completion receipt. Before returning success, the supervisor checks the run/stage/generation identity, selected manifest hash, required images and every requested tile, actual PNG sizes and dimensions, and image hashes. Success also requires usage-report publication. Scene code exiting with zero before this receipt is a failed workload. An interruption code without a recorded stop request is also a failure.

### Manifest schema version 1

This table describes the generated format; it is not a fabricated successful execution report.

| Field | Meaning |
| --- | --- |
| `schema_version` | `1`. |
| `run_id` | Identity of the CLI invocation. |
| `generation_id` | Identity of this immutable publication within the run. |
| `stage` | Requested `preview`, `render` or `paint` stage. |
| `partial` | Whether this publication is a painting checkpoint partial. |
| `terminal_reason` | Completion, partial or intermediate reason. |
| `provenance` | Scene/engine identities, settings and runtime information described below. |
| `images` | Entries containing `export`, `path`, `sha256`, `bytes` and `size: [width, height]`. |
| `previous_good_generation` | Retained prior complete-good generation descriptor, or `null`. |
| `previous_generation` | Compatibility alias for that same prior-good descriptor. |
| `storage_plan` | Retention, per-generation disk and metadata bounds plus estimated new-generation bytes. |

A prior-good descriptor contains `run_id`, `generation_id` and `manifest`, with `manifest` relative to the output directory. A nonpartial manifest is not sufficient to infer stage completion: paint first publishes a kept render with `terminal_reason: render_ready` while painting is still pending. A complete-good image set has the required stage outputs and an allowed completion reason: `preset_complete`, `quality_target` or `diminishing_returns`.

An application can locate authoritative images without guessing flat filenames:

```python
import hashlib
import json
from pathlib import Path

manifest_path = Path("out/painting_manifest.json")
manifest = json.loads(manifest_path.read_text())
for entry in manifest["images"]:
    image_path = manifest_path.parent / entry["path"]
    if hashlib.sha256(image_path.read_bytes()).hexdigest() != entry["sha256"]:
        raise ValueError("image changed since publication")
    print(entry["export"], image_path, entry["size"])
```

This reads a real existing manifest. Check `partial`, `terminal_reason` and the matching usage report before presenting it as a completed painting. A report copied from an earlier run can legitimately refer to a manifest that has since been superseded.

## Retention and disk accounting

Successful managed retention keeps at most **two generations**: the selected generation and its prior complete-good generation when one exists. Render-ready and partial publications preserve the prior complete-good reference rather than displacing it with another unfinished painting. The current in-flight generation and sibling staging files temporarily require additional disk space.

“Prior good” is based on the image-set manifest and required outputs. It can be a completed preview or render, not necessarily a finished painting. Reusing one prefix for further successful stages advances history. Keep a separate user-managed copy or choose a new output prefix when a particular image must remain indefinitely.

The ownership journal, declared file inventory and directory identity control pruning. Owned incomplete generations can be cleaned after failure. Unknown directories, user scenes, changed files, links and unproven ownership are not silently removed. Entries that prevent safe bounded retention block admission/cleanup rather than sacrificing protected complete images. A cleanup failure can leave extra data; inspect the selected manifest and `blocked_entries` instead of assuming every failed run selected the same generation. Unknown user directories are reported without recursively claiming their contents or size as managed output.

Disk checks use both the profile and actual job dimensions. Each generation's encoded images and metadata have a finite bound; manifests/traces are capped at **16 MiB**. The per-generation profile bound is:

```text
20 × profile_pixel_cap + 1027 × 4096 + 16 MiB bytes
```

Preflight estimates staging plus an independent new generation and checks current free space with an additional 16 MiB recovery margin. The smaller job estimate does not raise the profile bound. These are admission estimates and observations, not a filesystem reservation against unrelated programs.

The report's `disk` object includes:

| Field/group | Interpretation |
| --- | --- |
| `retained_generation_count`, `retained_generations` | Managed generation identities, states and known byte sizes. |
| `retained_bytes`, `retained_generation_bytes` | Known bytes in managed generation files. |
| `control_bytes`, `actual_store_bytes_known` | Ownership/journal bytes and their sum with known generation bytes. Flat exports and the external usage report are outside this store total. |
| `retained_byte_count_complete` | Whether all retained managed byte counts could be established. |
| `incomplete_generation_count` | Managed creation records still preparing. |
| `removed_ids` | Generation IDs pruned by this store instance. |
| `unknown_entries`, `blocked_entries` | Preserved/unvalidated entries and conditions blocking safe cleanup. |
| `maximum_retained_generations`, `maximum_generation_bytes`, `maximum_metadata_bytes` | Enforced storage/metadata bounds. |
| `estimated_new_generation_bytes`, `estimated_additional_disk_bytes` | Conservative admission estimates. |
| `disk_free_bytes` | Free-space observation on the output filesystem. |
| `staging_bytes`, `observed_peak_staging_bytes` | Remaining staging bytes and observed successful-save peak; not a physical disk quota or every failed-write peak. |

## Output ownership and concurrent runs

A persistent `<prefix>.lock` coordinates the prefix. The launcher, supervisor and renderer retain the same inherited lock lease through cleanup. Killing the launcher does not free the namespace while its supervisor still holds the lease. PID identity includes process start time and run identity so a reused PID is not enough to stop another process.

Directory-scoped `.meshpaint-outputs.lock` and `.meshpaint-outputs.json` coordinate **actual selected-stage** export/control/recovery names across different prefixes. The directory lock is held only for short ledger transactions. Genuinely disjoint jobs can render concurrently; overlapping destinations are refused. For example, a render at `out/picture` conflicts with a paint at `out/picture_render`, because both would write `out/picture_render.png`. Two previews using those prefixes have disjoint selected-stage exports and can run together.

The ledger is bounded and uses held prefix flocks plus recorded PID/start identity to recover proven stale leases. It does not open one descriptor per tile. Unknown or replaced coordination state is handled conservatively. Do not remove active lock/control entries to force a conflicting writer through.

Scene and painting-configuration paths, including existing inode/hardlink aliases, are checked for collisions with selected output/control names and generation directories before output bookkeeping begins. Use independent input files such as `scene.py` and `painting.json`; do not place them among generated/control files.

## Stops and partial recovery

```bash
"$PYTHON" "$SKILL_DIR/scripts/run.py" --stop --out out/painting
```

`--stop` verifies process identity before signaling. The supervisor also bounds cleanup of a renderer that ignores a stop or time limit. A real terminal interrupt normally returns `130`; a stop normally returns `143`. Resource stops use exit `3`; malformed CLI inputs normally use `2`, while execution/output failures use a nonzero failure code. Inspect the report rather than treating any existing PNG as success.

During paint, the kept render can survive a later painting stop. A partial painting is best effort:

- Legacy checkpoints occur after a whole pass.
- Adaptive checkpoints occur after an accepted batch, before final relief/grain processing.
- Partial metadata records the checkpoint's work-unit ordinal and accepted strokes. Rejected adaptive batches can make that ordinal differ from the number of accepted batches.
- A hard kill or early memory/time stop can occur before a safe checkpoint or leave insufficient capacity to encode one. No partial is then promised.

There is **no full-state resume**. Keeping a PNG and decision trace does not save the painter's complete mutable state. A later invocation starts the scene/render/paint workflow again. Successful paint publication clears an obsolete flat `_partial.png`; stale other flat outputs can still remain outside the selected set.

## Usage reports and provenance

`<prefix>_usage.json` has `schema_version: 1` and describes the latest supervised invocation for that prefix. A CLI/input/ownership refusal before supervision can leave an older report unchanged or create no report; combine command exit status with run identity. Selected top-level fields are:

| Field | Meaning |
| --- | --- |
| `run_id`, `level`, `stage`, `scale`, `scene` | Invocation identity and requested context; preview reports effective scale `1`. |
| `result` | `completed`, `stopped`, `interrupted` or `failed`. |
| `terminal_reason` | Policy completion/stop reason or a resource/input/output reason. See [painting stops](painting.md#stops-and-completion). |
| `stop`, `failure` | Structured resource-stop details or failure diagnostic, when applicable. |
| `allowed`, `usage` | Effective allowances and observed work/time/memory; painting decisions and disk details are nested in `usage`. |
| `stage_complete` | Renderer receipt or `null`; includes manifest hash, generation identity, image entries and resolved dimensions. Success requires validation; its presence alone does not establish a completed run. |
| `authoritative_generation`, `manifest_error` | Selected manifest snapshot or its read error. |
| `images_written_by_this_run`, `images_kept` | Reported flat exports and retained conventional image names with ownership flags. `images_kept` is not a complete tile inventory. |
| `compatibility_exports` | Whether each reported conventional flat image matches the selected generation. |
| `export_errors` | Reported PNG export failures. |
| `disk`, `memory_ceiling_enforced`, `processes_left_running` | Storage observations, memory-enforcement status and cleanup result. |

Provenance includes the SHA-256 of the exact scene bytes executed, an engine-source content digest, requested profile/effective limits, CLI settings and resolved adaptive configuration, resolved camera/look/environment settings, requested/working dimensions, Python/NumPy/SciPy/Pillow versions, selected native/Python core and fallback reason, and the named numerical-thread environment values. Resource plans, stage timing markers and painting decisions provide the corresponding work context.

An omitted adaptive `--finish` can remain `null` in CLI settings; `usage.painting.finish` records the effective `photographic` finish. Numerical-thread environment values are recorded settings, not CPU quotas. Memory estimates, peak address space and resident memory are distinct observations; see [limits.md](limits.md).

Reports do not embed full scene source or a complete environment dump. They can still reveal caller paths, camera/settings and fingerprints of private input. Review a report before sharing it. Keep the editable scene and any configuration separately with the image if later revision or repeatability matters.
