"""P4 acceptance: bounded history, overlapping exports and preservation of source files."""
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from PIL import Image

import common
from meshpaint.__main__ import Outputs
from meshpaint.__main__ import _lock_output, _start_time
from meshpaint.output_control import ExportLease
from test_output_ownership import SCENE


class SourceCollision(unittest.TestCase):
    def test_source_control_collisions_preserve_bytes_before_any_output_write(self):
        with tempfile.TemporaryDirectory(prefix='meshpaint source collisions ') as directory:
            root = Path(directory)
            for suffix in ('.pid', '.lock', '_usage.json', '_manifest.json', '_preview.png'):
                with self.subTest(suffix=suffix):
                    prefix = root/('picture'+str(len(suffix)))
                    source = Path(str(prefix)+suffix)
                    data = b'def build():\n    raise SystemExit(0)\n'
                    source.write_bytes(data)
                    result = common.run_engine([str(source), '--out', str(prefix)], timeout=10)
                    self.assertEqual(result.returncode, 2, result.stdout+result.stderr)
                    self.assertEqual(source.read_bytes(), data)
                    self.assertIn('collid', result.stderr.lower())

    def test_hardlinked_output_alias_of_source_is_refused(self):
        with tempfile.TemporaryDirectory(prefix='meshpaint source alias ') as directory:
            root = Path(directory); source = root/'scene.py'; prefix = root/'picture'
            source.write_text(SCENE)
            alias = Path(str(prefix)+'_preview.png'); os.link(source, alias)
            before = source.read_bytes()
            result = common.run_engine([str(source), '--out', str(prefix)], timeout=5)
            self.assertEqual(result.returncode, 2, result.stdout+result.stderr)
            self.assertEqual(source.read_bytes(), before)
            self.assertEqual(source.stat().st_ino, alias.stat().st_ino)
            self.assertFalse(Path(str(prefix)+'.pid').exists())

    def test_config_output_alias_is_refused_without_overwrite(self):
        with tempfile.TemporaryDirectory(prefix='meshpaint config collision ') as directory:
            root = Path(directory); source = root/'scene.py'; prefix = root/'picture'
            source.write_text('def build():\n    raise SystemExit(0)\n')
            config = Path(str(prefix)+'_usage.json'); data = b'{"schema_version":1}\n'; config.write_bytes(data)
            result = common.run_engine([str(source), '--out', str(prefix), '--paint-policy', 'adaptive-v1',
                                       '--paint-config', str(config)])
            self.assertEqual(result.returncode, 2, result.stdout+result.stderr)
            self.assertEqual(config.read_bytes(), data)
            self.assertIn('configuration', result.stderr)


class CrossPrefixCoordination(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='meshpaint export leases ')
        self.addCleanup(self.tmp.cleanup); self.root = Path(self.tmp.name)
        self.scene = self.root/'scene.py'; self.scene.write_text(SCENE)
        self.processes = []; self.addCleanup(self.cleanup)

    def cleanup(self):
        self.scene.with_suffix('.release').touch()
        for process in self.processes:
            if process.poll() is None: process.terminate()
            try: process.communicate(timeout=30)
            except subprocess.TimeoutExpired:
                process.kill(); process.communicate(timeout=5)

    def start(self, name, stage='preview'):
        process = subprocess.Popen([sys.executable, '-B', common.RUN, str(self.scene), '--out', str(self.root/name),
                                    '--stage', stage, '--scale', '1', '--tiles', '0'],
                                   cwd=self.root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.processes.append(process); return process

    def entered(self):
        p = self.scene.with_suffix('.entered')
        return p.read_text().splitlines() if p.exists() else []

    def wait(self, count):
        deadline = time.monotonic()+15
        while time.monotonic()<deadline:
            if len(self.entered()) >= count: return
            time.sleep(.02)
        self.fail('renderer never reached the gated build')

    def test_distinct_overlapping_prefixes_are_refused_without_overwrite(self):
        sentinel = self.root/'picture_render.png'; sentinel.write_bytes(b'previous verified rendering')
        first = self.start('picture', 'render'); self.wait(1)
        second = self.start('picture_render', 'paint')
        try: stdout, stderr = second.communicate(timeout=3)
        except subprocess.TimeoutExpired: self.fail('overlapping prefix was not refused before scene build')
        self.assertEqual(second.returncode, 2, stdout+stderr)
        self.assertIn('overlap', stderr.lower())
        self.assertEqual(sentinel.read_bytes(), b'previous verified rendering')
        self.assertEqual(len(self.entered()), 1)
        self.scene.with_suffix('.release').touch()
        stdout, stderr = first.communicate(timeout=30)
        self.assertEqual(first.returncode, 0, stdout+stderr)

    def test_disjoint_prefixes_still_render_concurrently(self):
        first = self.start('first'); self.wait(1)
        second = self.start('second'); self.wait(2)
        self.assertIsNone(first.poll()); self.assertIsNone(second.poll())
        self.scene.with_suffix('.release').touch()
        for process in (first, second):
            stdout, stderr = process.communicate(timeout=30)
            self.assertEqual(process.returncode, 0, stdout+stderr)

    def test_related_prefixes_with_disjoint_selected_stage_exports_run_together(self):
        first = self.start('picture', 'preview'); self.wait(1)
        second = self.start('picture_render', 'preview'); self.wait(2)
        self.assertIsNone(first.poll()); self.assertIsNone(second.poll())
        self.scene.with_suffix('.release').touch()
        for process in (first, second):
            stdout, stderr = process.communicate(timeout=30)
            self.assertEqual(process.returncode, 0, stdout+stderr)


class BoundedGenerations(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='meshpaint generation retention ')
        self.addCleanup(self.tmp.cleanup); self.root = Path(self.tmp.name); self.prefix = str(self.root/'picture')

    def create(self, run, *, complete=True, partial=False):
        outs = Outputs(prefix=self.prefix, run_id=run, stage='paint')
        suffixes = ['_render.png', '_full.png', '.png'] if complete else ['_partial.png' if partial else '_render.png']
        for suffix in suffixes:
            outs.save(Image.new('RGB', (8, 6), (20, 40, 60)), self.prefix+suffix)
        receipt = outs.commit(terminal_reason='preset_complete' if complete else 'stroke_budget' if partial else 'render_ready', partial=partial)
        return outs, receipt

    def manifest(self):
        return json.loads(Path(self.prefix+'_manifest.json').read_text())

    def check_images(self, manifest):
        for image in manifest['images']:
            path = self.root/image['path']
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), image['sha256'])

    def test_repeated_publications_keep_two_complete_generations_and_report_bytes(self):
        for n in range(5):
            outs, receipt = self.create('run'+str(n))
            self.check_images(self.manifest())
            disk = outs.disk_report
            self.assertLessEqual(disk['retained_generation_count'], 2)
            self.assertGreater(disk['retained_bytes'], 0)
        manifest = self.manifest()
        previous = manifest['previous_good_generation']
        old = json.loads((self.root/previous['manifest']).read_text())
        self.check_images(old)
        self.assertEqual(old['run_id'], 'run3')
        self.assertTrue(outs.disk_report['removed_ids'])

    def test_partial_and_render_ready_do_not_displace_last_complete_painting(self):
        _, good = self.create('good')
        self.create('intermediate', complete=False)
        outs, _ = self.create('partial', complete=False, partial=True)
        manifest = self.manifest(); previous = manifest['previous_good_generation']
        self.assertEqual(previous['generation_id'], good['generation_id'])
        self.check_images(json.loads((self.root/previous['manifest']).read_text()))
        self.assertEqual(outs.disk_report['retained_generation_count'], 2)

    def test_unknown_root_directory_and_scene_are_preserved(self):
        root = Path(self.prefix+'_generations'); root.mkdir()
        unknown = root/'personal project'; unknown.mkdir()
        source = unknown/'scene.py'; source.write_text('def build(): return "private"\n')
        before = source.read_bytes()
        for n in range(4): outs, _ = self.create('run'+str(n))
        self.assertEqual(source.read_bytes(), before)
        self.assertTrue(outs.disk_report['unknown_entries'])
        self.assertLessEqual(outs.disk_report['retained_generation_count'], 2)

    def test_failed_candidate_is_cleaned_without_discarding_last_good(self):
        self.create('good'); selected = Path(self.prefix+'_manifest.json').read_bytes()
        broken = Outputs(prefix=self.prefix, run_id='broken', stage='paint')
        broken.save(Image.new('RGB', (8, 6)), self.prefix+'_full.png')
        with patch('shutil.copyfileobj', side_effect=OSError('injected generation copy failure')):
            with self.assertRaises(OSError): broken.commit(terminal_reason='preset_complete')
        broken.discard()
        self.assertEqual(Path(self.prefix+'_manifest.json').read_bytes(), selected)
        outs, _ = self.create('next')
        self.assertEqual(outs.disk_report['incomplete_generation_count'], 0)
        self.assertLessEqual(outs.disk_report['retained_generation_count'], 2)

    def test_unknown_note_with_journal_temp_prefix_is_not_deleted(self):
        self.create('first')
        note = Path(self.prefix+'_generations')/'.meshpaint-generations.json.part-personal-note'
        note.write_text('keep this private note')
        outs, _ = self.create('second')
        self.assertEqual(note.read_text(), 'keep this private note')
        self.assertTrue(any(note.name in x['path'] for x in outs.disk_report['unknown_entries']))

    def test_unknown_file_in_managed_generation_blocks_new_publication(self):
        self.create('first'); self.create('second')
        selected = Path(self.prefix+'_manifest.json').read_bytes(); manifest = self.manifest()
        prior = self.root/manifest['previous_good_generation']['manifest']
        source = prior.parent/'user scene.py'; data = b'def build(): return "private"\n'; source.write_bytes(data)
        with self.assertRaisesRegex(OSError, 'unknown|changed'):
            self.create('third')
        self.assertEqual(source.read_bytes(), data)
        self.assertEqual(Path(self.prefix+'_manifest.json').read_bytes(), selected)


class LeaseLiveness(unittest.TestCase):
    def test_dead_launcher_identity_does_not_override_a_held_inherited_flock(self):
        with tempfile.TemporaryDirectory(prefix='meshpaint inherited lease ') as directory:
            first = str(Path(directory)/'picture'); second = first+'_render'
            first_fd = _lock_output(first); second_fd = _lock_output(second)
            try:
                old = ExportLease(first, '0', 'old', first_fd, 999999999, '0', stage='render')
                with self.assertRaisesRegex(OSError, 'overlap'):
                    ExportLease(second, '0', 'new', second_fd, os.getpid(), _start_time(os.getpid()), stage='paint')
                os.close(first_fd); first_fd = None
                lease = ExportLease(second, '0', 'new', second_fd, os.getpid(), _start_time(os.getpid()), stage='paint')
                lease.release()
                old.claimed = False
            finally:
                if first_fd is not None: os.close(first_fd)
                os.close(second_fd)


if __name__ == '__main__': unittest.main()
