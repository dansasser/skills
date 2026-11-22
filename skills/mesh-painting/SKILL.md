---
name: mesh-painting
description: Paint realistic or painterly landscapes, seascapes and buildings with code alone, using 3D triangle meshes, procedural materials, lighting, shadows and millions of brushstrokes. Use when asked to use mesh painting, render a scene from meshes and brushstrokes, or make a realistic scene without photographs or an image-generation model. Exclude logos, icons, charts, diagrams, text placeholders, photo editing, video and requests for an image-generation service.
license: MIT
metadata:
  version: "0.1.0"
---

# Mesh Painting

Build the requested scene with the bundled renderer and keep its Python source editable. Inspect the preview, every render tile and every painted tile. Use procedural geometry and colour throughout. If PNG viewing or the required Linux runtime is unavailable, explain that before starting.

## Runtime and paths

Use Linux, Python 3.10+, NumPy, SciPy, Pillow and `findmnt` from util-linux. Procfs must describe the current PID namespace; forking, signals, file locking and enforced address-space limits must work. A C compiler enables the faster core. Review [setup, installation and commands](README.md) before creating the project environment. Use one selected interpreter for pip, checks and every stage.

Locate this skill's directory from the loaded `SKILL.md`, then resolve `scripts/run.py`, `requirements.txt` and assets relative to that directory. Do not assume the caller's working directory is the installed skill. Keep the environment, editable scene and outputs in the user's project. Copy the appropriate bundled scene into that workspace before editing it. Quote paths with spaces.

For a terminal checkout, the README supplies runnable relative commands. For an installed skill, invoke the resolved launcher with the selected project interpreter, passing workspace-relative scene and output paths. Do not copy machine-specific paths into scene code or shared instructions.

Scene modules execute trusted Python with the current user's permissions. The resource supervisor is not a file/network or malicious-code sandbox. Review supplied scene code; use only declared engine interfaces, NumPy and the standard library in authored scenes. Install dependencies within the user's existing authorization and environment policy.

## Workflow

1. **Compose.** Use the brief's subject, viewpoint and time of day. Choose `sunset` for an in-frame sun and backlighting, or `golden_hour` for side lighting and cast shadows. `--sun OFFSET ELEV` specifies a custom direction.
2. **Model.** Read the [scene API](references/scene-api.md). Copy [lighthouse.py](assets/scenes/lighthouse.py) for coast, structures and lamps, or [moor.py](assets/scenes/moor.py) for ground cover and stones. Work in metres, Z up. Use `scene_rng(name)` and keep dense region IDs from 2 through 127. Model unsupported features from available meshes and disclose visible limitations.
3. **Preview.** Run `--stage preview`, open `_preview.png`, and check composition, scale, light and exposure. Revise and preview until the scene matches the brief. Preview uses scale 1 and still builds all geometry.
4. **Render.** Run `--stage render`. Open `_render.png` and **every tile at original resolution**, covering foreground and background. The default 3 × 2 grid runs from `_render_tile_r1c1.png` through `_render_tile_r2c3.png`. Check seams, stretched triangles, floating/buried objects, speckle, blur and material detail. Change the tile grid if cuts divide the subject, then inspect the new complete grid.
5. **Paint.** After approving the final render, run `--stage paint` with exactly the same scene, seed, look, sun, level and scale. This stage rerenders internally. Open the delivery PNG, `_full.png` and every painted tile. If a defect comes from the underlying render, fix the scene and repeat the render inspection before painting again.
6. **Deliver.** Provide the editable scene, delivery PNG and full canvas. Identify the completed stage, actual stroke count, elapsed time, settings, interpreter/runtime and any visible weaknesses. Cite the usage report for outcome and resource figures. State that the image was made from code without photographs or an image model.

For revisions, preserve the earlier source and outputs with versioned names. Keep camera, seed and other settings unchanged unless the requested edit needs them to change. Compare matching stages and review the source diff for unintended changes. Record scene hashes and commands separately when provenance matters: the usage report does not capture every rendering setting.

## Modelling cues

- Shape silhouettes before adding skin detail. Give cliffs bays, buttresses and notches; use `terrace` and `ridged` for ledges and fissures.
- Layer broad colour variations, grain, stains and wet bands. Match mesh step to visible relief and fade subpixel material detail with `g.fade`.
- Side light around 60–110 degrees from the view catches ridges. Backlighting emphasizes silhouette.
- Use familiar doors, windows and rails to establish scale. A door is about 2 m high and an eye-level camera about 1.7 m.
- Seat objects using the terrain height function. Lower coarse patches under fine patches to prevent intersections.

## Limits and interruption

Keep `standard` unless the job calls for an explicitly chosen resource profile. Do not automatically increase limits or retry a stopped job. For exit 3, report the stage, limiting resource, requested/observed use and ceiling; propose a concrete reduction or ask whether to use a higher level. The moor needs standard geometry capacity even at low image scale. [Limits, reports and recovery](references/limits.md)

Run in the foreground when the tool timeout permits. Stop background work with the same launcher, interpreter and `--out` prefix plus `--stop`; Ctrl+C also requests cancellation. Same-prefix concurrent runs are refused. Keep the persistent `.lock` file in place.

Read `_usage.json` after each stage. Distinguish new outputs from preserved earlier images; label `_partial.png` unfinished. A report may be absent after startup or output-permission errors. Report failed or interrupted runs honestly and retain their evidence. The Python core is slower; use the README's bounded low-scale fallback to verify it and disclose its reduced detail.

## Reporting

Explain brushwork when useful: seven passes build the canvas, relief varies brightness by up to 7 percent, grain adds texture and downsampling creates the delivery image. Describe visible output and measured behavior rather than promising photorealism or claiming unseen tiles were inspected.
