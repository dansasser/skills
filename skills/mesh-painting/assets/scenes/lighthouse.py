"""Lighthouse on a headland, with foreground rocks. Units are metres, Z up.

A scene file builds meshes, says how each material looks (shade), and returns a Scene.
"""
import numpy as np
from meshpaint.geometry import Camera
from meshpaint.mesh import MeshSet, grid_mesh, lathe, quad, tri
from meshpaint.scene import Scene, Light, Glow
from meshpaint.util import smooth, lerp, norm, noise, ridged, terrace, tex

TERR, FG, TWR, DARK, LAMP, WALL, ROOF, WIN, CAP = 2, 3, 4, 5, 6, 7, 8, 9, 10
TX, TY = 88.0, 163.0           # tower position


def head_h(x, y):
    """Headland height: a promontory with ragged coast, stepped setbacks and tilted strata ledges."""
    xx = x - 40
    hw = np.clip(22 + 0.35 * xx, 0, 110)
    yc = 170 + 0.2 * xx
    d = np.minimum(xx * 0.6, hw - np.abs(y - yc))                       # distance inside the coastline
    d = d + 16 * (noise(x, y, 45) - 0.5) + 8 * (noise(x, y, 11) - 0.5) + 4 * (ridged(x, y, 4) - 0.5)
    dj = d + 3.0 * (noise(x + 70, y, 3) - 0.5)
    d = d + (terrace(dj, 2.4, 0.0) - d) * 0.55 * smooth(-1, 3, d)       # blocky setbacks and buttresses
    cl = smooth(0, 1, d / 14) ** 0.65
    top = 26 * (0.6 + 0.4 * smooth(0, 110, xx)) + 5 * (noise(x, y, 90) - 0.5) + 0.02 * xx
    face = np.sin(np.pi * cl)
    h = cl * top + (4.0 * (ridged(x, y, 7) - 0.5) + 1.6 * (noise(x, y, 2.2) - 0.5) + 0.9 * (ridged(x, y, 1.3) - 0.5)) * face
    h = h + (terrace(h, 3.2, 0.10 * x + 0.05 * y) - h) * 0.5 * np.sqrt(face)
    h = h + (terrace(h, 1.1, 0.07 * x - 0.04 * y) - h) * 0.35 * np.sqrt(face)
    h = h + 0.5 * (noise(x, y, 1.2) - 0.5)
    return np.where(d < 0, np.maximum(-6, d * 0.35), h).astype(np.float32)


def fg_h(x, y):
    """Foreground rocks: low ledged slabs rising out of the water."""
    h = (-2.6 + 12 * (noise(x + 300, y, 13) - 0.5) + 5.5 * smooth(8, -18, x) * smooth(108, 60, y)
         + 3.0 * (ridged(x, y + 500, 3.5) - 0.5) + 0.9 * (noise(x, y, 1.0) - 0.5))
    h = h + (terrace(h, 1.15, 0.22 * x - 0.12 * y) - h) * 0.85
    h = h + 0.22 * (noise(x, y, 0.4) - 0.5)
    return (h - 14 * (smooth(4, 16, x) + smooth(96, 110, y))).astype(np.float32)   # sink before the mesh edge


def build():
    ms = MeshSet()
    ms.add(*grid_mesh(np.arange(20, 420, 0.7), np.arange(90, 372, 0.7), head_h), TERR, "up")
    ms.add(*grid_mesh(np.arange(-36, 18, 0.3), np.arange(44, 112, 0.3), fg_h), FG, "up")

    # tower: one lathe profile, bottom to finial
    z0 = float(head_h(np.array([TX]), np.array([TY]))[0]) - 0.4
    topz = z0 + 25.0
    prof = [(4.6, z0, TWR), (4.1, z0 + 1.2, TWR)]
    prof += [(4.1 - 1.35 * t, z0 + 1.2 + 23.8 * t, TWR) for t in np.linspace(0.04, 1, 24)]
    prof += [(4.3, topz, DARK), (4.3, topz + 0.45, DARK), (2.05, topz + 0.45, LAMP),
             (2.05, topz + 3.6, CAP), (2.75, topz + 3.6, CAP), (2.3, topz + 4.3, CAP),
             (1.2, topz + 5.5, CAP), (0.22, topz + 6.1, CAP), (0.22, topz + 7.4, CAP), (0.0, topz + 7.5, CAP)]
    V, F, M = lathe(TX, TY, prof)
    ms.add(V, F, M, (TX, TY, z0 + 14), attr=(V[:, 2] - z0) / 25.0)
    V, F, M = lathe(TX, TY, [(4.2, topz + 1.50, DARK), (4.2, topz + 1.62, DARK)])      # gallery rail
    ms.add(V, F, M, (TX, TY, topz + 1.5))
    for a in np.linspace(0, 2 * np.pi, 20, endpoint=False):                            # rail posts
        px, py = TX + 4.2 * np.cos(a), TY + 4.2 * np.sin(a)
        t = np.array([-np.sin(a), np.cos(a)]) * 0.06
        ms.add(*quad((px - t[0], py - t[1], topz + 0.45), (px + t[0], py + t[1], topz + 0.45),
                     (px + t[0], py + t[1], topz + 1.6), (px - t[0], py - t[1], topz + 1.6)), DARK, (TX, TY, topz + 1))
    lamp = (TX, TY, topz + 2.0)

    # keeper's cottage
    bx, by = 104.0, 166.0
    bz = float(head_h(np.array([bx]), np.array([by]))[0]) - 0.3
    x0, x1, y0, y1, zb, z1, zr = bx - 5.5, bx + 5.5, by - 3.2, by + 3.2, bz, bz + 3.6, bz + 5.9
    ctr = (bx, by, bz + 2)
    for q in [((x0, y0, zb), (x1, y0, zb), (x1, y0, z1), (x0, y0, z1)),
              ((x0, y1, zb), (x1, y1, zb), (x1, y1, z1), (x0, y1, z1)),
              ((x0, y0, zb), (x0, y1, zb), (x0, y1, z1), (x0, y0, z1)),
              ((x1, y0, zb), (x1, y1, zb), (x1, y1, z1), (x1, y0, z1))]:
        ms.add(*quad(*q), WALL, ctr)
    for xe in (x0, x1):
        ms.add(*tri((xe, y0, z1), (xe, y1, z1), (xe, by, zr)), WALL, ctr)             # gables
    e = 0.5
    ms.add(*quad((x0 - e, y0 - e, z1 - 0.3), (x1 + e, y0 - e, z1 - 0.3), (x1 + e, by, zr), (x0 - e, by, zr)), ROOF, ctr)
    ms.add(*quad((x0 - e, y1 + e, z1 - 0.3), (x1 + e, y1 + e, z1 - 0.3), (x1 + e, by, zr), (x0 - e, by, zr)), ROOF, ctr)
    for wx in (bx - 2.6, bx + 2.2):
        ms.add(*quad((wx - 0.6, y0 - 0.05, zb + 1.4), (wx + 0.6, y0 - 0.05, zb + 1.4),
                     (wx + 0.6, y0 - 0.05, zb + 2.7), (wx - 0.6, y0 - 0.05, zb + 2.7)), WIN, ctr)
    ms.add(*quad((x1 - 2.4, by - 0.5, zr - 1.2), (x1 - 1.4, by - 0.5, zr - 1.2),
                 (x1 - 1.4, by - 0.5, zr + 1.3), (x1 - 2.4, by - 0.5, zr + 1.3)), ROOF, ctr)   # chimney

    def bump(g):
        """Fine rock relief the mesh is too coarse to carry, faded where it would be smaller than a pixel."""
        rk = (g.mat == TERR) | (g.mat == FG)
        for bs, ba in ((3.0, 0.30), (0.8, 0.30), (0.22, 0.22)):
            q0, q1, q2 = g.x[rk] + 0.6 * g.z[rk], g.y[rk] + 0.6 * g.z[rk], g.z[rk]
            b = np.stack([tex("gx", q0, q1, bs), tex("gy", q0, q1, bs), tex("gx", q1 + 40, q2 * 2, bs)], 1) * ba
            b *= np.where(g.mat[rk] == FG, 1.0, np.clip(g.foc * bs / (g.dist[rk] * 6), 0, 1) * (1 - 0.7 * (g.N[rk, 2] > 0.9)))[:, None]
            g.N[rk] = norm(g.N[rk] + b)

    def shade(g):
        """Base colour, gloss and emission for every visible point."""
        x, y, z, mat = g.x, g.y, g.z, g.mat
        alb = np.zeros((len(x), 3), np.float32); gloss = np.zeros(len(x), np.float32); emit = np.zeros((len(x), 3), np.float32)

        m = mat == TERR                                    # cliff rock, grass on top, wet band at the waterline
        xm, ym, zm = x[m], y[m], z[m]
        al = xm + 0.6 * ym
        zw = zm + 3 * (noise(xm, ym, 6) - 0.5)
        strata = 0.65 * noise(al * 0.08, zw * 6, 12) + 0.35 * noise(al * 0.3, zw * 30, 6)
        rock = lerp((0.105, 0.078, 0.062), (0.34, 0.25, 0.19), smooth(0.3, 0.7, strata))
        rock = rock * lerp((0.84, 0.90, 1.0), (1.20, 1.02, 0.84), noise(xm + zm, ym, 14))
        rock = rock * (1 - 0.45 * smooth(0.52, 0.75, noise(al, zm * 0.07, 1.5)))[:, None]           # run-off stains
        rock = rock * (1 - 0.55 * ridged(al + zm * 0.3, zm * 1.4 + ym * 0.2, 1.1) ** 6)[:, None]   # cracks
        rock = rock * (0.8 + 0.4 * noise(xm + zm, ym + zm, 0.25))[:, None]
        lich = (smooth(0.66, 0.78, noise(xm + zm * 2, ym, 1.8)) * smooth(8, 16, zm) * 0.5)[:, None]
        rock = rock + (np.array((0.40, 0.34, 0.15), np.float32) - rock) * lich
        grass = lerp((0.045, 0.075, 0.022), (0.20, 0.17, 0.055), noise(xm, ym, 7)) * (0.7 + 0.6 * noise(xm, ym, 0.5))[:, None]
        gmix = smooth(0.80, 0.92, g.N[m, 2] + 0.08 * (noise(xm, ym, 3) - 0.5)) * smooth(6, 12, zm)
        a = rock + (grass - rock) * gmix[:, None]
        wet = smooth(3.2, 0.6, zm + 1.5 * (noise(xm, ym, 4) - 0.5))
        a = a * (1 - 0.62 * wet)[:, None]
        alb[m] = a + (np.array((0.018, 0.030, 0.014), np.float32) - a) * (0.5 * wet * smooth(0.4, 0.7, noise(xm, ym + zm, 1.5)))[:, None]
        gloss[m] = 0.35 * wet

        m = mat == FG                                      # dark wet foreground rock
        a = lerp((0.035, 0.032, 0.035), (0.13, 0.11, 0.10), noise(x[m], y[m] + z[m] * 2, 1.6))
        wet = smooth(2.6, 0.3, z[m] + 0.8 * (noise(x[m], y[m], 2) - 0.5))
        alb[m] = a * (1 - 0.5 * wet)[:, None]
        gloss[m] = 0.25 + 0.6 * wet

        m = mat == TWR                                     # white tower, red bands, weather streaks, windows, door
        v = g.attr[m]
        ang = np.arctan2(y[m] - TY, x[m] - TX)
        red = ((v > 0.20) & (v < 0.40)) | ((v > 0.60) & (v < 0.80))
        a = np.where(red[:, None], (0.50, 0.045, 0.035), (0.80, 0.78, 0.73)) * (0.82 + 0.3 * noise(ang * 6, z[m] * 0.15, 1.0))[:, None]
        hole = ((np.abs(ang + 1.75) < 0.13) & ((np.abs(v - 0.50) < 0.022) | (np.abs(v - 0.90) < 0.022))) \
            | ((np.abs(ang + 1.45) < 0.2) & (v < 0.115) & (v > 0.03))
        a[hole] = (0.012, 0.012, 0.018)
        alb[m] = a

        alb[mat == DARK] = (0.022, 0.02, 0.024)
        alb[mat == CAP] = (0.10, 0.028, 0.022); gloss[mat == CAP] = 0.25
        m = mat == LAMP                                    # lantern glass with dark glazing bars
        bars = np.abs(((np.arctan2(y[m] - TY, x[m] - TX) * 8 / np.pi) % 1.0) - 0.5) > 0.42
        alb[m] = (0.02, 0.02, 0.02)
        emit[m] = np.where(bars[:, None], (0.02, 0.015, 0.01), (15.0, 10.5, 4.2)) * (0.65 + 0.35 * np.exp(-((z[m] - lamp[2]) / 0.9) ** 2))[:, None]
        alb[mat == WALL] = (0.74, 0.70, 0.63)
        alb[mat == ROOF] = (0.07, 0.06, 0.065); gloss[mat == ROOF] = 0.2
        emit[mat == WIN] = (5.0, 2.9, 0.85)
        return alb, gloss, emit

    return Scene(
        meshes=ms,
        camera=Camera(eye=(0, 0, 13.0), bearing=12.0, pitch=3.7, focal=1537.0, focus=185.0),
        shade=shade, bump=bump,
        look="golden_hour",
        regions={TERR: 2, FG: 3}, default_region=4,                 # 4 = built structures
        stroke_angle={2: 1.25, 3: 0.6, 4: np.pi / 2},
        no_flip=(TERR, FG),
        lights=[Light(pos=lamp, skip_mats=(TWR, LAMP), transparent_mats=(LAMP,))],
        glows=[Glow(pos=lamp, beams=((np.pi + 0.10, 0.085), (-0.07, 0.085)))],
    )
