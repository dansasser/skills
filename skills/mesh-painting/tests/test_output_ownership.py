"""One output prefix has one owner through rendering and process recovery."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

import common


SCENE = '''import os
import time
from pathlib import Path
import numpy as np
from meshpaint.geometry import Camera
from meshpaint.mesh import MeshSet, grid_mesh
from meshpaint.scene import Scene

def build():
    marker = Path(__file__).with_suffix('.entered')
    with marker.open('a') as fh:
        fh.write(str(os.getpid()) + '\\n')
    release = Path(__file__).with_suffix('.release')
    while not release.exists():
        time.sleep(0.02)
    meshes = MeshSet()
    meshes.add(*grid_mesh(np.array([-2., 2.]), np.array([2., 6.]),
                         lambda x, y: np.zeros_like(x), None), 2, 'up')
    def shade(g):
        return (np.full((len(g.x), 3), 0.3, np.float32),
                np.zeros(len(g.x), np.float32), np.zeros((len(g.x), 3), np.float32))
    return Scene(meshes=meshes, camera=Camera(eye=(0, -5, 2), bearing=0, pitch=-4),
                 shade=shade, sea=False, size=(32, 32))
'''


class OutputOwnership(unittest.TestCase):
    def setUp(self):
        self.workspace = tempfile.TemporaryDirectory(prefix='meshpaint ownership ')
        self.addCleanup(self.workspace.cleanup)
        self.root = Path(self.workspace.name)
        self.scene = self.root / 'scene.py'
        self.scene.write_text(SCENE)
        self.prefix = self.root / 'picture'
        self.processes = []
        self.addCleanup(self._stop_processes)

    def _stop_processes(self):
        self.scene.with_suffix('.release').touch()
        for process in self.processes:
            if process.poll() is None:
                process.terminate()
            try:
                process.communicate(timeout=40)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=10)

    def start(self, *, synchronized=False, prefix=None, extra=()):
        command = [sys.executable, '-B', common.RUN]
        if synchronized:
            # Force the old read/check/write implementation to read the missing
            # PID file in both launchers before either can write it. With a lock,
            # the second launcher can only inspect it for its refusal diagnostic.
            wrapper = '''import os, sys, time
from pathlib import Path
sys.path.insert(0, sys.argv.pop(1))
from meshpaint import __main__ as cli
read = cli._read_pidfile
def synchronized_read(path):
    error = None
    try:
        result = read(path)
    except (OSError, ValueError) as exc:
        error = exc
    marker = Path(path + '.checked.' + str(os.getpid()))
    marker.touch()
    until = time.monotonic() + 1.5
    while len(list(marker.parent.glob(marker.name.split('.checked.')[0] + '.checked.*'))) < 2 and time.monotonic() < until:
        time.sleep(0.01)
    if error is not None:
        raise error
    return result
cli._read_pidfile = synchronized_read
cli.main(own_process=True)
'''
            command = [sys.executable, '-B', '-c', wrapper, common.SCRIPTS]
        command += [str(self.scene), '--out', str(prefix or self.prefix), '--stage', 'preview']
        command += list(extra)
        process = subprocess.Popen(command, cwd=self.root, env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1'),
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.processes.append(process)
        return process

    def wait_for(self, predicate, timeout=20):
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            if predicate():
                return
            time.sleep(0.02)
        self.fail('timed out waiting for ownership/process state')

    def entered(self):
        marker = self.scene.with_suffix('.entered')
        return marker.read_text().splitlines() if marker.exists() else []

    def release(self):
        self.scene.with_suffix('.release').touch()

    def finish(self, process, expected=0):
        out, err = process.communicate(timeout=40)
        self.assertEqual(process.returncode, expected, out + err)
        return out, err

    def test_simultaneous_starts_have_exactly_one_renderer(self):
        first = self.start(synchronized=True)
        second = self.start(synchronized=True)
        self.wait_for(lambda: any(p.poll() is not None for p in (first, second)), timeout=8)
        loser, winner = (first, second) if first.poll() is not None else (second, first)
        _, error = self.finish(loser, 2)
        self.assertIn('another run', error)
        self.wait_for(lambda: bool(self.entered()))
        self.assertEqual(len(self.entered()), 1)
        pidfile = Path(str(self.prefix) + '.pid')
        self.assertEqual(int(pidfile.read_text().split()[0]), winner.pid)
        self.assertIsNone(winner.poll())
        self.release()
        self.finish(winner)
        self.assertFalse(pidfile.exists())
        self.assertEqual(len(self.entered()), 1)

    def test_lock_inode_persists_and_later_run_can_acquire_it(self):
        self.release()
        self.finish(self.start())
        lockfile = Path(str(self.prefix) + '.lock')
        self.assertTrue(lockfile.exists())
        inode = lockfile.stat().st_ino
        self.finish(self.start())
        self.assertEqual(lockfile.stat().st_ino, inode)

    def test_independent_prefixes_can_render_together(self):
        first = self.start()
        self.wait_for(lambda: len(self.entered()) == 1)
        second = self.start(prefix=self.root / 'other picture')
        self.wait_for(lambda: len(self.entered()) == 2)
        self.assertIsNone(first.poll())
        self.assertIsNone(second.poll())
        self.release()
        self.finish(first)
        self.finish(second)

    def test_refusal_preserves_owner_pid_and_existing_outputs(self):
        kept = {}
        for suffix, data in [('.png', b'previous painting'), ('_preview.png', b'previous preview'),
                             ('_usage.json', b'{"previous":true}')]:
            path = Path(str(self.prefix) + suffix)
            path.write_bytes(data)
            kept[path] = data
        owner = self.start()
        self.wait_for(lambda: bool(self.entered()))
        pidfile = Path(str(self.prefix) + '.pid')
        identity = pidfile.read_bytes()
        _, error = self.finish(self.start(), 2)
        self.assertIn('another run', error)
        self.assertEqual(pidfile.read_bytes(), identity)
        for path, data in kept.items():
            self.assertEqual(path.read_bytes(), data)
        self.release()
        self.finish(owner)
        self.assertEqual(Path(str(self.prefix) + '.png').read_bytes(), b'previous painting')

    def test_stale_pid_is_replaced_without_signalling_a_stranger(self):
        stranger = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
        self.addCleanup(stranger.wait)
        self.addCleanup(stranger.kill)
        pidfile = Path(str(self.prefix) + '.pid')
        pidfile.write_text(f'{stranger.pid} 0')
        owner = self.start()
        self.wait_for(lambda: bool(self.entered()))
        self.assertEqual(int(pidfile.read_text().split()[0]), owner.pid)
        self.assertIsNone(stranger.poll())
        self.release()
        self.finish(owner)
        self.assertIsNone(stranger.poll())

    def test_scene_failure_and_limit_stop_release_ownership(self):
        kept = Path(str(self.prefix) + '.png')
        kept.write_bytes(b'finished painting')
        self.scene.write_text("def build():\n    raise RuntimeError('deliberate scene failure')\n")
        self.finish(self.start(), 1)
        self.assertEqual(json.loads(Path(str(self.prefix) + '_usage.json').read_text())['result'], 'failed')
        self.assertFalse(Path(str(self.prefix) + '.pid').exists())
        self.assertEqual(kept.read_bytes(), b'finished painting')
        self.scene.write_text(SCENE)
        self.finish(self.start(extra=('--time-limit', '0.3')), 3)
        report = json.loads(Path(str(self.prefix) + '_usage.json').read_text())
        self.assertEqual(report['result'], 'stopped')
        self.assertFalse(Path(str(self.prefix) + '.pid').exists())
        self.assertEqual(kept.read_bytes(), b'finished painting')
        self.release()
        self.finish(self.start())

    def test_interrupt_and_stop_release_ownership(self):
        for mode in ('interrupt', 'stop'):
            with self.subTest(mode=mode):
                self.scene.with_suffix('.entered').unlink(missing_ok=True)
                self.scene.with_suffix('.release').unlink(missing_ok=True)
                process = self.start()
                self.wait_for(lambda: bool(self.entered()))
                if mode == 'interrupt':
                    process.send_signal(signal.SIGINT)
                    self.finish(process, 130)
                else:
                    reply = common.run_engine(['--stop', '--out', str(self.prefix)], cwd=self.root)
                    self.assertEqual(reply.returncode, 0, reply.stderr)
                    self.assertIn('stopped', reply.stdout)
                    self.finish(process, 143)
                self.assertFalse(Path(str(self.prefix) + '.pid').exists())
                report = json.loads(Path(str(self.prefix) + '_usage.json').read_text())
                self.assertEqual(report['processes_left_running'], [])
                self.release()
                self.finish(self.start())

    def test_killed_launcher_keeps_ownership_until_supervisor_exits(self):
        # Delay only supervisor cleanup to make the inherited-descriptor window
        # deterministic. A new launcher must not pass the dead launcher's PID
        # check and start writing while that supervisor is still finishing.
        wrapper = '''import sys, time
from pathlib import Path
sys.path.insert(0, sys.argv.pop(1))
from meshpaint import __main__ as cli
cleanup = cli._kill_leftovers
def delayed_cleanup():
    marker = Path(sys.argv[sys.argv.index('--out') + 1] + '.supervisor-cleanup')
    marker.touch()
    time.sleep(1.0)
    return cleanup()
cli._kill_leftovers = delayed_cleanup
cli.main(own_process=True)
'''
        command = [sys.executable, '-B', '-c', wrapper, common.SCRIPTS, str(self.scene),
                   '--out', str(self.prefix), '--stage', 'preview']
        owner = subprocess.Popen(command, cwd=self.root, env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1'),
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.processes.append(owner)
        self.wait_for(lambda: bool(self.entered()))
        renderer = int(self.entered()[0])
        from meshpaint import __main__ as cli
        renderer_start = cli._start_time(renderer)
        owner.kill()
        self.wait_for(lambda: owner.poll() is not None)
        self.wait_for(lambda: Path(str(self.prefix) + '.supervisor-cleanup').exists())
        _, error = self.finish(self.start(), 2)
        self.assertIn('another run', error)
        self.wait_for(lambda: not cli._is_meshpaint(renderer, renderer_start))
        self.wait_for(lambda: Path(str(self.prefix) + '_usage.json').exists())
        self.finish(owner, -signal.SIGKILL)
        self.release()
        self.finish(self.start())
        self.assertFalse(Path(str(self.prefix) + '.pid').exists())

    def test_stop_cleanup_cannot_remove_a_new_owner_pid(self):
        from meshpaint import __main__ as cli
        pidfile = Path(str(self.prefix) + '.pid')
        pidfile.write_text('1 0')
        owner = self.start()
        self.wait_for(lambda: bool(self.entered()))
        current = pidfile.read_bytes()
        cli._remove_stopped_pidfile(str(pidfile), (1, '0'))
        self.assertEqual(pidfile.read_bytes(), current)
        self.release()
        self.finish(owner)
        pidfile.write_text('987654 42')                # a later generation's state after its lock is released
        cli._remove_stopped_pidfile(str(pidfile), (1, '0'))
        self.assertEqual(pidfile.read_text(), '987654 42')

    def test_lock_is_held_after_report_and_through_pid_cleanup(self):
        self.release()
        wrapper = '''import os, sys, time
from pathlib import Path
sys.path.insert(0, sys.argv.pop(1))
from meshpaint import __main__ as cli
prefix = sys.argv[sys.argv.index('--out') + 1]
remove = os.remove
def delayed_pid_cleanup(path, *args, **kwargs):
    remove(path, *args, **kwargs)
    if path == prefix + '.pid':
        Path(prefix + '.pid-cleanup').touch()
        until = time.monotonic() + 10
        while not Path(prefix + '.finish-cleanup').exists() and time.monotonic() < until:
            time.sleep(0.01)
os.remove = delayed_pid_cleanup
cli.main(own_process=True)
'''
        command = [sys.executable, '-B', '-c', wrapper, common.SCRIPTS, str(self.scene),
                   '--out', str(self.prefix), '--stage', 'preview']
        owner = subprocess.Popen(command, cwd=self.root, env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1'),
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.processes.append(owner)
        try:
            self.wait_for(lambda: Path(str(self.prefix) + '.pid-cleanup').exists())
            self.assertEqual(json.loads(Path(str(self.prefix) + '_usage.json').read_text())['result'], 'completed')
            self.assertFalse(Path(str(self.prefix) + '.pid').exists())
            _, error = self.finish(self.start(), 2)
            self.assertIn('another run', error)
            self.assertIsNone(owner.poll())
        finally:
            Path(str(self.prefix) + '.finish-cleanup').touch()
        self.finish(owner)
        self.finish(self.start())

