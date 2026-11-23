# Mesh Painting

Create landscapes, seascapes and building scenes from editable Python code. Mesh Painting builds triangle meshes, shades procedural materials with lighting, shadows and lens effects, then paints the rendered image with brushstrokes. It uses CPU code without photographs, downloaded textures, model weights or an image-generation service.

If Mesh Painting is useful to you, [⭐ star the repo on GitHub](https://github.com/dansasser/skills).

The reusable agent workflow is in [SKILL.md](SKILL.md). The same renderer runs directly from a terminal. Your scene remains an editable asset: change its geometry, materials, camera or light and make another revision.

## What it does

- Turns an agent-authored or hand-written scene into a composition preview, shaded render and painted image.
- Provides terrain, mesh builders, procedural materials, ground cover, lights, sky, water and lens controls. The [scene API](references/scene-api.md) defines the supported interfaces.
- Separates delivery size from the larger working canvas, with tiles for inspecting details at original resolution.
- Keeps the historical seven-pass recipe as the default `legacy-seven` policy. Opt-in `adaptive-v1` allocates refinement by image/region content and measured progress; its work is not fixed to seven passes.
- Records source/configuration hashes, resolved settings, runtime/core, resource usage, painting decisions and output generations for comparisons and recovery.

Use it for editable still scenes made from meshes and brushwork. Logos, diagrams, charts, text graphics, photo edits and video need different tools. A request for an image-generation model is outside this workflow. Realism depends on modelling, materials, resolution, brushwork and visual revision; the engine does not guarantee a photographic result.

## Requirements

- **Linux**, with procfs describing the current PID namespace and `findmnt` from util-linux. The environment must allow forking, signals, file locking and enforced `RLIMIT_AS` address-space limits.
- **Python 3.10+**, with venv/pip and compatible builds of **NumPy, SciPy and Pillow** from [requirements.txt](requirements.txt).
- **A PNG viewer** or agent image-viewing tool. Reviewing the actual preview, render and painted tiles is part of the workflow.
- **Optional C compiler:** `cc`, GCC or Clang. The bundled C source is compiled on first use into a checked private cache. Without a usable native core, rendering/painting falls back to slower Python.

A GPU and API keys are unnecessary. Dependency installation can need network access; rendering needs no network service. Scene modules execute trusted Python with your account's permissions. Resource supervision limits computation; it does not sandbox file access, network access or malicious code. Review supplied scene files before running them.

The default Standard profile allows up to 5,120 MiB of process address space and 600 seconds per supervised run. Address space is not reserved physical RAM. Use a host with enough available memory for the scene and other work. Native Windows, macOS and hosted execution have not been qualified; the Agent Skills format does not provide a rendering runtime. See [limits and platform boundaries](references/limits.md).

## Install and set up

Install the complete `mesh-painting` folder from the shared repository; it does not need its own repository. The [collection installation guide](https://github.com/dansasser/skills/blob/main/docs/installation.md) covers the pinned Skills CLI, project/user copies and agent-specific invocation.

For direct terminal use, run the following from the directory where you want the collection checkout:

```sh
git clone https://github.com/dansasser/skills.git
cd skills
SKILL_DIR=skills/mesh-painting
python3 -m venv .venv
PYTHON=.venv/bin/python
export OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1
"$PYTHON" -m pip install -r "$SKILL_DIR/requirements.txt"
"$PYTHON" "$SKILL_DIR/scripts/run.py" --help
```

All commands below continue from that **collection root**, with `SKILL_DIR` and `PYTHON` set as above. For an installed skill, resolve its root from the loaded `SKILL.md`, set `SKILL_DIR` accordingly and use the interpreter for your project environment. Keep scene files, environments and outputs outside the installed skill. Quote paths containing spaces and use the same selected interpreter for pip, checks and every render stage.

## General workflow

### 1. Build an editable scene

Write a Python module whose `build()` returns a `meshpaint.scene.Scene`. Start with the [minimal scene](references/scene-api.md), then choose geometry, procedural materials, camera and lighting for your subject. The launcher supplies the bundled package on the import path. Use metres, Z up, and `scene_rng(name)` for scene randomness controlled by `--seed`.

Keep your source in a workspace. Bundled lighthouse and moor scenes are examples to copy when useful, rather than templates every subject must follow. Scene delivery size is `(width, height)`; camera optical scale and working resolution have separate meanings. See [dimensions and resource accounting](references/limits.md).

### 2. Preview, render and inspect

For a scene saved as `workspace/scene.py`:

```sh
"$PYTHON" "$SKILL_DIR/scripts/run.py" workspace/scene.py --stage preview --look golden_hour --seed 11 --out workspace/out/scene
"$PYTHON" "$SKILL_DIR/scripts/run.py" workspace/scene.py --stage render --look golden_hour --seed 11 --out workspace/out/scene
```

Open `_preview.png` after the first command. Check composition, proportions, exposure and light; revise and preview before running the second command. Preview uses scale 1 and still constructs the complete scene geometry.

After rendering, open `_render.png` and **every emitted render tile at original resolution**. Inspect foreground and background for seams, stretched triangles, floating objects, blur, aliasing and material defects. The default grid is `3x2`, producing six tiles; another valid grid changes that count. Repeat the render and review after a source or settings change.

### 3. Paint and deliver

```sh
"$PYTHON" "$SKILL_DIR/scripts/run.py" workspace/scene.py --stage paint --look golden_hour --seed 11 --out workspace/out/scene
```

Keep the source, seed, camera, look, custom sun, level and scale matched to the inspected render. Painting renders internally again; it does not load the earlier render PNG. Open the delivery `.png`, `_full.png` and every painted tile. Relief shading and grain follow the brushwork. Resizing makes the delivery image: downsampling a larger working canvas or enlarging a smaller one.

Deliver the editable scene with the finished images, selected policy/settings and outcome. Explain visible limitations and skipped checks. For revisions, keep earlier source/output under versioned names and preserve camera, seed and unrelated content unless the requested change requires them to change.

## Painting policies

| Policy | Behaviour | Controls |
| --- | --- | --- |
| `legacy-seven` — default | Exact historical seven-pass recipe; counts depend on its working-width recipe | Select explicitly with `--paint-policy legacy-seven`; adaptive customization flags are rejected |
| `adaptive-v1` — opt-in | Bounded batches choose image regions and brush tiers using fixed-reference error/coverage metrics | `--finish`, `--paint-config`, `--max-strokes`, `--max-passes` |

Opt into adaptive painting with a separate prefix for comparison:

```sh
"$PYTHON" "$SKILL_DIR/scripts/run.py" workspace/scene.py --stage paint --look golden_hour --seed 11 --out workspace/out/scene-adaptive --paint-policy adaptive-v1 --finish photographic
```

`photographic` is the adaptive default finish; `painterly` favours larger visible marks. These labels select brush settings, not a realism guarantee. Stroke ceilings depend on working area; useful work also depends on detail, regions, brush tiers and progress. Quality-target and diminishing-returns outcomes are distinct. Hitting a stroke, work-unit, time or memory guard produces a stopped outcome, not an automatically completed painting.

Adaptive painting can use fewer strokes while taking longer or looking coarser than legacy. Small detailed targets can exhaust their admitted work. Keep it opt-in while comparing actual images, full-size details and cost. The [painting reference](references/painting.md) gives the versioned JSON fields, defaults, ranges, budget formula, deterministic streams and terminal reasons. A custom configuration must be a regular JSON file no larger than 64 KiB. The CLI `--max-strokes` and `--max-passes` options only lower their resolved guards; JSON settings stay within validated ranges and the selected resource profile.

## Command controls

| Option | Meaning / default |
| --- | --- |
| `--stage preview\|render\|paint` | Default `preview`; render has no brush painting |
| `--out PREFIX` | Default `out/painting`; keep separate prefixes for comparisons |
| `--level draft\|standard\|high` | Default `standard`; profile selection is an explicit resource choice |
| `--scale NUMBER` | Working scale for render/paint: Draft 1.2, Standard 2.4, High 3.6; preview uses 1 |
| `--look sunset\|golden_hour` | Override the scene's look; otherwise use its setting |
| `--sun OFFSET ELEV` | Sun angles in degrees relative to view direction; overrides scene/custom look direction |
| `--seed INTEGER` | Nonnegative seed, default 11 |
| `--tiles COLSxROWS` | Default `3x2`; `0` disables tiles; at most 1,024 tile images and no empty tiles |
| `--time-limit SECONDS`, `--memory-limit MIB` | Lower the selected profile's time/address-space ceiling |
| `--paint-policy`, `--finish`, `--paint-config` | Painting policy and opt-in adaptive configuration |
| `--max-strokes N`, `--max-passes N` | Lower adaptive attempted-stroke / work-unit guards; these are not requests for extra passes |
| `--stop` | Request cancellation using the same `--out` prefix; no scene argument needed |

Use `--help` for syntax, [limits](references/limits.md) for admission/profile rules and [painting](references/painting.md) for adaptive settings. An invalid or over-budget request does not authorize silently changing the scene, raising a profile or relaunching it.

## Outputs, provenance and recovery

Names follow the chosen prefix. Tile dimensions depend on the working size and grid, including uneven edge divisions.

| Output | Purpose |
| --- | --- |
| `_preview.png` | Composition preview at delivery size |
| `_render.png` | Shaded render at delivery size, also emitted during painting |
| `_render_tile_rROWcCOL.png` | Full-working-resolution render crops |
| `.png`, `_full.png` | Painted delivery image and full working canvas |
| `_tile_rROWcCOL.png` | Full-working-resolution painted crops |
| `_partial.png` | Best-effort unfinished painting checkpoint before final relief/grain |
| `_manifest.json`, `_generations/` | Selected authoritative image set and immutable generation images |
| `_usage.json` | Outcome, limits, settings/source/runtime provenance, painting decisions, image ownership, managed storage and cleanup |
| `.pid`, `.lock` and coordination state | Active identity and output ownership; persistent idle lock/control files are expected |

**The manifest selects the authoritative generation.** Flat PNGs are compatibility exports. A failed export can leave those files stale or mixed; their existence is not proof the new run succeeded. A completed stage requires the verified stage receipt and matching generation. Check the command exit status, report `result`/`terminal_reason`, selected manifest and whether an image was written by this run.

Successful managed retention keeps at most two generations: the selected image set and its true previous complete-good set when one exists. Partial/render-ready intermediates do not displace the previous good output. Unknown user files are preserved and reported; managed history bounds do not bound arbitrary user files or compatibility exports. Blocked cleanup can leave extra data and causes a reported failure rather than unsafe deletion. Overlapping active output sets are refused, while disjoint jobs can run concurrently. Keep control files in place.

Ctrl+C requests cancellation. To stop a background run:

```sh
"$PYTHON" "$SKILL_DIR/scripts/run.py" --stop --out workspace/out/scene
```

A new partial is best effort: forced termination, memory exhaustion or output failure can prevent it. A partial is not a finished image or a resumable full-state checkpoint; **resume is not implemented**. Reports can also be absent after early failures. The [output/recovery reference](references/outputs.md) defines reports, generations, storage, stale exports and interruption boundaries.

## Bundled examples

The lighthouse and moor show particular scenes; their dimensions, stroke counts and brushwork do not define the general interface.

```sh
mkdir -p workspace
cp "$SKILL_DIR/assets/scenes/lighthouse.py" workspace/lighthouse.py
"$PYTHON" "$SKILL_DIR/scripts/run.py" workspace/lighthouse.py --stage preview --look golden_hour --seed 11 --out workspace/out/lighthouse
"$PYTHON" "$SKILL_DIR/scripts/run.py" workspace/lighthouse.py --stage render --look golden_hour --seed 11 --out workspace/out/lighthouse
"$PYTHON" "$SKILL_DIR/scripts/run.py" workspace/lighthouse.py --stage paint --look golden_hour --seed 11 --out workspace/out/lighthouse
```

Inspect between stages as described above. Both bundled scenes have delivery size 1600 × 1000. At Standard scale 2.4, the working canvas is 3840 × 2400 and a `3x2` grid gives six 1280 × 1200 tiles. For the moor, copy `assets/scenes/moor.py` into the workspace and use a different prefix. Its geometry exceeds Draft even at low scale; use Standard.

![A striped lighthouse above a rocky headland in evening light](doc/examples/lighthouse_golden_hour.png)

*Bundled lighthouse example, golden-hour lighting and legacy painting.*

![A stone cairn rising above golden moor grass](doc/examples/moor_golden_hour.png)

*Bundled moor example, golden-hour lighting and legacy painting.*

For a bounded Python-fallback check, use the lighthouse at scale 0.25 without tiles:

```sh
MESHPAINT_NO_NATIVE=1 "$PYTHON" "$SKILL_DIR/scripts/run.py" workspace/lighthouse.py --stage paint --look golden_hour --seed 11 --scale 0.25 --tiles 0 --out workspace/out/lighthouse-python
```

This deliberately lowers detail. Python-core painting at full Standard scale can exceed the time limit. See [runtime/cache controls](references/limits.md).

## Verification

```sh
"$PYTHON" "$SKILL_DIR/tools/check.py"
"$PYTHON" "$SKILL_DIR/tools/check.py" --slow
```

The default suite explicitly skips slow cases. `--slow` enables full-size, timing, cancellation and recovery checks; runtime depends on the machine. Use `--test NAME` for a focused selection. The checker runs tests with the selected interpreter and exits nonzero on failures or an empty selection.

The 192-test default suites passed with these Linux runtime combinations:

| Python | NumPy | SciPy | Pillow | Result |
| --- | --- | --- | --- | --- |
| 3.14.4 | 2.5.5 | 1.18.1 | 13.0.0 | 182 passed, 10 slow skips |
| 3.10.22 | 2.2.6 | 1.15.3 | 12.3.0 | 181 passed, 10 slow skips plus one historical-environment skip |

Separate slow checks covered all ten slow methods, including the four historical PNG references, on the unchanged render/painting math. Historical byte assertions require their recorded numerical/PNG environment; other environments retain portable behavior checks. Requirements declare dependency floors; the table does not certify every intermediate library combination. Independent copies in paths with spaces and native/Python fallback stage outputs were also verified.

These checks establish specific source/runtime behavior. Fresh agent discovery, trigger classification and a complete visual workflow for every supported client are separate checks; [frozen evaluation cases](evals/cases.md) are criteria, not universal client-acceptance claims.

The renderer has visible limits: simplified/repetitive geometry, terrain intersections, undersampled materials, approximate sky/water/lens effects and coarse brushwork can remain. Inspect underlying renders before trying to fix a modelling defect with more strokes. See [scene limitations and fixes](references/limits.md#scene-limitations). Same-runtime determinism is tested; cross-version/core/platform identical pixels and general photorealism are not promised.

## License

Published by **Dan Sasser** under the [MIT License](LICENSE). It covers this folder's code, instructions, documentation, bundled scene sources and gallery images. Third-party dependencies retain their own licenses.
