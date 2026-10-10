from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from router.identity import (
    ERROR_DEVICE_SWITCH_CONFIRMATION_REQUIRED,
    ERROR_IDENTITY_MISMATCH,
    ERROR_OPERATOR_DEVICE_ALIAS_MISMATCH,
    ERROR_SESSION_IDENTITY_REQUIRED,
    IdentityBindingStore,
    IdentityRoutingError,
)

DEVICE_ID = "11111111-2222-3333-4444-555555555555"
REMOTE_ID = "66666666-7777-8888-9999-aaaaaaaaaaaa"
DEVICE_BINDING = "ab" * 32


class ZisbIdentityTests(unittest.TestCase):
    def make_store(
        self,
        root: Path,
        *,
        enforcement: str = "mandatory",
        device_id: str = DEVICE_ID,
        device_alias: str = "Ikbal",
        control_plane_path: Path | None = None,
    ) -> IdentityBindingStore:
        return IdentityBindingStore(
            root,
            device_id=device_id,
            device_name=device_alias,
            device_alias=device_alias,
            fingerprint="cd" * 32,
            installation_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            device_binding=DEVICE_BINDING,
            enforcement_mode=enforcement,
            control_plane_path=control_plane_path,
        )

    def test_local_authority_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = self.make_store(Path(tmp))
            status = store.status()
            self.assertEqual("local-device-only", status["registry_scope"])
            self.assertEqual("single-device-local-authority", status["authority_model"])
            self.assertFalse(status["shared_authority_configured"])

    def test_shared_control_plane_path_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            requested_shared = root / "shared" / "identity.json"
            store = self.make_store(root / "local", control_plane_path=requested_shared)
            self.assertNotEqual(requested_shared.resolve(), store.path)
            self.assertTrue(store.legacy_shared_control_plane_ignored)
            self.assertEqual("local-device-only", store.registry_scope)

    def test_list_devices_returns_local_device_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = self.make_store(Path(tmp))
            inventory = store.list_devices()
            self.assertEqual(1, inventory["count"])
            self.assertEqual(DEVICE_ID, inventory["devices"][0]["device_id"])
            self.assertEqual("local-device-only", inventory["inventory_scope"])
            self.assertTrue(inventory["inventory_authoritative"])
            self.assertFalse(inventory["cross_device_routing"])

    def test_operator_must_match_device_alias(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = self.make_store(Path(tmp))
            with self.assertRaises(IdentityRoutingError) as captured:
                store.bind("chat-a", "Harry", DEVICE_ID)
            self.assertEqual(
                ERROR_OPERATOR_DEVICE_ALIAS_MISMATCH,
                captured.exception.code,
            )
            self.assertIsNone(store.get("chat-a"))

    def test_operator_match_is_case_insensitive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = self.make_store(Path(tmp))
            bound = store.bind("chat-a", " ikbal ", DEVICE_ID)
            self.assertEqual("ikbal", bound["operator_alias"].casefold())
            self.assertEqual("local-device", bound["authority"])

    def test_bind_restart_and_resolve_local(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = self.make_store(root)
            store.bind("chat-a", "Ikbal", DEVICE_ID)
            restored = self.make_store(root)
            resolved = restored.resolve_target("chat-a")
            self.assertEqual(DEVICE_ID, resolved["target_device_id"])
            self.assertEqual("local-device-session", resolved["binding_source"])
            self.assertTrue(resolved["local_target"])

    def test_many_chats_same_local_operator_are_independent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = self.make_store(Path(tmp))
            for i in range(20):
                store.bind(f"chat-{i}", "Ikbal", DEVICE_ID)
            self.assertEqual(20, store.list_bindings()["count"])

    def test_remote_target_bind_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = self.make_store(Path(tmp))
            with self.assertRaises(IdentityRoutingError) as captured:
                store.bind("chat-remote", "Ikbal", REMOTE_ID)
            self.assertEqual(ERROR_IDENTITY_MISMATCH, captured.exception.code)

    def test_remote_route_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = self.make_store(Path(tmp))
            with self.assertRaises(IdentityRoutingError) as captured:
                store.route_for_target(REMOTE_ID)
            self.assertEqual(ERROR_IDENTITY_MISMATCH, captured.exception.code)

    def test_remote_switch_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = self.make_store(Path(tmp))
            store.bind("chat-a", "Ikbal", DEVICE_ID)
            with self.assertRaises(IdentityRoutingError) as captured:
                store.switch("chat-a", REMOTE_ID, confirmation=True)
            self.assertEqual(ERROR_IDENTITY_MISMATCH, captured.exception.code)
            self.assertEqual(DEVICE_ID, store.get("chat-a")["target_device_id"])

    def test_local_switch_requires_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = self.make_store(Path(tmp))
            store.bind("chat-a", "Ikbal", DEVICE_ID)
            with self.assertRaises(IdentityRoutingError) as captured:
                store.switch("chat-a", DEVICE_ID, confirmation=False)
            self.assertEqual(
                ERROR_DEVICE_SWITCH_CONFIRMATION_REQUIRED,
                captured.exception.code,
            )

    def test_unbound_mandatory_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = self.make_store(Path(tmp))
            with self.assertRaises(IdentityRoutingError) as captured:
                store.resolve_target("chat-unbound")
            self.assertEqual(ERROR_SESSION_IDENTITY_REQUIRED, captured.exception.code)

    def test_bound_wrong_supplied_target_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = self.make_store(Path(tmp))
            store.bind("chat-a", "Ikbal", DEVICE_ID)
            with self.assertRaises(IdentityRoutingError) as captured:
                store.resolve_target("chat-a", REMOTE_ID)
            self.assertEqual(ERROR_IDENTITY_MISMATCH, captured.exception.code)

    def test_challenge_proof(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = self.make_store(Path(tmp))
            challenge = store.create_challenge()
            proof = store.create_local_proof(challenge["challenge_id"])
            verified = store.verify_challenge(
                challenge["challenge_id"],
                proof["proof"],
            )
            self.assertTrue(verified["trusted"])


if __name__ == "__main__":
    unittest.main()
