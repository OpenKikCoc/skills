#!/usr/bin/env python3
"""Lint planning Markdown. Exit 0 no errors (warnings allowed), 1 errors, 2 usage/config/I/O."""
import argparse
import json
import re
from pathlib import Path


DEFAULTS = {
    'banned_terms': [], 'person_names': [], 'forbid_first_person': ['we', 'our', 'I'],
    'placeholder_patterns': [r'(?<![A-Za-z])X{1,2}(?![A-Za-z])', r'\bTBD\b', r'\bTODO\b', r'\?\?'],
    'required_links': [], 'max_sentence_chars': 160, 'max_enumeration_marks': 6,
    'max_heading_depth': 4, 'number_check': False,
}
UNIT_WORDS = (r'(?:GiB|TiB|MiB|KiB|GB|TB|MB|KB|ms|us|%|x|cores?|nodes?|hosts?|pods?|users?|'
              r'teams?|people|engineers?|days?|hours?|minutes?|seconds?|weeks?|months?|years?|times)')
UNITS = r'(?:' + UNIT_WORDS + r'(?![A-Za-z])|[kKMBGT](?![A-Za-z]))'
SPACE = r'[^\S\r\n\v\f\x85  ]'
QUANTITY = re.compile(r'(?<![A-Za-z0-9_.])\d+(?:,\d{3})*(?:\.\d+)?(?:' + SPACE + r'*' + UNITS + r')?')
WIDTH_MAP = str.maketrans({**{chr(i): chr(i - 0xFEE0) for i in range(0xFF01, 0xFF5F)}, '　': ' '})
LINK = re.compile(r'\[([^\]\n]+)\]\((https?://[^\s)]+)\)')


def halfwidth(value):
    # One source character stays one character, so offsets remain exact.
    return value.translate(WIDTH_MAP)


def normalize(value):
    value = halfwidth(value).strip()
    value = re.sub(r'^(?:~|about\s+|approx\.?\s+|around\s+)', '', value, flags=re.I)
    return re.sub(r'[,\s]', '', value).lower()


def mask(markdown, inline=False):
    chars = list(markdown)

    def hide(start, end):
        for i in range(start, end):
            if chars[i] != '\n':
                chars[i] = ' '
    fence = None
    offset = 0
    for line in markdown.splitlines(keepends=True):
        match = re.match(r'^\s{0,3}(`{3,}|~{3,})', line)
        if fence:
            hide(offset, offset + len(line))
            if match and match[1][0] == fence[0] and len(match[1]) >= len(fence):
                fence = None
        elif match:
            fence = match[1]
            hide(offset, offset + len(line))
        offset += len(line)
    for match in re.finditer(r'\]\((?:[^()\n]|\([^()\n]*\))*\)', markdown):
        hide(match.start() + 2, match.end() - 1)
    if inline:
        for match in re.finditer(r'(`+)(.*?)\1', markdown):
            hide(match.start(), match.end())
        for match in re.finditer(r'https?://[^\s<>]+', markdown):
            hide(match.start(), match.end())
    return ''.join(chars)


def load_config(path, numbers):
    supplied = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(supplied, dict) or set(supplied) - set(DEFAULTS):
        raise ValueError('unknown config keys or config is not an object')
    config = dict(DEFAULTS, number_check=bool(numbers))
    config.update(supplied)
    for key in ('person_names', 'forbid_first_person', 'placeholder_patterns', 'required_links'):
        if not isinstance(config[key], list) or any(not isinstance(v, str) or not v for v in config[key]):
            raise ValueError(key + ' must be a list of nonempty strings')
    if not isinstance(config['banned_terms'], list):
        raise ValueError('banned_terms must be a list')
    for item in config['banned_terms']:
        if (not isinstance(item, dict) or set(item) != {'term', 'reason'}
                or any(not isinstance(v, str) or not v for v in item.values())):
            raise ValueError('banned_terms entries require term and reason')
    for key in ('max_sentence_chars', 'max_enumeration_marks', 'max_heading_depth'):
        if type(config[key]) is not int or config[key] < 1:
            raise ValueError(key + ' must be a positive integer')
    if type(config['number_check']) is not bool:
        raise ValueError('number_check must be boolean')
    for pattern in config['placeholder_patterns']:
        re.compile(pattern)
    if config['number_check'] and not numbers:
        raise ValueError('number_check requires --numbers')
    return config


def word_pattern(term):
    """Whole-word match for word-like terms; plain substring otherwise."""
    escaped = re.escape(term)
    if re.match(r'\w', term):
        escaped = r'(?<!\w)' + escaped
    if re.search(r'\w$', term):
        escaped += r'(?!\w)'
    return escaped


def lint(doc, config, numbers=None):
    visible = mask(doc)
    numeric = mask(doc, inline=True)
    findings = []

    def add(rule, severity, start, message):
        line = doc.count('\n', 0, start) + 1
        line_start = doc.rfind('\n', 0, start) + 1
        end = doc.find('\n', start)
        findings.append({'rule': rule, 'severity': severity, 'line': line,
                         'col': start - line_start + 1,
                         'excerpt': doc[start:end if end >= 0 else len(doc)][:160],
                         'message': message})
    for key, rule in (('person_names', 'person-name'), ('forbid_first_person', 'first-person')):
        for term in config[key]:
            flags = re.I if len(term) > 1 else 0
            for m in re.finditer(word_pattern(term), visible, flags):
                add(rule, 'error', m.start(), 'forbidden: ' + term)
    for item in config['banned_terms']:
        for m in re.finditer(word_pattern(item['term']), visible, re.I):
            add('banned-term', 'error', m.start(), item['reason'])
    for pattern in config['placeholder_patterns']:
        for m in re.finditer(pattern, visible):
            if m.end() > m.start():
                add('placeholder', 'error', m.start(), 'replace unresolved placeholder')
    headings = set()
    for m in re.finditer(r'^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$', visible, re.M):
        title = m[2].strip()
        if len(m[1]) > config['max_heading_depth']:
            add('heading-depth', 'warning', m.start(), 'heading exceeds configured depth')
        if title in headings:
            add('duplicate-heading', 'warning', m.start(), 'duplicate heading: ' + title)
        headings.add(title)
    prose = list(visible)
    for pattern in (r'\]\((?:[^()\n]|\([^()\n]*\))*\)', r'(`+)([^\n]*?)\1'):
        for m in re.finditer(pattern, doc):
            start = m.start() + 1 if pattern.startswith(r'\]') else m.start()
            for i in range(start, m.end()):
                prose[i] = '\0'
    prose = ''.join(prose)
    # A sentence ends at . ! ? followed by whitespace/end, or at a newline; decimals stay whole.
    for m in re.finditer(r'[^\n]+?(?:[.!?](?=\s|\0|$)|(?=\n)|$)', prose):
        sentence = m[0].strip(' \t\r\0')
        if not sentence:
            continue
        leading = len(m[0]) - len(m[0].lstrip(' \t\r\0'))
        start = m.start() + leading
        measured = re.sub(r'\[([^\]\n]+)\]', r'\1', sentence.replace('\0', ''))
        if len(measured.strip()) > config['max_sentence_chars']:
            add('sentence-length', 'warning', start, 'split long sentence')
            findings[-1]['excerpt'] = doc[start:start + len(sentence)][:160]
        if sentence.count(',') + sentence.count(';') > config['max_enumeration_marks']:
            add('enumeration', 'warning', start, 'reduce enumeration marks')
            findings[-1]['excerpt'] = doc[start:start + len(sentence)][:160]
    starts = [0] + [m.start() for m in re.finditer(r'^##\s+', visible, re.M)] + [len(doc)]
    for start, end in zip(starts, starts[1:]):
        section = visible[start:end]
        for name in config['required_links']:
            first = section.find(name)
            if first < 0:
                continue
            absolute = start + first
            linked = any(m[1] == name and m.start() + 1 == absolute for m in LINK.finditer(doc, start, end))
            if not linked:
                add('required-link', 'warning', absolute, 'link first mention of ' + name + ' in this section')
    if config['number_check']:
        whitelist = set()
        for row in numbers.splitlines():
            if row.lstrip().startswith('|'):
                whitelist.add(normalize(row.split('|')[1]))
        numeric = halfwidth(numeric)
        month, day = r'(?:0?[1-9]|1[0-2])', r'(?:0?[1-9]|[12]\d|3[01])'
        gap = SPACE + '*'
        dates = [r'(?<![A-Za-z0-9])\d{4}[-/]' + month + r'(?:[-/]' + day + r')?(?![\d/-])']
        ranges = [(m.start(), m.end()) for m in re.finditer(
            r'\d+(?:\.\d+)?' + gap + r'[-/]' + gap + r'\d+(?:\.\d+)?' + gap + UNITS, numeric)]
        exemptions = [r'(?<![A-Za-z0-9])(?:\d{4}|\d{2})[HQ][1-4](?![A-Za-z0-9])',
                      r'(?<![A-Za-z0-9])(?:H|Q)[1-4](?![A-Za-z0-9])',
                      r'\bFY\d{2,4}\b',
                      r'(?<![A-Za-z0-9])(?:19|20)\d{2}(?![\d.,]|' + gap + UNITS + r')',
                      r'(?<!\d)\d+(?:\.\d+){2,}(?!\d)',
                      r'\b(?:Figure|Fig\.|Table|Section|Sec\.|Appendix|Step|Phase|Milestone|M|R|P)\s*\d+(?:\.\d+)*',
                      r'§\s*\d+(?:\.\d+)*', r'^\s*\d+(?:\.\d+)*[.)]\s*',
                      r'^\s*#{1,6}\s+\d+(?:\.\d+)*(?:\s|$)']
        exempt_spans = [(m.start(), m.end()) for p in exemptions for m in re.finditer(p, numeric, re.M | re.I)]
        exempt_spans += [(m.start(), m.end()) for p in dates for m in re.finditer(p, numeric)
                         if not re.match(gap + UNITS, numeric[m.end():])]
        for m in QUANTITY.finditer(numeric):
            if (not any(a < m.end() and m.start() < b for a, b in ranges)
                    and any(a <= m.start() and m.end() <= b for a, b in exempt_spans)):
                continue
            if normalize(m[0]) not in whitelist:
                add('unlisted-number', 'warning', m.start(), 'quantity absent from NUMBERS: ' + m[0].strip())
    return sorted(findings, key=lambda f: (f['line'], f['col'], f['rule'], f['message']))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--doc', required=True, type=Path)
    parser.add_argument('--config', required=True, type=Path)
    parser.add_argument('--numbers', type=Path)
    parser.add_argument('--format', choices=('json', 'text'), default='json')
    args = parser.parse_args()
    try:
        config = load_config(args.config, args.numbers)
        findings = lint(args.doc.read_text(encoding='utf-8'), config,
                        args.numbers.read_text(encoding='utf-8') if args.numbers else None)
    except (OSError, UnicodeError, ValueError, re.error) as error:
        parser.error('invalid input/config: ' + (str(error) if not isinstance(error, OSError) else 'cannot read input'))
    errors = sum(f['severity'] == 'error' for f in findings)
    if args.format == 'json':
        print(json.dumps({'doc': str(args.doc), 'errors': errors,
                          'warnings': len(findings) - errors, 'findings': findings}, ensure_ascii=False))
    else:
        for f in findings:
            print(f"{args.doc}:{f['line']}:{f['col']}: {f['severity']} {f['rule']}: {f['message']}")
    return int(bool(errors))


if __name__ == '__main__':
    raise SystemExit(main())
