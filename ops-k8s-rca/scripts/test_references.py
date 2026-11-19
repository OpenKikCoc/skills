"""Doc contract tests: relative links resolve, required sections exist."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LINK = re.compile(r"\]\(([^)#\s]+)(?:#([^)\s]+))?\)")


def anchors(text):
    out = set()
    for line in text.splitlines():
        m = re.match(r"#{1,6}\s+(.*)", line)
        if m:
            h = m.group(1).strip().lower()
            h = re.sub(r"[^\w\s-]", "", h)
            out.add(re.sub(r"\s+", "-", h))
    return out


class DocLinks(unittest.TestCase):
    def test_links_and_anchors_resolve(self):
        for md in [ROOT / "SKILL.md", *sorted((ROOT / "references").glob("*.md"))]:
            text = md.read_text()
            for target, frag in LINK.findall(text):
                if "://" in target:
                    continue
                path = (md.parent / target).resolve()
                self.assertTrue(path.exists(), f"{md.name}: missing {target}")
                if frag and path.suffix == ".md":
                    self.assertIn(frag, anchors(path.read_text()),
                                  f"{md.name}: bad anchor {target}#{frag}")


class ContractContent(unittest.TestCase):
    def test_skill_has_conclusion_and_binding_rules(self):
        s = (ROOT / "SKILL.md").read_text()
        self.assertIn("## Conclusion rules", s)
        self.assertIn("## Evidence binding", s)
        self.assertIn("conclusion-contract.md", s)

    def test_deliverable_points_to_contract(self):
        s = (ROOT / "references/rca-deliverable.md").read_text()
        self.assertIn("conclusion-contract.md", s)

    def test_mechanism_sections_have_required_parts(self):
        s = (ROOT / "references/mechanism-checks.md").read_text()
        for sec in re.split(r"\n## ", s)[1:]:
            title = sec.splitlines()[0]
            if not re.match(r"[A-G]\. ", title):
                continue
            for part in ("**Signature.**", "**Mechanism.**", "**Ordered reads.**",
                         "**Falsifiers.**", "**Conclude.**"):
                self.assertIn(part, sec, f"{title}: missing {part}")

    def test_contract_has_self_check_and_grades(self):
        c = (ROOT / "references/conclusion-contract.md").read_text()
        self.assertIn("## Self-check", c)
        g = (ROOT / "references/entity-derivation-and-freshness.md").read_text()
        for grade in ("OBSERVED", "EXTRACTED", "INFERRED", "STALE-RISK", "UNKNOWN"):
            self.assertIn(grade, g)


if __name__ == "__main__":
    unittest.main()
