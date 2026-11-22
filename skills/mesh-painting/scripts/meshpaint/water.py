"""Sea: a plane at z = 0 with a filtered wave field, Fresnel sky reflection, sun glitter and shore foam."""
import numpy as np
from scipy.ndimage import distance_transform_edt, map_coordinates, gaussian_filter
from .util import smooth, norm, tex, noise, ridged
from . import native


class Shore:
    """Plan-view map of the coastline, taken from the meshes themselves.
    dist: metres to the nearest land. exposure: 0 in enclosed pools and narrow gaps, 1 on open water."""

    def __init__(self, V, F, cell=0.25, max_cells=4096, open_width=3.0):
        lo = V[:, :2].min(0) - 30; hi = V[:, :2].max(0) + 30
        cell = max(cell, float((hi - lo).max()) / max_cells)
        self.lo, self.cell = lo, cell
        w = int((hi[0] - lo[0]) / cell) + 1; h = int((hi[1] - lo[1]) / cell) + 1
        top = native.raster((V[:, 0] - lo[0]) / cell, (V[:, 1] - lo[1]) / cell, V[:, 2], F,
                            np.ones(len(F), bool), w, h, init=-1e9, persp=False)[3]
        water = top <= 0
        self.D = (distance_transform_edt(water) * cell).astype(np.float32)
        wide = self.D > open_width                                    # water at least open_width from any rock
        opened = (distance_transform_edt(~wide) * cell) <= open_width  # ...grown back: open water only
        self.E = gaussian_filter((opened & water).astype(np.float32), 2.0 / cell)
        self.w, self.h = w, h

    def _sample(self, A, x, y, outside):
        u = (x - self.lo[0]) / self.cell - 0.5; v = (y - self.lo[1]) / self.cell - 0.5
        val = map_coordinates(A, [v, u], order=1, mode="nearest")
        return np.where((u < 0) | (u > self.w - 1) | (v < 0) | (v > self.h - 1), outside, val).astype(np.float32)

    def dist(self, x, y):
        return self._sample(self.D, x, y, 99.0)

    def exposure(self, x, y):
        return smooth(0.15, 0.6, self._sample(self.E, x, y, 1.0))


def shade_sea(Dw, cam, sky, shore, sun_shadow, rng, shape, wmask, sea_state=1.0, foam_amount=1.0, mirror=None, px=None, py=None):
    """Dw: view rays that hit the sea. Returns (colour, distance along ray)."""
    eye, FOC, S, SUN = cam.eye, cam.foc, cam.S, sky.sun
    Dw = Dw.copy(); Dw[:, 2] = np.minimum(Dw[:, 2], -2e-4)
    tw = -eye[2] / Dw[:, 2]
    Pw = eye + Dw * tw[:, None]
    fx, fy = Pw[:, 0], Pw[:, 1]
    # A pixel covers far more water along the view direction than across it. Waves smaller than that
    # footprint are faded out of the surface and moved into `rough`, which widens reflections instead.
    foot = tw / FOC / np.sqrt(np.clip(-Dw[:, 2], 1e-3, 1))
    sx = np.zeros(len(Pw), np.float32); sy = np.zeros(len(Pw), np.float32); rough = np.zeros(len(Pw), np.float32)
    for lamb, amp in ((0.9, 0.16), (3.5, 0.15), (14.0, 0.10), (60.0, 0.05)):
        amp = amp * sea_state
        fade = np.clip(lamb / (foot * 2.5), 0, 1) ** 1.5
        sx += amp * fade * tex("gx", fx, fy * 0.55, lamb)
        sy += amp * fade * tex("gy", fx, fy * 0.55, lamb)
        rough += amp ** 2 * (1 - fade ** 2)
    rsd = np.sqrt(rough)
    Nw = norm(np.stack([-sx, -sy, np.ones_like(sx)], 1))
    cosv = np.sqrt(np.clip(-(Dw * Nw).sum(1), 0.0, 1) ** 2 + 0.45 * rough)     # hidden facets face the viewer more
    Rw = Dw - 2 * (Dw * Nw).sum(1, keepdims=True) * Nw
    Rw[:, 2] = np.abs(Rw[:, 2]) + 0.004 + 0.5 * rsd                          # ...and mirror sky from higher up
    Rw = norm(Rw)
    Fw = 0.02 + 0.98 * (1 - np.clip(cosv, 0, 1)) ** 5
    refl = sky.radiance(Rw, disk=False)
    if mirror is not None:
        # Reflected land: look up the upside-down render where this ripple's tilt points. Ripples
        # smear a reflection mostly up and down the image, so blur more vertically, more in rougher water.
        img, cover = mirror
        sv = (3.0 + 26.0 * min(sea_state, 1.5)) * S; sh = (1.0 + 4.0 * min(sea_state, 1.5)) * S
        soft = np.stack([gaussian_filter(img[..., c], (sv, sh)) for c in range(3)], -1)
        csoft = gaussian_filter(cover, (sv, sh))
        # shift the lookup by the long swell only; short ripples are already in the blur
        lx = tex("gx", fx, fy * 0.55, 60.0) * 0.05 * sea_state; ly = tex("gy", fx, fy * 0.55, 60.0) * 0.05 * sea_state
        u = px - 0.5 + 2 * lx * FOC * 0.30; v = py - 0.5 + 2 * ly * FOC * 0.55
        cov = map_coordinates(csoft, [v, u], order=1, mode="nearest")
        land = np.stack([map_coordinates(soft[..., c], [v, u], order=1, mode="nearest") for c in range(3)], 1)
        land = land / np.maximum(cov, 1e-3)[:, None]
        k = np.clip(cov * 1.25, 0, 1) * (1 - 0.35 * np.clip(rsd * 4, 0, 1))
        refl = refl + (land - refl) * k[:, None]
    seac = np.array((0.006, 0.020, 0.030), np.float32) * (1 - Fw)[:, None] + refl * Fw[:, None]
    # sun glitter: sub-pixel facets that happen to mirror the sun
    sig = 0.035 + 1.6 * rsd
    gam = np.arccos(np.clip(Rw @ SUN, -1, 1))
    sp = gaussian_filter(rng.random(shape).astype(np.float32), 0.42 * S)[wmask]
    spark = np.clip(0.5 + (sp - 0.5) / (sp.std() * 3.4), 0, 1) ** 6
    near = tw < 900
    wsh = np.ones(len(Pw), np.float32); wsh[near] = sun_shadow.lit(Pw[near])
    seac += np.array((34.0, 15.0, 4.2), np.float32) * (np.exp(-(gam / sig) ** 2) * (0.004 / sig ** 2)
                                                        * (0.25 + 3.0 * spark) * Fw * wsh)[:, None]
    dsh = shore.dist(fx, fy)
    seac *= (1 - 0.45 * smooth(14.0, 1.0, dsh))[:, None]             # land darkens the water beside it
    # ---- foam
    expo = shore.exposure(fx, fy)
    wx = fx + 5.0 * (noise(fx, fy, 6) - 0.5); wy = fy + 5.0 * (noise(fx + 90, fy, 6) - 0.5)   # swirl the pattern
    reach = 2.2 + 4.5 * noise(fx, fy, 22)                             # exposed stretches carry foam further out
    prox = np.exp(-dsh / reach)                                       # 1 at the rock, fading offshore
    lace = np.maximum(ridged(wx, wy, 2.6) ** 3, 0.9 * ridged(wx + 31, wy + 17, 0.9) ** 3)     # web of foam lines
    lace = np.maximum(lace, 0.75 * ridged(wx + 5, wy + 3, 0.3) ** 3)
    thick = smooth(0.50, 0.70, 0.85 * prox + 0.95 * (lace - 0.45) + 0.50 * (noise(wx, wy, 3.5) - 0.5))
    arcs = (smooth(0.72, 1.0, np.sin(dsh * 1.5 - 5 * noise(fx, fy, 9))) * np.exp(-dsh / 9.0)
            * smooth(0.30, 0.60, 0.5 * lace + 0.5 * noise(wx, wy, 1.2)))                      # lines left by earlier waves
    wash = smooth(1.3, 0.2, dsh + 1.2 * (noise(fx, fy, 1.0) - 0.5)) * (0.55 + 0.45 * smooth(0.3, 0.7, noise(wx + 13, wy, 0.6)))
    thick = np.clip(np.maximum(thick, 0.75 * arcs) + wash, 0, 1) * (0.12 + 0.88 * expo)      # sheltered water stays calm
    thick = thick * np.clip(foam_amount, 0, 1) * np.clip(0.25 + 0.75 * sea_state, 0, 1)        # calm water carries little foam
    thick *= smooth(0.20, 0.42, noise(fx + 7, fy, 0.22) + 0.75 * thick)                      # pinholes in thin foam
    caps = smooth(0.84, 0.92, noise(fx / 6, fy, 2.2)) * ridged(wx, wy, 0.7) ** 4 * smooth(500, 150, tw) * 0.5 * smooth(0.7, 1.2, sea_state) * foam_amount
    thick = np.clip(thick + caps * (dsh > 25), 0, 1)
    bub = 0.74 + 0.5 * smooth(0.3, 0.7, noise(fx, fy, 0.1))            # bubble texture
    relief = 1 + 0.32 * thick * np.clip(tex("gx", wx, wy, 0.9) * SUN[0] + tex("gy", wx, wy, 0.9) * SUN[1], -1.5, 1.5)
    up = np.tile(np.array([[0, 0, 1.0]], np.float32), (len(Pw), 1))
    foamc = np.array((0.86, 0.85, 0.85), np.float32) * (sky.ambient(up) * 1.7
                                                        + sky.sun_colour * (max(float(SUN[2]), 0.02) * 1.6 * wsh)[:, None])
    foamc = foamc * (1.75 * (0.55 + 0.45 * thick) * bub * relief)[:, None]
    seac = seac + (foamc - seac) * (np.clip(thick * 1.15, 0, 1) ** 0.75 * 0.94)[:, None]
    fogw = 1 - np.exp(-tw / 5200)
    far = sky.radiance(norm(np.stack([Dw[:, 0], Dw[:, 1], np.full(len(Dw), 0.004)], 1)), clouds=False, disk=False)
    return seac + (far - seac) * fogw[:, None], tw
