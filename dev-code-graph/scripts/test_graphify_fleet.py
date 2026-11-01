#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
import types
import unittest
import urllib.error
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

MODULE_PATH = Path(__file__).with_name("graphify_fleet.py")
SPEC = importlib.util.spec_from_file_location("graphify_fleet_under_test", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("cannot load graphify_fleet.py")
gf = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = gf
SPEC.loader.exec_module(gf)

OK_BODY = b'{"choices":[{"message":{"content":"ok"}}]}'


def make_config(key="secret", base="https://example.test/v1", **kw):
    return gf.BackendConfig(
        api_key=key, base_url=base, model="stub-model",
        max_tokens=500, timeout_seconds=12, **kw,
    )


class FakeResponse:
    def __init__(self, body=OK_BODY):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return None

    def read(self):
        return self.body


def http_error(code, body=b"", headers=None):
    error = urllib.error.HTTPError("https://example.test", code, "x", headers or {}, None)
    error.read = lambda: body
    return error


class EnvFileTest(unittest.TestCase):
    def test_load_env_does_not_override_process_value(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / ".env"
            f.write_text("GRAPHIFY_LLM_MODEL=from-file\nGRAPHIFY_LLM_BASE_URL='http://x/v1'\n")
            f.chmod(0o600)
            with mock.patch.dict(os.environ, {"GRAPHIFY_LLM_MODEL": "proc"}, clear=True):
                self.assertEqual(gf.load_env(f), f.resolve())
                self.assertEqual(os.environ["GRAPHIFY_LLM_MODEL"], "proc")
                self.assertEqual(os.environ["GRAPHIFY_LLM_BASE_URL"], "http://x/v1")

    def test_explicit_missing_file_does_not_fall_back(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            other = root / "home" / ".config" / "graphify-fleet" / ".env"
            other.parent.mkdir(parents=True)
            other.write_text("X=1\n")
            other.chmod(0o600)
            with mock.patch.dict(
                os.environ, {"GRAPHIFY_FLEET_ENV": str(root / "missing.env")}, clear=True
            ), mock.patch.object(Path, "home", return_value=root / "home"):
                with self.assertRaisesRegex(gf.BackendError, "does not point to a file"):
                    gf.find_config_path()

    def test_default_location_is_found(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            f = home / ".config" / "graphify-fleet" / ".env"
            f.parent.mkdir(parents=True)
            f.write_text("X=1\n")
            with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
                Path, "home", return_value=home
            ):
                self.assertEqual(gf.find_config_path(), f.resolve())

    def test_group_readable_file_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / ".env"
            f.write_text("X=1\n")
            f.chmod(0o640)
            with self.assertRaisesRegex(gf.BackendError, "group/world accessible"):
                gf.load_env(f)


class CredentialTest(unittest.TestCase):
    def test_key_from_default_env_var(self):
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": " k1 "}, clear=True):
            self.assertEqual(gf.resolve_api_key(), ("k1", "$OPENAI_API_KEY"))

    def test_key_from_named_env_var(self):
        with mock.patch.dict(
            os.environ, {"GRAPHIFY_LLM_API_KEY_ENV": "MY_KEY", "MY_KEY": "k2"}, clear=True
        ):
            self.assertEqual(gf.resolve_api_key()[0], "k2")

    def test_helper_command_wins_and_needs_no_shell(self):
        cmd = f"{sys.executable} -c \"print('helper-key')\""
        with mock.patch.dict(
            os.environ,
            {"GRAPHIFY_LLM_API_KEY_COMMAND": cmd, "OPENAI_API_KEY": "ignored"},
            clear=True,
        ):
            key, source = gf.resolve_api_key()
        self.assertEqual(key, "helper-key")
        self.assertNotIn("helper-key", source)

    def test_helper_failure_never_echoes_output(self):
        cmd = f"{sys.executable} -c \"import sys; print('leaky-secret'); sys.exit(3)\""
        with mock.patch.dict(os.environ, {"GRAPHIFY_LLM_API_KEY_COMMAND": cmd}, clear=True):
            with self.assertRaises(gf.BackendError) as ctx:
                gf.resolve_api_key()
        self.assertNotIn("leaky-secret", str(ctx.exception))

    def test_missing_helper_binary_is_clean_error(self):
        with mock.patch.dict(
            os.environ, {"GRAPHIFY_LLM_API_KEY_COMMAND": "/nonexistent/helper"}, clear=True
        ):
            with self.assertRaisesRegex(gf.BackendError, "could not run"):
                gf.resolve_api_key()

    def test_missing_key_message_names_options(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(gf.BackendError, "OPENAI_API_KEY"):
                gf.resolve_api_key()


class ConfigTest(unittest.TestCase):
    def env(self, **extra):
        base = {
            "GRAPHIFY_LLM_BASE_URL": "https://example.test/v1/",
            "GRAPHIFY_LLM_MODEL": "m",
            "OPENAI_API_KEY": "k",
        }
        base.update(extra)
        return mock.patch.dict(os.environ, base, clear=True)

    def test_endpoint_appends_chat_path_once(self):
        with self.env():
            cfg = gf.BackendConfig.from_env()
        self.assertEqual(cfg.endpoint, "https://example.test/v1/chat/completions")
        full = make_config(base="https://example.test/v1/chat/completions")
        self.assertEqual(full.endpoint, "https://example.test/v1/chat/completions")

    def test_required_vars_and_scheme(self):
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "k"}, clear=True):
            with self.assertRaisesRegex(gf.BackendError, "required"):
                gf.BackendConfig.from_env()
        with self.env(GRAPHIFY_LLM_BASE_URL="ftp://x"):
            with self.assertRaisesRegex(gf.BackendError, "http"):
                gf.BackendConfig.from_env()

    def test_numeric_and_param_validation(self):
        for extra in (
            {"GRAPHIFY_LLM_MAX_TOKENS": "abc"},
            {"GRAPHIFY_LLM_TIMEOUT_SECONDS": "0"},
            {"GRAPHIFY_LLM_MAX_TOKENS_PARAM": "bogus"},
        ):
            with self.env(**extra):
                with self.assertRaises(gf.BackendError):
                    gf.BackendConfig.from_env()


class RequestTest(unittest.TestCase):
    def test_payload_and_headers(self):
        captured = {}

        def fake(request, timeout):
            captured["r"], captured["t"] = request, timeout
            return FakeResponse()

        with mock.patch.object(gf.urllib.request, "urlopen", fake):
            gf.request_chat(make_config(), [{"role": "user", "content": "1+1"}])
        r = captured["r"]
        payload = json.loads(r.data)
        self.assertEqual(r.get_header("Authorization"), "Bearer secret")
        self.assertTrue(r.full_url.endswith("/v1/chat/completions"))
        self.assertNotIn("secret", r.full_url)
        self.assertEqual(payload["max_tokens"], 500)
        self.assertEqual(payload["model"], "stub-model")
        self.assertFalse(payload["stream"])
        self.assertEqual(captured["t"], 12)

    def test_max_completion_tokens_param(self):
        captured = {}
        with mock.patch.object(
            gf.urllib.request, "urlopen",
            lambda r, timeout: captured.setdefault("r", r) and FakeResponse(),
        ):
            gf.request_chat(make_config(max_tokens_param="max_completion_tokens"), [])
        payload = json.loads(captured["r"].data)
        self.assertIn("max_completion_tokens", payload)
        self.assertNotIn("max_tokens", payload)

    def retry_env(self, retries="2"):
        return mock.patch.dict(
            os.environ,
            {
                "GRAPHIFY_LLM_MAX_RETRIES": retries,
                "GRAPHIFY_LLM_RETRY_BASE_SECONDS": "1",
                "GRAPHIFY_LLM_RETRY_MAX_SECONDS": "2",
            },
        )

    def test_retries_429_then_succeeds(self):
        calls = []

        def fake(request, timeout):
            calls.append(1)
            if len(calls) == 1:
                raise http_error(429)
            return FakeResponse()

        with mock.patch.object(gf.urllib.request, "urlopen", fake), mock.patch.object(
            gf.time, "sleep"
        ) as sleep, self.retry_env():
            gf.request_chat(make_config(), [])
        self.assertEqual(len(calls), 2)
        sleep.assert_called_once_with(1.0)

    def test_retry_after_finite_and_capped(self):
        calls = []

        def fake(request, timeout):
            calls.append(1)
            if len(calls) <= 2:
                raise http_error(429, headers={"Retry-After": "999999" if len(calls) == 1 else "inf"})
            return FakeResponse()

        with mock.patch.object(gf.urllib.request, "urlopen", fake), mock.patch.object(
            gf.time, "sleep"
        ) as sleep, self.retry_env():
            gf.request_chat(make_config(), [])
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [2.0, 2.0])

    def test_non_retryable_status_fails_fast_without_leaking_key(self):
        key = "top-" + "secret-key"
        body = f'{{"error":"bad","echo":"{key}"}}'.encode()
        with mock.patch.object(
            gf.urllib.request, "urlopen", side_effect=http_error(401, body)
        ) as urlopen, mock.patch.object(gf.time, "sleep") as sleep:
            with self.assertRaises(gf.BackendError) as ctx:
                gf.request_chat(make_config(key=key), [])
        self.assertEqual(urlopen.call_count, 1)
        sleep.assert_not_called()
        self.assertIn("HTTP 401", str(ctx.exception))
        self.assertNotIn(key, str(ctx.exception))

    def test_redaction_happens_before_truncation(self):
        key = "boundary-" + "secret-key"
        encoded = urllib.parse.quote(key + " x", safe="")
        for k, planted in ((key, key), (key + " x", encoded)):
            body = ("x" * 995 + planted + "tail").encode()
            with mock.patch.object(
                gf.urllib.request, "urlopen", side_effect=http_error(400, body)
            ):
                with self.assertRaises(gf.BackendError) as ctx:
                    gf.request_chat(make_config(key=k), [])
            self.assertNotIn(planted, str(ctx.exception))

    def test_invalid_json_body_is_redacted_and_hints_base_url(self):
        key = "k" + "ey-in-html"
        page = f"<html>{key}</html>".encode()
        with mock.patch.object(
            gf.urllib.request, "urlopen", return_value=FakeResponse(page)
        ):
            with self.assertRaises(gf.BackendError) as ctx:
                gf.request_chat(make_config(key=key), [])
        self.assertNotIn(key, str(ctx.exception))
        self.assertIn("invalid JSON", str(ctx.exception))

    def test_network_error_retries_then_raises(self):
        with mock.patch.object(
            gf.urllib.request, "urlopen", side_effect=urllib.error.URLError("refused")
        ) as urlopen, mock.patch.object(gf.time, "sleep"), self.retry_env("1"):
            with self.assertRaisesRegex(gf.BackendError, "refused"):
                gf.request_chat(make_config(), [])
        self.assertEqual(urlopen.call_count, 2)

    def test_extract_content_shapes(self):
        self.assertEqual(gf._extract_content({"choices": [{"message": {"content": "a"}}]}), "a")
        self.assertEqual(
            gf._extract_content(
                {"choices": [{"message": {"content": [{"type": "text", "text": "b"}]}}]}
            ),
            "b",
        )
        self.assertEqual(gf._extract_content({"choices": [{"text": "c"}]}), "c")
        with self.assertRaisesRegex(gf.BackendError, "MAX_TOKENS"):
            gf._extract_content({"choices": [{"message": {"content": ""}}]})


class StubServerTest(unittest.TestCase):
    """End-to-end against a loopback OpenAI-compatible stub (no network)."""

    @classmethod
    def setUpClass(cls):
        cls.seen = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                cls.seen.append(
                    (self.path, self.headers.get("Authorization"), json.loads(self.rfile.read(length)))
                )
                body = json.dumps(
                    {
                        "model": "stub-model",
                        "choices": [{"message": {"content": '{"nodes": [], "edges": []}'}}],
                        "usage": {"prompt_tokens": 3, "completion_tokens": 2},
                    }
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_a):
                pass

        cls.server = HTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def env(self):
        return mock.patch.dict(
            os.environ,
            {
                "GRAPHIFY_LLM_BASE_URL": f"http://127.0.0.1:{self.server.server_port}/v1",
                "GRAPHIFY_LLM_MODEL": "stub-model",
                "OPENAI_API_KEY": "stub-key",
                "no_proxy": "127.0.0.1",
                "NO_PROXY": "127.0.0.1",
            },
            clear=True,
        )

    def test_smoke_test_json_check(self):
        self.seen.clear()
        with self.env():
            self.assertEqual(gf.main(["smoke-test", "--json-check"]), 0)
        path, auth, payload = self.seen[-1]
        self.assertEqual(path, "/v1/chat/completions")
        self.assertEqual(auth, "Bearer stub-key")
        self.assertEqual(payload["model"], "stub-model")

    def test_doctor_hides_key(self):
        with self.env(), mock.patch("sys.stdout") as out:
            self.assertEqual(gf.run_doctor(), 0)
        printed = "".join(c.args[0] for c in out.write.call_args_list)
        self.assertIn("key source", printed)
        self.assertNotIn("stub-key", printed)


class GraphifyIntegrationTest(unittest.TestCase):
    def fake_graphify(self):
        original_calls = []

        def original(*args, **kwargs):
            original_calls.append(kwargs)
            return {"original": True}

        llm = types.ModuleType("graphify.llm")
        llm.BACKENDS = {}
        llm.extract_files_direct = original
        llm._read_files = lambda files, root: "file content"
        llm._extraction_system = lambda deep=False: "deep system" if deep else "system"
        llm._parse_llm_json = json.loads
        pkg = types.ModuleType("graphify")
        pkg.llm = llm
        return llm, pkg, original_calls

    def test_registration_intercepts_only_own_backend(self):
        llm, pkg, calls = self.fake_graphify()
        with mock.patch.dict(sys.modules, {"graphify": pkg, "graphify.llm": llm}), mock.patch.object(
            gf.BackendConfig, "from_env", return_value=make_config()
        ):
            gf.register_graphify_backend()
            result = llm.extract_files_direct([], backend="kimi")
        self.assertEqual(result, {"original": True})
        self.assertEqual(calls[0]["backend"], "kimi")
        self.assertIn(gf.BACKEND_NAME, llm.BACKENDS)

    def test_extraction_path(self):
        llm, pkg, _ = self.fake_graphify()
        response = {
            "choices": [{"message": {"content": '{"nodes": [], "edges": []}'}}],
            "usage": {"prompt_tokens": 11, "completion_tokens": 7},
        }
        with mock.patch.dict(sys.modules, {"graphify": pkg, "graphify.llm": llm}), mock.patch.object(
            gf.BackendConfig, "from_env", return_value=make_config()
        ), mock.patch.object(gf, "request_chat", return_value=response) as request:
            gf.register_graphify_backend()
            result = llm.extract_files_direct(
                [Path("a.md")], backend=gf.BACKEND_NAME, root=Path("."), deep_mode=True
            )
        self.assertEqual((result["input_tokens"], result["output_tokens"]), (11, 7))
        self.assertEqual(result["model"], "stub-model")
        messages = request.call_args.args[1]
        self.assertEqual(messages[0]["content"], "deep system")
        self.assertEqual(messages[1]["content"], "file content")

    def test_extract_adds_backend_flag_unless_given(self):
        with mock.patch.object(gf, "load_env", return_value=None), mock.patch.object(
            gf, "run_graphify", return_value=0
        ) as run:
            gf.main(["extract", "repo"])
            self.assertEqual(run.call_args.args[0], ["extract", "repo", "--backend", gf.BACKEND_NAME])
            gf.main(["extract", "repo", "--backend", "kimi"])
            self.assertEqual(run.call_args.args[0], ["extract", "repo", "--backend", "kimi"])


class BackendNameTest(unittest.TestCase):
    def test_fleet_index_uses_same_backend_name(self):
        path = Path(__file__).with_name("fleet_index.py")
        spec = importlib.util.spec_from_file_location("fleet_index_name_check", path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        self.assertEqual(module.BACKEND_NAME, gf.BACKEND_NAME)


class LauncherTest(unittest.TestCase):
    def test_help_via_launcher_shows_config_without_secrets(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            env_file = home / ".config" / "graphify-fleet" / ".env"
            env_file.parent.mkdir(parents=True)
            env_file.write_text("OPENAI_API_KEY=very-secret\n")
            env_file.chmod(0o600)
            p = subprocess.run(
                [str(MODULE_PATH.with_name("graphify-fleet")), "--help"],
                capture_output=True, text=True, check=False,
                env={"PATH": os.environ.get("PATH", ""), "HOME": d, "GRAPHIFY_PYTHON": sys.executable},
            )
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn(str(env_file.resolve()), p.stdout)
        self.assertNotIn("very-secret", p.stdout)

    def test_bad_input_has_no_traceback(self):
        p = subprocess.run(
            [str(MODULE_PATH.with_name("graphify-fleet")), "smoke-test"],
            capture_output=True, text=True, check=False,
            env={"PATH": os.environ.get("PATH", ""), "HOME": "/nonexistent", "GRAPHIFY_PYTHON": sys.executable},
        )
        self.assertEqual(p.returncode, 1)
        self.assertNotIn("Traceback", p.stderr)
        self.assertIn("required", p.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
