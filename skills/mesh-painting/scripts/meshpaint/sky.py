"""Sky colour, clouds, sun, and the light they throw on surfaces."""
import numpy as np
from .util import smooth, lerp, tex
from .looks import LOOKS  # noqa: F401  (re-exported: sky.LOOKS)


class Sky:
    def __init__(self, sun_bearing, sun_elev, eye, clouds=1.0):
        sb, se = np.radians(sun_bearing), np.radians(sun_elev)
        self.sun = np.array([np.sin(sb) * np.cos(se), np.cos(sb) * np.cos(se), np.sin(se)], np.float32)
        self.day = float(smooth(0.5, 10.0, sun_elev))      # 0 = sun on the horizon, 1 = well up
        self.sun_colour = np.asarray((2.0, 0.78, 0.24), np.float32) * (1 - self.day) \
            + np.asarray((3.67, 2.15, 0.96), np.float32) * self.day
        self.eye = eye
        self.clouds = float(clouds)
        self.haze = np.array((0.36, 0.18, 0.16), np.float32)

    def radiance(self, d, clouds=True, disk=True):
        d = np.asarray(d, np.float32); DAY, SUN = self.day, self.sun
        dz = np.clip(d[:, 2], 0, 1)
        cg = np.clip(d @ SUN, -1, 1)
        w = ((cg + 1) / 2) ** 3
        t = np.clip(dz / 0.42, 0, 1) ** 0.6
        hor = lerp(np.array((0.24, 0.15, 0.22)) * (1 - DAY) + np.array((0.62, 0.46, 0.44)) * DAY, (1.05, 0.42, 0.13), w)
        mid = lerp(np.array((0.09, 0.075, 0.17)) * (1 - DAY) + np.array((0.24, 0.27, 0.46)) * DAY, (0.30, 0.15, 0.21), w)
        zen = (np.array((0.012, 0.024, 0.085)) * (1 - DAY) + np.array((0.07, 0.13, 0.33)) * DAY).astype(np.float32)
        c = np.where((t < 0.42)[:, None], hor + (mid - hor) * (t / 0.42)[:, None],
                     mid + (zen - mid) * smooth(0.42, 1.0, t)[:, None])
        gam = np.arccos(cg)
        c = c + np.array((1.5, 0.62, 0.17), np.float32) * np.exp(-(gam / 0.13) ** 2)[:, None]
        c = c + np.array((0.30, 0.09, 0.03), np.float32) * np.exp(-(gam / 0.5) ** 2)[:, None]
        if clouds:
            for alt, sx, sy, k, lo, hi in ((1400.0, 1000.0, 1000.0, 0.95, 0.50, 0.64), (7000.0, 16000.0, 4200.0, 0.45, 0.52, 0.80)):
                tt = alt / np.maximum(d[:, 2], 0.012)
                px = self.eye[0] + d[:, 0] * tt; py = self.eye[1] + d[:, 1] * tt
                n = tex("nz", px / sx, py / sy, 1.0)
                ns = tex("nz", (px + SUN[0] * alt * 0.35) / sx, (py + SUN[1] * alt * 0.35) / sy, 1.0)
                cov = np.clip(smooth(lo - 0.10 * (self.clouds - 1), hi - 0.10 * (self.clouds - 1), n) * min(self.clouds, 1.0), 0, 1) * smooth(0.012, 0.07, d[:, 2])
                lit = np.clip((n - ns) * 5 + 0.55, 0, 1) * (1 - 0.65 * smooth(hi - 0.02, hi + 0.2, n))
                wc = ((cg + 1) / 2) ** 2
                litc = lerp(np.array((0.42, 0.20, 0.25)) * (1 - DAY) + np.array((1.25, 0.80, 0.62)) * DAY, (1.55, 0.52, 0.17), wc)
                shc = lerp(np.array((0.035, 0.033, 0.075)) * (1 - DAY) + np.array((0.20, 0.19, 0.30)) * DAY, (0.13, 0.075, 0.105), wc)
                c = c + ((shc + (litc - shc) * lit[:, None]) - c) * (cov * k)[:, None]
        if disk:
            c = c + np.array((38.0, 13.0, 2.6), np.float32) * smooth(0.0085, 0.0065, gam)[:, None]
        return c

    def ambient(self, N):
        """Cheap sky irradiance for a surface normal."""
        nz = N[:, 2]
        w = (np.clip(N @ self.sun, -1, 1) + 1) / 2
        zen = np.array((0.050, 0.070, 0.165), np.float32) * (1 + 0.9 * self.day)
        hor = lerp((0.17, 0.12, 0.19), (0.55, 0.25, 0.11), w ** 1.5)
        return zen * (0.5 + 0.5 * nz)[:, None] + hor * (1 - np.abs(nz))[:, None] * 0.75 + 0.012
