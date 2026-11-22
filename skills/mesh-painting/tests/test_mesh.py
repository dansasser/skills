"""The scatter helper (P10)."""
import unittest

import numpy as np

import common  # noqa: F401
from meshpaint import mesh, util


class Scatter(unittest.TestCase):
    def setUp(self):
        util.seed(11)
        self.V = np.array([(0, 0, 0), (1, 0, 0), (0, 1, 0), (0, 0, 1)], np.float32)
        self.F = np.array([(0, 1, 2), (0, 1, 3), (1, 2, 3), (0, 2, 3)])
        self.pts = np.array([(10, 20, 1), (-5, 0, 2), (0, 0, 0)], np.float32)

    def test_counts_and_indices(self):
        V, F = mesh.scatter(self.pts, self.V, self.F)
        self.assertEqual(V.shape, (12, 3)); self.assertEqual(F.shape, (12, 3))
        self.assertEqual(int(F.max()), 11)
        for k in range(3):                               # each instance only references its own vertices
            block = F[k * 4:(k + 1) * 4]
            self.assertTrue(((block >= k * 4) & (block < (k + 1) * 4)).all())

    def test_placement_scale_and_yaw(self):
        V, _ = mesh.scatter(self.pts, self.V, self.F, scale=[1, 2, 3], yaw=[0, np.pi / 2, 0])
        V = V.reshape(3, 4, 3)
        np.testing.assert_allclose(V[:, 0], self.pts, atol=1e-5)                  # origin lands on the point
        np.testing.assert_allclose(V[0, 1] - self.pts[0], (1, 0, 0), atol=1e-5)
        np.testing.assert_allclose(V[1, 1] - self.pts[1], (0, 2, 0), atol=1e-5)   # turned a quarter and doubled
        np.testing.assert_allclose(V[2, 3] - self.pts[2], (0, 0, 3), atol=1e-5)

    def test_repeatable_and_jittered(self):
        a, _ = mesh.scatter(self.pts, self.V, self.F, jitter=0.2)
        b, _ = mesh.scatter(self.pts, self.V, self.F, jitter=0.2)
        c, _ = mesh.scatter(self.pts, self.V, self.F, jitter=0.0, yaw=0.0)
        np.testing.assert_array_equal(a, b)
        self.assertGreater(float(np.abs(a.reshape(3, 4, 3)[0] - a.reshape(3, 4, 3)[2] + (self.pts[2] - self.pts[0])).max()), 0.01)
        np.testing.assert_allclose(c.reshape(3, 4, 3)[1] - self.pts[1], self.V, atol=1e-5)

    def test_adds_to_a_mesh_set(self):
        ms = mesh.MeshSet()
        ms.add(*mesh.scatter(self.pts, self.V, self.F), 7, "up")
        V, F, N, M, A = ms.done()
        self.assertEqual(len(V), 12); self.assertTrue((M == 7).all())
        self.assertTrue(np.isfinite(N).all())


class Blades(unittest.TestCase):
    def test_shape_and_base(self):
        util.seed(11)
        V, F = mesh.blades(n=7, height=0.4, width=0.02)
        self.assertEqual(V.shape, (21, 3)); self.assertEqual(F.shape, (7, 3))
        tri = V.reshape(7, 3, 3)
        np.testing.assert_allclose(tri[:, :2, 2], 0, atol=1e-7)             # both base corners on the ground
        self.assertTrue((tri[:, 2, 2] > 0.2).all() and (tri[:, 2, 2] <= 0.4 + 1e-6).all())
        np.testing.assert_allclose(np.linalg.norm(tri[:, 0] - tri[:, 1], axis=1), 0.02, atol=1e-6)


class Guards(unittest.TestCase):
    def test_grid_mesh_rejects_values_that_are_not_numbers(self):
        with self.assertRaises(ValueError) as cm:
            mesh.grid_mesh(np.arange(0, 3.0), np.arange(0, 3.0), lambda x, y: np.sqrt(x - 1.5))
        self.assertIn("not finite", str(cm.exception))

    def test_beam(self):
        V, F = mesh.beam((0, 0, 0), (0, 0, 2), 0.2)
        self.assertEqual(V.shape, (8, 3)); self.assertEqual(F.shape, (12, 3))
        np.testing.assert_allclose(np.abs(V[:, :2]).max(), 0.1, atol=1e-6)
        np.testing.assert_allclose(sorted(set(np.round(V[:, 2], 5))), [0, 2])
        V2, _ = mesh.beam((0, 0, 0), (3, 0, 0), 0.2)                      # a level bar must work too
        self.assertTrue(np.isfinite(V2).all())


if __name__ == "__main__":
    unittest.main()
