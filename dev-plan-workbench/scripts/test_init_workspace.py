#!/usr/bin/env python3
"""Offline workspace and docs-contract tests. Exit 0 pass, 1 failure, 2 usage."""
import argparse
import json
import re
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).with_name('init_workspace.py')
SKILL = SCRIPT.parent.parent


class WorkspaceTests(unittest.TestCase):
    def run_init(self, root, *args, cwd=None):
        return subprocess.run([sys.executable, '-I', str(SCRIPT), '--dir', str(root), *args],
                              capture_output=True, text=True, cwd=cwd)

    def snapshot(self, root):
        return {p.name: p.read_bytes() for p in root.iterdir() if p.is_file()}

    def test_create_refuse_and_fill(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'workspace'
            result = self.run_init(root, '--title', 'Neutral plan')
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(len(json.loads(result.stdout)['created']), 12)
            files = self.snapshot(root)
            self.assertEqual(len(files), 9)
            self.assertIn(b'Neutral plan', files['CONTRACT.md'])
            result = self.run_init(root)
            self.assertEqual(result.returncode, 3)
            self.assertEqual(len(json.loads(result.stdout)['conflicts']), 9)
            self.assertEqual(files, self.snapshot(root))
            (root / 'NUMBERS.md').unlink()
            (root / 'reviews').rmdir()
            result = self.run_init(root, '--missing-only')
            self.assertEqual(result.returncode, 0)
            self.assertEqual(json.loads(result.stdout)['created'], ['NUMBERS.md', 'reviews/'])
            self.assertEqual(files, self.snapshot(root))

    def test_conflict_is_preflight(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'NUMBERS.md').write_bytes(b'private evidence')
            result = self.run_init(root)
            self.assertEqual(result.returncode, 3)
            self.assertEqual(list(root.iterdir()), [root / 'NUMBERS.md'])

    def test_symlink_and_directory_safety(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'workspace'
            root.mkdir()
            outside = Path(tmp) / 'outside'
            outside.mkdir()
            (root / 'reviews').symlink_to(outside, target_is_directory=True)
            result = self.run_init(root)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(list(outside.iterdir()), [])
            self.assertFalse((root / 'CONTRACT.md').exists())

    def test_symlinked_state_file_is_not_followed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'workspace'
            root.mkdir()
            target = Path(tmp) / 'secret.txt'
            target.write_text('keep')
            (root / 'RULES.md').symlink_to(target)
            self.assertEqual(self.run_init(root).returncode, 3)
            self.assertEqual(self.run_init(root, '--missing-only').returncode, 0)
            self.assertEqual(target.read_text(), 'keep')

    def test_symlinked_workspace_root_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            real = Path(tmp) / 'real'
            real.mkdir()
            link = Path(tmp) / 'link'
            link.symlink_to(real, target_is_directory=True)
            self.assertEqual(self.run_init(link).returncode, 2)
            self.assertEqual(list(real.iterdir()), [])

    def test_documented_command_runs_from_unrelated_cwd_and_lint_passes(self):
        skill = (SKILL / 'SKILL.md').read_text()
        self.assertIn('`SKILL_ROOT`', skill)
        self.assertIn('`WORKSPACE`', skill)
        for path in [SKILL / 'SKILL.md', *sorted((SKILL / 'references').glob('*.md'))]:
            for line in path.read_text().splitlines():
                if 'python3 ' in line and '.py' in line:
                    self.assertIn('"$SKILL_ROOT/scripts/', line, str(path))
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp) / 'unrelated'
            cwd.mkdir()
            workspace = Path(tmp) / 'workspace'
            command = re.search(r'python3 .*init_workspace\.py.*', skill)[0]
            command = command.replace('$SKILL_ROOT', str(SKILL)).replace('$WORKSPACE', str(workspace))
            args = shlex.split(command)
            args[0] = sys.executable
            result = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            draft = workspace / 'versions' / 'draft.md'
            draft.write_text('## Main\nSafe short plan.\n')
            result = subprocess.run([sys.executable, '-I', str(SCRIPT.with_name('plan_lint.py')),
                                     '--doc', str(draft), '--config', str(workspace / 'lint.json')],
                                    cwd=cwd, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_template_example_rows_are_marked_illustrative(self):
        for name in ('NUMBERS.md', 'FEEDBACK.md'):
            text = (SKILL / 'assets/templates' / name).read_text()
            self.assertRegex(text, r'invented|illustrative|example')

    def test_review_contract_phrases(self):
        skill = (SKILL / 'SKILL.md').read_text()
        review = (SKILL / 'references/review-protocol.md').read_text()
        for phrase in ('degraded independence', 'publishable-after-fixes', 'not-publishable'):
            self.assertIn(phrase, skill + review)
        self.assertIn('NOT EXECUTABLE', review)
        self.assertIn('fresh approval', (SKILL / 'references/publishing.md').read_text())

    def test_links_resolve_and_no_platform_wiring(self):
        for path in [SKILL / 'SKILL.md', *SKILL.glob('references/*.md')]:
            for target in re.findall(r'\]\(([^)#]+)\)', path.read_text()):
                if '://' not in target:
                    self.assertTrue((path.parent / target).exists(), f'{path.name}: {target}')

    def test_usage(self):
        result = subprocess.run([sys.executable, '-I', str(SCRIPT)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)


if __name__ == '__main__':
    argparse.ArgumentParser(description=__doc__).parse_args()
    result = unittest.TextTestRunner(verbosity=1).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(WorkspaceTests))
    print(f'Workspace tests: {result.testsRun} run; failures={len(result.failures)}; errors={len(result.errors)}')
    raise SystemExit(0 if result.wasSuccessful() else 1)
