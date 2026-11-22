"""Synthetic worst-case scene for calibrating resource limits. Not a picture anyone wants.

An island (one dense terrain grid) covered in blade tufts, in a sea with reflections, lit by the
sun and one lamp. The mesh size is set by two environment variables so one file serves every level:

    MESHPAINT_STRESS_VERTICES   total vertices wanted   (default 1,000,000)
    MESHPAINT_STRESS_TRIANGLES  total triangles wanted  (default 1,000,000)

A grid costs 1 vertex per 2 triangles and a blade costs 3 vertices per triangle, so the two are mixed
to land on both numbers (to within a few thousand).
"""
import os

import numpy as np
from meshpaint.geometry import Camera
from meshpaint.mesh import MeshSet, grid_mesh, scatter, blades
from meshpaint.scene import Scene, Light, Glow
from meshpaint.util import noise, scene_rng

LAND, BLADE = 2, 3
WANT_V = int(os.environ.get("MESHPAINT_STRESS_VERTICES", 1_000_000))
WANT_T = int(os.environ.get("MESHPAINT_STRESS_TRIANGLES", 1_000_000))


def ground(x, y):
    d = np.hypot(x, y - 260)
    return (26.0 * np.clip(1 - (d / 240.0) ** 2, -0.02, 1) + 3.0 * (noise(x, y, 40) - 0.5) * (d < 235)).astype(np.float32)


def build():
    g2 = max((3 * WANT_T - WANT_V) / 5.0, 4.0)             # grid vertices
    g = max(int(round(np.sqrt(g2))), 2)
    nb = max(int(WANT_T - 2 * (g - 1) ** 2), 0)            # blade triangles
    per = 20
    tufts = nb // per
    ms = MeshSet()
    ms.add(*grid_mesh(np.linspace(-250, 250, g), np.linspace(10, 510, g), ground, None), LAND, "up")
    if tufts:
        rng = scene_rng("stress")
        a = rng.uniform(0, 2 * np.pi, tufts); r = 225 * np.sqrt(rng.uniform(0, 1, tufts))
        x, y = r * np.cos(a), 260 + r * np.sin(a)
        pts = np.stack([x, y, ground(x, y)], 1)
        ms.add(*scatter(pts, *blades(n=per, height=1.2, width=0.2, rng=rng), scale=rng.uniform(0.6, 1.5, tufts), jitter=0.03, rng=rng), BLADE, "up")

    def shade(gb):
        n = len(gb.x)
        alb = np.zeros((n, 3), np.float32); gloss = np.zeros(n, np.float32); emit = np.zeros((n, 3), np.float32)
        m = gb.mat == LAND
        alb[m] = (0.18 + 0.2 * noise(gb.x[m], gb.y[m], 3.0))[:, None] * np.array((0.9, 0.8, 0.6), np.float32)
        gloss[m] = 0.1
        m = gb.mat == BLADE
        alb[m] = (0.10 + 0.25 * noise(gb.x[m], gb.y[m], 0.7))[:, None] * np.array((0.6, 0.9, 0.4), np.float32)
        return alb, gloss, emit

    lamp = (0.0, 260.0, 40.0)
    return Scene(meshes=ms, camera=Camera(eye=(0, -120, 14), bearing=0.0, pitch=-1.0, focus=300.0),
                 shade=shade, look="sunset", sea=True, regions={LAND: 2, BLADE: 3}, stroke_angle={2: 0.0, 3: 1.4},
                 no_flip=(LAND,), lights=[Light(pos=lamp)], glows=[Glow(pos=lamp)])
