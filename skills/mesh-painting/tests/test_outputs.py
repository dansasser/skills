"""Output authority and launcher outcomes are verified independently of exit status."""
import json
import os
from pathlib import Path
import tempfile
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

from PIL import Image

import common
from meshpaint.__main__ import Outputs
from meshpaint.output import verify_receipt


class GenerationOutputs(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='meshpaint output contract ')
        self.addCleanup(self.tmp.cleanup)
        self.prefix = str(Path(self.tmp.name)/'image')

    def transaction(self, run='first'):
        return Outputs(prefix=self.prefix, run_id=run, stage='render')

    def save(self, outs, colour):
        for suffix in ('_render.png', '_render_tile_r1c1.png'):
            outs.save(Image.new('RGB', (8, 6), colour), self.prefix+suffix)

    def test_manifest_selects_complete_independent_images(self):
        outs = self.transaction()
        self.save(outs, (1, 2, 3))
        receipt = outs.commit()
        manifest = json.loads(Path(self.prefix+'_manifest.json').read_text())
        self.assertEqual(receipt['generation_id'], manifest['generation_id'])
        self.assertEqual(manifest['run_id'], 'first')
        self.assertEqual(len(manifest['images']), 2)
        for entry in manifest['images']:
            authoritative = Path(self.tmp.name)/entry['path']
            export = Path(self.tmp.name)/entry['export']
            self.assertEqual(common.sha(authoritative), entry['sha256'])
            self.assertEqual(authoritative.read_bytes(), export.read_bytes())
            self.assertNotEqual(authoritative.stat().st_ino, export.stat().st_ino)

    def test_second_export_failure_preserves_authoritative_generation(self):
        first = self.transaction()
        self.save(first, (1, 2, 3)); first.commit()
        original = Path(self.prefix+'_manifest.json').read_bytes()
        second = self.transaction('second')
        self.save(second, (100, 90, 80))
        replace = os.replace
        exports = []

        def fail_second(src, dst, *args, **kwargs):
            if str(dst).endswith('.png'):
                exports.append(str(dst))
                if len(exports) == 2:
                    raise OSError('injected second export failure')
            return replace(src, dst, *args, **kwargs)

        with patch('os.replace', fail_second):
            with self.assertRaisesRegex(OSError, 'second export'):
                second.commit()
        second.discard()
        self.assertEqual(Path(self.prefix+'_manifest.json').read_bytes(), original)
        manifest = json.loads(original)
        for entry in manifest['images']:
            self.assertEqual(common.sha(Path(self.tmp.name)/entry['path']), entry['sha256'])

    def test_manifest_failure_does_not_select_candidate(self):
        outs = self.transaction()
        self.save(outs, (1, 2, 3))
        replace = os.replace

        def fail_manifest(src, dst, *args, **kwargs):
            if str(dst).endswith('_manifest.json'):
                raise OSError('injected manifest failure')
            return replace(src, dst, *args, **kwargs)

        with patch('os.replace', fail_manifest):
            with self.assertRaisesRegex(OSError, 'manifest failure'):
                outs.commit()
        outs.discard()
        self.assertFalse(Path(self.prefix+'_manifest.json').exists())

    def test_receipt_requires_dimensions_and_all_tiles(self):
        outs = self.transaction()
        outs.provenance = dict(settings=dict(tiles='1x1'))
        self.save(outs, (1, 2, 3))
        receipt = outs.commit(terminal_reason='preset_complete')
        receipt['dimensions'] = dict(delivery_size=[8, 6], working_size=[8, 6])
        verify_receipt(receipt, run_id='first', stage='render', prefix=self.prefix)
        receipt['dimensions']['working_size'] = [9, 6]
        with self.assertRaisesRegex(ValueError, 'dimensions'):
            verify_receipt(receipt, run_id='first', stage='render', prefix=self.prefix)
        other = self.transaction('missing-tile')
        other.provenance = dict(settings=dict(tiles='1x1'))
        other.save(Image.new('RGB', (8, 6)), self.prefix+'_render.png')
        receipt = other.commit(terminal_reason='preset_complete')
        receipt['dimensions'] = dict(delivery_size=[8, 6], working_size=[8, 6])
        with self.assertRaisesRegex(ValueError, 'missing required'):
            verify_receipt(receipt, run_id='missing-tile', stage='render', prefix=self.prefix)

    def test_generated_output_state_is_ignored(self):
        for path in ('example_manifest.json', 'example_generations/run/image.png'):
            result = subprocess.run(['git', 'check-ignore', '--quiet', path], cwd=common.ROOT)
            self.assertEqual(result.returncode, 0, path)

    def test_staging_symlink_is_refused_without_touching_target(self):
        outs = self.transaction()
        target = Path(self.tmp.name)/'kept'
        target.write_text('untouched')
        entry = Path(self.prefix+'_render.png'+outs.tag)
        entry.symlink_to(target)
        with self.assertRaises(OSError):
            outs.save(Image.new('RGB', (8, 6)), self.prefix+'_render.png')
        self.assertEqual(target.read_text(), 'untouched')
        self.assertTrue(entry.is_symlink())

    def test_corrupt_encoded_image_is_not_selected(self):
        outs = self.transaction()
        self.save(outs, (1, 2, 3))
        Path(self.prefix+'_render.png'+outs.tag).write_bytes(b'not a PNG')
        with self.assertRaises(OSError):
            outs.commit()
        outs.discard()
        self.assertFalse(Path(self.prefix+'_manifest.json').exists())

    def test_prior_complete_generation_remains_inspectable(self):
        first = self.transaction()
        self.save(first, (1, 2, 3)); first.commit()
        second = self.transaction('second')
        self.save(second, (100, 90, 80)); second.commit()
        current = json.loads(Path(self.prefix+'_manifest.json').read_text())
        prior_path = Path(self.tmp.name)/current['previous_generation']['manifest']
        prior = json.loads(prior_path.read_text())
        self.assertEqual(prior['run_id'], 'first')
        for entry in prior['images']:
            path = Path(self.tmp.name)/entry['path']
            self.assertEqual(common.sha(path), entry['sha256'])
            with Image.open(path) as image:
                self.assertEqual(image.getpixel((0, 0)), (1, 2, 3))


class LauncherContracts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='meshpaint launcher contract ')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.scene = self.root/'scene.py'
        self.prefix = str(self.root/'image')

    def run_scene(self, *extra):
        return common.run_engine([str(self.scene), '--out', self.prefix, *extra])

    def test_scene_early_zero_exit_is_failed(self):
        self.scene.write_text('def build():\n    raise SystemExit(0)\n')
        result = self.run_scene()
        self.assertEqual(result.returncode, 1, result.stdout+result.stderr)
        report = json.loads(Path(self.prefix+'_usage.json').read_text())
        self.assertEqual(report['result'], 'failed')
        self.assertIsNone(report['stage_complete'])
        self.assertEqual(report['images_written_by_this_run'], [])

    def test_scene_forged_interrupt_is_failed(self):
        for code in (130, 143):
            self.scene.write_text(f'def build():\n    raise SystemExit({code})\n')
            result = self.run_scene()
            self.assertEqual(result.returncode, 1, result.stdout+result.stderr)
            self.assertEqual(json.loads(Path(self.prefix+'_usage.json').read_text())['result'], 'failed')

    def test_pid_symlink_does_not_change_target(self):
        self.scene.write_text('def build():\n    raise SystemExit(0)\n')
        target = self.root/'owner record'
        target.write_text('untouched')
        Path(self.prefix+'.pid').symlink_to(target)
        result = self.run_scene()
        self.assertEqual(result.returncode, 2, result.stdout+result.stderr)
        self.assertEqual(target.read_text(), 'untouched')
        self.assertTrue(Path(self.prefix+'.pid').is_symlink())

    def test_pid_hardlink_and_fifo_are_refused(self):
        self.scene.write_text('def build():\n    raise SystemExit(0)\n')
        target = self.root/'owner record'
        target.write_text('untouched')
        entry = Path(self.prefix+'.pid')
        os.link(target, entry)
        result = self.run_scene()
        self.assertEqual(result.returncode, 2, result.stdout+result.stderr)
        self.assertEqual(target.read_text(), 'untouched')
        entry.unlink(); os.mkfifo(entry)
        result = self.run_scene()
        self.assertEqual(result.returncode, 2, result.stdout+result.stderr)

    def test_usage_publication_preserves_link_targets_and_replaces_fifo(self):
        self.scene.write_text('def build():\n    raise SystemExit(0)\n')
        target = self.root/'report owner'
        target.write_text('untouched')
        entry = Path(self.prefix+'_usage.json')
        for kind in ('symlink', 'hardlink', 'fifo'):
            if entry.exists() or entry.is_symlink():
                entry.unlink()
            if kind == 'symlink':
                entry.symlink_to(target)
            elif kind == 'hardlink':
                os.link(target, entry)
            else:
                os.mkfifo(entry)
            result = self.run_scene()
            self.assertEqual(result.returncode, 1, result.stdout+result.stderr)
            self.assertEqual(target.read_text(), 'untouched')
            self.assertFalse(entry.is_symlink())
            self.assertEqual(json.loads(entry.read_text())['result'], 'failed')
            self.assertEqual(entry.stat().st_nlink, 1)

    def test_existing_manifest_fifo_does_not_block_failure_report(self):
        self.scene.write_text('def build():\n    raise SystemExit(0)\n')
        os.mkfifo(self.prefix+'_manifest.json')
        result = self.run_scene()
        self.assertEqual(result.returncode, 1, result.stdout+result.stderr)
        report = json.loads(Path(self.prefix+'_usage.json').read_text())
        self.assertIn('regular file', report['manifest_error'])

    def test_invalid_cli_is_rejected_before_scene_execution(self):
        marker = self.root/'scene-loaded'
        self.scene.write_text(f'from pathlib import Path\nPath({str(marker)!r}).touch()\ndef build():\n    raise SystemExit(0)\n')
        for args in [('--tiles', '0x2'), ('--tiles', '2x-1'), ('--tiles', '33x32'),
                     ('--seed', '-1'), ('--sun', 'nan', '30')]:
            result = self.run_scene(*args)
            self.assertEqual(result.returncode, 2, result.stdout+result.stderr)
            self.assertFalse(marker.exists())

    def test_adaptive_config_and_pass_guard_are_bounded_before_scene(self):
        marker = self.root/'scene-loaded'
        self.scene.write_text(f'from pathlib import Path\nPath({str(marker)!r}).touch()\ndef build():\n    raise SystemExit(0)\n')
        config = self.root/'config.json'
        config.write_bytes(b' '*65537)
        for args in [('--paint-config', str(config)), ('--max-passes', '8192')]:
            result = self.run_scene('--paint-policy', 'adaptive-v1', *args)
            self.assertEqual(result.returncode, 2, result.stdout+result.stderr)
            self.assertFalse(marker.exists())
        config.unlink(); os.mkfifo(config)
        result = self.run_scene('--paint-policy', 'adaptive-v1', '--paint-config', str(config))
        self.assertEqual(result.returncode, 2, result.stdout+result.stderr)
        self.assertFalse(marker.exists())

    def test_adaptive_area_override_is_rejected_before_render(self):
        marker = self.root/'render-entered'
        self.scene.write_text('''import numpy as np
from pathlib import Path
from meshpaint.geometry import Camera
from meshpaint.mesh import MeshSet
from meshpaint.scene import Scene
from meshpaint import pipeline
def build():
    meshes = MeshSet()
    meshes.add(np.array([[-2,2,0],[2,2,0],[0,6,0]],np.float32),np.array([[0,1,2]]),2,'up')
    def shade(g):
        return np.full((len(g.x),3),0.3,np.float32),np.zeros(len(g.x)),np.zeros((len(g.x),3))
    def render(*args, **kwargs):
        Path(MARKER).touch()
        raise AssertionError('render must not run')
    pipeline.render=render
    return Scene(meshes,Camera((0,-5,2),0,-4),shade,sea=False,size=(32,32))
'''.replace('MARKER', repr(str(marker))))
        result = self.run_scene('--stage', 'paint', '--scale', '1', '--paint-policy', 'adaptive-v1', '--max-strokes', '1000')
        self.assertEqual(result.returncode, 1, result.stdout+result.stderr)
        self.assertIn('only lower', result.stderr)
        self.assertFalse(marker.exists())

    def test_changed_scene_source_is_not_executed(self):
        marker = self.root/'executed'
        self.scene.write_text('def build():\n    raise SystemExit(0)\n')
        wrapper = '''import sys
from pathlib import Path
sys.path.insert(0,sys.argv.pop(1))
from meshpaint import __main__ as cli
marker=sys.argv.pop()
original=cli._run
def changed(a,*args):
    Path(a.scene).write_text("from pathlib import Path\\nPath("+repr(marker)+").touch()\\ndef build():\\n    raise SystemExit(0)\\n")
    return original(a,*args)
cli._run=changed
cli.main(own_process=True)
'''
        command = [sys.executable, '-B', '-c', wrapper, common.SCRIPTS,
                   str(self.scene), '--out', self.prefix, str(marker)]
        result = subprocess.run(command, cwd=self.root, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 1, result.stdout+result.stderr)
        self.assertIn('source changed during startup', result.stderr)
        self.assertFalse(marker.exists())

    def test_nonregular_scene_is_refused_before_output_side_effects(self):
        fifo = self.root/'scene-fifo.py'
        os.mkfifo(fifo)
        paths = [fifo]
        if Path('/dev/zero').exists():
            paths.append(Path('/dev/zero'))
        for path in paths:
            with self.subTest(path=path):
                started = time.monotonic()
                command = [sys.executable, '-B', common.RUN, str(path), '--out', self.prefix]
                try:
                    result = subprocess.run(command, capture_output=True, text=True, timeout=2)
                except subprocess.TimeoutExpired:
                    self.fail('nonregular scene did not receive a bounded preflight refusal')
                self.assertEqual(result.returncode, 2, result.stdout+result.stderr)
                self.assertIn('regular file', result.stderr)
                self.assertLess(time.monotonic()-started, 2)
                for suffix in ('.pid', '.lock', '_usage.json', '_preview.png'):
                    self.assertFalse(Path(self.prefix+suffix).exists())


if __name__ == '__main__':
    unittest.main()
