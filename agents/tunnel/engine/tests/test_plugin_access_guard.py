from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path

from router.access import (
    PluginAccessStore,
    STATE_APPROVED,
    STATE_DENIED,
    STATE_PENDING,
    STATE_REVOKED,
)


class PluginAccessGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def store(
        self,
        audience: str = "zagent-production",
        *,
        mode: str = "manager-approval",
        alias: str = "Ikbal",
    ) -> PluginAccessStore:
        return PluginAccessStore(
            self.root,
            audience=audience,
            display_name=audience,
            device_id=f"device-{alias.casefold()}",
            device_alias=alias,
            mode=mode,
            enforcement="mandatory",
            fallback_plugin="",
        )

    def test_unapproved_bootstrap_asks_operator(self) -> None:
        result = self.store().bootstrap("chat-01")
        self.assertFalse(result["allowed"])
        self.assertTrue(result["requires_operator_prompt"])

    def test_operator_must_match_local_device_alias(self) -> None:
        result = self.store().request_access("chat-01", "Harry")
        self.assertFalse(result["ok"])
        self.assertEqual("OPERATOR_DEVICE_ALIAS_MISMATCH", result["code"])
        self.assertFalse(result["created"])

    def test_manager_approval_persists(self) -> None:
        store = self.store()
        request = store.request_access("chat-01", "Ikbal")
        self.assertEqual(STATE_PENDING, request["state"])
        resolved = store.resolve_request(request["request_id"], "approve")
        self.assertEqual(STATE_APPROVED, resolved["state"])
        self.assertTrue(store.authorization("chat-01")["allowed"])

    def test_denied_is_fail_closed_without_fallback(self) -> None:
        store = self.store()
        request = store.request_access("chat-01", "Ikbal")
        store.resolve_request(request["request_id"], "deny")
        denied = store.authorization("chat-01")
        self.assertEqual(STATE_DENIED, denied["state"])
        self.assertEqual("PLUGIN_ACCESS_DENIED", denied["code"])
        self.assertIsNone(denied["recommended_plugin"])
        self.assertEqual("denied-fail-closed", store.bootstrap("chat-01")["bootstrap_policy"])

    def test_revoke_blocks_previously_approved_session(self) -> None:
        store = self.store()
        request = store.request_access("chat-01", "Ikbal")
        store.resolve_request(request["request_id"], "approve")
        store.revoke("chat-01")
        state = store.authorization("chat-01")
        self.assertEqual(STATE_REVOKED, state["state"])
        self.assertEqual("PLUGIN_ACCESS_REVOKED", state["code"])

    def test_wait_window_uses_same_request_and_approves(self) -> None:
        store = self.store()
        request = store.request_access("chat-01", "Ikbal")

        def approve() -> None:
            time.sleep(0.1)
            store.resolve_request(request["request_id"], "approve")

        thread = threading.Thread(target=approve)
        thread.start()
        waited = store.wait_for_request(
            request["request_id"],
            timeout_seconds=2.0,
            poll_interval_seconds=0.05,
        )
        thread.join()
        self.assertEqual(request["request_id"], waited["request_id"])
        self.assertEqual(STATE_APPROVED, waited["state"])
        self.assertFalse(waited["timed_out"])

    def test_wait_window_times_out_fail_closed(self) -> None:
        store = self.store()
        request = store.request_access("chat-01", "Ikbal")
        waited = store.wait_for_request(
            request["request_id"],
            timeout_seconds=0.1,
            poll_interval_seconds=0.05,
        )
        self.assertFalse(waited["ok"])
        self.assertEqual("APPROVAL_TIMEOUT", waited["code"])
        self.assertTrue(waited["timed_out"])

    def test_bound_production_session_blocks_try_plugin(self) -> None:
        production = self.store("zagent-production", alias="Ikbal")
        request = production.request_access("chat-01", "Ikbal")
        production.resolve_request(request["request_id"], "approve")
        production.lock_session_to_current_audience(
            "chat-01",
            operator_alias="Ikbal",
            target_device_id="device-ikbal",
            device_alias="Ikbal",
        )

        tryplugin = self.store("tryplugin", alias="Harry")
        status = tryplugin.status("chat-01")
        self.assertFalse(status["allowed"])
        self.assertEqual("PLUGIN_SESSION_LOCKED", status["code"])
        attempted = tryplugin.request_access("chat-01", "Harry")
        self.assertFalse(attempted["created"])
        self.assertEqual("PLUGIN_SESSION_LOCKED", attempted["code"])

    def test_dev_is_not_blocked_by_production_session_lock(self) -> None:
        production = self.store("zagent-production", alias="Ikbal")
        request = production.request_access("chat-01", "Ikbal")
        production.resolve_request(request["request_id"], "approve")
        production.lock_session_to_current_audience(
            "chat-01",
            operator_alias="Ikbal",
            target_device_id="device-ikbal",
            device_alias="Ikbal",
        )
        grant = production.create_dev_grant("chat-01")
        self.assertTrue(grant["allowed"])

        dev = self.store("zagent-dev", mode="production-grant", alias="Ikbal")
        self.assertTrue(dev.authorization("chat-01")["allowed"])

    def test_dev_child_session_inherits_parent_production_grant(self) -> None:
        production = self.store("zagent-production", alias="Ikbal")
        request = production.request_access("chat-prod", "Ikbal")
        production.resolve_request(request["request_id"], "approve")
        grant = production.create_dev_grant("chat-prod")
        self.assertTrue(grant["allowed"])

        dev = self.store("zagent-dev", mode="production-grant", alias="Ikbal")
        linked = dev.bind_dev_child(
            "chat-dev",
            "chat-prod",
            grant["grant_id"],
        )
        self.assertTrue(linked["allowed"])

        authorized = dev.authorization("chat-dev")
        self.assertTrue(authorized["allowed"])
        self.assertEqual("chat-prod", authorized["parent_session_id"])
        self.assertFalse(authorized["requires_manager_approval"])

        production.revoke("chat-prod")
        revoked = dev.authorization("chat-dev")
        self.assertFalse(revoked["allowed"])
        self.assertEqual("DEV_ACCESS_REQUIRES_PRODUCTION", revoked["code"])

    def test_dev_child_allows_different_dev_device_when_parent_trust_is_valid(self) -> None:
        production = self.store("zagent-production", alias="Ikbal")
        request = production.request_access("chat-prod", "Ikbal")
        production.resolve_request(request["request_id"], "approve")
        grant = production.create_dev_grant("chat-prod")

        dev = self.store("zagent-dev", mode="production-grant", alias="Ikbal dev")
        linked = dev.bind_dev_child(
            "chat-dev",
            "chat-prod",
            grant["grant_id"],
        )
        self.assertTrue(linked["allowed"])
        self.assertTrue(dev.authorization("chat-dev")["allowed"])

    def test_dev_child_rejects_grant_if_parent_approval_chain_is_tampered(self) -> None:
        production = self.store("zagent-production", alias="Ikbal")
        request = production.request_access("chat-prod", "Ikbal")
        production.resolve_request(request["request_id"], "approve")
        grant = production.create_dev_grant("chat-prod")
        grant["issuer_device_id"] = "forged-device"
        production._write(production._grant_path("chat-prod"), grant)

        dev = self.store("zagent-dev", mode="production-grant", alias="Ikbal dev")
        linked = dev.bind_dev_child(
            "chat-dev",
            "chat-prod",
            grant["grant_id"],
        )
        self.assertFalse(linked["allowed"])
        self.assertEqual("DEV_PARENT_TRUST_INVALID", linked["code"])

    def test_dev_child_rejects_wrong_parent_grant_id(self) -> None:
        production = self.store("zagent-production", alias="Ikbal")
        request = production.request_access("chat-prod", "Ikbal")
        production.resolve_request(request["request_id"], "approve")
        production.create_dev_grant("chat-prod")

        dev = self.store("zagent-dev", mode="production-grant", alias="Ikbal")
        linked = dev.bind_dev_child(
            "chat-dev",
            "chat-prod",
            "DEV-WRONG",
        )
        self.assertFalse(linked["allowed"])
        self.assertEqual("DEV_PARENT_GRANT_INVALID", linked["code"])

    def test_release_lock_allows_separate_audience_workflow(self) -> None:
        production = self.store("zagent-production", alias="Ikbal")
        production.lock_session_to_current_audience(
            "chat-01",
            operator_alias="Ikbal",
            target_device_id="device-ikbal",
            device_alias="Ikbal",
        )
        self.assertTrue(production.release_session_audience_lock("chat-01")["released"])
        tryplugin = self.store("tryplugin", alias="Harry")
        self.assertEqual("UNREQUESTED", tryplugin.status("chat-01")["state"])


if __name__ == "__main__":
    unittest.main()
