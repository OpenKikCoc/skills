#!/usr/bin/env python3
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
import platform
from pathlib import Path
import secrets
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
from typing import Callable, Iterable, Iterator


ASSETS = Path(__file__).resolve().parents[1] / "assets"
TOOLS = (
    "activate_project", "get_current_config", "get_symbols_overview",
    "find_symbol", "find_referencing_symbols", "find_implementations",
    "find_declaration", "restart_language_server",
)
DEFAULT_EXCLUDED_PARTS = (("tmp",), (".tmp",), (".cache",), ("__pycache__",))
PRUNED_DIRECTORIES = {".git": "Git metadata", "vendor": "dependency directory",
                      "node_modules": "dependency directory"}
SOURCE_SUFFIXES = (
    ".go", ".py", ".rs", ".java", ".kt", ".kts", ".c", ".cc",
    ".cpp", ".h", ".hpp", ".ts", ".tsx", ".js", ".jsx", ".rb",
    ".yaml", ".yml", ".md", ".sh", ".bash",
)
LANGUAGE_MARKERS = (
    ("go", ("go.mod",), (".go",)),
    ("rust", ("Cargo.toml",), (".rs",)),
    ("python", ("pyproject.toml", "setup.py", "requirements.txt"), (".py",)),
    ("typescript", ("package.json", "tsconfig.json"), (".ts", ".tsx", ".js", ".jsx")),
    ("yaml", (), (".yaml", ".yml")),
    ("markdown", (), (".md",)),
    ("bash", (), (".sh", ".bash")),
)
EXTERNAL_EXECUTABLES = {
    "go": (("go",), ("gopls",)),
    "python": (("uvx", "uv"),),
    "typescript": (("node",), ("npm",)),
    "yaml": (("node",), ("npm",)),
    "bash": (("node",), ("npm",)),
}
MANAGED_RESOURCE_LANGUAGES = frozenset({"rust", "python", "typescript", "yaml", "markdown", "bash"})
RUNTIME_RESOURCE_SCHEMA = 1
PROJECT_SEED_SCHEMA = 1
SESSION_LEASE_SCHEMA = 1
SESSION_OWNER = "dev-code-navigation"
PROJECT_SELECTION_FILE = "launcher-project.json"


def project_root(cwd: Path) -> Path | None:
    result = subprocess.run(
        ["git", "-C", str(cwd), "rev-parse", "--show-toplevel"],
        capture_output=True, text=True, timeout=10, check=False,
    )
    if result.returncode:
        return None
    root = Path(result.stdout.strip()).resolve()
    if root in (Path.home().resolve(), Path(root.anchor)):
        return None
    return root


def cache_folder(state_root: Path, project: Path) -> Path:
    return state_root / "projects" / str(project.resolve()).lstrip(os.sep) / ".serena"


def workspace_roots() -> list[Path]:
    configured = os.environ.get("SERENA_CODE_ROOTS")
    values = configured.split(os.pathsep) if configured else [str(Path.home() / "src")]
    return sorted({Path(value).expanduser().resolve() for value in values if value.strip()})


def _contains_parts(path: Path, parts: tuple[str, ...]) -> bool:
    values = path.parts
    size = len(parts)
    return any(values[index:index + size] == parts for index in range(len(values) - size + 1))


def configured_excluded_parts() -> tuple[tuple[str, ...], ...]:
    """Parse SERENA_DISCOVERY_EXCLUDES: a JSON list of relative directory paths."""
    try:
        values = json.loads(os.environ.get("SERENA_DISCOVERY_EXCLUDES", "[]"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"SERENA_DISCOVERY_EXCLUDES is not valid JSON: {exc.msg}") from None
    if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
        raise ValueError("SERENA_DISCOVERY_EXCLUDES must be a JSON list of relative directory paths")
    parts = []
    for value in values:
        path = Path(value)
        if not value or path.is_absolute() or any(part in {"", ".", ".."} for part in value.split("/")):
            raise ValueError("SERENA_DISCOVERY_EXCLUDES entries must be non-empty relative directory paths")
        parts.append(path.parts)
    return tuple(parts)


def discover_projects(roots: Iterable[Path], excluded_report: list[dict] | None = None) -> list[Path]:
    """Find Git repositories below explicit roots.

    Exclusions apply to paths *below* a root (matched on the root-relative path), so
    an explicitly configured root is always scanned even if its own path contains an
    excluded name. Every exclusion is appended to ``excluded_report`` when given.
    """
    projects: set[Path] = set()
    exclusions = [(parts, "temporary directory") for parts in DEFAULT_EXCLUDED_PARTS]
    exclusions.extend((parts, "configured SERENA_DISCOVERY_EXCLUDES") for parts in configured_excluded_parts())

    def record(path: Path, pattern: str, reason: str) -> None:
        if excluded_report is not None:
            excluded_report.append({"path": str(path), "pattern": pattern, "reason": reason})

    for root in roots:
        if not root.is_dir():
            continue
        if (root / ".git").exists():
            projects.add(root.resolve())
            # An explicitly configured repository root is terminal. Walking below
            # it would discover generated or nested repositories that the caller
            # did not independently place in SERENA_CODE_ROOTS.
            continue
        for current, directories, files in os.walk(root):
            path = Path(current)
            matched = next(((parts, reason) for parts, reason in exclusions
                            if _contains_parts(path.relative_to(root), parts)), None)
            if matched:
                record(path, "/".join(matched[0]), matched[1])
                directories[:] = []
                continue
            is_repository = ".git" in directories or ".git" in files
            for name in sorted(directories):
                if name in PRUNED_DIRECTORIES:
                    record(path / name, name, PRUNED_DIRECTORIES[name])
            directories[:] = sorted(name for name in directories if name not in PRUNED_DIRECTORIES)
            if is_repository:
                projects.add(path.resolve())
                # A nested repository is a complete discovery result. Do not scan its
                # internals for generated worktrees or vendored repositories. A workspace
                # root itself remains traversable because umbrella roots such as ~/src can
                # intentionally contain independently activated repositories.
                if path.resolve() != root.resolve():
                    directories[:] = []
    return sorted(projects)


def write_runtime_config(runtime_home: Path, state_root: Path, projects: Iterable[Path]) -> Path:
    config = json.loads((ASSETS / "serena-config.json").read_text())
    config["project_serena_folder_location"] = str(state_root / "projects") + "/$projectDir/.serena"
    config["projects"] = [str(project) for project in projects if (cache_folder(state_root, project) / "project.yml").is_file()]
    path = runtime_home / "serena_config.yml"
    path.write_text(json.dumps(config, indent=2) + "\n")
    return path


def infer_languages(project: Path) -> list[str]:
    result = subprocess.run(
        ["git", "-C", str(project), "ls-files"],
        capture_output=True, text=True, timeout=30, check=False,
    )
    if result.returncode:
        return []
    files = result.stdout.splitlines()
    root_names = {name for name in files if "/" not in name}
    for language, markers, _ in LANGUAGE_MARKERS:
        if root_names.intersection(markers):
            return [language]
    names = {Path(name).name for name in files}
    suffixes = {Path(name).suffix.lower() for name in files}
    return [language for language, markers, extensions in LANGUAGE_MARKERS
            if names.intersection(markers) or suffixes.intersection(extensions)]


def configured_project_profile(project: Path, project_state_root: Path | None = None) -> dict[str, list[str]]:
    if project_state_root is not None:
        config = cache_folder(project_state_root, project) / PROJECT_SELECTION_FILE
        try:
            document = json.loads(config.read_text())
            languages = document.get("language_servers")
            ignored_paths = document.get("ignored_paths", [])
            if (
                isinstance(languages, list)
                and all(isinstance(item, str) for item in languages)
                and isinstance(ignored_paths, list)
                and all(isinstance(item, str) for item in ignored_paths)
            ):
                return {"languages": languages, "ignored_paths": ignored_paths}
        except (OSError, ValueError):
            pass
    return {"languages": infer_languages(project), "ignored_paths": []}


def preflight_project(project: Path, path: str | None = None, project_state_root: Path | None = None) -> dict:
    profile = configured_project_profile(project, project_state_root)
    languages = profile["languages"]
    resolved: dict[str, str] = {}
    missing: list[str] = []
    for language in languages:
        for alternatives in EXTERNAL_EXECUTABLES.get(language, ()):
            executable = next((found for candidate in alternatives if (found := shutil.which(candidate, path=path))), None)
            label = "/".join(alternatives)
            if executable is None:
                missing.append(label)
            else:
                resolved[label] = executable
    return {
        "project": str(project),
        "languages": languages,
        "ignored_paths": profile["ignored_paths"],
        "resolved_executables": resolved,
        "missing_executables": sorted(set(missing)),
        "status": "blocked" if missing else "ready",
    }


def preflight_projects(projects: Iterable[Path], path: str | None = None, project_state_root: Path | None = None) -> list[dict]:
    return [preflight_project(project, path=path, project_state_root=project_state_root) for project in projects]


def _command_prefix(command: str | Iterable[str]) -> list[str]:
    return [command] if isinstance(command, str) else list(command)


def serena_cli_prefix(binary: str) -> list[str]:
    try:
        first_line = Path(binary).read_text(errors="replace").splitlines()[0]
    except (OSError, IndexError):
        raise ValueError(f"cannot inspect Serena executable entrypoint: {binary}")
    if not first_line.startswith("#!"):
        raise ValueError("Serena executable must expose a Python shebang; set SERENA_EXECUTABLE to its console script")
    interpreter = shlex.split(first_line[2:].strip())
    if not interpreter:
        raise ValueError(f"invalid Serena executable shebang: {binary}")
    if Path(interpreter[0]).name == "env":
        if len(interpreter) != 2 or (resolved := shutil.which(interpreter[1])) is None:
            raise ValueError(f"cannot resolve Serena Python interpreter from shebang: {first_line}")
        interpreter = [resolved]
    if not os.path.isfile(interpreter[0]) or not os.access(interpreter[0], os.X_OK):
        raise ValueError(f"Serena Python interpreter is not runnable: {interpreter[0]}")
    return [*interpreter, str(ASSETS / "runtime" / "serena_bootstrap.py")]


def ensure_project_configs(binary: str | Iterable[str], env: dict[str, str], state_root: Path, projects: Iterable[Path]) -> tuple[list[Path], list[dict[str, str]]]:
    prefix = _command_prefix(binary)
    ready: list[Path] = []
    failures: list[dict[str, str]] = []
    for project in projects:
        if (cache_folder(state_root, project) / "project.yml").is_file():
            ready.append(project)
            continue
        local_config = project.resolve() / ".serena" / "project.yml"
        if local_config.exists() or local_config.is_symlink():
            command = [*prefix, "isolation-copy-project-config", str(local_config), str(cache_folder(state_root, project) / "project.yml"), str(project)]
        else:
            languages = infer_languages(project)
            if not languages:
                failures.append({"project": str(project), "error": "no supported tracked source language"})
                continue
            command = [*prefix, "project", "create", str(project)]
            for language in languages:
                command.extend(("--ls", language))
        result = subprocess.run(
            command,
            env=env, capture_output=True, text=True, timeout=120, check=False,
        )
        project_config = cache_folder(state_root, project) / "project.yml"
        if result.returncode == 0 and project_config.is_file():
            selection = project_config.with_name(PROJECT_SELECTION_FILE)
            if not selection.is_file():
                _write_json_atomic(selection, {"language_servers": infer_languages(project), "ignored_paths": []})
            ready.append(project)
        else:
            detail = (result.stderr or result.stdout).strip()
            if result.returncode == 0:
                detail = "Serena did not create the private project configuration"
            failures.append({"project": str(project), "error": detail})
    return ready, failures


def resolve_projects(selectors: list[str], projects: list[Path]) -> list[Path]:
    if not selectors:
        return projects
    selected: list[Path] = []
    for selector in selectors:
        candidate = Path(selector).expanduser()
        if candidate.exists():
            resolved = project_root(candidate)
            matches = [resolved] if resolved is not None else []
        else:
            matches = [project for project in projects if project.name == selector]
        if not matches:
            raise ValueError(f"unknown Serena project: {selector}")
        if len(matches) > 1:
            raise ValueError(f"ambiguous Serena project '{selector}'; use an absolute path")
        if matches[0] not in selected:
            selected.append(matches[0])
    return selected


def filter_ignored_sources(
    binary: str | Iterable[str], env: dict[str, str], paths: list[str], patterns: Iterable[str],
) -> list[str]:
    requested_patterns = list(patterns)
    if not requested_patterns:
        return paths
    result = subprocess.run(
        [*_command_prefix(binary), "isolation-filter-paths"],
        env=env,
        input=json.dumps({"paths": paths, "patterns": requested_patterns}),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode:
        raise ValueError(f"cannot evaluate Serena ignored_paths: {(result.stderr or result.stdout).strip()}")
    try:
        selected = json.loads(result.stdout)["paths"]
    except (KeyError, TypeError, ValueError):
        raise ValueError("Serena ignore matcher returned an invalid response")
    if not isinstance(selected, list) or not all(isinstance(path, str) and path in paths for path in selected):
        raise ValueError("Serena ignore matcher returned paths outside the request")
    return selected


def first_source_file(
    project: Path,
    languages: Iterable[str] | None = None,
    ignored_paths: Iterable[str] = (),
    ignore_filter: Callable[[list[str], Iterable[str]], list[str]] | None = None,
) -> Path | None:
    result = subprocess.run(
        ["git", "-C", str(project), "ls-files"],
        capture_output=True, text=True, timeout=30, check=False,
    )
    if result.returncode:
        return None
    known_languages = {language for language, _, _ in LANGUAGE_MARKERS}
    configured = set(languages) if languages is not None else None
    allowed_suffixes = set(SOURCE_SUFFIXES) if configured is None or not configured.issubset(known_languages) else {
        suffix for language, _, suffixes in LANGUAGE_MARKERS if language in configured for suffix in suffixes
    }
    candidates = [name for name in result.stdout.splitlines() if (project / name).suffix.lower() in allowed_suffixes]
    if ignored_paths:
        if ignore_filter is None:
            raise ValueError("ignored_paths require the Serena gitignore-style filter")
        candidates = ignore_filter(candidates, ignored_paths)
    for name in candidates:
        path = project / name
        if (
            path.is_file()
            and path.stat().st_size > 0
        ):
            return path
    return None


def run_project_command(
    binary: str | Iterable[str],
    env: dict[str, str],
    action: str,
    projects: Iterable[Path],
    project_profiles: dict[str, dict] | None = None,
) -> list[dict[str, str]]:
    prefix = _command_prefix(binary)
    results: list[dict[str, str]] = []
    for project in projects:
        if action == "warm":
            command = [*prefix, "project", "index", str(project), "--log-level", "WARNING"]
        else:
            profile = (project_profiles or {}).get(str(project), {})
            source = first_source_file(
                project,
                profile.get("languages"),
                profile.get("ignored_paths", ()),
                ignore_filter=lambda paths, patterns: filter_ignored_sources(prefix, env, paths, patterns),
            )
            if source is None:
                results.append({"project": str(project), "status": "unsupported", "detail": "no tracked source file"})
                continue
            command = [*prefix, "project", "index-file", str(source), str(project)]
        result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=600, check=False)
        results.append({
            "project": str(project),
            "status": "ok" if result.returncode == 0 else "error",
            "detail": (result.stdout if result.returncode == 0 else result.stderr or result.stdout).strip(),
        })
    return results


def runtime_identity(binary: str | Iterable[str]) -> dict[str, str | int]:
    prefix = _command_prefix(binary)
    result = subprocess.run(
        [*prefix, "--version"], capture_output=True, text=True, timeout=10, check=False,
    )
    if result.returncode:
        raise ValueError(f"cannot identify Serena runtime: {(result.stderr or result.stdout).strip()}")
    payload: dict[str, str | int] = {
        "schema_version": RUNTIME_RESOURCE_SCHEMA,
        "serena_version": result.stdout.strip(),
        "platform": sys.platform,
        "machine": platform.machine(),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    payload["sha256"] = hashlib.sha256(encoded).hexdigest()
    payload["key"] = f"v{RUNTIME_RESOURCE_SCHEMA}-{payload['sha256'][:16]}"
    return payload


def _manifest_with_checksum(identity: dict[str, str | int], languages: Iterable[str]) -> dict:
    manifest = {
        "schema_version": RUNTIME_RESOURCE_SCHEMA,
        "runtime_identity": identity,
        "prepared_languages": sorted(set(languages)),
    }
    encoded = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    manifest["manifest_sha256"] = hashlib.sha256(encoded).hexdigest()
    return manifest


def _valid_generation(path: Path, identity: dict[str, str | int]) -> dict | None:
    manifest_path = path / "manifest.json"
    static_path = path / "static"
    if not manifest_path.is_file() or not static_path.is_dir():
        return None
    try:
        manifest = json.loads(manifest_path.read_text())
    except (OSError, ValueError):
        return None
    checksum = manifest.pop("manifest_sha256", None)
    encoded = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    if checksum != hashlib.sha256(encoded).hexdigest():
        return None
    if manifest.get("runtime_identity") != identity:
        return None
    languages = manifest.get("prepared_languages")
    if not isinstance(languages, list) or not all(isinstance(item, str) for item in languages):
        return None
    manifest["manifest_sha256"] = checksum
    return manifest


def _available_generations(identity_root: Path, identity: dict[str, str | int]) -> list[tuple[Path, dict]]:
    generations = identity_root / "generations"
    if not generations.is_dir():
        return []
    valid: list[tuple[Path, dict]] = []
    for path in sorted(generations.iterdir()):
        if path.is_dir() and (manifest := _valid_generation(path, identity)) is not None:
            valid.append((path, manifest))
    return valid


@contextmanager
def _exclusive_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _make_tree_writable(root: Path) -> None:
    for current, directories, files in os.walk(root):
        os.chmod(current, 0o700)
        for name in directories:
            path = Path(current) / name
            if not path.is_symlink():
                os.chmod(path, 0o700)
        for name in files:
            path = Path(current) / name
            if not path.is_symlink():
                os.chmod(path, path.stat().st_mode | 0o600)


def _make_tree_read_only(root: Path) -> None:
    for current, directories, files in os.walk(root):
        for name in files:
            path = Path(current) / name
            if not path.is_symlink():
                executable = path.stat().st_mode & 0o111
                os.chmod(path, 0o555 if executable else 0o444)
        for name in directories:
            path = Path(current) / name
            if not path.is_symlink():
                os.chmod(path, 0o555)
    os.chmod(root, 0o555)


def link_preparation_resources(runtime_home: Path, generation: Path) -> Path:
    language_servers = runtime_home / "language_servers"
    language_servers.mkdir(parents=True, exist_ok=True)
    link = language_servers / "static"
    if link.exists() or link.is_symlink():
        if not link.is_symlink() or link.resolve() != (generation / "static").resolve():
            raise ValueError(f"runtime resource link already points elsewhere: {link}")
        return link
    link.symlink_to(generation / "static", target_is_directory=True)
    return link


def materialize_runtime_overlay(runtime_home: Path, generation: Path) -> Path:
    static = runtime_home / "language_servers" / "static"
    if static.exists() or static.is_symlink():
        raise ValueError(f"runtime resource overlay already exists: {static}")
    static.mkdir(parents=True)
    for resource in sorted((generation / "static").iterdir()):
        (static / resource.name).symlink_to(resource, target_is_directory=resource.is_dir())
    _write_json_atomic(runtime_home / "runtime-resources.json", {
        "schema_version": RUNTIME_RESOURCE_SCHEMA,
        "seed_generation": str(generation),
        "overlay": str(static),
    })
    return static


def prepare_runtime_resources(
    binary: str | Iterable[str],
    state_root: Path,
    runtime_home: Path,
    languages: Iterable[str],
    prepare: Callable[[Path], object] | None = None,
) -> dict:
    identity = runtime_identity(binary)
    requested = set(languages).intersection(MANAGED_RESOURCE_LANGUAGES)
    resources_root = state_root / "runtime-resources"
    identity_root = resources_root / str(identity["key"])
    lock_path = resources_root / ".locks" / f"{identity['key']}.lock"
    with _exclusive_lock(lock_path):
        valid = _available_generations(identity_root, identity)
        reusable = [item for item in valid if requested.issubset(item[1]["prepared_languages"])]
        if reusable:
            generation, manifest = max(reusable, key=lambda item: len(item[1]["prepared_languages"]))
            materialize_runtime_overlay(runtime_home, generation)
            return {"status": "reused", "generation": str(generation), "manifest": manifest, "prepare_result": None}

        base = max(valid, key=lambda item: len(item[1]["prepared_languages"]), default=None)
        prepared_languages = set(base[1]["prepared_languages"] if base else ())
        prepared_languages.update(requested)
        identity_root.mkdir(parents=True, exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix="resource-prepare-", dir=runtime_home))
        try:
            static = stage / "static"
            if base is None:
                static.mkdir()
            else:
                shutil.copytree(base[0] / "static", static, symlinks=True)
                _make_tree_writable(static)
            preparation_home = stage / "private"
            preparation_home.mkdir()
            link_preparation_resources(preparation_home, stage)
            preparation_result = prepare(preparation_home) if requested and prepare is not None else None
            shutil.rmtree(preparation_home)
            manifest = _manifest_with_checksum(identity, prepared_languages)
            (stage / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
            _make_tree_read_only(static)
            generation_name = f"gen-{manifest['manifest_sha256'][:16]}"
            generation = identity_root / "generations" / generation_name
            generation.parent.mkdir(parents=True, exist_ok=True)
            if generation.exists():
                existing = _valid_generation(generation, identity)
                if existing != manifest:
                    raise ValueError(f"runtime resource generation collision: {generation}")
            else:
                os.replace(stage, generation)
            materialize_runtime_overlay(runtime_home, generation)
            return {
                "status": "prepared",
                "generation": str(generation),
                "manifest": manifest,
                "prepare_result": preparation_result,
            }
        finally:
            if stage.exists():
                _make_tree_writable(stage)
                shutil.rmtree(stage)


def project_seed_identity(project: Path, project_config: Path) -> dict[str, str | int]:
    revision = subprocess.run(
        ["git", "-C", str(project), "rev-parse", "HEAD"],
        capture_output=True, text=True, timeout=10, check=False,
    )
    diff = subprocess.run(
        ["git", "-C", str(project), "diff", "--binary", "--no-ext-diff", "HEAD"],
        capture_output=True, timeout=60, check=False,
    )
    untracked = subprocess.run(
        ["git", "-C", str(project), "ls-files", "--others", "--exclude-standard", "-z"],
        capture_output=True, timeout=30, check=False,
    )
    if revision.returncode or diff.returncode or untracked.returncode:
        raise ValueError(f"cannot fingerprint project for warm seed: {project}")
    worktree_hash = hashlib.sha256(diff.stdout)
    for raw_name in sorted(name for name in untracked.stdout.split(b"\0") if name):
        relative = os.fsdecode(raw_name)
        path = project / relative
        worktree_hash.update(raw_name)
        if path.is_symlink():
            worktree_hash.update(os.fsencode(os.readlink(path)))
        elif path.is_file():
            worktree_hash.update(path.read_bytes())
    payload: dict[str, str | int] = {
        "schema_version": PROJECT_SEED_SCHEMA,
        "project": str(project.resolve()),
        "revision": revision.stdout.strip(),
        "project_config_sha256": hashlib.sha256(project_config.read_bytes()).hexdigest(),
        "saved_worktree_sha256": worktree_hash.hexdigest(),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    payload["sha256"] = hashlib.sha256(encoded).hexdigest()
    return payload


def project_seed_path(state_root: Path, project: Path, identity: dict[str, str | int]) -> Path:
    project_key = str(project.resolve()).lstrip(os.sep)
    return state_root / "project-seeds" / project_key / "snapshots" / str(identity["sha256"])


def _project_seed_manifest(identity: dict[str, str | int]) -> dict:
    manifest = {"schema_version": PROJECT_SEED_SCHEMA, "project_identity": identity}
    encoded = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    manifest["manifest_sha256"] = hashlib.sha256(encoded).hexdigest()
    return manifest


def _valid_project_seed(path: Path, identity: dict[str, str | int]) -> dict | None:
    manifest_path = path / "manifest.json"
    if not manifest_path.is_file() or not (path / "state" / "project.yml").is_file():
        return None
    try:
        manifest = json.loads(manifest_path.read_text())
    except (OSError, ValueError):
        return None
    checksum = manifest.pop("manifest_sha256", None)
    encoded = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    if checksum != hashlib.sha256(encoded).hexdigest() or manifest.get("project_identity") != identity:
        return None
    manifest["manifest_sha256"] = checksum
    return manifest


def restore_project_seed(state_root: Path, runtime_home: Path, project: Path) -> dict:
    project_state = cache_folder(runtime_home, project)
    try:
        identity = project_seed_identity(project, project_state / "project.yml")
    except (OSError, ValueError) as exc:
        return {"project": str(project), "status": "unavailable", "reason": str(exc)}
    seed = project_seed_path(state_root, project, identity)
    if _valid_project_seed(seed, identity) is None:
        return {"project": str(project), "status": "cold", "identity": identity}
    stage = project_state.with_name(f".serena-restore-{secrets.token_hex(6)}")
    shutil.copytree(seed / "state", stage, symlinks=True)
    _make_tree_writable(stage)
    shutil.rmtree(project_state)
    os.replace(stage, project_state)
    return {"project": str(project), "status": "restored", "identity": identity, "seed": str(seed)}


def publish_project_seed(state_root: Path, runtime_home: Path, project: Path) -> dict:
    project_state = cache_folder(runtime_home, project)
    identity = project_seed_identity(project, project_state / "project.yml")
    seed = project_seed_path(state_root, project, identity)
    project_lock = hashlib.sha256(str(project.resolve()).encode()).hexdigest()
    lock_path = state_root / "project-seeds" / ".locks" / f"{project_lock}.lock"
    with _exclusive_lock(lock_path):
        existing = _valid_project_seed(seed, identity)
        if existing is not None:
            return {"project": str(project), "status": "reused", "identity": identity, "seed": str(seed)}
        stage = Path(tempfile.mkdtemp(prefix="project-seed-", dir=runtime_home))
        try:
            shutil.copytree(project_state, stage / "state", symlinks=True)
            _make_tree_read_only(stage / "state")
            manifest = _project_seed_manifest(identity)
            (stage / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
            seed.parent.mkdir(parents=True, exist_ok=True)
            if seed.exists():
                raise ValueError(f"project seed collision: {seed}")
            os.replace(stage, seed)
            return {"project": str(project), "status": "published", "identity": identity, "seed": str(seed)}
        finally:
            if stage.exists():
                _make_tree_writable(stage)
                shutil.rmtree(stage)


def git_common_dir(project: Path) -> Path | None:
    result = subprocess.run(
        ["git", "-C", str(project), "rev-parse", "--git-common-dir"],
        capture_output=True, text=True, timeout=10, check=False,
    )
    if result.returncode:
        return None
    path = Path(result.stdout.strip())
    return (project / path).resolve() if not path.is_absolute() else path.resolve()


def binding_report(startup_project: Path | None, expected_project: Path | None = None) -> dict[str, str | None]:
    if startup_project is None:
        return {
            "status": "unbound",
            "startup_project": None,
            "expected_project": str(expected_project) if expected_project else None,
            "guidance": "Start the client from the intended Git worktree or activate its absolute root; shell cd does not retarget a running server.",
        }
    if expected_project is None or startup_project.resolve() == expected_project.resolve():
        return {
            "status": "bound_exact_worktree",
            "startup_project": str(startup_project.resolve()),
            "expected_project": str(expected_project.resolve()) if expected_project else None,
            "guidance": "The server is bound to this exact working-tree root.",
        }
    startup_common = git_common_dir(startup_project)
    expected_common = git_common_dir(expected_project)
    same_repository = startup_common is not None and startup_common == expected_common
    status = "wrong_worktree" if same_repository else "different_project"
    return {
        "status": status,
        "startup_project": str(startup_project.resolve()),
        "expected_project": str(expected_project.resolve()),
        "guidance": "Restart from the expected worktree or activate its absolute root; shell cd does not retarget a running server.",
    }


def process_identity(pid: int) -> dict[str, str]:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return {"status": "absent", "identity": ""}
    except PermissionError:
        return {"status": "unknown", "identity": ""}
    proc_stat = Path(f"/proc/{pid}/stat")
    boot_id = Path("/proc/sys/kernel/random/boot_id")
    if proc_stat.is_file() and boot_id.is_file():
        try:
            fields = proc_stat.read_text().rsplit(")", 1)[1].split()
            return {"status": "active", "identity": f"linux:{boot_id.read_text().strip()}:{fields[19]}"}
        except (OSError, IndexError):
            return {"status": "unknown", "identity": ""}
    try:
        result = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return {"status": "unknown", "identity": ""}
    started = result.stdout.strip()
    if result.returncode == 0 and started:
        return {"status": "active", "identity": f"ps:{started}"}
    return {"status": "unknown", "identity": ""}


def _write_json_atomic(path: Path, document: dict) -> None:
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    temporary.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")
    os.replace(temporary, path)


def create_session(sessions: Path, identity: dict[str, str | int], project: Path | None) -> tuple[Path, dict]:
    sessions.mkdir(parents=True, exist_ok=True)
    runtime_home = Path(tempfile.mkdtemp(prefix="mcp-", dir=sessions))
    launcher = process_identity(os.getpid())
    lease = {
        "schema_version": SESSION_LEASE_SCHEMA,
        "owner": SESSION_OWNER,
        "session_id": secrets.token_hex(16),
        "runtime_dir": str(runtime_home.resolve()),
        "hostname": socket.gethostname(),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "runtime_identity": identity,
        "startup_project": str(project) if project else None,
        "processes": [{
            "role": "launcher",
            "pid": os.getpid(),
            "identity": launcher["identity"],
            "identity_status": launcher["status"],
        }],
    }
    _write_json_atomic(runtime_home / "lease.json", lease)
    return runtime_home, lease


def add_child_to_lease(runtime_home: Path, lease: dict, pid: int) -> None:
    child = process_identity(pid)
    lease["processes"].append({
        "role": "serena",
        "pid": pid,
        "identity": child["identity"],
        "identity_status": child["status"],
    })
    _write_json_atomic(runtime_home / "lease.json", lease)


def classify_session(path: Path, host: str | None = None, probe: Callable[[int], dict[str, str]] = process_identity) -> dict:
    base = {"path": str(path), "classification": "unknown", "proposed_action": "preserve"}
    try:
        lease = json.loads((path / "lease.json").read_text())
    except (OSError, ValueError):
        return {**base, "reason": "missing or invalid lease metadata"}
    if lease.get("schema_version") != SESSION_LEASE_SCHEMA or lease.get("owner") != SESSION_OWNER:
        return {**base, "reason": "unrecognized lease owner or schema"}
    runtime_dir = lease.get("runtime_dir")
    if not isinstance(runtime_dir, str) or Path(runtime_dir).resolve() != path.resolve():
        return {**base, "reason": "lease path identity mismatch"}
    runtime = lease.get("runtime_identity")
    if not isinstance(runtime, dict) or not isinstance(runtime.get("key"), str):
        return {**base, "reason": "lease has no runtime identity"}
    if lease.get("hostname") != (host or socket.gethostname()):
        return {**base, "reason": "lease belongs to another host"}
    processes = lease.get("processes")
    if not isinstance(processes, list) or not processes:
        return {**base, "reason": "lease has no process identities"}
    observations = []
    has_active = False
    has_unknown = False
    for process in processes:
        if not isinstance(process, dict) or not isinstance(process.get("pid"), int) or not process.get("identity"):
            return {**base, "reason": "lease contains an invalid process identity"}
        observed = probe(process["pid"])
        observations.append({"role": process.get("role"), "pid": process["pid"], "status": observed["status"]})
        if observed["status"] == "active" and observed["identity"] == process["identity"]:
            has_active = True
        if observed["status"] == "unknown":
            has_unknown = True
    if has_active:
        return {**base, "classification": "active", "reason": "a leased process identity is active", "observations": observations}
    if has_unknown:
        return {**base, "reason": "a leased process identity could not be verified", "observations": observations}
    return {
        **base,
        "classification": "abandoned_candidate",
        "proposed_action": "review_then_remove",
        "reason": "no leased process identity is active; no deletion was performed",
        "observations": observations,
    }


def cleanup_plan(state_root: Path) -> dict:
    sessions = state_root / "sessions"
    entries = [] if not sessions.is_dir() else [classify_session(path) for path in sorted(sessions.iterdir()) if path.is_dir()]
    return {
        "mode": "read_only",
        "automatic_deletion": False,
        "policy": "Never delete by age. Preserve active and unknown sessions; manually review abandoned candidates.",
        "cleanup_candidates": [item["path"] for item in entries if item["classification"] == "abandoned_candidate"],
        "required_review": [
            "confirm the lease path and owner",
            "confirm every recorded process identity is inactive",
            "confirm no process has the runtime directory open",
        ],
        "sessions": entries,
    }


def registrable_projects(projects: Iterable[Path], preflight: Iterable[dict], manifest: dict) -> list[Path]:
    reports = {item["project"]: item for item in preflight}
    prepared = set(manifest.get("prepared_languages", ()))
    registered: list[Path] = []
    for project in projects:
        report = reports.get(str(project))
        if report is None or report.get("missing_executables"):
            continue
        managed = set(report.get("languages", ())).intersection(MANAGED_RESOURCE_LANGUAGES)
        if managed.issubset(prepared):
            registered.append(project)
    return registered


def action_failed(target_failures: Iterable[dict], blocked_projects: Iterable[str], results: Iterable[dict[str, str]]) -> bool:
    return bool(list(target_failures) or list(blocked_projects) or any(item["status"] != "ok" for item in results))


def launch_spec(cwd: Path, runtime_home: Path, state_root: Path) -> tuple[list[str], dict[str, str], dict]:
    env = os.environ.copy()
    env["PATH"] = os.pathsep.join((str(Path.home() / ".local/bin"), env.get("PATH", "")))
    configured_binary = env.get("SERENA_EXECUTABLE")
    binary = str(Path(configured_binary).expanduser().resolve()) if configured_binary else shutil.which("serena", path=env["PATH"])
    if not binary:
        raise ValueError("serena must be installed on PATH")
    if configured_binary and (not os.path.isfile(binary) or not os.access(binary, os.X_OK)):
        raise ValueError(f"Serena executable is not runnable: {binary}")
    prefix = serena_cli_prefix(binary)
    config = json.loads((ASSETS / "serena-config.json").read_text())
    config["project_serena_folder_location"] = str(runtime_home / "projects") + "/$projectDir/.serena"
    env.update({
        "SERENA_HOME": str(runtime_home),
        "SERENA_USAGE_REPORTING": "false",
        "GOTELEMETRY": "off",
        "PYTHONDONTWRITEBYTECODE": "1",
    })
    command = [*prefix, "start-mcp-server", "--context", str(ASSETS / "code-navigation.yml"),
               "--mode", "no-memories", "--language-backend", "LSP",
               "--enable-web-dashboard", "false", "--open-web-dashboard", "false",
               "--enable-gui-log-window", "false"]
    project = project_root(cwd)
    if project is not None:
        command.extend(("--project", str(project)))
    return command, env, config


def main() -> int:
    parser = argparse.ArgumentParser(description="Start session-isolated Serena with persistent worktree-scoped caches.")
    parser.add_argument("--check", action="store_true", help="Print resolved project and executable paths without starting Serena.")
    parser.add_argument("--expect-project", metavar="PATH", help="Report and fail a check when startup is bound to another project or worktree.")
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--discover", action="store_true", help="Discover and register repositories without starting the MCP server.")
    actions.add_argument("--warm", nargs="*", metavar="PROJECT", help="Build Serena symbol caches for selected projects, or all projects when omitted.")
    actions.add_argument("--doctor", nargs="*", metavar="PROJECT", help="Run one real language-server symbol query per selected project.")
    actions.add_argument("--cleanup-plan", action="store_true", help="Classify session directories without deleting any state.")
    args = parser.parse_args()
    state_root = Path(os.environ.get("SERENA_CODE_STATE_ROOT", str(Path.home() / ".serena/code-intelligence"))).expanduser().resolve()
    cwd = Path.cwd().resolve()
    if args.cleanup_plan:
        print(json.dumps(cleanup_plan(state_root), indent=2))
        return 0
    if args.expect_project and not args.check:
        raise ValueError("--expect-project is only valid with --check")
    if args.check:
        root = project_root(cwd)
        expected = Path(args.expect_project).expanduser().resolve() if args.expect_project else None
        check_home = state_root / "sessions" / "<connection>"
        command, env, _ = launch_spec(cwd, check_home, state_root)
        cli = command[:command.index("start-mcp-server")]
        excluded_report: list[dict] = []
        discovered = discover_projects(workspace_roots(), excluded_report)
        preflight = preflight_projects([root], path=env["PATH"]) if root else []
        binding = binding_report(root, expected)
        output = {
            "command": command,
            "project": str(root) if root else None,
            "project_cache": str(cache_folder(check_home, root)) if root else None,
            "project_state": "per_connection",
            "binding": binding,
            "preflight": preflight,
            "runtime_identity": runtime_identity(cli),
            "workspace_roots": [str(path) for path in workspace_roots()],
            "discovered_projects": len(discovered),
            "discovery_exclusions": excluded_report,
            "configured_projects": 0,
            "tools": TOOLS,
        }
        print(json.dumps(output, indent=2))
        missing = any(item["missing_executables"] for item in preflight)
        binding_failed = binding["status"] in ("unbound", "wrong_worktree", "different_project")
        return 1 if missing or binding_failed else 0
    os.umask(0o077)
    startup_project = project_root(cwd)
    provisional_home = state_root / "sessions/provisional"
    command, env, _ = launch_spec(cwd, provisional_home, state_root)
    cli = command[:command.index("start-mcp-server")]
    identity = runtime_identity(cli)
    runtime_home, lease = create_session(state_root / "sessions", identity, startup_project)
    try:
        command, env, _ = launch_spec(cwd, runtime_home, state_root)
        cli = command[:command.index("start-mcp-server")]
        excluded_report: list[dict] = []
        discovered = discover_projects(workspace_roots(), excluded_report)
        if startup_project is not None and startup_project not in discovered:
            discovered.append(startup_project)
            discovered.sort()
        selected_action = "warm" if args.warm is not None else "doctor" if args.doctor is not None else None
        selectors = args.warm if args.warm is not None else args.doctor if args.doctor is not None else None
        if args.discover:
            selected = discovered
        elif selected_action is not None:
            selected = resolve_projects(selectors or [], discovered)
        else:
            selected = [startup_project] if startup_project else []
        write_runtime_config(runtime_home, runtime_home, [])
        ready, failures = ensure_project_configs(cli, env, runtime_home, selected)
        if args.discover:
            print(json.dumps({"projects": [str(path) for path in ready], "failures": failures,
                              "discovery_exclusions": excluded_report}, indent=2))
            return 1 if failures else 0
        target_failures = failures
        selected_names = {str(path) for path in selected}
        unrelated_discovery = [{
            "project": str(path),
            "status": "not_evaluated",
            "reason": "outside the explicit target set",
        } for path in discovered if str(path) not in selected_names] if selectors else []
        runnable = [path for path in selected if path in ready]
        preflight = preflight_projects(runnable, path=env["PATH"], project_state_root=runtime_home)
        project_profiles = {item["project"]: item for item in preflight}
        blocked_projects = {item["project"] for item in preflight if item["missing_executables"]}
        runnable = [path for path in runnable if str(path) not in blocked_projects]
        languages = {language for item in preflight if item["project"] not in blocked_projects for language in item["languages"]}

        if target_failures or blocked_projects:
            if selected_action is None:
                if target_failures:
                    raise ValueError("startup project configuration failed: " + json.dumps(target_failures, sort_keys=True))
                missing = sorted({name for item in preflight for name in item["missing_executables"]})
                raise ValueError("missing inferred language-server executables: " + ", ".join(missing))
            results = [{
                "project": item["project"],
                "status": "blocked",
                "detail": "missing inferred language-server executables: " + ", ".join(item["missing_executables"]),
            } for item in preflight if item["missing_executables"]]
            print(json.dumps({
                "action": selected_action,
                "binding": binding_report(startup_project, selected[0] if len(selected) == 1 else None),
                "preflight": preflight,
                "runtime_resources": {"status": "not_prepared", "reason": "target preflight failed"},
                "results": results,
                "target_failures": target_failures,
                "unrelated_discovery_failures": [],
                "unrelated_discovery": unrelated_discovery,
            }, indent=2))
            return 1

        seed_restores = [restore_project_seed(state_root, runtime_home, project) for project in runnable]

        prepared_results: list[dict[str, str]] | None = None

        def prepare(preparation_home: Path) -> list[dict[str, str]]:
            preparation_env = env.copy()
            preparation_env["SERENA_HOME"] = str(preparation_home)
            write_runtime_config(preparation_home, runtime_home, runnable)
            results = run_project_command(
                cli, preparation_env, selected_action or "doctor", runnable, project_profiles=project_profiles,
            )
            if any(item["status"] != "ok" for item in results):
                raise ValueError("runtime resource preparation failed: " + json.dumps(results, sort_keys=True))
            return results

        cache = prepare_runtime_resources(cli, state_root, runtime_home, languages, prepare=prepare)
        registered = registrable_projects(runnable, preflight, cache["manifest"])
        write_runtime_config(runtime_home, runtime_home, registered)
        if cache["prepare_result"] is not None:
            prepared_results = cache["prepare_result"]

        if selected_action is not None:
            results = (
                prepared_results
                if prepared_results is not None
                else run_project_command(cli, env, selected_action, runnable, project_profiles=project_profiles)
                if runnable
                else []
            )
            project_seeds = []
            if selected_action == "warm" and not action_failed(target_failures, blocked_projects, results):
                project_seeds = [publish_project_seed(state_root, runtime_home, project) for project in runnable]
            print(json.dumps({
                "action": selected_action,
                "binding": binding_report(startup_project, selected[0] if len(selected) == 1 else None),
                "preflight": preflight,
                "runtime_resources": {key: cache[key] for key in ("status", "generation", "manifest")},
                "project_seed_restores": seed_restores,
                "project_seeds": project_seeds,
                "results": results,
                "target_failures": target_failures,
                "unrelated_discovery_failures": [],
                "unrelated_discovery": unrelated_discovery,
            }, indent=2))
            return 1 if action_failed(target_failures, blocked_projects, results) else 0
        if startup_project is None:
            print(binding_report(None)["guidance"], file=sys.stderr)
        # Each connection owns config, logs, registry and active-project state. Only
        # immutable, versioned language-server runtime resources are shared.
        process = subprocess.Popen(command, env=env)
        try:
            add_child_to_lease(runtime_home, lease, process.pid)
            return process.wait()
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
    finally:
        if runtime_home.exists():
            shutil.rmtree(runtime_home)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"dev-code-navigation: {exc}", file=sys.stderr)
        raise SystemExit(1)
