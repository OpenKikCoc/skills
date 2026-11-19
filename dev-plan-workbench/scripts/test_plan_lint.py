#!/usr/bin/env python3
"""Offline lint tests. Exit 0 pass, 1 failure, 2 usage."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(os.environ.get('PLAN_TEST_LINT_SCRIPT', Path(__file__).with_name('plan_lint.py')))
NUM = '| value | x |'


class LintTests(unittest.TestCase):
    def run_lint(self, doc, config=None, numbers=None, fmt='json'):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'doc.md').write_text(doc, encoding='utf-8')
            (root / 'config.json').write_text(json.dumps(config or {}), encoding='utf-8')
            args = [sys.executable, '-I', str(SCRIPT), '--doc', str(root / 'doc.md'),
                    '--config', str(root / 'config.json'), '--format', fmt]
            if numbers is not None:
                (root / 'NUMBERS.md').write_text(numbers, encoding='utf-8')
                args += ['--numbers', str(root / 'NUMBERS.md')]
            result = subprocess.run(args, capture_output=True, text=True)
            out = json.loads(result.stdout) if fmt == 'json' and result.stdout else result.stdout
            return result.returncode, out

    def rules(self, result):
        return [f['rule'] for f in result['findings']]

    def quantities(self, result):
        return [f['message'].split(': ')[-1] for f in result['findings'] if f['rule'] == 'unlisted-number']

    def test_clean_and_normalization(self):
        rc, result = self.run_lint(
            '## Capacity\nAvailable: ~1,280 cores. Reference: [Design](https://example.invalid/reference).',
            {'required_links': ['Design']},
            '| value | section | source | basis | snapshot |\n| about 1280 cores | Capacity | s:2 | reserve excluded | <date> |')
        self.assertEqual((rc, result['findings']), (0, []))

    def test_each_rule_and_occurrences(self):
        doc = ('## Repeated\nWe TBD XX ?? TOKEN TOKEN PNAME.\n##### Deep\n'
               'Long sentence, one, two; three.\n## Repeated\nDesign appears without link. Capacity 37 nodes.')
        rc, result = self.run_lint(doc, {
            'banned_terms': [{'term': 'TOKEN', 'reason': 'avoid literal instruction'}],
            'person_names': ['PNAME'], 'required_links': ['Design'], 'max_sentence_chars': 12,
            'max_enumeration_marks': 2}, '| 640 cores | example | s:2 | basis | <date> |')
        self.assertEqual(rc, 1)
        self.assertEqual(set(self.rules(result)), {
            'first-person', 'placeholder', 'banned-term', 'person-name', 'required-link', 'sentence-length',
            'enumeration', 'heading-depth', 'duplicate-heading', 'unlisted-number'})
        self.assertEqual(self.rules(result).count('banned-term'), 2)
        self.assertEqual(result['findings'], sorted(
            result['findings'], key=lambda f: (f['line'], f['col'], f['rule'], f['message'])))
        self.assertTrue(all(f['line'] > 0 and f['col'] > 0 and f['excerpt'] for f in result['findings']))

    def test_whole_word_terms(self):
        # "we" must not match inside "answer" or "network"; "I" is case-sensitive.
        rc, result = self.run_lint('The answer is a network. i think it is fine.')
        self.assertEqual((rc, result['findings']), (0, []))
        rc, result = self.run_lint('Then I decided. Our plan.')
        self.assertEqual(rc, 1)
        self.assertEqual(self.rules(result), ['first-person', 'first-person'])

    def test_exemptions(self):
        doc = ('## 2.2 Capacity\n1900 2099 FY99 2099Q1 Q2 H1 2.2.1 Figure 3 Table 2 Section 4 §5.1.\n'
               '1. item\n2) item\n[Reference](https://example.invalid/r/765) `99 nodes`.\n'
               'https://example.invalid/r/886 .\n```md\nwe TOKEN TBD 55 nodes\n```\n~~~\n66 nodes\n~~~\n')
        rc, result = self.run_lint(doc, {'banned_terms': [{'term': 'TOKEN', 'reason': 'banned'}]},
                                   '| 1 core | x |')
        self.assertEqual(rc, 0)
        self.assertEqual(result['findings'], [])

    def test_link_destination_masked_but_label_linted(self):
        rc, result = self.run_lint('[we](https://example.invalid/TBD/we/77).', numbers=NUM)
        self.assertEqual(rc, 1)
        self.assertEqual(self.rules(result), ['first-person'])

    def test_first_mention_each_section(self):
        rc, result = self.run_lint(
            '## A\n[Design](https://example.invalid/a). Design.\n## B\nDesign. Later [Design](https://example.invalid/b).',
            {'required_links': ['Design']})
        self.assertEqual(rc, 0)
        found = [f for f in result['findings'] if f['rule'] == 'required-link']
        self.assertEqual([f['line'] for f in found], [4])

    def test_quantities_and_warning_exit(self):
        rc, result = self.run_lint('32% 1,500.5 GiB 8 nodes 7.2ms 10x 9k.',
                                   numbers='| 32% | x |\n| 1500.5GiB | x |')
        self.assertEqual(rc, 0)
        self.assertEqual(self.quantities(result), ['8 nodes', '7.2ms', '10x', '9k'])

    def test_adjacent_units_and_year_shaped_quantities(self):
        rc, result = self.run_lint(
            'Capacity 999 cores, share 40%; latency 7.2ms. Size 2099 cores; fleet of 1900 hosts.',
            numbers='| 1 core | x |')
        self.assertEqual(rc, 0)
        self.assertEqual(self.quantities(result), ['999 cores', '40%', '7.2ms', '2099 cores', '1900 hosts'])
        # A bare year is exempt, a unit-bearing year-shaped number is not.
        _, result = self.run_lint('In 2099 it ships.', numbers=NUM)
        self.assertEqual(self.quantities(result), [])

    def test_sentence_boundaries_and_decimals(self):
        doc = '- short\n## Heading\n| a | b |\nShort.  This sentence is actually far too long.'
        _, result = self.run_lint(doc, {'max_sentence_chars': 20})
        found = [f for f in result['findings'] if f['rule'] == 'sentence-length']
        self.assertEqual(len(found), 1)
        self.assertEqual((found[0]['line'], found[0]['col']), (4, 9))
        self.assertEqual(found[0]['excerpt'], 'This sentence is actually far too long.')
        # A decimal point does not end a sentence.
        _, result = self.run_lint('Latency is 1.5 ms today.', {'max_sentence_chars': 30}, '| 1.5 ms | x |')
        self.assertEqual(result['findings'], [])

    def test_dates_and_period_labels(self):
        _, result = self.run_lint('Snapshot 2099-01-02, 2099-01, 2099/01/02, 99H1, 98Q4, 2099H2.', numbers=NUM)
        self.assertEqual(self.quantities(result), [])
        _, result = self.run_lint('Capacity 2099 cores, share 17%.', numbers='| 1 core | x |')
        self.assertEqual(self.quantities(result), ['2099 cores', '17%'])
        for bad in ('2099-00-02', '2099-13-02', '2099-01-32', '2099-01-02 cores'):
            _, result = self.run_lint(bad, numbers=NUM)
            self.assertTrue(self.quantities(result), bad)

    def test_fullwidth_whitelist_and_offsets(self):
        doc = 'Share ４０％; size 1,234,567 cores.'
        _, result = self.run_lint(doc, numbers='| 40% | x |\n| 1234567 cores | x |')
        self.assertEqual(result['findings'], [])
        _, result = self.run_lint(doc, numbers='| 1 core | x |')
        found = [f for f in result['findings'] if f['rule'] == 'unlisted-number']
        self.assertEqual([f['col'] for f in found], [7, 17])
        self.assertTrue(found[0]['excerpt'].startswith('４０％'))

    def test_non_newline_whitespace_in_quantities(self):
        for space in ('　', ' ', '\t'):
            doc = 'Size 2099' + space + 'cores.'
            _, result = self.run_lint(doc, numbers='| 2099 cores | x |')
            self.assertEqual(result['findings'], [], repr(space))
            _, result = self.run_lint(doc, numbers=NUM)
            self.assertEqual(len(self.quantities(result)), 1, repr(space))
        _, result = self.run_lint('Size 2099\ncores.', numbers=NUM)
        self.assertEqual(self.quantities(result), [])

    def test_visible_sentence_length(self):
        doc = ('[Short](https://example.invalid/' + 'x' * 200 + ') `' + 'code' * 50 + '`.\n'
               'Short.  ' + 'Long' * 6 + '.')
        _, result = self.run_lint(doc, {'max_sentence_chars': 20})
        found = [f for f in result['findings'] if f['rule'] == 'sentence-length']
        self.assertEqual([(f['line'], f['col'], f['excerpt']) for f in found], [(2, 9, 'Long' * 6 + '.')])

    def test_empty_and_zero_length_patterns(self):
        for doc in ('', 'safe', 'safe\n'):
            for pattern in ('^', '$', '(?=safe)'):
                with self.subTest(doc=doc, pattern=pattern):
                    rc, result = self.run_lint(doc, {'placeholder_patterns': [pattern]})
                    self.assertEqual((rc, result['findings']), (0, []))
        rc, result = self.run_lint('TBD', {'placeholder_patterns': ['TBD|$']})
        self.assertEqual((rc, len(result['findings'])), (1, 1))

    def test_config_errors(self):
        for config in ({'unknown': True}, {'banned_terms': ['bad']}, {'person_names': 'bad'},
                       {'max_sentence_chars': True}, {'placeholder_patterns': ['[']},
                       {'number_check': True}, {'required_links': [3]}, {'number_check': 'yes'}):
            with self.subTest(config=config):
                rc, _ = self.run_lint('doc', config)
                self.assertEqual(rc, 2)

    def test_custom_rules_text_and_disabled_number_check(self):
        rc, out = self.run_lint('I UNKNOWN.', {'forbid_first_person': ['I'], 'placeholder_patterns': ['UNKNOWN']},
                                fmt='text')
        self.assertEqual(rc, 1)
        self.assertIn(':1:1: error first-person:', out)
        self.assertIn('error placeholder:', out)
        _, result = self.run_lint('37 cores.', {'number_check': False}, numbers='| 1 core | x |')
        self.assertEqual(result['findings'], [])


if __name__ == '__main__':
    argparse.ArgumentParser(description=__doc__).parse_args()
    result = unittest.TextTestRunner(verbosity=1).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(LintTests))
    print(f'Lint tests: {result.testsRun} run; failures={len(result.failures)}; errors={len(result.errors)}')
    raise SystemExit(0 if result.wasSuccessful() else 1)
