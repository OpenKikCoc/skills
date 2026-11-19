"""Doc contract tests for the staged-write reference."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class StagedWriteDocs(unittest.TestCase):
    def test_links_resolve(self):
        for md in [ROOT / "SKILL.md", *(ROOT / "references").glob("*.md")]:
            for target in re.findall(r"\]\(([^)#\s]+)", md.read_text()):
                if "://" in target:
                    continue
                self.assertTrue((md.parent / target).resolve().exists(),
                                f"{md.name}: missing {target}")

    def test_vocabulary_present(self):
        s = (ROOT / "references/staged-writes.md").read_text()
        for term in ("immutable intent", "authoritative pre-read", "outcome_unknown",
                     "reconcile", "applied", "absent", "conflict",
                     "not permission to replay", "intermediate read-back"):
            self.assertIn(term, s)

    def test_skill_routes_to_it(self):
        self.assertIn("staged-writes.md", (ROOT / "SKILL.md").read_text())


if __name__ == "__main__":
    unittest.main()
