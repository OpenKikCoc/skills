#!/usr/bin/env python3
"""Run Go with a shared cache and cooperative cross-session disk reservations."""

from __future__ import annotations

import argparse
import contextlib
import ctypes
import errno
import fcntl
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any, Iterator

sys.path.insert(0, str(Path(__file__).resolve().parent))

import disk_budget as hygiene  # noqa: E402


KIND = "dev-disk-budget-go-host"
MIGRATION_PREFLIGHT_KIND = "dev-disk-budget-go-migration-preflight"
COMMANDS = {"build", "test", "vet", "list", "generate", "run", "env", "version"}
GIB = 1024 ** 3
MIB = 1024 ** 2
DEFAULT_GROWTH_BYTES = {
    "version": 512 * MIB,
    "env": 1 * GIB,
    "list": 2 * GIB,
    "build": 8 * GIB,
    "generate": 8 * GIB,
    "run": 8 * GIB,
    "vet": 8 * GIB,
    "test": 12 * GIB,
    "test_full": 24 * GIB,
    "test_race": 32 * GIB,
}
LIVENESS_STATES = {"alive", "dead", "identity_mismatch", "unknown"}


def default_state() -> Path:
    dedicated = os.environ.get("DEV_DISK_BUDGET_STATE_ROOT")
    if dedicated and dedicated.strip():
        return Path(dedicated.strip())
    return Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state"))) / "dev-disk-budget"


def _linux_process_identity(pid: int) -> dict[str, Any]:
    process_root = Path("/proc") / str(pid)
    metadata = process_root.stat()
    raw_stat = (process_root / "stat").read_text(encoding="utf-8")
    closing = raw_stat.rfind(")")
    fields = raw_stat[closing + 2:].split() if closing >= 0 else []
    if len(fields) <= 19:
        raise OSError(f"cannot parse process start identity for pid {pid}")
    boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
    namespace = os.readlink(process_root / "ns/pid")
    if not boot_id or not namespace:
        raise OSError(f"incomplete Linux process identity for pid {pid}")
    return {
        "schema_version": 1,
        "pid": pid,
        "uid": metadata.st_uid,
        "platform": "linux",
        "boot_id": boot_id,
        "start_token": fields[19],
        "pid_namespace": namespace,
        "process_group_id": int(fields[2]),
    }


def _ps_field(pid: int, field: str) -> str:
    completed = subprocess.run(
        ["ps", "-o", f"{field}=", "-p", str(pid)],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        timeout=5,
    )
    value = completed.stdout.strip()
    if completed.returncode != 0 or not value:
        raise ProcessLookupError(pid)
    return value


def _darwin_process_identity(pid: int) -> dict[str, Any]:
    uint32 = ctypes.c_uint32

    class ProcBSDInfo(ctypes.Structure):
        _fields_ = [
            ("flags", uint32), ("status", uint32), ("xstatus", uint32),
            ("pid", uint32), ("ppid", uint32), ("uid", uint32),
            ("gid", uint32), ("ruid", uint32), ("rgid", uint32),
            ("svuid", uint32), ("svgid", uint32), ("rfu", uint32),
            ("comm", ctypes.c_char * 16), ("name", ctypes.c_char * 32),
            ("nfiles", uint32), ("pgid", uint32), ("pjobc", uint32),
            ("e_tdev", uint32), ("e_tpgid", uint32), ("nice", ctypes.c_int32),
            ("start_sec", ctypes.c_uint64), ("start_usec", ctypes.c_uint64),
        ]

    library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    library.proc_pidinfo.argtypes = [
        ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int,
    ]
    library.proc_pidinfo.restype = ctypes.c_int
    info = ProcBSDInfo()
    observed = library.proc_pidinfo(pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info))
    if observed != ctypes.sizeof(info):
        error_number = ctypes.get_errno()
        if error_number == errno.ESRCH:
            raise ProcessLookupError(pid)
        raise PermissionError(error_number or errno.EPERM, f"cannot inspect pid {pid}")
    return {
        "schema_version": 1,
        "pid": pid,
        "uid": int(info.uid),
        "platform": "darwin",
        "start_token": f"{info.start_sec}.{info.start_usec}",
        "process_group_id": int(info.pgid),
    }


def capture_process_identity(pid: int) -> dict[str, Any]:
    """Return immutable process evidence, or raise when it cannot be observed."""
    if type(pid) is not int or pid <= 0:
        raise ValueError("process pid must be positive")
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        raise
    except PermissionError:
        pass
    if sys.platform.startswith("linux"):
        return _linux_process_identity(pid)
    if sys.platform == "darwin":
        return _darwin_process_identity(pid)
    uid = int(_ps_field(pid, "uid"))
    identity = {
        "schema_version": 1,
        "pid": pid,
        "uid": uid,
        "platform": sys.platform,
        "start_token": _ps_field(pid, "lstart"),
        "process_group_id": int(_ps_field(pid, "pgid")),
    }
    return identity


def validate_process_identity(value: Any, pid: int, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise hygiene.HygieneError(f"{field} must be an object")
    if type(pid) is not int or pid <= 0:
        raise hygiene.HygieneError(f"invalid {field} pid")
    if value.get("schema_version") != 1 or value.get("pid") != pid:
        raise hygiene.HygieneError(f"invalid {field} process identity")
    if type(value.get("uid")) is not int or value["uid"] < 0:
        raise hygiene.HygieneError(f"invalid {field} uid")
    if not isinstance(value.get("platform"), str) or not value["platform"]:
        raise hygiene.HygieneError(f"invalid {field} platform")
    if not isinstance(value.get("start_token"), str) or not value["start_token"]:
        raise hygiene.HygieneError(f"invalid {field} start token")
    if type(value.get("process_group_id")) is not int or value["process_group_id"] <= 0:
        raise hygiene.HygieneError(f"invalid {field} process group")
    if value["platform"] == "linux":
        for key in ("boot_id", "pid_namespace"):
            if not isinstance(value.get(key), str) or not value[key]:
                raise hygiene.HygieneError(f"invalid {field} Linux {key}")
    return value


def process_group_present(group_id: int) -> bool | None:
    try:
        os.killpg(group_id, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return None


def inspect_process(identity: Any, *, group: bool = False) -> dict[str, str]:
    """Classify a recorded process without treating a reused PID as its owner."""
    if not isinstance(identity, dict):
        return {"status": "unknown", "reason": "legacy_identity_missing"}
    try:
        expected = validate_process_identity(identity, identity.get("pid"), "recorded")
    except (hygiene.HygieneError, ValueError):
        return {"status": "unknown", "reason": "recorded_identity_invalid"}
    try:
        observer = capture_process_identity(os.getpid())
    except (OSError, PermissionError, ValueError, subprocess.SubprocessError):
        return {"status": "unknown", "reason": "observer_identity_unavailable"}
    if observer.get("platform") != expected["platform"]:
        return {"status": "unknown", "reason": "platform_not_comparable"}
    if expected["platform"] == "linux":
        if observer.get("boot_id") != expected.get("boot_id"):
            return {"status": "identity_mismatch", "reason": "boot_identity_changed"}
        if observer.get("pid_namespace") != expected.get("pid_namespace"):
            return {"status": "unknown", "reason": "pid_namespace_not_comparable"}
    try:
        observed = capture_process_identity(expected["pid"])
    except ProcessLookupError:
        status, reason = "dead", "pid_absent"
    except (OSError, PermissionError, ValueError, subprocess.SubprocessError):
        return {"status": "unknown", "reason": "identity_unavailable"}
    else:
        if all(observed.get(key) == value for key, value in expected.items()):
            return {"status": "alive", "reason": "identity_match"}
        status, reason = "identity_mismatch", "pid_reused_or_namespace_changed"
    if not group:
        return {"status": status, "reason": reason}
    group_present = process_group_present(expected["process_group_id"])
    if group_present is True:
        return {"status": "unknown", "reason": "recorded_leader_gone_but_process_group_present"}
    if group_present is None:
        return {"status": "unknown", "reason": "process_group_uninspectable"}
    return {"status": status, "reason": reason}


def growth_defaults(profile: dict[str, Any]) -> dict[str, int]:
    raw = profile.get("growth_defaults_bytes", DEFAULT_GROWTH_BYTES)
    if not isinstance(raw, dict) or set(raw) != set(DEFAULT_GROWTH_BYTES):
        raise hygiene.HygieneError("invalid host growth defaults")
    if any(type(raw[key]) is not int or raw[key] <= 0 for key in DEFAULT_GROWTH_BYTES):
        raise hygiene.HygieneError("invalid host growth default size")
    return {key: raw[key] for key in DEFAULT_GROWTH_BYTES}


def default_growth_for(profile: dict[str, Any], command: list[str]) -> int:
    defaults = growth_defaults(profile)
    subcommand = command[0]
    if subcommand != "test":
        return defaults[subcommand]
    arguments = command[1:]
    race = any(value == "-race" or value.startswith("-race=") for value in arguments)
    full = any(value in {"...", "all", "std", "cmd", "./..."} or value.endswith("/...")
               for value in arguments)
    if race:
        return defaults["test_race"]
    if full:
        return defaults["test_full"]
    return defaults["test"]


def derive_session_id(cwd: Path, command: list[str]) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", f"{cwd.name}-{command[0]}".lower()).strip("-") or "go"
    return f"{base[:40]}-{uuid.uuid4().hex[:12]}"


def owned_directory(path: Path) -> dict[str, int]:
    identity = hygiene.path_identity(path)
    if not path.is_dir() or identity["uid"] != os.getuid():
        raise hygiene.HygieneError(f"directory must belong to current user: {path}")
    return identity


def private_json(path: Path) -> Any:
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid() or metadata.st_mode & 0o077:
        raise hygiene.HygieneError(f"state file must be private and not a symlink: {path}")
    return hygiene.load_json(path, "host state")


@contextlib.contextmanager
def locked(state: Path) -> Iterator[None]:
    owned_directory(state)
    if state.stat().st_mode & 0o077:
        raise hygiene.HygieneError("host state directory must have mode 0700")
    descriptor = os.open(state / ".budget.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "r+") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def configure(state: Path, cache: Path, modules: Path, min_bytes: int, min_percent: float,
              temp_root: Path | None = None) -> dict[str, Any]:
    for path in (state, cache, modules):
        hygiene.validate_removal_root(path)
    if min_bytes < 0 or not 0 <= min_percent <= 100:
        raise hygiene.HygieneError("invalid free-space floors")
    cache_identity = owned_directory(cache)
    module_identity = owned_directory(modules)
    for path in (cache, modules):
        if hygiene.is_within(state, path) or hygiene.is_within(path, state):
            raise hygiene.HygieneError("host state and language caches must not overlap")
        if any((parent / hygiene.MARKER_NAME).exists() for parent in (path, *path.parents)):
            raise hygiene.HygieneError("cache is nested under a disposable owner")
        if (path / ".git").exists():
            raise hygiene.HygieneError("cannot bind a disposable or repository root as shared cache")
    temporary_root = temp_root or state / "tmp"
    hygiene.validate_removal_root(temporary_root)
    hygiene.reject_ancestor_ownership_boundaries(temporary_root)
    for path in (cache, modules):
        if hygiene.is_within(temporary_root, path) or hygiene.is_within(path, temporary_root):
            raise hygiene.HygieneError("temporary root must not overlap a shared cache")
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    with locked(state):
        identity = owned_directory(state)
        if len({identity["device"], cache_identity["device"], module_identity["device"]}) != 1:
            raise hygiene.HygieneError("state and both caches must share one filesystem")
        profile = {
            "schema_version": 1, "kind": KIND, "identity": identity,
            "gocache": str(cache), "gocache_identity": cache_identity,
            "gomodcache": str(modules), "gomodcache_identity": module_identity,
            "min_free_bytes": min_bytes, "min_free_percent": min_percent,
            "cache_cleanup": "report_only", "budget_accounting": "full_peak_conservative",
            "temp_root": str(temporary_root),
            "growth_defaults_bytes": dict(DEFAULT_GROWTH_BYTES),
        }
        config = state / "config.json"
        if config.is_symlink():
            raise hygiene.HygieneError("host profile must not be a symlink")
        if config.exists():
            existing = private_json(config)
            comparable = {
                key: value for key, value in profile.items()
                if key != "growth_defaults_bytes" or key in existing
            } if isinstance(existing, dict) else profile
            if not isinstance(existing, dict) or any(existing.get(k) != v for k, v in comparable.items()):
                raise hygiene.HygieneError("host profile already exists with different settings")
            load_state(state)
            return existing
        if (state / "budget.json").exists():
            raise hygiene.HygieneError("budget exists without a host profile")
        for path in (state / "runs", temporary_root):
            if path.exists() or path.is_symlink():
                raise hygiene.HygieneError(f"refusing preexisting host runtime directory: {path}")
            if path.parent.stat().st_dev != identity["device"]:
                raise hygiene.HygieneError("temporary and state roots must share one filesystem")
        for path in (state / "runs", temporary_root):
            path.mkdir(mode=0o700)
        profile["runs_identity"] = owned_directory(state / "runs")
        profile["temp_root_identity"] = owned_directory(temporary_root)
        hygiene.exclusive_write_json(state / "budget.json", {"schema_version": 1, "reservations": {}})
        hygiene.exclusive_write_json(config, profile)
        return profile


def load_state(state: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    profile = private_json(state / "config.json")
    if not isinstance(profile, dict) or profile.get("kind") != KIND or profile.get("schema_version") != 1:
        raise hygiene.HygieneError("invalid host profile")
    if profile.get("identity") != owned_directory(state):
        raise hygiene.HygieneError("host state identity changed")
    for key in ("gocache", "gomodcache"):
        path = Path(hygiene.nonempty_string(profile.get(key), key))
        if not path.is_absolute() or owned_directory(path) != profile.get(key + "_identity"):
            raise hygiene.HygieneError(f"{key} identity changed")
    temporary_root = Path(hygiene.nonempty_string(profile.get("temp_root"), "temp_root"))
    for path, key in ((state / "runs", "runs_identity"), (temporary_root, "temp_root_identity")):
        if not path.is_absolute() or owned_directory(path) != profile.get(key):
            raise hygiene.HygieneError("host runtime directory identity changed")
    if type(profile.get("min_free_bytes")) is not int or profile["min_free_bytes"] < 0:
        raise hygiene.HygieneError("invalid host byte floor")
    if not isinstance(profile.get("min_free_percent"), (int, float)) or not 0 <= profile["min_free_percent"] <= 100:
        raise hygiene.HygieneError("invalid host percentage floor")
    growth_defaults(profile)
    ledger = private_json(state / "budget.json")
    if not isinstance(ledger, dict) or ledger.get("schema_version") != 1 or not isinstance(ledger.get("reservations"), dict):
        raise hygiene.HygieneError("invalid budget ledger")
    for token, record in ledger["reservations"].items():
        if not isinstance(record, dict) or record.get("id") != token:
            raise hygiene.HygieneError("invalid reservation identity")
        hygiene.identifier(token, "reservation id")
        if type(record.get("expected_growth_bytes")) is not int or record["expected_growth_bytes"] <= 0:
            raise hygiene.HygieneError("invalid reservation size")
        if type(record.get("owner_pid")) is not int or record["owner_pid"] <= 0:
            raise hygiene.HygieneError("invalid reservation owner")
        if "owner_identity" in record:
            validate_process_identity(record["owner_identity"], record["owner_pid"], "reservation owner")
        if record.get("phase") not in {"reserved", "launched", "outcome_unknown"}:
            raise hygiene.HygieneError("invalid reservation phase")
        child = record.get("child_pid")
        if child is not None and (type(child) is not int or child <= 0):
            raise hygiene.HygieneError("invalid reservation process group")
        if "child_identity" in record:
            if child is None:
                raise hygiene.HygieneError("child identity exists without a process group")
            validate_process_identity(record["child_identity"], child, "reservation child")
    return profile, ledger


def reservation_liveness(record: dict[str, Any]) -> dict[str, Any]:
    owner = inspect_process(record.get("owner_identity"))
    child = (
        inspect_process(record.get("child_identity"), group=True)
        if record.get("child_pid") is not None
        else {"status": "unknown", "reason": "child_not_recorded"}
    )
    if owner["reason"] == "legacy_identity_missing" or (
        record.get("child_pid") is not None and child["reason"] == "legacy_identity_missing"
    ):
        reconcile_class = "legacy_identity_missing"
    elif owner["status"] == "alive":
        reconcile_class = "owner_alive"
    elif owner["status"] == "unknown":
        reconcile_class = "owner_unknown"
    elif record.get("child_pid") is None:
        reconcile_class = "launch_outcome_unknown"
    elif child["status"] == "alive":
        reconcile_class = "child_alive"
    elif child["status"] == "unknown":
        reconcile_class = "child_unknown"
    else:
        reconcile_class = "eligible"
    return {"owner": owner, "child": child, "reconcile_class": reconcile_class}


def budget_status(state: Path) -> dict[str, Any]:
    with locked(state):
        profile, ledger = load_state(state)
        records = list(ledger["reservations"].values())
        reserved = sum(record["expected_growth_bytes"] for record in records)
        report = hygiene.check_disk(state, profile["min_free_bytes"], profile["min_free_percent"], reserved)
        if reserved > report["free_bytes"]:
            report["healthy"] = False
            report["reasons"].append("reservations exceed actual free bytes")
        rendered = []
        assessments = []
        for record in sorted(records, key=lambda item: item["id"]):
            assessment = reservation_liveness(record)
            assessments.append(assessment)
            rendered.append({
                "id": record["id"],
                "session_id": record.get("session_id"),
                "cwd": record.get("cwd"),
                "phase": record["phase"],
                "expected_growth_bytes": record["expected_growth_bytes"],
                "owner_liveness": assessment["owner"],
                "child_liveness": assessment["child"],
                "reconcile_class": assessment["reconcile_class"],
                "record_sha256": record_hash(record),
            })
        owner_counts = {status: 0 for status in sorted(LIVENESS_STATES)}
        child_counts = {status: 0 for status in sorted(LIVENESS_STATES)}
        class_counts: dict[str, int] = {}
        for assessment in assessments:
            owner_counts[assessment["owner"]["status"]] += 1
            child_counts[assessment["child"]["status"]] += 1
            key = assessment["reconcile_class"]
            class_counts[key] = class_counts.get(key, 0) + 1
        return {
            "kind": KIND,
            "state_root": str(state),
            "gocache": profile["gocache"],
            "reserved_bytes": reserved,
            "reservation_count": len(records),
            "summary": {
                "owner_liveness": owner_counts,
                "child_liveness": child_counts,
                "reconcile_class": dict(sorted(class_counts.items())),
            },
            "disk": report,
            "reservations": rendered,
            "policy": "status/reconcile never expire reservations or delete caches/temporary data",
        }


def reserve(state: Path, session_id: str, cwd: Path, growth: int) -> tuple[dict[str, Any], dict[str, Any]]:
    hygiene.identifier(session_id, "session_id")
    if type(growth) is not int or growth <= 0:
        raise hygiene.HygieneError("expected growth must be positive")
    with locked(state):
        profile, ledger = load_state(state)
        if not cwd.is_dir() or cwd.stat().st_dev != profile["identity"]["device"]:
            raise hygiene.HygieneError("command cwd must share the configured filesystem")
        reserved = sum(r["expected_growth_bytes"] for r in ledger["reservations"].values())
        report = hygiene.check_disk(state, profile["min_free_bytes"], profile["min_free_percent"], growth + reserved)
        if not report["healthy"] or growth + reserved > report["free_bytes"]:
            raise BlockingIOError(f"disk admission refused: reserved={reserved}, requested={growth}, projected={report['projected_free_bytes']}")
        token = uuid.uuid4().hex
        record = {
            "id": token, "session_id": session_id, "cwd": str(cwd),
            "expected_growth_bytes": growth, "owner_pid": os.getpid(),
            "owner_identity": capture_process_identity(os.getpid()),
            "child_pid": None, "phase": "reserved", "created_at": hygiene.format_time(hygiene.utc_now()),
        }
        ledger["reservations"][token] = record
        hygiene.atomic_write_json(state / "budget.json", ledger)
        return profile, record


def update_reservation(state: Path, token: str, **changes: Any) -> None:
    with locked(state):
        _, ledger = load_state(state)
        record = ledger["reservations"].get(token)
        if record is None or record["owner_pid"] != os.getpid():
            raise hygiene.HygieneError("reservation is not owned by this process")
        owner = inspect_process(record.get("owner_identity"))
        if owner["status"] != "alive":
            raise hygiene.HygieneError("reservation owner identity no longer matches this process")
        record.update(changes)
        hygiene.atomic_write_json(state / "budget.json", ledger)


def finish_budget(state: Path, record: dict[str, Any], outcome: dict[str, Any]) -> None:
    token = record["id"]
    with locked(state):
        _, ledger = load_state(state)
        if ledger["reservations"].get(token) != record:
            raise hygiene.HygieneError("reservation changed before release")
        run_root = state / "runs" / token
        run_root.mkdir(mode=0o700, exist_ok=True)
        hygiene.atomic_write_json(run_root / "outcome.json", outcome)
        del ledger["reservations"][token]
        hygiene.atomic_write_json(state / "budget.json", ledger)


def reconcile(state: Path, token: str, expected_hash: str) -> dict[str, Any]:
    hygiene.identifier(token, "reservation id")
    with locked(state):
        _, ledger = load_state(state)
        record = ledger["reservations"].get(token)
        if record is None or record_hash(record) != expected_hash:
            raise hygiene.HygieneError("reservation digest does not match")
        assessment = reservation_liveness(record)
        if assessment["reconcile_class"] == "legacy_identity_missing":
            raise hygiene.HygieneError("legacy reservation lacks process identity and cannot be reconciled automatically")
        if assessment["reconcile_class"] != "eligible":
            raise hygiene.HygieneError(
                "reservation is not reconcilable: " + assessment["reconcile_class"]
            )
        result = {
            "id": token,
            "status": "budget_reconciled",
            "temporary_data": "retained",
            "record": record,
            "liveness": assessment,
        }
        run_root = state / "runs" / token
        run_root.mkdir(mode=0o700, exist_ok=True)
        hygiene.atomic_write_json(run_root / "outcome.json", result)
        del ledger["reservations"][token]
        hygiene.atomic_write_json(state / "budget.json", ledger)
        return result


def existing_parent_identity(path: Path) -> dict[str, Any]:
    candidate = path.parent
    while not candidate.exists():
        if candidate == candidate.parent:
            raise hygiene.HygieneError(f"no existing parent for prospective path: {path}")
        candidate = candidate.parent
    identity = owned_directory(candidate)
    return {"path": str(candidate), **identity}


def validate_prospective_configuration(
    profile: dict[str, Any], state: Path, new_state_root: Path,
    new_gocache: Path, gomodcache: Path, new_temp_root: Path,
) -> dict[str, Any]:
    for path in (new_state_root, new_gocache, gomodcache, new_temp_root):
        hygiene.validate_removal_root(path)
    for path, label in ((new_state_root, "state root"), (new_temp_root, "temporary root")):
        if path.exists() or path.is_symlink():
            raise hygiene.HygieneError(f"prospective {label} must not already exist")
        hygiene.reject_ancestor_ownership_boundaries(path)
    cache_identity = owned_directory(new_gocache)
    module_identity = owned_directory(gomodcache)
    for path, label in ((new_gocache, "gocache"), (gomodcache, "gomodcache")):
        if any((parent / hygiene.MARKER_NAME).exists() for parent in (path, *path.parents)):
            raise hygiene.HygieneError(f"prospective {label} is nested under a disposable owner")
        if (path / ".git").exists():
            raise hygiene.HygieneError(f"prospective {label} cannot be a repository root")
    old_gocache = Path(profile["gocache"])
    old_temp_root = Path(profile["temp_root"])
    if hygiene.is_within(new_gocache, old_gocache) or hygiene.is_within(old_gocache, new_gocache):
        raise hygiene.HygieneError("prospective gocache must be distinct from the old gocache")
    if hygiene.is_within(new_temp_root, old_temp_root) or hygiene.is_within(old_temp_root, new_temp_root):
        raise hygiene.HygieneError("prospective temporary root must be distinct from the old temporary root")
    protected = [state, old_gocache, Path(profile["gomodcache"]), old_temp_root]
    for path in (new_state_root, new_gocache, new_temp_root):
        if any(hygiene.is_within(path, old) or hygiene.is_within(old, path) for old in protected):
            raise hygiene.HygieneError("prospective roots must not overlap old state, cache, or temporary roots")
    prospective = [new_state_root, new_gocache, gomodcache, new_temp_root]
    for index, path in enumerate(prospective):
        for other in prospective[index + 1:]:
            if hygiene.is_within(path, other) or hygiene.is_within(other, path):
                raise hygiene.HygieneError("prospective state, cache, module, and temporary roots must not overlap")
    state_parent = existing_parent_identity(new_state_root)
    temp_parent = existing_parent_identity(new_temp_root)
    devices = {
        state_parent["device"], cache_identity["device"],
        module_identity["device"], temp_parent["device"],
    }
    if len(devices) != 1:
        raise hygiene.HygieneError("prospective state, caches, and temporary root must share the same filesystem")
    return {
        "status": "valid_but_not_authorized",
        "state_root": str(new_state_root),
        "state_parent_identity": state_parent,
        "gocache": str(new_gocache),
        "gocache_identity": cache_identity,
        "gomodcache": str(gomodcache),
        "gomodcache_identity": module_identity,
        "temp_root": str(new_temp_root),
        "temp_parent_identity": temp_parent,
    }


def migration_preflight(state: Path, new_state_root: Path | None = None,
                        new_gocache: Path | None = None, gomodcache: Path | None = None,
                        new_temp_root: Path | None = None) -> dict[str, Any]:
    """Read-only gate for adopting identity-aware reservations on a state root."""
    with locked(state):
        profile, ledger = load_state(state)
        legacy_records = [
            record for record in ledger["reservations"].values()
            if "owner_identity" not in record or (
                record.get("child_pid") is not None and "child_identity" not in record
            )
        ]
    rendered = [{
        "id": record["id"],
        "phase": record["phase"],
        "child_pid_recorded": record.get("child_pid") is not None,
        "record_sha256": record_hash(record),
    } for record in sorted(legacy_records, key=lambda item: item["id"])]
    result = {
        "kind": MIGRATION_PREFLIGHT_KIND,
        "current_state_root": str(state),
        "legacy_reservation_count": len(rendered),
        "legacy_reservations": rendered,
        "old_state_preserved": True,
        "mutation_authorized": False,
    }
    supplied = (new_state_root, new_gocache, gomodcache, new_temp_root)
    if any(value is not None for value in supplied) and not all(value is not None for value in supplied):
        raise hygiene.HygieneError(
            "prospective configuration requires new state root, new gocache, gomodcache, and new temp root"
        )
    prospective = None
    if all(value is not None for value in supplied):
        prospective = validate_prospective_configuration(
            profile, state, new_state_root, new_gocache, gomodcache, new_temp_root
        )
    if not rendered:
        result.update({
            "status": "ready",
            "decision": "same_root_allowed",
            "selected_state_root": str(state),
            "prospective_configuration": prospective,
        })
        return result
    result.update({
        "status": "blocked",
        "decision": "blocked_requires_external_drain",
        "selected_state_root": None,
        "prospective_configuration": prospective,
    })
    return result


def record_hash(record: dict[str, Any]) -> str:
    return hygiene.sha256_bytes(json.dumps(record, sort_keys=True, separators=(",", ":")).encode())


class SignalRelay:
    """Forward only while the recorded child leader is still the live process."""

    def __init__(self) -> None:
        self.process: subprocess.Popen[Any] | None = None
        self.child_identity: dict[str, Any] | None = None
        self.pending: list[int] = []

    def __call__(self, signum: int, _frame: Any) -> None:
        if self.process is None or self.child_identity is None or self.process.poll() is not None:
            self.pending.append(signum)
            return
        observation = inspect_process(self.child_identity, group=True)
        if observation["status"] != "alive":
            self.pending.append(signum)
            return
        try:
            os.killpg(self.child_identity["process_group_id"], signum)
        except (ProcessLookupError, PermissionError):
            self.pending.append(signum)


def run_go(state: Path, cwd: Path, session_id: str | None, growth: int | None,
           binary: str, command: list[str]) -> int:
    if not command or command[0] not in COMMANDS:
        raise hygiene.HygieneError("use a Go build/test command; clean and configuration mutations are not supported")
    if command[0] == "env" and any(x in {"-w", "-u"} or x.startswith(("-w=", "-u=")) for x in command[1:]):
        raise hygiene.HygieneError("go env mutation is not supported")
    if os.environ.get("GOCACHEPROG"):
        raise hygiene.HygieneError("external GOCACHEPROG is outside the configured disk budget")
    if os.environ.get("DEV_DISK_BUDGET_ACTIVE_RUN"):
        raise hygiene.HygieneError("nested guarded runs would double-reserve the same command tree")
    executable = shutil.which(binary)
    if executable is None:
        raise hygiene.HygieneError("Go executable is unavailable")
    if session_id is None:
        session_id = derive_session_id(cwd, command)
    growth_source = "explicit"
    if growth is None:
        with locked(state):
            profile_for_defaults, _ = load_state(state)
        growth = default_growth_for(profile_for_defaults, command)
        growth_source = "profile_default"
    relay = SignalRelay()
    previous_handlers = {
        signum: signal.signal(signum, relay) for signum in (signal.SIGINT, signal.SIGTERM)
    }
    handlers_restored = False
    record = None
    launched = False

    def restore_handlers() -> None:
        nonlocal handlers_restored
        if handlers_restored:
            return
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)
        handlers_restored = True

    try:
        profile, record = reserve(state, session_id, cwd, growth)
        token = record["id"]
        run_root = state / "runs" / token
        manifest = run_root / "resources.json"
        tmp = Path(profile["temp_root"]) / token
        if relay.pending:
            signum = relay.pending[-1]
            finish_budget(state, record, {
                "id": token, "status": "interrupted_before_launch",
                "signal": signum, "temporary_data": "not_created",
            })
            return 128 + signum
        run_root.mkdir(mode=0o700)
        hygiene.init_manifest(manifest, token, hygiene.utc_now())
        hygiene.claim_directory(manifest, "go-tmp", str(tmp), "guarded Go invocation temporary data",
                                "delete_when_released", hygiene.utc_now(), role="go_tmp", scope="task", task_id=session_id)
        hygiene.start_run(manifest, hygiene.utc_now())
        env = dict(os.environ, GOCACHE=profile["gocache"], GOMODCACHE=profile["gomodcache"],
                   GOTMPDIR=str(tmp), TMPDIR=str(tmp), DEV_DISK_BUDGET_ACTIVE_RUN=token)
        hygiene.exclusive_write_json(run_root / "launch.json", {
            "reservation": record, "go_binary": executable, "command": command,
            "environment": {k: env[k] for k in ("GOCACHE", "GOMODCACHE", "GOTMPDIR", "TMPDIR")},
        })
        if relay.pending:
            signum = relay.pending[-1]
            hygiene.finish_run(manifest, "interrupted", hygiene.utc_now())
            finish_budget(state, record, {
                "id": token, "status": "interrupted_before_launch",
                "signal": signum, "temporary_data": "retained",
            })
            return 128 + signum
        process = subprocess.Popen([executable, *command], cwd=cwd, env=env, start_new_session=True)
        relay.process = process
        launched = True
        try:
            child_identity = capture_process_identity(process.pid)
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            update_reservation(state, token, child_pid=process.pid, phase="outcome_unknown")
            record.update(child_pid=process.pid, phase="outcome_unknown")
            raise hygiene.HygieneError(f"child process identity could not be recorded: {error}") from error
        update_reservation(
            state, token, child_pid=process.pid, child_identity=child_identity, phase="launched"
        )
        record.update(child_pid=process.pid, child_identity=child_identity, phase="launched")
        relay.child_identity = child_identity
        pending = list(relay.pending)
        relay.pending.clear()
        for signum in pending:
            relay(signum, None)
        print(json.dumps({"guarded_go": "started", "id": token, "session_id": session_id,
                          "reserved_bytes": growth, "growth_source": growth_source,
                          "gocache": profile["gocache"], "gotmpdir": str(tmp)}), file=sys.stderr)
        returncode = process.wait()
        relay.child_identity = None
        child_liveness = inspect_process(record["child_identity"], group=True)
        if child_liveness["status"] not in {"dead", "identity_mismatch"}:
            update_reservation(state, token, phase="outcome_unknown")
            record.update(phase="outcome_unknown")
            print(json.dumps({"guarded_go": "retained", "id": token,
                              "reason": child_liveness["reason"] + "; budget and temporary data retained"}),
                  file=sys.stderr)
            return 75
        restore_handlers()
        if relay.pending:
            returncode = -relay.pending[-1]
        hygiene.finish_run(manifest, "succeeded" if returncode == 0 else "failed", hygiene.utc_now())
        cleanup = "retained_after_failure"
        if returncode == 0:
            try:
                plan = hygiene.build_plan(manifest, min_age_seconds=0, ttl_seconds=900, now=hygiene.utc_now())
                plan_path = run_root / "cleanup-plan.json"
                hygiene.exclusive_write_json(plan_path, plan)
                receipt = hygiene.execute_cleanup(plan_path, expected_plan_sha256=hygiene.sha256_file(plan_path),
                                                   receipt_path=run_root / "cleanup-receipt.json", now=hygiene.utc_now())
                cleanup = receipt["status"] if not tmp.exists() else "retained_for_review"
            except (hygiene.HygieneError, OSError) as error:
                cleanup = "retained_for_review"
                print(json.dumps({"guarded_go": cleanup, "id": token, "reason": str(error)}), file=sys.stderr)
        finish_budget(state, record, {"id": token, "status": "completed", "returncode": returncode, "cleanup": cleanup})
        return returncode if returncode >= 0 else 128 - returncode
    except BaseException:
        if record is not None and not launched:
            finish_budget(state, record, {"id": token, "status": "launch_failed", "temporary_data": "retained"})
        elif record is not None:
            update_reservation(state, token, phase="outcome_unknown")
        raise
    finally:
        restore_handlers()


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    root.add_argument("--state-root", type=Path, default=default_state())
    commands = root.add_subparsers(dest="action", required=True)
    configure_parser = commands.add_parser("configure", help="Bind existing shared caches without granting cleanup authority.")
    configure_parser.add_argument("--gocache", type=Path, required=True)
    configure_parser.add_argument("--gomodcache", type=Path, required=True)
    configure_parser.add_argument("--temp-root", type=Path, default=Path(tempfile.gettempdir()) / f"guarded-go-{os.getuid()}",
                                  help="New private temporary parent outside repositories and caches.")
    configure_parser.add_argument("--min-free-bytes", type=int, default=hygiene.DEFAULT_MIN_FREE_BYTES)
    configure_parser.add_argument("--min-free-percent", type=float, default=hygiene.DEFAULT_MIN_FREE_PERCENT)
    commands.add_parser("status", help="Report reservations and projected headroom; never expire or delete them.")
    run = commands.add_parser("run", help="Atomically reserve disk, isolate temporary output, and execute Go.")
    run.add_argument("--cwd", type=Path, default=Path.cwd())
    run.add_argument("--session-id", help="Optional lowercase reservation label; generated when omitted.")
    run.add_argument("--expected-growth-bytes", type=int,
                     help="Override the conservative command-class default from the host profile.")
    run.add_argument("--go-binary", default="go")
    run.add_argument("go_args", nargs=argparse.REMAINDER)
    release = commands.add_parser("reconcile", help="Release a reviewed abandoned budget, retaining all temporary data.")
    release.add_argument("--id", required=True)
    release.add_argument("--expect-record-sha256", required=True)
    migration = commands.add_parser(
        "migration-preflight",
        help="Read-only gate for same-root identity-aware rollout and prospective configuration checks.",
    )
    migration.add_argument("--new-state-root", type=Path)
    migration.add_argument("--new-gocache", type=Path)
    migration.add_argument("--gomodcache", type=Path)
    migration.add_argument("--new-temp-root", type=Path)
    return root


def main() -> int:
    args = parser().parse_args()
    try:
        state = hygiene.canonical_path(args.state_root)
        if args.action == "configure":
            result = configure(state, hygiene.canonical_path(args.gocache, must_exist=True),
                               hygiene.canonical_path(args.gomodcache, must_exist=True),
                               args.min_free_bytes, args.min_free_percent,
                               hygiene.canonical_path(args.temp_root))
        elif args.action == "status":
            result = budget_status(state)
        elif args.action == "reconcile":
            result = reconcile(state, args.id, args.expect_record_sha256)
        elif args.action == "migration-preflight":
            new_state_root = (
                hygiene.canonical_path(args.new_state_root)
                if args.new_state_root is not None else None
            )
            new_gocache = (
                hygiene.canonical_path(args.new_gocache, must_exist=True)
                if args.new_gocache is not None else None
            )
            gomodcache = (
                hygiene.canonical_path(args.gomodcache, must_exist=True)
                if args.gomodcache is not None else None
            )
            new_temp_root = (
                hygiene.canonical_path(args.new_temp_root)
                if args.new_temp_root is not None else None
            )
            result = migration_preflight(
                state, new_state_root, new_gocache, gomodcache, new_temp_root
            )
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0 if result["status"] == "ready" else 75
        else:
            command = args.go_args[1:] if args.go_args[:1] == ["--"] else args.go_args
            return run_go(state, hygiene.canonical_path(args.cwd, must_exist=True), args.session_id,
                          args.expected_growth_bytes, args.go_binary, command)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except (hygiene.HygieneError, OSError, ValueError) as error:
        print(json.dumps({"kind": KIND + "-error", "error": str(error)}), file=sys.stderr)
        return 75 if isinstance(error, BlockingIOError) else 2


if __name__ == "__main__":
    raise SystemExit(main())
