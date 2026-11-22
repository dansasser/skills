"""Public source completeness, portable verification and generated-file handling."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

import common


class PublicSource(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory(prefix="meshpaint public source ")
        self.addCleanup(self.work.cleanup)
        self.root = Path(self.work.name)

    def git(self, root, *args):
        if not shutil.which("git"):
            self.skipTest("Git is needed for repository-inventory checks")
        env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1",
                   GIT_OPTIONAL_LOCKS="0")
        return subprocess.check_output(["git", "-C", str(root), *args], env=env, text=True)

    def test_inventory_contains_every_tracked_file_even_unknown_or_missing(self):
        self.git(self.root, "init", "--quiet", "--template=")
        (self.root / "SKILL.md").write_text("skill")
        (self.root / "extra-document.json").write_text("review me")
        self.git(self.root, "add", "SKILL.md", "extra-document.json")
        (self.root / "extra-document.json").unlink()
        self.assertEqual(common.public_source_files(self.root), ["SKILL.md", "extra-document.json"])

    def test_gitless_copy_does_not_inherit_its_parent_repository_inventory(self):
        self.git(self.root, "init", "--quiet", "--template=")
        (self.root / "parent-only.txt").write_text("not part of the source copy")
        self.git(self.root, "add", "parent-only.txt")
        copy = self.root / "source copy"
        copy.mkdir()
        (copy / "SKILL.md").write_text("skill")
        (copy / "extra-document.json").write_text("review me")
        self.assertEqual(common.public_source_files(copy), ["SKILL.md", "extra-document.json"])
        for directory in (".git", ".venv", "workspace", "out", "__pycache__"):
            generated = copy / directory
            generated.mkdir()
            (generated / "generated.txt").write_text("generated")
        self.assertEqual(common.public_source_files(copy), ["SKILL.md", "extra-document.json"])

    def test_public_inventory_includes_runtime_examples_references_and_gallery(self):
        files = set(common.public_source_files())
        required = {
            ".gitignore", "SKILL.md", "README.md", "requirements.txt", "agents/openai.yaml",
            "scripts/run.py", "assets/scenes/lighthouse.py", "assets/scenes/moor.py",
            "references/scene-api.md", "references/limits.md", "evals/cases.md",
            "doc/examples/lighthouse_golden_hour.png", "doc/examples/moor_golden_hour.png",
            "tests/common.py", "tools/check.py",
        }
        required.update("scripts/meshpaint/" + name for name in (
            "__init__.py", "__main__.py", "core.c", "geometry.py", "lens.py", "limits.py",
            "looks.py", "mesh.py", "native.py", "paint.py", "pipeline.py", "procfs.py",
            "scene.py", "sky.py", "util.py", "water.py"))
        self.assertEqual(required - files, set(), "public source inventory is incomplete")
        for name in files:
            self.assertTrue(Path(common.ROOT, name).is_file(), f"missing public source file: {name}")

    def test_gitless_inventory_exposes_links_and_copy_refuses_them(self):
        source = self.root / "source"
        source.mkdir()
        target = self.root / "external.txt"
        target.write_text("not bundled")
        (source / "linked-file.py").symlink_to(target)
        (source / "linked-directory").symlink_to(self.root, target_is_directory=True)
        files = common.public_source_files(source)
        self.assertEqual(files, ["linked-directory", "linked-file.py"])
        with self.assertRaisesRegex(ValueError, "public source contains a link"):
            common.copy_files(files, self.root / "copy", root=source)

    def test_workspace_outputs_are_ignored_but_bundled_resources_are_not(self):
        self.git(self.root, "init", "--quiet", "--template=")
        shutil.copyfile(Path(common.ROOT, ".gitignore"), self.root / ".gitignore")
        generated = (
            ".venv/bin/python", ".venv310/bin/python", "workspace/lighthouse.py",
            "workspace/out/lighthouse.png", "out/lighthouse.png", "lighthouse.png",
            "lighthouse_preview.png", "lighthouse_render.png", "lighthouse_full.png",
            "lighthouse_partial.png", "lighthouse_tile_r1c1.png", "lighthouse_usage.json",
            "lighthouse.pid", "lighthouse.lock", "lighthouse.png.part-123",
            "scripts/meshpaint/core.so", ".cache/meshpaint/core.so", "build/core.o",
        )
        for name in generated:
            with self.subTest(generated=name):
                self.assertEqual(self.git(self.root, "check-ignore", name).strip(), name)
        for name in ("assets/scenes/lighthouse.py", "doc/examples/lighthouse_golden_hour.png",
                     "doc/examples/moor_golden_hour.png", "requirements.txt", "scripts/meshpaint/core.c"):
            with self.subTest(source=name):
                result = subprocess.run(["git", "-C", str(self.root), "check-ignore", name],
                                        env=dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull,
                                                 GIT_CONFIG_NOSYSTEM="1"), capture_output=True)
                self.assertEqual(result.returncode, 1, f"bundled source is ignored: {name}")


class PublicChecker(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory(prefix="meshpaint checker ")
        self.addCleanup(self.work.cleanup)
        self.root = Path(self.work.name)

    def run_checker(self, checker, *args, env=None):
        variables = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        variables.pop("MESHPAINT_SLOW", None)
        variables.update(env or {})
        return subprocess.run([sys.executable, "-B", str(checker), *args], cwd=self.root,
                              env=variables, capture_output=True, text=True, timeout=60)

    def fixture_checker(self, source):
        root = self.root / "source without git"
        (root / "tools").mkdir(parents=True)
        (root / "tests").mkdir()
        checker = root / "tools" / "check.py"
        shutil.copyfile(Path(common.ROOT, "tools", "check.py"), checker)
        (root / "tests" / "test_fixture.py").write_text(source)
        return checker

    def test_checker_uses_selected_interpreter_from_an_unrelated_working_directory(self):
        logger = self.root / "startup"
        logger.mkdir()
        events = self.root / "interpreters.jsonl"
        (logger / "sitecustomize.py").write_text(
            "import json, os, sys\n"
            "with open(os.environ['MESHPAINT_INTERPRETER_LOG'], 'a') as stream:\n"
            "    stream.write(json.dumps({'pid': os.getpid(), 'executable': sys.executable, "
            "'prefix': sys.prefix}) + '\\n')\n")
        env = {"PYTHONPATH": str(logger), "MESHPAINT_INTERPRETER_LOG": str(events)}
        reply = self.run_checker(Path(common.ROOT, "tools", "check.py"),
                                 "--test", "test_static.Dependencies.test_imports", env=env)
        self.assertEqual(reply.returncode, 0, reply.stdout + reply.stderr)
        records = [json.loads(line) for line in events.read_text().splitlines()]
        self.assertGreaterEqual(len({record["pid"] for record in records}), 2)
        self.assertEqual({record["executable"] for record in records}, {sys.executable})
        self.assertEqual({record["prefix"] for record in records}, {sys.prefix})
        self.assertIn("Ran 1 test", reply.stderr)

    def test_checker_propagates_a_test_failure_without_git_or_private_tools(self):
        checker = self.fixture_checker(
            "import unittest\nclass Failure(unittest.TestCase):\n"
            "    def test_failure(self):\n        self.fail('intentional verification failure')\n")
        reply = self.run_checker(checker)
        self.assertEqual(reply.returncode, 1, reply.stdout + reply.stderr)
        self.assertIn("intentional verification failure", reply.stderr)

    def test_empty_default_and_focused_selections_are_rejected(self):
        checker = self.fixture_checker(
            "import unittest\nclass Empty(unittest.TestCase):\n    pass\n")
        for args in ((), ("--test", "test_fixture.Empty")):
            with self.subTest(arguments=args):
                reply = self.run_checker(checker, *args)
                self.assertEqual(reply.returncode, 5, reply.stdout + reply.stderr)
                self.assertIn("no tests", reply.stderr.lower())
                self.assertNotIn("OK", reply.stderr)

    def test_missing_focused_target_is_a_verification_failure(self):
        checker = self.fixture_checker(
            "import unittest\nclass Exists(unittest.TestCase):\n"
            "    def test_ok(self):\n        pass\n")
        reply = self.run_checker(checker, "--test", "test_fixture.Missing")
        self.assertEqual(reply.returncode, 1, reply.stdout + reply.stderr)
        self.assertIn("Missing", reply.stderr)

    def test_missing_dependencies_fail_explicitly_without_switching_interpreters(self):
        env = dict(os.environ, PYTHONPATH="", PYTHONDONTWRITEBYTECODE="1")
        reply = subprocess.run([sys.executable, "-B", "-S", str(Path(common.ROOT, "tools", "check.py"))],
                               cwd=self.root, env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(reply.returncode, 2, reply.stdout + reply.stderr)
        self.assertIn("Missing Python dependencies", reply.stderr)
        self.assertIn("Install with:", reply.stderr)
        self.assertIn(sys.executable, reply.stderr)
        self.assertNotIn("Ran ", reply.stderr)

    def test_slow_checks_are_explicitly_enabled(self):
        checker = self.fixture_checker(
            "import os, unittest\n"
            "@unittest.skipUnless(os.environ.get('MESHPAINT_SLOW') == '1', 'slow disabled')\n"
            "class Slow(unittest.TestCase):\n"
            "    def test_mode(self):\n        self.assertEqual(os.environ['MESHPAINT_SLOW'], '1')\n")
        default = self.run_checker(checker)
        self.assertEqual(default.returncode, 0, default.stdout + default.stderr)
        self.assertIn("skipped", default.stderr)
        slow = self.run_checker(checker, "--slow")
        self.assertEqual(slow.returncode, 0, slow.stdout + slow.stderr)
        self.assertNotIn("skipped", slow.stderr)
        self.assertIn("Ran 1 test", slow.stderr)


if __name__ == "__main__":
    unittest.main()
