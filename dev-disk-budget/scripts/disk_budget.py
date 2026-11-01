#!/usr/bin/env python3
"""Audit and clean explicitly owned parallel-workspace resources."""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterator


SCHEMA_VERSION = 1
MANIFEST_KIND = "dev-disk-budget-run"
PLAN_KIND = "dev-disk-budget-plan"
RECEIPT_KIND = "dev-disk-budget-cleanup-receipt"
MARKER_KIND = "dev-disk-budget-owner"
MARKER_NAME = ".dev-disk-budget-owner.json"
HOST_CACHE_MARKER_SUFFIX = ".dev-disk-budget-host-cache.json"
RESERVATION_KIND = "dev-disk-budget-worktree-reservation"
RESERVATION_STATUS_KIND = "dev-disk-budget-reservation-status"
RUN_STATUSES = {
    "planning",
    "active",
    "succeeded",
    "failed",
    "interrupted",
    "released",
}
RESOURCE_KINDS = {"owned_directory", "git_worktree"}
CLEANUP_POLICIES = {"delete_when_released", "report_only"}
DIRECTORY_ROLES = {"artifact", "generic", "go_build_cache", "go_tmp"}
DIRECTORY_SCOPES = {"session_shared", "task", "wave_shared"}
ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
DEFAULT_MIN_FREE_BYTES = 20 * 1024**3
DEFAULT_MIN_FREE_PERCENT = 5.0
DEFAULT_MIN_AGE_SECONDS = 300
DEFAULT_PLAN_TTL_SECONDS = 900


class HygieneError(RuntimeError):
    """Raised when a disk-budget safety contract is violated."""


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def format_time(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def parse_time(value: Any, field: str) -> dt.datetime:
    if not isinstance(value, str) or not value.strip():
        raise HygieneError(f"{field} must be a non-empty RFC3339 timestamp")
    candidate = value.strip().replace("Z", "+00:00")
    try:
        parsed = dt.datetime.fromisoformat(candidate)
    except ValueError as error:
        raise HygieneError(f"{field} must be an RFC3339 timestamp") from error
    if parsed.tzinfo is None:
        raise HygieneError(f"{field} must include a timezone")
    return parsed.astimezone(dt.timezone.utc)


def identifier(value: Any, field: str) -> str:
    if not isinstance(value, str) or not ID_PATTERN.fullmatch(value):
        raise HygieneError(
            f"{field} must use lowercase letters, digits, '.', '_', or '-'"
        )
    return value


def nonempty_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise HygieneError(f"{field} must be a non-empty string")
    return value.strip()


def canonical_path(raw_path: str | Path, *, must_exist: bool = False) -> Path:
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    if candidate.is_symlink():
        raise HygieneError(f"resource path must not be a symlink: {candidate}")
    try:
        return candidate.resolve(strict=must_exist)
    except OSError as error:
        raise HygieneError(f"cannot resolve path {candidate}: {error}") from error


def is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def validate_removal_root(path: Path) -> None:
    anchors = {Path(path.anchor), Path.home().resolve()}
    if path in anchors or len(path.parts) < 3:
        raise HygieneError(f"refusing unsafe resource root: {path}")


def is_git_marker(candidate: Path) -> bool:
    """True when ``candidate`` (a ``.git`` path) can belong to a repository.

    Agent sandboxes (bubblewrap-based) mount an empty, read-only ``.git``
    directory at every writable root, and older runs left such empty
    directories behind on the host (for example in the system temp root). Git itself does not treat
    an empty directory or a non-``gitdir:`` file as a repository, so neither is
    an ownership boundary. Anything unreadable, symlinked, or non-empty stays a
    boundary (conservative).
    """
    try:
        metadata = candidate.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        return True
    if stat.S_ISLNK(metadata.st_mode):
        return True
    if stat.S_ISDIR(metadata.st_mode):
        try:
            with os.scandir(candidate) as entries:
                return any(True for _ in entries)
        except OSError:
            return True
    if stat.S_ISREG(metadata.st_mode):
        try:
            with open(candidate, "rb") as stream:
                return stream.read(7) == b"gitdir:"
        except OSError:
            return True
    return False  # character device (/dev/null bind) or other placeholder


def reject_ancestor_ownership_boundaries(path: Path) -> None:
    current = path.parent
    while current != current.parent:
        if (current / MARKER_NAME).exists():
            raise HygieneError(
                f"resource path is nested under another owned root: {current}"
            )
        if is_git_marker(current / ".git"):
            raise HygieneError(
                f"resource path is nested inside an existing Git worktree: {current}"
            )
        current = current.parent


def path_identity(path: Path) -> dict[str, int]:
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode):
        raise HygieneError(f"resource path became a symlink: {path}")
    return {
        "device": int(metadata.st_dev),
        "inode": int(metadata.st_ino),
        "uid": int(metadata.st_uid),
    }


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def sha256_file(path: Path) -> str:
    try:
        return sha256_bytes(path.read_bytes())
    except OSError as error:
        raise HygieneError(f"cannot hash {path}: {error}") from error


def atomic_write_json(path: Path, document: Any, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.new")
    rendered = json.dumps(document, indent=2, sort_keys=True) + "\n"
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            mode,
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(rendered)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()


def exclusive_write_json(path: Path, document: Any, mode: int = 0o600) -> None:
    rendered = json.dumps(document, indent=2, sort_keys=True) + "\n"
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            mode,
        )
    except FileExistsError as error:
        raise HygieneError(f"reservation already exists: {path}") from error
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(rendered)
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        with contextlib.suppress(OSError):
            path.unlink()
        raise


@contextlib.contextmanager
def manifest_lock(manifest_path: Path) -> Iterator[None]:
    lock_path = manifest_path.with_name(f".{manifest_path.name}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def load_json(path: Path, field: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise HygieneError(f"{field} does not exist: {path}") from error
    except (OSError, json.JSONDecodeError) as error:
        raise HygieneError(f"cannot read {field} {path}: {error}") from error


def validate_manifest(
    document: Any,
    *,
    allow_reserved_worktree_target_symlink: bool = False,
    allow_reserved_worktree_reservation_symlink: bool = False,
) -> dict[str, Any]:
    if not isinstance(document, dict):
        raise HygieneError("manifest must be an object")
    if document.get("schema_version") != SCHEMA_VERSION:
        raise HygieneError(f"manifest.schema_version must be {SCHEMA_VERSION}")
    if document.get("kind") != MANIFEST_KIND:
        raise HygieneError(f"manifest.kind must be {MANIFEST_KIND}")
    run_id = identifier(document.get("run_id"), "manifest.run_id")
    status = nonempty_string(document.get("status"), "manifest.status")
    if status not in RUN_STATUSES:
        raise HygieneError(f"manifest.status must be one of {sorted(RUN_STATUSES)}")
    parse_time(document.get("created_at"), "manifest.created_at")
    updated_at = parse_time(document.get("updated_at"), "manifest.updated_at")
    resources = document.get("resources")
    if not isinstance(resources, list):
        raise HygieneError("manifest.resources must be an array")

    normalized_resources: list[dict[str, Any]] = []
    resource_ids: set[str] = set()
    resource_paths: list[tuple[str, Path]] = []
    for index, raw_resource in enumerate(resources):
        field = f"manifest.resources[{index}]"
        if not isinstance(raw_resource, dict):
            raise HygieneError(f"{field} must be an object")
        resource_id = identifier(raw_resource.get("id"), f"{field}.id")
        if resource_id in resource_ids:
            raise HygieneError(f"manifest contains duplicate resource id '{resource_id}'")
        resource_ids.add(resource_id)
        kind = nonempty_string(raw_resource.get("kind"), f"{field}.kind")
        if kind not in RESOURCE_KINDS:
            raise HygieneError(f"{field}.kind must be one of {sorted(RESOURCE_KINDS)}")
        policy = nonempty_string(
            raw_resource.get("cleanup_policy"), f"{field}.cleanup_policy"
        )
        if policy not in CLEANUP_POLICIES:
            raise HygieneError(
                f"{field}.cleanup_policy must be one of {sorted(CLEANUP_POLICIES)}"
            )
        purpose = nonempty_string(raw_resource.get("purpose"), f"{field}.purpose")
        claimed_at = parse_time(raw_resource.get("claimed_at"), f"{field}.claimed_at")
        if claimed_at > updated_at:
            raise HygieneError(f"{field}.claimed_at must not exceed manifest.updated_at")
        raw_path = nonempty_string(raw_resource.get("path"), f"{field}.path")
        requested_path = Path(raw_path).expanduser()
        if not requested_path.is_absolute():
            requested_path = Path.cwd() / requested_path
        allow_leaf_symlink = (
            allow_reserved_worktree_target_symlink
            and kind == "git_worktree"
            and raw_resource.get("registration_state") == "reserved"
            and requested_path.is_symlink()
        )
        path = (
            requested_path.parent.resolve(strict=False) / requested_path.name
            if allow_leaf_symlink
            else canonical_path(requested_path)
        )
        validate_removal_root(path)
        for other_id, other_path in resource_paths:
            if is_within(path, other_path) or is_within(other_path, path):
                raise HygieneError(
                    f"resource paths overlap: '{resource_id}' and '{other_id}'"
                )
        resource_paths.append((resource_id, path))

        normalized = {
            "id": resource_id,
            "kind": kind,
            "path": str(path),
            "purpose": purpose,
            "cleanup_policy": policy,
            "claimed_at": format_time(claimed_at),
        }
        if kind == "owned_directory":
            claimed_identity = raw_resource.get("claimed_identity")
            if (
                not isinstance(claimed_identity, dict)
                or not isinstance(claimed_identity.get("device"), int)
                or not isinstance(claimed_identity.get("inode"), int)
                or not isinstance(claimed_identity.get("uid"), int)
            ):
                raise HygieneError(
                    f"{field}.claimed_identity must contain integer device/inode/uid"
                )
            normalized["claimed_identity"] = {
                "device": claimed_identity["device"],
                "inode": claimed_identity["inode"],
                "uid": claimed_identity["uid"],
            }
            marker_path = canonical_path(
                nonempty_string(raw_resource.get("marker_path"), f"{field}.marker_path")
            )
            if marker_path != path / MARKER_NAME:
                raise HygieneError(f"{field}.marker_path must be the ownership marker")
            normalized["marker_path"] = str(marker_path)
            lifecycle_fields = {
                "role",
                "scope",
                "task_id",
            } & set(raw_resource)
            if lifecycle_fields:
                role = nonempty_string(raw_resource.get("role"), f"{field}.role")
                if role not in DIRECTORY_ROLES:
                    raise HygieneError(
                        f"{field}.role must be one of {sorted(DIRECTORY_ROLES)}"
                    )
                scope = nonempty_string(raw_resource.get("scope"), f"{field}.scope")
                if scope not in DIRECTORY_SCOPES:
                    raise HygieneError(
                        f"{field}.scope must be one of {sorted(DIRECTORY_SCOPES)}"
                    )
                task_id_value = raw_resource.get("task_id")
                task_id = (
                    identifier(task_id_value, f"{field}.task_id")
                    if task_id_value is not None
                    else None
                )
                if scope == "task" and task_id is None:
                    raise HygieneError(
                        f"{field}.task_id is required for task-scoped directories"
                    )
                if scope != "task" and task_id is not None:
                    raise HygieneError(
                        f"{field}.task_id is allowed only for task-scoped directories"
                    )
                if role == "go_build_cache" and scope != "session_shared":
                    raise HygieneError(
                        f"{field} go_build_cache must be session_shared"
                    )
                if role == "go_tmp" and scope != "task":
                    raise HygieneError(f"{field} go_tmp must be task-scoped")
                normalized.update(
                    {
                        "role": role,
                        "scope": scope,
                        "task_id": task_id,
                    }
                )
        else:
            repo = canonical_path(
                nonempty_string(raw_resource.get("repo"), f"{field}.repo")
            )
            task_id = identifier(raw_resource.get("task_id"), f"{field}.task_id")
            registration_state = nonempty_string(
                raw_resource.get("registration_state"),
                f"{field}.registration_state",
            )
            if registration_state not in {"reserved", "bound"}:
                raise HygieneError(
                    f"{field}.registration_state must be 'reserved' or 'bound'"
                )
            cleanup_authorized = raw_resource.get("cleanup_authorized")
            if not isinstance(cleanup_authorized, bool):
                raise HygieneError(f"{field}.cleanup_authorized must be boolean")
            raw_reservation_path = nonempty_string(
                raw_resource.get("reservation_path"),
                f"{field}.reservation_path",
            )
            requested_reservation_path = Path(raw_reservation_path).expanduser()
            if not requested_reservation_path.is_absolute():
                requested_reservation_path = Path.cwd() / requested_reservation_path
            allow_reservation_symlink = (
                allow_reserved_worktree_reservation_symlink
                and registration_state == "reserved"
                and requested_reservation_path.is_symlink()
            )
            reservation_path = (
                requested_reservation_path.parent.resolve(strict=False)
                / requested_reservation_path.name
                if allow_reservation_symlink
                else canonical_path(requested_reservation_path)
            )
            if reservation_path != reservation_path_for(path):
                raise HygieneError(
                    f"{field}.reservation_path does not match worktree path"
                )
            normalized.update(
                {
                    "repo": str(repo),
                    "task_id": task_id,
                    "reservation_path": str(reservation_path),
                    "registration_state": registration_state,
                    "claimed_identity": None,
                    "initial_head": None,
                    "bound_at": None,
                    "cleanup_authorized": cleanup_authorized,
                    "expected_head": None,
                    "preservation_ref": None,
                    "handoff_path": None,
                    "handoff_sha256": None,
                    "authorized_at": None,
                }
            )
            if registration_state == "reserved":
                if cleanup_authorized:
                    raise HygieneError(
                        f"{field} cannot authorize cleanup before worktree binding"
                    )
            else:
                claimed_identity = raw_resource.get("claimed_identity")
                if (
                    not isinstance(claimed_identity, dict)
                    or not isinstance(claimed_identity.get("device"), int)
                    or not isinstance(claimed_identity.get("inode"), int)
                    or not isinstance(claimed_identity.get("uid"), int)
                ):
                    raise HygieneError(
                        f"{field}.claimed_identity must contain integer device/inode/uid"
                    )
                initial_head = nonempty_string(
                    raw_resource.get("initial_head"), f"{field}.initial_head"
                ).lower()
                if not SHA_PATTERN.fullmatch(initial_head):
                    raise HygieneError(f"{field}.initial_head must be a full Git SHA")
                bound_at = parse_time(raw_resource.get("bound_at"), f"{field}.bound_at")
                normalized.update(
                    {
                        "claimed_identity": {
                            "device": claimed_identity["device"],
                            "inode": claimed_identity["inode"],
                            "uid": claimed_identity["uid"],
                        },
                        "initial_head": initial_head,
                        "bound_at": format_time(bound_at),
                    }
                )
            if cleanup_authorized:
                expected_head = nonempty_string(
                    raw_resource.get("expected_head"), f"{field}.expected_head"
                ).lower()
                if not SHA_PATTERN.fullmatch(expected_head):
                    raise HygieneError(f"{field}.expected_head must be a full Git SHA")
                preservation_ref = nonempty_string(
                    raw_resource.get("preservation_ref"), f"{field}.preservation_ref"
                )
                handoff_path = canonical_path(
                    nonempty_string(
                        raw_resource.get("handoff_path"), f"{field}.handoff_path"
                    )
                )
                handoff_sha256 = nonempty_string(
                    raw_resource.get("handoff_sha256"), f"{field}.handoff_sha256"
                )
                if not SHA256_PATTERN.fullmatch(handoff_sha256):
                    raise HygieneError(f"{field}.handoff_sha256 must be a SHA-256")
                authorized_at = parse_time(
                    raw_resource.get("authorized_at"), f"{field}.authorized_at"
                )
                normalized.update(
                    {
                        "expected_head": expected_head,
                        "preservation_ref": preservation_ref,
                        "handoff_path": str(handoff_path),
                        "handoff_sha256": handoff_sha256,
                        "authorized_at": format_time(authorized_at),
                    }
                )
        normalized_resources.append(normalized)

    normalized_manifest = {
        "schema_version": SCHEMA_VERSION,
        "kind": MANIFEST_KIND,
        "run_id": run_id,
        "status": status,
        "created_at": format_time(
            parse_time(document.get("created_at"), "manifest.created_at")
        ),
        "updated_at": format_time(updated_at),
        "resources": sorted(normalized_resources, key=lambda item: item["id"]),
        "resource_set_sha256": document.get("resource_set_sha256"),
    }
    expected_resource_set_hash = resource_set_sha256(normalized_manifest)
    observed_resource_set_hash = normalized_manifest["resource_set_sha256"]
    if status == "planning":
        if observed_resource_set_hash is not None:
            raise HygieneError(
                "planning manifests must not contain a sealed resource-set hash"
            )
    elif (
        not isinstance(observed_resource_set_hash, str)
        or not SHA256_PATTERN.fullmatch(observed_resource_set_hash)
        or observed_resource_set_hash != expected_resource_set_hash
    ):
        raise HygieneError("sealed manifest resource set changed after run start")
    return normalized_manifest


def resource_set_sha256(manifest: dict[str, Any]) -> str:
    resources: list[dict[str, Any]] = []
    for resource in manifest["resources"]:
        item = {
            key: resource[key]
            for key in (
                "id",
                "kind",
                "path",
                "purpose",
                "cleanup_policy",
                "claimed_at",
            )
        }
        if resource["kind"] == "owned_directory":
            item["claimed_identity"] = resource["claimed_identity"]
            item["marker_path"] = resource["marker_path"]
            for key in ("role", "scope", "task_id"):
                if key in resource:
                    item[key] = resource[key]
        else:
            item["repo"] = resource["repo"]
            item["task_id"] = resource["task_id"]
            item["reservation_path"] = resource["reservation_path"]
            item["registration_state"] = resource["registration_state"]
            item["claimed_identity"] = resource["claimed_identity"]
            item["initial_head"] = resource["initial_head"]
            item["bound_at"] = resource["bound_at"]
        resources.append(item)
    encoded = json.dumps(
        sorted(resources, key=lambda item: item["id"]),
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return sha256_bytes(encoded)


def load_manifest(path: Path) -> dict[str, Any]:
    return validate_manifest(load_json(path, "manifest"))


def save_manifest(
    path: Path,
    manifest: dict[str, Any],
    now: dt.datetime,
) -> None:
    manifest["updated_at"] = format_time(now)
    atomic_write_json(path, validate_manifest(manifest))


def run_command(
    arguments: list[str],
    *,
    timeout: int = 30,
    acceptable: set[int] | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        completed = subprocess.run(
            arguments,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise HygieneError(f"command failed: {' '.join(arguments)}: {error}") from error
    allowed = acceptable if acceptable is not None else {0}
    if completed.returncode not in allowed:
        detail = completed.stderr.strip() or completed.stdout.strip() or "no output"
        raise HygieneError(
            f"command returned {completed.returncode}: {' '.join(arguments)}: {detail}"
        )
    return completed


def git(repo: Path, *arguments: str, acceptable: set[int] | None = None) -> str:
    executable = shutil.which("git")
    if executable is None:
        raise HygieneError("git executable is unavailable")
    completed = run_command(
        [executable, "-C", str(repo), *arguments],
        acceptable=acceptable,
    )
    return completed.stdout.strip()


def git_worktrees(repo: Path) -> dict[Path, dict[str, str]]:
    output = git(repo, "worktree", "list", "--porcelain")
    entries: dict[Path, dict[str, str]] = {}
    current: dict[str, str] = {}
    for line in [*output.splitlines(), ""]:
        if not line:
            if current:
                path = canonical_path(current["worktree"])
                entries[path] = current
                current = {}
            continue
        key, _, value = line.partition(" ")
        current[key] = value
    return entries


def current_git_head(worktree: Path) -> str:
    head = git(worktree, "rev-parse", "HEAD").lower()
    if not SHA_PATTERN.fullmatch(head):
        raise HygieneError(f"Git returned an invalid HEAD for {worktree}")
    return head


def git_status_clean(worktree: Path) -> bool:
    return not git(
        worktree,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
        "--ignored=matching",
    )


def verify_preservation(repo: Path, head: str, preservation_ref: str) -> str:
    if not preservation_ref.startswith("refs/"):
        raise HygieneError(
            "preservation_ref must be a named ref under refs/, not a symbolic "
            "name or raw SHA"
        )
    resolved = git(repo, "rev-parse", "--verify", f"{preservation_ref}^{{commit}}").lower()
    if not SHA_PATTERN.fullmatch(resolved):
        raise HygieneError(f"preservation ref did not resolve to a commit: {preservation_ref}")
    executable = shutil.which("git")
    if executable is None:
        raise HygieneError("git executable is unavailable")
    run_command(
        [executable, "-C", str(repo), "merge-base", "--is-ancestor", head, resolved],
        acceptable={0},
    )
    return resolved


def verify_handoff(
    path: Path,
    task_id: str,
    head: str,
    repo: Path,
    preservation_ref: str,
) -> str:
    content = path.read_bytes()
    try:
        handoff = json.loads(content)
    except json.JSONDecodeError as error:
        raise HygieneError(f"cannot parse orchestrator handoff {path}: {error}") from error
    if (
        not isinstance(handoff, dict)
        or handoff.get("schema_version") != 1
        or handoff.get("kind") != "parallel-change-handoff"
        or handoff.get("status") != "ready"
        or handoff.get("blockers") != []
    ):
        raise HygieneError("orchestrator handoff is not ready and blocker-free")
    workers = handoff.get("workers")
    if not isinstance(workers, list):
        raise HygieneError("orchestrator handoff workers must be an array")
    matches = [
        worker
        for worker in workers
        if isinstance(worker, dict) and worker.get("task_id") == task_id
    ]
    if len(matches) != 1:
        raise HygieneError(
            f"orchestrator handoff must contain exactly one worker for task '{task_id}'"
        )
    worker = matches[0]
    cleanup = worker.get("cleanup")
    preservation = worker.get("preservation")
    if (
        worker.get("accepted") is not True
        or worker.get("commit_sha") != head
        or not isinstance(cleanup, dict)
        or cleanup.get("allowed") is not True
        or cleanup.get("reasons") != []
        or not isinstance(preservation, dict)
        or preservation.get("kind") not in {"integrated", "archived"}
        or preservation.get("contains_head") is not True
        or not isinstance(preservation.get("locator"), str)
        or not preservation["locator"]
        or not isinstance(preservation.get("observed_at"), str)
        or not preservation["observed_at"]
    ):
        raise HygieneError(
            f"orchestrator handoff does not authorize cleanup for task '{task_id}'"
        )
    parse_time(preservation["observed_at"], "orchestrator preservation observed_at")
    if preservation["kind"] == "archived":
        if preservation["locator"] != preservation_ref:
            raise HygieneError(
                "orchestrator archive locator does not match preservation_ref"
            )
    else:
        locator = preservation["locator"].lower()
        if not SHA_PATTERN.fullmatch(locator):
            raise HygieneError(
                "orchestrator integrated preservation locator must be a full SHA"
            )
        git(repo, "cat-file", "-e", f"{locator}^{{commit}}")
        executable = shutil.which("git")
        if executable is None:
            raise HygieneError("git executable is unavailable")
        run_command(
            [
                executable,
                "-C",
                str(repo),
                "merge-base",
                "--is-ancestor",
                head,
                locator,
            ],
            acceptable={0},
        )
    return sha256_bytes(content)


def directory_snapshot(
    path: Path,
    *,
    reject_symlinks: bool,
    enforce_ownership_boundary: bool = True,
) -> tuple[dict[str, int], str]:
    logical = 0
    allocated = 0
    digest = hashlib.sha256()
    root_metadata = path.lstat()
    expected_device = root_metadata.st_dev
    expected_uid = os.getuid()
    if enforce_ownership_boundary and root_metadata.st_uid != expected_uid:
        raise HygieneError(f"resource root is not owned by current uid: {path}")

    def raise_walk_error(error: OSError) -> None:
        raise HygieneError(f"cannot inspect resource tree {path}: {error}")

    try:
        for root, directories, files in os.walk(
            path,
            followlinks=False,
            onerror=raise_walk_error,
        ):
            directories.sort()
            files.sort()
            root_path = Path(root)
            for name in [*directories, *files]:
                entry = root_path / name
                try:
                    metadata = entry.lstat()
                except FileNotFoundError:
                    continue
                if reject_symlinks and stat.S_ISLNK(metadata.st_mode):
                    raise HygieneError(
                        f"resource tree contains a symlink: {entry}"
                    )
                if (
                    enforce_ownership_boundary
                    and metadata.st_dev != expected_device
                ):
                    raise HygieneError(
                        f"resource tree crosses a filesystem boundary: {entry}"
                    )
                if enforce_ownership_boundary and metadata.st_uid != expected_uid:
                    raise HygieneError(
                        f"resource tree contains a different owner: {entry}"
                    )
                logical += int(metadata.st_size)
                allocated += int(getattr(metadata, "st_blocks", 0)) * 512
                relative = entry.relative_to(path).as_posix()
                if reject_symlinks and relative != MARKER_NAME and (
                    entry.name == MARKER_NAME or entry.name == ".git"
                ):
                    raise HygieneError(
                        f"resource tree contains a nested ownership boundary: {entry}"
                    )
                record = "\0".join(
                    (
                        relative,
                        str(stat.S_IFMT(metadata.st_mode)),
                        str(metadata.st_dev),
                        str(metadata.st_ino),
                        str(metadata.st_size),
                        str(metadata.st_mtime_ns),
                    )
                )
                digest.update(record.encode("utf-8", errors="surrogateescape"))
                digest.update(b"\0")
    except OSError as error:
        raise HygieneError(f"cannot measure {path}: {error}") from error
    return (
        {"logical_bytes": logical, "allocated_bytes": allocated},
        digest.hexdigest(),
    )


def active_handle_check(path: Path) -> tuple[bool | None, str]:
    executable = shutil.which("lsof")
    if executable is None:
        return None, "lsof is unavailable"
    try:
        completed = subprocess.run(
            [executable, "+D", str(path)],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return None, f"lsof activity check failed: {error}"
    if completed.returncode == 0:
        return True, "lsof found an open handle or working directory"
    if completed.returncode == 1 and not completed.stderr.strip():
        return False, "lsof found no open handles"
    detail = completed.stderr.strip() or completed.stdout.strip() or "no output"
    return None, f"lsof activity check was inconclusive: {detail}"


def marker_document(run_id: str, resource_id: str, path: Path) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": MARKER_KIND,
        "run_id": run_id,
        "resource_id": resource_id,
        "canonical_path": str(path),
    }


def reservation_path_for(path: Path) -> Path:
    return path.with_name(
        f".{path.name}.dev-disk-budget-reservation.json"
    )


def reservation_document(
    run_id: str,
    resource_id: str,
    task_id: str,
    repo: Path,
    path: Path,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": RESERVATION_KIND,
        "run_id": run_id,
        "resource_id": resource_id,
        "task_id": task_id,
        "repo": str(repo),
        "canonical_path": str(path),
    }


def validate_reservation(
    reservation_path: Path,
    run_id: str,
    resource_id: str,
    task_id: str,
    repo: Path,
    path: Path,
) -> str:
    expected = reservation_document(
        run_id,
        resource_id,
        task_id,
        repo,
        path,
    )
    observed = load_json(reservation_path, "worktree reservation")
    if observed != expected:
        raise HygieneError(
            f"worktree reservation does not match manifest: {reservation_path}"
        )
    return sha256_file(reservation_path)


def validate_marker(
    marker_path: Path,
    run_id: str,
    resource_id: str,
    path: Path,
) -> str:
    expected = marker_document(run_id, resource_id, path)
    observed = load_json(marker_path, "ownership marker")
    if observed != expected:
        raise HygieneError(f"ownership marker does not match manifest: {marker_path}")
    return sha256_file(marker_path)


def init_manifest(path: Path, run_id: str, now: dt.datetime) -> dict[str, Any]:
    identifier(run_id, "run_id")
    if path.exists():
        raise HygieneError(f"manifest already exists: {path}")
    document = {
        "schema_version": SCHEMA_VERSION,
        "kind": MANIFEST_KIND,
        "run_id": run_id,
        "status": "planning",
        "created_at": format_time(now),
        "updated_at": format_time(now),
        "resources": [],
        "resource_set_sha256": None,
    }
    atomic_write_json(path, document)
    return document


def resource_by_id(manifest: dict[str, Any], resource_id: str) -> dict[str, Any]:
    matches = [
        resource for resource in manifest["resources"] if resource["id"] == resource_id
    ]
    if not matches:
        raise HygieneError(f"resource is not registered: {resource_id}")
    return matches[0]


def matching_owned_directories(
    manifest: dict[str, Any],
    *,
    role: str,
    scope: str,
    task_id: str | None,
) -> list[dict[str, Any]]:
    return [
        resource
        for resource in manifest["resources"]
        if resource["kind"] == "owned_directory"
        and resource.get("role") == role
        and resource.get("scope") == scope
        and resource.get("task_id") == task_id
    ]


def validate_live_owned_directory(
    manifest: dict[str, Any],
    resource: dict[str, Any],
) -> None:
    path = Path(resource["path"])
    if not path.is_dir() or path.is_symlink():
        raise HygieneError(f"owned directory is unavailable: {path}")
    if path_identity(path) != resource["claimed_identity"]:
        raise HygieneError(f"owned directory identity changed: {path}")
    validate_marker(
        Path(resource["marker_path"]),
        manifest["run_id"],
        resource["id"],
        path,
    )


def host_cache_marker_path(path: Path) -> Path:
    return path.parent / f".{path.name}{HOST_CACHE_MARKER_SUFFIX}"


def validate_host_shared_go_cache(
    raw_path: str | Path,
    wave_manifest: dict[str, Any],
) -> tuple[Path, dict[str, int]]:
    path = canonical_path(raw_path, must_exist=True)
    if not path.is_dir():
        raise HygieneError(f"host-shared GOCACHE is not a directory: {path}")
    reject_ancestor_ownership_boundaries(path)
    if (path / MARKER_NAME).exists():
        raise HygieneError(
            "host-shared GOCACHE must not be owned by a disposable run manifest"
        )
    identity = path_identity(path)
    if identity["uid"] != os.getuid():
        raise HygieneError("host-shared GOCACHE is not owned by the current uid")
    marker_path = host_cache_marker_path(path)
    marker = load_json(marker_path, "host-shared Go cache marker")
    if marker.get("schema_version") != SCHEMA_VERSION:
        raise HygieneError("host-shared Go cache marker schema_version is invalid")
    if marker.get("kind") != "dev-disk-budget-host-go-cache":
        raise HygieneError("host-shared Go cache marker kind is invalid")
    if marker.get("path") != str(path):
        raise HygieneError("host-shared Go cache marker path does not match")
    if marker.get("identity") != identity:
        raise HygieneError("host-shared Go cache marker identity changed")
    for resource in wave_manifest["resources"]:
        disposable_path = Path(resource["path"])
        if is_within(path, disposable_path) or is_within(disposable_path, path):
            raise HygieneError(
                "host-shared GOCACHE must be outside every wave resource"
            )
    return path, identity


def init_host_go_cache(raw_path: str | Path, now: dt.datetime) -> dict[str, Any]:
    requested = Path(raw_path).expanduser()
    if not requested.is_absolute():
        requested = Path.cwd() / requested
    if requested.is_symlink():
        raise HygieneError(f"host-shared GOCACHE path must not be a symlink: {requested}")
    path = requested.resolve(strict=False)
    validate_removal_root(path)
    reject_ancestor_ownership_boundaries(path)
    if path.exists() or path.is_symlink():
        raise HygieneError(
            "refusing to adopt an existing host-shared GOCACHE; choose a new path"
        )
    if not path.parent.is_dir():
        raise HygieneError(f"host-shared GOCACHE parent must exist: {path.parent}")
    try:
        path.mkdir(mode=0o700)
    except FileExistsError as error:
        raise HygieneError(
            "host-shared GOCACHE was created concurrently; inspect and reuse "
            "only when its marker is valid"
        ) from error
    marker_path = host_cache_marker_path(path)
    marker = {
        "schema_version": SCHEMA_VERSION,
        "kind": "dev-disk-budget-host-go-cache",
        "path": str(path),
        "identity": path_identity(path),
        "created_at": format_time(now),
        "cleanup": "report_only_native_gc",
    }
    try:
        exclusive_write_json(marker_path, marker)
    except HygieneError as error:
        with contextlib.suppress(OSError):
            path.rmdir()
        raise HygieneError(
            f"host-shared Go cache marker already exists: {marker_path}"
        ) from error
    except Exception:
        with contextlib.suppress(OSError):
            path.rmdir()
        raise
    return {
        **marker,
        "marker_path": str(marker_path),
        "environment": {"GOCACHE": str(path)},
    }


def build_go_environment(
    session_manifest_path: Path | None,
    wave_manifest_path: Path,
    task_id: str,
    host_gocache: str | Path | None = None,
) -> dict[str, Any]:
    identifier(task_id, "task_id")
    wave_manifest = load_manifest(wave_manifest_path)
    if wave_manifest["status"] != "active":
        raise HygieneError("wave manifest must be active before rendering Go env")
    if (session_manifest_path is None) == (host_gocache is None):
        raise HygieneError(
            "select exactly one GOCACHE source: session manifest or host-shared path"
        )
    session_manifest: dict[str, Any] | None = None
    build_cache_path: Path
    build_cache_identity: dict[str, int]
    if session_manifest_path is not None:
        session_manifest = load_manifest(session_manifest_path)
        if session_manifest["status"] != "active":
            raise HygieneError(
                "session manifest must be active before rendering Go env"
            )
        if session_manifest["run_id"] == wave_manifest["run_id"]:
            raise HygieneError("session and wave manifests must be distinct runs")
        build_caches = matching_owned_directories(
            session_manifest,
            role="go_build_cache",
            scope="session_shared",
            task_id=None,
        )
        if len(build_caches) != 1:
            raise HygieneError(
                "session manifest must contain exactly one session_shared "
                "go_build_cache"
            )
        build_cache = build_caches[0]
        validate_live_owned_directory(session_manifest, build_cache)
        build_cache_path = Path(build_cache["path"])
        build_cache_identity = build_cache["claimed_identity"]
        gocache_source = "session_owned"
        gocache_lifecycle = (
            "release only after every wave in the session is terminal"
        )
    else:
        build_cache_path, build_cache_identity = validate_host_shared_go_cache(
            host_gocache,
            wave_manifest,
        )
        gocache_source = "operator_managed_host_shared"
        gocache_lifecycle = (
            "operator-managed host-shared cache; never automatically cleaned"
        )
    task_tmpdirs = matching_owned_directories(
        wave_manifest,
        role="go_tmp",
        scope="task",
        task_id=task_id,
    )
    if len(task_tmpdirs) != 1:
        raise HygieneError(
            f"wave manifest must contain exactly one task go_tmp for '{task_id}'"
        )
    task_tmpdir = task_tmpdirs[0]
    validate_live_owned_directory(wave_manifest, task_tmpdir)
    if str(build_cache_path) == task_tmpdir["path"]:
        raise HygieneError("GOCACHE and GOTMPDIR must use distinct owned roots")

    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "dev-disk-budget-go-environment",
        "session_run_id": (
            session_manifest["run_id"] if session_manifest is not None else None
        ),
        "wave_run_id": wave_manifest["run_id"],
        "task_id": task_id,
        "gocache_source": gocache_source,
        "gocache_identity": build_cache_identity,
        "environment": {
            "GOCACHE": str(build_cache_path),
            "GOTMPDIR": task_tmpdir["path"],
        },
        "inherited": ["GOMODCACHE"],
        "lifecycle": {
            "GOCACHE": gocache_lifecycle,
            "GOTMPDIR": "release with this wave after the task stops",
            "GOMODCACHE": "host-shared and outside automatic cleanup",
        },
    }


def validate_go_topology(
    session_manifest_path: Path | None,
    wave_manifest_path: Path,
    plan_sha256: str,
    task_ids: list[str],
    host_gocache: str | Path | None = None,
) -> dict[str, Any]:
    if not SHA256_PATTERN.fullmatch(plan_sha256):
        raise HygieneError("plan_sha256 must be a lowercase SHA-256 digest")
    normalized_task_ids = sorted(identifier(task_id, "task_id") for task_id in task_ids)
    if not normalized_task_ids:
        raise HygieneError("go topology requires at least one task_id")
    if len(set(normalized_task_ids)) != len(normalized_task_ids):
        raise HygieneError("go topology task_ids must be unique")

    wave_manifest = load_manifest(wave_manifest_path)
    blockers: list[str] = []
    if wave_manifest["status"] != "active":
        blockers.append("wave manifest must be active")
    if (session_manifest_path is None) == (host_gocache is None):
        blockers.append(
            "select exactly one GOCACHE source: session manifest or host-shared path"
        )
    session_manifest: dict[str, Any] | None = None
    if session_manifest_path is not None:
        session_manifest = load_manifest(session_manifest_path)
        if session_manifest["status"] != "active":
            blockers.append("session manifest must be active")
        if session_manifest["run_id"] == wave_manifest["run_id"]:
            blockers.append("session and wave manifests must be distinct runs")
        build_caches = matching_owned_directories(
            session_manifest,
            role="go_build_cache",
            scope="session_shared",
            task_id=None,
        )
        if len(build_caches) != 1:
            blockers.append(
                "session manifest must contain exactly one session_shared "
                "go_build_cache"
            )
    elif host_gocache is not None:
        try:
            validate_host_shared_go_cache(host_gocache, wave_manifest)
        except HygieneError as error:
            blockers.append(str(error))
    if any(
        resource["kind"] == "owned_directory"
        and resource.get("role") == "go_build_cache"
        for resource in wave_manifest["resources"]
    ):
        blockers.append("wave manifest must not own a go_build_cache")
    if session_manifest is not None and any(
        resource["kind"] == "owned_directory"
        and resource.get("role") == "go_tmp"
        for resource in session_manifest["resources"]
    ):
        blockers.append("session manifest must not own task go_tmp directories")

    wave_tmp_by_task: dict[str, list[dict[str, Any]]] = {}
    for resource in wave_manifest["resources"]:
        if (
            resource["kind"] == "owned_directory"
            and resource.get("role") == "go_tmp"
            and resource.get("scope") == "task"
        ):
            wave_tmp_by_task.setdefault(resource["task_id"], []).append(resource)
    if sorted(wave_tmp_by_task) != normalized_task_ids:
        blockers.append(
            "wave go_tmp task set must exactly match declared Go participants"
        )
    for task_id in normalized_task_ids:
        if len(wave_tmp_by_task.get(task_id, [])) != 1:
            blockers.append(
                f"task '{task_id}' must have exactly one task-scoped go_tmp"
            )

    placeholder_pattern = re.compile(
        r"\b(?:gocache|gotmpdir|go[ _-]?(?:build[ _-]?cache|tmp|temp))\b",
        re.IGNORECASE,
    )
    manifests = [("wave", wave_manifest)]
    if session_manifest is not None:
        manifests.insert(0, ("session", session_manifest))
    for manifest_name, manifest in manifests:
        for resource in manifest["resources"]:
            if (
                resource["kind"] == "owned_directory"
                and resource.get("role") == "generic"
                and placeholder_pattern.search(resource["purpose"])
            ):
                blockers.append(
                    f"{manifest_name} resource '{resource['id']}' describes Go "
                    "cache/tmp usage but has generic role"
                )

    environments: list[dict[str, Any]] = []
    if not blockers:
        for task_id in normalized_task_ids:
            rendered = build_go_environment(
                session_manifest_path,
                wave_manifest_path,
                task_id,
                host_gocache,
            )
            environments.append(
                {
                    "task_id": task_id,
                    "environment": rendered["environment"],
                    "go_env_sha256": sha256_bytes(
                        json.dumps(
                            rendered,
                            separators=(",", ":"),
                            sort_keys=True,
                        ).encode("utf-8")
                    ),
                }
            )
    topology = {
        "plan_sha256": plan_sha256,
        "session_run_id": (
            session_manifest["run_id"] if session_manifest is not None else None
        ),
        "wave_run_id": wave_manifest["run_id"],
        "gocache_source": (
            "session_owned"
            if session_manifest is not None
            else "operator_managed_host_shared"
        ),
        "task_ids": normalized_task_ids,
        "environments": environments,
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "dev-disk-budget-go-topology",
        "status": "ready" if not blockers else "blocked",
        "blockers": list(dict.fromkeys(blockers)),
        **topology,
        "topology_sha256": sha256_bytes(
            json.dumps(
                topology,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ),
    }


def ensure_resource_path_available(
    manifest_path: Path,
    manifest: dict[str, Any],
    resource_path: Path,
) -> None:
    if is_within(manifest_path, resource_path):
        raise HygieneError("manifest must live outside every cleanup resource")
    for resource in manifest["resources"]:
        other = Path(resource["path"])
        if is_within(resource_path, other) or is_within(other, resource_path):
            raise HygieneError(
                f"resource path overlaps registered resource '{resource['id']}'"
            )


def claim_directory(
    manifest_path: Path,
    resource_id: str,
    raw_path: str,
    purpose: str,
    cleanup_policy: str,
    now: dt.datetime,
    role: str = "generic",
    scope: str = "wave_shared",
    task_id: str | None = None,
) -> dict[str, Any]:
    identifier(resource_id, "resource_id")
    nonempty_string(purpose, "purpose")
    if cleanup_policy not in CLEANUP_POLICIES:
        raise HygieneError(f"cleanup_policy must be one of {sorted(CLEANUP_POLICIES)}")
    if role not in DIRECTORY_ROLES:
        raise HygieneError(f"role must be one of {sorted(DIRECTORY_ROLES)}")
    if scope not in DIRECTORY_SCOPES:
        raise HygieneError(f"scope must be one of {sorted(DIRECTORY_SCOPES)}")
    if scope == "task":
        if task_id is None:
            raise HygieneError("task_id is required for task-scoped directories")
        identifier(task_id, "task_id")
    elif task_id is not None:
        raise HygieneError("task_id is allowed only for task-scoped directories")
    if role == "go_build_cache" and scope != "session_shared":
        raise HygieneError("go_build_cache must be session_shared")
    if role == "go_tmp" and scope != "task":
        raise HygieneError("go_tmp must be task-scoped")
    with manifest_lock(manifest_path):
        manifest = load_manifest(manifest_path)
        if manifest["status"] != "planning":
            raise HygieneError(
                "resources can be claimed only before the run manifest is sealed"
            )
        if any(item["id"] == resource_id for item in manifest["resources"]):
            raise HygieneError(f"resource id already exists: {resource_id}")
        requested = Path(raw_path).expanduser()
        if not requested.is_absolute():
            requested = Path.cwd() / requested
        if requested.is_symlink():
            raise HygieneError(f"resource path must not be a symlink: {requested}")
        path = requested.resolve(strict=False)
        validate_removal_root(path)
        reject_ancestor_ownership_boundaries(path)
        ensure_resource_path_available(manifest_path, manifest, path)
        if path.exists() or path.is_symlink():
            raise HygieneError(
                "refusing to adopt an existing directory; declare a new path"
            )
        if not path.parent.is_dir():
            raise HygieneError(f"resource parent must already exist: {path.parent}")
        path.mkdir()
        marker_path = path / MARKER_NAME
        expected_marker = marker_document(manifest["run_id"], resource_id, path)
        atomic_write_json(marker_path, expected_marker)
        resource = {
            "id": resource_id,
            "kind": "owned_directory",
            "path": str(path),
            "purpose": purpose,
            "cleanup_policy": cleanup_policy,
            "claimed_at": format_time(now),
            "claimed_identity": path_identity(path),
            "marker_path": str(marker_path),
            "role": role,
            "scope": scope,
            "task_id": task_id,
        }
        manifest["resources"].append(resource)
        try:
            save_manifest(manifest_path, manifest, now)
        except Exception:
            with contextlib.suppress(OSError):
                shutil.rmtree(path, onerror=make_writable)
            raise
        return resource


def reserve_worktree(
    manifest_path: Path,
    resource_id: str,
    task_id: str,
    raw_repo: str,
    raw_path: str,
    purpose: str,
    cleanup_policy: str,
    now: dt.datetime,
) -> dict[str, Any]:
    identifier(resource_id, "resource_id")
    identifier(task_id, "task_id")
    nonempty_string(purpose, "purpose")
    if cleanup_policy not in CLEANUP_POLICIES:
        raise HygieneError(f"cleanup_policy must be one of {sorted(CLEANUP_POLICIES)}")
    with manifest_lock(manifest_path):
        manifest = load_manifest(manifest_path)
        if manifest["status"] != "planning":
            raise HygieneError(
                "resources can be claimed only before the run manifest is sealed"
            )
        if any(item["id"] == resource_id for item in manifest["resources"]):
            raise HygieneError(f"resource id already exists: {resource_id}")
        repo = canonical_path(raw_repo, must_exist=True)
        path = canonical_path(raw_path)
        validate_removal_root(path)
        reject_ancestor_ownership_boundaries(path)
        ensure_resource_path_available(manifest_path, manifest, path)
        if path.exists() or path.is_symlink():
            raise HygieneError(
                "refusing to adopt an existing worktree path; reserve a new path"
            )
        if path in git_worktrees(repo):
            raise HygieneError(f"worktree path is already registered: {path}")
        reservation_path = reservation_path_for(path)
        reservation = reservation_document(
            manifest["run_id"],
            resource_id,
            task_id,
            repo,
            path,
        )
        exclusive_write_json(reservation_path, reservation)
        resource = {
            "id": resource_id,
            "kind": "git_worktree",
            "path": str(path),
            "purpose": purpose,
            "cleanup_policy": cleanup_policy,
            "claimed_at": format_time(now),
            "repo": str(repo),
            "task_id": task_id,
            "reservation_path": str(reservation_path),
            "registration_state": "reserved",
            "claimed_identity": None,
            "initial_head": None,
            "bound_at": None,
            "cleanup_authorized": False,
            "expected_head": None,
            "preservation_ref": None,
            "handoff_path": None,
            "handoff_sha256": None,
            "authorized_at": None,
        }
        manifest["resources"].append(resource)
        try:
            save_manifest(manifest_path, manifest, now)
        except Exception:
            with contextlib.suppress(OSError):
                reservation_path.unlink()
            raise
        return resource


def reservation_status(
    manifest_path: Path,
    resource_id: str,
    now: dt.datetime,
) -> dict[str, Any]:
    identifier(resource_id, "resource_id")
    manifest = validate_manifest(
        load_json(manifest_path, "manifest"),
        allow_reserved_worktree_target_symlink=True,
        allow_reserved_worktree_reservation_symlink=True,
    )
    resource = resource_by_id(manifest, resource_id)
    path = Path(resource["path"])
    repo = Path(resource["repo"]) if resource["kind"] == "git_worktree" else None
    task_id = resource.get("task_id")
    blockers: list[str] = []
    reservation_sha256: str | None = None
    identity_sha256: str | None = None

    if manifest["status"] != "planning":
        blockers.append(
            f"run status must be planning, observed '{manifest['status']}'"
        )
    if resource["kind"] != "git_worktree":
        blockers.append(
            "resource kind must be git_worktree, "
            f"observed '{resource['kind']}'"
        )
    else:
        if resource["registration_state"] != "reserved":
            blockers.append(
                "worktree registration_state must be reserved and unbound, "
                f"observed '{resource['registration_state']}'"
            )

        reservation_path = Path(resource["reservation_path"])
        expected_reservation = reservation_document(
            manifest["run_id"],
            resource["id"],
            resource["task_id"],
            repo,
            path,
        )
        expected_bytes = (
            json.dumps(expected_reservation, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
        expected_sha256 = sha256_bytes(expected_bytes)
        identity_sha256 = sha256_bytes(
            json.dumps(
                expected_reservation,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        )
        reservation_bytes: bytes | None = None
        identity_changed = False
        try:
            reservation_metadata = reservation_path.lstat()
        except FileNotFoundError:
            blockers.append(
                f"worktree reservation marker does not exist: {reservation_path}"
            )
        except OSError as error:
            blockers.append(
                f"cannot inspect worktree reservation marker {reservation_path}: {error}"
            )
        else:
            if stat.S_ISLNK(reservation_metadata.st_mode):
                blockers.append(
                    f"worktree reservation marker is a symlink: {reservation_path}"
                )
            elif not stat.S_ISREG(reservation_metadata.st_mode):
                blockers.append(
                    "worktree reservation marker is not a regular file: "
                    f"{reservation_path}"
                )
            else:
                descriptor: int | None = None
                try:
                    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
                    flags |= getattr(os, "O_NOFOLLOW", 0)
                    descriptor = os.open(reservation_path, flags)
                    opened_metadata = os.fstat(descriptor)
                    with os.fdopen(descriptor, "rb") as stream:
                        descriptor = None
                        reservation_bytes = stream.read()
                    current_metadata = reservation_path.lstat()
                    identity_changed = any(
                        (
                            opened_metadata.st_dev != reservation_metadata.st_dev,
                            opened_metadata.st_ino != reservation_metadata.st_ino,
                            current_metadata.st_dev != opened_metadata.st_dev,
                            current_metadata.st_ino != opened_metadata.st_ino,
                            not stat.S_ISREG(opened_metadata.st_mode),
                            not stat.S_ISREG(current_metadata.st_mode),
                        )
                    )
                except FileNotFoundError:
                    blockers.append(
                        "worktree reservation marker disappeared while observed: "
                        f"{reservation_path}"
                    )
                except OSError as error:
                    blockers.append(
                        "cannot read worktree reservation marker without following "
                        f"links {reservation_path}: {error}"
                    )
                finally:
                    if descriptor is not None:
                        os.close(descriptor)
                if reservation_bytes is not None:
                    if identity_changed:
                        blockers.append(
                            "worktree reservation marker changed while observed: "
                            f"{reservation_path}"
                        )
                    else:
                        reservation_sha256 = sha256_bytes(reservation_bytes)
                        try:
                            observed_reservation = json.loads(reservation_bytes)
                        except (UnicodeDecodeError, json.JSONDecodeError) as error:
                            blockers.append(
                                "cannot parse worktree reservation marker "
                                f"{reservation_path}: {error}"
                            )
                        else:
                            if observed_reservation != expected_reservation:
                                blockers.append(
                                    "worktree reservation marker identity does not "
                                    "match manifest"
                                )
                        if reservation_bytes != expected_bytes:
                            blockers.append(
                                "worktree reservation marker bytes do not match "
                                "canonical manifest identity"
                            )
                        if reservation_sha256 != expected_sha256:
                            blockers.append(
                                "worktree reservation marker SHA-256 does not match "
                                "canonical manifest identity"
                            )

        try:
            target_metadata = path.lstat()
        except FileNotFoundError:
            target_metadata = None
        except OSError as error:
            blockers.append(f"cannot inspect reserved worktree path {path}: {error}")
            target_metadata = None
        if target_metadata is not None:
            if stat.S_ISLNK(target_metadata.st_mode):
                blockers.append(f"reserved worktree path is a symlink: {path}")
            else:
                blockers.append(f"reserved worktree path is occupied: {path}")

        try:
            registered_worktrees = git_worktrees(repo)
        except HygieneError as error:
            blockers.append(
                f"cannot inspect repository worktree registry: {error}"
            )
        else:
            if path in registered_worktrees:
                blockers.append(
                    f"worktree path is already registered in repository: {path}"
                )

    return {
        "schema_version": SCHEMA_VERSION,
        "kind": RESERVATION_STATUS_KIND,
        "status": "blocked" if blockers else "ready",
        "run_id": manifest["run_id"],
        "resource_id": resource["id"],
        "task_id": task_id,
        "reservation_sha256": reservation_sha256,
        "reservation_identity_sha256": identity_sha256,
        "repo": str(repo) if repo is not None else None,
        "path": str(path),
        "blockers": list(dict.fromkeys(blockers)),
        "observed_at": format_time(now),
    }


def bind_worktree(
    manifest_path: Path,
    resource_id: str,
    now: dt.datetime,
) -> dict[str, Any]:
    with manifest_lock(manifest_path):
        manifest = load_manifest(manifest_path)
        if manifest["status"] != "planning":
            raise HygieneError(
                "worktrees can be bound only before the run manifest is sealed"
            )
        resource = resource_by_id(manifest, resource_id)
        if resource["kind"] != "git_worktree":
            raise HygieneError(f"resource is not a Git worktree: {resource_id}")
        if resource["registration_state"] != "reserved":
            raise HygieneError(f"worktree resource is already bound: {resource_id}")
        path = Path(resource["path"])
        repo = Path(resource["repo"])
        validate_reservation(
            Path(resource["reservation_path"]),
            manifest["run_id"],
            resource["id"],
            resource["task_id"],
            repo,
            path,
        )
        if path not in git_worktrees(repo):
            raise HygieneError(
                f"reserved path is not a registered worktree of {repo}: {path}"
            )
        if not path.is_dir() or path.is_symlink():
            raise HygieneError(f"reserved worktree path is not a real directory: {path}")
        if not git_status_clean(path):
            raise HygieneError(f"newly bound worktree is not clean: {path}")
        resource.update(
            {
                "registration_state": "bound",
                "claimed_identity": path_identity(path),
                "initial_head": current_git_head(path),
                "bound_at": format_time(now),
            }
        )
        save_manifest(manifest_path, manifest, now)
        return resource


def authorize_worktree(
    manifest_path: Path,
    resource_id: str,
    preservation_ref: str,
    raw_handoff_path: str,
    now: dt.datetime,
) -> dict[str, Any]:
    with manifest_lock(manifest_path):
        manifest = load_manifest(manifest_path)
        if manifest["status"] != "active":
            raise HygieneError(
                "worktree cleanup can be authorized only while the run is active"
            )
        resource = resource_by_id(manifest, resource_id)
        if resource["kind"] != "git_worktree":
            raise HygieneError(f"resource is not a Git worktree: {resource_id}")
        if resource["registration_state"] != "bound":
            raise HygieneError(f"worktree resource is not bound: {resource_id}")
        path = Path(resource["path"])
        repo = Path(resource["repo"])
        if path not in git_worktrees(repo):
            raise HygieneError(f"worktree is no longer registered: {path}")
        if not git_status_clean(path):
            raise HygieneError(f"worktree is dirty: {path}")
        head = current_git_head(path)
        verify_preservation(repo, head, preservation_ref)
        handoff_path = canonical_path(raw_handoff_path, must_exist=True)
        for disposable in manifest["resources"]:
            if is_within(handoff_path, Path(disposable["path"])):
                raise HygieneError(
                    "orchestrator handoff must live outside every disposable resource"
                )
        handoff_sha256 = verify_handoff(
            handoff_path,
            resource["task_id"],
            head,
            repo,
            preservation_ref,
        )
        resource.update(
            {
                "cleanup_authorized": True,
                "expected_head": head,
                "preservation_ref": preservation_ref,
                "handoff_path": str(handoff_path),
                "handoff_sha256": handoff_sha256,
                "authorized_at": format_time(now),
            }
        )
        save_manifest(manifest_path, manifest, now)
        return resource


def start_run(
    manifest_path: Path,
    now: dt.datetime,
) -> dict[str, Any]:
    with manifest_lock(manifest_path):
        manifest = load_manifest(manifest_path)
        if manifest["status"] != "planning":
            raise HygieneError(
                f"run can start only from planning, not {manifest['status']}"
            )
        if not manifest["resources"]:
            raise HygieneError("refusing to seal an empty resource manifest")
        for resource in manifest["resources"]:
            path = Path(resource["path"])
            if (
                resource["kind"] == "git_worktree"
                and resource["registration_state"] != "bound"
            ):
                raise HygieneError(
                    f"worktree reservation was not bound before run start: {path}"
                )
            if path_identity(path) != resource["claimed_identity"]:
                raise HygieneError(
                    f"resource path identity changed before run start: {path}"
                )
            if resource["claimed_identity"]["uid"] != os.getuid():
                raise HygieneError(
                    f"resource root is not owned by current uid: {path}"
                )
            if resource["kind"] == "owned_directory":
                validate_marker(
                    Path(resource["marker_path"]),
                    manifest["run_id"],
                    resource["id"],
                    path,
                )
                unexpected = [
                    entry for entry in path.iterdir() if entry.name != MARKER_NAME
                ]
                if unexpected:
                    raise HygieneError(
                        f"owned directory received content before run start: {path}"
                    )
            else:
                repo = Path(resource["repo"])
                validate_reservation(
                    Path(resource["reservation_path"]),
                    manifest["run_id"],
                    resource["id"],
                    resource["task_id"],
                    repo,
                    path,
                )
                if path not in git_worktrees(repo):
                    raise HygieneError(
                        f"worktree is no longer registered before run start: {path}"
                    )
                if current_git_head(path) != resource["initial_head"]:
                    raise HygieneError(
                        f"worktree HEAD changed before run start: {path}"
                    )
        manifest["resource_set_sha256"] = resource_set_sha256(manifest)
        manifest["status"] = "active"
        manifest["updated_at"] = format_time(now)
        atomic_write_json(manifest_path, validate_manifest(manifest))
        return manifest


def finish_run(
    manifest_path: Path,
    status: str,
    now: dt.datetime,
) -> dict[str, Any]:
    terminal_statuses = {"succeeded", "failed", "interrupted", "released"}
    if status not in terminal_statuses:
        raise HygieneError(
            f"finish status must be one of {sorted(terminal_statuses)}"
        )
    with manifest_lock(manifest_path):
        manifest = load_manifest(manifest_path)
        current = manifest["status"]
        allowed = (
            current == "active" and status in {"succeeded", "failed", "interrupted"}
        ) or (
            status == "released"
            and current in {"failed", "interrupted", "succeeded"}
        )
        if not allowed:
            raise HygieneError(f"invalid run status transition: {current} -> {status}")
        if status == "succeeded":
            unauthorized = sorted(
                resource["id"]
                for resource in manifest["resources"]
                if resource["kind"] == "git_worktree"
                and resource["cleanup_policy"] == "delete_when_released"
                and not resource["cleanup_authorized"]
            )
            if unauthorized:
                raise HygieneError(
                    "succeeded run has unauthorized disposable worktrees: "
                    + ", ".join(unauthorized)
                )
        manifest["status"] = status
        manifest["updated_at"] = format_time(now)
        atomic_write_json(manifest_path, validate_manifest(manifest))
        return manifest


def inspect_resource(
    manifest: dict[str, Any],
    resource: dict[str, Any],
    *,
    now: dt.datetime,
    min_age_seconds: int,
    check_activity: bool,
) -> dict[str, Any]:
    path = Path(resource["path"])
    reasons: list[str] = []
    fingerprint: dict[str, Any] = {}
    size = {"logical_bytes": 0, "allocated_bytes": 0}
    exists = path.exists() and not path.is_symlink()
    if not exists:
        reasons.append("resource path is absent or a symlink")
    else:
        try:
            identity = path_identity(path)
            fingerprint["path_identity"] = identity
            if identity != resource["claimed_identity"]:
                reasons.append("resource path identity changed after it was claimed")
            size, tree_sha256 = directory_snapshot(
                path,
                reject_symlinks=resource["kind"] == "owned_directory",
            )
            fingerprint["tree_sha256"] = tree_sha256
        except (HygieneError, OSError) as error:
            reasons.append(str(error))

    if resource["cleanup_policy"] == "report_only":
        reasons.append("resource cleanup policy is report_only")
    if manifest["status"] not in {"succeeded", "released"}:
        reasons.append(f"run status '{manifest['status']}' is not releasable")
    eligibility_times = [
        parse_time(resource["claimed_at"], "resource.claimed_at"),
        parse_time(manifest["updated_at"], "manifest.updated_at"),
    ]
    if resource.get("authorized_at"):
        eligibility_times.append(
            parse_time(resource["authorized_at"], "resource.authorized_at")
        )
    eligibility_started_at = max(eligibility_times)
    age_seconds = max(0, int((now - eligibility_started_at).total_seconds()))
    if age_seconds < min_age_seconds:
        reasons.append(
            f"resource age {age_seconds}s is below minimum {min_age_seconds}s"
        )

    action: str
    if resource["kind"] == "owned_directory":
        action = "remove_owned_directory"
        if exists:
            try:
                marker_hash = validate_marker(
                    Path(resource["marker_path"]),
                    manifest["run_id"],
                    resource["id"],
                    path,
                )
                fingerprint["marker_sha256"] = marker_hash
            except HygieneError as error:
                reasons.append(str(error))
    else:
        action = "git_worktree_remove"
        repo = Path(resource["repo"])
        fingerprint["repo"] = str(repo)
        try:
            fingerprint["reservation_sha256"] = validate_reservation(
                Path(resource["reservation_path"]),
                manifest["run_id"],
                resource["id"],
                resource["task_id"],
                repo,
                path,
            )
        except HygieneError as error:
            reasons.append(str(error))
        if not resource["cleanup_authorized"]:
            reasons.append("worktree cleanup is not explicitly authorized")
        if exists:
            try:
                if path not in git_worktrees(repo):
                    reasons.append("worktree is no longer registered")
                if not git_status_clean(path):
                    reasons.append("worktree is dirty")
                head = current_git_head(path)
                fingerprint["head"] = head
                if head != resource["expected_head"]:
                    reasons.append("worktree HEAD changed after cleanup authorization")
                if resource["cleanup_authorized"]:
                    resolved = verify_preservation(
                        repo,
                        head,
                        resource["preservation_ref"],
                    )
                    fingerprint["preservation_commit"] = resolved
                    handoff_path = Path(resource["handoff_path"])
                    handoff_sha256 = verify_handoff(
                        handoff_path,
                        resource["task_id"],
                        head,
                        repo,
                        resource["preservation_ref"],
                    )
                    fingerprint["handoff_sha256"] = handoff_sha256
                    if handoff_sha256 != resource["handoff_sha256"]:
                        reasons.append(
                            "orchestrator handoff changed after cleanup authorization"
                        )
            except HygieneError as error:
                reasons.append(str(error))

    if exists and check_activity and not reasons:
        active, detail = active_handle_check(path)
        fingerprint["activity_check"] = detail
        if active is True:
            reasons.append(detail)
        elif active is None:
            reasons.append(detail)

    result = {
        "id": resource["id"],
        "kind": resource["kind"],
        "path": str(path),
        "purpose": resource["purpose"],
        "cleanup_policy": resource["cleanup_policy"],
        "eligibility_started_at": format_time(eligibility_started_at),
        "age_seconds": age_seconds,
        "size": size,
        "action": action,
        "eligible": not reasons,
        "reasons": list(dict.fromkeys(reasons)),
        "fingerprint": fingerprint,
    }
    for key in ("role", "scope", "task_id"):
        if key in resource:
            result[key] = resource[key]
    return result


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


def inventory(
    manifest_path: Path,
    observed_paths: list[str],
    now: dt.datetime,
) -> dict[str, Any]:
    manifest = load_manifest(manifest_path)
    resources = [
        inspect_resource(
            manifest,
            resource,
            now=now,
            min_age_seconds=0,
            check_activity=False,
        )
        for resource in manifest["resources"]
    ]
    observations: list[dict[str, Any]] = []
    for raw_path in observed_paths:
        path = canonical_path(raw_path)
        observation = filesystem_report(path)
        if path.exists() and path.is_dir() and not path.is_symlink():
            try:
                (
                    observation["size"],
                    observation["tree_sha256"],
                ) = directory_snapshot(
                    path,
                    reject_symlinks=False,
                    enforce_ownership_boundary=False,
                )
                observation["error"] = None
            except HygieneError as error:
                observation["size"] = None
                observation["tree_sha256"] = None
                observation["error"] = str(error)
        else:
            observation["size"] = None
            observation["tree_sha256"] = None
            observation["error"] = "observed path is absent, not a directory, or a symlink"
        observation["cleanup"] = "report_only"
        observations.append(observation)
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "dev-disk-budget-inventory",
        "generated_at": format_time(now),
        "manifest_path": str(manifest_path),
        "run_id": manifest["run_id"],
        "run_status": manifest["status"],
        "filesystem": filesystem_report(manifest_path.parent),
        "resources": resources,
        "observations": observations,
        "notes": [
            "Sizes are directional estimates; copy-on-write filesystems may reclaim less.",
            "Observed paths are never cleanup candidates.",
        ],
    }


def build_plan(
    manifest_path: Path,
    *,
    min_age_seconds: int,
    ttl_seconds: int,
    now: dt.datetime,
) -> dict[str, Any]:
    if min_age_seconds < 0:
        raise HygieneError("min_age_seconds must not be negative")
    if ttl_seconds <= 0:
        raise HygieneError("ttl_seconds must be positive")
    manifest_bytes = manifest_path.read_bytes()
    manifest = validate_manifest(json.loads(manifest_bytes))
    resources = [
        inspect_resource(
            manifest,
            resource,
            now=now,
            min_age_seconds=min_age_seconds,
            check_activity=True,
        )
        for resource in manifest["resources"]
    ]
    resources.sort(
        key=lambda item: (
            0 if item["kind"] == "git_worktree" else 1,
            item["id"],
        )
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": PLAN_KIND,
        "generated_at": format_time(now),
        "expires_at": format_time(now + dt.timedelta(seconds=ttl_seconds)),
        "manifest_path": str(manifest_path),
        "manifest_sha256": sha256_bytes(manifest_bytes),
        "run_id": manifest["run_id"],
        "run_status": manifest["status"],
        "min_age_seconds": min_age_seconds,
        "filesystem_before": filesystem_report(manifest_path.parent),
        "resources": resources,
        "eligible_count": sum(item["eligible"] for item in resources),
        "blocked_count": sum(not item["eligible"] for item in resources),
    }


def check_disk(
    path: Path,
    min_free_bytes: int,
    min_free_percent: float,
    expected_growth_bytes: int = 0,
) -> dict[str, Any]:
    if min_free_bytes < 0:
        raise HygieneError("min_free_bytes must not be negative")
    if expected_growth_bytes < 0:
        raise HygieneError("expected_growth_bytes must not be negative")
    if not 0 <= min_free_percent <= 100:
        raise HygieneError("min_free_percent must be between 0 and 100")
    report = filesystem_report(path)
    projected_free_bytes = max(0, report["free_bytes"] - expected_growth_bytes)
    projected_free_percent = (
        projected_free_bytes / report["total_bytes"] * 100
        if report["total_bytes"]
        else 0.0
    )
    byte_ok = projected_free_bytes >= min_free_bytes
    percent_ok = projected_free_percent >= min_free_percent
    byte_reason = (
        f"free bytes are below required floor {min_free_bytes}"
        if expected_growth_bytes == 0
        else "projected free bytes after expected growth are below "
        f"required floor {min_free_bytes}"
    )
    percent_reason = (
        f"free percent is below required floor {min_free_percent}"
        if expected_growth_bytes == 0
        else "projected free percent after expected growth is below "
        f"required floor {min_free_percent}"
    )
    report.update(
        {
            "schema_version": SCHEMA_VERSION,
            "kind": "dev-disk-budget-disk-check",
            "min_free_bytes": min_free_bytes,
            "min_free_percent": min_free_percent,
            "expected_growth_bytes": expected_growth_bytes,
            "projected_free_bytes": projected_free_bytes,
            "projected_free_percent": projected_free_percent,
            "healthy": byte_ok and percent_ok,
            "reasons": [
                reason
                for condition, reason in (
                    (
                        byte_ok,
                        byte_reason,
                    ),
                    (
                        percent_ok,
                        percent_reason,
                    ),
                )
                if not condition
            ],
        }
    )
    return report


def make_writable(function: Any, path: str, _: Any) -> None:
    target = Path(path)
    with contextlib.suppress(OSError):
        mode = target.lstat().st_mode
        if target.is_dir():
            os.chmod(target, mode | stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
        else:
            os.chmod(target, mode | stat.S_IRUSR | stat.S_IWUSR)
    function(path)


def remove_owned_directory(
    path: Path,
    expected_identity: dict[str, int],
    quarantine_path: Path,
) -> None:
    if quarantine_path.exists() or quarantine_path.is_symlink():
        raise HygieneError(f"cleanup quarantine already exists: {quarantine_path}")
    try:
        path.rename(quarantine_path)
    except OSError as error:
        raise HygieneError(f"cannot quarantine owned directory {path}: {error}") from error
    if path.exists() or path.is_symlink():
        raise HygieneError(f"owned directory still exists after quarantine: {path}")
    if path_identity(quarantine_path) != expected_identity:
        raise HygieneError(
            f"quarantined directory identity does not match plan: {quarantine_path}"
        )
    active, detail = active_handle_check(quarantine_path)
    if active is not False:
        raise HygieneError(
            f"quarantined directory activity check blocks removal: {detail}"
        )
    try:
        shutil.rmtree(quarantine_path, onerror=make_writable)
    except OSError as error:
        raise HygieneError(
            f"cannot remove quarantined directory {quarantine_path}: {error}"
        ) from error
    if quarantine_path.exists() or quarantine_path.is_symlink():
        raise HygieneError(
            f"quarantined directory still exists after cleanup: {quarantine_path}"
        )


def remove_worktree(
    repo: Path,
    path: Path,
    reservation_path: Path,
    reservation_sha256: str,
) -> None:
    git(repo, "worktree", "remove", str(path))
    if path.exists() or path in git_worktrees(repo):
        raise HygieneError(f"worktree still exists after cleanup: {path}")
    if sha256_file(reservation_path) != reservation_sha256:
        raise HygieneError(
            f"worktree reservation changed before release: {reservation_path}"
        )
    try:
        reservation_path.unlink()
    except OSError as error:
        raise HygieneError(
            f"cannot release worktree reservation {reservation_path}: {error}"
        ) from error


def _execute_cleanup_locked(
    plan_path: Path,
    *,
    expected_plan_sha256: str,
    receipt_path: Path,
    now: dt.datetime,
) -> dict[str, Any]:
    if not SHA256_PATTERN.fullmatch(expected_plan_sha256):
        raise HygieneError("confirm_plan_sha256 must be a lowercase SHA-256 digest")
    plan_bytes = plan_path.read_bytes()
    actual_plan_sha256 = sha256_bytes(plan_bytes)
    if actual_plan_sha256 != expected_plan_sha256:
        raise HygieneError("cleanup plan SHA-256 confirmation does not match")
    try:
        plan = json.loads(plan_bytes)
    except json.JSONDecodeError as error:
        raise HygieneError(f"cannot parse cleanup plan {plan_path}: {error}") from error
    if not isinstance(plan, dict):
        raise HygieneError("cleanup plan must be an object")
    if plan.get("schema_version") != SCHEMA_VERSION or plan.get("kind") != PLAN_KIND:
        raise HygieneError("cleanup plan has an unsupported schema or kind")
    expires_at = parse_time(plan.get("expires_at"), "plan.expires_at")
    if now > expires_at:
        raise HygieneError("cleanup plan expired; generate a fresh plan")
    manifest_path = canonical_path(
        nonempty_string(plan.get("manifest_path"), "plan.manifest_path"),
        must_exist=True,
    )
    manifest_bytes = manifest_path.read_bytes()
    if sha256_bytes(manifest_bytes) != plan.get("manifest_sha256"):
        raise HygieneError("manifest changed after the cleanup plan was generated")
    manifest = validate_manifest(json.loads(manifest_bytes))
    if plan.get("run_id") != manifest["run_id"]:
        raise HygieneError("cleanup plan run_id does not match manifest")
    if plan.get("run_status") != manifest["status"]:
        raise HygieneError("cleanup plan run status does not match manifest")
    receipt_path = canonical_path(receipt_path)
    if receipt_path.exists() or receipt_path.is_symlink():
        raise HygieneError(f"cleanup receipt already exists: {receipt_path}")
    planned_resources = plan.get("resources")
    if not isinstance(planned_resources, list):
        raise HygieneError("plan.resources must be an array")
    if plan.get("eligible_count") != sum(
        isinstance(item, dict) and item.get("eligible") is True
        for item in planned_resources
    ):
        raise HygieneError("cleanup plan eligible_count is inconsistent")
    if plan.get("blocked_count") != sum(
        isinstance(item, dict) and item.get("eligible") is False
        for item in planned_resources
    ):
        raise HygieneError("cleanup plan blocked_count is inconsistent")
    eligible: list[dict[str, Any]] = []
    seen_plan_ids: set[str] = set()
    for index, item in enumerate(planned_resources):
        if not isinstance(item, dict):
            raise HygieneError(f"plan.resources[{index}] must be an object")
        resource_id = identifier(item.get("id"), f"plan.resources[{index}].id")
        if resource_id in seen_plan_ids:
            raise HygieneError(f"cleanup plan has duplicate resource id: {resource_id}")
        seen_plan_ids.add(resource_id)
        if not isinstance(item.get("eligible"), bool):
            raise HygieneError(f"plan resource '{resource_id}' eligible must be boolean")
        if item["eligible"]:
            eligible.append(item)

    for planned in eligible:
        resource_id = identifier(planned.get("id"), "plan.resource.id")
        resource = resource_by_id(manifest, resource_id)
        path = Path(resource["path"])
        expected_action = (
            "git_worktree_remove"
            if resource["kind"] == "git_worktree"
            else "remove_owned_directory"
        )
        if planned.get("path") != str(path):
            raise HygieneError(
                f"resource '{resource_id}' plan path does not match manifest"
            )
        if planned.get("kind") != resource["kind"]:
            raise HygieneError(
                f"resource '{resource_id}' plan kind does not match manifest"
            )
        if planned.get("action") != expected_action:
            raise HygieneError(
                f"resource '{resource_id}' plan action does not match manifest"
            )
        for protected in (
            manifest_path,
            plan_path.resolve(),
            receipt_path,
            Path.cwd().resolve(),
        ):
            if is_within(protected, path):
                raise HygieneError(
                    f"cleanup target contains protected active path {protected}: {path}"
                )
        fresh = inspect_resource(
            manifest,
            resource,
            now=now,
            min_age_seconds=int(plan.get("min_age_seconds", -1)),
            check_activity=True,
        )
        if not fresh["eligible"]:
            raise HygieneError(
                f"resource '{resource_id}' is no longer eligible: "
                + "; ".join(fresh["reasons"])
            )
        if fresh["action"] != planned.get("action"):
            raise HygieneError(f"resource '{resource_id}' cleanup action changed")
        if fresh["fingerprint"] != planned.get("fingerprint"):
            raise HygieneError(f"resource '{resource_id}' fingerprint changed")
        for field in (
            "purpose",
            "cleanup_policy",
            "eligibility_started_at",
            "size",
            "reasons",
        ):
            if planned.get(field) != fresh[field]:
                raise HygieneError(
                    f"resource '{resource_id}' reviewed field changed: {field}"
                )
        for field in ("role", "scope", "task_id"):
            if planned.get(field) != fresh.get(field):
                raise HygieneError(
                    f"resource '{resource_id}' reviewed field changed: {field}"
                )

    receipt = {
        "schema_version": SCHEMA_VERSION,
        "kind": RECEIPT_KIND,
        "executed_at": format_time(now),
        "plan_path": str(plan_path),
        "plan_sha256": actual_plan_sha256,
        "manifest_path": str(manifest_path),
        "run_id": manifest["run_id"],
        "status": "prepared",
        "current_resource": None,
        "current_quarantine": None,
        "results": [],
        "filesystem_before": filesystem_report(manifest_path.parent),
        "filesystem_after": None,
    }
    atomic_write_json(receipt_path, receipt)

    results: list[dict[str, Any]] = []
    for planned in eligible:
        resource = resource_by_id(manifest, planned["id"])
        path = Path(resource["path"])
        quarantine_path = path.with_name(
            f".{path.name}.dev-disk-budget."
            f"{actual_plan_sha256[:12]}.{resource['id']}"
        )
        try:
            receipt["status"] = "cleaning"
            receipt["current_resource"] = resource["id"]
            receipt["current_quarantine"] = (
                str(quarantine_path)
                if planned["action"] == "remove_owned_directory"
                else None
            )
            receipt["results"] = results
            atomic_write_json(receipt_path, receipt)
            fresh = inspect_resource(
                manifest,
                resource,
                now=now,
                min_age_seconds=int(plan.get("min_age_seconds", -1)),
                check_activity=True,
            )
            if not fresh["eligible"] or fresh["fingerprint"] != planned["fingerprint"]:
                reasons = fresh["reasons"] or ["resource fingerprint changed"]
                raise HygieneError(
                    "resource changed immediately before cleanup: "
                    + "; ".join(reasons)
                )
            if planned["action"] == "git_worktree_remove":
                remove_worktree(
                    Path(resource["repo"]),
                    path,
                    Path(resource["reservation_path"]),
                    planned["fingerprint"]["reservation_sha256"],
                )
            elif planned["action"] == "remove_owned_directory":
                validate_marker(
                    Path(resource["marker_path"]),
                    manifest["run_id"],
                    resource["id"],
                    path,
                )
                remove_owned_directory(
                    path,
                    planned["fingerprint"]["path_identity"],
                    quarantine_path,
                )
            else:
                raise HygieneError(f"unsupported cleanup action: {planned['action']}")
            results.append(
                {
                    "id": resource["id"],
                    "path": str(path),
                    "action": planned["action"],
                    "status": "removed",
                    "estimated_allocated_bytes": planned["size"]["allocated_bytes"],
                }
            )
            receipt["results"] = results
            receipt["current_resource"] = None
            receipt["current_quarantine"] = None
            atomic_write_json(receipt_path, receipt)
        except (HygieneError, OSError) as error:
            results.append(
                {
                    "id": resource["id"],
                    "path": str(path),
                    "action": planned["action"],
                    "status": "failed",
                    "error": str(error),
                }
            )
            receipt["status"] = "recovery_required"
            receipt["current_resource"] = resource["id"]
            receipt["results"] = results
            receipt["filesystem_after"] = filesystem_report(manifest_path.parent)
            atomic_write_json(receipt_path, receipt)
            break

    failed = [result for result in results if result["status"] == "failed"]
    receipt["status"] = "recovery_required" if failed else "completed"
    receipt["current_resource"] = None if not failed else receipt["current_resource"]
    if not failed:
        receipt["current_quarantine"] = None
    receipt["results"] = results
    receipt["filesystem_after"] = filesystem_report(manifest_path.parent)
    atomic_write_json(receipt_path, receipt)
    return receipt


def execute_cleanup(
    plan_path: Path,
    *,
    expected_plan_sha256: str,
    receipt_path: Path,
    now: dt.datetime,
) -> dict[str, Any]:
    if not SHA256_PATTERN.fullmatch(expected_plan_sha256):
        raise HygieneError("confirm_plan_sha256 must be a lowercase SHA-256 digest")
    try:
        plan_bytes = plan_path.read_bytes()
    except OSError as error:
        raise HygieneError(f"cannot read cleanup plan {plan_path}: {error}") from error
    if sha256_bytes(plan_bytes) != expected_plan_sha256:
        raise HygieneError("cleanup plan SHA-256 confirmation does not match")
    try:
        plan = json.loads(plan_bytes)
    except json.JSONDecodeError as error:
        raise HygieneError(f"cannot parse cleanup plan {plan_path}: {error}") from error
    if not isinstance(plan, dict):
        raise HygieneError("cleanup plan must be an object")
    manifest_path = canonical_path(
        nonempty_string(plan.get("manifest_path"), "plan.manifest_path"),
        must_exist=True,
    )
    with manifest_lock(manifest_path):
        return _execute_cleanup_locked(
            plan_path,
            expected_plan_sha256=expected_plan_sha256,
            receipt_path=receipt_path,
            now=now,
        )


def write_output(document: Any, output: str | None) -> None:
    rendered = json.dumps(document, indent=2, sort_keys=True) + "\n"
    if output is None or output == "-":
        sys.stdout.write(rendered)
    else:
        Path(output).write_text(rendered, encoding="utf-8")


def add_output_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--output", help="Write JSON to this path instead of stdout.")


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        description="Audit and clean explicitly owned workspace resources."
    )
    subparsers = root.add_subparsers(dest="command", required=True)

    check = subparsers.add_parser("check", help="Fail when free disk is below a floor.")
    check.add_argument("--path", default=".")
    check.add_argument("--min-free-bytes", type=int, default=DEFAULT_MIN_FREE_BYTES)
    check.add_argument(
        "--min-free-percent", type=float, default=DEFAULT_MIN_FREE_PERCENT
    )
    check.add_argument(
        "--expected-growth-bytes",
        type=int,
        default=0,
        help="Projected peak bytes for the next wave.",
    )
    add_output_argument(check)

    init = subparsers.add_parser("init", help="Create a planning run manifest.")
    init.add_argument("--manifest", required=True)
    init.add_argument("--run-id", required=True)
    add_output_argument(init)

    init_host_go_cache_parser = subparsers.add_parser(
        "init-host-go-cache",
        help="Create one operator-managed cross-session Go build cache.",
    )
    init_host_go_cache_parser.add_argument("--path", required=True)
    add_output_argument(init_host_go_cache_parser)

    claim_directory_parser = subparsers.add_parser(
        "claim-directory",
        help="Create and claim a new task-owned directory.",
    )
    claim_directory_parser.add_argument("--manifest", required=True)
    claim_directory_parser.add_argument("--id", required=True)
    claim_directory_parser.add_argument("--path", required=True)
    claim_directory_parser.add_argument("--purpose", required=True)
    claim_directory_parser.add_argument(
        "--role",
        choices=sorted(DIRECTORY_ROLES),
        default="generic",
    )
    claim_directory_parser.add_argument(
        "--scope",
        choices=sorted(DIRECTORY_SCOPES),
        default="wave_shared",
    )
    claim_directory_parser.add_argument("--task-id")
    claim_directory_parser.add_argument(
        "--cleanup-policy",
        choices=sorted(CLEANUP_POLICIES),
        default="delete_when_released",
    )
    add_output_argument(claim_directory_parser)

    reserve_worktree_parser = subparsers.add_parser(
        "reserve-worktree",
        help="Reserve a nonexistent path before creating a Git worktree.",
    )
    reserve_worktree_parser.add_argument("--manifest", required=True)
    reserve_worktree_parser.add_argument("--id", required=True)
    reserve_worktree_parser.add_argument("--task-id", required=True)
    reserve_worktree_parser.add_argument("--repo", required=True)
    reserve_worktree_parser.add_argument("--path", required=True)
    reserve_worktree_parser.add_argument("--purpose", required=True)
    reserve_worktree_parser.add_argument(
        "--cleanup-policy",
        choices=sorted(CLEANUP_POLICIES),
        default="delete_when_released",
    )
    add_output_argument(reserve_worktree_parser)

    reservation_status_parser = subparsers.add_parser(
        "reservation-status",
        help="Revalidate an unbound worktree reservation without changing it.",
    )
    reservation_status_parser.add_argument("--manifest", required=True)
    reservation_status_parser.add_argument("--id", required=True)
    add_output_argument(reservation_status_parser)

    bind_worktree_parser = subparsers.add_parser(
        "bind-worktree",
        help="Bind a newly created worktree to its prior reservation.",
    )
    bind_worktree_parser.add_argument("--manifest", required=True)
    bind_worktree_parser.add_argument("--id", required=True)
    add_output_argument(bind_worktree_parser)

    authorize = subparsers.add_parser(
        "authorize-worktree",
        help="Record clean HEAD and independently verified preservation.",
    )
    authorize.add_argument("--manifest", required=True)
    authorize.add_argument("--id", required=True)
    authorize.add_argument("--preservation-ref", required=True)
    authorize.add_argument("--handoff", required=True)
    add_output_argument(authorize)

    start = subparsers.add_parser(
        "start",
        help="Seal the declared resource set before parallel work begins.",
    )
    start.add_argument("--manifest", required=True)
    add_output_argument(start)

    finish = subparsers.add_parser(
        "finish",
        help="Mark a run terminal; failed runs require a later released transition.",
    )
    finish.add_argument("--manifest", required=True)
    finish.add_argument(
        "--status",
        choices=["failed", "interrupted", "released", "succeeded"],
        required=True,
    )
    add_output_argument(finish)

    go_env = subparsers.add_parser(
        "go-env",
        help="Render one shared GOCACHE and one task-scoped GOTMPDIR.",
    )
    go_env_cache = go_env.add_mutually_exclusive_group(required=True)
    go_env_cache.add_argument("--session-manifest")
    go_env_cache.add_argument("--host-gocache")
    go_env.add_argument("--wave-manifest", required=True)
    go_env.add_argument("--task-id", required=True)
    add_output_argument(go_env)

    go_topology = subparsers.add_parser(
        "go-topology",
        help="Validate every Go participant against sealed cache/tmp manifests.",
    )
    go_topology_cache = go_topology.add_mutually_exclusive_group(required=True)
    go_topology_cache.add_argument("--session-manifest")
    go_topology_cache.add_argument("--host-gocache")
    go_topology.add_argument("--wave-manifest", required=True)
    go_topology.add_argument("--plan-sha256", required=True)
    go_topology.add_argument("--task-id", action="append", required=True)
    add_output_argument(go_topology)

    inventory_parser = subparsers.add_parser(
        "inventory",
        help="Inspect managed resources and optional report-only paths.",
    )
    inventory_parser.add_argument("--manifest", required=True)
    inventory_parser.add_argument("--observe", action="append", default=[])
    add_output_argument(inventory_parser)

    plan = subparsers.add_parser(
        "plan",
        help="Create a short-lived cleanup plan without deleting anything.",
    )
    plan.add_argument("--manifest", required=True)
    plan.add_argument(
        "--min-age-seconds",
        type=int,
        default=DEFAULT_MIN_AGE_SECONDS,
    )
    plan.add_argument("--ttl-seconds", type=int, default=DEFAULT_PLAN_TTL_SECONDS)
    add_output_argument(plan)

    cleanup = subparsers.add_parser(
        "cleanup",
        help="Execute an eligible short-lived cleanup plan.",
    )
    cleanup.add_argument("--plan", required=True)
    cleanup.add_argument(
        "--execute",
        action="store_true",
        help="Required acknowledgment for deletion.",
    )
    cleanup.add_argument(
        "--confirm-plan-sha256",
        required=True,
        help="Exact SHA-256 of the reviewed cleanup plan.",
    )
    cleanup.add_argument(
        "--receipt",
        required=True,
        help="New durable receipt path outside all cleanup targets.",
    )
    add_output_argument(cleanup)
    return root


def main() -> int:
    arguments = parser().parse_args()
    now = utc_now()
    try:
        if arguments.command == "check":
            result = check_disk(
                canonical_path(arguments.path),
                arguments.min_free_bytes,
                arguments.min_free_percent,
                arguments.expected_growth_bytes,
            )
            write_output(result, arguments.output)
            return 0 if result["healthy"] else 75
        if arguments.command == "init":
            result = init_manifest(
                canonical_path(arguments.manifest),
                arguments.run_id,
                now,
            )
        elif arguments.command == "init-host-go-cache":
            result = init_host_go_cache(arguments.path, now)
        elif arguments.command == "claim-directory":
            result = claim_directory(
                canonical_path(arguments.manifest, must_exist=True),
                arguments.id,
                arguments.path,
                arguments.purpose,
                arguments.cleanup_policy,
                now,
                arguments.role,
                arguments.scope,
                arguments.task_id,
            )
        elif arguments.command == "reserve-worktree":
            result = reserve_worktree(
                canonical_path(arguments.manifest, must_exist=True),
                arguments.id,
                arguments.task_id,
                arguments.repo,
                arguments.path,
                arguments.purpose,
                arguments.cleanup_policy,
                now,
            )
        elif arguments.command == "reservation-status":
            result = reservation_status(
                canonical_path(arguments.manifest, must_exist=True),
                arguments.id,
                now,
            )
        elif arguments.command == "bind-worktree":
            result = bind_worktree(
                canonical_path(arguments.manifest, must_exist=True),
                arguments.id,
                now,
            )
        elif arguments.command == "authorize-worktree":
            result = authorize_worktree(
                canonical_path(arguments.manifest, must_exist=True),
                arguments.id,
                arguments.preservation_ref,
                arguments.handoff,
                now,
            )
        elif arguments.command == "start":
            result = start_run(
                canonical_path(arguments.manifest, must_exist=True),
                now,
            )
        elif arguments.command == "finish":
            result = finish_run(
                canonical_path(arguments.manifest, must_exist=True),
                arguments.status,
                now,
            )
        elif arguments.command == "go-env":
            result = build_go_environment(
                (
                    canonical_path(arguments.session_manifest, must_exist=True)
                    if arguments.session_manifest
                    else None
                ),
                canonical_path(arguments.wave_manifest, must_exist=True),
                arguments.task_id,
                arguments.host_gocache,
            )
        elif arguments.command == "go-topology":
            result = validate_go_topology(
                (
                    canonical_path(arguments.session_manifest, must_exist=True)
                    if arguments.session_manifest
                    else None
                ),
                canonical_path(arguments.wave_manifest, must_exist=True),
                arguments.plan_sha256,
                arguments.task_id,
                arguments.host_gocache,
            )
        elif arguments.command == "inventory":
            result = inventory(
                canonical_path(arguments.manifest, must_exist=True),
                arguments.observe,
                now,
            )
        elif arguments.command == "plan":
            result = build_plan(
                canonical_path(arguments.manifest, must_exist=True),
                min_age_seconds=arguments.min_age_seconds,
                ttl_seconds=arguments.ttl_seconds,
                now=now,
            )
        elif arguments.command == "cleanup":
            if not arguments.execute:
                raise HygieneError("cleanup requires explicit --execute")
            result = execute_cleanup(
                canonical_path(arguments.plan, must_exist=True),
                expected_plan_sha256=arguments.confirm_plan_sha256,
                receipt_path=canonical_path(arguments.receipt),
                now=now,
            )
        else:
            raise HygieneError(f"unsupported command: {arguments.command}")
        write_output(result, arguments.output)
        if result.get("kind") == RECEIPT_KIND and result.get("status") != "completed":
            return 2
        if (
            result.get("kind") == RESERVATION_STATUS_KIND
            and result.get("status") == "blocked"
        ):
            return 1
        if (
            result.get("kind") == "dev-disk-budget-go-topology"
            and result.get("status") == "blocked"
        ):
            return 1
        return 0
    except (HygieneError, OSError, json.JSONDecodeError) as error:
        write_output(
            {
                "schema_version": SCHEMA_VERSION,
                "kind": "dev-disk-budget-error",
                "command": arguments.command,
                "errors": [str(error)],
            },
            getattr(arguments, "output", None),
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
