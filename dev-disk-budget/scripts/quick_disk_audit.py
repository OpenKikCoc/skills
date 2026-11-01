#!/usr/bin/env python3
"""Run a bounded read-only disk usage audit."""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1
REPORT_KIND = "dev-disk-budget-quick-audit"
DEFAULT_JOBS = 4
DEFAULT_TOP = 20
DEFAULT_TIMEOUT_SECONDS = 120
GROUP_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


class AuditError(RuntimeError):
    """Raised when the quick-audit input contract is invalid."""


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def format_time(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def audit_path(raw_path: str | Path) -> Path:
    return Path(os.path.abspath(os.path.expanduser(str(raw_path))))


def filesystem_report(path: Path) -> dict[str, Any]:
    nearest = path
    while not nearest.exists() and nearest != nearest.parent:
        nearest = nearest.parent
    usage = shutil.disk_usage(nearest)
    free_percent = 100.0 * usage.free / usage.total if usage.total else 0.0
    return {
        "path": str(path),
        "filesystem_probe": str(nearest),
        "total_bytes": usage.total,
        "used_bytes": usage.used,
        "free_bytes": usage.free,
        "free_percent": round(free_percent, 2),
    }


def parse_groups(raw_groups: list[str]) -> list[tuple[str, re.Pattern[str]]]:
    groups: list[tuple[str, re.Pattern[str]]] = []
    names: set[str] = set()
    for index, raw_group in enumerate(raw_groups):
        if "=" not in raw_group:
            raise AuditError(f"group {index} must use NAME=REGEX syntax")
        raw_name, raw_pattern = raw_group.split("=", 1)
        name = raw_name.strip()
        if not GROUP_NAME_PATTERN.fullmatch(name):
            raise AuditError(
                f"group {index} name must use lowercase letters, digits, '.', "
                "'_', or '-'"
            )
        if name == "other":
            raise AuditError("group name 'other' is reserved")
        if name in names:
            raise AuditError(f"duplicate group name '{name}'")
        if not raw_pattern:
            raise AuditError(f"group {index} pattern must not be empty")
        try:
            pattern = re.compile(raw_pattern)
        except re.error as error:
            raise AuditError(f"group {index} pattern is invalid: {error}") from error
        names.add(name)
        groups.append((name, pattern))
    return groups


def discover_root(raw_root: str) -> dict[str, Any]:
    root = audit_path(raw_root)
    result: dict[str, Any] = {
        "path": str(root),
        "entries": [],
        "ignored_entry_count": 0,
        "error": None,
    }
    if root.is_symlink():
        result["error"] = "scan root must not be a symlink"
        return result
    if not root.exists():
        result["error"] = "scan root is absent"
        return result
    if not root.is_dir():
        result["error"] = "scan root is not a directory"
        return result

    entries: list[dict[str, str]] = []
    ignored = 0
    try:
        with os.scandir(root) as stream:
            for entry in stream:
                try:
                    if entry.is_symlink():
                        ignored += 1
                    elif entry.is_dir(follow_symlinks=False):
                        entries.append(
                            {
                                "name": entry.name,
                                "path": entry.path,
                                "type": "directory",
                            }
                        )
                    elif entry.is_file(follow_symlinks=False):
                        entries.append(
                            {
                                "name": entry.name,
                                "path": entry.path,
                                "type": "file",
                            }
                        )
                    else:
                        ignored += 1
                except OSError:
                    ignored += 1
    except OSError as error:
        result["error"] = f"cannot enumerate scan root: {error}"
        return result

    entries.sort(key=lambda item: item["name"])
    result["entries"] = entries
    result["ignored_entry_count"] = ignored
    return result


def measure_path(path: Path, timeout_seconds: int) -> dict[str, Any]:
    started = time.monotonic()
    if path.is_symlink():
        return {
            "path": str(path),
            "status": "excluded",
            "allocated_bytes": None,
            "duration_seconds": round(time.monotonic() - started, 3),
            "error": "symlink paths are excluded",
        }
    if not path.exists():
        return {
            "path": str(path),
            "status": "absent",
            "allocated_bytes": None,
            "duration_seconds": round(time.monotonic() - started, 3),
            "error": None,
        }

    try:
        completed = subprocess.run(
            ["du", "-skx", str(path)],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_seconds,
            check=False,
            env={**os.environ, "LC_ALL": "C"},
        )
    except FileNotFoundError as error:
        return {
            "path": str(path),
            "status": "error",
            "allocated_bytes": None,
            "duration_seconds": round(time.monotonic() - started, 3),
            "error": f"du is unavailable: {error}",
        }
    except subprocess.TimeoutExpired:
        return {
            "path": str(path),
            "status": "timeout",
            "allocated_bytes": None,
            "duration_seconds": round(time.monotonic() - started, 3),
            "error": f"du exceeded {timeout_seconds}s timeout",
        }

    allocated_bytes: int | None = None
    output = completed.stdout.strip()
    if output:
        first_field = output.splitlines()[-1].split(maxsplit=1)[0]
        try:
            allocated_bytes = int(first_field) * 1024
        except ValueError:
            allocated_bytes = None
    error = completed.stderr.strip() or None
    if completed.returncode != 0 and error is None:
        error = f"du exited with status {completed.returncode}"
    if allocated_bytes is None and error is None:
        error = "du returned no parseable size"
    return {
        "path": str(path),
        "status": "measured" if allocated_bytes is not None else "error",
        "allocated_bytes": allocated_bytes,
        "duration_seconds": round(time.monotonic() - started, 3),
        "error": error,
    }


def measure_paths(
    paths: list[Path],
    *,
    jobs: int,
    timeout_seconds: int,
) -> dict[str, dict[str, Any]]:
    unique_paths = list(dict.fromkeys(paths))
    measurements: dict[str, dict[str, Any]] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as executor:
        futures = {
            executor.submit(measure_path, path, timeout_seconds): path
            for path in unique_paths
        }
        for future in concurrent.futures.as_completed(futures):
            path = futures[future]
            try:
                measurements[str(path)] = future.result()
            except Exception as error:
                measurements[str(path)] = {
                    "path": str(path),
                    "status": "error",
                    "allocated_bytes": None,
                    "duration_seconds": 0.0,
                    "error": f"unexpected measurement failure: {error}",
                }
    return measurements


def scope_document(
    filesystem_path: Path,
    discovered_roots: list[dict[str, Any]],
    focused_paths: list[Path],
    groups: list[tuple[str, re.Pattern[str]]],
    top: int,
) -> dict[str, Any]:
    return {
        "filesystem_path": str(filesystem_path),
        "scan_roots": [root["path"] for root in discovered_roots],
        "focused_paths": [str(path) for path in focused_paths],
        "groups": [
            {"name": name, "pattern": pattern.pattern} for name, pattern in groups
        ],
        "top": top,
    }


def scope_sha256(scope: dict[str, Any]) -> str:
    canonical = json.dumps(scope, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(canonical).hexdigest()


def validate_previous(document: dict[str, Any]) -> None:
    if document.get("schema_version") != SCHEMA_VERSION:
        raise AuditError(f"previous audit schema_version must be {SCHEMA_VERSION}")
    if document.get("kind") != REPORT_KIND:
        raise AuditError(f"previous audit kind must be {REPORT_KIND}")
    previous_scope = document.get("scope")
    if not isinstance(previous_scope, dict):
        raise AuditError("previous audit scope must be an object")
    if document.get("scope_sha256") != scope_sha256(previous_scope):
        raise AuditError("previous audit scope_sha256 does not match its scope")


def load_previous(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise AuditError(f"previous audit does not exist: {path}") from error
    except (OSError, json.JSONDecodeError) as error:
        raise AuditError(f"cannot read previous audit {path}: {error}") from error
    if not isinstance(document, dict):
        raise AuditError("previous audit must be a JSON object")
    validate_previous(document)
    return document


def add_size_delta(current: dict[str, Any], previous: dict[str, Any]) -> None:
    previous_size = previous.get("allocated_bytes")
    current_size = current.get("allocated_bytes")
    if isinstance(previous_size, int) and isinstance(current_size, int):
        current["previous_allocated_bytes"] = previous_size
        current["delta_allocated_bytes"] = current_size - previous_size


def add_deltas(
    report: dict[str, Any],
    previous: dict[str, Any] | None,
    previous_path: Path | None,
) -> None:
    if previous is None:
        report["previous"] = None
        return
    validate_previous(previous)
    if previous.get("status") != "complete":
        raise AuditError("previous audit must have status complete")
    if previous.get("scope_sha256") != report["scope_sha256"]:
        raise AuditError("previous audit scope does not match current audit scope")
    report["previous"] = {
        "path": str(previous_path),
        "generated_at": previous.get("generated_at"),
    }

    previous_filesystem = previous.get("filesystem")
    if isinstance(previous_filesystem, dict):
        for key in ("used_bytes", "free_bytes"):
            previous_value = previous_filesystem.get(key)
            current_value = report["filesystem"].get(key)
            if isinstance(previous_value, int) and isinstance(current_value, int):
                report["filesystem"][f"previous_{key}"] = previous_value
                report["filesystem"][f"delta_{key}"] = current_value - previous_value

    previous_roots = {
        root.get("path"): root
        for root in previous.get("scan_roots", [])
        if isinstance(root, dict) and isinstance(root.get("path"), str)
    }
    for root in report["scan_roots"]:
        previous_root = previous_roots.get(root["path"])
        if not isinstance(previous_root, dict):
            continue
        add_size_delta(root, previous_root)
        previous_groups = {
            group.get("name"): group
            for group in previous_root.get("groups", [])
            if isinstance(group, dict) and isinstance(group.get("name"), str)
        }
        for group in root["groups"]:
            previous_group = previous_groups.get(group["name"])
            if isinstance(previous_group, dict):
                add_size_delta(group, previous_group)
        previous_entries = {
            entry.get("path"): entry
            for entry in previous_root.get("top_entries", [])
            if isinstance(entry, dict) and isinstance(entry.get("path"), str)
        }
        for entry in root["top_entries"]:
            previous_entry = previous_entries.get(entry["path"])
            if isinstance(previous_entry, dict):
                add_size_delta(entry, previous_entry)

    previous_focused = {
        item.get("path"): item
        for item in previous.get("focused_paths", [])
        if isinstance(item, dict) and isinstance(item.get("path"), str)
    }
    for item in report["focused_paths"]:
        previous_item = previous_focused.get(item["path"])
        if isinstance(previous_item, dict):
            add_size_delta(item, previous_item)


def quick_audit(
    filesystem_path: Path,
    *,
    scan_roots: list[str],
    focused_paths: list[str],
    raw_groups: list[str],
    jobs: int,
    top: int,
    timeout_seconds: int,
    previous: dict[str, Any] | None,
    previous_path: Path | None,
    now: dt.datetime,
) -> dict[str, Any]:
    if not scan_roots and not focused_paths:
        raise AuditError("quick audit requires --scan-root or --focus")
    if jobs <= 0 or jobs > 16:
        raise AuditError("jobs must be between 1 and 16")
    if top <= 0:
        raise AuditError("top must be positive")
    if timeout_seconds <= 0:
        raise AuditError("timeout-seconds must be positive")

    groups = parse_groups(raw_groups)
    started = time.monotonic()
    discovered_roots = [discover_root(root) for root in scan_roots]
    normalized_focused = list(
        dict.fromkeys(audit_path(path) for path in focused_paths)
    )
    scope = scope_document(
        filesystem_path,
        discovered_roots,
        normalized_focused,
        groups,
        top,
    )

    scan_paths = [
        Path(entry["path"])
        for root in discovered_roots
        for entry in root["entries"]
    ]
    measurements = measure_paths(
        scan_paths + normalized_focused,
        jobs=jobs,
        timeout_seconds=timeout_seconds,
    )

    complete = True
    rendered_roots: list[dict[str, Any]] = []
    for root in discovered_roots:
        group_totals = {
            name: {"name": name, "entry_count": 0, "allocated_bytes": 0}
            for name, _ in groups
        }
        group_totals["other"] = {
            "name": "other",
            "entry_count": 0,
            "allocated_bytes": 0,
        }
        rendered_entries: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []
        if root["error"]:
            complete = False
            errors.append({"path": root["path"], "error": root["error"]})
        for entry in root["entries"]:
            measurement = measurements[str(Path(entry["path"]))]
            rendered = {
                "name": entry["name"],
                "path": entry["path"],
                "type": entry["type"],
                "allocated_bytes": measurement["allocated_bytes"],
                "status": measurement["status"],
                "error": measurement["error"],
            }
            rendered_entries.append(rendered)
            if measurement["error"]:
                complete = False
                errors.append(
                    {"path": entry["path"], "error": measurement["error"]}
                )
            if not isinstance(measurement["allocated_bytes"], int):
                continue
            group_name = "other"
            for name, pattern in groups:
                if pattern.search(entry["name"]):
                    group_name = name
                    break
            group_totals[group_name]["entry_count"] += 1
            group_totals[group_name]["allocated_bytes"] += measurement[
                "allocated_bytes"
            ]

        measured_entries = [
            entry
            for entry in rendered_entries
            if isinstance(entry["allocated_bytes"], int)
        ]
        measured_entries.sort(
            key=lambda entry: (-entry["allocated_bytes"], entry["path"])
        )
        rendered_roots.append(
            {
                "path": root["path"],
                "entry_count": len(root["entries"]),
                "measured_entry_count": len(measured_entries),
                "ignored_entry_count": root["ignored_entry_count"],
                "allocated_bytes": sum(
                    entry["allocated_bytes"] for entry in measured_entries
                ),
                "groups": sorted(
                    group_totals.values(),
                    key=lambda group: (-group["allocated_bytes"], group["name"]),
                ),
                "top_entries": measured_entries[:top],
                "errors": errors,
            }
        )

    rendered_focused: list[dict[str, Any]] = []
    for path in normalized_focused:
        measurement = measurements[str(path)]
        rendered_focused.append(measurement)
        if measurement["error"]:
            complete = False
    rendered_focused.sort(
        key=lambda item: (
            -item["allocated_bytes"]
            if isinstance(item["allocated_bytes"], int)
            else 1,
            item["path"],
        )
    )

    report = {
        "schema_version": SCHEMA_VERSION,
        "kind": REPORT_KIND,
        "generated_at": format_time(now),
        "status": "complete" if complete else "partial",
        "duration_seconds": round(time.monotonic() - started, 3),
        "jobs": jobs,
        "top": top,
        "timeout_seconds": timeout_seconds,
        "scope": scope,
        "scope_sha256": scope_sha256(scope),
        "filesystem": filesystem_report(filesystem_path),
        "scan_roots": rendered_roots,
        "focused_paths": rendered_focused,
        "notes": [
            "Quick audit is read-only and grants no cleanup authority.",
            "Each discovered child and focused path is measured at most once per run.",
            "Allocated sizes are directional on copy-on-write or sparse filesystems.",
            "Run exact-path activity checks only for reviewed cleanup candidates.",
        ],
    }
    add_deltas(report, previous, previous_path)
    return report


def cleanup_candidates(report: dict[str, Any]) -> list[dict[str, Any]]:
    entries = list(report["focused_paths"])
    for root in report["scan_roots"]:
        entries.extend(root["top_entries"])
    candidates = []
    seen = set()
    for entry in entries:
        path = Path(entry["path"])
        if str(path) in seen or entry.get("status") != "measured":
            continue
        seen.add(str(path))
        marker = path / ".dev-disk-budget-owner.json"
        host_marker = path.with_name(f".{path.name}.dev-disk-budget-host-cache.json")
        if path.is_symlink():
            category, next_check = "unknown", "resolve path identity without following deletion targets"
        elif marker.is_file() and not marker.is_symlink():
            category, next_check = "owned_marker_present", "locate and validate the authoritative manifest, terminal state, and live activity"
        elif host_marker.is_file() and not host_marker.is_symlink():
            category, next_check = "shared_cache", "validate cache binding and all users before separately authorized native GC"
        elif path.name in {"sessions", "cli", ".claude", ".codex"}:
            category, next_check = "agent_state", "resolve active and resumable sessions and product retention before archival"
        elif path.name in {"VM", "Preboot", "Recovery"}:
            category, next_check = "system_managed", "inspect with OS tools; never manually delete"
        else:
            category, next_check = "unknown", "establish owner, retention, and current usage; name and age are not authority"
        candidates.append({
            "path": str(path), "allocated_bytes": entry["allocated_bytes"],
            "classification": category, "next_check": next_check,
            "cleanup_authorized": False, "reclaimable_bytes": None,
        })
    return sorted(candidates, key=lambda item: (-item["allocated_bytes"], item["path"]))


def write_output(document: dict[str, Any], output: str | None) -> None:
    rendered = json.dumps(document, indent=2, sort_keys=True) + "\n"
    if output is None or output == "-":
        sys.stdout.write(rendered)
        return
    path = audit_path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.new")
    try:
        temporary.write_text(rendered, encoding="utf-8")
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(
        description="Run a bounded read-only disk audit."
    )
    command.add_argument(
        "--path",
        default=".",
        help="Filesystem path used for total/free capacity.",
    )
    command.add_argument(
        "--scan-root",
        action="append",
        default=[],
        help="Measure each immediate child once; repeat for multiple roots.",
    )
    command.add_argument(
        "--focus",
        action="append",
        default=[],
        help="Measure one known path without listing its descendants.",
    )
    command.add_argument(
        "--group",
        action="append",
        default=[],
        help="Group scan-root children using NAME=REGEX; first match wins.",
    )
    command.add_argument("--jobs", type=int, default=DEFAULT_JOBS)
    command.add_argument("--top", type=int, default=DEFAULT_TOP)
    command.add_argument(
        "--timeout-seconds",
        type=int,
        default=DEFAULT_TIMEOUT_SECONDS,
    )
    command.add_argument(
        "--previous",
        help="Prior report with the same scope, used to calculate deltas.",
    )
    command.add_argument("--cleanup-candidates", action="store_true", help="Classify measured Top N for review without granting deletion authority.")
    command.add_argument("--output", help="Write JSON to this path.")
    return command


def main() -> int:
    arguments = parser().parse_args()
    previous_path = audit_path(arguments.previous) if arguments.previous else None
    try:
        result = quick_audit(
            audit_path(arguments.path),
            scan_roots=arguments.scan_root,
            focused_paths=arguments.focus,
            raw_groups=arguments.group,
            jobs=arguments.jobs,
            top=arguments.top,
            timeout_seconds=arguments.timeout_seconds,
            previous=load_previous(previous_path),
            previous_path=previous_path,
            now=utc_now(),
        )
        if arguments.cleanup_candidates:
            result["cleanup_candidates"] = cleanup_candidates(result)
        write_output(result, arguments.output)
        return 0 if result["status"] == "complete" else 1
    except (AuditError, OSError, json.JSONDecodeError) as error:
        error_document = {
            "schema_version": SCHEMA_VERSION,
            "kind": "dev-disk-budget-quick-audit-error",
            "errors": [str(error)],
        }
        try:
            write_output(error_document, arguments.output)
        except OSError as output_error:
            error_document["errors"].append(
                f"cannot write requested output {arguments.output}: {output_error}"
            )
            write_output(error_document, None)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
