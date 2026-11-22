"""Hard resource limits: A17 to A28.

Stopping with a resource report is checked by Base.stopped().
"""
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import zlib

from PIL import Image

import common

MOOR = os.path.join(common.ROOT, "assets", "scenes", "moor.py")
STRESS = os.path.join(common.ROOT, "tests", "scenes", "stress.py")

# sha256 of the unmodified v0.1 engine's output on the development machine (compiled core).
REFERENCE = {
    "lighthouse_preview": "cd4f0c80e43f38d8c7c95ef2fc23955e221fe9421bd520ce01b59cc29e84252b",
    "moor_preview": "adedf60b22c9438f60cc50e206fb0334302718becfe57f288eaf7491c4042615",
    "lighthouse_paint_half": "156c9300473d731f4d9115593b041d11ddca0211c66140e25bd53da45602eca3",
    "lighthouse_paint_half_full": "c7a064949b15ab77f31e2f7d3f191d2b218751daf65526284b7411abb36d0d06",
}

# Historical PNG bytes belong to a recorded numerical/encoding environment.
# Other supported environments still run the portable behavior and repeatability checks.
REFERENCE_ENVIRONMENT = {
    "implementation": "CPython", "python": "3.14.4", "numpy": "2.5.5",
    "scipy": "1.18.1", "Pillow": "13.0.0", "system": "Linux", "machine": "x86_64",
    "zlib_compile": "1.3.1", "zlib_runtime": "1.3.1",
}


def _reference_environment():
    import numpy
    import scipy
    import PIL

    return {
        "implementation": platform.python_implementation(), "python": platform.python_version(),
        "numpy": numpy.__version__, "scipy": scipy.__version__, "Pillow": PIL.__version__,
        "system": platform.system(), "machine": platform.machine(),
        "zlib_compile": zlib.ZLIB_VERSION, "zlib_runtime": zlib.ZLIB_RUNTIME_VERSION,
    }


_ACTUAL_REFERENCE_ENVIRONMENT = _reference_environment()
_REFERENCE_PREREQUISITE = ("historical PNG references require " + str(REFERENCE_ENVIRONMENT)
                           + "; observed " + str(_ACTUAL_REFERENCE_ENVIRONMENT))

RENDER_TILES = [f"_render_tile_r{r}c{c}.png" for r in (1, 2) for c in (1, 2, 3)]
PAINT_TILES = [f"_tile_r{r}c{c}.png" for r in (1, 2) for c in (1, 2, 3)]

SCENE_HEAD = """import numpy as np
from meshpaint.geometry import Camera
from meshpaint.mesh import MeshSet, grid_mesh, scatter, blades, lathe
from meshpaint.scene import Scene

def flat(x, y):
    return np.zeros_like(x, dtype=np.float32)

def shade(g):
    n = len(g.x)
    return np.full((n, 3), 0.3, np.float32), np.zeros(n, np.float32), np.zeros((n, 3), np.float32)

def scene(ms, **kw):
    return Scene(meshes=ms, camera=Camera(eye=(0, -40, 8), bearing=0.0, pitch=-4.0), shade=shade, sea=False, **kw)

def small():
    ms = MeshSet()
    ms.add(*grid_mesh(np.arange(-50.0, 50.0, 1.0), np.arange(0.0, 100.0, 1.0), flat, None), 2, "up")
    return ms
"""

OVERSIZED = SCENE_HEAD + """
def marked(x, y):
    open(__file__ + ".called", "w").write("the height function ran")
    return np.zeros_like(x, dtype=np.float32)

def build():
    ms = small()                                               # 10,000 vertices
    ms.add(*grid_mesh(np.arange(3000.0), np.arange(3000.0), marked, None), 2, "up")   # 9,000,000 more
    return scene(ms)
"""


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="meshpaint_limits_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def out(self, name):
        return os.path.join(self.tmp, name)

    def scene(self, name, text):
        path = self.out(name)
        with open(path, "w") as fh:
            fh.write(text)
        return path

    def run_engine(self, *args, **kw):
        return common.run_engine(list(args), **kw)

    def usage(self, prefix):
        with open(prefix + "_usage.json") as fh:
            return json.load(fh)

    def place_images(self, prefix, suffixes):
        """Put a valid image at each output name and return {path: sha256}."""
        before = {}
        for k, sfx in enumerate(suffixes):
            Image.new("RGB", (64, 40), (10 * k + 5, 90, 160)).save(prefix + sfx)
            before[prefix + sfx] = common.sha(prefix + sfx)
        return before

    def stopped(self, r, prefix, limit, level="standard", stage=None, allowed=None, requested=None, scene=None, scene_sha=None):
        """The run stopped with the report (A18 to A25 share this)."""
        self.assertEqual(r.returncode, 3, r.stdout[-600:] + r.stderr[-1200:])
        self.assertNotIn("Traceback", r.stderr)
        line = next((l for l in r.stderr.splitlines() if "stopped by a resource limit" in l), None)
        self.assertIsNotNone(line, r.stderr)
        self.assertIn(f"hit the {limit} limit of level '{level}'", line)
        self.assertRegex(line, r"(requested|used) [\d,.]+ ")
        self.assertRegex(line, r"allowed [\d,.]+ ")
        self.assertIn("stage '", line)
        u = self.usage(prefix)
        self.assertEqual(u["result"], "stopped")
        self.assertIsNone(u["failure"], "a clean stop was also recorded as a failure")
        self.assertEqual(u["level"], level)                    # A25: the level asked for, not another
        st = u["stop"]
        self.assertEqual(st["limit"], limit)
        self.assertTrue(st["stage"])
        self.assertTrue(st["requested"] is not None or st["used"] is not None)
        if stage:
            self.assertEqual(st["stage"], stage); self.assertIn(f"stage '{stage}'", line)
        if allowed is not None:
            self.assertEqual(st["allowed"], allowed); self.assertIn(f"allowed {allowed:,}", line)
        if requested is not None:
            self.assertEqual(st["requested"], requested); self.assertIn(f"requested {requested:,}", line)
        self.assertEqual([n for n in os.listdir(os.path.dirname(prefix)) if ".part" in n], [], "half-written files left behind")
        self.assertFalse(os.path.exists(prefix + ".pid"), "pid file left after a stop")
        self.assertIn("left as they are", r.stderr)
        self.assertLessEqual(r.stdout.count("meshes:"), 1, "the job was attempted a second time")    # A25
        if scene:
            self.assertEqual(common.sha(scene), scene_sha, "the scene file was changed")
        return u

    def unchanged(self, before):
        for path, digest in before.items():
            self.assertTrue(os.path.exists(path), f"{os.path.basename(path)} was removed")
            self.assertEqual(common.sha(path), digest, f"{os.path.basename(path)} was changed by a stopped run")
            with Image.open(path) as im:
                im.verify()


class Levels(unittest.TestCase):                                       # A17
    def test_three_levels_rising_with_standard_default(self):
        from meshpaint import limits
        self.assertEqual(list(limits.LEVELS), ["draft", "standard", "high"])
        self.assertEqual(limits.DEFAULT_LEVEL, "standard")
        self.assertEqual(limits.Budget().level.name, "standard")
        d, s, h = (limits.LEVELS[k] for k in ("draft", "standard", "high"))
        for field in ("scale", "vertices", "triangles", "pixels", "strokes", "memory_mb", "seconds"):
            self.assertLess(getattr(d, field), getattr(s, field), field)
            self.assertLess(getattr(s, field), getattr(h, field), field)

    def rows(self, path):
        from meshpaint import limits
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        got = {}
        for name in limits.LEVELS:
            m = re.search(r"(?m)^\| `%s`[^|]*\|(.+)\|\s*$" % name, text)
            self.assertIsNotNone(m, f"{os.path.basename(path)} has no table row for {name}")
            cells = [c.strip() for c in m.group(1).split("|")]
            got[name] = [float(re.match(r"[\d,.]+", c).group(0).replace(",", "")) for c in cells[:7]]
        return got

    def test_documents_carry_the_same_figures(self):
        from meshpaint import limits
        want = {k: [v.scale, v.vertices, v.triangles, v.pixels, v.strokes, v.memory_mb, v.seconds] for k, v in limits.LEVELS.items()}
        self.assertEqual(self.rows(os.path.join(common.ROOT, "references", "limits.md")), want)

    def test_help_names_the_default(self):
        r = common.run_engine(["--help"])
        self.assertEqual(r.returncode, 0)
        self.assertIn("--level {draft,standard,high}", r.stdout)


class Units(unittest.TestCase):                                        # A18, A20 unit parts
    def setUp(self):
        from meshpaint import limits
        self.limits = limits
        limits.use("draft")
        self.addCleanup(limits.use, "standard")

    def expect(self, limit, fn):
        with self.assertRaises(self.limits.LimitExceeded) as cm:
            fn()
        self.assertEqual(cm.exception.limit, limit)
        self.assertEqual(cm.exception.level, "draft")
        self.assertEqual(cm.exception.allowed, getattr(self.limits.LEVELS["draft"], limit))
        self.assertGreater(cm.exception.requested, cm.exception.allowed)
        return cm.exception

    def test_helpers_refuse_before_building(self):
        import numpy as np
        from meshpaint import mesh
        calls = []

        def height(x, y):
            calls.append(1)
            return np.zeros_like(x)
        e = self.expect("vertices", lambda: mesh.grid_mesh(np.arange(2000.0), np.arange(2000.0), height, None))
        self.assertEqual(calls, [], "grid_mesh called the height function before checking the budget")
        self.assertEqual(e.stage, "scene build (grid_mesh)")
        self.assertEqual(e.requested, 4_000_000)
        tuft = mesh.blades(n=10)
        e = self.expect("vertices", lambda: mesh.scatter(np.zeros((40_000, 3)), *tuft))          # 1,200,000 vertices
        self.assertEqual(e.stage, "scene build (scatter)")
        e = self.expect("vertices", lambda: mesh.lathe(0, 0, [(1.0, float(z), 2) for z in range(1001)], seg=1000))
        self.assertEqual(e.stage, "scene build (lathe)")
        e = self.expect("vertices", lambda: mesh.blades(n=400_000))
        self.assertEqual(e.stage, "scene build (blades)")

    def test_triangles_are_capped_on_their_own(self):
        import numpy as np
        from meshpaint import mesh
        # a 708 x 708 grid: 501,264 vertices (inside the cap) but 999,698 triangles; two of them pass the triangle cap first
        ms = mesh.MeshSet()
        ms.add(*mesh.grid_mesh(np.arange(708.0), np.arange(708.0), lambda x, y: np.zeros_like(x), None), 2, "up")
        V = np.zeros((3, 3), np.float32); V[1, 0] = V[2, 1] = 1
        F = np.zeros((400, 3), np.int64); F[:, 1] = 1; F[:, 2] = 2
        e = self.expect("triangles", lambda: ms.add(V, F, 2, "up"))
        self.assertEqual(e.stage, "scene build (MeshSet.add)")
        self.assertEqual(e.used, 999_698)
        self.assertEqual(self.limits.active().triangles, 999_698, "a refused mesh was counted")

    def test_running_total_across_adds(self):
        import numpy as np
        from meshpaint import mesh
        ms = mesh.MeshSet()
        tuft = mesh.blades(n=10)                                       # 30 vertices, 10 triangles
        field = lambda: mesh.scatter(np.zeros((12_000, 3)), *tuft)     # 360,000 vertices each
        ms.add(*field(), 2, "up"); ms.add(*field(), 2, "up")
        e = self.expect("vertices", field)                             # the third would make 1,080,000
        self.assertEqual((e.used, e.requested), (720_000, 1_080_000))

    def test_planned_strokes_matches_the_passes(self):
        from meshpaint import paint
        self.assertEqual(paint.planned_strokes(2.4), 2_996_000)
        self.assertEqual(paint.planned_strokes(0.5), 130_024)          # the count v0.1 reported at scale 0.5
        self.assertEqual(paint.planned_strokes(1.2), 749_000)
        self.assertEqual(paint.planned_strokes(3.6), 6_741_000)
        for name, lv in self.limits.LEVELS.items():
            self.assertLessEqual(paint.planned_strokes(lv.scale), lv.strokes, name)

    def test_paint_refuses_before_any_stroke(self):
        import numpy as np
        from meshpaint import paint
        tgt = np.zeros((40, 64, 3), np.float32); reg = np.zeros((40, 64), np.int8)
        e = self.expect("strokes", lambda: paint.paint(tgt, reg, {}, 1.3, 20.0, np.random.default_rng(1), lambda m: None))
        self.assertEqual(e.requested, paint.planned_strokes(1.3))
        self.assertEqual(self.limits.active().strokes_done, 0)

    def test_process_limits_can_only_be_lowered(self):
        lv = self.limits.LEVELS["standard"]
        self.assertEqual(self.limits.Budget("standard", memory_mb=1000, seconds=5).level.memory_mb, 1000)
        for kw in (dict(memory_mb=lv.memory_mb + 1), dict(seconds=lv.seconds + 1), dict(seconds=0), dict(memory_mb=-5)):
            with self.assertRaises(ValueError):
                self.limits.Budget("standard", **kw)


class Stops(Base):
    def test_oversized_mesh_stops_in_every_stage_and_keeps_images(self):   # A18, A24, A25, A28
        scene = self.scene("big.py", OVERSIZED)
        digest = common.sha(scene)
        for stage, sfx in (("preview", ["_preview.png"]), ("render", ["_render.png"] + RENDER_TILES),
                           ("paint", [".png", "_full.png", "_render.png"] + PAINT_TILES)):
            prefix = self.out("big_" + stage)
            before = self.place_images(prefix, sfx)
            r = self.run_engine(scene, "--out", prefix, "--stage", stage)
            self.stopped(r, prefix, "vertices", stage="scene build (grid_mesh)", allowed=4_000_000, requested=9_010_000,
                         scene=scene, scene_sha=digest)
            self.assertIn("10,000 already in the scene plus 9,000,000 for this call", r.stderr)
            self.assertFalse(os.path.exists(scene + ".called"), "the oversized grid's height function was called")
            self.assertNotIn("meshes:", r.stdout, "the render started")
            self.unchanged(before)
            self.assertIn("(from an earlier run)", r.stderr)

    def test_same_scene_fits_high_and_a_bypass_is_caught(self):            # A18 (mesh totals), level honoured
        scene = self.scene("bypass.py", SCENE_HEAD + """
def build():
    ms = small()
    V = np.zeros((1_200_000, 3), np.float32); V[:, 0] = np.arange(len(V)) % 100; V[:, 1] = 50 + np.arange(len(V)) // 100
    F = np.stack([np.arange(0, 300), np.arange(1, 301), np.arange(100, 400)], 1)
    ms.V.append(V); ms.F.append(F + ms.n); ms.N.append(np.tile(np.float32([0, 0, 1]), (len(V), 1)))
    ms.M.append(np.full(len(F), 2, np.int32)); ms.A.append(np.zeros(len(V), np.float32)); ms.n += len(V)
    return scene(ms)
""")
        r = self.run_engine(scene, "--out", self.out("bp"), "--stage", "preview", "--level", "draft")
        self.stopped(r, self.out("bp"), "vertices", level="draft", stage="render (mesh totals)", allowed=1_000_000, requested=1_210_000)
        r = self.run_engine(scene, "--out", self.out("bp2"), "--stage", "preview")
        self.assertEqual(r.returncode, 0, r.stderr[-800:])
        u = self.usage(self.out("bp2"))
        self.assertEqual((u["result"], u["usage"]["vertices"]), ("completed", 1_210_000))
        self.assertIn("level standard:", r.stdout.splitlines()[-1])

    def test_pixel_budget(self):                                            # A19, A24, A28
        digest = common.sha(common.LIGHTHOUSE)
        for stage, sfx in (("render", ["_render.png"] + RENDER_TILES), ("paint", [".png", "_full.png", "_render.png"] + PAINT_TILES)):
            prefix = self.out("px_" + stage)
            before = self.place_images(prefix, sfx)
            r = self.run_engine(common.LIGHTHOUSE, "--out", prefix, "--stage", stage, "--scale", "5")
            self.stopped(r, prefix, "pixels", stage="job check (canvas size)", allowed=9_216_000, requested=40_000_000,
                         scene=common.LIGHTHOUSE, scene_sha=digest)
            self.assertNotIn("meshes:", r.stdout)
            self.unchanged(before)
        scene = self.scene("wide.py", SCENE_HEAD + "\ndef build():\n    return scene(small(), size=(4000, 2500))\n")
        r = self.run_engine(scene, "--out", self.out("wide"), "--stage", "preview")      # preview: Scene.size alone is too big
        self.stopped(r, self.out("wide"), "pixels", allowed=9_216_000, requested=10_000_000)
        r = self.run_engine(scene, "--out", self.out("wide_d"), "--stage", "preview", "--level", "draft")
        self.stopped(r, self.out("wide_d"), "pixels", level="draft", allowed=2_304_000, requested=10_000_000)

    def test_stroke_budget(self):                                           # A20
        from meshpaint import paint
        scene = self.scene("strip.py", SCENE_HEAD + "\ndef build():\n    return scene(small(), size=(1600, 600))\n")
        prefix = self.out("strip")
        r = self.run_engine(scene, "--out", prefix, "--stage", "paint", "--scale", "2.5")
        u = self.stopped(r, prefix, "strokes", stage="job check (planning the passes)", allowed=3_000_000,
                         requested=paint.planned_strokes(2.5))
        self.assertEqual(u["usage"]["pixels"], 6_000_000)                   # the canvas itself was within the level
        self.assertNotIn("meshes:", r.stdout)
        r = self.run_engine(scene, "--out", prefix, "--stage", "render", "--scale", "2.5", "--tiles", "0")
        self.assertEqual(r.returncode, 0, r.stderr[-800:])                  # a render lays no strokes, so it is allowed

    def test_ceilings_are_in_place_before_the_scene_loads(self):            # A21
        from meshpaint import limits
        scene = self.scene("probe.py", "import json, resource, signal\n"
                           "json.dump({'as': resource.getrlimit(resource.RLIMIT_AS), 'timer': signal.getitimer(signal.ITIMER_REAL)[0]},\n"
                           "          open(__file__ + '.json', 'w'))\n"
                           + SCENE_HEAD + "\ndef build():\n    return scene(small())\n")
        for level, extra, mem, secs in (("standard", [], limits.LEVELS["standard"].memory_mb, limits.LEVELS["standard"].seconds),
                                        ("draft", [], limits.LEVELS["draft"].memory_mb, limits.LEVELS["draft"].seconds),
                                        ("high", ["--memory-limit", "3000", "--time-limit", "50"], 3000, 50)):
            r = self.run_engine(scene, "--out", self.out("probe"), "--stage", "preview", "--level", level, *extra)
            self.assertEqual(r.returncode, 0, r.stderr[-800:])
            with open(scene + ".json") as fh:
                seen = json.load(fh)
            self.assertEqual(seen["as"], [mem * 1024 * 1024] * 2, level)
            self.assertTrue(0 < seen["timer"] <= secs and seen["timer"] > secs - 30, (level, seen["timer"]))
            self.assertTrue(self.usage(self.out("probe"))["memory_ceiling_enforced"])
        for flag, value in (("--memory-limit", str(limits.LEVELS["standard"].memory_mb + 1)),
                            ("--time-limit", str(limits.LEVELS["standard"].seconds + 1))):
            r = self.run_engine(scene, "--out", self.out("probe"), "--stage", "preview", flag, value)
            self.assertEqual(r.returncode, 2, r.stderr[-300:])
            self.assertIn("only lower", r.stderr)

    def test_memory_ceiling(self):                                          # A22, A24
        # Make the request larger than either ceiling instead of assuming the
        # bundled picture consumes a fixed amount of memory on every platform.
        scene = self.scene("hog.py", "import json, resource\n" + SCENE_HEAD +
                           "\ndef build():\n"
                           "    with open(__file__ + '.limits.json', 'w') as fh:\n"
                           "        json.dump(resource.getrlimit(resource.RLIMIT_AS), fh)\n"
                           "    hog = np.ones(8 << 30)          # 64 GiB\n"
                           "    return scene(small())\n")
        digest = common.sha(scene)
        from meshpaint import limits
        for ceiling in (900, limits.LEVELS["standard"].memory_mb):
            with self.subTest(memory_mb=ceiling):
                prefix = self.out(f"mem-{ceiling}")
                before = self.place_images(prefix, [".png", "_preview.png"])
                r = self.run_engine(scene, "--out", prefix, "--stage", "preview", "--memory-limit", str(ceiling))
                u = self.stopped(r, prefix, "memory", stage="scene build", allowed=ceiling,
                                 scene=scene, scene_sha=digest)
                self.assertLessEqual(u["usage"]["peak_address_space_kb"], ceiling * 1024)
                with open(scene + ".limits.json") as fh:
                    soft, hard = json.load(fh)
                self.assertGreater(soft, 0)
                self.assertLessEqual(soft, ceiling * 1024 ** 2)
                self.assertLessEqual(hard, ceiling * 1024 ** 2)
                self.assertIn("64.0 GiB", r.stderr)
                self.unchanged(before)

    def test_time_limit(self):                                              # A23 first part, A24
        prefix = self.out("slow")
        before = self.place_images(prefix, ["_preview.png"])
        t = time.time()
        r = self.run_engine(common.LIGHTHOUSE, "--out", prefix, "--stage", "preview", "--time-limit", "3")
        dt = time.time() - t
        u = self.stopped(r, prefix, "time", allowed=3.0)
        self.assertGreaterEqual(u["stop"]["used"], 3.0)
        self.assertLess(dt, 10.0)
        self.unchanged(before)

    def test_stop_while_images_are_being_written(self):                    # A24: the mechanism itself
        # Fault injection: the scene makes the third image write of the run fail, either with a limit stop
        # or by killing the renderer outright, when two finished images are already waiting under temporary names.
        scene = self.scene("fault.py", "import os, signal\n" + SCENE_HEAD + """
from PIL import Image
from meshpaint import limits
real_save, count = Image.Image.save, [0]

def save(self, fp, *a, **kw):
    real_save(self, fp, *a, **kw)
    count[0] += 1
    if count[0] == 3:
        waiting = [n for n in os.listdir(os.path.dirname(fp)) if '.part-' in n]
        open(__file__ + '.waiting', 'w').write(str(len(waiting)))
        if os.environ['FAULT'] == 'kill':
            os.kill(os.getpid(), signal.SIGKILL)
        raise limits.LimitExceeded('writing images', 'time', 1.0, used=2.0)

def build():
    Image.Image.save = save
    return scene(small())
""")
        for fault, code in (("limit", 3), ("kill", 137)):
            prefix = self.out("fault_" + fault)
            before = self.place_images(prefix, ["_render.png"] + RENDER_TILES)
            r = self.run_engine(scene, "--out", prefix, "--stage", "render", "--level", "draft", env={"FAULT": fault})
            self.assertEqual(r.returncode, code, r.stderr[-800:])
            with open(scene + ".waiting") as fh:
                self.assertEqual(fh.read(), "3", "the fault did not land while images were waiting")
            self.unchanged(before)
            self.assertEqual([n for n in os.listdir(self.tmp) if ".part" in n], [], "half-written files left behind")
            u = self.usage(prefix)
            self.assertEqual(u["result"], "stopped" if fault == "limit" else "failed")
            self.assertTrue(all(not k["written_by_this_run"] for k in u["images_kept"]))
            if fault == "kill":
                self.assertIn("died from signal 9 during stage 'writing images'", r.stderr)
                self.assertNotIn("Traceback", r.stderr)
            else:
                self.stopped(r, prefix, "time", level="draft", stage="writing images")

    def test_commit_holds_a_stop_until_all_files_are_renamed(self):        # A24: a stage's images change together
        # In a separate process with numpy's worker threads alive and the renderer's own SIGTERM handler,
        # which is where a signal mask alone fails: SIGTERM arrives after the first of three renames.
        code = """
import os, signal, sys
sys.path.insert(0, %r)
import numpy as np
np.ones((400, 400)) @ np.ones((400, 400))                    # start the worker threads
from PIL import Image
from meshpaint import limits
from meshpaint.__main__ import Outputs
signal.signal(signal.SIGTERM, lambda *a: limits.raise_or_hold(SystemExit(143)))
prefix = sys.argv[1]
for sfx in ("_a.png", "_b.png", "_c.png"):
    Image.new("RGB", (8, 8), (1, 2, 3)).save(prefix + sfx)
outs = Outputs()
for sfx in ("_a.png", "_b.png", "_c.png"):
    outs.save(Image.new("RGB", (8, 8), (200, 100, 50)), prefix + sfx)
real, sent = os.replace, []
def replace(src, dst):
    real(src, dst)
    if not sent:
        sent.append(1); os.kill(os.getpid(), signal.SIGTERM)
        for _ in range(200000): pass                          # give the handler every chance to run now
os.replace = replace
try:
    outs.commit()
except SystemExit as e:
    print("stopped after commit", e.code, len(outs.done))
""" % common.SCRIPTS
        prefix = self.out("c")
        r = subprocess.run([sys.executable, "-B", "-c", code, prefix], capture_output=True, text=True, timeout=120)
        self.assertEqual(r.stdout.strip(), "stopped after commit 143 3", r.stdout + r.stderr[-600:])
        for sfx in ("_a.png", "_b.png", "_c.png"):
            with Image.open(prefix + sfx) as im:
                self.assertEqual(im.getpixel((0, 0)), (200, 100, 50), sfx + " was not replaced: a mixed set")

    def test_killed_renderer_is_reported_truthfully(self):                 # which images this run replaced
        scene = self.scene("dies.py", "import os, signal\n" + SCENE_HEAD + """
from meshpaint import native
def die(*a, **kw):
    os.kill(os.getpid(), signal.SIGKILL)

def build():
    native.strokes = die                      # the renderer is killed at its first stroke batch, after its render is kept
    return scene(small())
""")
        prefix = self.out("dies")
        before = self.place_images(prefix, [".png", "_full.png", "_render.png"])
        r = self.run_engine(scene, "--out", prefix, "--stage", "paint", "--level", "draft", "--tiles", "0")
        self.assertEqual(r.returncode, 137, r.stderr[-600:])
        u = self.usage(prefix)
        kept = {os.path.basename(k["path"]): k["written_by_this_run"] for k in u["images_kept"]}
        self.assertEqual(kept, {"dies.png": False, "dies_full.png": False, "dies_render.png": True})
        self.assertNotEqual(common.sha(prefix + "_render.png"), before[prefix + "_render.png"])
        self.assertIn("dies_render.png (from this run)", r.stderr)
        self.assertIn("dies.png (from an earlier run)", r.stderr)
        self.assertNotIn("were not replaced", r.stderr)
        del before[prefix + "_render.png"]
        self.unchanged(before)

    def test_scene_file_cannot_loosen_the_limits(self):                    # C10
        from meshpaint import limits
        scene = self.scene("tamper.py", "import os, resource, time\n" + SCENE_HEAD + """
from meshpaint import limits

def build():
    mode = os.environ['MODE']
    if mode == 'use':
        limits.use('high')
    if mode == 'rlimit':
        resource.setrlimit(resource.RLIMIT_AS, (resource.RLIM_INFINITY, resource.RLIM_INFINITY))
    if mode == 'swallow':
        try:
            time.sleep(6)
        except Exception:
            pass
    if mode == 'bare':
        try:
            time.sleep(4)
        except:
            pass
    if mode == 'thread':
        import threading
        def work():
            try:
                grid_mesh(np.arange(2000.0), np.arange(2000.0), flat, None)
            except BaseException:
                pass                                  # lost in the thread; the engine still reports it
        t = threading.Thread(target=work); t.start(); t.join()
    if mode == 'child' and os.fork() == 0:
        open(__file__ + '.child', 'w').write(str(os.getpid()))
        time.sleep(120); os._exit(0)
    if mode == 'forksession' and os.fork() == 0:
        os.setsid()                           # no exec: leaves the renderer's group and session as it is
        open(__file__ + '.child', 'w').write(str(os.getpid()))
        time.sleep(120); os._exit(0)
    if mode == 'exit3raw':
        os._exit(3)
    if mode == 'session':
        import subprocess, sys
        p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'], start_new_session=True)
        open(__file__ + '.child', 'w').write(str(p.pid))
    if mode == 'exit3':
        raise SystemExit(3)
    return scene(small())
""")
        prefix = self.out("tamper")
        for mode in ("use", "rlimit"):                                 # refused; the run fails as a scene error, not a success
            r = self.run_engine(scene, "--out", prefix, "--stage", "preview", "--level", "draft", env={"MODE": mode})
            self.assertEqual(r.returncode, 1, mode + r.stderr[-400:])
            self.assertFalse(os.path.exists(prefix + "_preview.png"))
            self.assertEqual(self.usage(prefix)["result"], "failed")
        r = self.run_engine(scene, "--out", prefix, "--stage", "preview", "--level", "draft", "--time-limit", "2", env={"MODE": "swallow"})
        u = self.stopped(r, prefix, "time", level="draft", stage="scene build", allowed=2.0)
        self.assertLess(u["usage"]["seconds"], 4.0, "except Exception swallowed the time limit")
        r = self.run_engine(scene, "--out", prefix, "--stage", "preview", "--level", "draft", "--time-limit", "2", env={"MODE": "bare"})
        self.stopped(r, prefix, "time", level="draft", stage="scene build", allowed=2.0)   # swallowed by a bare except: still a stop
        self.assertFalse(os.path.exists(prefix + "_preview.png"))
        r = self.run_engine(scene, "--out", prefix, "--stage", "preview", "--level", "draft", env={"MODE": "thread"})
        self.stopped(r, prefix, "vertices", level="draft", stage="scene build (grid_mesh)")   # raised in a thread, still a stop
        for mode in ("child", "session", "forksession"):               # same group; new session by exec; new session by fork alone
            t = time.time()
            r = self.run_engine(scene, "--out", prefix, "--stage", "preview", "--level", "draft", env={"MODE": mode})
            self.assertEqual(r.returncode, 0, r.stderr[-400:])
            self.assertLess(time.time() - t, 30, "the supervisor waited for a process the scene started")
            with open(scene + ".child") as fh:
                child = fh.read()
            self.assertFalse(os.path.exists(f"/proc/{child}"), f"a process started by the scene ({mode}) outlived the run")
            self.assertEqual(self.usage(prefix)["processes_left_running"], [])
        r = self.run_engine(scene, "--out", prefix, "--stage", "preview", "--level", "draft", env={"MODE": "exit3"})
        self.assertEqual(r.returncode, 1)                               # 3 is reserved for a limit stop
        r = self.run_engine(scene, "--out", prefix, "--stage", "preview", "--level", "draft", env={"MODE": "exit3raw"})
        self.assertEqual(r.returncode, 1); self.assertIn("sent no report", r.stderr)
        self.assertEqual(self.usage(prefix)["result"], "failed")
        r = self.run_engine(scene, "--out", prefix, "--stage", "preview", "--memory-limit", str(limits.MIN_MEMORY_MB - 1), env={"MODE": "none"})
        self.assertEqual(r.returncode, 2)
        r = self.run_engine(scene, "--out", prefix, "--stage", "render", "--scale", "1e-9", env={"MODE": "none"})
        self.assertEqual(r.returncode, 1); self.assertNotIn("Traceback", r.stderr)

    def test_outside_soft_limit_is_kept_and_reported(self):                # a tighter limit set outside is not raised
        scene = self.scene("probe2.py", "import json, resource\njson.dump(resource.getrlimit(resource.RLIMIT_AS), open(__file__ + '.json', 'w'))\n"
                           + SCENE_HEAD + "\ndef build():\n    return scene(small())\n")
        cmd = f"ulimit -Sv 1500000; exec '{sys.executable}' -B '{common.RUN}' '{scene}' --out '{self.out('soft')}' --stage preview --level draft"
        r = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True, timeout=300, cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stderr[-600:])
        with open(scene + ".json") as fh:
            self.assertEqual(json.load(fh)[0], 1500000 * 1024)
        self.assertEqual(self.usage(self.out("soft"))["allowed"]["memory_mb"], 1500000 * 1024 // (1024 * 1024))

    def test_outside_limit_below_the_floor_is_a_clean_stop(self):
        cmd = f"ulimit -Sv 400000; exec '{sys.executable}' -B '{common.RUN}' '{common.LIGHTHOUSE}' --out '{self.out('low')}' --stage preview"
        r = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True, timeout=300, cwd=self.tmp)
        u = self.stopped(r, self.out("low"), "memory", stage="process setup")
        self.assertEqual(u["allowed"]["memory_mb"], u["stop"]["allowed"])

    def test_in_process_caller_keeps_its_own_children(self):               # main() called from another program
        code = """
import subprocess, sys
sys.path.insert(0, %r)
import os, time
out = sys.argv[1]
mine = subprocess.Popen(["sleep", "60"])
# a child of mine that leaves a child of its own behind and exits while the run is going on
subprocess.Popen(["sh", "-c", "sleep 61 & echo $! > '%%s'; sleep 1" %% (out + ".grandchild")])
time.sleep(0.3)
from meshpaint.__main__ import main
sys.argv = ["meshpaint", %r, "--out", out, "--stage", "preview", "--level", "draft"]
main()
grandchild = int(open(out + ".grandchild").read())
print("mine alive:", mine.poll() is None)
print("grandchild alive:", os.path.exists("/proc/%%d" %% grandchild))
mine.kill(); os.kill(grandchild, 9)
""" % (common.SCRIPTS, self.scene("plain.py", SCENE_HEAD + "\ndef build():\n    return scene(small())\n"))
        r = subprocess.run([sys.executable, "-B", "-c", code, self.out("inproc")], capture_output=True, text=True, timeout=300)
        self.assertIn("mine alive: True", r.stdout, r.stdout[-300:] + r.stderr[-600:])
        self.assertIn("grandchild alive: True", r.stdout, "a descendant of the caller's own child was killed")

    def test_stop_cannot_be_swallowed_and_strangers_are_safe(self):
        scene = self.scene("stubborn.py", "import time\n" + SCENE_HEAD + """
def build():
    end = time.time() + 15
    while time.time() < end:
        try:
            time.sleep(0.2)
        except BaseException:
            pass                              # swallows the stop request once
    return scene(small())
""")
        prefix = self.out("stubborn")
        p = subprocess.Popen([sys.executable, "-B", common.RUN, scene, "--out", prefix, "--stage", "preview"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=self.tmp)
        try:
            for _ in range(100):
                if os.path.exists(prefix + ".pid"):
                    break
                time.sleep(0.1)
            time.sleep(1.5)
            with open(prefix + ".pid") as fh:
                self.assertEqual(len(fh.read().split()), 2, "the pid file should hold the pid and its start time")
            r = self.run_engine("--stop", "--out", prefix)
            self.assertEqual(p.wait(timeout=60), 143)                       # it stopped although the scene swallowed the request
            self.assertFalse(os.path.exists(prefix + "_preview.png"))
            self.assertEqual(self.usage(prefix)["result"], "interrupted")
            self.assertFalse(os.path.exists(prefix + ".pid"))
        finally:
            if p.poll() is None:
                p.kill()
        # a pid file naming a live process from the same interpreter that is not this run: never signalled
        other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        try:
            with open(prefix + ".pid", "w") as fh:
                fh.write(f"{other.pid} 12345")                               # right pid, wrong start time: a reused pid
            r = self.run_engine("--stop", "--out", prefix)
            self.assertIn("no signal sent", r.stdout)
            with open(prefix + ".pid", "w") as fh:
                fh.write(str(other.pid))                                    # old-style pid file: no start time, so no proof
            r = self.run_engine("--stop", "--out", prefix)
            self.assertIn("no signal sent", r.stdout)
            named = self.scene("run.py", "import time; time.sleep(60)\n")   # a bystander whose script is even called run.py
            third = subprocess.Popen([sys.executable, named])
            self.addCleanup(third.kill)
            with open(prefix + ".pid", "w") as fh:
                fh.write(str(third.pid))
            r = self.run_engine("--stop", "--out", prefix)
            self.assertIn("no signal sent", r.stdout)
            time.sleep(0.3)
            self.assertIsNone(third.poll(), "--stop killed a bystander named in an old-style pid file")
            time.sleep(0.3)
            self.assertIsNone(other.poll(), "--stop killed an unrelated process")
        finally:
            other.kill(); other.wait()

    def test_ctrl_c_from_a_terminal_is_a_clean_interruption(self):
        # A terminal sends SIGINT to the whole foreground group: launcher and supervisor at once.
        scene = self.scene("sleepy.py", "import time\n" + SCENE_HEAD + "\ndef build():\n    time.sleep(30)\n    return scene(small())\n")
        import signal
        for k in range(24):
            prefix = self.out(f"int{k}")
            p = subprocess.Popen([sys.executable, "-B", common.RUN, scene, "--out", prefix, "--stage", "preview"],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, cwd=self.tmp, start_new_session=True)
            try:
                while not os.path.exists(prefix + ".pid") and p.poll() is None:
                    pass                                                    # no sleep: catch the first instant
                time.sleep((0.0, 0.0, 0.0005, 0.002, 0.01, 0.3, 1.0, 0.0)[k % 8])
                os.killpg(p.pid, signal.SIGINT)
                err = p.communicate(timeout=60)[1]
            finally:
                if p.poll() is None:
                    os.killpg(p.pid, signal.SIGKILL)
            self.assertEqual(p.returncode, 130, f"trial {k}: {err[-500:]}")
            self.assertNotIn("Traceback", err)
            self.assertEqual(self.usage(prefix)["result"], "interrupted")
            self.assertFalse(os.path.exists(prefix + ".pid"))
            self.assertFalse(os.path.exists(prefix + "_preview.png"))

    def test_stop_during_numpy_datetime_import_is_clean(self):
        # PyCapsule_Import can translate SystemExit from an interrupted datetime
        # import into ImportError. Gate the real NumPy import at that boundary.
        import signal
        hooks = self.out("import-hooks")
        os.makedirs(hooks)
        with open(os.path.join(hooks, "sitecustomize.py"), "w") as stream:
            stream.write("import os, time, builtins\n"
                         "_original = builtins.__import__\n"
                         "def _gate(name, *args, **kwargs):\n"
                         "    if name == 'datetime':\n"
                         "        with open(os.environ['MESHPAINT_DATETIME_GATE'], 'w') as f: f.write(str(os.getpid()))\n"
                         "        while True: time.sleep(0.01)\n"
                         "    return _original(name, *args, **kwargs)\n"
                         "builtins.__import__ = _gate\n")
        for signum, expected in ((signal.SIGINT, 130), (signal.SIGTERM, 143)):
            with self.subTest(signal=signum):
                ready = self.out(f"import-ready-{signum}")
                prefix = self.out(f"import-stop-{signum}")
                env = dict(os.environ, PYTHONPATH=hooks, MESHPAINT_DATETIME_GATE=ready)
                p = subprocess.Popen([sys.executable, "-B", common.RUN, common.LIGHTHOUSE,
                                      "--out", prefix, "--stage", "preview"],
                                     env=env, cwd=self.tmp, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.PIPE, text=True, start_new_session=True)
                try:
                    deadline = time.monotonic() + 20
                    while not os.path.exists(ready) and p.poll() is None and time.monotonic() < deadline:
                        time.sleep(0.005)
                    self.assertTrue(os.path.exists(ready), "NumPy datetime import was not reached")
                    p.send_signal(signum)
                    err = p.communicate(timeout=45)[1]
                finally:
                    if p.poll() is None:
                        os.killpg(p.pid, signal.SIGKILL)
                        p.wait()
                self.assertEqual(p.returncode, expected, err)
                self.assertNotIn("Traceback", err)
                self.assertEqual(self.usage(prefix)["result"], "interrupted")
                self.assertFalse(os.path.exists(prefix + ".pid"))
                self.assertFalse(os.path.exists(prefix + "_preview.png"))

    def test_dependency_import_failure_remains_a_failure(self):
        hooks = self.out("failed-import-hooks")
        os.makedirs(hooks)
        with open(os.path.join(hooks, "sitecustomize.py"), "w") as stream:
            stream.write("import builtins\n"
                         "_original = builtins.__import__\n"
                         "def _fail(name, *args, **kwargs):\n"
                         "    if name == 'numpy': raise ImportError('deliberate dependency import failure')\n"
                         "    return _original(name, *args, **kwargs)\n"
                         "builtins.__import__ = _fail\n")
        prefix = self.out("failed-import")
        result = common.run_engine([common.LIGHTHOUSE, "--out", prefix, "--stage", "preview"],
                                   env={"PYTHONPATH": hooks})
        self.assertEqual(result.returncode, 1)
        self.assertIn("deliberate dependency import failure", result.stderr)
        self.assertIn("Traceback", result.stderr)
        self.assertEqual(self.usage(prefix)["result"], "failed")

    def test_ctrl_c_to_the_launcher_alone_straight_after_start(self):
        scene = self.scene("sleepy3.py", "import time\n" + SCENE_HEAD + "\ndef build():\n    time.sleep(30)\n    return scene(small())\n")
        import signal
        for k, delay in enumerate((0.0, 0.0, 0.0, 0.0005, 0.002, 0.2, 0.0, 0.0)):
            prefix = self.out(f"solo{k}")
            p = subprocess.Popen([sys.executable, "-B", common.RUN, scene, "--out", prefix, "--stage", "preview"],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, cwd=self.tmp)
            try:
                while not os.path.exists(prefix + ".pid") and p.poll() is None:
                    pass
                time.sleep(delay)
                p.send_signal(signal.SIGINT)
                err = p.communicate(timeout=60)[1]
            finally:
                if p.poll() is None:
                    p.kill()
            self.assertEqual(p.returncode, 130, f"delay {delay}: {err[-500:]}")
            self.assertNotIn("Traceback", err)
            self.assertEqual(self.usage(prefix)["result"], "interrupted", f"delay {delay}")
            self.assertFalse(os.path.exists(prefix + ".pid"))

    def test_stop_as_the_run_finishes_never_kills_the_launcher(self):
        # Either the run was interrupted (130 or 143) or it completed (0); the usage file must say the same,
        # and the launcher must never die by signal or print a traceback, wherever in the last moments it lands.
        scene = self.scene("quick.py", SCENE_HEAD + "\ndef build():\n    return scene(small())\n")
        import signal
        seen = set()
        for k in range(36):
            prefix = self.out(f"fin{k}")
            sig = signal.SIGINT if k % 2 else signal.SIGTERM
            trigger = prefix + ("_preview.png", "_usage.json", ".pid")[k % 3]
            p = subprocess.Popen([sys.executable, "-B", common.RUN, scene, "--out", prefix, "--stage", "preview", "--level", "draft"],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, cwd=self.tmp)
            try:
                if trigger.endswith(".pid"):                               # signal just after the pid file is removed
                    while not os.path.exists(trigger) and p.poll() is None:
                        pass
                    while os.path.exists(trigger) and p.poll() is None:
                        pass
                else:
                    while not os.path.exists(trigger) and p.poll() is None:
                        pass
                time.sleep((0.0, 0.001, 0.004, 0.015)[(k // 6) % 4])
                try:
                    p.send_signal(sig)
                except ProcessLookupError:
                    pass
                err = p.communicate(timeout=60)[1]
            finally:
                if p.poll() is None:
                    p.kill()
            want = {0: "completed", 130: "interrupted", 143: "interrupted"}
            self.assertIn(p.returncode, (0, 130 if sig == signal.SIGINT else 143), f"trial {k}: {err[-400:]}")
            self.assertNotIn("Traceback", err, f"trial {k}")
            self.assertEqual(self.usage(prefix)["result"], want[p.returncode], f"trial {k}")
            self.assertFalse(os.path.exists(prefix + ".pid"))
            seen.add(p.returncode)
        self.assertIn(0, seen, "no trial landed after the run had finished; the test is not sampling the end")

    def test_stop_straight_after_start_is_not_lost(self):
        scene = self.scene("sleepy2.py", "import time\n" + SCENE_HEAD + "\ndef build():\n    time.sleep(30)\n    return scene(small())\n")
        import signal
        # Repeatedly cover the narrow startup timing that previously lost a stop.
        delays = (0.0, 0.0005, 0.002, 0.005, 0.01, 0.05, 0.0, 0.002) + (0.0005,) * 96
        for k, delay in enumerate(delays):
            prefix = self.out(f"term{k}")
            p = subprocess.Popen([sys.executable, "-B", common.RUN, scene, "--out", prefix, "--stage", "preview"],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, cwd=self.tmp)
            try:
                while not os.path.exists(prefix + ".pid") and p.poll() is None:
                    pass
                time.sleep(delay)
                p.send_signal(signal.SIGTERM)
                err = p.communicate(timeout=60)[1]
            finally:
                if p.poll() is None:
                    p.kill()
            self.assertEqual(p.returncode, 143, f"delay {delay}: {err[-500:]}")
            self.assertEqual(self.usage(prefix)["result"], "interrupted", f"delay {delay}")
            self.assertFalse(os.path.exists(prefix + "_preview.png"))

    def test_exited_launcher_is_not_taken_for_a_live_run(self):
        prefix = self.out("zombie")
        z = subprocess.Popen([sys.executable, "-c", "pass"])               # exits at once and is left unreaped
        time.sleep(0.5)
        with open(f"/proc/{z.pid}/stat") as fh:
            started = fh.read().rsplit(")", 1)[1].split()[19]
        with open(prefix + ".pid", "w") as fh:
            fh.write(f"{z.pid} {started}")
        t = time.time()
        r = self.run_engine("--stop", "--out", prefix)
        self.assertIn("no signal sent", r.stdout); self.assertLess(time.time() - t, 10)
        z.wait()

    def test_second_run_on_one_output_name_is_refused(self):
        scene = self.scene("wait.py", "import time\n" + SCENE_HEAD + "\ndef build():\n    time.sleep(8)\n    return scene(small())\n")
        prefix = self.out("shared")
        p = subprocess.Popen([sys.executable, "-B", common.RUN, scene, "--out", prefix, "--stage", "preview"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=self.tmp)
        try:
            for _ in range(100):
                if os.path.exists(prefix + ".pid"):
                    break
                time.sleep(0.1)
            r = self.run_engine(scene, "--out", prefix, "--stage", "preview")
            self.assertEqual(r.returncode, 2); self.assertIn("another run", r.stderr)
            self.assertTrue(os.path.exists(prefix + ".pid"), "the refused run removed the first run's pid file")
            self.assertEqual(p.wait(timeout=120), 0)
        finally:
            if p.poll() is None:
                p.kill()

    @unittest.skipUnless(_ACTUAL_REFERENCE_ENVIRONMENT == REFERENCE_ENVIRONMENT, _REFERENCE_PREREQUISITE)
    def test_default_output_is_what_v01_made(self):                         # A26 (previews)
        for name, scene in (("lighthouse_preview", common.LIGHTHOUSE), ("moor_preview", MOOR)):
            r = self.run_engine(scene, "--out", self.out(name), "--stage", "preview")
            self.assertEqual(r.returncode, 0, r.stderr[-800:])
            if "compiled core" not in r.stdout.splitlines()[0]:
                self.skipTest("reference hashes were recorded with the compiled core")
            self.assertEqual(common.sha(self.out(name + "_preview.png")), REFERENCE[name], name)
            self.assertEqual(sorted(os.listdir(self.tmp)), sorted(n for n in os.listdir(self.tmp) if ".part" not in n and not n.endswith(".pid")))

    def test_moor_needs_standard_and_draft_says_so(self):                   # the consequence recorded in open-questions
        r = self.run_engine(MOOR, "--out", self.out("moor"), "--stage", "preview", "--level", "draft")
        self.stopped(r, self.out("moor"), "triangles", level="draft", allowed=1_000_000)   # its triangles pass the cap first

    def test_worst_case_scene_at_draft_caps_fits_draft(self):               # A27 (draft)
        env = {"MESHPAINT_STRESS_VERTICES": "980000", "MESHPAINT_STRESS_TRIANGLES": "990000"}
        r = self.run_engine(STRESS, "--out", self.out("st"), "--stage", "paint", "--level", "draft", "--tiles", "0", env=env)
        self.assertEqual(r.returncode, 0, r.stdout[-400:] + r.stderr[-800:])
        u = self.usage(self.out("st"))
        self.assertGreater(u["usage"]["vertices"], 950_000); self.assertGreater(u["usage"]["triangles"], 950_000)
        self.assertEqual(u["usage"]["strokes_done"], 749_000)
        with Image.open(self.out("st.png")) as im:
            self.assertEqual(im.size, (1600, 1000))
        self.assertTrue(os.path.exists(self.out("st_render.png")))


@unittest.skipUnless(common.SLOW, "slow: set MESHPAINT_SLOW=1")
class SlowStops(Base):
    def test_supervisor_kills_a_renderer_that_ignores_the_timer(self):      # A23 second part
        from meshpaint import limits
        scene = self.scene("deaf.py", "import os, signal, time\n" + SCENE_HEAD +
                           "\ndef build():\n    open(__file__ + '.pid', 'w').write(str(os.getpid()))\n"
                           "    signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGALRM})\n    time.sleep(300)\n    return scene(small())\n")
        prefix = self.out("deaf")
        before = self.place_images(prefix, ["_preview.png"])
        t = time.time()
        r = self.run_engine(scene, "--out", prefix, "--stage", "preview", "--time-limit", "2")
        dt = time.time() - t
        self.assertLess(dt, 2 + limits.KILL_GRACE + 5)
        self.assertGreaterEqual(dt, 2 + limits.KILL_GRACE - 1)
        u = self.stopped(r, prefix, "time", stage="scene build", allowed=2.0)
        self.assertIn("was killed", u["stop"]["detail"])
        self.unchanged(before)
        with open(scene + ".pid") as fh:
            pid = int(fh.read())
        self.assertFalse(os.path.exists(f"/proc/{pid}"), "the renderer process is still alive")

    def test_stop_during_painting_keeps_earlier_painting_and_this_render(self):   # A24 slow part
        prefix = self.out("lh")
        r = self.run_engine(common.LIGHTHOUSE, "--out", prefix, "--stage", "render", "--level", "draft", "--tiles", "0")
        self.assertEqual(r.returncode, 0, r.stderr[-800:])
        render_seconds = self.usage(prefix)["usage"]["seconds"]
        os.remove(prefix + "_render.png")
        before = self.place_images(prefix, [".png", "_full.png"])
        r = self.run_engine(common.LIGHTHOUSE, "--out", prefix, "--stage", "paint", "--level", "draft", "--tiles", "0",
                            "--time-limit", f"{render_seconds + 5:.1f}")
        u = self.stopped(r, prefix, "time", level="draft")
        self.assertTrue(u["stop"]["stage"].startswith("paint"), u["stop"]["stage"])
        self.unchanged(before)
        with Image.open(prefix + "_render.png") as im:
            self.assertEqual(im.size, (1600, 1000))
        kept = {os.path.basename(k["path"]): k["written_by_this_run"] for k in u["images_kept"]}
        self.assertEqual(kept.get("lh_render.png"), True)
        self.assertEqual(kept.get("lh.png"), False)
        self.assertIn("lh_render.png (from this run)", r.stderr)
        if u["usage"]["strokes_done"] >= 1500:                              # at least pass 1 finished
            with Image.open(prefix + "_partial.png") as im:
                self.assertEqual(im.size, (1600, 1000))
            self.assertEqual(kept.get("lh_partial.png"), True)
        # the same job without the short limit completes, and clears the partial image
        r = self.run_engine(common.LIGHTHOUSE, "--out", prefix, "--stage", "paint", "--level", "draft", "--tiles", "0")
        self.assertEqual(r.returncode, 0, r.stderr[-800:])
        self.assertFalse(os.path.exists(prefix + "_partial.png"))
        self.assertNotEqual(common.sha(prefix + ".png"), before[prefix + ".png"])

    @unittest.skipUnless(_ACTUAL_REFERENCE_ENVIRONMENT == REFERENCE_ENVIRONMENT, _REFERENCE_PREREQUISITE)
    def test_default_paint_is_what_v01_made(self):                          # A26 (paint)
        r = self.run_engine(common.LIGHTHOUSE, "--out", self.out("p"), "--stage", "paint", "--scale", "0.5", "--tiles", "0")
        self.assertEqual(r.returncode, 0, r.stderr[-800:])
        if "compiled core" not in r.stdout.splitlines()[0]:
            self.skipTest("reference hashes were recorded with the compiled core")
        self.assertEqual(common.sha(self.out("p.png")), REFERENCE["lighthouse_paint_half"])
        self.assertEqual(common.sha(self.out("p_full.png")), REFERENCE["lighthouse_paint_half_full"])
        self.assertEqual(self.usage(self.out("p"))["usage"]["strokes_done"], 130_024)

    def test_worst_case_scene_at_standard_caps_fits_standard(self):         # A27 (standard)
        env = {"MESHPAINT_STRESS_VERTICES": "3950000", "MESHPAINT_STRESS_TRIANGLES": "3990000"}
        r = self.run_engine(STRESS, "--out", self.out("st"), "--stage", "paint", "--tiles", "0", env=env)
        self.assertEqual(r.returncode, 0, r.stdout[-400:] + r.stderr[-800:])
        u = self.usage(self.out("st"))
        self.assertGreater(u["usage"]["vertices"], 3_900_000); self.assertGreater(u["usage"]["triangles"], 3_900_000)
        self.assertEqual((u["usage"]["pixels"], u["usage"]["strokes_done"]), (9_216_000, 2_996_000))
        print(f"\n    A27: worst-case scene at Standard's caps: peak address space {u['usage']['peak_address_space_kb'] // 1024} MiB, "
              f"peak RSS {u['usage']['peak_rss_kb'] // 1024} MiB, {u['usage']['seconds']} s", file=sys.stderr)


if __name__ == "__main__":
    unittest.main()
