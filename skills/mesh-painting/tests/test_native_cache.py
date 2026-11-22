"""Native cache trust, recovery and concurrent-build regression checks."""
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

import common


PROBE = """import json, sys
sys.path.insert(0, sys.argv[1])
from meshpaint import native
lib, reason = native._build()
print(json.dumps({'native': bool(lib), 'reason': reason,
                  'symbols': bool(lib) and hasattr(lib, 'mp_raster') and hasattr(lib, 'mp_strokes')}))
"""

SCENE = """import numpy as np
from meshpaint.geometry import Camera
from meshpaint.mesh import MeshSet, quad
from meshpaint.scene import Scene

def shade(g):
    n = len(g.x)
    return np.full((n, 3), 0.4, np.float32), np.zeros(n, np.float32), np.zeros((n, 3), np.float32)

def build():
    m = MeshSet()
    m.add(*quad((-3, -3, 0), (3, -3, 0), (3, 3, 0), (-3, 3, 0)), 2)
    return Scene(meshes=m, camera=Camera(eye=(0, -8, 4), bearing=0, pitch=-15),
                 shade=shade, sea=False, size=(64, 48))
"""


class NativeCache(unittest.TestCase):
    def setUp(self):
        self.compiler = shutil.which('cc') or shutil.which('gcc') or shutil.which('clang')
        if not self.compiler:
            self.skipTest('a C compiler is needed for native-cache checks')
        self.work = tempfile.TemporaryDirectory(prefix='meshpaint native cache ')
        self.addCleanup(self.work.cleanup)
        self.root = Path(self.work.name)
        self.cache = self.root / 'cache'
        self.cache.mkdir(mode=0o700)
        self.tmp = self.root / 'tmp'
        self.tmp.mkdir(mode=0o700)
        self.marker = self.root / 'constructor-marker'

    def env(self, **extra):
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1', OPENBLAS_NUM_THREADS='1',
                   OMP_NUM_THREADS='1', XDG_CACHE_HOME=str(self.cache), TMPDIR=str(self.tmp),
                   MESHPAINT_TEST_MARKER=str(self.marker))
        env.pop('MESHPAINT_NO_NATIVE', None)
        env.update(extra)
        return env

    def probe(self, **extra):
        result = subprocess.run([sys.executable, '-B', '-c', PROBE, common.SCRIPTS],
                                env=self.env(**extra), capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def good(self, **extra):
        result = self.probe(**extra)
        self.assertTrue(result['native'], result['reason'])
        self.assertTrue(result['symbols'])
        return result

    def library(self):
        libraries = list(self.cache.rglob('core.so'))
        self.assertEqual(len(libraries), 1)
        return libraries[0]

    def evil_library(self, target):
        source = self.root / 'marker.c'
        source.write_text('#include <stdio.h>\n#include <stdlib.h>\n'
                          '__attribute__((constructor)) static void mark(void) {\n'
                          ' const char *p = getenv("MESHPAINT_TEST_MARKER");\n'
                          ' if (p) { FILE *f = fopen(p, "w"); if (f) { fputs("loaded", f); fclose(f); } }\n'
                          '}\n')
        subprocess.run([self.compiler, '-shared', '-fPIC', str(source), '-o', str(target)],
                       check=True, capture_output=True)
        target.chmod(0o600)

    def wrapper(self, fail=False, delay=0):
        path = self.root / 'compiler wrapper.py'
        log = self.root / 'compiler-calls'
        path.write_text('import subprocess, sys, time\n'
                        f'with open({str(log)!r}, "a") as f: f.write("called\\n")\n'
                        f'time.sleep({delay!r})\n'
                        + ('sys.exit(1)\n' if fail else
                           f'sys.exit(subprocess.call([{self.compiler!r}] + sys.argv[1:]))\n'))
        return shlex.join([sys.executable, str(path)]), log

    def test_legacy_predictable_temporary_library_is_never_loaded_by_launcher(self):
        # This is the original constructor-execution reproduction, with harmless output.
        source = Path(common.SCRIPTS, 'meshpaint', 'core.c').read_bytes()
        legacy = self.tmp / ('meshpaint_' + hashlib.sha1(source).hexdigest()[:12])
        legacy.mkdir(mode=0o777)
        self.evil_library(legacy / 'core.so')
        blocked = self.root / 'not-a-cache-directory'
        blocked.write_text('occupied')
        scene = self.root / 'scene.py'
        scene.write_text(SCENE)
        result = common.run_engine([str(scene), '--out', 'picture', '--stage', 'preview'],
                                   cwd=str(self.root), env=self.env(XDG_CACHE_HOME=str(blocked)))
        self.assertFalse(self.marker.exists(), 'a pre-populated native constructor executed')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue((self.root / 'picture_preview.png').is_file())
        self.assertTrue((legacy / 'core.so').is_file())
        self.assertEqual(list(self.tmp.glob('meshpaint-native-*')), [], 'temporary native cache leaked')

    def test_fresh_compilation_and_private_permissions(self):
        self.good()
        library = self.library()
        self.assertEqual(stat.S_IMODE(library.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(library.parent.stat().st_mode), 0o700)
        receipt = library.with_name('core.json')
        record = json.loads(receipt.read_text())
        self.assertEqual(record['library_sha256'], hashlib.sha256(library.read_bytes()).hexdigest())
        self.assertEqual(record['source_sha256'], hashlib.sha256(Path(common.SCRIPTS, 'meshpaint', 'core.c').read_bytes()).hexdigest())
        self.assertEqual(stat.S_IMODE(receipt.stat().st_mode), 0o600)

    def test_trusted_cache_reused_without_compiler(self):
        cc, log = self.wrapper()
        self.good(CC=cc)
        before = self.library().stat().st_mtime_ns
        self.good(CC=cc)
        self.assertEqual(log.read_text().splitlines(), ['called'])
        self.assertEqual(self.library().stat().st_mtime_ns, before)

    def test_cc_wrapper_can_close_descriptors_and_change_directory(self):
        bindir = self.root / 'compiler-tools'
        bindir.mkdir(mode=0o700)
        for name in ('findmnt', 'as', 'ld'):
            tool = shutil.which(name)
            if tool:
                (bindir / name).symlink_to(tool)
        wrapper = self.root / 'isolated compiler.py'
        outcome = self.root / 'compiler-outcome.json'
        wrapper.write_text('import json, os, subprocess, sys\n'
                           'os.chdir(os.path.sep)\n'
                           f'r = subprocess.run([{self.compiler!r}] + sys.argv[1:], capture_output=True, text=True)\n'
                           f'with open({str(outcome)!r}, "w") as f: json.dump(dict(exit_code=r.returncode, stderr=r.stderr), f)\n'
                           'sys.exit(r.returncode)\n')
        cc = shlex.join([sys.executable, str(wrapper)])
        self.good(CC=cc, PATH=str(bindir))
        record = json.loads(outcome.read_text())
        self.assertEqual(record['exit_code'], 0, record['stderr'])
        for name in ('cc', 'gcc', 'clang'):
            self.assertIsNone(shutil.which(name, path=str(bindir)))

    def test_missing_receipt_requires_rebuild_before_loading_prepopulation(self):
        self.good()
        library = self.library()
        library.with_name('core.json').unlink()
        self.evil_library(library)
        self.good()
        self.assertFalse(self.marker.exists())

    def test_library_digest_mismatch_never_loads_prepopulation(self):
        self.good()
        library = self.library()
        self.evil_library(library)
        self.good()
        self.assertFalse(self.marker.exists())

    def test_damaged_trusted_library_is_rebuilt(self):
        self.good()
        library = self.library()
        library.write_bytes(b'not an ELF shared library')
        receipt = library.with_name('core.json')
        record = json.loads(receipt.read_text())
        record['library_sha256'] = hashlib.sha256(library.read_bytes()).hexdigest()
        receipt.write_text(json.dumps(record))
        self.good()
        self.assertGreater(library.stat().st_size, 100)

    def test_unavailable_primary_uses_private_temporary_cache_and_cleans_it(self):
        blocked = self.root / 'blocked'
        blocked.write_text('occupied')
        self.good(XDG_CACHE_HOME=str(blocked))
        self.assertEqual(list(self.tmp.iterdir()), [])
        self.assertEqual(blocked.read_text(), 'occupied')

    def test_primary_cache_symlink_is_rejected(self):
        target = self.root / 'target'
        target.mkdir()
        self.cache.rmdir()
        self.cache.symlink_to(target, target_is_directory=True)
        self.good()
        self.assertEqual(list(target.iterdir()), [])

    def test_unsafe_cache_directory_is_not_used_or_repaired(self):
        self.good()
        library = self.library()
        library.parent.chmod(0o777)
        self.evil_library(library)
        before = library.read_bytes()
        self.good()
        self.assertFalse(self.marker.exists())
        self.assertEqual(library.read_bytes(), before)
        self.assertEqual(stat.S_IMODE(library.parent.stat().st_mode), 0o777)

    def test_unsafe_library_permissions_are_rejected_before_load(self):
        self.good()
        library = self.library()
        self.evil_library(library)
        library.chmod(0o666)
        before = library.read_bytes()
        self.good()
        self.assertFalse(self.marker.exists())
        self.assertEqual(library.read_bytes(), before)

    def test_library_symlink_cannot_redirect_loading(self):
        self.good()
        library = self.library()
        foreign = self.root / 'foreign.so'
        self.evil_library(foreign)
        library.unlink()
        library.symlink_to(foreign)
        self.good()
        self.assertFalse(self.marker.exists())
        self.assertTrue(library.is_symlink())

    def test_receipt_symlink_is_rejected(self):
        self.good()
        receipt = self.library().with_name('core.json')
        foreign = self.root / 'foreign.json'
        foreign.write_text(receipt.read_text())
        receipt.unlink()
        receipt.symlink_to(foreign)
        self.good()
        self.assertTrue(receipt.is_symlink())

    def test_hardlinked_library_is_not_trusted(self):
        self.good()
        library = self.library()
        other = self.root / 'library-link'
        os.link(library, other)
        self.good()
        self.assertEqual(library.stat().st_nlink, 2)

    def test_disabled_native_does_not_create_cache(self):
        result = self.probe(MESHPAINT_NO_NATIVE='1')
        self.assertFalse(result['native'])
        self.assertEqual(result['reason'], 'MESHPAINT_NO_NATIVE=1')
        self.assertEqual(list(self.cache.iterdir()), [])

    def test_compiler_failure_uses_python_without_partial_cache_files(self):
        bindir = self.root / 'no-compilers'
        bindir.mkdir()
        monitor = shutil.which('findmnt')
        self.assertIsNotNone(monitor)
        (bindir / 'findmnt').symlink_to(monitor)
        cc, _ = self.wrapper(fail=True)
        result = self.probe(CC=cc, PATH=str(bindir))
        self.assertFalse(result['native'])
        self.assertIn('compiler', result['reason'])
        self.assertFalse(list(self.cache.rglob('core.so')))
        self.assertFalse(list(self.cache.rglob('*.tmp')))
        self.assertEqual(list(self.tmp.iterdir()), [])

    def test_wrong_cache_owner_is_rejected_before_loading(self):
        self.good()
        program = PROBE.replace('lib, reason = native._build()',
                                'uid = native.os.geteuid()\n'
                                'native.os.geteuid = lambda: uid + 1\n'
                                'def forbidden(*args, **kwargs):\n'
                                '    raise AssertionError("untrusted cache reached CDLL")\n'
                                'native.ctypes.CDLL = forbidden\n'
                                'lib, reason = native._build()')
        result = subprocess.run([sys.executable, '-B', '-c', program, common.SCRIPTS],
                                env=self.env(), capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(json.loads(result.stdout)['native'])
        self.assertIn('ownership', json.loads(result.stdout)['reason'])

    def test_build_lock_symlink_is_not_followed(self):
        self.good()
        lock = self.library().with_name('build.lock')
        target = self.root / 'foreign-lock'
        target.write_text('preserve')
        lock.unlink()
        lock.symlink_to(target)
        self.good()
        self.assertTrue(lock.is_symlink())
        self.assertEqual(target.read_text(), 'preserve')

    def test_nonregular_cached_library_is_rejected_without_blocking(self):
        self.good()
        library = self.library()
        library.unlink()
        os.mkfifo(library, 0o600)
        self.good()
        self.assertTrue(stat.S_ISFIFO(library.stat().st_mode))

    def test_writable_cache_ancestor_is_rejected(self):
        self.cache.chmod(0o777)
        self.good()
        self.assertEqual(list(self.cache.iterdir()), [])

    def test_failure_of_both_storage_locations_preserves_python_fallback(self):
        program = PROBE.replace('lib, reason = native._build()',
                                'def unavailable(*args, **kwargs):\n'
                                '    raise OSError("no trusted storage")\n'
                                'native._primary_directory = unavailable\n'
                                'native.tempfile.TemporaryDirectory = unavailable\n'
                                'lib, reason = native._build()')
        result = subprocess.run([sys.executable, '-B', '-c', program, common.SCRIPTS],
                                env=self.env(), capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(json.loads(result.stdout)['native'])
        self.assertIn('no trusted storage', json.loads(result.stdout)['reason'])

    def test_compiler_uses_source_snapshot_bound_to_receipt(self):
        source_root = self.root / 'mutable-source'
        source_root.mkdir()
        source = source_root / 'core.c'
        original = Path(common.SCRIPTS, 'meshpaint', 'core.c').read_bytes()
        source.write_bytes(original)
        wrapper = self.root / 'changing-source.py'
        outcome = self.root / 'snapshot-compiler-outcome.json'
        wrapper.write_text('from pathlib import Path\nimport subprocess, sys\n'
                           f'Path({str(source)!r}).write_text("source changed during compilation")\n'
                           f'r = subprocess.run([{self.compiler!r}] + sys.argv[1:], capture_output=True, text=True)\n'
                           f'Path({str(outcome)!r}).write_text(str(r.returncode) + "\\n" + r.stderr)\n'
                           'sys.exit(r.returncode)\n')
        program = PROBE.replace('lib, reason = native._build()',
                                f'native._HERE = {str(source_root)!r}\n'
                                'lib, reason = native._build()')
        cc = shlex.join([sys.executable, str(wrapper)])
        result = subprocess.run([sys.executable, '-B', '-c', program, common.SCRIPTS],
                                env=self.env(CC=cc), capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(json.loads(result.stdout)['symbols'], result.stdout)
        receipt = json.loads(self.library().with_name('core.json').read_text())
        self.assertEqual(receipt['source_sha256'], hashlib.sha256(original).hexdigest())
        self.assertEqual(source.read_text(), 'source changed during compilation')
        self.assertTrue(outcome.read_text().startswith('0\n'), outcome.read_text())

    def test_path_substitution_after_validation_cannot_change_loaded_inode(self):
        self.good()
        library = self.library()
        foreign = self.root / 'foreign.so'
        self.evil_library(foreign)
        program = PROBE.replace('lib, reason = native._build()',
                                'from pathlib import Path\n'
                                f'path = Path({str(library)!r})\n'
                                f'foreign = Path({str(foreign)!r})\n'
                                'original = native.ctypes.CDLL\n'
                                'def substitute(name, *args, **kwargs):\n'
                                '    path.rename(path.with_name("replaced.so"))\n'
                                '    path.symlink_to(foreign)\n'
                                '    return original(name, *args, **kwargs)\n'
                                'native.ctypes.CDLL = substitute\n'
                                'lib, reason = native._build()')
        result = subprocess.run([sys.executable, '-B', '-c', program, common.SCRIPTS],
                                env=self.env(), capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(json.loads(result.stdout)['symbols'])
        self.assertTrue(library.is_symlink())
        self.assertFalse(self.marker.exists(), 'path substitution changed the loaded library')

    def test_concurrent_builders_publish_one_complete_cache_entry(self):
        cc, log = self.wrapper(delay=0.3)
        command = [sys.executable, '-B', '-c', PROBE, common.SCRIPTS]
        processes = [subprocess.Popen(command, env=self.env(CC=cc), stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE, text=True) for _ in range(3)]
        try:
            for process in processes:
                stdout, stderr = process.communicate(timeout=60)
                self.assertEqual(process.returncode, 0, stdout + stderr)
                result = json.loads(stdout)
                self.assertTrue(result['native'], result['reason'])
                self.assertTrue(result['symbols'])
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                    process.wait()
        self.assertEqual(log.read_text().splitlines(), ['called'])
        self.assertEqual(len(list(self.cache.rglob('core.so'))), 1)
        self.assertFalse(list(self.cache.rglob('*.tmp')))


if __name__ == '__main__':
    unittest.main()
