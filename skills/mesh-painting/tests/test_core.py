"""A4: the compiled core and the Python core agree. Also the scene generator (R14)."""
import unittest

import numpy as np
from scipy.ndimage import gaussian_filter1d

import common  # noqa: F401  (puts the engine on the path)
from meshpaint import native, util


def stroke_fixture():
    """Deterministic brushstroke fixture for native/Python parity."""
    rng = np.random.default_rng(1234)
    n, W, H = 200, 256, 256
    P = np.zeros((n, 11), np.float32)
    for i in range(n):
        cx, cy = rng.uniform(0, W), rng.uniform(0, H)
        ang = rng.uniform(0, np.pi)
        width = rng.uniform(2, 20); length = rng.uniform(6, 80)
        col = rng.uniform(0, 1, 3)
        bend = rng.normal(0, 1) * 0.05 * length
        P[i] = (cx, cy, ang, length, width, col[0], col[1], col[2], 0.9, bend, 0.3)
    BR = gaussian_filter1d(rng.random(8192).astype(np.float32), 1.1, mode="wrap")
    BR = np.ascontiguousarray((BR - BR.min()) / (BR.max() - BR.min()), np.float32)
    return P, np.zeros(n, np.int32), np.ones((1, H, W), np.float32), BR, (H, W)


class CoreParity(unittest.TestCase):
    def setUp(self):
        if not native._load():
            self.skipTest(f"compiled core not available: {native.REASON}")

    def test_raster_matches(self):
        rng = np.random.default_rng(5)
        W, H, nv = 160, 120, 300
        xs = rng.uniform(-20, W + 20, nv); ys = rng.uniform(-20, H + 20, nv); q = rng.uniform(0.01, 1.0, nv)
        faces = rng.integers(0, nv, (400, 3)).astype(np.int32)
        fmask = rng.random(400) > 0.1
        for persp in (True, False):
            a = native.raster(xs, ys, q, faces, fmask, W, H, 0.0, persp)
            b = native.raster_py(xs, ys, q, faces, fmask, W, H, 0.0, persp)
            self.assertTrue(np.array_equal(a[0], b[0]), "triangle ids differ")
            for k in (1, 2, 3):
                self.assertLessEqual(float(np.abs(a[k] - b[k]).max()), 1e-5)

    def test_strokes_match(self):
        P, reg, masks, BR, (H, W) = stroke_fixture()
        ca = np.zeros((H, W, 3), np.float32); ha = np.zeros((H, W), np.float32)
        cb = np.zeros((H, W, 3), np.float32); hb = np.zeros((H, W), np.float32)
        native.strokes(ca, ha, P, reg, masks, BR, 1)
        native.strokes_py(cb, hb, P, reg, masks, BR, 1)
        self.assertGreater(float(ca.mean()), 0.05, "fixture painted nothing")
        for name, x, y in (("canvas", ca, cb), ("height", ha, hb)):
            d = np.abs(x - y)
            self.assertGreaterEqual(float((d <= 1e-4).mean()), 0.999, name)
            self.assertLessEqual(float(d.max()), 0.02, name)


class SceneGenerator(unittest.TestCase):
    def test_repeatable_and_separate(self):
        util.seed(11)
        a = util.scene_rng("bushes").random(5)
        before = util.rng().bit_generator.state
        b = util.scene_rng("bushes").random(5)
        c = util.scene_rng("stones").random(5)
        self.assertTrue(np.array_equal(a, b))
        self.assertFalse(np.array_equal(a, c))
        self.assertEqual(before, util.rng().bit_generator.state, "scene draws moved the pipeline generator")
        util.seed(12)
        self.assertFalse(np.array_equal(a, util.scene_rng("bushes").random(5)))
        util.seed(11)


if __name__ == "__main__":
    unittest.main()
