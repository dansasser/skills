"""A8 (dependencies) and A15 (product-neutral text): checks that read files only."""
import ast
import os
import re
import sys
import unittest

import common

ALLOWED_THIRD_PARTY = {"numpy", "scipy", "PIL", "meshpaint"}
FORBIDDEN = {"socket", "ssl", "http", "urllib", "ftplib", "requests", "httpx", "torch", "tensorflow",
             "diffusers", "openai", "anthropic"}
PRODUCT_WORDS = ("claude", "codex", "openai", "anthropic", "cursor", "copilot", "gemini", "gpt")


def python_files(root):
    for d, _, names in os.walk(root):
        if "__pycache__" in d:
            continue
        for n in names:
            if n.endswith(".py"):
                yield os.path.join(d, n)


class Dependencies(unittest.TestCase):
    def test_imports(self):
        std = set(sys.stdlib_module_names)
        for path in python_files(common.SCRIPTS):
            with open(path, encoding="utf-8") as fh:
                tree = ast.parse(fh.read())
            for node in ast.walk(tree):
                mods = []
                if isinstance(node, ast.Import):
                    mods = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    mods = [node.module]
                for m in mods:
                    top = m.split(".")[0]
                    rel = os.path.relpath(path, common.ROOT)
                    self.assertNotIn(top, FORBIDDEN, f"{rel} imports {m}")
                    self.assertTrue(top in std or top in ALLOWED_THIRD_PARTY, f"{rel} imports {m}")
                    if top == "subprocess":
                        self.assertIn(os.path.basename(path), {"native.py", "procfs.py"}, f"{rel} imports subprocess")


class ProductNeutral(unittest.TestCase):
    def test_no_product_names(self):
        pat = re.compile(r"\b(" + "|".join(PRODUCT_WORDS) + r")\b", re.IGNORECASE)
        targets = [os.path.join(common.ROOT, "SKILL.md")]
        for sub in ("references", "evals", "scripts", "assets"):
            for d, _, names in os.walk(os.path.join(common.ROOT, sub)):
                if "__pycache__" in d:
                    continue
                targets += [os.path.join(d, n) for n in names if n.endswith((".md", ".py", ".c", ".txt", ".yaml", ".json"))]
        for path in targets:
            with open(path, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
            m = pat.search(text)
            self.assertIsNone(m, f"{os.path.relpath(path, common.ROOT)} names an agent product: {m.group(0) if m else ''}")


class ReferenceCoverage(unittest.TestCase):                           # A16
    def read(self, *parts):
        with open(os.path.join(common.ROOT, *parts), encoding="utf-8") as fh:
            return fh.read()

    def test_scene_api_documents_the_new_interfaces(self):
        text = self.read("references", "scene-api.md")
        for needle in ("scatter(", "scene_rng(", "Scene.size", "--seed"):
            self.assertIn(needle, text)
        self.assertRegex(text, r"(?m)^#+ Ground detail\s*$")

    def test_skill_discloses_the_non_stroke_steps(self):
        text = self.read("SKILL.md")
        section = text[text.index("## Reporting"):]
        section = section[:section.index("\n## ", 5)] if "\n## " in section[5:] else section
        for needle in ("grain", "relief", "downsampl"):
            self.assertIn(needle, section)


if __name__ == "__main__":
    unittest.main()
