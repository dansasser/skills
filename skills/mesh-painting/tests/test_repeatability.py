"""Finite image publication and repeatability under allocation perturbation."""
import json
from pathlib import Path
import tempfile
import unittest

import numpy as np
from PIL import Image

import common


class FiniteImages(unittest.TestCase):
    def test_nonfinite_values_cannot_be_published_as_pixels(self):
        from meshpaint.__main__ import _to_image

        for value in (np.nan, np.inf, -np.inf):
            with self.subTest(value=value):
                pixels = np.zeros((2, 3, 3), np.float32)
                pixels[1, 2, 0] = value
                with self.assertRaisesRegex(ValueError, 'non-finite'):
                    _to_image(pixels)

    def test_finite_pixel_conversion_is_unchanged(self):
        from meshpaint.__main__ import _to_image

        pixels = np.array([[[-1, 0, 0.5], [0.25, 1, 2]]], np.float32)
        image = _to_image(pixels)
        self.assertEqual(image.size, (2, 1))
        self.assertEqual(image.mode, 'RGB')
        self.assertEqual(image.getpixel((0, 0)), (0, 0, 127))
        self.assertEqual(image.getpixel((1, 0)), (63, 255, 255))

    def test_invalid_render_preserves_finished_images_through_launcher(self):
        with tempfile.TemporaryDirectory(prefix='nonfinite output ') as work:
            root = Path(work)
            for index, value in enumerate(('np.nan', 'np.inf', '-np.inf')):
                with self.subTest(value=value):
                    scene = root / f'invalid-{index}.py'
                    scene.write_text(
                        'import numpy as np\n'
                        'from meshpaint import pipeline\n'
                        'from meshpaint.scene import Scene\n'
                        'def invalid_render(*args, **kwargs):\n'
                        f'    return dict(target=np.full((24, 32, 3), {value}, np.float32))\n'
                        'def build():\n'
                        '    pipeline.render = invalid_render\n'
                        '    return Scene(meshes=None, camera=None, shade=None, size=(32, 24))\n')
                    prefix = root / f'picture-{index}'
                    previous = {}
                    for suffix in ('.png', '_preview.png', '_render.png', '_full.png'):
                        path = Path(str(prefix) + suffix)
                        Image.new('RGB', (32, 24), (37, 71, 109)).save(path)
                        previous[path] = common.sha(path)
                    reply = common.run_engine([str(scene), '--out', str(prefix), '--stage', 'preview'], cwd=work)
                    self.assertEqual(reply.returncode, 1, reply.stdout + reply.stderr)
                    self.assertIn('non-finite', reply.stderr)
                    for image, digest in previous.items():
                        self.assertEqual(common.sha(image), digest)
                    report = json.loads(Path(str(prefix) + '_usage.json').read_text())
                    self.assertEqual(report['result'], 'failed')
                    self.assertEqual(report['images_written_by_this_run'], [])
                    self.assertEqual(report['processes_left_running'], [])
                    self.assertFalse(Path(str(prefix) + '.pid').exists())
                    self.assertEqual(list(root.glob('*.part-*')), [])


@unittest.skipUnless(common.SLOW, 'slow: set MESHPAINT_SLOW=1')
class AllocationRepeatability(unittest.TestCase):
    def test_bundled_previews_are_repeatable_with_distinct_allocation_patterns(self):
        with tempfile.TemporaryDirectory(prefix='preview allocation patterns ') as work:
            root = Path(work)
            for name in ('lighthouse', 'moor'):
                with self.subTest(scene=name):
                    scene = Path(common.ROOT, 'assets', 'scenes', name + '.py')
                    hashes = []
                    for pattern in ('1', '165'):
                        prefix = root / (name + '-' + pattern)
                        reply = common.run_engine([str(scene), '--out', str(prefix), '--stage', 'preview'],
                                                  cwd=work, env={'MALLOC_PERTURB_': pattern})
                        self.assertEqual(reply.returncode, 0, reply.stdout + reply.stderr)
                        image = Path(str(prefix) + '_preview.png')
                        with Image.open(image) as picture:
                            self.assertEqual(picture.size, (1600, 1000))
                        hashes.append(common.sha(image))
                    self.assertEqual(hashes[0], hashes[1], name + ' differs between allocation patterns')


if __name__ == '__main__':
    unittest.main()
