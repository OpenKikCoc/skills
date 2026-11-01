#!/usr/bin/env python3
"""Launch and observe self-advancing remote tasks through SSH and tmux."""

from __future__ import annotations

import argparse
import base64
import copy
import hashlib
import io
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
import zlib
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence


SCHEMA_VERSION = 1
TASK_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")
SESSION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,62}$")
HOST_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,252}$")
USER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
STEP_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$")
ENV_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
SENSITIVE_NAME_PATTERN = re.compile(
    r"(?i)(?:password|passwd|passphrase|token|secret|api[_-]?key|"
    r"access[_-]?key|private[_-]?key|credential|authorization|cookie|"
    r"session|keytab)"
)
SENSITIVE_ARG_PATTERN = re.compile(
    r"(?i)^--?(?:password|passwd|passphrase|token|secret|api[-_]?key|"
    r"access[-_]?key|private[-_]?key|client[-_]?secret|credential|"
    r"authorization|cookie|session|keytab)(?:=|$)"
)
SENSITIVE_HEADER_PATTERN = re.compile(
    r"(?i)^\s*(?:authorization|proxy-authorization|cookie|set-cookie)"
    r"(?:\s*[:;]|\s*$)"
)
SENSITIVE_VALUE_PATTERNS = (
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+"),
    re.compile(r"(?i)\bBasic\s+[A-Za-z0-9._~+/=-]+"),
    re.compile(
        r"(?i)\b(?:password|passwd|passphrase|token|secret|api[_-]?key|"
        r"access[_-]?key|client[_-]?secret|session|cookie|keytab)"
        r"\s*[:=]\s*\S+"
    ),
    re.compile(r"(?i)://[^/\s:@]+:[^/\s@]+@"),
    re.compile(r"-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----"),
)
ALLOWED_AMBIENT_ENV = (
    "HOME",
    "LANG",
    "LC_ALL",
    "LOGNAME",
    "PATH",
    "SHELL",
    "TZ",
    "USER",
)
TOP_LEVEL_KEYS = {
    "schema_version",
    "task_id",
    "remote",
    "steps",
    "restoration",
    "artifacts",
    "policy",
}
REMOTE_KEYS = {"host", "user", "port", "state_dir", "work_dir", "session"}
STEP_KEYS = {"id", "argv", "cwd", "env", "timeout_seconds"}
RESTORATION_KEYS = {"steps", "checks"}
ARTIFACT_KEYS = {"path", "required"}
POLICY_KEYS = {
    "heartbeat_interval_seconds",
    "heartbeat_timeout_seconds",
    "stall_timeout_seconds",
    "task_timeout_seconds",
    "default_step_timeout_seconds",
    "restoration_timeout_seconds",
    "terminate_grace_seconds",
}
POLICY_DEFAULTS: Dict[str, float] = {
    "heartbeat_interval_seconds": 10.0,
    "heartbeat_timeout_seconds": 45.0,
    "stall_timeout_seconds": 600.0,
    "task_timeout_seconds": 86400.0,
    "default_step_timeout_seconds": 21600.0,
    "restoration_timeout_seconds": 900.0,
    "terminate_grace_seconds": 10.0,
}
CONTROL_FILES = (
    "manifest.json",
    "state.json",
    "heartbeat.json",
    "terminal.json",
    "artifact-receipt.json",
    "restoration-receipt.json",
    "handoff.json",
    "events.jsonl",
    "runner.log",
)
COMPLETE_STATUSES = {"succeeded", "failed"}
REMOTE_PYTHON_EXECUTABLE = "python3"
REMOTE_TMUX_EXECUTABLE = "tmux"
MIN_REMOTE_PYTHON_VERSION = (3, 9)
REQUIRED_REMOTE_PYTHON_MODULES = (
    "argparse",
    "base64",
    "copy",
    "datetime",
    "hashlib",
    "io",
    "json",
    "os",
    "pathlib",
    "re",
    "shlex",
    "shutil",
    "signal",
    "subprocess",
    "sys",
    "tarfile",
    "tempfile",
    "time",
    "typing",
    "zlib",
)


class SupervisorError(RuntimeError):
    """A deterministic task-supervision error."""


class TransportError(SupervisorError):
    """An SSH transport operation failed."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def object_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    payload = json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
    with temporary.open("w", encoding="utf-8") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    _fsync_directory(path.parent)


def read_json(path: Path) -> Dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SupervisorError(f"cannot read JSON {path}: {error}") from error
    if not isinstance(value, dict):
        raise SupervisorError(f"JSON root must be an object: {path}")
    return value


def append_json_line(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = canonical_json_bytes(dict(value)) + b"\n"
    with path.open("ab", buffering=0) as stream:
        stream.write(payload)
        os.fsync(stream.fileno())


def reject_unknown_keys(value: Mapping[str, Any], allowed: set[str], label: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise SupervisorError(f"{label} contains unsupported fields: {unknown}")


def require_mapping(value: Any, label: str) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise SupervisorError(f"{label} must be an object")
    return dict(value)


def require_list(value: Any, label: str) -> List[Any]:
    if not isinstance(value, list):
        raise SupervisorError(f"{label} must be an array")
    return list(value)


def require_nonempty_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise SupervisorError(f"{label} must be a non-empty string without NUL")
    return value


def validate_posix_absolute_path(value: Any, label: str) -> str:
    raw = require_nonempty_string(value, label)
    if any(ord(character) < 32 for character in raw):
        raise SupervisorError(f"{label} must not contain control characters")
    path = PurePosixPath(raw)
    if not path.is_absolute() or raw == "/" or ".." in path.parts:
        raise SupervisorError(
            f"{label} must be an absolute non-root POSIX path without '..'"
        )
    return raw


def validate_relative_path(value: Any, label: str, allow_dot: bool = True) -> str:
    raw = require_nonempty_string(value, label)
    if any(ord(character) < 32 for character in raw):
        raise SupervisorError(f"{label} must not contain control characters")
    path = PurePosixPath(raw)
    if path.is_absolute() or ".." in path.parts:
        raise SupervisorError(f"{label} must be a relative path without '..'")
    if raw == "." and not allow_dot:
        raise SupervisorError(f"{label} must name a file")
    return raw


def reject_sensitive_value(value: str, label: str) -> None:
    if SENSITIVE_HEADER_PATTERN.search(value):
        raise SupervisorError(f"{label} declares a credential-bearing header")
    for pattern in SENSITIVE_VALUE_PATTERNS:
        if pattern.search(value):
            raise SupervisorError(f"{label} appears to contain credential material")


def validate_argv(value: Any, label: str) -> List[str]:
    raw = require_list(value, label)
    if not raw:
        raise SupervisorError(f"{label} must not be empty")
    argv: List[str] = []
    for index, item in enumerate(raw):
        argument = require_nonempty_string(item, f"{label}[{index}]")
        if SENSITIVE_ARG_PATTERN.search(argument):
            raise SupervisorError(
                f"{label}[{index}] requests an inline credential; use remote "
                "the remote host's own credential mechanism (e.g. an agent on "
                "that host) outside this supervisor"
            )
        reject_sensitive_value(argument, f"{label}[{index}]")
        argv.append(argument)
    return argv


def validate_command(value: Any, label: str) -> Dict[str, Any]:
    command = require_mapping(value, label)
    reject_unknown_keys(command, STEP_KEYS, label)
    command_id = require_nonempty_string(command.get("id"), f"{label}.id")
    if not STEP_ID_PATTERN.fullmatch(command_id):
        raise SupervisorError(f"{label}.id has an invalid format")
    normalized: Dict[str, Any] = {
        "id": command_id,
        "argv": validate_argv(command.get("argv"), f"{label}.argv"),
        "cwd": validate_relative_path(command.get("cwd", "."), f"{label}.cwd"),
        "env": {},
    }
    environment = require_mapping(command.get("env", {}), f"{label}.env")
    for name, raw_value in environment.items():
        if not isinstance(name, str) or not ENV_NAME_PATTERN.fullmatch(name):
            raise SupervisorError(f"{label}.env has an invalid variable name")
        if SENSITIVE_NAME_PATTERN.search(name):
            raise SupervisorError(
                f"{label}.env.{name} is credential-like and is not accepted"
            )
        env_value = require_nonempty_string(raw_value, f"{label}.env.{name}")
        reject_sensitive_value(env_value, f"{label}.env.{name}")
        normalized["env"][name] = env_value
    if "timeout_seconds" in command:
        timeout = command["timeout_seconds"]
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
            raise SupervisorError(f"{label}.timeout_seconds must be positive")
        normalized["timeout_seconds"] = float(timeout)
    return normalized


def validate_manifest(value: Any) -> Dict[str, Any]:
    manifest = require_mapping(value, "manifest")
    reject_unknown_keys(manifest, TOP_LEVEL_KEYS, "manifest")
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise SupervisorError(f"manifest.schema_version must be {SCHEMA_VERSION}")
    task_id = require_nonempty_string(manifest.get("task_id"), "manifest.task_id")
    if not TASK_ID_PATTERN.fullmatch(task_id):
        raise SupervisorError("manifest.task_id has an invalid format")

    remote = require_mapping(manifest.get("remote"), "manifest.remote")
    reject_unknown_keys(remote, REMOTE_KEYS, "manifest.remote")
    host = require_nonempty_string(remote.get("host"), "manifest.remote.host")
    if not HOST_PATTERN.fullmatch(host) or host.startswith("-"):
        raise SupervisorError("manifest.remote.host has an invalid format")
    normalized_remote: Dict[str, Any] = {
        "host": host,
        "state_dir": validate_posix_absolute_path(
            remote.get("state_dir"), "manifest.remote.state_dir"
        ),
        "work_dir": validate_posix_absolute_path(
            remote.get("work_dir"), "manifest.remote.work_dir"
        ),
    }
    if "user" in remote:
        user = require_nonempty_string(remote["user"], "manifest.remote.user")
        if not USER_PATTERN.fullmatch(user):
            raise SupervisorError("manifest.remote.user has an invalid format")
        normalized_remote["user"] = user
    if "port" in remote:
        port = remote["port"]
        if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
            raise SupervisorError("manifest.remote.port must be an integer in 1..65535")
        normalized_remote["port"] = port
    session = remote.get("session", f"rr-{task_id}")
    session = require_nonempty_string(session, "manifest.remote.session")
    if not SESSION_PATTERN.fullmatch(session):
        raise SupervisorError("manifest.remote.session has an invalid format")
    normalized_remote["session"] = session

    steps = [
        validate_command(item, f"manifest.steps[{index}]")
        for index, item in enumerate(require_list(manifest.get("steps"), "manifest.steps"))
    ]
    if not steps:
        raise SupervisorError("manifest.steps must contain at least one command")
    all_ids = [step["id"] for step in steps]

    restoration = require_mapping(manifest.get("restoration"), "manifest.restoration")
    reject_unknown_keys(restoration, RESTORATION_KEYS, "manifest.restoration")
    restoration_steps = [
        validate_command(item, f"manifest.restoration.steps[{index}]")
        for index, item in enumerate(
            require_list(restoration.get("steps", []), "manifest.restoration.steps")
        )
    ]
    restoration_checks = [
        validate_command(item, f"manifest.restoration.checks[{index}]")
        for index, item in enumerate(
            require_list(restoration.get("checks"), "manifest.restoration.checks")
        )
    ]
    if not restoration_checks:
        raise SupervisorError(
            "manifest.restoration.checks must contain at least one explicit check"
        )
    all_ids.extend(item["id"] for item in restoration_steps)
    all_ids.extend(item["id"] for item in restoration_checks)
    if len(all_ids) != len(set(all_ids)):
        raise SupervisorError("command IDs must be unique across the manifest")

    artifacts: List[Dict[str, Any]] = []
    artifact_paths: set[str] = set()
    for index, item in enumerate(
        require_list(manifest.get("artifacts", []), "manifest.artifacts")
    ):
        artifact = require_mapping(item, f"manifest.artifacts[{index}]")
        reject_unknown_keys(artifact, ARTIFACT_KEYS, f"manifest.artifacts[{index}]")
        path = validate_relative_path(
            artifact.get("path"), f"manifest.artifacts[{index}].path", allow_dot=False
        )
        if path in artifact_paths:
            raise SupervisorError(f"duplicate artifact path: {path}")
        artifact_paths.add(path)
        required = artifact.get("required", True)
        if not isinstance(required, bool):
            raise SupervisorError(
                f"manifest.artifacts[{index}].required must be boolean"
            )
        artifacts.append({"path": path, "required": required})

    raw_policy = require_mapping(manifest.get("policy", {}), "manifest.policy")
    reject_unknown_keys(raw_policy, POLICY_KEYS, "manifest.policy")
    policy = dict(POLICY_DEFAULTS)
    for name, raw_value in raw_policy.items():
        if (
            not isinstance(raw_value, (int, float))
            or isinstance(raw_value, bool)
            or raw_value <= 0
        ):
            raise SupervisorError(f"manifest.policy.{name} must be positive")
        policy[name] = float(raw_value)
    if policy["heartbeat_timeout_seconds"] <= policy["heartbeat_interval_seconds"]:
        raise SupervisorError(
            "heartbeat_timeout_seconds must exceed heartbeat_interval_seconds"
        )

    normalized = {
        "schema_version": SCHEMA_VERSION,
        "task_id": task_id,
        "remote": normalized_remote,
        "steps": steps,
        "restoration": {
            "steps": restoration_steps,
            "checks": restoration_checks,
        },
        "artifacts": artifacts,
        "policy": policy,
    }
    return normalized


def load_manifest(path: Path) -> Dict[str, Any]:
    return validate_manifest(read_json(path))


def shell_join(argv: Sequence[str]) -> str:
    return " ".join(shlex.quote(argument) for argument in argv)


def ssh_target(manifest: Mapping[str, Any]) -> str:
    remote = manifest["remote"]
    return (
        f"{remote['user']}@{remote['host']}"
        if remote.get("user")
        else str(remote["host"])
    )


def ssh_base_argv(manifest: Mapping[str, Any]) -> List[str]:
    argv = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "PasswordAuthentication=no",
        "-o",
        "KbdInteractiveAuthentication=no",
        "-o",
        "NumberOfPasswordPrompts=0",
    ]
    port = manifest["remote"].get("port")
    if port:
        argv.extend(["-p", str(port)])
    argv.append(ssh_target(manifest))
    return argv


def detached_tmux_argv(manifest: Mapping[str, Any]) -> List[str]:
    state_dir = PurePosixPath(manifest["remote"]["state_dir"])
    runner_path = state_dir / "remote_runner.py"
    return [
        REMOTE_TMUX_EXECUTABLE,
        "new-session",
        "-d",
        "-s",
        str(manifest["remote"]["session"]),
        "--",
        REMOTE_PYTHON_EXECUTABLE,
        str(runner_path),
        "_remote-run",
        "--state-dir",
        str(state_dir),
    ]


def minimum_remote_python_version() -> str:
    return ".".join(str(component) for component in MIN_REMOTE_PYTHON_VERSION)


def remote_capability_requirements() -> Dict[str, Any]:
    return {
        "python": {
            "executable": REMOTE_PYTHON_EXECUTABLE,
            "minimum_version": minimum_remote_python_version(),
            "required_modules": list(REQUIRED_REMOTE_PYTHON_MODULES),
        },
        "tmux": {"executable": REMOTE_TMUX_EXECUTABLE, "probe_argv": ["-V"]},
    }


def remote_python_preflight_source() -> str:
    return """
import sys

minimum_text = sys.argv[1]
runtime_path = sys.argv[2]
minimum = tuple(int(component) for component in minimum_text.split('.'))
observed = tuple(sys.version_info[:3])
observed_text = '.'.join(str(component) for component in observed)
if observed < minimum:
    print(
        'remote capability preflight failed: Python runtime {} is too old; required >= {}; observed {}'.format(
            runtime_path, minimum_text, observed_text
        ),
        file=sys.stderr,
    )
    raise SystemExit(70)

failed = False
for module_name in sys.argv[3:]:
    try:
        __import__(module_name)
    except BaseException as error:
        failed = True
        print(
            'remote capability preflight failed: Python module import {}: {}: {}'.format(
                module_name, type(error).__name__, error
            ),
            file=sys.stderr,
        )
if failed:
    raise SystemExit(71)
""".strip()


def build_launch_payload(
    manifest: Mapping[str, Any], runner_bytes: bytes
) -> bytes:
    normalized_manifest = (
        json.dumps(manifest, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
    ).encode("utf-8")
    envelope = {
        "schema_version": 1,
        "manifest": base64.b64encode(normalized_manifest).decode("ascii"),
        "manifest_sha256": hashlib.sha256(normalized_manifest).hexdigest(),
        "runner": base64.b64encode(runner_bytes).decode("ascii"),
        "runner_sha256": hashlib.sha256(runner_bytes).hexdigest(),
    }
    return zlib.compress(
        json.dumps(
            envelope,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8"),
        level=9,
    )


def build_remote_launch_command(
    manifest: Mapping[str, Any], runner_bytes: bytes
) -> str:
    state_dir = PurePosixPath(manifest["remote"]["state_dir"])
    work_dir = PurePosixPath(manifest["remote"]["work_dir"])
    session = str(manifest["remote"]["session"])
    launch_payload = build_launch_payload(manifest, runner_bytes)
    payload_sha256 = hashlib.sha256(launch_payload).hexdigest()
    bootstrap = """
import base64, hashlib, json, os, pathlib, shutil, subprocess, sys, tempfile, zlib

def fail(code, message):
    print(message, file=sys.stderr)
    raise SystemExit(code)

blob = sys.stdin.buffer.read()
expected = sys.argv[4]
if hashlib.sha256(blob).hexdigest() != expected:
    fail(75, 'launch payload checksum mismatch')
envelope = json.loads(zlib.decompress(blob))
manifest = base64.b64decode(envelope['manifest'], validate=True)
runner = base64.b64decode(envelope['runner'], validate=True)
if hashlib.sha256(manifest).hexdigest() != envelope['manifest_sha256']:
    fail(76, 'manifest checksum mismatch')
if hashlib.sha256(runner).hexdigest() != envelope['runner_sha256']:
    fail(77, 'runner checksum mismatch')
state = pathlib.Path(sys.argv[1])
work = pathlib.Path(sys.argv[2])
session = sys.argv[3]
runtime_path = sys.argv[5]
tmux_path = sys.argv[6]
if subprocess.run([tmux_path, 'has-session', '-t', session], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
    fail(73, f'tmux session collision: {session}')
if state.exists() or state.is_symlink():
    fail(74, f'remote state directory already exists: {state}; reconcile it instead of relaunching')
os.umask(0o077)
state.parent.mkdir(parents=True, exist_ok=True)
staging = pathlib.Path(tempfile.mkdtemp(prefix=f'.{state.name}.prestage-', dir=state.parent))
try:
    manifest_path = staging / 'manifest.json'
    runner_path = staging / 'remote_runner.py'
    manifest_path.write_bytes(manifest)
    os.chmod(manifest_path, 0o600)
    runner_path.write_bytes(runner)
    os.chmod(runner_path, 0o700)
    work.mkdir(parents=True, exist_ok=True)
    if state.exists() or state.is_symlink():
        fail(74, f'remote state directory already exists: {state}; reconcile it instead of relaunching')
    os.rename(staging, state)
except BaseException:
    shutil.rmtree(staging, ignore_errors=True)
    raise
runner_path = state / 'remote_runner.py'
raise SystemExit(subprocess.run([tmux_path, 'new-session', '-d', '-s', session, '--', runtime_path, str(runner_path), '_remote-run', '--state-dir', str(state)]).returncode)
""".strip()
    module_arguments = " ".join(
        shlex.quote(module_name) for module_name in REQUIRED_REMOTE_PYTHON_MODULES
    )
    python_preflight = (
        f'"$runtime_path" -c {shlex.quote(remote_python_preflight_source())} '
        f'{shlex.quote(minimum_remote_python_version())} "$runtime_path" '
        f"{module_arguments}"
    )
    capability_bootstrap = f"""
runtime_name=$1
tmux_name=$2
runtime_path=$(command -v "$runtime_name" 2>&1)
runtime_status=$?
if [ "$runtime_status" -ne 0 ] || [ -z "$runtime_path" ]; then
    printf 'remote capability preflight failed: Python runtime executable not found: %s\n' "$runtime_name" >&2
    if [ -n "$runtime_path" ]; then
        printf 'remote capability preflight evidence: %s\n' "$runtime_path" >&2
    fi
    exit 69
fi
{python_preflight} </dev/null
python_status=$?
if [ "$python_status" -ne 0 ]; then
    exit "$python_status"
fi
tmux_path=$(command -v "$tmux_name" 2>&1)
tmux_status=$?
if [ "$tmux_status" -ne 0 ] || [ -z "$tmux_path" ]; then
    printf 'remote capability preflight failed: tmux executable not found: %s\n' "$tmux_name" >&2
    if [ -n "$tmux_path" ]; then
        printf 'remote capability preflight evidence: %s\n' "$tmux_path" >&2
    fi
    exit 72
fi
tmux_evidence=$("$tmux_path" -V </dev/null 2>&1)
tmux_status=$?
if [ "$tmux_status" -ne 0 ]; then
    printf 'remote capability preflight failed: tmux probe exited %s: %s -V\n' "$tmux_status" "$tmux_path" >&2
    if [ -n "$tmux_evidence" ]; then
        printf '%s\n' "$tmux_evidence" >&2
    fi
    exit 72
fi
exec "$runtime_path" -c {shlex.quote(bootstrap)} "$3" "$4" "$5" "$6" "$runtime_path" "$tmux_path"
""".strip()
    return shell_join(
        [
            "sh",
            "-c",
            capability_bootstrap,
            "rr-launch",
            REMOTE_PYTHON_EXECUTABLE,
            REMOTE_TMUX_EXECUTABLE,
            str(state_dir),
            str(work_dir),
            session,
            payload_sha256,
        ]
    )


def build_launch_plan(manifest: Mapping[str, Any], runner_bytes: bytes) -> Dict[str, Any]:
    remote_command = build_remote_launch_command(manifest, runner_bytes)
    launch_payload = build_launch_payload(manifest, runner_bytes)
    ssh_argv = ssh_base_argv(manifest) + [remote_command]
    return {
        "dry_run": True,
        "task_id": manifest["task_id"],
        "manifest_sha256": object_sha256(manifest),
        "session": manifest["remote"]["session"],
        "remote_capability_preflight": remote_capability_requirements(),
        "detached_tmux_argv": detached_tmux_argv(manifest),
        "stdin_payload_sha256": hashlib.sha256(launch_payload).hexdigest(),
        "stdin_payload_size_bytes": len(launch_payload),
        "ssh_argv": ssh_argv,
        "shell_display": shell_join(ssh_argv),
    }


def remote_control_argv(
    manifest: Mapping[str, Any], action: str
) -> List[str]:
    state_dir = PurePosixPath(manifest["remote"]["state_dir"])
    runner_path = state_dir / "remote_runner.py"
    remote_command = shell_join(
        [
            REMOTE_PYTHON_EXECUTABLE,
            str(runner_path),
            action,
            "--state-dir",
            str(state_dir),
        ]
    )
    return ssh_base_argv(manifest) + [remote_command]


def run_ssh_json(argv: Sequence[str]) -> Dict[str, Any]:
    completed = subprocess.run(argv, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or "no output"
        raise TransportError(
            f"SSH command failed with exit {completed.returncode}: {detail}"
        )
    try:
        value = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise TransportError(f"remote status returned invalid JSON: {error}") from error
    if not isinstance(value, dict):
        raise TransportError("remote status JSON root must be an object")
    return value


def fetch_remote_status(manifest: Mapping[str, Any]) -> Dict[str, Any]:
    return run_ssh_json(remote_control_argv(manifest, "_remote-status"))


def poll_until_terminal(
    fetch: Callable[[], Dict[str, Any]],
    interval_seconds: float,
    max_wait_seconds: Optional[float],
    on_update: Optional[Callable[[Dict[str, Any]], None]] = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> Dict[str, Any]:
    started = clock()
    transport_errors = 0
    while True:
        if max_wait_seconds is not None and clock() - started >= max_wait_seconds:
            return {
                "effective_status": "observer_timeout",
                "complete": False,
                "transport_errors": transport_errors,
            }
        try:
            status = fetch()
        except TransportError as error:
            transport_errors += 1
            status = {
                "effective_status": "transport_error",
                "complete": False,
                "transport_errors": transport_errors,
                "error": str(error),
            }
        if on_update:
            on_update(status)
        if status.get("complete"):
            status["transport_errors"] = transport_errors
            return status
        sleep(interval_seconds)


def resolve_under(root: Path, relative: str) -> Path:
    candidate = (root / relative).resolve()
    resolved_root = root.resolve()
    try:
        candidate.relative_to(resolved_root)
    except ValueError as error:
        raise SupervisorError(f"path escapes work directory: {relative}") from error
    return candidate


class RemoteSupervisor:
    """Durable, single-process state machine executed inside detached tmux."""

    def __init__(self, state_dir: Path, manifest: Dict[str, Any]) -> None:
        self.state_dir = state_dir
        self.manifest = manifest
        self.work_dir = Path(manifest["remote"]["work_dir"])
        self.logs_dir = state_dir / "logs"
        self.policy = manifest["policy"]
        self.manifest_sha256 = object_sha256(manifest)
        self.task_started_epoch = time.time()
        self.task_started_at = utc_now()
        self.overall_deadline = (
            self.task_started_epoch + self.policy["task_timeout_seconds"]
        )
        self.current_phase = "initializing"
        self.current_command: Optional[str] = None
        self.current_log: Optional[Path] = None
        self.last_log_size = 0
        self.last_progress_epoch = self.task_started_epoch

    def event(self, event: str, **fields: Any) -> None:
        append_json_line(
            self.state_dir / "events.jsonl",
            {"at": utc_now(), "event": event, **fields},
        )

    def write_state(self, status: str, **fields: Any) -> None:
        value = {
            "schema_version": SCHEMA_VERSION,
            "task_id": self.manifest["task_id"],
            "manifest_sha256": self.manifest_sha256,
            "status": status,
            "phase": self.current_phase,
            "current_command": self.current_command,
            "started_at": self.task_started_at,
            "started_at_epoch": self.task_started_epoch,
            "updated_at": utc_now(),
            "updated_at_epoch": time.time(),
            **fields,
        }
        atomic_write_json(self.state_dir / "state.json", value)

    def heartbeat(self, force_progress: bool = False) -> None:
        now = time.time()
        log_size = 0
        if self.current_log and self.current_log.exists():
            log_size = self.current_log.stat().st_size
        if force_progress or log_size != self.last_log_size:
            self.last_progress_epoch = now
            self.last_log_size = log_size
        value = {
            "schema_version": SCHEMA_VERSION,
            "task_id": self.manifest["task_id"],
            "manifest_sha256": self.manifest_sha256,
            "phase": self.current_phase,
            "current_command": self.current_command,
            "runner_pid": os.getpid(),
            "heartbeat_at": utc_now(),
            "heartbeat_at_epoch": now,
            "last_log_progress_at": datetime.fromtimestamp(
                self.last_progress_epoch, tz=timezone.utc
            ).isoformat().replace("+00:00", "Z"),
            "last_log_progress_at_epoch": self.last_progress_epoch,
            "log_size": log_size,
            "log_path": (
                str(self.current_log.relative_to(self.state_dir))
                if self.current_log
                else None
            ),
        }
        atomic_write_json(self.state_dir / "heartbeat.json", value)

    def _command_env(self, command: Mapping[str, Any]) -> Dict[str, str]:
        environment = {
            name: os.environ[name]
            for name in ALLOWED_AMBIENT_ENV
            if name in os.environ
        }
        environment.update(command.get("env", {}))
        return environment

    def _terminate_owned_process(self, process: subprocess.Popen[bytes]) -> None:
        if process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=self.policy["terminate_grace_seconds"])
            return
        except subprocess.TimeoutExpired:
            pass
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        process.wait()

    def run_command(
        self,
        command: Mapping[str, Any],
        phase: str,
        apply_task_deadline: bool,
        timeout_override: Optional[float] = None,
    ) -> Dict[str, Any]:
        command_id = str(command["id"])
        self.current_phase = phase
        self.current_command = command_id
        self.current_log = self.logs_dir / f"{phase}-{command_id}.log"
        self.current_log.parent.mkdir(parents=True, exist_ok=True)
        started_epoch = time.time()
        started_at = utc_now()
        self.last_progress_epoch = started_epoch
        self.last_log_size = 0
        command_timeout = float(
            timeout_override
            or command.get("timeout_seconds")
            or self.policy["default_step_timeout_seconds"]
        )
        command_deadline = started_epoch + command_timeout
        cwd = resolve_under(self.work_dir, str(command.get("cwd", ".")))
        cwd.mkdir(parents=True, exist_ok=True)
        self.event("command_started", phase=phase, command_id=command_id)
        spawn_error: Optional[str] = None
        with self.current_log.open("ab", buffering=0) as log:
            log.write(
                canonical_json_bytes(
                    {
                        "at": started_at,
                        "event": "command_started",
                        "phase": phase,
                        "command_id": command_id,
                    }
                )
                + b"\n"
            )
            try:
                process = subprocess.Popen(
                    list(command["argv"]),
                    cwd=cwd,
                    env=self._command_env(command),
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            except OSError as error:
                outcome, reason, returncode = "failed", "spawn_failed", None
                spawn_error = f"{type(error).__name__}: {error}"
            else:
                self.heartbeat(force_progress=True)
                outcome = "succeeded"
                reason = "exit_zero"
                last_heartbeat = 0.0
                while process.poll() is None:
                    now = time.time()
                    if now - last_heartbeat >= self.policy["heartbeat_interval_seconds"]:
                        self.heartbeat()
                        last_heartbeat = now
                    if apply_task_deadline and now >= self.overall_deadline:
                        outcome, reason = "failed", "task_timeout"
                        self._terminate_owned_process(process)
                        break
                    if now >= command_deadline:
                        outcome, reason = "failed", "command_timeout"
                        self._terminate_owned_process(process)
                        break
                    if (
                        now - self.last_progress_epoch
                        >= self.policy["stall_timeout_seconds"]
                    ):
                        outcome, reason = "failed", "log_stalled"
                        self._terminate_owned_process(process)
                        break
                    time.sleep(
                        min(0.2, self.policy["heartbeat_interval_seconds"] / 2.0)
                    )
                returncode = process.wait()
                if outcome == "succeeded" and returncode != 0:
                    outcome, reason = "failed", "exit_nonzero"
            finished_at = utc_now()
            log.write(
                canonical_json_bytes(
                    {
                        "at": finished_at,
                        "event": "command_finished",
                        "phase": phase,
                        "command_id": command_id,
                        "status": outcome,
                        "reason": reason,
                        "returncode": returncode,
                        "error": spawn_error,
                    }
                )
                + b"\n"
            )
        self.heartbeat(force_progress=True)
        result = {
            "id": command_id,
            "phase": phase,
            "status": outcome,
            "reason": reason,
            "returncode": returncode,
            "started_at": started_at,
            "finished_at": finished_at,
            "log_path": str(self.current_log.relative_to(self.state_dir)),
            "log_sha256": file_sha256(self.current_log),
        }
        if spawn_error:
            result["error"] = spawn_error
        self.event("command_finished", **result)
        return result

    def run_work(self) -> Dict[str, Any]:
        results: List[Dict[str, Any]] = []
        for command in self.manifest["steps"]:
            result = self.run_command(command, "step", apply_task_deadline=True)
            results.append(result)
            self.write_state("running", completed_steps=len(results))
            if result["status"] != "succeeded":
                return {
                    "success": False,
                    "reason": result["reason"],
                    "results": results,
                }
        return {"success": True, "reason": "all_steps_succeeded", "results": results}

    def run_restoration(self) -> Dict[str, Any]:
        self.current_phase = "restoration"
        self.current_command = None
        self.current_log = None
        self.write_state("restoring")
        self.event("restoration_started")
        results: List[Dict[str, Any]] = []
        for command in self.manifest["restoration"]["steps"]:
            results.append(
                self.run_command(
                    command,
                    "restoration",
                    apply_task_deadline=False,
                    timeout_override=self.policy["restoration_timeout_seconds"],
                )
            )
        check_results: List[Dict[str, Any]] = []
        for command in self.manifest["restoration"]["checks"]:
            check_results.append(
                self.run_command(
                    command,
                    "restoration-check",
                    apply_task_deadline=False,
                    timeout_override=self.policy["restoration_timeout_seconds"],
                )
            )
        success = all(
            result["status"] == "succeeded" for result in results + check_results
        )
        receipt = {
            "schema_version": SCHEMA_VERSION,
            "receipt_type": "restoration",
            "task_id": self.manifest["task_id"],
            "manifest_sha256": self.manifest_sha256,
            "created_at": utc_now(),
            "success": success,
            "steps": results,
            "checks": check_results,
        }
        atomic_write_json(self.state_dir / "restoration-receipt.json", receipt)
        self.event("restoration_finished", success=success)
        return receipt

    def failed_restoration_receipt(self, error: Exception) -> Dict[str, Any]:
        receipt = {
            "schema_version": SCHEMA_VERSION,
            "receipt_type": "restoration",
            "task_id": self.manifest["task_id"],
            "manifest_sha256": self.manifest_sha256,
            "created_at": utc_now(),
            "success": False,
            "steps": [],
            "checks": [],
            "error_type": type(error).__name__,
            "error": str(error),
        }
        atomic_write_json(self.state_dir / "restoration-receipt.json", receipt)
        self.event(
            "restoration_exception",
            error_type=type(error).__name__,
            error=str(error),
        )
        return receipt

    def create_artifact_receipt(self) -> Dict[str, Any]:
        entries: List[Dict[str, Any]] = []
        complete = True
        for artifact in self.manifest["artifacts"]:
            relative = str(artifact["path"])
            entry: Dict[str, Any] = {
                "path": relative,
                "required": bool(artifact["required"]),
            }
            try:
                path = resolve_under(self.work_dir, relative)
                entry["exists"] = path.is_file() and not path.is_symlink()
                if entry["exists"]:
                    entry["size_bytes"] = path.stat().st_size
                    entry["sha256"] = file_sha256(path)
                elif entry["required"]:
                    complete = False
            except (OSError, SupervisorError) as error:
                entry["exists"] = False
                entry["error_type"] = type(error).__name__
                entry["error"] = str(error)
                complete = False
            entries.append(entry)
        receipt = {
            "schema_version": SCHEMA_VERSION,
            "receipt_type": "artifacts",
            "task_id": self.manifest["task_id"],
            "manifest_sha256": self.manifest_sha256,
            "created_at": utc_now(),
            "complete": complete,
            "artifacts": entries,
        }
        atomic_write_json(self.state_dir / "artifact-receipt.json", receipt)
        self.event("artifact_receipt_created", complete=complete)
        return receipt

    def failed_artifact_receipt(self, error: Exception) -> Dict[str, Any]:
        receipt = {
            "schema_version": SCHEMA_VERSION,
            "receipt_type": "artifacts",
            "task_id": self.manifest["task_id"],
            "manifest_sha256": self.manifest_sha256,
            "created_at": utc_now(),
            "complete": False,
            "artifacts": [],
            "error_type": type(error).__name__,
            "error": str(error),
        }
        atomic_write_json(self.state_dir / "artifact-receipt.json", receipt)
        self.event(
            "artifact_receipt_exception",
            error_type=type(error).__name__,
            error=str(error),
        )
        return receipt

    def finalize(
        self,
        work: Mapping[str, Any],
        restoration: Mapping[str, Any],
        artifacts: Mapping[str, Any],
    ) -> Dict[str, Any]:
        if work["success"] and restoration["success"] and artifacts["complete"]:
            terminal_status = "succeeded"
            reason = "task_restored_and_artifacts_verified"
        elif not restoration["success"]:
            terminal_status = "failed"
            reason = "restoration_failed"
        elif not work["success"]:
            terminal_status = "failed"
            reason = str(work["reason"])
        else:
            terminal_status = "failed"
            reason = "required_artifact_missing"
        restoration_path = self.state_dir / "restoration-receipt.json"
        artifact_path = self.state_dir / "artifact-receipt.json"
        handoff = {
            "schema_version": SCHEMA_VERSION,
            "receipt_type": "handoff",
            "task_id": self.manifest["task_id"],
            "manifest_sha256": self.manifest_sha256,
            "created_at": utc_now(),
            "terminal_status": terminal_status,
            "reason": reason,
            "state_dir": self.manifest["remote"]["state_dir"],
            "work_dir": self.manifest["remote"]["work_dir"],
            "session": self.manifest["remote"]["session"],
            "restoration_receipt_sha256": file_sha256(restoration_path),
            "artifact_receipt_sha256": file_sha256(artifact_path),
            "suggested_observer_operations": ["status", "sync"],
        }
        handoff_path = self.state_dir / "handoff.json"
        atomic_write_json(handoff_path, handoff)
        terminal = {
            "schema_version": SCHEMA_VERSION,
            "marker_type": "terminal",
            "task_id": self.manifest["task_id"],
            "manifest_sha256": self.manifest_sha256,
            "created_at": utc_now(),
            "status": terminal_status,
            "reason": reason,
            "restoration_receipt_sha256": file_sha256(restoration_path),
            "artifact_receipt_sha256": file_sha256(artifact_path),
            "handoff_sha256": file_sha256(handoff_path),
        }
        self.current_phase = "terminal"
        self.current_command = None
        self.current_log = None
        self.write_state("finalizing", reason=reason)
        atomic_write_json(self.state_dir / "terminal.json", terminal)
        self.write_state(terminal_status, reason=reason)
        self.event("terminal_marker_created", status=terminal_status, reason=reason)
        return terminal

    def run(self) -> Dict[str, Any]:
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.current_phase = "work"
        self.write_state("running")
        self.heartbeat(force_progress=True)
        self.event("task_started", runner_pid=os.getpid())
        work: Dict[str, Any]
        try:
            work = self.run_work()
        except Exception as error:
            work = {
                "success": False,
                "reason": "supervisor_exception",
                "results": [],
                "error_type": type(error).__name__,
                "error": str(error),
            }
            self.event(
                "supervisor_exception",
                error_type=type(error).__name__,
                error=str(error),
            )
        try:
            restoration = self.run_restoration()
        except Exception as error:
            restoration = self.failed_restoration_receipt(error)
        try:
            artifacts = self.create_artifact_receipt()
        except Exception as error:
            artifacts = self.failed_artifact_receipt(error)
        return self.finalize(work, restoration, artifacts)


def acquire_runner_lock(state_dir: Path) -> None:
    lock_path = state_dir / "runner.lock"
    try:
        descriptor = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as error:
        raise SupervisorError(f"runner lock already exists: {lock_path}") from error
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(f"{os.getpid()}\n")
        stream.flush()
        os.fsync(stream.fileno())


def remote_run(state_dir: Path) -> Dict[str, Any]:
    state_dir = state_dir.resolve()
    manifest = load_manifest(state_dir / "manifest.json")
    expected = Path(manifest["remote"]["state_dir"]).resolve()
    if expected != state_dir:
        raise SupervisorError(
            f"state directory mismatch: manifest={expected} argument={state_dir}"
        )
    acquire_runner_lock(state_dir)
    supervisor = RemoteSupervisor(state_dir, manifest)
    return supervisor.run()


def _verify_receipt(
    state_dir: Path,
    terminal: Mapping[str, Any],
    filename: str,
    hash_field: str,
    receipt_type: str,
    manifest_sha256: str,
    issues: List[str],
) -> Optional[Dict[str, Any]]:
    path = state_dir / filename
    if not path.is_file():
        issues.append(f"missing {filename}")
        return None
    actual_hash = file_sha256(path)
    if terminal.get(hash_field) != actual_hash:
        issues.append(f"{filename} hash mismatch")
        return None
    try:
        receipt = read_json(path)
    except SupervisorError as error:
        issues.append(str(error))
        return None
    if receipt.get("receipt_type") != receipt_type:
        issues.append(f"{filename} has the wrong receipt type")
    if receipt.get("manifest_sha256") != manifest_sha256:
        issues.append(f"{filename} has the wrong manifest hash")
    if receipt.get("task_id") != terminal.get("task_id"):
        issues.append(f"{filename} has the wrong task id")
    return receipt


def remote_status(state_dir: Path, now: Optional[float] = None) -> Dict[str, Any]:
    state_dir = state_dir.resolve()
    manifest = load_manifest(state_dir / "manifest.json")
    manifest_sha256 = object_sha256(manifest)
    current_time = time.time() if now is None else now
    state = read_json(state_dir / "state.json") if (state_dir / "state.json").is_file() else {}
    heartbeat = (
        read_json(state_dir / "heartbeat.json")
        if (state_dir / "heartbeat.json").is_file()
        else {}
    )
    result: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "task_id": manifest["task_id"],
        "manifest_sha256": manifest_sha256,
        "declared_status": state.get("status", "pending"),
        "effective_status": state.get("status", "pending"),
        "complete": False,
        "phase": state.get("phase"),
        "current_command": state.get("current_command"),
        "session": manifest["remote"]["session"],
        "state_dir": manifest["remote"]["state_dir"],
        "work_dir": manifest["remote"]["work_dir"],
        "heartbeat": heartbeat,
        "issues": [],
    }
    terminal_path = state_dir / "terminal.json"
    if terminal_path.is_file():
        try:
            terminal = read_json(terminal_path)
        except SupervisorError as error:
            result["effective_status"] = "integrity_error"
            result["issues"].append(str(error))
            return result
        issues: List[str] = result["issues"]
        if terminal.get("marker_type") != "terminal":
            issues.append("terminal marker has the wrong type")
        if terminal.get("task_id") != manifest["task_id"]:
            issues.append("terminal marker has the wrong task id")
        if terminal.get("manifest_sha256") != manifest_sha256:
            issues.append("terminal marker has the wrong manifest hash")
        restoration = _verify_receipt(
            state_dir,
            terminal,
            "restoration-receipt.json",
            "restoration_receipt_sha256",
            "restoration",
            manifest_sha256,
            issues,
        )
        artifacts = _verify_receipt(
            state_dir,
            terminal,
            "artifact-receipt.json",
            "artifact_receipt_sha256",
            "artifacts",
            manifest_sha256,
            issues,
        )
        handoff = _verify_receipt(
            state_dir,
            terminal,
            "handoff.json",
            "handoff_sha256",
            "handoff",
            manifest_sha256,
            issues,
        )
        terminal_status = terminal.get("status")
        if terminal_status not in COMPLETE_STATUSES:
            issues.append("terminal marker has an invalid status")
        if terminal_status == "succeeded":
            if not restoration or not restoration.get("success"):
                issues.append("successful terminal marker lacks successful restoration")
            if not artifacts or not artifacts.get("complete"):
                issues.append("successful terminal marker lacks complete artifacts")
        if not handoff:
            issues.append("terminal marker lacks a valid handoff")
        else:
            if handoff.get("terminal_status") != terminal_status:
                issues.append("handoff terminal status mismatch")
            if (
                restoration is not None
                and handoff.get("restoration_receipt_sha256")
                != terminal.get("restoration_receipt_sha256")
            ):
                issues.append("handoff restoration receipt hash mismatch")
            if (
                artifacts is not None
                and handoff.get("artifact_receipt_sha256")
                != terminal.get("artifact_receipt_sha256")
            ):
                issues.append("handoff artifact receipt hash mismatch")
        if issues:
            result["effective_status"] = "integrity_error"
            result["terminal"] = terminal
            return result
        result.update(
            {
                "declared_status": terminal_status,
                "effective_status": terminal_status,
                "complete": True,
                "terminal": terminal,
                "restoration_receipt": restoration,
                "artifact_receipt": artifacts,
                "handoff": handoff,
            }
        )
        return result

    started_epoch = state.get("started_at_epoch")
    heartbeat_epoch = heartbeat.get("heartbeat_at_epoch")
    progress_epoch = heartbeat.get("last_log_progress_at_epoch")
    policy = manifest["policy"]
    if isinstance(started_epoch, (int, float)) and (
        current_time - started_epoch >= policy["task_timeout_seconds"]
    ):
        result["effective_status"] = "timed_out_unfinalized"
    elif isinstance(heartbeat_epoch, (int, float)) and (
        current_time - heartbeat_epoch >= policy["heartbeat_timeout_seconds"]
    ):
        result["effective_status"] = "heartbeat_lost"
    elif isinstance(progress_epoch, (int, float)) and (
        current_time - progress_epoch >= policy["stall_timeout_seconds"]
    ):
        result["effective_status"] = "stalled_unfinalized"
    return result


def iter_sync_files(state_dir: Path, manifest: Mapping[str, Any]) -> Iterable[tuple[Path, str]]:
    for filename in CONTROL_FILES:
        path = state_dir / filename
        if path.is_file() and not path.is_symlink():
            yield path, f"control/{filename}"
    logs_dir = state_dir / "logs"
    if logs_dir.is_dir():
        for path in sorted(logs_dir.rglob("*")):
            if path.is_file() and not path.is_symlink():
                relative = path.relative_to(logs_dir)
                yield path, f"control/logs/{relative.as_posix()}"
    work_dir = Path(manifest["remote"]["work_dir"])
    for artifact in manifest["artifacts"]:
        try:
            path = resolve_under(work_dir, artifact["path"])
        except SupervisorError:
            continue
        if path.is_file() and not path.is_symlink():
            yield path, f"artifacts/{artifact['path']}"


def write_sync_archive(state_dir: Path, output: io.BufferedWriter) -> None:
    state_dir = state_dir.resolve()
    manifest = load_manifest(state_dir / "manifest.json")
    with tarfile.open(fileobj=output, mode="w|gz") as archive:
        for source, archive_name in iter_sync_files(state_dir, manifest):
            archive.add(source, arcname=archive_name, recursive=False)


def safe_extract_archive(archive_path: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=False)
    root = destination.resolve()
    names: set[str] = set()
    with tarfile.open(archive_path, mode="r:gz") as archive:
        for member in archive.getmembers():
            pure = PurePosixPath(member.name)
            if pure.is_absolute() or ".." in pure.parts:
                raise SupervisorError(f"unsafe archive path: {member.name}")
            normalized_name = pure.as_posix()
            if normalized_name in names:
                raise SupervisorError(f"duplicate archive member: {member.name}")
            names.add(normalized_name)
            if member.issym() or member.islnk() or member.isdev():
                raise SupervisorError(f"unsupported archive member: {member.name}")
            target = (root / Path(*pure.parts)).resolve()
            try:
                target.relative_to(root)
            except ValueError as error:
                raise SupervisorError(f"archive path escapes destination: {member.name}") from error
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if not member.isfile():
                raise SupervisorError(f"unsupported archive member: {member.name}")
            target.parent.mkdir(parents=True, exist_ok=True)
            source = archive.extractfile(member)
            if source is None:
                raise SupervisorError(f"cannot read archive member: {member.name}")
            with source, target.open("wb") as output:
                shutil.copyfileobj(source, output)


def verify_synced_artifacts(
    destination: Path,
    expected_manifest: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    control = destination / "control"
    manifest = load_manifest(control / "manifest.json")
    if (
        expected_manifest is not None
        and object_sha256(manifest) != object_sha256(expected_manifest)
    ):
        raise SupervisorError(
            "synced manifest does not match the requested task manifest"
        )
    status = remote_status(control)
    if not status.get("complete"):
        issues = ", ".join(status.get("issues", [])) or status.get(
            "effective_status", "incomplete"
        )
        raise SupervisorError(f"synced task integrity validation failed: {issues}")
    receipt_path = control / "artifact-receipt.json"
    receipt = read_json(receipt_path)
    declared = {
        str(item["path"]): bool(item["required"])
        for item in manifest["artifacts"]
    }
    entries = receipt.get("artifacts")
    if not isinstance(entries, list):
        raise SupervisorError("artifact receipt artifacts must be an array")
    observed: set[str] = set()
    verified: List[Dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise SupervisorError("artifact receipt entry must be an object")
        relative = validate_relative_path(
            entry.get("path"), "artifact receipt path", allow_dot=False
        )
        if relative in observed:
            raise SupervisorError(f"duplicate artifact receipt path: {relative}")
        observed.add(relative)
        if relative not in declared:
            raise SupervisorError(f"undeclared artifact receipt path: {relative}")
        if entry.get("required") is not declared[relative]:
            raise SupervisorError(f"artifact required flag mismatch: {relative}")
        path = resolve_under(destination / "artifacts", relative)
        if entry.get("exists") is True:
            if not path.is_file() or path.is_symlink():
                raise SupervisorError(f"synced artifact is missing: {relative}")
            actual = file_sha256(path)
            if actual != entry.get("sha256"):
                raise SupervisorError(f"synced artifact hash mismatch: {relative}")
            if path.stat().st_size != entry.get("size_bytes"):
                raise SupervisorError(f"synced artifact size mismatch: {relative}")
            verified.append(
                {
                    "path": relative,
                    "sha256": actual,
                    "size_bytes": path.stat().st_size,
                }
            )
        elif path.exists():
            raise SupervisorError(
                f"artifact receipt says absent but sync contains file: {relative}"
            )
    if observed != set(declared):
        missing = sorted(set(declared) - observed)
        raise SupervisorError(
            f"artifact receipt is missing declared path(s): {', '.join(missing)}"
        )
    allowed_files = {
        f"control/{filename}"
        for filename in CONTROL_FILES
        if (control / filename).is_file()
    }
    declared_log_paths: set[str] = set()
    for receipt_name in ("restoration-receipt.json", "handoff.json"):
        receipt_path = control / receipt_name
        if not receipt_path.is_file():
            continue
        receipt = read_json(receipt_path)
        for collection in ("steps", "checks"):
            for entry in receipt.get(collection, []):
                if isinstance(entry, dict) and isinstance(
                    entry.get("log_path"), str
                ):
                    declared_log_paths.add(
                        "control/" + validate_relative_path(
                            entry["log_path"],
                            f"{receipt_name} log path",
                            allow_dot=False,
                        )
                    )
    events_path = control / "events.jsonl"
    if events_path.is_file():
        for line_number, line in enumerate(
            events_path.read_text(encoding="utf-8").splitlines(), 1
        ):
            try:
                event = json.loads(line)
            except json.JSONDecodeError as error:
                raise SupervisorError(
                    f"invalid events.jsonl line {line_number}: {error}"
                ) from error
            if isinstance(event, dict) and isinstance(event.get("log_path"), str):
                declared_log_paths.add(
                    "control/" + validate_relative_path(
                        event["log_path"],
                        f"events.jsonl line {line_number} log path",
                        allow_dot=False,
                    )
                )
    allowed_files.update(declared_log_paths)
    allowed_files.update(
        f"artifacts/{relative}"
        for relative in observed
        if (destination / "artifacts" / relative).is_file()
    )
    actual_files: set[str] = set()
    for path in destination.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(destination).as_posix()
        actual_files.add(relative)
    unexpected = sorted(actual_files - allowed_files)
    if unexpected:
        raise SupervisorError(
            "sync archive contains undeclared file(s): " + ", ".join(unexpected)
        )
    sync_receipt = {
        "schema_version": SCHEMA_VERSION,
        "receipt_type": "local-sync",
        "task_id": manifest["task_id"],
        "manifest_sha256": object_sha256(manifest),
        "created_at": utc_now(),
        "terminal_status": status["effective_status"],
        "terminal_sha256": file_sha256(control / "terminal.json"),
        "verified_artifacts": verified,
    }
    atomic_write_json(destination / "sync-receipt.json", sync_receipt)
    return sync_receipt


def sync_remote(manifest: Mapping[str, Any], destination: Path) -> Dict[str, Any]:
    if destination.exists():
        raise SupervisorError(f"sync destination already exists: {destination}")
    destination_parent = destination.parent.resolve()
    destination_parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        prefix=".durable-task-sync-", suffix=".tar.gz", dir=destination_parent, delete=False
    ) as temporary:
        archive_path = Path(temporary.name)
        completed = subprocess.run(
            remote_control_argv(manifest, "_remote-sync"),
            stdout=temporary,
            stderr=subprocess.PIPE,
            check=False,
        )
    try:
        if completed.returncode != 0:
            detail = completed.stderr.decode("utf-8", errors="replace").strip()
            raise TransportError(
                f"remote sync failed with exit {completed.returncode}: {detail}"
            )
        staging = Path(
            tempfile.mkdtemp(
                prefix=f".{destination.name}.sync-",
                dir=destination_parent,
            )
        )
        staging.rmdir()
        try:
            safe_extract_archive(archive_path, staging)
            receipt = verify_synced_artifacts(staging, manifest)
            os.replace(staging, destination)
            _fsync_directory(destination_parent)
            return receipt
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise
    finally:
        archive_path.unlink(missing_ok=True)


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Supervise self-advancing remote tasks. Public observer operations are "
            "launch, status, poll, and sync."
        )
    )
    subparsers = parser.add_subparsers(dest="action", required=True)

    launch = subparsers.add_parser(
        "launch", help="Build a detached tmux launch plan; dry-run by default."
    )
    launch.add_argument("--manifest", required=True, type=Path)
    launch.add_argument(
        "--execute",
        action="store_true",
        help="Execute the displayed SSH launch plan.",
    )

    status = subparsers.add_parser("status", help="Read durable remote status.")
    status.add_argument("--manifest", required=True, type=Path)

    poll = subparsers.add_parser(
        "poll", help="Reconnect and poll status without controlling task progress."
    )
    poll.add_argument("--manifest", required=True, type=Path)
    poll.add_argument("--interval-seconds", type=float, default=15.0)
    poll.add_argument("--max-wait-seconds", type=float)

    sync = subparsers.add_parser(
        "sync", help="Copy receipts, logs, and declared artifacts locally."
    )
    sync.add_argument("--manifest", required=True, type=Path)
    sync.add_argument("--destination", required=True, type=Path)

    remote_run_parser = subparsers.add_parser("_remote-run")
    remote_run_parser.add_argument("--state-dir", required=True, type=Path)
    remote_status_parser = subparsers.add_parser("_remote-status")
    remote_status_parser.add_argument("--state-dir", required=True, type=Path)
    remote_sync_parser = subparsers.add_parser("_remote-sync")
    remote_sync_parser.add_argument("--state-dir", required=True, type=Path)
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    if args.action == "launch":
        manifest = load_manifest(args.manifest)
        runner_bytes = Path(__file__).read_bytes()
        plan = build_launch_plan(manifest, runner_bytes)
        plan["dry_run"] = not args.execute
        print(json.dumps(plan, sort_keys=True, indent=2))
        if not args.execute:
            return 0
        completed = subprocess.run(
            plan["ssh_argv"],
            input=build_launch_payload(manifest, runner_bytes),
            check=False,
        )
        if completed.returncode != 0:
            raise TransportError(
                f"SSH launch failed with exit {completed.returncode}"
            )
        return 0
    if args.action == "status":
        manifest = load_manifest(args.manifest)
        status = fetch_remote_status(manifest)
        print(json.dumps(status, sort_keys=True, indent=2))
        return 0
    if args.action == "poll":
        if args.interval_seconds <= 0:
            raise SupervisorError("--interval-seconds must be positive")
        if args.max_wait_seconds is not None and args.max_wait_seconds <= 0:
            raise SupervisorError("--max-wait-seconds must be positive")
        manifest = load_manifest(args.manifest)

        def emit(status_value: Dict[str, Any]) -> None:
            print(json.dumps(status_value, sort_keys=True), flush=True)

        result = poll_until_terminal(
            lambda: fetch_remote_status(manifest),
            args.interval_seconds,
            args.max_wait_seconds,
            on_update=emit,
        )
        if result.get("effective_status") == "succeeded":
            return 0
        if result.get("effective_status") == "failed":
            return 1
        return 2
    if args.action == "sync":
        manifest = load_manifest(args.manifest)
        receipt = sync_remote(manifest, args.destination)
        print(json.dumps(receipt, sort_keys=True, indent=2))
        return 0
    if args.action == "_remote-run":
        terminal = remote_run(args.state_dir)
        return 0 if terminal.get("status") in COMPLETE_STATUSES else 1
    if args.action == "_remote-status":
        print(json.dumps(remote_status(args.state_dir), sort_keys=True))
        return 0
    if args.action == "_remote-sync":
        write_sync_archive(args.state_dir, sys.stdout.buffer)
        return 0
    raise SupervisorError(f"unsupported action: {args.action}")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (SupervisorError, OSError) as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(2)
