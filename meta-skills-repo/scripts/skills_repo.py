#!/usr/bin/env python3
"""Validate, test, mirror, and gate a canonical skills repository.

The repository checkout is the source of truth; agent skill directories
(for example ~/.claude/skills, ~/.codex/skills, ~/.gemini/skills) are mirrors.

Actions: targets, validate, test, sync [--dry-run], verify, gate, reproduce.
Standard library only (Python >= 3.9).
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
LINK_RE = re.compile(r"!?\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
MARKER = ".skills-repo-managed.json"
DEFAULT_IGNORES = [".git", "__pycache__", ".DS_Store", "*.pyc", "*.pyo", MARKER]
DEFAULT_TARGETS = [
    {"id": "claude", "path": "~/.claude/skills"},
    {"id": "codex", "path": "~/.codex/skills"},
    {"id": "gemini", "path": "~/.gemini/skills"},
]
HOME_PATH_RE = re.compile(r"(?<![\w.])(?:/Users|/home)/(?!(?:you|user|username|me|example|name)/)[A-Za-z0-9._-]+/")
# placeholder owners (/Users/you/, /home/user/, ...) are documentation, not leaks
PRIVATE_KEY_RE = re.compile(r"-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----")
SECRET_ASSIGN_RE = re.compile(
    r"""(?ix)\b(?:password|passwd|token|secret|api[_-]?key)\b\s*[:=]\s*["'][^"'${}<\s]{12,}["']"""
)
DEFAULT_DENY_LIST = ".sensitive-terms.txt"


class RepoError(RuntimeError):
    pass


# ------------------------------------------------------------------ config

class Config:
    def __init__(self, repo: Path, home: Path, manifest: Dict[str, Any]) -> None:
        self.repo = repo
        self.home = home
        self.manifest = manifest
        skills_dir = manifest.get("skills_dir")
        if skills_dir is None:
            skills_dir = "skills" if (repo / "skills").is_dir() else "."
        self.skills_root = (repo / skills_dir).resolve()
        self.ignores = DEFAULT_IGNORES + list(manifest.get("ignore") or [])
        self.deny_list = manifest.get("deny_list", DEFAULT_DENY_LIST)

    def is_ignored(self, rel: Path) -> bool:
        return any(fnmatch.fnmatch(part, pat) for part in rel.parts for pat in self.ignores)

    def skills(self, only: Optional[Sequence[str]] = None) -> List[Path]:
        if not self.skills_root.is_dir():
            raise RepoError("skills directory not found: %s" % self.skills_root)
        found = sorted(
            p for p in self.skills_root.iterdir()
            if p.is_dir() and not p.name.startswith((".", "_")) and (p / "SKILL.md").exists()
        )
        if only:
            unknown = sorted(set(only) - {p.name for p in found})
            if unknown:
                raise RepoError("unknown skills: %s" % ", ".join(unknown))
            found = [p for p in found if p.name in only]
        return found

    def targets(self, selected: Optional[Sequence[str]] = None) -> List[Dict[str, Any]]:
        raw = self.manifest.get("targets") or DEFAULT_TARGETS
        out = []
        for t in raw:
            if not isinstance(t, dict) or "id" not in t or "path" not in t:
                raise RepoError("each target needs id and path")
            path = str(t["path"])
            if path.startswith("~/"):
                resolved = self.home / path[2:]
            else:
                resolved = Path(path).expanduser()
            out.append({"id": t["id"], "path": resolved, "create": bool(t.get("create"))})
        if selected:
            unknown = sorted(set(selected) - {t["id"] for t in out})
            if unknown:
                raise RepoError("unknown targets: %s" % ", ".join(unknown))
            out = [t for t in out if t["id"] in selected]
        return out


def load_config(args: argparse.Namespace) -> Config:
    repo = Path(args.repo).expanduser().resolve()
    manifest_path = Path(args.manifest).expanduser() if args.manifest else repo / "skills-manifest.json"
    manifest: Dict[str, Any] = {}
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise RepoError("cannot read manifest: %s" % error)
        if not isinstance(manifest, dict):
            raise RepoError("manifest root must be an object")
    elif args.manifest:
        raise RepoError("manifest not found: %s" % manifest_path)
    return Config(repo, Path(args.home).expanduser(), manifest)


# ---------------------------------------------------------------- validate

def parse_frontmatter(text: str) -> Tuple[Dict[str, str], Optional[str]]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, "missing opening front matter delimiter"
    end = next((i for i, ln in enumerate(lines[1:], 1) if ln.strip() == "---"), None)
    if end is None:
        return {}, "missing closing front matter delimiter"
    meta: Dict[str, str] = {}
    for line in lines[1:end]:
        if not line.strip() or line.startswith((" ", "\t", "#")):
            continue
        if ":" not in line:
            return {}, "malformed front matter line: %r" % line[:40]
        key, value = line.split(":", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            if value[0] == '"':
                try:
                    value = json.loads(value)
                except ValueError:
                    value = value[1:-1]
            else:
                value = value[1:-1]
        meta[key.strip()] = value
    return meta, None


def check_links(skill: Path, md: Path, errors: List[str]) -> None:
    text = md.read_text(encoding="utf-8", errors="replace")
    text = re.sub(r"```.*?```", "", text, flags=re.S)  # ignore fenced examples
    for target in LINK_RE.findall(text):
        if re.match(r"^(?:[a-z][a-z0-9+.-]*:|#|<)", target, re.I):
            continue
        rel = target.split("#", 1)[0]
        if not rel:
            continue
        dest = (md.parent / rel).resolve()
        if not dest.exists():
            errors.append("%s: broken link: %s" % (md.relative_to(skill.parent), target))
        elif skill.resolve() not in dest.parents and dest != skill.resolve():
            errors.append("%s: link escapes skill directory: %s" % (md.relative_to(skill.parent), target))


def validate_skill(cfg: Config, skill: Path) -> List[str]:
    errors: List[str] = []
    label = skill.name
    if not NAME_RE.match(skill.name):
        errors.append("%s: directory name must be lowercase-hyphen" % label)
    meta, problem = parse_frontmatter((skill / "SKILL.md").read_text(encoding="utf-8", errors="replace"))
    if problem:
        errors.append("%s/SKILL.md: %s" % (label, problem))
    else:
        if meta.get("name") != skill.name:
            errors.append("%s/SKILL.md: name %r must equal directory name" % (label, meta.get("name")))
        description = meta.get("description", "")
        if not description.strip():
            errors.append("%s/SKILL.md: empty description" % label)
        elif len(description) > 1024:
            errors.append("%s/SKILL.md: description over 1024 characters" % label)
    for md in sorted(skill.rglob("*.md")):
        if not cfg.is_ignored(md.relative_to(skill)):
            check_links(skill, md, errors)
    for path in sorted(skill.rglob("*")):
        rel = path.relative_to(skill)
        if path.name.startswith("._") and not cfg.is_ignored(rel):
            errors.append("%s/%s: stray AppleDouble file" % (label, rel))
        if path.is_symlink():
            errors.append("%s/%s: symlinks are not allowed in skills" % (label, rel))
    scripts = skill / "scripts"
    if scripts.is_dir():
        sources = [p for p in scripts.glob("*.py") if not p.name.startswith("test_")]
        tests = list(scripts.glob("test_*.py"))
        if sources and not tests:
            errors.append("%s: scripts/ has Python sources but no test_*.py" % label)
    return errors


def action_validate(cfg: Config, skills: Sequence[Path]) -> int:
    errors: List[str] = []
    seen = set()
    for skill in skills:
        if skill.name in seen:
            errors.append("duplicate skill name: %s" % skill.name)
        seen.add(skill.name)
        errors.extend(validate_skill(cfg, skill))
    for error in errors:
        print("error: %s" % error)
    print("validate: %d skills, %d errors" % (len(skills), len(errors)))
    return 1 if errors else 0


# -------------------------------------------------------------------- test

def action_test(cfg: Config, skills: Sequence[Path]) -> int:
    failures = 0
    ran = 0
    for skill in skills:
        for test in sorted((skill / "scripts").glob("test_*.py")) if (skill / "scripts").is_dir() else []:
            ran += 1
            env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
            proc = subprocess.run([sys.executable, "-I", str(test.relative_to(skill))], cwd=str(skill),
                                  capture_output=True, text=True, env=env)
            status = "ok" if proc.returncode == 0 else "FAIL"
            print("test %s/%s: %s" % (skill.name, test.name, status))
            if proc.returncode != 0:
                failures += 1
                print((proc.stdout + proc.stderr)[-1500:])
    print("test: %d files, %d failures" % (ran, failures))
    return 1 if failures else 0


# --------------------------------------------------------------- sync/verify

def file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_files(cfg: Config, skill: Path) -> Dict[str, Tuple[str, bool]]:
    files = {}
    for path in sorted(skill.rglob("*")):
        rel = path.relative_to(skill)
        if path.is_file() and not path.is_symlink() and not cfg.is_ignored(rel):
            files[rel.as_posix()] = (file_digest(path), bool(path.stat().st_mode & 0o111))
    return files


def read_marker(dest: Path) -> Dict[str, str]:
    try:
        data = json.loads((dest / MARKER).read_text(encoding="utf-8"))
        return {k: v for k, v in data.get("files", {}).items() if isinstance(v, str)}
    except (OSError, ValueError, AttributeError):
        return {}


def plan_skill(cfg: Config, skill: Path, dest: Path) -> Dict[str, Any]:
    """Compute copy/remove/conflict lists for one skill/target pair."""
    src = source_files(cfg, skill)
    managed = read_marker(dest)
    plan: Dict[str, Any] = {"copy": [], "remove": [], "conflicts": [], "unknown_kept": []}
    if dest.is_symlink():
        plan["conflicts"].append("%s is a symlink; refusing to write through it" % dest.name)
        return plan
    for rel, (digest, executable) in src.items():
        target = dest / rel
        if not target.exists():
            plan["copy"].append(rel)
            continue
        same = target.is_file() and file_digest(target) == digest and bool(target.stat().st_mode & 0o111) == executable
        if same:
            continue
        if rel in managed or (target.is_file() and file_digest(target) == digest):
            plan["copy"].append(rel)
        else:
            plan["conflicts"].append("%s: exists and was not written by this tool" % rel)
    for rel in sorted(managed):
        if rel not in src and (dest / rel).exists():
            plan["remove"].append(rel)
    if dest.is_dir():
        known = set(src) | set(managed)
        for path in sorted(dest.rglob("*")):
            rel = path.relative_to(dest).as_posix()
            if path.is_file() and rel not in known and path.name != MARKER:
                plan["unknown_kept"].append(rel)
    return plan


def apply_skill(cfg: Config, skill: Path, dest: Path, plan: Dict[str, Any]) -> None:
    src = source_files(cfg, skill)
    for rel in plan["copy"]:
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(skill / rel, target)
        mode = (skill / rel).stat().st_mode & 0o777
        target.chmod(mode)
    for rel in plan["remove"]:
        (dest / rel).unlink()
        parent = (dest / rel).parent
        while parent != dest and parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()
            parent = parent.parent
    dest.mkdir(parents=True, exist_ok=True)
    (dest / MARKER).write_text(
        json.dumps({"files": {rel: d for rel, (d, _) in src.items()}}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8")


def action_sync(cfg: Config, skills: Sequence[Path], targets: Sequence[Dict[str, Any]], dry_run: bool, force: bool) -> int:
    exit_code = 0
    for target in targets:
        root: Path = target["path"]
        if not root.is_dir():
            if target["create"] and not dry_run:
                root.mkdir(parents=True)
            else:
                print("skip target %s: %s does not exist" % (target["id"], "directory" if not target["create"] else "(dry-run)"))
                continue
        for skill in skills:
            dest = root / skill.name
            plan = plan_skill(cfg, skill, dest)
            if plan["conflicts"] and force and not dest.is_symlink():
                plan["copy"] += [c.split(":", 1)[0] for c in plan["conflicts"] if ":" in c]
                plan["conflicts"] = []
            for conflict in plan["conflicts"]:
                print("conflict %s/%s: %s" % (target["id"], skill.name, conflict))
                exit_code = 1
            for rel in plan["unknown_kept"]:
                print("kept unknown file %s/%s/%s" % (target["id"], skill.name, rel))
            if plan["conflicts"]:
                continue
            changed = bool(plan["copy"] or plan["remove"] or not (dest / MARKER).exists())
            verb = "would sync" if dry_run else "sync"
            if changed:
                print("%s %s/%s: %d copy, %d remove" % (verb, target["id"], skill.name, len(plan["copy"]), len(plan["remove"])))
                if not dry_run:
                    apply_skill(cfg, skill, dest, plan)
    return exit_code


def action_verify(cfg: Config, skills: Sequence[Path], targets: Sequence[Dict[str, Any]]) -> int:
    problems = 0
    for target in targets:
        root: Path = target["path"]
        if not root.is_dir():
            print("skip target %s: directory missing" % target["id"])
            continue
        for skill in skills:
            dest = root / skill.name
            if dest.is_symlink() or not dest.is_dir():
                print("missing %s/%s" % (target["id"], skill.name))
                problems += 1
                continue
            plan = plan_skill(cfg, skill, dest)
            for rel in plan["copy"] + [c.split(":")[0] for c in plan["conflicts"]]:
                print("differs %s/%s/%s" % (target["id"], skill.name, rel))
                problems += 1
            for rel in plan["remove"]:
                print("stale %s/%s/%s" % (target["id"], skill.name, rel))
                problems += 1
    print("verify: %d problems" % problems)
    return 1 if problems else 0


# -------------------------------------------------------------------- gate

def load_deny_terms(path: Path) -> List[Tuple[str, re.Pattern]]:
    terms = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            pattern = re.compile(line[3:] if line.startswith("re:") else re.escape(line), re.I)
        except re.error as error:
            raise RepoError("invalid deny-list pattern on line %d: %s" % (len(terms) + 1, error))
        terms.append((line, pattern))
    return terms


def repo_text_files(cfg: Config) -> List[Path]:
    files = []
    for path in sorted(cfg.repo.rglob("*")):
        rel = path.relative_to(cfg.repo)
        if ".git" in rel.parts or not path.is_file() or path.is_symlink():
            continue
        files.append(path)
    return files


def scan_sensitive(cfg: Config, deny_path: Optional[Path], history: bool) -> Tuple[List[str], List[str]]:
    errors: List[str] = []
    notes: List[str] = []
    terms: List[Tuple[str, re.Pattern]] = []
    if deny_path and deny_path.is_file():
        terms = load_deny_terms(deny_path)
        notes.append("deny-list: %d terms from %s" % (len(terms), deny_path.name))
    else:
        notes.append("deny-list: none configured (copy references/sensitive-terms.example.txt to enable)")
    try:
        deny_rel = deny_path.resolve().relative_to(cfg.repo) if deny_path else None
    except ValueError:
        deny_rel = None
    for path in repo_text_files(cfg):
        rel = path.relative_to(cfg.repo)
        if deny_rel is not None and rel == deny_rel:
            continue  # the list itself is expected to hold the terms; keep it out of the public tree
        for idx, (_, pattern) in enumerate(terms, 1):
            if pattern.search(rel.as_posix()):
                errors.append("%s: path matches deny-list entry #%d" % (rel, idx))
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            for idx, (_, pattern) in enumerate(terms, 1):
                if pattern.search(line):
                    errors.append("%s:%d: matches deny-list entry #%d" % (rel, lineno, idx))
            if HOME_PATH_RE.search(line):
                errors.append("%s:%d: absolute home-directory path" % (rel, lineno))
            if PRIVATE_KEY_RE.search(line):
                errors.append("%s:%d: private key block" % (rel, lineno))
            if SECRET_ASSIGN_RE.search(line):
                errors.append("%s:%d: probable literal secret assignment" % (rel, lineno))
    if deny_rel is not None:
        tracked = subprocess.run(["git", "-C", str(cfg.repo), "ls-files", "--error-unmatch", str(deny_rel)],
                                 capture_output=True) if (cfg.repo / ".git").exists() else None
        if tracked is not None and tracked.returncode == 0:
            errors.append("%s: deny-list file is tracked by git; ignore or untrack it" % deny_rel)
    if history and terms and (cfg.repo / ".git").exists():
        log = subprocess.run(["git", "-C", str(cfg.repo), "log", "--all", "--format=%an%n%ae%n%cn%n%ce%n%B"],
                             capture_output=True, text=True)
        for idx, (_, pattern) in enumerate(terms, 1):
            if pattern.search(log.stdout):
                errors.append("git history: author/message matches deny-list entry #%d" % idx)
    return errors, notes


def action_gate(cfg: Config, skills: Sequence[Path], deny_arg: Optional[str], require_deny: bool, history: bool) -> int:
    code = action_validate(cfg, skills)
    code |= action_test(cfg, skills)
    deny_path = Path(deny_arg).expanduser() if deny_arg else cfg.repo / cfg.deny_list
    if require_deny and not deny_path.is_file():
        print("error: --require-deny-list set but %s not found" % deny_path.name)
        return 1
    errors, notes = scan_sensitive(cfg, deny_path, history)
    for note in notes:
        print(note)
    for error in errors:
        print("error: %s" % error)
    print("gate: %d sensitive findings" % len(errors))
    return 1 if (errors or code) else 0


# -------------------------------------------------------------------- main

def make_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--repo", default=".", help="repository root (default: current directory)")
    p.add_argument("--manifest", help="manifest path (default: <repo>/skills-manifest.json, optional)")
    p.add_argument("--home", default=str(Path.home()), help="HOME used to expand target paths")
    p.add_argument("--target", action="append", help="limit to a target id (repeatable)")
    p.add_argument("--skill", action="append", help="limit to a skill name (repeatable)")
    sub = p.add_subparsers(dest="action", required=True)
    sub.add_parser("targets", help="list resolved mirror targets")
    sub.add_parser("validate", help="static checks: front matter, name==dir, links, tests present")
    sub.add_parser("test", help="run each skill's scripts/test_*.py")
    s = sub.add_parser("sync", help="mirror skills into targets without clobbering unknown files")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--force", action="store_true", help="overwrite pre-existing unmanaged files that differ")
    sub.add_parser("verify", help="check mirrors match the repository (content and exec bit)")
    g = sub.add_parser("gate", help="publish gate: validate + test + sensitive-term scan")
    g.add_argument("--deny-list", help="deny-list file (default: <repo>/.sensitive-terms.txt)")
    g.add_argument("--require-deny-list", action="store_true", help="fail when no deny-list file exists")
    g.add_argument("--history", action="store_true", help="also scan git author names and commit messages")
    sub.add_parser("reproduce", help="validate, test, sync, verify")
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = make_parser().parse_args(argv)
    try:
        cfg = load_config(args)
        skills = cfg.skills(args.skill)
        if args.action == "targets":
            for t in cfg.targets(args.target):
                print("%s\t%s\t%s" % (t["id"], t["path"], "exists" if t["path"].is_dir() else "missing"))
            return 0
        if args.action == "validate":
            return action_validate(cfg, skills)
        if args.action == "test":
            return action_test(cfg, skills)
        if args.action == "sync":
            return action_sync(cfg, skills, cfg.targets(args.target), args.dry_run, args.force)
        if args.action == "verify":
            return action_verify(cfg, skills, cfg.targets(args.target))
        if args.action == "gate":
            return action_gate(cfg, skills, args.deny_list, args.require_deny_list, args.history)
        if args.action == "reproduce":
            code = action_validate(cfg, skills)
            if code:
                return code
            code = action_test(cfg, skills)
            if code:
                return code
            code = action_sync(cfg, skills, cfg.targets(args.target), False, False)
            return code or action_verify(cfg, skills, cfg.targets(args.target))
    except RepoError as error:
        print("error: %s" % error, file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    sys.exit(main())
