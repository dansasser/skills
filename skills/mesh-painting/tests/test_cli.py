"""Command-line behaviour: A5 to A7, A9 to A14, and that scene_rng leaves pipeline output alone."""
import json
import os
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest

from PIL import Image

import common


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="meshpaint_test_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def out(self, name):
        return os.path.join(self.tmp, name)

    def ok(self, r):
        self.assertEqual(r.returncode, 0, r.stdout[-800:] + r.stderr[-1500:])
        return r

    def completed_usage(self, prefix):
        with open(prefix + "_usage.json") as stream:
            report = json.load(stream)
        self.assertEqual(report["result"], "completed")
        self.assertIsNone(report["stop"])
        self.assertIsNone(report["failure"])
        self.assertTrue(report["memory_ceiling_enforced"])
        self.assertEqual(report["processes_left_running"], [])
        self.assertFalse(os.path.exists(prefix + ".pid"), "pid file left after a normal run")
        self.assertEqual([name for name in os.listdir(self.tmp) if ".part-" in name], [])
        return report


class PreviewAndLooks(Base):
    def test_smoke_size(self):                                         # A6
        self.ok(common.run_engine([common.LIGHTHOUSE, "--out", self.out("s"), "--stage", "preview"]))
        with Image.open(self.out("s_preview.png")) as im:
            self.assertEqual(im.size, (1600, 1000))
            self.assertEqual(im.mode, "RGB")
        self.completed_usage(self.out("s"))

    def test_moor_example_previews(self):                              # A6
        self.ok(common.run_engine([os.path.join(common.ROOT, "assets", "scenes", "moor.py"), "--out", self.out("moor"), "--stage", "preview"]))
        with Image.open(self.out("moor_preview.png")) as im:
            self.assertEqual(im.size, (1600, 1000))
            self.assertEqual(im.mode, "RGB")
        self.completed_usage(self.out("moor"))

    def test_preview_repeatable_for_both_looks(self):                  # A5, A11
        for look in ("sunset", "golden_hour"):
            hashes = []
            for k in (1, 2):
                self.ok(common.run_engine([common.LIGHTHOUSE, "--out", self.out(f"{look}{k}"), "--stage", "preview", "--look", look]))
                hashes.append(common.sha(self.out(f"{look}{k}_preview.png")))
            self.assertEqual(hashes[0], hashes[1], f"{look} preview differs between two runs")

    def test_calm_sea_and_ghosts_off_change_the_picture(self):         # sea_state, foam, lens_ghosts
        scene = self.out("calm.py")
        with open(scene, "w") as fh:
            fh.write("import importlib.util\n"
                     f"spec = importlib.util.spec_from_file_location('lh', {common.LIGHTHOUSE!r})\n"
                     "lh = importlib.util.module_from_spec(spec); spec.loader.exec_module(lh)\n"
                     "import sys\n"
                     "def build():\n    sc = lh.build(); sc.sea_state = 0.2; sc.foam = 0.0; sc.lens_ghosts = False\n"
                     "    if 'noreflect' in __file__: sc.reflections = False\n    if 'clear' in __file__: sc.clouds = 0.0\n    return sc\n")
        self.ok(common.run_engine([common.LIGHTHOUSE, "--out", self.out("rough"), "--stage", "preview", "--look", "sunset"]))
        self.ok(common.run_engine([scene, "--out", self.out("calm"), "--stage", "preview", "--look", "sunset"]))
        self.assertNotEqual(common.sha(self.out("rough_preview.png")), common.sha(self.out("calm_preview.png")))
        import shutil as _sh
        hashes = {"calm": common.sha(self.out("calm_preview.png"))}
        for variant in ("noreflect", "clear"):                            # reflections and clouds each change the picture
            _sh.copy(scene, self.out(variant + ".py"))
            self.ok(common.run_engine([self.out(variant + ".py"), "--out", self.out(variant), "--stage", "preview", "--look", "sunset"]))
            hashes[variant] = common.sha(self.out(variant + "_preview.png"))
        self.assertEqual(len(set(hashes.values())), 3)

    def test_unknown_look(self):                                       # A11
        r = common.run_engine([common.LIGHTHOUSE, "--out", self.out("u"), "--stage", "preview", "--look", "noon"])
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn("Traceback", r.stderr)
        lines = [l for l in r.stderr.strip().splitlines() if l.strip()]
        self.assertEqual(len(lines), 1, r.stderr)
        self.assertIn("sunset", lines[0]); self.assertIn("golden_hour", lines[0])

    def test_scene_rng_does_not_change_pipeline(self):                 # R14
        scene = self.out("with_rng.py")
        with open(scene, "w") as fh:
            fh.write("import importlib.util\nfrom meshpaint import util\n"
                     f"spec = importlib.util.spec_from_file_location('lh', {common.LIGHTHOUSE!r})\n"
                     "lh = importlib.util.module_from_spec(spec); spec.loader.exec_module(lh)\n"
                     "def build():\n    util.scene_rng('extra').random(1000)\n    return lh.build()\n")
        self.ok(common.run_engine([common.LIGHTHOUSE, "--out", self.out("plain"), "--stage", "preview"]))
        self.ok(common.run_engine([scene, "--out", self.out("rng"), "--stage", "preview"]))
        self.assertEqual(common.sha(self.out("plain_preview.png")), common.sha(self.out("rng_preview.png")))


class Copies(Base):
    def test_complete_public_source_copy_runs_without_changing_source(self):    # A7, A12
        files = common.public_source_files()
        copy, work = self.out("public copy/mesh-painting"), self.out("work")
        os.makedirs(work)
        common.copy_files(files, copy)
        before = common.tree_hashes(copy)
        self.assertEqual(set(before), set(files))
        for resource in ("requirements.txt", "scripts/meshpaint/procfs.py", "assets/scenes/moor.py",
                         "references/scene-api.md", "references/limits.md", "agents/openai.yaml",
                         "doc/examples/lighthouse_golden_hour.png", "doc/examples/moor_golden_hour.png"):
            self.assertIn(resource, before)
        self.ok(common.run_engine([os.path.join(copy, "assets", "scenes", "lighthouse.py"),
                                   "--out", self.out("out/painting"), "--stage", "preview"],
                                  cwd=work, runner=os.path.join(copy, "scripts", "run.py")))
        self.assertTrue(os.path.exists(self.out("out/painting_preview.png")))
        self.assertEqual(before, common.tree_hashes(copy), "running the engine changed the public source")
        self.assertEqual(common.public_source_files(copy), files,
                         "a Gitless copy must retain the complete public source inventory")


class CompilerSearch(Base):                                            # A13
    def setUp(self):
        super().setUp()
        self.real = shutil.which("gcc") or shutil.which("clang")
        if not self.real:
            self.skipTest("neither gcc nor clang is installed")

    def env(self, **extra):
        e = {"XDG_CACHE_HOME": self.out("cache"), "TMPDIR": self.out("tmp"), "CC": ""}
        os.makedirs(e["TMPDIR"], mode=0o700, exist_ok=True)
        e.update(extra)
        return e

    def test_finds_gcc_or_clang_when_cc_is_missing(self):
        bindir = self.out("bin"); os.makedirs(bindir)
        for d in os.environ.get("PATH", "").split(os.pathsep):         # everything on PATH except `cc`
            if not os.path.isdir(d):
                continue
            for n in os.listdir(d):
                link = os.path.join(bindir, n)
                if n != "cc" and not os.path.lexists(link):
                    os.symlink(os.path.join(d, n), link)
        self.assertIsNone(shutil.which("cc", path=bindir))
        r = self.ok(common.run_engine([common.LIGHTHOUSE, "--out", self.out("c"), "--stage", "preview"], env=self.env(PATH=bindir)))
        self.assertIn("compiled core", r.stdout.splitlines()[0])

    def test_cc_variable_is_used_first(self):
        log = self.out("compiler calls.log"); wrap = self.out("compiler wrapper with spaces")
        with open(wrap, "w") as fh:
            fh.write("#!/bin/sh\n"
                     f"{shlex.quote(self.real)} \"$@\"\n"
                     "meshpaint_compiler_status=$?\n"
                     f"printf '%s\\n' \"$meshpaint_compiler_status\" >> {shlex.quote(log)}\n"
                     "exit \"$meshpaint_compiler_status\"\n")
        os.chmod(wrap, os.stat(wrap).st_mode | stat.S_IEXEC)
        r = self.ok(common.run_engine([common.LIGHTHOUSE, "--out", self.out("w"), "--stage", "preview"],
                                      env=self.env(CC=shlex.quote(wrap))))
        self.assertTrue(os.path.exists(log), "the CC wrapper was not invoked")
        with open(log) as fh:
            self.assertEqual(fh.read().splitlines(), ["0"], "the selected compiler failed before fallback")
        self.assertIn("compiled core", r.stdout.splitlines()[0])


@unittest.skipUnless(common.SLOW, "slow: set MESHPAINT_SLOW=1")
class Slow(Base):
    def test_paint_repeatable_at_half_scale(self):                     # A5
        h = []
        for k in (1, 2):
            self.ok(common.run_engine([common.LIGHTHOUSE, "--out", self.out(f"p{k}"), "--stage", "paint", "--scale", "0.5", "--tiles", "0"]))
            report = self.completed_usage(self.out(f"p{k}"))
            self.assertEqual(report["usage"]["strokes_done"], 130_024)
            self.assertEqual(report["usage"]["pixels"], 400_000)
            for suffix, size in ((".png", (1600, 1000)), ("_full.png", (800, 500))):
                with Image.open(self.out(f"p{k}" + suffix)) as image:
                    self.assertEqual(image.size, size)
                    self.assertEqual(image.mode, "RGB")
            h.append((common.sha(self.out(f"p{k}.png")), common.sha(self.out(f"p{k}_full.png"))))
        self.assertEqual(h[0], h[1])

    def test_paint_time_at_default_scale(self):                        # A9
        t = time.time()
        r = self.ok(common.run_engine([common.LIGHTHOUSE, "--out", self.out("big"), "--stage", "paint", "--tiles", "0"]))
        dt = time.time() - t
        print(f"\n    A9: lighthouse paint at scale 2.4 took {dt:.0f} s", file=sys.stderr)
        self.assertIn("compiled core", r.stdout.splitlines()[0])
        self.assertLessEqual(dt, 300)

    def test_python_core_paints(self):                                 # A10
        t = time.time()
        r = self.ok(common.run_engine([common.LIGHTHOUSE, "--out", self.out("py"), "--stage", "paint", "--scale", "0.5", "--tiles", "0"],
                                      env={"MESHPAINT_NO_NATIVE": "1"}, timeout=3000))
        print(f"\n    A10: Python-core paint at scale 0.5 took {time.time() - t:.0f} s", file=sys.stderr)
        self.assertIn("python core", r.stdout.splitlines()[0])
        self.assertTrue(os.path.exists(self.out("py.png")))

    def test_stop_removes_pid_file(self):                              # A14
        prefix = self.out("long")
        p = subprocess.Popen([sys.executable, "-B", common.RUN, common.LIGHTHOUSE, "--out", prefix, "--stage", "paint", "--tiles", "0"],
                             cwd=self.tmp, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
        try:
            for _ in range(300):
                if os.path.exists(prefix + ".pid"):
                    break
                time.sleep(0.1)
            self.assertTrue(os.path.exists(prefix + ".pid"), "run wrote no pid file")
            time.sleep(3)
            self.ok(common.run_engine(["--stop", "--out", prefix]))
            p.wait(timeout=30)
            self.assertNotEqual(p.returncode, 0)
            self.assertFalse(os.path.exists(prefix + ".pid"), "pid file left after --stop")
        finally:
            if p.poll() is None:
                p.kill()

    def test_stop_never_signals_a_stranger(self):                      # A14
        sleeper = subprocess.Popen(["sleep", "60"])
        try:
            prefix = self.out("stale")
            with open(prefix + ".pid", "w") as fh:
                fh.write(str(sleeper.pid))
            r = self.ok(common.run_engine(["--stop", "--out", prefix]))
            self.assertIn("no signal sent", r.stdout)
            time.sleep(0.5)
            self.assertIsNone(sleeper.poll(), "--stop killed an unrelated process")
        finally:
            sleeper.send_signal(signal.SIGKILL)
            sleeper.wait()


if __name__ == "__main__":
    unittest.main()
