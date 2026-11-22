"""Camera, and shadow maps built by rasterising the same meshes from each light."""
import numpy as np
from .util import norm
from . import native


class Camera:
    """eye in metres (Z up). bearing: degrees clockwise from +Y. pitch: degrees up.
    focal: pixels at a 1600-wide frame (1537 is about a 55 degree view). focus: metres to the sharp plane.
    aperture: depth-of-field strength; 1 is a gentle default, 0 keeps everything sharp."""

    def __init__(self, eye, bearing, pitch, focal=1537.0, focus=185.0, aperture=1.0):
        self.eye = np.asarray(eye, np.float32)
        self.bearing, self.pitch, self.focal, self.focus = float(bearing), float(pitch), float(focal), float(focus)
        self.aperture = float(aperture)

    def setup(self, W, H, S):
        self.W, self.H, self.S = W, H, S
        self.foc = self.focal * S
        b, p = np.radians(self.bearing), np.radians(self.pitch)
        self.fwd = np.array([np.sin(b) * np.cos(p), np.cos(b) * np.cos(p), np.sin(p)], np.float32)
        self.rgt = norm(np.cross(self.fwd, [0, 0, 1])).astype(np.float32)
        self.upv = np.cross(self.rgt, self.fwd).astype(np.float32)
        self.horizon_y = H / 2 + self.foc * np.tan(p)

    def project(self, P):
        c = np.asarray(P, np.float32) - self.eye
        zc = c @ self.fwd
        return self.W / 2 + self.foc * (c @ self.rgt) / zc, self.H / 2 - self.foc * (c @ self.upv) / zc, zc

    def rays(self):
        PX, PY = np.meshgrid(np.arange(self.W, dtype=np.float32) + 0.5, np.arange(self.H, dtype=np.float32) + 0.5)
        D = norm(self.fwd + self.rgt * ((PX - self.W / 2) / self.foc)[..., None]
                 + self.upv * ((self.H / 2 - PY) / self.foc)[..., None]).astype(np.float32)
        return D, PX, PY


class SunShadow:
    """Orthographic depth map from the sun's direction."""

    def __init__(self, V, F, sun, res=4096):
        self.sun = sun
        self.lx = norm(np.cross(sun, [0, 0, 1])).astype(np.float32)
        self.ly = np.cross(self.lx, sun).astype(np.float32)
        u, v, s = V @ self.lx, V @ self.ly, V @ sun
        self.cell = (u.max() - u.min() + 4) / res
        self.u0, self.v1 = u.min() - 2, v.max() + 2
        self.w, self.h = res, int((v.max() - v.min() + 4) / self.cell) + 1
        self.Q = native.raster((u - self.u0) / self.cell, (self.v1 - v) / self.cell, s, F,
                               np.ones(len(F), bool), self.w, self.h, init=-1e9, persp=False)[3]

    def lit(self, P):
        """Fraction of the sun each point can see (9 taps give a soft edge)."""
        u = (P @ self.lx - self.u0) / self.cell; v = (self.v1 - P @ self.ly) / self.cell; sd = P @ self.sun
        lit = np.zeros(len(P), np.float32)
        for du in (-1.3, 0, 1.3):
            for dv in (-1.3, 0, 1.3):
                ui = (u + du).astype(np.int64); vi = (v + dv).astype(np.int64)
                inside = (ui >= 0) & (ui < self.w) & (vi >= 0) & (vi < self.h)
                occ = self.Q[np.clip(vi, 0, self.h - 1), np.clip(ui, 0, self.w - 1)]
                lit += np.where(inside & (occ > sd + 0.35), 0.0, 1.0)
        return lit / 9


class DownShadow:
    """Shadow map for a point light, looking straight down from it (covers about 72 degrees off vertical).
    Triangles of `transparent` materials (lamp glass) do not block the light."""

    def __init__(self, V, F, FM, pos, transparent=(), res=2048, focal=333.0):
        self.pos, self.res, self.f = np.asarray(pos, np.float32), res, focal
        c = V - self.pos
        lz = -c[:, 2]
        fmask = (lz[F] > 0.2).all(1) & ~np.isin(FM, list(transparent))
        with np.errstate(all="ignore"):
            self.Q = native.raster(res / 2 + focal * c[:, 0] / lz, res / 2 - focal * c[:, 1] / lz,
                                   1 / np.maximum(lz, 1e-3), F, fmask, res, res, init=0.0)[3]

    def lit(self, P):
        c = P - self.pos
        lz = np.maximum(-c[:, 2], 1e-3)
        u = self.res / 2 + self.f * c[:, 0] / lz; v = self.res / 2 - self.f * c[:, 1] / lz
        lit = np.zeros(len(P), np.float32)
        for du in (-1.2, 0, 1.2):
            for dv in (-1.2, 0, 1.2):
                ui = (u + du).astype(np.int64); vi = (v + dv).astype(np.int64)
                inside = (ui >= 0) & (ui < self.res) & (vi >= 0) & (vi < self.res) & (-c[:, 2] > 0.2)
                occ = self.Q[np.clip(vi, 0, self.res - 1), np.clip(ui, 0, self.res - 1)]
                lit += np.where(inside & (occ * (lz * 0.97 - 0.2) > 1.0), 0.0, 1.0)
        return lit / 9
