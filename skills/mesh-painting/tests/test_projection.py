"""Near-plane crossings retain visible geometry and interpolated attributes."""
import unittest
import numpy as np
import common  # noqa: F401
from meshpaint import limits


class ClipNear(unittest.TestCase):
    def test_crossing_triangle_becomes_two_with_interpolated_values(self):
        from meshpaint.geometry import clip_near
        V = np.array([(-1, .5, 0), (1, 2, 0), (0, 2, 1)], np.float32)
        F = np.array([[0, 1, 2]], np.int32)
        attr = {'world': V.copy(), 'weight': np.array([0., 3., 3.], np.float32)}
        v, f, a, source = clip_near(V, F, attr, V[:, 1], near=1)
        self.assertEqual(f.shape, (2, 3))
        np.testing.assert_array_equal(source, [0, 0])
        self.assertTrue((v[f, 1] >= 1).all())
        self.assertTrue((f >= 0).all() and (f < len(v)).all())
        new = np.where(v[:, 1] == 1)[0]
        np.testing.assert_allclose(a['weight'][new], 1, atol=1e-6)
        np.testing.assert_allclose(a['world'], v)

    def test_all_front_keeps_original_buffers_and_behind_is_absent(self):
        from meshpaint.geometry import clip_near
        V = np.array([(0, 2, 0), (1, 2, 0), (0, 2, 1)], np.float32)
        F = np.array([[0, 1, 2]], np.int32)
        v, f, a, src = clip_near(V, F, {}, V[:, 1])
        self.assertIs(v, V); self.assertIs(f, F)
        np.testing.assert_array_equal(src, [0])
        _, f, _, src = clip_near(V, F, {}, -V[:, 1])
        self.assertEqual(len(f), 0); self.assertEqual(len(src), 0)

    def test_visible_area_remains_when_one_vertex_is_behind(self):
        from meshpaint.geometry import Camera, clip_near
        from meshpaint import native
        cam = Camera((0, 0, 0), 0, 0, focal=25)
        cam.setup(64, 64, 1)
        V = np.array([(-1, .5, -.3), (1, 2, -.3), (0, 2, .8)], np.float32)
        v, f, _, _ = clip_near(V, np.array([[0, 1, 2]], np.int32), {}, V[:, 1])
        x,y,z=cam.project(v)
        a=native.raster_py(x,y,1/np.maximum(z,1e-3),f,np.ones(len(f),bool),64,64)
        self.assertGreater(np.count_nonzero(a[0]>=0), 100)

    def test_native_large_screen_bounds_are_clamped_before_integer_conversion(self):
        from meshpaint import native
        if not native._load():
            self.skipTest('native compiler unavailable: '+native.REASON)
        x=np.array([-1e12,1e12,0.]);y=np.array([-1e12,-1e12,1e12]);q=np.ones(3)
        f=np.array([[0,1,2]],np.int32);mask=np.ones(1,bool)
        a=native.raster(x,y,q,f,mask,24,24)
        b=native.raster_py(x,y,q,f,mask,24,24)
        self.assertEqual(np.count_nonzero(a[0]>=0),24*24)
        for aa,bb in zip(a,b):
            np.testing.assert_allclose(aa,bb,atol=1e-5)


if __name__ == '__main__':
    unittest.main()
