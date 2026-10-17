from __future__ import annotations

import json
import os
import socket
import subprocess
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ENGINE = Path(__file__).resolve().parents[1]
ACCESS_REQUIRED = -32041


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _post(url: str, payload: dict[str, Any], headers: dict[str, str] | None = None):
    merged = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    merged.update(headers or {})
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), method="POST", headers=merged
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            raw = response.read()
            return (
                {k.lower(): v for k, v in response.headers.items()},
                json.loads(raw.decode("utf-8")) if raw else {},
            )
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        return (
            {k.lower(): v for k, v in exc.headers.items()},
            json.loads(raw.decode("utf-8")) if raw else {},
        )


class _RouterCase(unittest.TestCase):
    EXTRA_ENV: dict[str, str] = {}

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.port = _free_port()
        for name in ("config", "runtime", "access"):
            (root / name).mkdir()
        (root / "config" / "core_services.json").write_text('{"services": []}\n')
        (root / "config" / "additional_services.json").write_text('{"services": []}\n')
        (root / "config" / "tunnel.json").write_text(
            json.dumps(
                {
                    "router": {"host": "127.0.0.1", "port": self.port, "log_level": "warning"},
                    "security": {
                        "allow_local_upstreams_only": True,
                        "allowed_hosts": ["127.0.0.1", "localhost"],
                        "allow_http": True,
                    },
                    "orchestration": {"workers": 1, "fast_lane_workers": 1},
                }
            )
            + "\n"
        )
        env = os.environ.copy()
        env.update(
            {
                "PYTHONPATH": str(ENGINE),
                "ZAGENT_TUNNEL_CONFIG_DIR": str(root / "config"),
                "ZAGENT_TUNNEL_RUNTIME_DIR": str(root / "runtime"),
                "ZAGENT_CHAT_LOG_PATH": str(root / "runtime" / "chat.jsonl"),
                "ZAGENT_DEVICE_ID": "mcp-host-test-device",
                "ZAGENT_DEVICE_NAME": "MCP-HOST-TEST",
                "ZAGENT_DEVICE_ALIAS": "Ikbal",
                "ZAGENT_DEVICE_FINGERPRINT": "ab" * 32,
                "ZAGENT_INSTALLATION_ID": "mcp-host-test-install",
                "ZAGENT_DEVICE_BINDING": "cd" * 32,
                "ZAGENT_IDENTITY_ENFORCEMENT": "mandatory",
                "ZAGENT_PLUGIN_AUDIENCE": "zagent-production",
                "ZAGENT_PLUGIN_DISPLAY_NAME": "Zagent",
                "ZAGENT_PLUGIN_ACCESS_MODE": "manager-approval",
                "ZAGENT_PLUGIN_ACCESS_ENFORCEMENT": "mandatory",
                "ZAGENT_PLUGIN_ACCESS_ROOT": str(root / "access"),
                "ZAGENT_PRODUCTION_ARCMAP_MCP_URL": "http://127.0.0.1:1/mcp",
            }
        )
        for key in ("ZAGENT_MCP_HOST_SESSIONS", "ZAGENT_MCP_HOST_CLIENTS", "ZAGENT_MCP_HOST_FIXED_SESSION"):
            env.pop(key, None)
        env.update(self.EXTRA_ENV)
        self.process = subprocess.Popen(
            [os.sys.executable, "-m", "router.server"],
            cwd=str(ENGINE),
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        deadline = time.time() + 15
        while time.time() < deadline:
            if self.process.poll() is not None:
                self.fail("router exited early: " + (self.process.stderr.read() or ""))
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/healthz", timeout=0.5) as r:
                    if json.loads(r.read()).get("ok"):
                        return
            except Exception:
                pass
            time.sleep(0.1)
        self.fail("router did not become ready")

    def tearDown(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
        if self.process.stderr:
            self.process.stderr.close()
        self.temp.cleanup()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/mcp"

    def initialize(self, client_name: str = "claude-ai"):
        return _post(
            self.url,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": client_name, "version": "1.0"},
                },
            },
        )

    def call(self, name: str, arguments: dict[str, Any] | None = None, *, mcp_session: str | None = None, openai_session: str | None = None, headers: dict[str, str] | None = None):
        headers = dict(headers or {})
        if mcp_session:
            headers["Mcp-Session-Id"] = mcp_session
        params: dict[str, Any] = {"name": name, "arguments": arguments or {}}
        if openai_session:
            params["_meta"] = {"openai/session": openai_session}
        _, body = _post(
            self.url,
            {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": params},
            headers,
        )
        return body

    def chat_log(self) -> list[dict[str, Any]]:
        path = Path(self.temp.name) / "runtime" / "chat.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def ensure_approved(self, **who: Any) -> None:
        status = self.payload(self.call("zagent_access_status", **who))
        if not status.get("allowed"):
            self.request_and_approve(**who)

    def progress(self, text: str, **who: Any) -> dict[str, Any]:
        """Approve the session, send a progress message, return the new chat-log row."""
        self.ensure_approved(**who)
        before = len(self.chat_log())
        self.call("zagent_telemetry_status", {"progress_message": text}, **who)
        rows = self.chat_log()
        self.assertEqual(before + 1, len(rows), rows)
        return rows[-1]

    def stop_and_read_stderr(self) -> str:
        self.process.terminate()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
        return self.process.stderr.read() or ""

    @staticmethod
    def payload(body: dict[str, Any]) -> dict[str, Any]:
        return json.loads(body["result"]["content"][0]["text"])

    def approve(self, request_id: str) -> dict[str, Any]:
        _, body = _post(
            f"http://127.0.0.1:{self.port}/router/access/request/{request_id}/resolve",
            {"action": "approve"},
        )
        return body

    def request_and_approve(self, **who: str) -> None:
        requested = self.payload(self.call("zagent_access_request", {"operator_alias": "Ikbal"}, **who))
        self.assertTrue(requested["pending"], requested)
        self.assertTrue(self.approve(requested["request_id"])["allowed"])

    def guard_denied(self, **who: str) -> bool:
        """An operational (non-management) tool hits the access guard first."""
        body = self.call("files_probe", {}, **who)
        return body.get("error", {}).get("code") == ACCESS_REQUIRED


class DefaultOffTests(_RouterCase):
    def test_default_behaviour_unchanged_for_clients_without_openai_session(self) -> None:
        headers, _ = self.initialize("claude-ai")
        self.assertNotIn("mcp-session-id", headers)

        boot = self.payload(self.call("zagent_access_bootstrap", mcp_session="anything"))
        self.assertEqual("", boot["session_id"])
        self.assertFalse(boot["allowed"])

        body = self.call("zagent_access_request", {"operator_alias": "Ikbal"}, mcp_session="anything")
        self.assertIn("session_id is required", body["error"]["message"])
        self.assertTrue(self.guard_denied(mcp_session="anything"))


class McpSessionHeaderTests(_RouterCase):
    EXTRA_ENV = {"ZAGENT_MCP_HOST_SESSIONS": "1"}

    def test_initialize_issues_session_only_to_configured_clients(self) -> None:
        headers, body = self.initialize("claude-ai")
        self.assertEqual(32, len(headers["mcp-session-id"]))
        self.assertEqual("2025-06-18", body["result"]["protocolVersion"])

        headers, _ = self.initialize("openai-mcp")
        self.assertNotIn("mcp-session-id", headers)

    def test_full_approval_flow_and_session_isolation(self) -> None:
        sid_a = self.initialize()[0]["mcp-session-id"]
        sid_b = self.initialize()[0]["mcp-session-id"]
        self.assertNotEqual(sid_a, sid_b)

        boot = self.payload(self.call("zagent_access_bootstrap", mcp_session=sid_a))
        self.assertTrue(boot["session_id"].startswith("chat_"))
        self.assertFalse(boot["allowed"])
        self.assertTrue(self.guard_denied(mcp_session=sid_a))

        wrong = self.payload(self.call("zagent_access_request", {"operator_alias": "Harry"}, mcp_session=sid_a))
        self.assertEqual("OPERATOR_DEVICE_ALIAS_MISMATCH", wrong["code"])

        self.request_and_approve(mcp_session=sid_a)
        self.assertTrue(self.payload(self.call("zagent_access_status", mcp_session=sid_a))["allowed"])
        self.assertFalse(self.guard_denied(mcp_session=sid_a))

        # Another Claude session, and an OpenAI session, must stay denied.
        self.assertTrue(self.guard_denied(mcp_session=sid_b))
        self.assertTrue(self.guard_denied(openai_session=sid_a))

    def test_openai_sessions_keep_working_and_do_not_leak_into_mcp_sessions(self) -> None:
        self.request_and_approve(openai_session="chatgpt-chat-1")
        self.assertFalse(self.guard_denied(openai_session="chatgpt-chat-1"))
        self.assertTrue(self.guard_denied(openai_session="chatgpt-chat-2"))
        self.assertTrue(self.guard_denied(mcp_session="chatgpt-chat-1"))

    def test_openai_session_wins_when_both_are_present(self) -> None:
        self.request_and_approve(openai_session="chatgpt-chat-3")
        self.assertFalse(self.guard_denied(openai_session="chatgpt-chat-3", mcp_session="other"))

    def test_fails_closed_without_header_and_without_fixed_session(self) -> None:
        body = self.call("zagent_access_request", {"operator_alias": "Ikbal"})
        self.assertIn("session_id is required", body["error"]["message"])
        self.assertTrue(self.guard_denied())


class FixedSessionFallbackTests(_RouterCase):
    EXTRA_ENV = {
        "ZAGENT_MCP_HOST_SESSIONS": "1",
        "ZAGENT_MCP_HOST_FIXED_SESSION": "claude-connector",
    }

    def test_one_approval_covers_every_headerless_call(self) -> None:
        self.assertTrue(self.guard_denied())
        self.request_and_approve()
        self.assertFalse(self.guard_denied())
        # A different, explicit Mcp-Session-Id is still its own, unapproved session.
        self.assertTrue(self.guard_denied(mcp_session="some-other-session"))


class HostLabelTests(_RouterCase):
    EXTRA_ENV = {
        "ZAGENT_MCP_HOST_SESSIONS": "1",
        "ZAGENT_MCP_HOST_CLIENTS": "claude,codex,other-agent",
    }

    def test_manager_chat_log_is_labelled_per_host(self) -> None:
        claude = self.initialize("claude-ai")[0]["mcp-session-id"]
        codex = self.initialize("codex-cli")[0]["mcp-session-id"]
        other = self.initialize("other-agent")[0]["mcp-session-id"]

        row = self.progress("halo dari claude", mcp_session=claude)
        self.assertEqual("Claude", row["source"])
        self.assertEqual("Claude", row["agent"])

        self.assertEqual("Codex", self.progress("halo dari codex", mcp_session=codex)["source"])
        self.assertEqual("MCP Client", self.progress("halo dari klien lain", mcp_session=other)["source"])

    def test_openai_sessions_keep_the_historical_chatgpt_label(self) -> None:
        row = self.progress("halo dari chatgpt", openai_session="chatgpt-label-1")
        self.assertEqual("ChatGPT", row["source"])
        self.assertEqual("ChatGPT", row["agent"])

    def test_diagnostic_log_names_the_signals_but_never_leaks_session_ids(self) -> None:
        sid = self.initialize("claude-ai")[0]["mcp-session-id"]
        self.call("zagent_access_bootstrap", mcp_session=sid)
        self.call("zagent_access_bootstrap", mcp_session=sid)  # same signals: logged once
        self.call("zagent_access_bootstrap", openai_session="chatgpt-secret-session")
        stderr = self.stop_and_read_stderr()

        self.assertIn("host detected: kind=claude session_source=mcp-session-id client='claude-ai'", stderr)
        self.assertIn("host detected: kind=openai session_source=openai", stderr)
        self.assertEqual(1, stderr.count("kind=claude session_source=mcp-session-id"))
        self.assertIn("openai/session", stderr)  # meta key NAME is shown...
        self.assertNotIn(sid, stderr)  # ...but never a session value
        self.assertNotIn("chatgpt-secret-session", stderr)

        # The same evidence is persisted for the operator (stderr is not captured).
        path = Path(self.temp.name) / "runtime" / "host_detection.jsonl"
        text = path.read_text(encoding="utf-8")
        rows = [json.loads(line) for line in text.splitlines() if line.strip()]
        self.assertEqual({"claude", "openai"}, {row["kind"] for row in rows})
        self.assertEqual(2, len(rows))
        self.assertNotIn(sid, text)
        self.assertNotIn("chatgpt-secret-session", text)


class UserAgentFallbackLabelTests(_RouterCase):
    EXTRA_ENV = {
        "ZAGENT_MCP_HOST_SESSIONS": "1",
        "ZAGENT_MCP_HOST_FIXED_SESSION": "connector",
    }

    def test_user_agent_labels_headerless_clients(self) -> None:
        row = self.progress("dari claude code", headers={"User-Agent": "claude-code/9.9"})
        self.assertEqual("Claude", row["source"])
        row = self.progress("dari klien anonim")
        self.assertEqual("MCP Client", row["source"])


if __name__ == "__main__":
    unittest.main()
