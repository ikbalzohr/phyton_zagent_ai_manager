from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from router.identity import (
    ERROR_IDENTITY_MISMATCH,
    IdentityBindingStore,
    IdentityRoutingError,
)

DEVICE_A = "11111111-2222-3333-4444-555555555555"
DEVICE_B = "66666666-7777-8888-9999-aaaaaaaaaaaa"


class LocalAuthorityNoRelayTests(unittest.TestCase):
    def store(self, root: Path, device_id: str, alias: str) -> IdentityBindingStore:
        return IdentityBindingStore(
            root,
            device_id=device_id,
            device_name=alias,
            device_alias=alias,
            fingerprint="ab" * 32,
            installation_id=f"install-{alias}",
            device_binding="cd" * 32,
            enforcement_mode="mandatory",
            control_plane_path=root.parent / "legacy-shared.json",
            route_url="http://127.0.0.1:18810",
        )

    def test_two_local_stores_do_not_form_shared_authority(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            a = self.store(base / "a", DEVICE_A, "Ikbal")
            b = self.store(base / "b", DEVICE_B, "Harry")
            self.assertEqual(1, a.list_devices()["count"])
            self.assertEqual(1, b.list_devices()["count"])
            self.assertEqual(DEVICE_A, a.list_devices()["devices"][0]["device_id"])
            self.assertEqual(DEVICE_B, b.list_devices()["devices"][0]["device_id"])
            self.assertNotEqual(a.path, b.path)

    def test_local_store_cannot_bind_remote_device(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            a = self.store(base / "a", DEVICE_A, "Ikbal")
            with self.assertRaises(IdentityRoutingError) as captured:
                a.bind("chat-a", "Ikbal", DEVICE_B)
            self.assertEqual(ERROR_IDENTITY_MISMATCH, captured.exception.code)

    def test_local_store_cannot_route_remote_device(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            a = self.store(base / "a", DEVICE_A, "Ikbal")
            with self.assertRaises(IdentityRoutingError) as captured:
                a.route_for_target(DEVICE_B)
            self.assertEqual(ERROR_IDENTITY_MISMATCH, captured.exception.code)


if __name__ == "__main__":
    unittest.main()
