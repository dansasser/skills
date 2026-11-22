# Writing a scene file

A scene file executes trusted Python with the caller's permissions; resource supervision is not a security sandbox. A scene file is a Python module with a `build()` function that returns a `Scene`. Run it through the launcher, which makes the `meshpaint` package available for import. The lighthouse demonstrates buildings, lights and coastal geometry. The moor demonstrates ground cover and mesh scattering. [Lighthouse source](../assets/scenes/lighthouse.py) · [Moor source](../assets/scenes/moor.py)

## Contents

- [Minimal runnable scene](#minimal-runnable-scene)

- [Coordinates and scale](#coordinates-and-scale)
- [Camera](#camera)
- [Meshes](#meshes)
- [Noise and shaping helpers](#noise-and-shaping-helpers)
- [Random numbers and seeds](#random-numbers-and-seed)
- [Ground detail](#ground-detail)
- [Rock faces](#rock-faces)
- [Material shading and bump](#materials-shade-and-bump)
- [Paint regions](#paint-regions)
- [Lights and glows](#lights-and-glows)
- [Looks](#looks)
- [Scene settings](#the-scene)
- [Modelling advice](#modelling-advice)

## Minimal runnable scene

Save this as `workspace/minimal.py` in a terminal checkout after the README setup. It makes a small ground patch with one material; it is an API example rather than a finished composition.

```python
import numpy as np
from meshpaint.geometry import Camera
from meshpaint.mesh import MeshSet, grid_mesh
from meshpaint.scene import Scene


def build():
    meshes = MeshSet()
    axis = np.linspace(-100, 100, 41)
    meshes.add(*grid_mesh(axis, axis, lambda x, y: np.ones_like(x),
                          drop_below=None), 2, "up")

    def shade(g):
        n = len(g.x)
        albedo = np.broadcast_to((0.20, 0.13, 0.07), (n, 3)).copy()
        return albedo, np.zeros(n), np.zeros((n, 3))

    return Scene(meshes=meshes,
                 camera=Camera(eye=(0, -40, 12), bearing=0, pitch=-8,
                               focal=300, focus=60, aperture=0),
                 shade=shade, sea=False, size=(480, 300),
                 regions={2: 2}, default_region=2, no_flip=(2,))
```

```sh
.venv/bin/python scripts/run.py workspace/minimal.py --stage preview --out workspace/out/minimal
```

Open `workspace/out/minimal_preview.png`. The complete [lighthouse](../assets/scenes/lighthouse.py) and [moor](../assets/scenes/moor.py) sources show the API used for detailed scenes. Run scenes through the launcher, which seeds the engine and applies limits before calling `build()`.

## Coordinates and scale

Z is up. Units are metres. The sea, if enabled, is the plane z = 0. A bearing is degrees clockwise from +Y when seen from above, so bearing 0 looks along +Y and bearing 90 looks along +X.

## Camera

```python
from meshpaint.geometry import Camera
Camera(eye=(0, 0, 13.0), bearing=12.0, pitch=3.7, focal=1537.0, focus=185.0, aperture=1.0)
```

- `eye`: camera position. Height above the sea matters: 1.7 is standing on a beach, 13 is a low clifftop.
- `bearing`, `pitch`: where it points. Pitch up a few degrees puts the horizon below centre.
- `focal`: focal length in delivery-image pixels, multiplied by the working scale. At the default 1600-pixel width, 1537 gives about a 55 degree horizontal view. Larger is more telephoto. When changing `Scene.size`, adjust `focal` too if you want to preserve the field of view.
- `focus`: distance in metres to the sharp plane. Set it to the subject's distance.
- `aperture`: depth-of-field strength, a plain multiplier with no unit (1 is the default). Blur grows with `abs(1/distance - 1/focus) * 48 * aperture` and is capped, so with focus at 200 m everything nearer than about 40 m is at full blur. If near ground matters, lower `aperture` (0.3 is subtle, 0 is sharp everywhere) or focus closer.

To place something on screen: an object at horizontal angle `a` from the view direction and distance `d` appears `focal * tan(a)` pixels from centre, and a height of `h` metres covers `focal * h / d` pixels.

## Meshes

```python
from meshpaint.mesh import MeshSet, grid_mesh, lathe, quad, tri, beam, scatter, blades
ms = MeshSet()
ms.add(V, F, material, orient, attr=None)
```

- `V`: (n, 3) vertices. `F`: (m, 3) triangle indices into `V`.
- `material`: one integer id for the whole mesh, or an array with one id per triangle. Use ids from 2 upward; you choose them. Material IDs and paint-region IDs are separate: map materials to validated paint regions in the `Scene`.
- `orient`: `"up"` for terrain, or a point `(x, y, z)` inside the object so normals face away from it.
- `attr`: optional one number per vertex, interpolated and handed to `shade` (for example height fraction up a tower, used to place bands and windows).

Builders:

- `grid_mesh(xs, ys, height_fn, drop_below=-0.4)`: terrain patch from a function `h(x, y)` that takes and returns arrays. Returns `(V, F)`. Triangles entirely below `drop_below` are removed, so land that dips under the sea disappears into it; pass `drop_below=None` in a scene with no sea. Choose the step so triangles are a few pixels wide: about 0.7 m at 200 m distance, 0.3 m at 60 m. Use separate patches at different steps for near and far ground.
- `lathe(cx, cy, profile, seg=56)`: anything round about a vertical axis (towers, tanks, silos, domes, pillars). `profile` is a list of `(radius, z, material)` from bottom to top; each material applies to the band above its point. Returns `V, F, M` with per-triangle materials: `ms.add(V, F, M, (cx, cy, mid_z))`. `seg` is the number of sides; use 8 to 12 for small objects you make hundreds of.
- `quad(p0, p1, p2, p3)` and `tri(p0, p1, p2)`: flat pieces for walls, roofs, windows. Each returns `(V, F)`, so call as `ms.add(*quad(...), MAT, centre)`. Put windows as separate quads 5 cm in front of the wall.
- `beam(p0, p1, width)`: a straight four-sided bar between two points, for legs, struts, rails, posts and masts. Returns `(V, F)`. Make thin parts thicker than life: anything under about 3 pixels wide at full size is smeared by the brushwork.

`lathe` returns no `attr`. To use one (for example height fraction up a tower), compute it from the vertices: `V, F, M = lathe(...); ms.add(V, F, M, centre, attr=(V[:, 2] - z0) / height)`.

A ruined or broken wall cannot be made with `lathe`, which always gives complete rings. Build it cell by cell, leaving out the cells that have fallen, with an inner and an outer face and with faces across the wall's thickness wherever it is broken (top, window jambs, the sides of a breach); without those it looks like paper. Give the broken faces their own material: a course pattern keyed on angle or height turns into stripes on them. Use cells of about 0.2 m so the broken edge is not blocky. You can build your own `V` and `F` arrays for the whole wall and add them in one call. For a window you can see sky through, the far opening must lie on the line of sight through the near one, which is not the opposite side of the tower unless the camera looks along a diameter.

A height function must return finite numbers everywhere; `grid_mesh` raises an error otherwise. The usual cause is a root or power of a value that dips slightly below zero, such as `np.sqrt(np.sin(np.pi * t))`: clip first, `np.sqrt(np.clip(np.sin(np.pi * t), 0, 1))`.

A terrain patch must not end inside the frame while still above water, or its cut edge shows as a straight wall. Either extend it past the frame or subtract a term that sinks it below z = 0 before the patch boundary.

One terrain patch can carry several materials: compute an id per triangle from where it lies, for example `c = V[F].mean(1); M = np.where(on_the_island(c[:, 0], c[:, 1]), ROCK, SAND)`, and pass `M` as the material. This avoids a rectangular material boundary at the patch edge.

Where a fine patch overlaps a coarse one, the coarse one pokes through because its triangles cut corners. Lower the coarse patch's height inside the fine patch's footprint (a `smooth` window on x and y) so only the fine one is visible there. Lower it by more than the steepest slope times the coarse step: a metre or two on rolling ground, 10 m or more under a cliff. The fine patch must cover the whole footprint of any feature it is there for; where a cliff or talus runs off onto the coarse patch it shows as facets.

To stand an object on terrain, evaluate the height function at its position and start the object slightly below that height.

## Noise and shaping helpers

From `meshpaint.util`. All take arrays of world coordinates.

- `noise(x, y, scale)`: smooth values in 0..1. Features are about `2 * scale` metres across. Subtract 0.5 to centre it.
- `ridged(x, y, scale)`: the same noise folded into thin bright ridges. Raise it to a power for thinner lines. Good for cracks, ribs, gullies.
- `terrace(h, step, tilt)`: snaps a height toward ledges `step` metres apart; `tilt` (for example `0.1 * x + 0.05 * y`) slopes the strata. This is what makes rock read as rock.
- `smooth(a, b, x)`: smoothstep from `a` to `b`; works with `a > b` for a falling edge.
- `lerp(colour_a, colour_b, t)`: blend two RGB colours by an array `t`.
- `tex("gx" or "gy", x, y, scale)`: gradient of the noise, for bump relief.
- `blur(array, sigma)`: Gaussian blur of a 2D array, for softening a mask or a height map you built yourself.

Offset the coordinates (`noise(x + 300, y, s)`) to get an independent pattern. For a pattern on a vertical face, feed height into one coordinate: `noise(x + 0.6 * y, z * 6, 12)` gives horizontal strata; `noise(x, z * 0.07, 1.5)` gives vertical streaks.

A typical cliff: compute `d`, the distance inside the coastline (positive on land); perturb `d` with noise for a ragged edge; set `cl = smooth(0, 1, d / 14) ** 0.65` so height rises steeply near the edge and flattens on top; add ridged and terraced detail weighted by `sin(pi * cl)` so it lands on the face and not the plateau.

## Random numbers and seed

`--seed N` (default 11) seeds the engine's procedural work: the noise field, so terrain and sky change with it, and every random draw the engine makes. Keep the same seed, scene, render settings and runtime when comparing revisions. Arbitrary external randomness in scene code is not seeded automatically. Cross-version numerical differences can change pixels and painted stroke placement.

In a scene file, take random numbers from `meshpaint.util.scene_rng(name)`. It returns a numpy generator that depends only on the seed and on `name`, so the scene is repeatable and using it changes nothing the engine draws. Do not use `numpy.random` unseeded, and do not draw from the engine's own generator.

```python
from meshpaint.util import scene_rng
rng = scene_rng("boulders")
x, y = rng.uniform(-50, 50, 200), rng.uniform(10, 150, 200)
```

## Ground detail

Open ground that fills a third of the frame is the hardest part of a landscape. Flat colour reads as a painted floor, and lumps in the height field read as tents. The moor combines three techniques. [Moor source](../assets/scenes/moor.py)

**1. Patches at several steps.** A fine patch near the camera (0.15 m), then coarser ones further out. Lower each coarser patch inside the finer one's footprint so it cannot poke through.

**2. A layered ground skin** in `shade`, each layer at its own scale:

| Layer | Scale | Example |
|---|---|---|
| Base soil | about 9 m | `lerp(peat, dry_earth, noise(x, y, 9))` |
| Plant cover in patches | 3 to 6 m | grass colour where `noise(x, y, 3.2)` is high |
| A second plant or mineral | about 9 m | heather, moss, gravel |
| Grit and blade speckle | 0.1 m, faded with distance | multiply by `0.7 + 0.6 * noise(x, y, 0.11)` |
| Broad light and dark drifts | 20 m | multiply by `0.8 + 0.4 * noise(x, y, 22)` |
| Damp hollows | 18 m | darker and slightly glossy where a low-frequency noise is low |

**3. Many small scattered meshes.** `scatter` places one template at many points and returns a single mesh:

```python
from meshpaint.mesh import scatter, blades, lathe
grass = blades(n=18, height=0.30, width=0.014, lean=0.55, spread=0.09, rng=rng)   # one tuft
pts = np.stack([x, y, ground(x, y) - 0.01], 1)                                  # seat them just into the ground
ms.add(*scatter(pts, *grass, scale=rng.uniform(0.6, 1.5, len(x)), jitter=0, rng=rng), GRASS, "up")
```

- `scatter(points, V, F, scale=1.0, yaw=None, jitter=0.0, rng=None)`: `scale` is one number or one per point; `yaw=None` turns each copy randomly; `jitter` moves every vertex by that fraction of the copy's size, which stops copies looking like copies. All copies share the `orient` you pass to `add`, so do not list a scattered material in `no_flip`.
- Use `jitter=0` when scattering blade tufts: jitter moves every vertex, which fattens thin blades into wedges.
- `blades(n, height, width, lean, spread, rng)`: a tuft of `n` thin triangular blades. `height`, `width` (of one blade at its base) and `spread` (radius the blades are rooted within) are metres. `lean` is a ratio: how far a tip tips outward, as a fraction of the blade's height (0 upright, 1 at 45 degrees). `scatter`'s `scale` multiplies all of it, so a tuft built 0.3 m tall and scattered at scale 1.5 stands 0.45 m. For fine grass keep `width` near 0.015 and `lean` near 0.5; wide, strongly leaning blades read as agave or yucca. Use it for grass, reeds, heather and twiggy scrub. Solid lumps do not read as plants: a jittered cone looks like a crystal, a dome looks like a boulder.
- For stones, a 5 to 7-sided `lathe` with a lumpy profile works; use `jitter` around 0.06; more than 0.1 gives crumpled shards, especially when backlit; stack the same template in shrinking rings for a cairn or a wall.

A low sun (under about 10 degrees) barely lights level ground, which then goes dim and takes the sky's blue; strong ground bump turns into dark streaks. Halve bump strength on flat ground at low sun and keep the ground albedo on the bright side.

Colour scattered plants by height above the ground (dark at the root, pale at the tip) and vary the tip colour slowly across the field. Leave clearings: thin the scatter with a noise threshold so soil and stones show through. The moor uses about 20,000 tufts. The vertex and triangle counts in the usage report show the cost of changing the planting density.

## Rock faces

A rock face reads as rock when its relief is real and its light is from the side.

- **Shape first.** Perturb the distance-inside-the-edge value at three scales before turning it into height, so the plan outline has bays, buttresses and notches. Add `terrace` so strata are ledges that catch light and cast shadow, not painted bands. Weight ledges and ribs by `np.clip(np.sin(np.pi * cl), 0, 1)` so they sit on the face.
- **Skin second.** Colour bands should follow the same height coordinate as the ledges. Keep vertical streaks (run-off, varnish) irregular: gate them with a second noise so they come and go, and never let them be the only pattern on the face.
- **Scree.** Model fallen blocks as scattered stones of varied size, clustered below gullies, not spread evenly.
- **Bump coordinates.** For bump or skin patterns on a large face, do not feed `x + z` into both noise axes: it draws diagonal hatching. Use the along-face coordinate for one axis and height for the other.

## Materials, shade and bump

```python
def shade(g):
    return albedo, gloss, emission     # (n, 3), (n,), (n, 3)
```

`g` describes every visible surface point: `g.x, g.y, g.z` (world position), `g.N` (normals, n x 3), `g.mat` (material ids), `g.attr` (0 for meshes added without `attr`), `g.dist` (metres from camera), `g.foc`. Fill the three arrays by material with boolean masks: `m = g.mat == ROCK; albedo[m] = ...`.

- `albedo`: base colour in linear light. Real surfaces are darker than you expect: dark rock 0.05 to 0.15, sunlit sandstone 0.25 to 0.4, white paint 0.8. Never 1.0.
- `gloss`: 0 to 1, how much sky the surface mirrors at a grazing angle. Wet rock 0.4 to 0.8, slate 0.2, dry rock 0.
- `emission`: light the surface gives off. Lit windows around (5, 3, 0.9); a lamp behind glass (15, 10, 4).

Optional `bump(g)` may change `g.N` in place to add relief finer than the mesh. `bump` runs before `shade`, so `g.N` in `shade` is the bumped normal.

Fade fine detail, in `bump` and in `shade`, so it never varies faster than a pixel: multiply it by `g.fade(feature_size)`, which is 1 where a feature of that size in metres covers several pixels and falls to 0 where it would be smaller than one. It allows for distance and for grazing view angles, which matter on level ground seen from a low camera; detail left unfaded there turns into horizontal streaks.

## Paint regions

Strokes are masked to a region so edges between surfaces stay crisp. Region 0 is sky and 1 is sea. Map your materials to regions **2 through 127 inclusive** with `regions={ROCK: 2, ...}`; unlisted materials go to `default_region`. Give each large distinct surface its own region and lump small built parts together. `stroke_angle={region: radians}` is the stroke direction used where the image has no clear grain: 0 is horizontal on screen and `pi / 2` is vertical (about 1.25 for cliffs, `pi / 2` for upright structures, 0 for flat ground).

Both mapping values and `default_region` must be Python or NumPy integers; booleans, floats, strings and out-of-range IDs are rejected after `build()` and before render-buffer allocation. This validation does not bound arbitrary arrays created inside `build()`.

Use dense low IDs. Region masks allocate through the highest rendered ID, including unused intermediate slots. At a 3840 × 2400 canvas, ID 127 implies 128 planes: two float32 mask sets alone require 9,000 MiB, before other buffers. A valid region ID can therefore still exceed the chosen memory ceiling. Give large distinct surfaces separate regions and group small related parts.

`no_flip` lists terrain materials. Other materials have their normals turned to face the camera, which lets thin two-sided pieces such as roofs and rails light correctly.

## Lights and glows

- `Light(pos, colour=(1.0, 0.72, 0.36), power=55.0, skip_mats=(), transparent_mats=())`: a point light. Its shadow map looks straight down, so it suits a lamp above the ground. `transparent_mats` are materials that do not block it (the glass around it); `skip_mats` are never lit by it (its own housing).
- `Glow(pos, colour=(1.0, 0.78, 0.42), halo=((0.9, 9), (0.22, 50), (0.05, 230)), beams=(), starburst=(0.55, 16.0))`: screen-space glare. Halo entries are `(strength, radius_in_pixels_at_1600_width)`; starburst is `(strength, length_in_pixels)` or `None`. `beams=((angle, strength), ...)` draws shafts at screen angles in radians.

Import both from `meshpaint.scene`. The sun and sky come from the look; `lights` supplies additional point lights and `glows` supplies screen-space glare.

## Looks

Offsets are measured from the view direction: negative puts the sun to the left, positive to the right. Under about 25 degrees the sun is in frame and the subject is backlit; around 90 it is side light; beyond 90 the sun is behind the camera and lights the faces you see, but shadows fall away from you and show less.

`look="sunset"` (sun offset -20, elevation 0.9) or `"golden_hour"` (offset -104, elevation 7) in the `Scene`, or a custom `look=dict(sun_offset=60, sun_elev=5)`. Override on the command line with `--look NAME` or `--sun OFFSET ELEV`. OFFSET is degrees from the view direction (negative is left of frame) and ELEV is degrees above the horizon. For a level camera and a 1600-wide frame, the horizontal half-angle is about 27 degrees at focal length 1537 and about 18 degrees at focal length 2400. Elevations from about 1 to 10 degrees are what the sky model was tuned for; outside that range, check the preview carefully.

## The Scene

Required fields are `meshes`, `camera` and `shade`. Optional `bump` defaults to `None`; lights and glows default to empty lists, regions and stroke-angle maps to empty dictionaries, `default_region` to 2 and `no_flip` to an empty tuple. The default look is `golden_hour`.

```python
from meshpaint.scene import Scene, Light, Glow
return Scene(meshes=ms, camera=cam, shade=shade, bump=bump, look="golden_hour", sea=True,
             regions={ROCK: 2}, default_region=3, stroke_angle={2: 1.25, 3: 1.57},
             no_flip=(ROCK,), lights=[...], glows=[...])
```

`size=(1600, 1000)` on the `Scene` sets the delivered image size, written `Scene.size`; the canvas is that size times `--scale`. The canvas, the scene's total vertices and triangles, and the brushstrokes must all fit the run's resource level (`--level`). A mesh helper raises `meshpaint.limits.LimitExceeded` before building an oversized mesh. Let that exception reach the launcher. [Resource levels](limits.md#resource-levels)

Water and lens settings on the `Scene`:

- `reflections` (default on): the sea mirrors land and structures as well as the sky. Reflections are sharpest in calm water and smear vertically as `sea_state` rises. A reflection needs room: it appears below its object, about as tall as the object looks, so leave water in front of the subject.
- `clouds`: cloud cover. 0 is a clear sky, 1 (default) broken cloud, 1.5 heavy. Lower it when the cloud layer competes with the subject.
- `sea_state`: wave height. 0 is mirror calm, 0.3 a calm bay, 1 (default) a breezy open sea, 1.5 rough. Calm water gives long clean reflections and a narrow sun path.
- `foam`: shore foam, 0 to 1 (default 1). Calm water carries little foam whatever this is set to.
- `lens_ghosts`: with the sun in frame the lens adds three faint ghost discs on the line from the sun through the image centre, on the far side of centre. Set `False` if they land on the subject, or move the subject off that line.

Set `sea=False` for a scene with no water; then model ground everywhere the camera can see below the horizon, or the sky will show through. In practice: a fine patch near the camera, coarser patches further out to several kilometres (a 30 m step is enough beyond 2 km), and distant hills or buttes that rise above the horizon line to hide the far edge. Aerial haze is applied automatically with distance and blends far ground toward the horizon colour, so distant patches need shape but little detail.

Use blade tufts to model grass and scrub. [Ground detail](#ground-detail)

## Modelling advice

- Build the terrain first and get the preview composition right before adding structures.
- Keep the height function and the object positions in one place so objects can be seated on the ground.
- If the preview is too dark or too bright, change the look or set `exposure=` on the `Scene` (the default is 0.98 with the sun on the horizon, falling to 0.80 with it 10 degrees up).
- For a new kind of object, combine the builders: a windmill is a lathe for the tower plus thin quads for sails; a bridge is quads; a boat hull is a few quads and tris.
