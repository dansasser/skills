"""Camera lens and film: depth of field, starburst, ghosts, bloom, chromatic aberration, vignette, film curve."""
import numpy as np
from scipy.ndimage import gaussian_filter, map_coordinates
from .util import smooth, blur

WARM = np.array((1.0, 0.78, 0.42), np.float32)


def film(img, exposure):
    x = img * exposure
    return (np.clip((x * (2.51 * x + 0.03)) / (x * (2.43 * x + 0.59) + 0.14), 0, 1) ** (1 / 2.2)).astype(np.float32)


def apply(img, depth, cam, sky, PX, PY, points, ghosts=True):
    """points: [(x, y, strength, length)] of bright sources that get an aperture starburst."""
    W, H, S = cam.W, cam.H, cam.S
    coc = np.clip(np.abs(1 / depth - 1 / cam.focus) * 48 * cam.aperture, 0, 1.0)
    lay = [img] + [np.stack([gaussian_filter(img[..., ch], sg * S) for ch in range(3)], -1) for sg in (0.5, 1.0, 1.5)]
    k = np.clip(coc / 0.5, 0, 2.999); k0 = k.astype(np.int32); kf = (k - k0)[..., None]
    out = np.zeros_like(img)
    for j in range(3):
        out += (k0 == j)[..., None] * (lay[j] * (1 - kf) + lay[j + 1] * kf)
    img = out; del lay, out
    sx, sy, sz = cam.project((cam.eye + sky.sun * 1e6)[None])
    sx, sy = float(sx[0]), float(sy[0])
    sunv = float(float(sky.sun @ cam.fwd) > 0.5)                 # is the sun in front of the lens at all
    for (qx, qy, I0, ln) in [(sx, sy, 1.1 * sunv, 30.0)] + list(points):
        if I0 <= 0:
            continue
        for kk in range(3):
            a = 0.26 + kk * np.pi / 3
            al = ((PX - qx) * np.cos(a) + (PY - qy) * np.sin(a)) / S
            pe = (-(PX - qx) * np.sin(a) + (PY - qy) * np.cos(a)) / S
            img += WARM * (I0 * np.exp(-(pe / 0.7) ** 2) * np.exp(-np.abs(al) / ln))[..., None]
    if sunv and ghosts:
        for kg, rad, tint in ((0.30, 36, (0.045, 0.028, 0.014)), (0.62, 78, (0.016, 0.028, 0.024)), (1.2, 20, (0.035, 0.018, 0.045))):
            gx = W / 2 + kg * (W / 2 - sx); gy = H / 2 + kg * (H / 2 - sy)
            img += np.array(tint, np.float32) * smooth(rad * S, rad * S * 0.86, np.hypot(PX - gx, PY - gy))[..., None]
    hot = np.clip(img - 1.6, 0, 40)
    for sg, kk in ((4, 0.10), (14, 0.07), (45, 0.05)):
        for ch in range(3):
            img[..., ch] += kk * blur(hot[..., ch], sg * S)
    for ch, mag in ((0, 1.0007), (2, 0.9993)):
        cy = (PY - 0.5 - H / 2) / mag + H / 2; cx = (PX - 0.5 - W / 2) / mag + W / 2
        img[..., ch] = map_coordinates(img[..., ch], [cy, cx], order=1, mode="nearest")
    img *= (1 - 0.27 * (((PX - W / 2) ** 2 + (PY - H / 2) ** 2) / ((W / 2) ** 2 + (H / 2) ** 2)))[..., None]
    return img
