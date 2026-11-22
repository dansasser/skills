"""Open moorland with a stone cairn. No sea. Shows ground detail and the scatter helper.

Ground that fills half the frame is the hardest part of a landscape to make convincing.
This scene uses the three things that help: terrain patches at several steps, a layered
ground skin, and many small scattered meshes (stones, scrub, grass tufts).
"""
import numpy as np
from meshpaint.geometry import Camera
from meshpaint.mesh import MeshSet, grid_mesh, lathe, scatter, blades
from meshpaint.scene import Scene
from meshpaint.util import smooth, lerp, norm, noise, ridged, tex, scene_rng

GROUND, STONE, SCRUB, TUFT = 2, 3, 4, 5
CAIRN = (9.0, 36.0)


def ground(x, y):
    """Rolling moor with a low rise under the cairn, and hills closing the horizon."""
    d = np.hypot(x, y)
    h = 5.0 * (noise(x, y, 70) - 0.5) + 1.6 * (noise(x + 200, y, 18) - 0.5) + 0.35 * (noise(x, y + 90, 3.0) - 0.5)
    h = h + 0.10 * (noise(x, y, 0.5) - 0.5) * smooth(60, 15, d)                      # small lumps, near only
    h = h + 4.5 * np.exp(-((x - CAIRN[0]) ** 2 + (y - CAIRN[1]) ** 2) / (2 * 22.0 ** 2))
    h = h + smooth(350, 2600, d) * (40 + 260 * noise(x, y, 900) + 60 * ridged(x + 500, y, 260))   # distant hills
    return h.astype(np.float32)


def window(x, y, x0, x1, y0, y1, margin):
    """1 inside the rectangle, falling to 0 over `margin` at its edges."""
    return smooth(x0, x0 + margin, x) * smooth(x1, x1 - margin, x) * smooth(y0, y0 + margin, y) * smooth(y1, y1 - margin, y)


NEAR, MID, FAR = (-26, 32, 3, 46), (-95, 115, 40, 210), (-850, 950, 190, 1450)


def build():
    ms = MeshSet()
    # Four terrain patches, fine to coarse. Each coarser one is lowered under the finer one's
    # footprint so its larger triangles cannot poke through.
    def duck(inner, margin, drop):
        return lambda x, y: ground(x, y) - drop * window(x, y, *inner, margin)
    ms.add(*grid_mesh(np.arange(NEAR[0], NEAR[1], 0.14), np.arange(NEAR[2], NEAR[3], 0.14), ground, None), GROUND, "up")
    ms.add(*grid_mesh(np.arange(MID[0], MID[1], 0.6), np.arange(MID[2], MID[3], 0.6), duck(NEAR, 3.0, 1.5), None), GROUND, "up")
    ms.add(*grid_mesh(np.arange(FAR[0], FAR[1], 4.0), np.arange(FAR[2], FAR[3], 4.0), duck(MID, 12.0, 4.0), None), GROUND, "up")
    ms.add(*grid_mesh(np.arange(-7000, 7500, 32.0), np.arange(1350, 9500, 32.0), duck(FAR, 90.0, 25.0), None), GROUND, "up")

    def on_ground(x, y, sink=0.0):
        return np.stack([x, y, ground(x, y) - sink], 1)

    # stones: one lumpy template, scattered as boulders and stacked as the cairn
    rng = scene_rng("moor")
    stone = lathe(0, 0, [(0.0, 0.0, STONE), (0.50, 0.08, STONE), (0.62, 0.34, STONE), (0.42, 0.60, STONE), (0.0, 0.70, STONE)], seg=7)[:2]
    n = 170
    bx, by = rng.uniform(-70, 90, n), rng.uniform(5, 170, n) ** 1.0
    keep = noise(bx + 400, by, 25) > 0.45                                        # boulders gather in patches
    bx, by = bx[keep], by[keep]
    ms.add(*scatter(on_ground(bx, by, 0.12), *stone, scale=rng.uniform(0.4, 1.4, len(bx)) ** 1.5 + 0.25, jitter=0.16, rng=rng), STONE, "up")
    layers = []
    for hgt in np.arange(0.0, 3.1, 0.26):                                         # the cairn: rings of stones, narrowing upward
        rad = 2.2 * (1 - hgt / 3.6)
        k = max(int(rad * 9), 3)
        a = rng.uniform(0, 2 * np.pi, k); r = rad * np.sqrt(rng.uniform(0.25, 1.0, k))
        px, py = CAIRN[0] + r * np.cos(a), CAIRN[1] + r * np.sin(a)
        layers.append(np.stack([px, py, ground(px, py) + hgt - 0.1], 1))
    cairn = np.concatenate(layers)
    ms.add(*scatter(cairn, *stone, scale=rng.uniform(0.40, 0.72, len(cairn)), jitter=0.13, rng=rng), STONE, "up")
    slab = lathe(0, 0, [(0.0, -0.3, STONE), (0.42, -0.3, STONE), (0.46, 0.6, STONE), (0.36, 1.7, STONE), (0.22, 2.25, STONE), (0.0, 2.35, STONE)], seg=5)[:2]
    ms.add(*scatter(on_ground(np.array([-8.0]), np.array([27.0])), *slab, yaw=[0.6], jitter=0.05, rng=rng), STONE, "up")

    # heather and grass: tufts of thin blades, not solid lumps
    heather = blades(n=34, height=0.42, width=0.07, lean=0.9, spread=0.30, rng=rng)
    n = 5200
    sx, sy = rng.uniform(-90, 110, n), 7 + 190 * rng.uniform(0, 1, n) ** 1.7
    keep = noise(sx + 50, sy, 9) + 0.25 * noise(sx, sy, 2.5) > 0.58
    sx, sy = sx[keep], sy[keep]
    ms.add(*scatter(on_ground(sx, sy, 0.03), *heather, scale=rng.uniform(0.6, 1.4, len(sx)), jitter=0.04, rng=rng), SCRUB, "up")
    grass = blades(n=18, height=0.30, width=0.014, lean=0.55, spread=0.09, rng=rng)
    n = 42000
    tx, ty = rng.uniform(-26, 34, n), 5.0 + 44 * rng.uniform(0, 1, n) ** 1.5
    keep = (noise(tx, ty, 4) + 0.3 * noise(tx + 9, ty, 0.9) > 0.56) & (noise(tx + 400, ty, 25) < 0.56)   # leave the boulder patches open
    tx, ty = tx[keep], ty[keep]
    ms.add(*scatter(on_ground(tx, ty, 0.01), *grass, scale=rng.uniform(0.6, 1.5, len(tx)), jitter=0.03, rng=rng), TUFT, "up")

    def bump(g):
        m = g.mat == GROUND
        for bs, ba in ((1.2, 0.20), (0.3, 0.28), (0.08, 0.22)):
            b = np.stack([tex("gx", g.x[m], g.y[m], bs), tex("gy", g.x[m], g.y[m], bs), np.zeros(m.sum(), np.float32)], 1) * ba
            g.N[m] = norm(g.N[m] + b * g.fade(bs)[m][:, None])
        m = g.mat == STONE
        for bs, ba in ((0.25, 0.35), (0.06, 0.25)):
            b = np.stack([tex("gx", g.x[m] + g.z[m], g.y[m], bs), tex("gy", g.x[m], g.y[m] + g.z[m], bs), tex("gx", g.y[m], g.z[m], bs)], 1) * ba
            g.N[m] = norm(g.N[m] + b * np.clip(g.foc * bs / (g.dist[m] * 6), 0, 1)[:, None])

    def shade(g):
        x, y, z, mat = g.x, g.y, g.z, g.mat
        alb = np.zeros((len(x), 3), np.float32); gloss = np.zeros(len(x), np.float32); emit = np.zeros((len(x), 3), np.float32)

        m = mat == GROUND                                   # layered ground skin
        xm, ym = x[m], y[m]
        near = g.fade(0.15)[m]                                           # fade fine detail with distance and grazing angle
        soil = lerp((0.040, 0.030, 0.024), (0.19, 0.14, 0.09), noise(xm, ym, 9))                 # 1. peat to dry earth
        grass = lerp((0.060, 0.075, 0.028), (0.36, 0.28, 0.11), 0.6 * noise(xm + 70, ym, 6) + 0.4 * noise(xm, ym, 0.9))   # 2. olive to straw
        heather = lerp((0.10, 0.045, 0.07), (0.22, 0.10, 0.13), noise(xm, ym + 30, 1.5))         # 3. heather
        cover = smooth(0.34, 0.58, noise(xm + 20, ym, 3.2) + 0.25 * (noise(xm, ym, 0.6) - 0.5))  # grass over soil, in patches
        a = soil + (grass - soil) * cover[:, None]
        hth = smooth(0.60, 0.72, noise(xm + 50, ym, 9) + 0.25 * noise(xm, ym, 2.5))
        a = a + (heather - a) * (0.55 * hth)[:, None]
        a = a * (0.72 + 0.56 * noise(xm, ym, 0.11) * near + 0.28 * (1 - near))[:, None]         # 4. grit and blade speckle
        a = a * (0.80 + 0.40 * noise(xm + 300, ym, 22))[:, None]                                 # 5. broad light and dark drifts
        damp = smooth(0.42, 0.30, noise(xm + 200, ym, 18))                                       # 6. hollows are darker and damp
        alb[m] = a * (1 - 0.45 * damp)[:, None]
        gloss[m] = 0.25 * damp

        m = mat == STONE                                    # grey gritstone with lichen and dark footings
        q = x[m] + y[m] * 0.7
        a = lerp((0.055, 0.052, 0.050), (0.24, 0.225, 0.20), 0.55 * noise(q, z[m] * 3, 0.7) + 0.45 * noise(x[m], y[m], 0.12))
        lichen = smooth(0.62, 0.74, noise(x[m] + z[m] * 2, y[m], 0.45))[:, None]
        a = a + (np.array((0.26, 0.24, 0.10), np.float32) - a) * 0.6 * lichen
        alb[m] = a * (0.55 + 0.45 * smooth(0.0, 0.25, z[m] - ground(x[m], y[m])))[:, None]

        m = mat == SCRUB                                    # heather: dark woody base, dusky purple tips
        up = smooth(0.0, 0.40, z[m] - ground(x[m], y[m]))
        tone = lerp((0.035, 0.030, 0.022), (0.24, 0.11, 0.16), up)
        green = lerp((0.035, 0.045, 0.020), (0.13, 0.15, 0.06), up)
        alb[m] = lerp(tone, green, smooth(0.40, 0.60, noise(x[m], y[m], 3.0))[:, None].repeat(1, 1)[:, 0]) * (0.65 + 0.7 * noise(x[m] * 5, y[m] * 5, 0.3))[:, None]

        m = mat == TUFT                                     # grass: dark at the root, straw at the tip, clump to clump variation
        up = smooth(0.0, 0.30, z[m] - ground(x[m], y[m]))
        dry = noise(x[m], y[m], 5.0)
        tip = lerp((0.16, 0.19, 0.06), (0.42, 0.33, 0.13), dry)
        alb[m] = lerp(np.broadcast_to(np.array((0.030, 0.034, 0.014), np.float32), tip.shape), tip, up) * (0.7 + 0.6 * noise(x[m] * 4, y[m] * 4, 0.3))[:, None]
        return alb, gloss, emit

    eye_z = float(ground(np.array([0.0]), np.array([0.0]))[0]) + 1.7
    return Scene(
        meshes=ms,
        camera=Camera(eye=(0, 0, eye_z + 0.5), bearing=11.0, pitch=0.5, focal=1537.0, focus=36.0, aperture=0.3),
        shade=shade, bump=bump, look="golden_hour", sea=False,
        regions={GROUND: 2, STONE: 3, SCRUB: 4, TUFT: 4}, default_region=2,
        stroke_angle={2: 0.0, 3: 1.2, 4: 1.4},
        no_flip=(GROUND,),
    )
