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


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def http_json(
    url: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    timeout: float = 3.0,
) -> dict[str, Any]:
    raw = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=raw,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = response.read()
            return json.loads(data.decode("utf-8")) if data else {}
    except urllib.error.HTTPError as exc:
        data = exc.read()
        body = json.loads(data.decode("utf-8")) if data else {}
        body["_http_status"] = exc.code
        return body


class PluginAccessRouterIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.engine = Path(__file__).resolve().parents[1]
        self.zagent_v3 = self.engine.parents[2]
        self.site_packages = (
            self.zagent_v3
            / "packaging"
            / "runtime"
            / "tunnel-python-3.11.9"
            / "Lib"
            / "site-packages"
        )
        self.port = free_port()
        self.process: subprocess.Popen[Any] | None = None

        config = self.root / "config"
        runtime = self.root / "runtime"
        access = self.root / "access"
        config.mkdir(parents=True)
        runtime.mkdir(parents=True)
        access.mkdir(parents=True)

        (config / "core_services.json").write_text(
            json.dumps({"services": []}, indent=2) + "\n",
            encoding="utf-8",
        )
        (config / "additional_services.json").write_text(
            json.dumps({"services": []}, indent=2) + "\n",
            encoding="utf-8",
        )
        (config / "tunnel.json").write_text(
            json.dumps(
                {
                    "router": {
                        "host": "127.0.0.1",
                        "port": self.port,
                        "log_level": "warning",
                    },
                    "security": {
                        "allow_local_upstreams_only": True,
                        "allowed_hosts": ["127.0.0.1", "localhost"],
                        "allow_http": True,
                    },
                    "orchestration": {
                        "workers": 1,
                        "fast_lane_workers": 1,
                    },
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

        env = os.environ.copy()
        env.update(
            {
                "PYTHONPATH": os.pathsep.join(
                    [str(self.engine), str(self.site_packages)]
                ),
                "ZAGENT_TUNNEL_CONFIG_DIR": str(config),
                "ZAGENT_TUNNEL_RUNTIME_DIR": str(runtime),
                "ZAGENT_CHAT_LOG_PATH": str(runtime / "chat.jsonl"),
                "ZAGENT_DEVICE_ID": "access-test-device",
                "ZAGENT_DEVICE_NAME": "ACCESS-TEST",
                "ZAGENT_DEVICE_ALIAS": "Ikbal",
                "ZAGENT_DEVICE_FINGERPRINT": "ab" * 32,
                "ZAGENT_INSTALLATION_ID": "access-test-install",
                "ZAGENT_DEVICE_BINDING": "cd" * 32,
                "ZAGENT_IDENTITY_ENFORCEMENT": "mandatory",
                "ZAGENT_PLUGIN_AUDIENCE": "zagent-production",
                "ZAGENT_PLUGIN_DISPLAY_NAME": "Zagent",
                "ZAGENT_PLUGIN_ACCESS_MODE": "manager-approval",
                "ZAGENT_PLUGIN_ACCESS_ENFORCEMENT": "mandatory",
                "ZAGENT_PLUGIN_ACCESS_ROOT": str(access),
                # Must be ignored by the new local-isolation contract.
                "ZAGENT_PLUGIN_FALLBACK": "tryplugin",
            }
        )

        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self.process = subprocess.Popen(
            [os.sys.executable, "-m", "router.server"],
            cwd=str(self.engine),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            creationflags=creationflags,
        )
        self.wait_ready()

    def tearDown(self) -> None:
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
        self.temp.cleanup()

    def wait_ready(self) -> None:
        deadline = time.time() + 12
        while time.time() < deadline:
            if self.process is not None and self.process.poll() is not None:
                stdout, stderr = self.process.communicate(timeout=1)
                self.fail(
                    f"router exited early: {self.process.returncode}\n"
                    f"stdout={stdout}\nstderr={stderr}"
                )
            try:
                body = http_json(
                    f"http://127.0.0.1:{self.port}/healthz",
                    timeout=0.5,
                )
                if body.get("ok"):
                    return
            except Exception:
                pass
            time.sleep(0.1)
        self.fail("router did not become ready")

    def mcp_call(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        *,
        raw_session: str = "plugin-access-integration",
        timeout: float = 5.0,
    ) -> dict[str, Any]:
        payload = {
            "jsonrpc": "2.0",
            "id": f"call-{name}",
            "method": "tools/call",
            "params": {
                "name": name,
                "arguments": arguments or {},
                "_meta": {
                    "openai/session": raw_session,
                },
            },
        }
        return http_json(
            f"http://127.0.0.1:{self.port}/mcp",
            method="POST",
            payload=payload,
            timeout=timeout,
        )

    @staticmethod
    def result_text(response: dict[str, Any]) -> dict[str, Any]:
        text = response["result"]["content"][0]["text"]
        return json.loads(text)

    def approve(self, request_id: str) -> dict[str, Any]:
        return http_json(
            f"http://127.0.0.1:{self.port}/router/access/request/{request_id}/resolve",
            method="POST",
            payload={"action": "approve"},
        )

    def test_access_wait_identity_and_local_binding(self) -> None:
        bootstrap = self.result_text(self.mcp_call("zagent_access_bootstrap"))
        self.assertFalse(bootstrap["allowed"])
        self.assertTrue(bootstrap["requires_operator_prompt"])

        wrong = self.result_text(
            self.mcp_call(
                "zagent_access_request",
                {"operator_alias": "Harry"},
            )
        )
        self.assertEqual("OPERATOR_DEVICE_ALIAS_MISMATCH", wrong["code"])
        self.assertFalse(wrong["created"])

        requested = self.result_text(
            self.mcp_call(
                "zagent_access_request",
                {"operator_alias": "Ikbal"},
            )
        )
        self.assertTrue(requested["pending"])
        request_id = requested["request_id"]
        self.assertIsNone(requested.get("recommended_plugin"))

        timed_out = self.result_text(
            self.mcp_call(
                "zagent_access_wait",
                {
                    "request_id": request_id,
                    "timeout_seconds": 0.15,
                    "poll_interval_seconds": 0.05,
                },
                timeout=3.0,
            )
        )
        self.assertEqual("APPROVAL_TIMEOUT", timed_out["code"])
        self.assertEqual(request_id, timed_out["request_id"])

        approved = self.approve(request_id)
        self.assertTrue(approved["allowed"])

        waited = self.result_text(
            self.mcp_call(
                "zagent_access_wait",
                {"request_id": request_id},
            )
        )
        self.assertEqual("APPROVED", waited["state"])
        self.assertEqual(request_id, waited["request_id"])

        identity = self.result_text(self.mcp_call("zagent_identity_bootstrap"))
        self.assertFalse(identity["bound"])
        inventory = identity["device_inventory"]
        self.assertEqual("local-device-only", inventory["inventory_scope"])
        self.assertEqual(1, inventory["count"])

        device_id = inventory["devices"][0]["device_id"]
        bound = self.result_text(
            self.mcp_call(
                "zagent_identity_session_bind",
                {
                    "operator_alias": "Ikbal",
                    "target_device_id": device_id,
                },
            )
        )
        self.assertTrue(bound["bound"])
        self.assertEqual("zagent-production", bound["session_plugin_lock"])
        self.assertEqual(
            bound["binding"]["target_device_id"],
            device_id,
        )
        self.assertEqual(
            bound["binding"]["device_alias"].casefold(),
            bound["binding"]["operator_alias"].casefold(),
        )

        status = self.result_text(self.mcp_call("zagent_identity_status"))
        self.assertEqual(
            "single-device-local-authority",
            status["authority_model"],
        )
        self.assertEqual(
            device_id,
            status["session_binding"]["target_device_id"],
        )

    def test_remote_target_and_internal_relay_fail_closed(self) -> None:
        request = self.result_text(
            self.mcp_call(
                "zagent_access_request",
                {"operator_alias": "Ikbal"},
                raw_session="relay-test",
            )
        )
        self.approve(request["request_id"])

        remote = self.mcp_call(
            "zagent_identity_session_bind",
            {
                "operator_alias": "Ikbal",
                "target_device_id": "remote-device-id",
            },
            raw_session="relay-test",
        )
        self.assertIn("error", remote)
        self.assertIn("IDENTITY_MISMATCH", remote["error"]["message"])

        relay = http_json(
            f"http://127.0.0.1:{self.port}/router/internal/execute",
            method="POST",
            payload={
                "service_id": "anything",
                "tool_name": "anything",
                "arguments": {},
                "meta": {"target_device_id": "remote-device-id"},
            },
        )
        self.assertEqual(410, relay["_http_status"])
        self.assertEqual("IDENTITY_MISMATCH", relay["error"]["code"])


if __name__ == "__main__":
    unittest.main()
