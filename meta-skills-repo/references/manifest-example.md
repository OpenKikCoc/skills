# Manifest example

`skills-manifest.json` is optional. Every field has a default.

```json
{
  "skills_dir": "skills",
  "targets": [
    {"id": "claude", "path": "~/.claude/skills"},
    {"id": "codex", "path": "~/.codex/skills"},
    {"id": "gemini", "path": "~/.gemini/skills"},
    {"id": "other-agent", "path": "~/.other-agent/skills", "create": true}
  ],
  "ignore": ["*.log", "node_modules"],
  "deny_list": ".sensitive-terms.txt"
}
```

- `skills_dir`: relative to the repository root. Default: `skills` if it
  exists, otherwise the root. A skill is a direct child directory that
  contains `SKILL.md`; names starting with `.` or `_` are skipped.
- `targets`: mirror roots. `~/` expands against `--home`, which makes tests
  and dry runs hermetic. A missing root is skipped unless `create` is true.
- `ignore`: extra file-name globs excluded from sync and validation, on top
  of `.git`, `__pycache__`, `.DS_Store`, `*.pyc`.
- `deny_list`: path (relative to the repo) of the sensitive-term file.

Suggested `.gitignore` entries: `.sensitive-terms.txt`, `__pycache__/`.

## Per-agent notes

The tool copies whole skill directories, which Claude Code, Codex, and Gemini
all load as `<root>/<name>/SKILL.md`. Agent-specific metadata files (for
example UI manifests) are ordinary files and mirror like any other; the
public repository does not need them.
