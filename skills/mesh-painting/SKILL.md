---
name: mesh-painting
description: Create editable landscapes, seascapes and building scenes with code alone, using triangle meshes, procedural materials, lighting and brushstrokes. Use for mesh painting, scenes rendered from meshes and brushstrokes, or realistic or painterly images made without photographs or an image-generation model. Exclude logos, icons, charts, diagrams, text placeholders, photo editing, video and requests for an image-generation service.
license: MIT
metadata:
  version: "0.2.0"
---

# Mesh Painting

Build the requested scene as editable Python source, render it, then paint the rendered target. Inspect the preview, every render tile and every painted tile. Use procedural geometry and colour; do not call an image-generation service or download photographs or textures for the scene. Explain unavailable Linux execution or PNG viewing before starting; never claim to have inspected unseen pixels.

## Runtime and paths

Read [setup and commands](README.md). Use Linux, Python 3.10+, NumPy, SciPy, Pillow and `findmnt`; current-namespace procfs, forking, signals, locking and enforced address-space limits must work. A C compiler enables the faster core; the Python core is slower. Use one selected project interpreter for dependency installation, checks and rendering.

Resolve the skill root from the loaded `SKILL.md`, then locate `scripts/run.py`, requirements, references and assets relative to that root. In the command pattern below, `SKILL_DIR` is that resolved root and `PYTHON` is the selected project's interpreter. Quote both. Keep the environment, authored scenes and generated output in the user's project; do not assume the current directory is the skill directory or edit bundled assets in place.

Scene modules are trusted Python running with the user's permissions. Resource supervision does not sandbox file or network access. Review supplied code and use the documented engine interfaces, NumPy and the standard library for authored scenes. Install dependencies within the user's authorization and environment policy.

## Workflow

1. **Compose the requested subject.** Establish viewpoint, proportions and time of day. Choose `sunset` for an in-frame sun/backlighting or `golden_hour` for side light; `--sun OFFSET ELEV` gives a custom direction. Keep the brief's subject rather than substituting a bundled example.
2. **Author an editable scene.** Read the [scene API](references/scene-api.md) and its minimal `build()` example. Use metres, Z up, procedural materials and `scene_rng(name)`. Paint-region IDs are integers 2–127; sky/water reserve 0/1. Sparse IDs use compact masks. The [lighthouse](assets/scenes/lighthouse.py) and [moor](assets/scenes/moor.py) are optional modelling references, not the only supported subjects. Copy an example into the workspace before changing it.
3. **Preview and revise.** Run `--stage preview`; open `_preview.png` and check framing, scale, lighting and exposure. Preview uses scale 1 but still builds all geometry. Correct the source and repeat until the composition fits the brief.
4. **Render and inspect every tile.** Run `--stage render`. Open `_render.png` and every actual emitted render tile at original resolution. The default `3x2` grid emits six tiles; another grid can emit a different count. Check foreground/background, silhouettes, seams, stretched triangles, floating/buried objects, speckle and material detail. Change the scene or grid when needed, then inspect the complete new render set.
5. **Paint the inspected scene.** Keep the same source, seed, camera, look, sun, level and scale. `--stage paint` rerenders internally; it does not read the earlier PNG. Default `legacy-seven` is the preserved seven-pass recipe. Use `--paint-policy adaptive-v1` only when image-dependent refinement is wanted; read [painting controls](references/painting.md). Adaptive work can stop at a quality target, diminishing returns or a guard; fewer strokes do not guarantee less time or better fidelity. Its `photographic`/`painterly` finish labels guide brushwork and do not guarantee realism.
6. **Inspect and deliver.** Open the delivery image, `_full.png` and every painted tile. For an underlying render defect, correct the scene and repeat render inspection before painting again. Deliver the editable scene with the delivery/full images, settings and outcome; label any partial unfinished.

Run the launcher from the caller's workspace using workspace-relative scene/output arguments:

```sh
"$PYTHON" "$SKILL_DIR/scripts/run.py" workspace/scene.py --stage preview --look golden_hour --seed 11 --out workspace/out/scene
"$PYTHON" "$SKILL_DIR/scripts/run.py" workspace/scene.py --stage render --look golden_hour --seed 11 --out workspace/out/scene
"$PYTHON" "$SKILL_DIR/scripts/run.py" workspace/scene.py --stage paint --look golden_hour --seed 11 --out workspace/out/scene
```

For a localized revision, preserve earlier source and outputs under versioned names. Keep the camera, seed and unrelated geometry/materials unchanged unless the requested edit requires a change. Compare source diffs and matching stages, then inspect the revised images and tiles. Check the recorded source hash and resolved settings when comparing runs.

## Modelling cues

- Establish silhouettes and familiar scale before adding material detail. Seat objects with the terrain height function.
- Give cliffs bays, buttresses and notches; use `terrace` and `ridged` for ledges and fissures. Layer broad colour, grain, stains and wet bands.
- Match mesh step to visible relief; fade subpixel material detail with `g.fade`. Lower coarse patches beneath fine patches to reduce intersections.
- Side light catches ridges; backlighting emphasizes silhouette. Use available geometry for unsupported features and disclose visible limitations.

## Limits and interruption

Read [limits and runtime boundaries](references/limits.md). Keep `standard` unless the task explicitly calls for another profile. Delivery/working dimensions, geometry, auxiliary maps, attempted strokes, address space, time and disk admission have distinct checks. Do not automatically raise limits or retry a stopped job. Explain the limiting resource and propose a specific reduction or an explicit profile choice. Low image scale does not reduce the moor's geometry counts.

Run in the foreground when the tool timeout permits. Ctrl+C requests cancellation. For a background job use the same interpreter, launcher and output prefix with `--stop`. Keep persistent lock/control files in place; active jobs with overlapping actual output sets are refused while disjoint jobs can run concurrently.

Pair the command exit status with the report/run identity: an early refusal can leave an older report unchanged. Read `_usage.json` and the authoritative `_manifest.json` after each stage; see [outputs, provenance and recovery](references/outputs.md). A preserved flat PNG does not prove the latest run succeeded. A complete stage needs its verified generation/receipt. `_partial.png` is a best-effort checkpoint before final relief/grain, not a finished painting; there is no full-state resume. Reports or new partials can be absent after startup, forced termination or output failures. Preserve evidence and state failures, stops, interruptions and skipped inspection honestly.

## Reporting

Describe the actual scene, completed stage, selected policy, attempted/accepted stroke counts, terminal reason, elapsed time, settings and runtime/core. Explain that relief shading and grain follow brush painting. Resizing creates the delivery image: downsampling at normal supersampling scales, or enlarging a smaller low-scale canvas. Use the report's source/config hashes and provenance for comparisons; it records resolved settings and deterministic policy decisions, not a promise of cross-platform identical pixels.

State visible weaknesses and whether every emitted tile was inspected. Describe images as made from code without photographs or an image model; do not promise photorealism, a fixed stroke count, seven passes for every policy, automatic recovery or an unverified client/runtime.
