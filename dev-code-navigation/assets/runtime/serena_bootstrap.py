"""Run Serena with strict private project-state resolution."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile


SAFE_PROJECT_KEYS = (
    "project_name",
    "language_servers",
    "languages",
    "ignored_paths",
    "ls_workspace_folders",
    "ls_additional_workspace_folders",
    "ignore_all_files_in_gitignore",
    "encoding",
    "line_ending",
    "symbol_info_budget",
    "ls_specific_settings",
)


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _safe_workspace_paths(values: object, project: Path, field: str) -> list[str]:
    if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
        raise ValueError(f"{field} must be a list of paths")
    normalized = []
    for value in values:
        candidate = Path(value)
        resolved = (candidate if candidate.is_absolute() else project / candidate).resolve()
        if not _is_within(resolved, project):
            raise ValueError(f"{field} escapes the project root: {value}")
        normalized.append(os.path.relpath(resolved, project))
    return normalized


def normalize_project_config(source: Path, destination: Path, project: Path) -> None:
    import yaml

    project = project.resolve()
    expected_source = project / ".serena" / "project.yml"
    if source != expected_source or source.is_symlink() or source.parent.is_symlink():
        raise ValueError("repository project config must be a non-symlink .serena/project.yml")
    if not source.is_file() or not _is_within(source.resolve(), project):
        raise ValueError("repository project config is missing or escapes the project root")
    private_root = (Path(os.environ["SERENA_HOME"]) / "projects").resolve()
    if not _is_within(destination.resolve(strict=False), private_root):
        raise ValueError("private project config destination escapes the session root")
    document = yaml.safe_load(source.read_text())
    if not isinstance(document, dict):
        raise ValueError("repository project config must be a mapping")
    normalized = {key: document[key] for key in SAFE_PROJECT_KEYS if key in document}
    if "language_servers" not in normalized and "languages" in normalized:
        normalized["language_servers"] = normalized.pop("languages")
    else:
        normalized.pop("languages", None)
    languages = normalized.get("language_servers")
    if not isinstance(languages, list) or not languages or not all(isinstance(item, str) for item in languages):
        raise ValueError("repository project config must declare language_servers")
    if "ignored_paths" in normalized and not all(isinstance(item, str) for item in normalized["ignored_paths"]):
        raise ValueError("ignored_paths must contain strings")
    if "encoding" in normalized and not isinstance(normalized["encoding"], str):
        raise ValueError("encoding must be a string")
    if "ls_specific_settings" in normalized and not isinstance(normalized["ls_specific_settings"], dict):
        raise ValueError("ls_specific_settings must be a mapping")
    for field in ("ls_workspace_folders", "ls_additional_workspace_folders"):
        if field in normalized:
            normalized[field] = _safe_workspace_paths(normalized[field], project, field)
    normalized["project_name"] = str(normalized.get("project_name") or project.name)
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".project-", suffix=".yml", dir=destination.parent)
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        temporary.write_text(json.dumps(normalized, indent=2, sort_keys=True) + "\n")
        os.replace(temporary, destination)
        selection = {
            "language_servers": normalized["language_servers"],
            "ignored_paths": normalized.get("ignored_paths", []),
        }
        selection_path = destination.with_name("launcher-project.json")
        descriptor, selection_name = tempfile.mkstemp(prefix=".launcher-project-", suffix=".json", dir=destination.parent)
        os.close(descriptor)
        selection_temporary = Path(selection_name)
        try:
            selection_temporary.write_text(json.dumps(selection, indent=2, sort_keys=True) + "\n")
            os.replace(selection_temporary, selection_path)
        finally:
            if selection_temporary.exists():
                selection_temporary.unlink()
    finally:
        if temporary.exists():
            temporary.unlink()


def patch_project_resolution() -> None:
    from serena.config.serena_config import SerenaConfig

    if getattr(SerenaConfig, "_code_intelligence_private_state_patch", False):
        return
    original_add_project_from_path = SerenaConfig.add_project_from_path

    def strict_project_serena_folder(self, project_root):
        return self.get_configured_project_serena_folder(project_root)

    def isolated_add_project_from_path(self, project_root, asynchronous_autogen=False):
        project = Path(project_root).resolve()
        destination = Path(self.get_configured_project_serena_folder(project)) / "project.yml"
        source = project / ".serena" / "project.yml"
        if not destination.is_file() and (source.exists() or source.is_symlink()):
            normalize_project_config(source, destination, project)
        return original_add_project_from_path(self, project, asynchronous_autogen=asynchronous_autogen)

    SerenaConfig.get_project_serena_folder = strict_project_serena_folder
    SerenaConfig.add_project_from_path = isolated_add_project_from_path
    SerenaConfig._code_intelligence_private_state_patch = True


def filter_gitwildmatch_paths(paths: object, patterns: object) -> list[str]:
    from pathspec.patterns import GitWildMatchPattern

    if not isinstance(paths, list) or not all(isinstance(path, str) for path in paths):
        raise ValueError("paths must be a list of strings")
    if not isinstance(patterns, list) or not all(isinstance(pattern, str) for pattern in patterns):
        raise ValueError("patterns must be a list of strings")
    compiled = [GitWildMatchPattern(pattern) for pattern in patterns]
    selected = []
    for path in paths:
        ignored = False
        for pattern in compiled:
            if pattern.include is not None and pattern.match_file(path) is not None:
                ignored = pattern.include
        if not ignored:
            selected.append(path)
    return selected


def main() -> int:
    patch_project_resolution()
    if len(sys.argv) == 5 and sys.argv[1] == "isolation-copy-project-config":
        normalize_project_config(Path(sys.argv[2]), Path(sys.argv[3]), Path(sys.argv[4]))
        return 0
    if len(sys.argv) == 2 and sys.argv[1] == "isolation-filter-paths":
        request = json.load(sys.stdin)
        print(json.dumps({"paths": filter_gitwildmatch_paths(request.get("paths"), request.get("patterns"))}))
        return 0
    from serena.cli import top_level

    return top_level()


if __name__ == "__main__":
    raise SystemExit(main())
