"""Invalid public inputs fail before counters or native pointers are used."""
import unittest
from unittest.mock import patch
import numpy as np

import common  # noqa: F401
from meshpaint import limits, mesh, native
from meshpaint.geometry import Camera
from meshpaint.scene import Scene


class MeshInputs(unittest.TestCase):
    def setUp(self):
        self.old = limits._active
        limits._active = limits.Budget()
        self.V = np.array([(0, 0, 1), (1, 0, 1), (0, 1, 1)], np.float32)

    def tearDown(self):
        limits._active = self.old

    def test_invalid_faces_do_not_commit_counts(self):
        for faces in ([[-1, 1, 2]], [[0, 1, 3]], [[0., 1.5, 2.]], [[True, False, True]], [[0, 1]]):
            with self.subTest(faces=faces):
                ms = mesh.MeshSet()
                with self.assertRaises(ValueError):
                    ms.add(self.V, faces, 2)
                self.assertEqual((limits.active().vertices, limits.active().triangles, ms.n), (0, 0, 0))
                self.assertEqual(ms.V, [])

    def test_material_attributes_and_orientation_are_atomic(self):
        for kw in (dict(mat=-1), dict(mat=2**40), dict(mat=2.5), dict(mat=[2, 3]),
                   dict(mat=2, attr=[0, 1]), dict(mat=2, attr=[0, np.nan, 1]),
                   dict(mat=2, orient='sideways'), dict(mat=2, orient=(0, np.inf, 1))):
            with self.subTest(kw=kw):
                ms = mesh.MeshSet()
                with self.assertRaises(ValueError):
                    ms.add(self.V, [[0, 1, 2]], **kw)
                self.assertEqual(limits.active().vertices, 0)

    def test_empty_scene_and_bad_helper_inputs_are_explicit(self):
        with self.assertRaisesRegex(ValueError, 'empty'):
            mesh.MeshSet().done()
        for fn in (lambda: mesh.beam((0, 0, 0), (0, 0, 0)),
                   lambda: mesh.beam((0, 0, 0), (0, 0, 1), width=-1),
                   lambda: mesh.lathe(0, 0, [(1, 0, 1)], seg=2),
                   lambda: mesh.blades(n=1.5),
                   lambda: mesh.grid_mesh([0], [0, 1], lambda x, y: x),
                   lambda: mesh.scatter([[0, 0, 0]], self.V, [[0, 1, 3]])):
            with self.assertRaises(ValueError):
                fn()


class NativeInputs(unittest.TestCase):
    def test_malformed_raster_rejected_before_loading_native(self):
        for faces in ([[0, 1, 3]], [[-1, 1, 2]], [[0, 1.5, 2]]):
            with patch.object(native, '_load') as load:
                with self.assertRaises(ValueError):
                    native.raster([0, 1, 0], [0, 0, 1], [1, 1, 1], faces, [True], 8, 8)
                load.assert_not_called()

    def test_bad_stroke_buffers_and_mask_slots_rejected_before_loading(self):
        canvas = np.zeros((8, 8, 3), np.float32)
        height = np.zeros((8, 8), np.float32)
        p = np.array([[2, 2, 0, 3, 2, .5, .5, .5, .9, 0, .1]], np.float32)
        for ca, reg, masks in ((canvas.astype(np.float64), [0], np.ones((1, 8, 8), np.float32)),
                               (canvas, [-1], np.ones((1, 8, 8), np.float32)),
                               (canvas, [1], np.ones((1, 8, 8), np.float32)),
                               (canvas, [0], np.ones((1, 8, 7), np.float32))):
            with patch.object(native, '_load') as load:
                with self.assertRaises(ValueError):
                    native.strokes(ca, height, p, reg, masks, np.ones(8192, np.float32), 1)
                load.assert_not_called()

    def test_signed_colour_pickup_is_preserved_but_unrepresentable_buffers_fail(self):
        canvas=np.zeros((8,8,3),np.float32);height=np.zeros((8,8),np.float32)
        p=np.array([[2,2,0,3,2,-.01,.5,1.02,.9,0,.1]],np.float32)
        masks=np.ones((1,8,8),np.float32);bristle=np.ones(8192,np.float32)
        with patch.object(native,'_load',return_value=False):
            native.strokes(canvas,height,p,[0],masks,bristle,1)
        self.assertLess(float(canvas[...,0].min()),0)
        huge=p.astype(np.float64);huge[0,3]=1e100
        with self.assertRaises(ValueError):
            native.strokes(canvas,height,huge,[0],masks,bristle,1)


class SceneInputs(unittest.TestCase):
    def test_camera_vertical_basis_is_finite_and_orthonormal(self):
        for pitch in (-90, 90):
            cam = Camera((0, 0, 1), 0, pitch)
            cam.setup(32, 24, .02)
            axes = np.stack([cam.fwd, cam.rgt, cam.upv])
            np.testing.assert_allclose(axes @ axes.T, np.eye(3), atol=1e-6)

    def test_scene_and_shader_validation(self):
        from meshpaint.scene import validate_scene, validate_shading
        for kw in (dict(size=(0, 32)), dict(size=(32.5, 32)), dict(shade=None),
                   dict(sea=True, camera=Camera((0, 0, 0), 0, 0)),
                   dict(regions={-1: 2}), dict(stroke_angle={2: float('nan')})):
            args = dict(meshes=mesh.MeshSet(), camera=Camera((0, 0, 1), 0, 0), shade=lambda g: None)
            args.update(kw)
            with self.assertRaises(ValueError):
                validate_scene(Scene(**args))
        with self.assertRaises(ValueError):
            validate_shading((np.ones((2, 3)), np.zeros(2), np.full((2, 3), np.nan)), 2)
        with self.assertRaises(ValueError):
            validate_shading((np.ones((2, 3)), np.ones(2)*-1, np.zeros((2, 3))), 2)


if __name__ == '__main__':
    unittest.main()
