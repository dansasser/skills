"""Scene paint-region validation and the existing sparse-mask cost."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np
from PIL import Image

import common
from meshpaint import limits, scene


SCENE = """import numpy as np
from meshpaint.geometry import Camera
from meshpaint.mesh import MeshSet, grid_mesh
from meshpaint.scene import Scene

def shade(g):
    n = len(g.x)
    return np.full((n, 3), 0.3, np.float32), np.zeros(n, np.float32), np.zeros((n, 3), np.float32)

def build():
    ms = MeshSet()
    ms.add(*grid_mesh(np.linspace(-30, 30, 3), np.linspace(0, 60, 3),
                     lambda x, y: np.zeros_like(x), None), 2, 'up')
    return Scene(meshes=ms, camera=Camera(eye=(0, -40, 8), bearing=0, pitch=-4),
                 shade=shade, sea=False, lens_ghosts=False, size=(32, 24), {regions})
"""


def built_scene(**kwargs):
    return scene.Scene(meshes=None, camera=None, shade=None, **kwargs)


class RegionValues(unittest.TestCase):
    def test_python_and_numpy_integer_boundaries_and_sparse_ids_are_valid(self):
        kinds = (int, np.int8, np.int16, np.int32, np.int64,
                 np.uint8, np.uint16, np.uint32, np.uint64)
        for kind in kinds:
            for value in (2, 127):
                with self.subTest(kind=kind.__name__, value=value):
                    sc = built_scene(default_region=kind(value), regions={2: kind(2), 7: kind(127)})
                    scene.validate_regions(sc)
                    self.assertEqual(sc.default_region, value)
                    self.assertEqual(sc.regions[7], 127)

    def test_reserved_negative_and_overflow_values_identify_the_field(self):
        for value in (-128, -1, 0, 1, 128, 1000, np.int64(128), np.uint64(2**64 - 1)):
            for mapped in (False, True):
                with self.subTest(value=value, mapped=mapped):
                    sc = built_scene(regions={7: value}) if mapped else built_scene(default_region=value)
                    field = 'Scene.regions[7]' if mapped else 'Scene.default_region'
                    with self.assertRaises(ValueError) as caught:
                        scene.validate_regions(sc)
                    message = str(caught.exception)
                    for part in (field, repr(value), '2', '127', '0', '1'):
                        self.assertIn(part, message)

    def test_non_integer_values_are_rejected_even_when_numerically_in_range(self):
        for value in (True, False, np.bool_(True), 2.0, np.float64(2), '2', None, np.array(2)):
            for mapped in (False, True):
                with self.subTest(value=repr(value), mapped=mapped):
                    sc = built_scene(regions={9: value}) if mapped else built_scene(default_region=value)
                    field = 'Scene.regions[9]' if mapped else 'Scene.default_region'
                    with self.assertRaises(ValueError) as caught:
                        scene.validate_regions(sc)
                    self.assertIn(field, str(caught.exception))
                    self.assertIn(repr(value), str(caught.exception))
                    self.assertIn('integer', str(caught.exception))

    def test_invalid_scene_is_rejected_before_canvas_checks_and_rendering(self):
        from meshpaint import __main__, pipeline

        with tempfile.TemporaryDirectory(prefix='region validation ') as work:
            path = Path(work, 'scene.py')
            path.write_text(SCENE.format(regions='default_region=128'))
            args = SimpleNamespace(scene=str(path), seed=11, stage='preview', scale=1)
            with patch.object(limits, '_active', limits.Budget()), \
                    patch.object(limits, 'check_pixels') as pixels, \
                    patch.object(pipeline, 'render') as render:
                with self.assertRaisesRegex(ValueError, 'Scene.default_region'):
                    __main__._job(args, None)
                pixels.assert_not_called()
                render.assert_not_called()


class RegionLauncher(unittest.TestCase):
    def test_invalid_default_and_mapped_regions_preserve_completed_images(self):
        cases = (
            ('default_region=128', 'Scene.default_region', '128'),
            ('default_region=2, regions={2: np.int64(128)}', 'Scene.regions[2]', '128'),
        )
        with tempfile.TemporaryDirectory(prefix='region launcher ') as work:
            root = Path(work)
            for index, (regions, field, value) in enumerate(cases):
                with self.subTest(regions=regions):
                    path = root / f'scene-{index}.py'
                    path.write_text(SCENE.format(regions=regions))
                    prefix = root / f'output-{index}'
                    before = {}
                    for suffix in ('.png', '_full.png', '_render.png', '_preview.png'):
                        image = Path(str(prefix) + suffix)
                        Image.new('RGB', (32, 24), (37, 71, 109)).save(image)
                        before[image] = common.sha(image)
                    result = common.run_engine([str(path), '--out', str(prefix), '--stage', 'preview'],
                                               cwd=work)
                    self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                    for part in (field, value, '2', '127'):
                        self.assertIn(part, result.stderr)
                    self.assertNotIn('OverflowError', result.stderr)
                    for image, digest in before.items():
                        self.assertEqual(common.sha(image), digest)
                    report = json.loads(Path(str(prefix) + '_usage.json').read_text())
                    self.assertEqual(report['result'], 'failed')
                    self.assertEqual(report['images_written_by_this_run'], [])
                    self.assertEqual(report['processes_left_running'], [])
                    self.assertFalse(Path(str(prefix) + '.pid').exists())

    def test_boundaries_and_sparse_mapping_complete_preview(self):
        cases = ('default_region=np.int64(2)', 'default_region=np.uint8(127)',
                 'default_region=2, regions={2: np.int16(127)}')
        with tempfile.TemporaryDirectory(prefix='valid regions ') as work:
            root = Path(work)
            for index, regions in enumerate(cases):
                with self.subTest(regions=regions):
                    path = root / f'scene-{index}.py'
                    path.write_text(SCENE.format(regions=regions))
                    prefix = root / f'output-{index}'
                    result = common.run_engine([str(path), '--out', str(prefix), '--stage', 'preview'],
                                               cwd=work)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    with Image.open(str(prefix) + '_preview.png') as image:
                        self.assertEqual(image.size, (32, 24))
                    report = json.loads(Path(str(prefix) + '_usage.json').read_text())
                    self.assertEqual(report['result'], 'completed')


class SparseMaskCost(unittest.TestCase):
    def test_sparse_region_127_retains_a_plane_for_each_id(self):
        from meshpaint import paint

        regions = np.full((8, 8), 2, np.int8)
        regions[:, 4:] = 127
        captured = {}

        class MasksCaptured(Exception):
            pass

        def capture_masks(canvas, height, strokes, ids, masks, bristle, seed):
            captured['masks'] = masks
            raise MasksCaptured

        with patch.object(limits, '_active', limits.Budget()), \
                patch.object(paint.native, 'strokes', side_effect=capture_masks):
            with self.assertRaises(MasksCaptured):
                paint.paint(np.full((8, 8, 3), 0.5, np.float32), regions, {}, 0.05,
                            0, np.random.default_rng(11), lambda message: None)
        masks = captured['masks']
        self.assertEqual(masks.shape, (128, 8, 8))
        self.assertEqual(masks.dtype, np.dtype(np.float32))
        self.assertTrue(masks.flags.c_contiguous)
        self.assertEqual(masks.nbytes, 4 * 128 * 8 * 8)
        self.assertEqual(np.count_nonzero(masks[3:127]), 0)


if __name__ == '__main__':
    unittest.main()
