#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, TextIO, TypeVar


DEFAULT_DISK_FLOOR_BYTES = 1024 * 1024 * 1024
DEFAULT_COMPACT_LIMIT_BYTES = 512 * 1024 * 1024
MAX_PARALLEL = 32
DEFAULT_EXCLUDES = (
    "graphify-out",
    "vendor",
    "node_modules",
    ".venv",
    "venv",
    "dist",
    "build",
    "target",
    "__pycache__",
    "coverage",
    ".next",
    "output",
    "outputs",
    "logs",
    "tmp",
    ".cache",
    "cache",
    "secrets",
    ".env",
    ".env.*",
)
FAILED_CHUNK_RE = re.compile(r"^\[graphify\] chunk (\d+)/(\d+) failed:")
SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
SCRIPT_ROOT = Path(__file__).resolve().parent
T = TypeVar("T")
R = TypeVar("R")


# Must match graphify_fleet.BACKEND_NAME (checked by test_graphify_fleet).
BACKEND_NAME = "openai-compat"


class FleetError(RuntimeError):
    pass


@dataclass(frozen=True)
class FleetConfig:
    root: Path
    output: Path
    launcher: str
    detect_manifest: Path
    excludes: tuple[str, ...]

    def target_output(self, name: str) -> Path:
        return self.output / "targets" / name

    def graph_path(self, name: str) -> Path:
        return self.target_output(name) / "graphify-out" / "graph.json"

    def fragment_path(self, name: str) -> Path:
        return self.target_output(name) / "graphify-out" / "repair-fragment.json"

    def repair_audit_path(self, name: str) -> Path:
        return self.target_output(name) / "graphify-out" / "repair-audit.json"

    def log_path(self, name: str) -> Path:
        return self.output / "logs" / f"{name}.log"

    def status_path(self, name: str) -> Path:
        return self.output / "status" / f"{name}.json"


@dataclass
class BuildResult:
    name: str
    status: str
    elapsed_seconds: float = 0.0
    return_code: int = 0
    nodes: int = 0
    edges: int = 0
    hyperedges: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    graph_bytes: int = 0
    log_path: str = ""
    failed_chunks: tuple[int, ...] = ()
    repair_fragment: str = ""
    resumed: bool = False
    error: str = ""

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "BuildResult":
        values: dict[str, Any] = {}
        for field in fields(cls):
            if field.name in payload:
                values[field.name] = payload[field.name]
        if "failed_chunks" in values:
            values["failed_chunks"] = tuple(values["failed_chunks"])
        return cls(**values)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def _bounded_parallel(value: str) -> int:
    parsed = _positive_int(value)
    if parsed > MAX_PARALLEL:
        raise argparse.ArgumentTypeError(
            f"must not exceed the safety bound {MAX_PARALLEL}"
        )
    return parsed


def make_config(args: argparse.Namespace) -> FleetConfig:
    root = args.root.expanduser().resolve()
    output = (
        args.output.expanduser().resolve()
        if args.output
        else (root / ".graphify-fleet").resolve()
    )
    detect_manifest = (
        args.detect_manifest.expanduser().resolve()
        if args.detect_manifest
        else output / "inventory.json"
    )
    launcher = args.launcher or str(SCRIPT_ROOT / "graphify-fleet")
    excludes = tuple(args.exclude)
    if not args.no_default_excludes:
        excludes = tuple(dict.fromkeys((*DEFAULT_EXCLUDES, *excludes)))
    if not root.is_dir():
        raise FleetError(f"root is not a directory: {root}")
    return FleetConfig(
        root=root,
        output=output,
        launcher=launcher,
        detect_manifest=detect_manifest,
        excludes=excludes,
    )


def resolve_launcher(command: str) -> str:
    expanded = str(Path(command).expanduser())
    if os.sep in expanded or (os.altsep and os.altsep in expanded):
        path = Path(expanded).resolve()
        if not path.is_file() or not os.access(path, os.X_OK):
            raise FleetError(f"launcher is not executable: {path}")
        return str(path)
    resolved = shutil.which(command)
    if not resolved:
        raise FleetError(f"launcher is not on PATH: {command}")
    return resolved


def nearest_existing(path: Path) -> Path:
    current = path.resolve()
    while not current.exists() and current != current.parent:
        current = current.parent
    return current


def disk_free_bytes(path: Path) -> int:
    return shutil.disk_usage(nearest_existing(path)).free


def _fsync_path(path: Path) -> None:
    with path.open("rb") as stream:
        os.fsync(stream.fileno())


def publish_temp_file(temporary: Path, destination: Path) -> str:
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.replace(temporary, destination)
        return "replace"
    except PermissionError:
        temporary.unlink(missing_ok=True)
        raise FleetError(
            f"atomic replacement is not permitted for destination: {destination}"
        )


def atomic_write_bytes(path: Path, payload: bytes) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        return publish_temp_file(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def atomic_write_json(path: Path, payload: Any) -> str:
    encoded = json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8")
    return atomic_write_bytes(path, encoded + b"\n")


def _validate_name(name: str) -> str:
    if not SAFE_NAME_RE.fullmatch(name):
        raise FleetError(
            f"invalid target name {name!r}; use letters, digits, dot, dash, or underscore"
        )
    return name


def _path_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def resolve_target_path(config: FleetConfig, row: dict[str, Any]) -> Path:
    raw_path = Path(str(row.get("path", ""))).expanduser()
    target = raw_path.resolve() if raw_path.is_absolute() else (config.root / raw_path).resolve()
    if not _path_within(target, config.root):
        raise FleetError(
            f"target {row.get('name', '<unknown>')} escapes root: {target}"
        )
    if not target.is_dir():
        raise FleetError(f"target is not a directory: {target}")
    return target


def load_manifest(config: FleetConfig) -> list[dict[str, Any]]:
    if not config.detect_manifest.is_file():
        raise FleetError(f"detect manifest is missing: {config.detect_manifest}")
    try:
        payload = json.loads(config.detect_manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise FleetError(
            f"cannot read detect manifest {config.detect_manifest}: {error}"
        ) from error
    rows = payload.get("targets") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        raise FleetError("detect manifest must be a list or contain a targets list")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw_row in rows:
        if not isinstance(raw_row, dict):
            raise FleetError("detect manifest target entries must be objects")
        row = dict(raw_row)
        name = _validate_name(str(row.get("name", "")))
        if name in seen:
            raise FleetError(f"duplicate target name in detect manifest: {name}")
        seen.add(name)
        resolve_target_path(config, row)
        result.append(row)
    return result


def select_rows(
    rows: list[dict[str, Any]], names: Iterable[str]
) -> list[dict[str, Any]]:
    requested = list(dict.fromkeys(names))
    available = {str(row["name"]): row for row in rows}
    missing = sorted(set(requested) - set(available))
    if missing:
        raise FleetError(f"unknown target(s): {', '.join(missing)}")
    selected = [available[name] for name in requested] if requested else list(rows)
    selected = [
        row
        for row in selected
        if row.get("status", "success") == "success"
        and int(row.get("total_files", 0) or 0) > 0
    ]
    return sorted(
        selected,
        key=lambda row: (int(row.get("total_files", 0) or 0), str(row["name"])),
    )


def bounded_map(
    items: Iterable[T],
    max_workers: int,
    function: Callable[[T], R],
    on_error: Callable[[T, Exception], R],
) -> list[R]:
    iterator = iter(items)
    results: list[R] = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        pending: dict[Future[R], T] = {}

        def submit_next() -> bool:
            try:
                item = next(iterator)
            except StopIteration:
                return False
            pending[executor.submit(function, item)] = item
            return True

        for _ in range(max_workers):
            if not submit_next():
                break
        while pending:
            completed, _ = wait(pending, return_when=FIRST_COMPLETED)
            for future in completed:
                item = pending.pop(future)
                try:
                    results.append(future.result())
                except Exception as error:
                    results.append(on_error(item, error))
                submit_next()
    return results


def _inventory_target_specs(
    config: FleetConfig, specifications: list[str]
) -> list[tuple[str, Path]]:
    targets: list[tuple[str, Path]] = []
    if specifications:
        for specification in specifications:
            if "=" in specification:
                name, raw_path = specification.split("=", 1)
                name = _validate_name(name)
            else:
                raw_path = specification
                path_name = Path(raw_path).expanduser().name
                name = _validate_name(path_name)
            raw = Path(raw_path).expanduser()
            path = raw.resolve() if raw.is_absolute() else (config.root / raw).resolve()
            targets.append((name, path))
    else:
        skipped_names = {
            pattern.rstrip("/")
            for pattern in config.excludes
            if "/" not in pattern.rstrip("/") and "*" not in pattern
        }
        for path in sorted(config.root.iterdir(), key=lambda item: item.name):
            if (
                path.is_dir()
                and path.resolve() != config.output
                and path.name not in skipped_names
            ):
                targets.append((_validate_name(path.name), path.resolve()))
    seen: set[str] = set()
    for name, path in targets:
        if name in seen:
            raise FleetError(f"duplicate inventory target name: {name}")
        seen.add(name)
        if not _path_within(path, config.root):
            raise FleetError(f"inventory target escapes root: {path}")
        if not path.is_dir():
            raise FleetError(f"inventory target is not a directory: {path}")
    return targets


def _detect_target(
    config: FleetConfig, target: tuple[str, Path]
) -> dict[str, Any]:
    name, path = target
    from graphify.detect import detect

    detection = detect(path, extra_excludes=list(config.excludes))
    files = detection.get("files", {})
    counts = {
        kind: len(paths)
        for kind, paths in files.items()
        if isinstance(paths, list)
    }
    return {
        "name": name,
        "path": path.resolve().relative_to(config.root.resolve()).as_posix() or ".",
        "status": "success",
        "total_files": int(detection.get("total_files", 0) or 0),
        "total_words": int(detection.get("total_words", 0) or 0),
        "counts": counts,
        "skipped_sensitive": len(detection.get("skipped_sensitive", [])),
        "needs_graph": bool(detection.get("needs_graph", False)),
        "warning": detection.get("warning"),
    }


def inventory(config: FleetConfig, args: argparse.Namespace) -> int:
    targets = _inventory_target_specs(config, args.target)

    def on_error(target: tuple[str, Path], error: Exception) -> dict[str, Any]:
        name, path = target
        return {
            "name": name,
            "path": path.resolve().relative_to(config.root.resolve()).as_posix() or ".",
            "status": "failed",
            "total_files": 0,
            "total_words": 0,
            "counts": {},
            "skipped_sensitive": 0,
            "error": repr(error),
        }

    rows = bounded_map(
        targets,
        args.parallel,
        lambda target: _detect_target(config, target),
        on_error,
    )
    rows.sort(key=lambda row: str(row["name"]))
    payload = {
        "schema_version": 1,
        "generated_at": utc_now(),
        "root": str(config.root),
        "output": str(config.output),
        "excludes": list(config.excludes),
        "targets": rows,
    }
    atomic_write_json(config.detect_manifest, payload)
    total_files = sum(int(row.get("total_files", 0)) for row in rows)
    failures = [row for row in rows if row["status"] != "success"]
    print(
        f"Inventoried {len(rows)} targets and {total_files} files -> "
        f"{config.detect_manifest}"
    )
    return 1 if failures else 0


def parse_graph(path: Path) -> dict[str, int]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise FleetError(f"invalid graph JSON {path}: {error}") from error
    if not isinstance(data, dict):
        raise FleetError(f"graph JSON must be an object: {path}")
    edges = data.get("links", data.get("edges", []))
    return {
        "nodes": len(data.get("nodes", [])),
        "edges": len(edges),
        "hyperedges": len(data.get("hyperedges", [])),
        "input_tokens": int(data.get("input_tokens", 0) or 0),
        "output_tokens": int(data.get("output_tokens", 0) or 0),
        "graph_bytes": path.stat().st_size,
    }


def failed_chunk_indexes(log_path: Path) -> tuple[int, ...]:
    if not log_path.is_file():
        return ()
    indexes: set[int] = set()
    for line in log_path.read_text(
        encoding="utf-8", errors="replace"
    ).splitlines():
        match = FAILED_CHUNK_RE.match(line)
        if match:
            indexes.add(int(match.group(1)))
    return tuple(sorted(indexes))


def read_status(config: FleetConfig, name: str) -> BuildResult | None:
    path = config.status_path(name)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return BuildResult.from_payload(payload)
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None


def write_status(config: FleetConfig, result: BuildResult) -> None:
    payload = asdict(result)
    payload["failed_chunks"] = list(result.failed_chunks)
    payload["updated_at"] = utc_now()
    atomic_write_json(config.status_path(result.name), payload)


def is_complete(config: FleetConfig, name: str) -> bool:
    result = read_status(config, name)
    if result is None or not config.graph_path(name).is_file():
        return False
    if result.status == "success":
        return not failed_chunk_indexes(config.log_path(name))
    if result.status == "repaired":
        return config.fragment_path(name).is_file()
    return False


def cleanup_runtime_cache(config: FleetConfig, name: str) -> None:
    target_output = config.target_output(name)
    for cache_path in (
        target_output / "cache",
        target_output / "graphify-out" / "cache",
    ):
        if cache_path.exists():
            shutil.rmtree(cache_path)


def clear_repair_state(config: FleetConfig, name: str) -> None:
    config.fragment_path(name).unlink(missing_ok=True)
    config.repair_audit_path(name).unlink(missing_ok=True)


def build_command(
    config: FleetConfig,
    row: dict[str, Any],
    args: argparse.Namespace,
) -> list[str]:
    target = resolve_target_path(config, row)
    command = [
        resolve_launcher(config.launcher),
        "extract",
        str(target),
        "--out",
        str(config.target_output(str(row["name"]))),
        "--max-workers",
        str(args.graphify_workers),
        "--max-concurrency",
        str(args.graphify_concurrency),
        "--token-budget",
        str(args.token_budget),
        "--api-timeout",
        str(args.api_timeout),
        "--no-cluster",
    ]
    for pattern in config.excludes:
        command.extend(["--exclude", pattern])
    return command


def _resumed_result(config: FleetConfig, name: str) -> BuildResult:
    previous = read_status(config, name)
    if previous is None:
        raise FleetError(f"cannot resume target without status: {name}")
    previous.resumed = True
    return previous


def build_target(
    config: FleetConfig,
    row: dict[str, Any],
    args: argparse.Namespace,
) -> BuildResult:
    name = str(row["name"])
    if not args.force and is_complete(config, name):
        return _resumed_result(config, name)
    free_bytes = disk_free_bytes(config.output)
    if free_bytes < args.disk_floor_bytes:
        result = BuildResult(
            name=name,
            status="blocked_low_disk",
            return_code=75,
            error=(
                f"free bytes {free_bytes} below configured floor "
                f"{args.disk_floor_bytes}"
            ),
        )
        write_status(config, result)
        return result

    target_output = config.target_output(name)
    target_output.mkdir(parents=True, exist_ok=True)
    clear_repair_state(config, name)
    log_path = config.log_path(name)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    command = build_command(config, row, args)
    environment = os.environ.copy()
    environment["GRAPHIFY_OUT"] = str(target_output)
    environment["GRAPHIFY_DISABLE_AST_PARALLEL"] = "1"
    environment["GRAPHIFY_LLM_TIMEOUT_SECONDS"] = str(args.api_timeout)

    started = time.monotonic()
    with log_path.open("w", encoding="utf-8") as log:
        log.write(f"started_at={utc_now()}\n")
        log.write(f"command={shlex.join(command)}\n")
        log.flush()
        completed = subprocess.run(
            command,
            cwd=config.root,
            env=environment,
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
    elapsed = round(time.monotonic() - started, 3)
    graph_path = config.graph_path(name)
    failed_chunks = failed_chunk_indexes(log_path)
    if completed.returncode == 0 and graph_path.is_file():
        try:
            metrics = parse_graph(graph_path)
        except FleetError as error:
            result = BuildResult(
                name=name,
                status="failed",
                elapsed_seconds=elapsed,
                return_code=1,
                log_path=str(log_path),
                error=str(error),
            )
        else:
            result = BuildResult(
                name=name,
                status="partial" if failed_chunks else "success",
                elapsed_seconds=elapsed,
                return_code=0,
                log_path=str(log_path),
                failed_chunks=failed_chunks,
                error=(
                    f"{len(failed_chunks)} semantic chunk(s) failed"
                    if failed_chunks
                    else ""
                ),
                **metrics,
            )
            if not failed_chunks:
                cleanup_runtime_cache(config, name)
    else:
        result = BuildResult(
            name=name,
            status="failed",
            elapsed_seconds=elapsed,
            return_code=completed.returncode,
            log_path=str(log_path),
            failed_chunks=failed_chunks,
            error="graphify command failed or graph.json is missing",
        )
    write_status(config, result)
    return result


def print_build_result(result: BuildResult) -> None:
    size_mib = result.graph_bytes / 1024 / 1024
    suffix = " resumed" if result.resumed else ""
    print(
        f"[{result.status:>16}] {result.name:<32} "
        f"{result.nodes:>8} nodes {result.edges:>8} edges "
        f"{size_mib:>7.1f} MiB {result.elapsed_seconds:>8.1f}s{suffix}",
        flush=True,
    )


def write_build_summary(
    config: FleetConfig, results: list[BuildResult], disk_floor_bytes: int
) -> None:
    statuses: dict[str, int] = {}
    for result in results:
        statuses[result.status] = statuses.get(result.status, 0) + 1
    payload = {
        "updated_at": utc_now(),
        "root": str(config.root),
        "output": str(config.output),
        "detect_manifest": str(config.detect_manifest),
        "disk_floor_bytes": disk_floor_bytes,
        "free_bytes": disk_free_bytes(config.output),
        "statuses": statuses,
        "total_nodes": sum(result.nodes for result in results),
        "total_edges": sum(result.edges for result in results),
        "total_graph_bytes": sum(result.graph_bytes for result in results),
        "results": [
            asdict(result) for result in sorted(results, key=lambda item: item.name)
        ],
    }
    atomic_write_json(config.output / "summary.json", payload)


def build(config: FleetConfig, args: argparse.Namespace) -> int:
    rows = select_rows(load_manifest(config), args.targets)
    if not rows:
        raise FleetError("no non-empty successful inventory targets selected")
    if args.dry_run:
        plan = []
        for row in rows:
            name = str(row["name"])
            plan.append(
                {
                    "name": name,
                    "path": str(resolve_target_path(config, row)),
                    "would_resume": not args.force and is_complete(config, name),
                    "command": build_command(config, row, args),
                }
            )
        print(json.dumps({"targets": plan}, indent=2))
        return 0

    def on_error(row: dict[str, Any], error: Exception) -> BuildResult:
        result = BuildResult(
            name=str(row["name"]),
            status="failed",
            return_code=1,
            error=repr(error),
        )
        write_status(config, result)
        return result

    results = bounded_map(
        rows,
        args.parallel,
        lambda row: build_target(config, row, args),
        on_error,
    )
    for result in sorted(results, key=lambda item: item.name):
        print_build_result(result)
    write_build_summary(config, results, args.disk_floor_bytes)
    return 1 if any(
        result.status in {"failed", "partial", "blocked_low_disk"}
        for result in results
    ) else 0


def stable_key(item: dict[str, Any], kind: str) -> str:
    if kind == "nodes":
        return str(item.get("id") or json.dumps(item, sort_keys=True))
    if kind == "edges":
        return "\x00".join(
            str(item.get(key, ""))
            for key in (
                "source",
                "target",
                "relation",
                "source_file",
                "source_location",
            )
        )
    return json.dumps(item, sort_keys=True)


def unique_additions(
    existing: list[dict[str, Any]],
    additions: list[dict[str, Any]],
    kind: str,
) -> list[dict[str, Any]]:
    existing_keys = {stable_key(item, kind) for item in existing}
    result: list[dict[str, Any]] = []
    result_keys: set[str] = set()
    for item in additions:
        key = stable_key(item, kind)
        if key in existing_keys or key in result_keys:
            continue
        result_keys.add(key)
        result.append(item)
    return result


def semantic_files(repo_root: Path, excludes: tuple[str, ...]) -> list[Path]:
    from graphify.detect import detect

    detection = detect(repo_root, extra_excludes=list(excludes))
    files: list[Path] = []
    for kind in ("document", "paper", "image"):
        files.extend(Path(path) for path in detection.get("files", {}).get(kind, []))
    return files


def prepare_repair_backend() -> None:
    try:
        from graphify_fleet import load_env, register_graphify_backend
    except ImportError as error:
        raise FleetError(
            "repair must run through the graphify-fleet launcher"
        ) from error
    load_env()
    register_graphify_backend()


def repair_target(
    config: FleetConfig,
    row: dict[str, Any],
    chunk_indexes: list[int] | None,
    token_budget: int,
) -> dict[str, Any]:
    name = str(row["name"])
    repo_root = resolve_target_path(config, row)
    graph_path = config.graph_path(name)
    log_path = config.log_path(name)
    if not graph_path.is_file() or not log_path.is_file():
        raise FleetError(f"build artifacts are incomplete for target {name}")

    from graphify.llm import _extract_with_adaptive_retry, _pack_chunks_by_tokens

    files = semantic_files(repo_root, config.excludes)
    chunks = _pack_chunks_by_tokens(files, token_budget=token_budget)
    indexes = (
        sorted(set(chunk_indexes))
        if chunk_indexes is not None
        else [index - 1 for index in failed_chunk_indexes(log_path)]
    )
    if not indexes:
        return {
            "name": name,
            "status": "no_failed_chunks",
            "chunk_indexes": [],
        }
    invalid = [index + 1 for index in indexes if index < 0 or index >= len(chunks)]
    if invalid:
        raise FleetError(
            f"{name}: invalid chunk indexes {invalid}; total chunks={len(chunks)}"
        )

    graph = json.loads(graph_path.read_text(encoding="utf-8"))
    fragment_path = config.fragment_path(name)
    if fragment_path.is_file():
        try:
            existing_fragment = json.loads(
                fragment_path.read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError) as error:
            raise FleetError(
                f"{name}: cannot read existing repair fragment: {error}"
            ) from error
        if not isinstance(existing_fragment, dict):
            raise FleetError(f"{name}: existing repair fragment must be an object")
    else:
        existing_fragment = {}
    existing_fragment_nodes = existing_fragment.get("nodes", [])
    existing_fragment_edges = existing_fragment.get(
        "links", existing_fragment.get("edges", [])
    )
    existing_fragment_hyperedges = existing_fragment.get("hyperedges", [])
    if not all(
        isinstance(value, list)
        for value in (
            existing_fragment_nodes,
            existing_fragment_edges,
            existing_fragment_hyperedges,
        )
    ):
        raise FleetError(f"{name}: existing repair fragment has invalid lists")
    repair_nodes: list[dict[str, Any]] = []
    repair_edges: list[dict[str, Any]] = []
    repair_hyperedges: list[dict[str, Any]] = []
    input_tokens = 0
    output_tokens = 0
    repaired_files: list[str] = []
    for index in indexes:
        chunk = chunks[index]
        print(
            f"{name}: repairing chunk {index + 1}/{len(chunks)} "
            f"({len(chunk)} files)",
            flush=True,
        )
        result = _extract_with_adaptive_retry(
            chunk,
            backend=BACKEND_NAME,
            api_key=None,
            model=None,
            root=repo_root,
            max_depth=3,
        )
        if not any(
            result.get(kind) for kind in ("nodes", "edges", "hyperedges")
        ):
            raise FleetError(
                f"{name}: repaired chunk {index + 1} returned no graph data"
            )
        repair_nodes.extend(result.get("nodes", []))
        repair_edges.extend(result.get("edges", []))
        repair_hyperedges.extend(result.get("hyperedges", []))
        input_tokens += int(result.get("input_tokens", 0) or 0)
        output_tokens += int(result.get("output_tokens", 0) or 0)
        repaired_files.extend(str(path) for path in chunk)

    fragment_nodes = unique_additions(
        [*graph.get("nodes", []), *existing_fragment_nodes],
        repair_nodes,
        "nodes",
    )
    existing_edges = graph.get("links", graph.get("edges", []))
    fragment_edges = unique_additions(
        [*existing_edges, *existing_fragment_edges],
        repair_edges,
        "edges",
    )
    fragment_hyperedges = unique_additions(
        [*graph.get("hyperedges", []), *existing_fragment_hyperedges],
        repair_hyperedges,
        "hyperedges",
    )
    fragment = {
        "nodes": [*existing_fragment_nodes, *fragment_nodes],
        "edges": [*existing_fragment_edges, *fragment_edges],
        "hyperedges": [
            *existing_fragment_hyperedges,
            *fragment_hyperedges,
        ],
        "input_tokens": int(existing_fragment.get("input_tokens", 0) or 0)
        + input_tokens,
        "output_tokens": int(existing_fragment.get("output_tokens", 0) or 0)
        + output_tokens,
        "repo": name,
        "repair_fragment": True,
    }
    atomic_write_json(fragment_path, fragment)
    repair_audit_path = config.repair_audit_path(name)
    if repair_audit_path.is_file():
        try:
            previous_audit = json.loads(
                repair_audit_path.read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError) as error:
            raise FleetError(
                f"{name}: cannot read existing repair audit: {error}"
            ) from error
        if not isinstance(previous_audit, dict):
            raise FleetError(f"{name}: existing repair audit must be an object")
    else:
        previous_audit = {}
    cumulative_chunks = sorted(
        {
            *(
                int(value)
                for value in previous_audit.get("chunk_indexes", [])
            ),
            *(index + 1 for index in indexes),
        }
    )
    cumulative_files = sorted(
        {
            *(
                str(value)
                for value in previous_audit.get("repaired_files", [])
            ),
            *repaired_files,
        }
    )
    audit = {
        "repository": name,
        "repaired_at": utc_now(),
        "chunk_indexes": cumulative_chunks,
        "total_chunks": len(chunks),
        "repaired_files": cumulative_files,
        "fragment_path": str(fragment_path),
        "nodes_added": len(fragment["nodes"]),
        "edges_added": len(fragment["edges"]),
        "hyperedges_added": len(fragment["hyperedges"]),
        "input_tokens": fragment["input_tokens"],
        "output_tokens": fragment["output_tokens"],
    }
    atomic_write_json(repair_audit_path, audit)

    previous = read_status(config, name)
    if previous is None:
        metrics = parse_graph(graph_path)
        previous = BuildResult(name=name, status="partial", **metrics)
    previous.status = "repaired"
    previous.repair_fragment = str(fragment_path)
    previous.error = ""
    write_status(config, previous)
    cleanup_runtime_cache(config, name)
    print(
        f"{name}: repaired {len(indexes)} chunk(s), "
        f"+{len(fragment_nodes)} nodes, +{len(fragment_edges)} edges",
        flush=True,
    )
    return {"name": name, "status": "repaired", **audit}


def repair(config: FleetConfig, args: argparse.Namespace) -> int:
    rows = select_rows(load_manifest(config), args.targets)
    if args.chunk and len(rows) != 1:
        raise FleetError("--chunk requires exactly one selected target")
    prepare_repair_backend()
    indexes = [value - 1 for value in args.chunk] if args.chunk else None
    results: list[dict[str, Any]] = []
    failures = 0
    for row in sorted(rows, key=lambda item: str(item["name"])):
        try:
            results.append(
                repair_target(config, row, indexes, args.token_budget)
            )
        except Exception as error:
            failures += 1
            results.append(
                {
                    "name": str(row["name"]),
                    "status": "failed",
                    "error": repr(error),
                }
            )
            print(f"repair {row['name']}: {error}", file=sys.stderr)
    atomic_write_json(
        config.output / "repair-summary.json",
        {"updated_at": utc_now(), "results": results},
    )
    return 1 if failures else 0


def _prefix_id(value: Any, repository: str) -> str:
    return f"{repository}::{value}"


def prefixed_node(node: dict[str, Any], repository: str) -> dict[str, Any]:
    result = dict(node)
    result["id"] = _prefix_id(result.get("id", ""), repository)
    result.setdefault("repo", repository)
    return result


def prefixed_edge(edge: dict[str, Any], repository: str) -> dict[str, Any]:
    result = dict(edge)
    result["source"] = _prefix_id(result.get("source", ""), repository)
    result["target"] = _prefix_id(result.get("target", ""), repository)
    result.setdefault("repo", repository)
    return result


def prefixed_hyperedge(
    hyperedge: dict[str, Any], repository: str
) -> dict[str, Any]:
    result = dict(hyperedge)
    for key in ("source", "target"):
        if key in result:
            result[key] = _prefix_id(result[key], repository)
    for key in ("nodes", "members", "sources", "targets"):
        if isinstance(result.get(key), list):
            result[key] = [
                _prefix_id(value, repository) for value in result[key]
            ]
    result.setdefault("repo", repository)
    return result


def compact_node(node: dict[str, Any]) -> dict[str, Any]:
    return {
        key: node[key]
        for key in (
            "id",
            "label",
            "name",
            "type",
            "file_type",
            "source_file",
            "repo",
        )
        if key in node and node[key] not in (None, "")
    }


def compact_edge(edge: dict[str, Any]) -> dict[str, Any]:
    return {
        key: edge[key]
        for key in (
            "source",
            "target",
            "relation",
            "label",
            "confidence",
            "repo",
        )
        if key in edge and edge[key] not in (None, "")
    }


def compact_hyperedge(hyperedge: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "source",
        "target",
        "nodes",
        "members",
        "sources",
        "targets",
        "relation",
        "label",
        "confidence",
        "repo",
    )
    return {
        key: hyperedge[key]
        for key in keys
        if key in hyperedge and hyperedge[key] not in (None, "", [])
    }


def write_json_items(
    output: TextIO,
    items: list[dict[str, Any]],
    transform: Callable[[dict[str, Any]], dict[str, Any]],
    first: bool,
) -> tuple[bool, int]:
    count = 0
    for item in items:
        if not first:
            output.write(",\n")
        output.write(json.dumps(transform(item), ensure_ascii=False))
        first = False
        count += 1
    return first, count


def merge_inputs(
    config: FleetConfig, rows: list[dict[str, Any]]
) -> list[tuple[str, Path]]:
    inputs: list[tuple[str, Path]] = []
    incomplete: list[str] = []
    for row in sorted(rows, key=lambda item: str(item["name"])):
        name = str(row["name"])
        status = read_status(config, name)
        graph_path = config.graph_path(name)
        if status is None or status.status not in {"success", "repaired"}:
            incomplete.append(
                f"{name} ({status.status if status is not None else 'missing status'})"
            )
            continue
        if not graph_path.is_file():
            incomplete.append(f"{name} (missing graph)")
            continue
        fragment = config.fragment_path(name)
        if status.status == "repaired" and not fragment.is_file():
            incomplete.append(f"{name} (missing repair fragment)")
            continue
        inputs.append((name, graph_path))
        if status.status == "repaired":
            inputs.append((name, fragment))
    if incomplete:
        raise FleetError(
            "refusing to merge incomplete targets: " + ", ".join(incomplete)
        )
    return inputs


def stream_merge(
    inputs: list[tuple[str, Path]],
    output_path: Path,
    *,
    compact: bool,
    max_bytes: int | None = None,
) -> tuple[dict[str, int], str]:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=output_path.parent,
        prefix=f".{output_path.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    counts = {"nodes": 0, "links": 0, "hyperedges": 0}
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write('{"directed":false,"multigraph":true,"graph":{},')
            output.write('"nodes":[\n')
            first = True
            for repository, path in inputs:
                data = json.loads(path.read_text(encoding="utf-8"))
                transform = (
                    lambda item, repository=repository: compact_node(
                        prefixed_node(item, repository)
                    )
                    if compact
                    else prefixed_node(item, repository)
                )
                first, count = write_json_items(
                    output, data.get("nodes", []), transform, first
                )
                counts["nodes"] += count

            output.write('],\n"links":[\n')
            first = True
            for repository, path in inputs:
                data = json.loads(path.read_text(encoding="utf-8"))
                edges = data.get("links", data.get("edges", []))
                transform = (
                    lambda item, repository=repository: compact_edge(
                        prefixed_edge(item, repository)
                    )
                    if compact
                    else prefixed_edge(item, repository)
                )
                first, count = write_json_items(output, edges, transform, first)
                counts["links"] += count

            output.write('],\n"hyperedges":[\n')
            first = True
            for repository, path in inputs:
                data = json.loads(path.read_text(encoding="utf-8"))
                transform = (
                    lambda item, repository=repository: compact_hyperedge(
                        prefixed_hyperedge(item, repository)
                    )
                    if compact
                    else prefixed_hyperedge(item, repository)
                )
                first, count = write_json_items(
                    output, data.get("hyperedges", []), transform, first
                )
                counts["hyperedges"] += count
            output.write("]}\n")
            output.flush()
            os.fsync(output.fileno())
        if max_bytes is not None and temporary.stat().st_size >= max_bytes:
            raise FleetError(
                f"compact graph is {temporary.stat().st_size} bytes; "
                f"configured limit requires < {max_bytes} bytes"
            )
        mode = publish_temp_file(temporary, output_path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return counts, mode


def merge(config: FleetConfig, args: argparse.Namespace) -> int:
    rows = select_rows(load_manifest(config), args.targets)
    inputs = merge_inputs(config, rows)
    if not inputs:
        raise FleetError("no completed or partial graph inputs are available")
    archive_path = (
        args.archive_out.expanduser().resolve()
        if args.archive_out
        else config.output / "fleet-graph.json"
    )
    compact_path = (
        args.compact_out.expanduser().resolve()
        if args.compact_out
        else config.output / "fleet-query-graph.json"
    )
    merge_audit_path = (config.output / "merge-audit.json").resolve()
    protected_paths = {
        path.resolve()
        for _repository, path in inputs
    }
    protected_paths.add(config.detect_manifest.resolve())
    for row in rows:
        name = str(row["name"])
        protected_paths.update(
            {
                config.status_path(name).resolve(),
                config.fragment_path(name).resolve(),
                config.repair_audit_path(name).resolve(),
            }
        )
    output_paths = {
        "archive": archive_path.resolve(),
        "compact": compact_path.resolve(),
        "merge audit": merge_audit_path,
    }
    if len(set(output_paths.values())) != len(output_paths):
        raise FleetError("archive, compact, and merge audit outputs must differ")
    for label, path in output_paths.items():
        if path in protected_paths:
            raise FleetError(f"{label} output conflicts with a fleet input: {path}")
    archive_counts, archive_mode = stream_merge(
        inputs, archive_path, compact=False
    )
    compact_counts, compact_mode = stream_merge(
        inputs,
        compact_path,
        compact=True,
        max_bytes=args.compact_limit_bytes,
    )
    payload = {
        "updated_at": utc_now(),
        "inputs": [
            {
                "repository": repository,
                "path": str(path.resolve()),
                "bytes": path.stat().st_size,
                "sha256": file_sha256(path),
            }
            for repository, path in inputs
        ],
        "targets": sorted(str(row["name"]) for row in rows),
        "archive": {
            "path": str(archive_path.resolve()),
            "bytes": archive_path.stat().st_size,
            "sha256": file_sha256(archive_path),
            "counts": archive_counts,
            "publish_mode": archive_mode,
        },
        "compact": {
            "path": str(compact_path.resolve()),
            "bytes": compact_path.stat().st_size,
            "sha256": file_sha256(compact_path),
            "limit_bytes": args.compact_limit_bytes,
            "counts": compact_counts,
            "publish_mode": compact_mode,
        },
    }
    atomic_write_json(merge_audit_path, payload)
    print(
        f"Merged {len(inputs)} inputs -> {archive_counts['nodes']} nodes, "
        f"{archive_counts['links']} edges"
    )
    print(
        f"Compact graph: {compact_path.stat().st_size} bytes "
        f"(< {args.compact_limit_bytes})"
    )
    return 0


def _output_forbidden_paths(output: Path) -> list[str]:
    if not output.exists():
        return []
    forbidden: list[str] = []
    for directory, directory_names, file_names in os.walk(output):
        base = Path(directory)
        for name in directory_names:
            if name.lower() in {"cache", "secrets"}:
                forbidden.append(str(base / name))
        for name in file_names:
            if name == ".env" or name.startswith(".env."):
                forbidden.append(str(base / name))
    return sorted(forbidden)


def audit_fleet(
    config: FleetConfig,
    rows: list[dict[str, Any]],
    compact_path: Path,
    compact_limit_bytes: int,
) -> dict[str, Any]:
    issues: list[dict[str, str]] = []
    targets: list[dict[str, Any]] = []
    expected_inputs: list[dict[str, Any]] = []
    for row in rows:
        name = str(row["name"])
        status = read_status(config, name)
        graph_path = config.graph_path(name)
        target_report: dict[str, Any] = {
            "name": name,
            "inventory_files": int(row.get("total_files", 0) or 0),
            "status": status.status if status else "missing",
            "graph": str(graph_path),
        }
        if status is None:
            issues.append(
                {"severity": "error", "target": name, "message": "status missing"}
            )
        elif status.status in {"failed", "partial", "blocked_low_disk"}:
            issues.append(
                {
                    "severity": "error",
                    "target": name,
                    "message": f"incomplete status: {status.status}",
                }
            )
        if not graph_path.is_file():
            issues.append(
                {"severity": "error", "target": name, "message": "graph missing"}
            )
        else:
            try:
                target_report.update(parse_graph(graph_path))
                expected_inputs.append(
                    {
                        "repository": name,
                        "path": str(graph_path.resolve()),
                        "bytes": graph_path.stat().st_size,
                        "sha256": file_sha256(graph_path),
                    }
                )
            except FleetError as error:
                issues.append(
                    {"severity": "error", "target": name, "message": str(error)}
                )
        failures = failed_chunk_indexes(config.log_path(name))
        target_report["failed_chunks"] = list(failures)
        if failures and (status is None or status.status != "repaired"):
            issues.append(
                {
                    "severity": "error",
                    "target": name,
                    "message": f"unrepaired failed chunks: {list(failures)}",
                }
            )
        if status is not None and status.status == "repaired":
            fragment = config.fragment_path(name)
            if not fragment.is_file():
                issues.append(
                    {
                        "severity": "error",
                        "target": name,
                        "message": "repair fragment missing",
                    }
                )
            else:
                expected_inputs.append(
                    {
                        "repository": name,
                        "path": str(fragment.resolve()),
                        "bytes": fragment.stat().st_size,
                        "sha256": file_sha256(fragment),
                    }
                )
            repair_audit = config.repair_audit_path(name)
            if not repair_audit.is_file():
                issues.append(
                    {
                        "severity": "error",
                        "target": name,
                        "message": "repair audit missing",
                    }
                )
        targets.append(target_report)

    if not compact_path.is_file():
        issues.append(
            {
                "severity": "error",
                "target": "fleet",
                "message": f"compact graph missing: {compact_path}",
            }
        )
        compact_bytes = 0
    else:
        compact_bytes = compact_path.stat().st_size
        try:
            parse_graph(compact_path)
        except FleetError as error:
            issues.append(
                {
                    "severity": "error",
                    "target": "fleet",
                    "message": str(error),
                }
            )
        if compact_bytes >= compact_limit_bytes:
            issues.append(
                {
                    "severity": "error",
                    "target": "fleet",
                    "message": (
                        f"compact graph {compact_bytes} bytes is not below "
                        f"{compact_limit_bytes}"
                    ),
                }
            )
    merge_audit_path = config.output / "merge-audit.json"
    if not merge_audit_path.is_file():
        issues.append(
            {
                "severity": "error",
                "target": "fleet",
                "message": "merge audit missing",
            }
        )
        merge_audit: dict[str, Any] = {}
    else:
        try:
            loaded_audit = json.loads(
                merge_audit_path.read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError) as error:
            issues.append(
                {
                    "severity": "error",
                    "target": "fleet",
                    "message": f"invalid merge audit: {error}",
                }
            )
            merge_audit = {}
        else:
            if not isinstance(loaded_audit, dict):
                issues.append(
                    {
                        "severity": "error",
                        "target": "fleet",
                        "message": "merge audit root must be an object",
                    }
                )
                merge_audit = {}
            else:
                merge_audit = loaded_audit
    expected_targets = sorted(str(row["name"]) for row in rows)
    if merge_audit:
        if merge_audit.get("targets") != expected_targets:
            issues.append(
                {
                    "severity": "error",
                    "target": "fleet",
                    "message": "merge audit target set does not match current selection",
                }
            )
        if merge_audit.get("inputs") != expected_inputs:
            issues.append(
                {
                    "severity": "error",
                    "target": "fleet",
                    "message": "merge audit inputs do not match current graph artifacts",
                }
            )
        compact_record = merge_audit.get("compact")
        archive_record = merge_audit.get("archive")
        if not isinstance(archive_record, dict):
            issues.append(
                {
                    "severity": "error",
                    "target": "fleet",
                    "message": "merge audit archive record is missing",
                }
            )
        else:
            archive_path_value = archive_record.get("path")
            archive_path = (
                Path(archive_path_value)
                if isinstance(archive_path_value, str)
                else None
            )
            if archive_path is None or not archive_path.is_file():
                issues.append(
                    {
                        "severity": "error",
                        "target": "fleet",
                        "message": "archival graph recorded by merge audit is missing",
                    }
                )
            else:
                expected_archive = {
                    "bytes": archive_path.stat().st_size,
                    "sha256": file_sha256(archive_path),
                }
                for field, expected in expected_archive.items():
                    if archive_record.get(field) != expected:
                        issues.append(
                            {
                                "severity": "error",
                                "target": "fleet",
                                "message": (
                                    f"merge audit archive {field} does not match "
                                    "the current artifact"
                                ),
                            }
                        )
        if not isinstance(compact_record, dict):
            issues.append(
                {
                    "severity": "error",
                    "target": "fleet",
                    "message": "merge audit compact record is missing",
                }
            )
        elif compact_path.is_file():
            expected_compact = {
                "path": str(compact_path.resolve()),
                "bytes": compact_path.stat().st_size,
                "sha256": file_sha256(compact_path),
                "limit_bytes": compact_limit_bytes,
            }
            for field, expected in expected_compact.items():
                if compact_record.get(field) != expected:
                    issues.append(
                        {
                            "severity": "error",
                            "target": "fleet",
                            "message": (
                                f"merge audit compact {field} does not match "
                                "the current artifact"
                            ),
                        }
                    )
    for path in _output_forbidden_paths(config.output):
        issues.append(
            {
                "severity": "error",
                "target": "fleet",
                "message": f"forbidden copied credential/cache path: {path}",
            }
        )
    return {
        "audited_at": utc_now(),
        "ok": not issues,
        "root": str(config.root),
        "output": str(config.output),
        "detect_manifest": str(config.detect_manifest),
        "free_bytes": disk_free_bytes(config.output),
        "compact_graph": str(compact_path),
        "compact_bytes": compact_bytes,
        "compact_limit_bytes": compact_limit_bytes,
        "targets": targets,
        "issues": issues,
    }


def audit(config: FleetConfig, args: argparse.Namespace) -> int:
    rows = select_rows(load_manifest(config), args.targets)
    compact_path = (
        args.compact_graph.expanduser().resolve()
        if args.compact_graph
        else config.output / "fleet-query-graph.json"
    )
    audit_path = (
        args.audit_out.expanduser().resolve()
        if args.audit_out
        else config.output / "audit.json"
    )
    payload = audit_fleet(
        config, rows, compact_path, args.compact_limit_bytes
    )
    atomic_write_json(audit_path, payload)
    print(
        f"Audit {'passed' if payload['ok'] else 'failed'}: "
        f"{len(payload['issues'])} issue(s) -> {audit_path}"
    )
    return 0 if payload["ok"] else 1


def add_common_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--root",
        type=Path,
        default=Path.cwd(),
        help="fleet root; defaults to the current directory",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="fleet output root; defaults to <root>/.graphify-fleet",
    )
    parser.add_argument(
        "--launcher",
        help="Graphify launcher; defaults to the sibling graphify-fleet script",
    )
    parser.add_argument(
        "--detect-manifest",
        type=Path,
        help="inventory manifest; defaults to <output>/inventory.json",
    )
    parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        help="additional Graphify exclusion pattern; repeatable",
    )
    parser.add_argument(
        "--no-default-excludes",
        action="store_true",
        help="disable the built-in dependency, output, cache, and secret exclusions",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Portable, resumable Graphify fleet indexing."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    inventory_parser = subparsers.add_parser(
        "inventory",
        help="detect files without semantic extraction and write a manifest",
    )
    add_common_options(inventory_parser)
    inventory_parser.add_argument(
        "--target",
        action="append",
        default=[],
        metavar="[NAME=]PATH",
        help="target under root; defaults to every immediate child directory",
    )
    inventory_parser.add_argument(
        "--parallel", type=_bounded_parallel, default=3
    )

    build_parser_ = subparsers.add_parser(
        "build", help="build or resume per-target graphs"
    )
    add_common_options(build_parser_)
    build_parser_.add_argument("targets", nargs="*")
    build_parser_.add_argument(
        "--parallel", type=_bounded_parallel, default=3
    )
    build_parser_.add_argument(
        "--disk-floor-bytes",
        type=_positive_int,
        default=DEFAULT_DISK_FLOOR_BYTES,
    )
    build_parser_.add_argument("--force", action="store_true")
    build_parser_.add_argument("--dry-run", action="store_true")
    build_parser_.add_argument(
        "--graphify-workers", type=_positive_int, default=1
    )
    build_parser_.add_argument(
        "--graphify-concurrency", type=_positive_int, default=1
    )
    build_parser_.add_argument(
        "--token-budget", type=_positive_int, default=12000
    )
    build_parser_.add_argument(
        "--api-timeout", type=_positive_int, default=600
    )

    repair_parser = subparsers.add_parser(
        "repair", help="replay failed semantic chunks into repair fragments"
    )
    add_common_options(repair_parser)
    repair_parser.add_argument("targets", nargs="*")
    repair_parser.add_argument(
        "--chunk",
        action="append",
        type=_positive_int,
        help="one-based chunk index; repeatable and limited to one target",
    )
    repair_parser.add_argument(
        "--token-budget", type=_positive_int, default=12000
    )

    merge_parser = subparsers.add_parser(
        "merge", help="merge per-target graphs and repair fragments"
    )
    add_common_options(merge_parser)
    merge_parser.add_argument("targets", nargs="*")
    merge_parser.add_argument("--archive-out", type=Path)
    merge_parser.add_argument("--compact-out", type=Path)
    merge_parser.add_argument(
        "--compact-limit-bytes",
        type=_positive_int,
        default=DEFAULT_COMPACT_LIMIT_BYTES,
    )

    audit_parser = subparsers.add_parser(
        "audit", help="verify fleet statuses, repairs, compact size, and outputs"
    )
    add_common_options(audit_parser)
    audit_parser.add_argument("targets", nargs="*")
    audit_parser.add_argument("--compact-graph", type=Path)
    audit_parser.add_argument("--audit-out", type=Path)
    audit_parser.add_argument(
        "--compact-limit-bytes",
        type=_positive_int,
        default=DEFAULT_COMPACT_LIMIT_BYTES,
    )
    return parser


def main(arguments: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(arguments)
    config = make_config(args)
    if args.command == "inventory":
        return inventory(config, args)
    if args.command == "build":
        return build(config, args)
    if args.command == "repair":
        return repair(config, args)
    if args.command == "merge":
        return merge(config, args)
    if args.command == "audit":
        return audit(config, args)
    parser.error(f"unsupported command: {args.command}")
    return 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except FleetError as error:
        print(f"graphify-fleet: {error}", file=sys.stderr)
        raise SystemExit(1)
