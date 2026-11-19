#!/usr/bin/env python3
"""Validate control identities and decode pane data before any state is emitted."""

import base64
import binascii
import fnmatch
import re
import sys
from pathlib import Path


def unpack(capture, directory, selectors=""):
    records = []
    seen = set()
    current = None
    patterns = selectors.split(",") if selectors else []
    for line in Path(capture).read_text(encoding="utf-8").splitlines():
        if line.startswith("@@SESSION ") or line.startswith("@@GONE "):
            if current is not None:
                raise ValueError
            fields = line.split(" ")
            gone = fields[0] == "@@GONE"
            if len(fields) != (2 if gone else 3):
                raise ValueError
            name = fields[1]
            if not re.fullmatch(r"[A-Za-z0-9_.:-]+", name) or name in seen:
                raise ValueError
            if patterns and (name not in patterns if gone else not any(fnmatch.fnmatchcase(name, p) for p in patterns)):
                raise ValueError
            seen.add(name)
            if gone:
                records.append((name, "-", "", True))
            else:
                if not re.fullmatch(r"[A-Za-z0-9_.:-]+", fields[2]):
                    raise ValueError
                current = [name, fields[2], [], False]
        elif line == "@@END":
            if current is None or (patterns and not current[3]):
                raise ValueError
            records.append((current[0], current[1], "\n".join(current[2]), False))
            current = None
        elif line.startswith("@@DATA "):
            if current is None or current[2] or current[3]:
                raise ValueError
            payload = line[7:]
            decoded = base64.b64decode(payload, validate=True)
            if base64.b64encode(decoded).decode() != payload:
                raise ValueError
            current[2] = [decoded.decode("utf-8")]
            current[3] = True
        elif line.startswith("@@") or current is None or patterns or current[3]:
            raise ValueError
        else:
            current[2].append(line)
    if current is not None or any(p not in seen for p in patterns if "*" not in p and "?" not in p):
        raise ValueError
    for index, (name, command, pane, gone) in enumerate(records, 1):
        prefix = Path(directory) / str(index)
        Path(str(prefix) + ".name").write_text(name)
        Path(str(prefix) + ".cmd").write_text(command)
        Path(str(prefix) + ".pane").write_text(pane)
        if gone:
            Path(str(prefix) + ".gone").touch()


if __name__ == "__main__":
    try:
        unpack(*sys.argv[1:])
    except (ValueError, OSError, UnicodeError, binascii.Error):
        print("error: invalid capture records; probe_error; remote work is unchanged", file=sys.stderr)
        raise SystemExit(3)
