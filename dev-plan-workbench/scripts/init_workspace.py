#!/usr/bin/env python3
"""Create durable planning state without overwriting. Exit 0 success, 2 usage/I/O, 3 conflict."""
import argparse
import json
import os
from pathlib import Path


FILES = ('CONTRACT.md', 'FEEDBACK.md', 'RULES.md', 'TRIAGE.md', 'HANDOFF.md',
         'NUMBERS.md', 'SOURCES.md', 'CONCEPTS.md', 'lint.json')
DIRS = ('reviews', 'versions', 'publish')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dir', required=True, type=Path)
    parser.add_argument('--title', default='<doc title>')
    parser.add_argument('--missing-only', action='store_true')
    args = parser.parse_args()
    root = args.dir
    targets = [root / name for name in FILES]
    conflicts = [str(p) for p in targets if os.path.lexists(p)]
    try:
        if root.is_symlink() or any((root / n).is_symlink() for n in DIRS):
            parser.error('workspace and state directories must not be symlinks')
        if conflicts and not args.missing_only:
            print(json.dumps({'conflicts': conflicts}, ensure_ascii=False))
            return 3
        for name in DIRS:
            path = root / name
            if path.exists() and not path.is_dir():
                parser.error('state directory is not a directory: ' + name)
        template_root = Path(__file__).resolve().parent.parent / 'assets' / 'templates'
        contents = {n: (template_root / n).read_text(encoding='utf-8') for n in FILES}
        root.mkdir(parents=True, exist_ok=True)
        created, skipped = [], []
        for name in FILES:
            path = root / name
            if os.path.lexists(path) and args.missing_only:
                skipped.append(name)
                continue
            try:
                with path.open('x', encoding='utf-8') as stream:
                    stream.write(contents[name].replace('{{TITLE}}', args.title.replace('\n', ' ')))
            except FileExistsError:
                print(json.dumps({'conflicts': [str(path)]}))
                return 3
            created.append(name)
        for name in DIRS:
            path = root / name
            if path.exists():
                skipped.append(name + '/')
            else:
                path.mkdir()
                created.append(name + '/')
        print(json.dumps({'created': created, 'skipped': skipped}, ensure_ascii=False))
        return 0
    except OSError:
        parser.error('cannot read templates or create workspace')


if __name__ == '__main__':
    raise SystemExit(main())
