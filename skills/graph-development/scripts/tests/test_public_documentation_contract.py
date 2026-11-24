from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class PublicDocumentationContractTests(unittest.TestCase):
    def test_readme_follows_the_collection_front_door(self) -> None:
        text = (ROOT / "README.md").read_text()
        headings = ["## Requirements", "## Install", "## Runtime and paths", "## Quick start",
                    "## Architecture and documentation", "## Verification", "## License"]
        offsets = [text.index(heading) for heading in headings]
        self.assertEqual(offsets, sorted(offsets))
        for phrase in ("Linux", "Python 3.12", "dansasser/skills", "--skill graph-development",
                       "--copy", "Apache-2.0", "outcome-contract.json", "development-graph.projection.json"):
            self.assertIn(phrase, text)

    def test_public_operator_and_architecture_documents_exist(self) -> None:
        for name in ("README", "architecture", "getting-started", "configuration", "operations", "development"):
            self.assertTrue((ROOT / "doc" / (name + ".md")).is_file(), name)
        self.assertFalse((ROOT / "docs" / "specifications").exists())
        self.assertFalse((ROOT / "reports").exists())

    def test_markdown_fences_and_local_links_are_self_contained(self) -> None:
        for source in ROOT.rglob("*.md"):
            text = source.read_text()
            self.assertEqual(text.count("`" * 3) % 2, 0, str(source.relative_to(ROOT)))
            for raw in re.findall(r"!?\[[^\]]*\]\(([^)]+)\)", text):
                target = raw.split("#", 1)[0]
                if not target or target.startswith(("http://", "https://", "mailto:")):
                    continue
                path = (source.parent / target).resolve()
                self.assertTrue(path.is_relative_to(ROOT.resolve()), raw)
                self.assertTrue(path.is_file(), str(source.relative_to(ROOT)) + " -> " + raw)

    def test_complete_runtime_example_is_bundled_and_quoted(self) -> None:
        readme = (ROOT / "README.md").read_text()
        self.assertIn('"$PYTHON" "$SKILL_DIR/assets/examples/run_example.py"', readme)
        for script in ("validate_graph.py", "graphctl.py", "graph_worker.py", "verify_distribution.py"):
            self.assertTrue((ROOT / "scripts" / script).is_file(), script)
        for required in ("--expected-base-digest", "--expected-plan-digest", "--contract", "--projection",
                         "verify-ledger", "result.txt", "synthetic"):
            self.assertIn(required, readme)

    def test_docs_hub_names_all_public_operator_paths(self) -> None:
        text = (ROOT / "doc" / "README.md").read_text()
        for name in ("architecture", "getting-started", "configuration", "operations", "development"):
            self.assertIn(name + ".md", text)
        self.assertNotIn("specifications/", text)

    def test_public_installation_uses_one_complete_collection_folder(self) -> None:
        text = (ROOT / "README.md").read_text()
        self.assertIn("complete `skills/graph-development` folder", text)
        self.assertIn(".agents/skills/graph-development/", text)
        self.assertIn(".claude/skills/graph-development/", text)
        self.assertNotRegex(text, r"https://github\.com/[^/\s]+/graph-development[^/\s]*\.git")
        self.assertNotIn("installable-package", text)
        self.assertNotIn("release archive", text)

    def test_verification_claims_keep_agent_acceptance_separate(self) -> None:
        text = (ROOT / "README.md").read_text().lower()
        self.assertIn("deterministic", text)
        self.assertIn("fresh", text)
        self.assertIn("invocation", text)
        self.assertIn("provider", text)


if __name__ == "__main__":
    unittest.main()
