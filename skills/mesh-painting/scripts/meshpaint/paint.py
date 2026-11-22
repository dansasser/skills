"""Skin a rendered target with brushstrokes."""
import numpy as np
from scipy.ndimage import gaussian_filter, gaussian_filter1d, sobel
from .util import blur
from . import limits, native

# stroke count, width range, length range, opacity, bristle, colour pickup blur, placed by error?
# Sizes are pixels at scale 2.4 (a 3840-wide canvas); the two finest passes stay at that size at any scale.
PASSES = [
    (6000, (80, 150), (250, 650), 0.96, 0.30, 26, False),
    (20000, (34, 60), (100, 240), 0.92, 0.30, 11, False),
    (70000, (15, 27), (46, 105), 0.90, 0.28, 5, True),
    (200000, (6.5, 12), (20, 44), 0.90, 0.26, 2.2, True),
    (500000, (3.2, 5.6), (9, 21), 0.90, 0.22, 1.0, True),
    (1000000, (1.8, 2.9), (5, 11), 0.90, 0.16, 0.5, True),
    (1200000, (1.2, 1.8), (3, 6.5), 0.92, 0.10, 0.0, True),
]


def _pass_counts(S):
    """(strokes per batch, batches) for each pass at effective scale S, exactly as paint() draws them."""
    ka = (S / 2.4) ** 2
    out = []
    for n, _, _, _, _, _, by_err in PASSES:
        n = max(int(n * ka), 1)
        nb = 4 if by_err else 1
        out.append((max(n // nb, 1), nb))
    return out


def planned_strokes(S):
    """Total brushstrokes paint() will lay across all passes at effective scale S."""
    return sum(m * nb for m, nb in _pass_counts(S))


def paint(TGT, REG, stroke_angle, S, horizon_y, rng, log, sea_region=1, grain=0.010, checkpoint=None):
    """TGT: (H, W, 3) reference in 0..1. REG: (H, W) region ids. Returns the painted canvas and stroke count.
    checkpoint(canvas, passes_done, strokes_done), if given, is called after each completed pass."""
    H, W = REG.shape
    limits.check_pixels("paint (canvas)", W, H)
    limits.check_strokes("paint (planning the passes)", planned_strokes(S))   # before any stroke is generated
    nreg = int(REG.max()) + 1
    canvas = np.empty((H, W, 3), np.float32); canvas[:] = (0.30, 0.22, 0.22)
    height = np.zeros((H, W), np.float32)
    BR = gaussian_filter1d(rng.random(8192).astype(np.float32), 1.1, mode="wrap")
    BR = np.ascontiguousarray((BR - BR.min()) / (BR.max() - BR.min()), np.float32)
    masks = np.ascontiguousarray(np.stack([gaussian_filter((REG == k).astype(np.float32), 0.8) for k in range(nreg)]))
    # stroke direction: along the form, i.e. across the local colour gradient
    lumi = gaussian_filter(TGT @ np.array((0.3, 0.6, 0.1), np.float32), 1.5 * S)
    gx, gy = sobel(lumi, 1), sobel(lumi, 0)
    Jxx, Jxy, Jyy = blur(gx * gx, 5 * S), blur(gx * gy, 5 * S), blur(gy * gy, 5 * S)
    ANG = 0.5 * np.arctan2(2 * Jxy, Jxx - Jyy) + np.pi / 2
    COH = np.sqrt((Jxx - Jyy) ** 2 + 4 * Jxy ** 2) / (Jxx + Jyy + 1e-6)
    del gx, gy, lumi, Jxx, Jxy, Jyy
    DEF = np.array([stroke_angle.get(k, 0.0) for k in range(nreg)])
    regmasks = [(REG == k).astype(np.float32) for k in range(nreg)]

    def region_blur(sig):
        """Blur the colour reference without letting colours leak across surfaces."""
        if sig <= 0:
            return TGT
        out = np.zeros_like(TGT)
        for mk in regmasks:
            if not mk.any():
                continue
            den = blur(mk, sig) + 1e-5
            for ch in range(3):
                out[..., ch] += mk * blur(TGT[..., ch] * mk, sig) / den
        return out

    ka = (S / 2.4) ** 2; ks = S / 2.4
    total = 0
    for pi, (n, wr, Lr, op, bri, sig, by_err) in enumerate(PASSES):
        limits.stage(f"paint (pass {pi + 1} of {len(PASSES)})")
        fine = pi >= len(PASSES) - 2
        n = max(int(n * ka), 1)
        if not fine:
            wr = (max(wr[0] * ks, 1.2), max(wr[1] * ks, 1.8)); Lr = (max(Lr[0] * ks, 3), max(Lr[1] * ks, 6.5)); sig *= ks
        ref = region_blur(sig)
        nb = 4 if by_err else 1
        for _ in range(nb):
            m = max(n // nb, 1)
            if by_err:                                   # look at the canvas, paint where it is wrong
                err = blur(np.abs(canvas - ref).sum(2), max(wr[0] * 0.4, 0.6)) + 0.004
                pe = err.ravel().astype(np.float64)
                pidx = rng.choice(H * W, m, p=pe / pe.sum())
                ys = (pidx // W).astype(np.float32) + rng.uniform(-0.5, 0.5, m)
                xs = (pidx % W).astype(np.float32) + rng.uniform(-0.5, 0.5, m)
            else:
                xs = rng.uniform(0, W, m); ys = rng.uniform(0, H, m)
            xi = np.clip(xs.astype(int), 0, W - 1); yi = np.clip(ys.astype(int), 0, H - 1)
            reg = REG[yi, xi].astype(np.int32)
            ang = np.where(COH[yi, xi] > 0.18, ANG[yi, xi], DEF[reg]) + rng.normal(0, 0.12, m)
            sea = reg == sea_region
            ang = np.where(sea, rng.normal(0, 0.03, m), ang)          # water lies flat
            Ls = rng.uniform(*Lr, m) * np.where(sea, 1.5, 1.0)
            ws = rng.uniform(*wr, m)
            if pi < 3:                                               # distant water gets smaller marks
                persp = np.where(sea, np.clip(0.35 + (ys - horizon_y) / (260 * S), 0.35, 1.0), 1.0)
                Ls = Ls * persp; ws = ws * np.maximum(persp, 0.5)
            cols = ref[yi, xi] + rng.normal(0, 0.012 if pi < 4 else 0.006, (m, 1))
            P = np.column_stack([xs, ys, ang, Ls, ws, cols, np.full(m, op), rng.normal(0, 1, m) * Ls * 0.05,
                                 np.full(m, bri)]).astype(np.float32)
            native.strokes(canvas, height, P, reg, masks, BR, int(rng.integers(1, 2 ** 31)))
            total += m
            limits.active().strokes_done = total
        log(f"pass {pi + 1}/{len(PASSES)}: {total} strokes so far, mean error {np.abs(canvas - TGT).mean():.4f}")
        if checkpoint:
            checkpoint(canvas, pi + 1, total)
    limits.stage("paint (relief and grain)")
    h = gaussian_filter(height, 0.7)
    canvas *= np.clip(1 - 0.15 * (np.gradient(h, axis=1) + np.gradient(h, axis=0)), 0.93, 1.07)[..., None]
    if grain:                                            # sensor grain: the one step that is not brushwork
        gr = gaussian_filter(rng.normal(0, 1, (H, W)).astype(np.float32), 0.38 * S); gr /= gr.std()
        lum = canvas @ np.array((0.3, 0.6, 0.1), np.float32)
        canvas += (gr * grain * (1 - 0.55 * lum))[..., None] + rng.normal(0, grain * 0.2, canvas.shape).astype(np.float32)
    return np.clip(canvas, 0, 1), total
