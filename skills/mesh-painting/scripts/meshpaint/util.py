"""Small maths helpers and the shared tileable noise field."""
import zlib

import numpy as np
from scipy.ndimage import zoom, map_coordinates, gaussian_filter

_S = {}


def seed(value=11):
    """(Re)build the noise field. Everything procedural derives from this seed."""
    rng = np.random.default_rng(value)
    out = np.zeros((1024, 1024), np.float32)
    for o in range(7):
        n = 8 * 2 ** o
        g = rng.random((n, n)).astype(np.float32)
        out += zoom(g, 1024 / n, order=3, mode="grid-wrap", grid_mode=True) * 0.5 ** o
    nz = (out - out.min()) / (out.max() - out.min())
    gx = np.roll(nz, -1, 1) - np.roll(nz, 1, 1); gx /= gx.std()
    gy = np.roll(nz, -1, 0) - np.roll(nz, 1, 0); gy /= gy.std()
    _S.update(nz=nz, gx=gx, gy=gy, rng=rng, seed=int(value))


def _get(k):
    if not _S:
        seed()
    return _S[k]


def rng():
    """The pipeline's own generator. Scene files should use scene_rng instead: drawing
    from this one shifts every later draw the engine makes."""
    return _get("rng")


def scene_rng(name="scene"):
    """A random generator for scene files. It depends only on --seed and `name`, so a scene
    that uses it is repeatable, and using it does not change anything the engine draws."""
    return np.random.default_rng([_get("seed"), zlib.crc32(str(name).encode("utf-8"))])


def smooth(a, b, x):
    t = np.clip((x - a) / (b - a), 0, 1)
    return t * t * (3 - 2 * t)


def lerp(a, b, t):
    a = np.asarray(a, np.float32); b = np.asarray(b, np.float32)
    return a + (b - a) * np.asarray(t, np.float32)[..., None]


def norm(v):
    return v / (np.linalg.norm(v, axis=-1, keepdims=True) + 1e-9)


def tex(which, x, y, scale):
    """Sample a field ('nz' noise, 'gx'/'gy' its gradient). `scale` metres of world = 64 texels."""
    c = np.stack([np.asarray(y, np.float64).ravel() * 64 / scale,
                  np.asarray(x, np.float64).ravel() * 64 / scale])
    return map_coordinates(_get(which), c, order=1, mode="grid-wrap").reshape(np.shape(x)).astype(np.float32)


def noise(x, y, scale):
    """Smooth noise in 0..1; features are roughly 2*scale across."""
    return tex("nz", x, y, scale)


def ridged(x, y, scale):
    """Noise folded into thin bright ridges (cracks, foam lines, rock ribs)."""
    return 1 - np.abs(2 * noise(x, y, scale) - 1)


def terrace(h, step, tilt):
    """Snap a height towards tilted ledges `step` apart: rock strata."""
    q = (h + tilt) / step
    fl = np.floor(q)
    return (fl + smooth(0.2, 0.8, q - fl)) * step - tilt


def blur(a, sig):
    """Gaussian blur; wide blurs run on a reduced copy for speed."""
    if sig <= 0:
        return a
    f = int(sig // 3)
    if f < 2:
        return gaussian_filter(a, sig)
    H, W = a.shape
    small = a[: H - H % f, : W - W % f].reshape(H // f, f, W // f, f).mean((1, 3))
    small = gaussian_filter(small, sig / f)
    yy = (np.arange(H) + 0.5) / f - 0.5; xx = (np.arange(W) + 0.5) / f - 0.5
    return map_coordinates(small, np.meshgrid(yy, xx, indexing="ij"), order=1, mode="nearest").astype(a.dtype)
