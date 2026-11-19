#!/usr/bin/env python3
"""Graphify wrapper with a pluggable OpenAI-compatible extraction backend.

Registers a Graphify LLM backend named ``openai-compat`` that talks to any
``/chat/completions`` endpoint, and exposes ``doctor``, ``smoke-test``,
``extract`` and ``fleet`` subcommands.  Credentials are never stored by this
skill: the key comes from an environment variable or a credential helper
command, and is redacted from every error message.
"""
from __future__ import annotations

import hashlib
import importlib.metadata
import inspect
import json
import math
import os
import shlex
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

BACKEND_NAME = "openai-compat"
CHAT_PATH = "/chat/completions"
RETRYABLE_HTTP_CODES = {408, 409, 425, 429, 500, 502, 503, 504}
DEFAULT_KEY_ENV = "OPENAI_API_KEY"
HELPER_TIMEOUT_SECONDS = 30


class BackendError(RuntimeError):
    pass


# ---------------------------------------------------------------- env files

def config_candidates() -> List[Path]:
    """Optional non-secret config files, in priority order."""
    candidates: List[Path] = []
    explicit_env = os.environ.get("GRAPHIFY_FLEET_ENV", "").strip()
    if explicit_env:
        candidates.append(Path(explicit_env).expanduser())
    explicit_home = os.environ.get("GRAPHIFY_FLEET_HOME", "").strip()
    if explicit_home:
        candidates.append(Path(explicit_home).expanduser() / ".env")
    xdg_home = os.environ.get("XDG_CONFIG_HOME", "").strip()
    if xdg_home:
        candidates.append(Path(xdg_home).expanduser() / "graphify-fleet" / ".env")
    candidates.append(Path.home() / ".config" / "graphify-fleet" / ".env")
    result: List[Path] = []
    seen = set()
    for candidate in candidates:
        normalized = candidate.resolve()
        if normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return result


def find_config_path() -> Optional[Path]:
    explicit_env = os.environ.get("GRAPHIFY_FLEET_ENV", "").strip()
    if explicit_env:
        path = Path(explicit_env).expanduser().resolve()
        if not path.is_file():
            raise BackendError(
                f"GRAPHIFY_FLEET_ENV does not point to a file: {path}"
            )
        return path
    explicit_home = os.environ.get("GRAPHIFY_FLEET_HOME", "").strip()
    if explicit_home:
        path = (Path(explicit_home).expanduser() / ".env").resolve()
        if not path.is_file():
            raise BackendError(f"GRAPHIFY_FLEET_HOME does not contain .env: {path}")
        return path
    for candidate in config_candidates():
        if candidate.is_file():
            return candidate
    return None


def require_private_mode(path: Path) -> None:
    if stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise BackendError(
            f"configuration file must not be group/world accessible: {path}"
        )


def load_env(path: Optional[Path] = None) -> Optional[Path]:
    """Load KEY=VALUE lines without overriding the process environment."""
    active = path.resolve() if path else find_config_path()
    if active is None:
        return None
    if not active.is_file():
        raise BackendError(f"configuration file does not exist: {active}")
    require_private_mode(active)
    for raw in active.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        os.environ.setdefault(key, value)
    return active


# --------------------------------------------------------------- credentials

def resolve_api_key() -> Tuple[str, str]:
    """Return (key, human-readable source). Never includes the key in source."""
    command = os.environ.get("GRAPHIFY_LLM_API_KEY_COMMAND", "").strip()
    if command:
        try:
            completed = subprocess.run(
                shlex.split(command),
                capture_output=True,
                text=True,
                timeout=HELPER_TIMEOUT_SECONDS,
                check=False,
            )
        except (OSError, ValueError, subprocess.TimeoutExpired) as error:
            raise BackendError(
                f"credential helper could not run: {type(error).__name__}"
            ) from error
        key = completed.stdout.strip()
        if completed.returncode != 0 or not key:
            raise BackendError(
                "credential helper failed "
                f"(exit {completed.returncode}, "
                f"{'empty output' if not key else 'non-zero exit'})"
            )
        return key, "GRAPHIFY_LLM_API_KEY_COMMAND"
    key_env = os.environ.get("GRAPHIFY_LLM_API_KEY_ENV", "").strip() or DEFAULT_KEY_ENV
    key = os.environ.get(key_env, "").strip()
    if not key:
        raise BackendError(
            f"API key is missing: set ${key_env} (or GRAPHIFY_LLM_API_KEY_ENV "
            "to the name of another variable, or GRAPHIFY_LLM_API_KEY_COMMAND "
            "to a credential helper that prints the key)"
        )
    return key, f"${key_env}"


# -------------------------------------------------------------------- config

@dataclass(frozen=True)
class BackendConfig:
    api_key: str
    base_url: str
    model: str
    max_tokens: int
    timeout_seconds: float
    max_tokens_param: str = "max_tokens"

    @property
    def endpoint(self) -> str:
        base = self.base_url.rstrip("/")
        if urllib.parse.urlsplit(base).path.rstrip("/").endswith(CHAT_PATH):
            return base
        return base + CHAT_PATH

    @classmethod
    def from_env(cls) -> "BackendConfig":
        base_url = os.environ.get("GRAPHIFY_LLM_BASE_URL", "").strip()
        model = os.environ.get("GRAPHIFY_LLM_MODEL", "").strip()
        if not base_url or not model:
            raise BackendError(
                "GRAPHIFY_LLM_BASE_URL and GRAPHIFY_LLM_MODEL are required "
                "(base URL usually ends in /v1)"
            )
        scheme = urllib.parse.urlsplit(base_url).scheme
        if scheme not in {"http", "https"}:
            raise BackendError("GRAPHIFY_LLM_BASE_URL must be an http(s) URL")
        try:
            max_tokens = int(os.environ.get("GRAPHIFY_LLM_MAX_TOKENS", "16384"))
            timeout = float(os.environ.get("GRAPHIFY_LLM_TIMEOUT_SECONDS", "600"))
        except ValueError as error:
            raise BackendError(
                "GRAPHIFY_LLM_MAX_TOKENS and GRAPHIFY_LLM_TIMEOUT_SECONDS "
                "must be numeric"
            ) from error
        if max_tokens <= 0 or timeout <= 0:
            raise BackendError(
                "GRAPHIFY_LLM_MAX_TOKENS and GRAPHIFY_LLM_TIMEOUT_SECONDS "
                "must be positive"
            )
        param = os.environ.get("GRAPHIFY_LLM_MAX_TOKENS_PARAM", "max_tokens").strip()
        if param not in {"max_tokens", "max_completion_tokens"}:
            raise BackendError(
                "GRAPHIFY_LLM_MAX_TOKENS_PARAM must be max_tokens or "
                "max_completion_tokens"
            )
        api_key, _source = resolve_api_key()
        return cls(api_key, base_url, model, max_tokens, timeout, param)


# ------------------------------------------------------------------- request

def _extract_content(response: Dict[str, Any]) -> str:
    choices = response.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        message = choices[0].get("message")
        if isinstance(message, dict):
            content = message.get("content")
            if isinstance(content, str) and content:
                return content
            if isinstance(content, list):
                parts = [
                    item["text"]
                    for item in content
                    if isinstance(item, dict) and isinstance(item.get("text"), str)
                ]
                if parts:
                    return "".join(parts)
        text = choices[0].get("text")
        if isinstance(text, str) and text:
            return text
    raise BackendError(
        "response had no model text in choices[0].message.content "
        "(reasoning-only or empty reply? raise GRAPHIFY_LLM_MAX_TOKENS)"
    )


def _usage(response: Dict[str, Any]) -> Tuple[int, int]:
    usage = response.get("usage")
    if not isinstance(usage, dict):
        usage = {}
    return (
        int(usage.get("prompt_tokens", usage.get("input_tokens", 0)) or 0),
        int(usage.get("completion_tokens", usage.get("output_tokens", 0)) or 0),
    )


def _redact(value: str, api_key: str) -> str:
    if not api_key:
        return value
    redacted = value.replace(api_key, "<redacted>")
    encoded = urllib.parse.quote(api_key, safe="")
    return redacted.replace(encoded, "<redacted>") if encoded != api_key else redacted


def request_chat(
    config: BackendConfig,
    messages: List[Dict[str, Any]],
    *,
    max_tokens: Optional[int] = None,
) -> Dict[str, Any]:
    payload = json.dumps(
        {
            "model": config.model,
            "stream": False,
            "temperature": 0,
            config.max_tokens_param: max_tokens or config.max_tokens,
            "messages": messages,
        }
    ).encode("utf-8")
    try:
        max_retries = max(0, int(os.environ.get("GRAPHIFY_LLM_MAX_RETRIES", "6")))
        retry_base = max(
            0.1, float(os.environ.get("GRAPHIFY_LLM_RETRY_BASE_SECONDS", "10"))
        )
        retry_cap = max(
            retry_base,
            float(os.environ.get("GRAPHIFY_LLM_RETRY_MAX_SECONDS", "120")),
        )
    except ValueError as error:
        raise BackendError("GRAPHIFY_LLM_*RETRY* variables must be numeric") from error

    raw = b""
    request_id = ""
    for attempt in range(max_retries + 1):
        request_id = f"graphify-fleet-{uuid.uuid4().hex}"
        request = urllib.request.Request(
            config.endpoint,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {config.api_key}",
                "X-Request-Id": request_id,
                "User-Agent": "graphify-fleet/1.0",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(
                request, timeout=config.timeout_seconds
            ) as response:
                raw = response.read()
            break
        except urllib.error.HTTPError as error:
            body = error.read().decode("utf-8", errors="replace")
            if error.code not in RETRYABLE_HTTP_CODES or attempt >= max_retries:
                preview = _redact(body, config.api_key)[:1000]
                raise BackendError(
                    f"request failed with HTTP {error.code}; "
                    f"request_id={request_id}; body={preview}"
                ) from error
            try:
                retry_after = float((error.headers or {}).get("Retry-After", "") or 0)
            except ValueError:
                retry_after = 0.0
            if not math.isfinite(retry_after) or retry_after < 0:
                retry_after = 0.0
            delay = min(
                max(retry_after, min(retry_base * (2**attempt), retry_cap)),
                retry_cap,
            )
            print(
                f"graphify-fleet: HTTP {error.code}; retrying in {delay:.1f}s "
                f"({attempt + 1}/{max_retries}); request_id={request_id}",
                file=sys.stderr,
                flush=True,
            )
            time.sleep(delay)
        except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
            if attempt >= max_retries:
                reason = _redact(str(getattr(error, "reason", error)), config.api_key)
                raise BackendError(
                    f"request failed; request_id={request_id}; reason={reason}"
                ) from error
            delay = min(retry_base * (2**attempt), retry_cap)
            print(
                f"graphify-fleet: network error; retrying in {delay:.1f}s "
                f"({attempt + 1}/{max_retries}); request_id={request_id}",
                file=sys.stderr,
                flush=True,
            )
            time.sleep(delay)
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError as error:
        preview = _redact(raw.decode("utf-8", errors="replace"), config.api_key)[:1000]
        raise BackendError(
            f"endpoint returned invalid JSON (wrong base URL?); "
            f"request_id={request_id}; body={preview}"
        ) from error
    if not isinstance(decoded, dict):
        raise BackendError(f"non-object JSON response; request_id={request_id}")
    return decoded


# ------------------------------------------------------- Graphify integration

def _call_shapes(repair: bool) -> Dict[str, Tuple[tuple, dict]]:
    """Private Graphify call shapes this wrapper relies on."""
    extract_kwargs = {
        "backend": BACKEND_NAME, "api_key": None, "model": None,
        "root": Path("."), "deep_mode": False,
    }
    calls: Dict[str, Tuple[tuple, dict]] = {
        "extract_files_direct": (([],), extract_kwargs),
        "_read_files": (([], Path(".")), {}),
        "_extraction_system": ((), {"deep": False}),
        "_parse_llm_json": (("{}",), {}),
    }
    if repair:
        calls.update({
            "_pack_chunks_by_tokens": (([],), {"token_budget": 12000}),
            "_extract_with_adaptive_retry": (
                ([],),
                {"backend": BACKEND_NAME, "api_key": None, "model": None,
                 "root": Path("."), "max_depth": 3},
            ),
        })
    return calls


def validate_graphify(
    graphify_llm: Any, *, repair: bool = False, secret: Optional[str] = None
) -> Dict[str, Any]:
    """Check the private Graphify API before anything is patched.

    Every function this wrapper calls must accept the exact call shape used
    (inspect.signature + bind); variadic signatures cannot be verified and are
    rejected.  Returns a provenance receipt (package version, credential-free
    source URL, module sha256) and optionally publishes it to the path in
    GRAPHIFY_COMPAT_RECEIPT.
    """
    signatures: Dict[str, Any] = {}
    try:
        if not isinstance(graphify_llm.BACKENDS, dict):
            raise ValueError("BACKENDS is not a dictionary")
        for name, (args, kwargs) in _call_shapes(repair).items():
            function = getattr(graphify_llm, name)
            if name == "extract_files_direct":
                function = getattr(
                    graphify_llm, "_fleet_original_extract_files_direct", function
                )
            signature = inspect.signature(function)
            if any(
                parameter.kind
                in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)
                for parameter in signature.parameters.values()
            ):
                raise ValueError(f"{name} has an unverifiable variadic signature")
            signature.bind(*args, **kwargs)
            signatures[name] = [
                {"name": parameter.name, "kind": parameter.kind.name,
                 "required": parameter.default is inspect.Parameter.empty}
                for parameter in signature.parameters.values()
            ]
        packages = importlib.metadata.packages_distributions().get(
            "graphify", ["graphify"]
        )
        if len(packages) != 1:
            raise ValueError("graphify module has ambiguous package ownership")
        distribution = importlib.metadata.distribution(packages[0])
        version = distribution.version
        if not version:
            raise ValueError("package version is missing")
        source_path = Path(graphify_llm.__file__).resolve()
        source_hash = hashlib.sha256(source_path.read_bytes()).hexdigest()
        direct = distribution.read_text("direct_url.json")
        direct_url = json.loads(direct) if direct else {}
        source = (
            direct_url.get("url")
            or distribution.metadata.get("Home-page")
            or str(source_path)
        )
        parsed = urllib.parse.urlsplit(source)
        if parsed.scheme:
            # Strip credentials, query and fragment.
            source = urllib.parse.urlunsplit(
                (parsed.scheme, parsed.netloc.rsplit("@", 1)[-1], parsed.path, "", "")
            )
    except (AttributeError, TypeError, ValueError, OSError,
            importlib.metadata.PackageNotFoundError) as error:
        raise BackendError(
            "unsupported Graphify: compatibility validation failed "
            f"({type(error).__name__})"
        ) from None
    receipt: Dict[str, Any] = {
        "package": distribution.metadata.get("Name", "graphify"),
        "version": version,
        "source": source,
        "module_path": str(source_path),
        "module_sha256": source_hash,
        "verified_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "signatures": signatures,
        "operation_id": os.environ.get("GRAPHIFY_OPERATION_ID"),
    }

    def protect(value: Any) -> Any:
        if isinstance(value, str):
            return _redact(value, secret) if secret else value
        if isinstance(value, list):
            return [protect(item) for item in value]
        if isinstance(value, dict):
            return {key: protect(item) for key, item in value.items()}
        return value

    receipt = protect(receipt)
    receipt_path = os.environ.get("GRAPHIFY_COMPAT_RECEIPT")
    if receipt_path:
        here = str(Path(__file__).resolve().parent)
        if here not in sys.path:
            sys.path.insert(0, here)
        from fleet_index import atomic_write_json

        try:
            atomic_write_json(Path(receipt_path), receipt)
        except OSError:
            raise BackendError(
                "unsupported Graphify: cannot publish compatibility receipt"
            ) from None
    return receipt


def register_graphify_backend(*, repair: bool = False) -> None:
    try:
        import graphify.llm as graphify_llm
    except ImportError as error:
        raise BackendError("unsupported Graphify: graphify.llm is unavailable") from error

    config = BackendConfig.from_env()
    receipt = validate_graphify(graphify_llm, repair=repair, secret=config.api_key)
    print(
        "graphify-fleet: Graphify provenance " + json.dumps(receipt, sort_keys=True),
        file=sys.stderr,
        flush=True,
    )
    graphify_llm.BACKENDS[BACKEND_NAME] = {
        "base_url": config.base_url,
        "default_model": config.model,
        "env_key": os.environ.get("GRAPHIFY_LLM_API_KEY_ENV", "").strip()
        or DEFAULT_KEY_ENV,
        "model_env_key": "GRAPHIFY_LLM_MODEL",
        "pricing": {"input": 0.0, "output": 0.0},
        "temperature": 0,
        "max_tokens": config.max_tokens,
    }
    # Idempotent: wrap the pristine function, never a previous wrapper.
    original_extract = getattr(
        graphify_llm,
        "_fleet_original_extract_files_direct",
        graphify_llm.extract_files_direct,
    )

    def extract_files_direct(
        files: List[Path],
        backend: str = "kimi",
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        root: Path = Path("."),
        *,
        deep_mode: bool = False,
    ) -> Dict[str, Any]:
        if backend != BACKEND_NAME:
            return original_extract(
                files,
                backend=backend,
                api_key=api_key,
                model=model,
                root=root,
                deep_mode=deep_mode,
            )
        active = BackendConfig(
            api_key=api_key or config.api_key,
            base_url=config.base_url,
            model=model or config.model,
            max_tokens=config.max_tokens,
            timeout_seconds=config.timeout_seconds,
            max_tokens_param=config.max_tokens_param,
        )
        response = request_chat(
            active,
            [
                {
                    "role": "system",
                    "content": graphify_llm._extraction_system(deep=deep_mode),
                },
                {"role": "user", "content": graphify_llm._read_files(files, root)},
            ],
        )
        result = graphify_llm._parse_llm_json(_extract_content(response))
        result["input_tokens"], result["output_tokens"] = _usage(response)
        result["model"] = active.model
        result["finish_reason"] = "stop"
        return result

    graphify_llm.extract_files_direct = extract_files_direct
    graphify_llm.__dict__["_fleet_original_extract_files_direct"] = original_extract


# ------------------------------------------------------------------ commands

def run_doctor() -> int:
    """Offline: show what would be used, never the key itself."""
    path = find_config_path()
    print(f"config file : {path or '<none>'}")
    print(f"base url    : {os.environ.get('GRAPHIFY_LLM_BASE_URL', '<unset>')}")
    print(f"model       : {os.environ.get('GRAPHIFY_LLM_MODEL', '<unset>')}")
    try:
        cfg = BackendConfig.from_env()
    except BackendError as error:
        print(f"status      : INVALID - {error}")
        return 1
    _key, source = resolve_api_key()
    print(f"endpoint    : {cfg.endpoint}")
    print(f"key source  : {source} (value hidden)")
    print(f"max tokens  : {cfg.max_tokens_param}={cfg.max_tokens}")
    for name in ("https_proxy", "HTTPS_PROXY", "http_proxy", "HTTP_PROXY", "no_proxy", "NO_PROXY"):
        if os.environ.get(name):
            print(f"proxy env   : {name} is set")
    print("status      : configuration complete (run smoke-test to probe the endpoint)")
    return 0


def run_smoke_test(json_check: bool) -> int:
    config = BackendConfig.from_env()
    prompt = (
        'Reply with only this JSON object: {"nodes": [], "edges": []}'
        if json_check
        else "What is the result of 1+1? Answer briefly."
    )
    started = time.monotonic()
    response = request_chat(
        config, [{"role": "user", "content": prompt}], max_tokens=500
    )
    text = _extract_content(response)
    elapsed = time.monotonic() - started
    if json_check:
        import re

        stripped = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError as error:
            raise BackendError(
                "reply was not parseable JSON; this model may not be reliable "
                f"for extraction: {error}"
            ) from error
        if not isinstance(parsed, dict):
            raise BackendError("reply JSON was not an object")
    in_tokens, out_tokens = _usage(response)
    print(text.strip()[:500])
    print(
        f"ok: model={response.get('model', config.model)} "
        f"latency={elapsed:.1f}s tokens_in={in_tokens} tokens_out={out_tokens}",
        file=sys.stderr,
    )
    return 0


def run_graphify(arguments: List[str]) -> int:
    register_graphify_backend()
    from graphify.__main__ import main as graphify_main

    original_argv = sys.argv[:]
    sys.argv = ["graphify", *arguments]
    try:
        result = graphify_main()
    finally:
        sys.argv = original_argv
    return int(result or 0)


USAGE = """Usage:
  graphify-fleet doctor                    show resolved config (offline)
  graphify-fleet smoke-test [--json-check] probe the endpoint
  graphify-fleet extract <path> [graphify extract options]
  graphify-fleet fleet <inventory|build|repair|merge|audit> ...
  graphify-fleet <graphify command> [arguments]

Required: GRAPHIFY_LLM_BASE_URL, GRAPHIFY_LLM_MODEL, and an API key from
$OPENAI_API_KEY, the variable named by GRAPHIFY_LLM_API_KEY_ENV, or the
credential helper GRAPHIFY_LLM_API_KEY_COMMAND."""


def main(arguments: Optional[List[str]] = None) -> int:
    args = list(sys.argv[1:] if arguments is None else arguments)
    if args and args[0] == "fleet":
        from fleet_index import main as fleet_main

        return fleet_main(args[1:])
    active = load_env()
    if not args or args[0] in {"-h", "--help", "help"}:
        print(f"{USAGE}\n\nConfig file: {active if active else '<not found>'}")
        return 0
    if args[0] == "doctor":
        return run_doctor()
    if args[0] == "smoke-test":
        return run_smoke_test("--json-check" in args[1:])
    if args[0] == "extract" and not any(
        a == "--backend" or a.startswith("--backend=") for a in args
    ):
        args.extend(["--backend", BACKEND_NAME])
    return run_graphify(args)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BackendError as error:
        print(f"graphify-fleet: {error}", file=sys.stderr)
        raise SystemExit(1)
