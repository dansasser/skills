from __future__ import annotations

import unittest
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[2]


class DocumentationContractTests(unittest.TestCase):
    def test_operator_readme_links_complete_contracts_and_helpers(self) -> None:
        readme = "\n".join(p.read_text(encoding="utf-8") for p in [SKILL_ROOT / "README.md", SKILL_ROOT / "SKILL.md", *sorted((SKILL_ROOT / "doc").glob("*.md"))])
        for required in [
            "references/future-direction.md",
            "references/runtime-contract.md",
            "references/mutation-contract.md",
            "scripts/new_graph.py",
            "scripts/graphctl.py",
            "scripts/graph_worker.py",
            "scripts/rewrite_graph.py",
            "complete",
            "relative",
            "relative",
            "runtime",
            "rollback",
        ]:
            self.assertIn(required, readme)

    def test_future_direction_rejects_silent_self_rewriting(self) -> None:
        future = (SKILL_ROOT / "references" / "future-direction.md").read_text(encoding="utf-8").lower()
        self.assertIn("no silent self-rewriting", future)
        self.assertIn("distributed", future)
        self.assertIn("maturity", future)


if __name__ == "__main__":
    unittest.main()
