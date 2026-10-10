from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


ERROR_SESSION_IDENTITY_REQUIRED = "SESSION_IDENTITY_REQUIRED"
ERROR_DEVICE_NOT_TRUSTED = "DEVICE_NOT_TRUSTED"
ERROR_TARGET_DEVICE_OFFLINE = "TARGET_DEVICE_OFFLINE"
ERROR_IDENTITY_MISMATCH = "IDENTITY_MISMATCH"
ERROR_IDENTITY_PROOF_FAILED = "IDENTITY_PROOF_FAILED"
ERROR_SESSION_BINDING_CONFLICT = "SESSION_BINDING_CONFLICT"
ERROR_DEVICE_SWITCH_CONFIRMATION_REQUIRED = "DEVICE_SWITCH_CONFIRMATION_REQUIRED"
ERROR_OPERATOR_DEVICE_ALIAS_MISMATCH = "OPERATOR_DEVICE_ALIAS_MISMATCH"


class IdentityRoutingError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.detail = message


class IdentityBindingStore:
    """
    Authoritative identity/session binding registry.

    v3 local-authority contract:
    - one installed Zagent is authoritative only for its own physical device;
    - multi-chat session bindings are durable but may target only the local device_id;
    - operator_alias must match the bound local device_alias;
    - cross-device relay, global device inventory, and shared gateway authority are
      intentionally outside local Zagent and belong to Zagent Cloud;
    - legacy identity_sessions.json remains a non-authoritative compatibility mirror.
    """

    SCHEMA_VERSION = 2
    LEGACY_SCHEMA_VERSION = 1

    def __init__(
        self,
        runtime_dir: Path,
        *,
        device_id: str,
        device_name: str,
        device_alias: str = "",
        fingerprint: str = "",
        installation_id: str = "",
        device_binding: str = "",
        enforcement_mode: str = "mandatory",
        control_plane_path: Path | str | None = None,
        device_ttl_seconds: int | None = None,
        route_url: str = "",
        router_id: str = "",
    ) -> None:
        self.runtime_dir = Path(runtime_dir).resolve()
        self.runtime_dir.mkdir(parents=True, exist_ok=True)

        configured_control_plane = str(
            control_plane_path
            or os.environ.get("ZAGENT_IDENTITY_CONTROL_PLANE_PATH", "")
            or ""
        ).strip()
        # Local Zagent is intentionally a single-device authority. A shared/global
        # authority is a Zagent Cloud concern, so local runtime never adopts a
        # shared control-plane path even when legacy configuration is present.
        self.path = self.runtime_dir / "identity_control_plane.json"
        self.registry_scope = "local-device-only"
        self.shared_authority_configured = False
        self.legacy_shared_control_plane_ignored = bool(configured_control_plane)

        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.legacy_path = self.runtime_dir / "identity_sessions.json"
        self.audit_path = self.runtime_dir / "identity_audit.jsonl"
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")

        self.device_id = str(device_id or "").strip().lower()
        self.device_name = str(device_name or "").strip()
        self.device_alias = str(
            device_alias or device_name or "Zagent Device"
        ).strip()
        self.fingerprint = str(fingerprint or "").strip()
        self.installation_id = str(installation_id or "").strip()
        self.device_binding = str(device_binding or "").strip().lower()
        self.route_url = str(
            route_url
            or os.environ.get("ZAGENT_ROUTER_ROUTE_URL", "")
            or ""
        ).strip().rstrip("/")
        self.router_id = str(
            router_id
            or os.environ.get("CONTROL_PLANE_TUNNEL_ID", "")
            or ""
        ).strip()

        ttl_raw = (
            device_ttl_seconds
            if device_ttl_seconds is not None
            else os.environ.get("ZAGENT_IDENTITY_DEVICE_TTL_SECONDS", "120")
        )
        try:
            ttl_value = int(ttl_raw)
        except (TypeError, ValueError):
            ttl_value = 120
        self.device_ttl_seconds = max(15, min(ttl_value, 3600))
        self.heartbeat_interval_seconds = max(
            5, min(30, self.device_ttl_seconds // 3)
        )

        mode = str(enforcement_mode or "mandatory").strip().lower()
        if mode not in {"compatibility", "mandatory"}:
            raise ValueError(
                "ZAGENT_IDENTITY_ENFORCEMENT must be compatibility or mandatory."
            )
        self.enforcement_mode = mode

        self._lock = threading.RLock()
        self._challenges: dict[str, dict[str, Any]] = {}
        self._ensure_file()
        self.heartbeat()

    @property
    def trust_state(self) -> str:
        if not self.device_id:
            return "error"
        if not self.device_binding:
            return "unverified"
        return "trusted"

    def _empty(self) -> dict[str, Any]:
        return {
            "schema_version": self.SCHEMA_VERSION,
            "revision": 0,
            "devices": {},
            "sessions": {},
        }

    @contextmanager
    def _file_guard(self, timeout_seconds: float = 5.0) -> Iterator[None]:
        deadline = time.monotonic() + max(0.5, timeout_seconds)
        fd: int | None = None

        while fd is None:
            try:
                fd = os.open(
                    str(self.lock_path),
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                )
                os.write(
                    fd,
                    (
                        json.dumps(
                            {
                                "pid": os.getpid(),
                                "created_at": time.time(),
                                "device_id": self.device_id or None,
                            }
                        )
                        + "\n"
                    ).encode("utf-8"),
                )
            except FileExistsError:
                try:
                    stale = (
                        time.time() - self.lock_path.stat().st_mtime
                    ) > 30.0
                except FileNotFoundError:
                    continue
                if stale:
                    try:
                        self.lock_path.unlink()
                    except FileNotFoundError:
                        pass
                    except OSError:
                        time.sleep(0.025)
                    continue
                if time.monotonic() >= deadline:
                    raise IdentityRoutingError(
                        ERROR_SESSION_BINDING_CONFLICT,
                        "Timed out waiting for the shared identity registry lock.",
                    )
                time.sleep(0.025)

        try:
            yield
        finally:
            try:
                if fd is not None:
                    os.close(fd)
            finally:
                try:
                    self.lock_path.unlink()
                except FileNotFoundError:
                    pass

    def _load_unlocked(self) -> dict[str, Any]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raw = self._empty()
        except Exception as exc:
            raise IdentityRoutingError(
                ERROR_SESSION_BINDING_CONFLICT,
                f"Identity control-plane registry is unreadable: {exc}",
            ) from exc

        if not isinstance(raw, dict):
            raise IdentityRoutingError(
                ERROR_SESSION_BINDING_CONFLICT,
                "Identity control-plane registry root must be an object.",
            )

        schema_version = int(raw.get("schema_version") or 0)
        if schema_version == self.LEGACY_SCHEMA_VERSION:
            sessions = raw.get("sessions")
            if not isinstance(sessions, dict):
                raise IdentityRoutingError(
                    ERROR_SESSION_BINDING_CONFLICT,
                    "Legacy identity session store sessions must be an object.",
                )
            raw = {
                "schema_version": self.SCHEMA_VERSION,
                "revision": 0,
                "devices": {},
                "sessions": sessions,
            }
        elif schema_version != self.SCHEMA_VERSION:
            raise IdentityRoutingError(
                ERROR_SESSION_BINDING_CONFLICT,
                f"Unsupported identity control-plane schema '{schema_version}'.",
            )

        devices = raw.get("devices")
        sessions = raw.get("sessions")
        if not isinstance(devices, dict) or not isinstance(sessions, dict):
            raise IdentityRoutingError(
                ERROR_SESSION_BINDING_CONFLICT,
                "Identity registry devices and sessions must be objects.",
            )

        raw.setdefault("revision", 0)
        return raw

    def _save_unlocked(self, data: dict[str, Any]) -> None:
        data = dict(data)
        data["schema_version"] = self.SCHEMA_VERSION
        data["revision"] = int(data.get("revision") or 0) + 1
        temporary = self.path.with_suffix(
            self.path.suffix + f".tmp.{secrets.token_hex(6)}"
        )
        payload = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
        temporary.write_text(payload, encoding="utf-8")
        os.replace(temporary, self.path)
        self._write_legacy_mirror_unlocked(data)

    def _write_legacy_mirror_unlocked(self, data: dict[str, Any]) -> None:
        if self.legacy_path == self.path:
            return
        try:
            mirror = {
                "schema_version": self.LEGACY_SCHEMA_VERSION,
                "authoritative": False,
                "source": str(self.path),
                "sessions": data.get("sessions", {}),
            }
            temporary = self.legacy_path.with_suffix(
                self.legacy_path.suffix + f".tmp.{secrets.token_hex(4)}"
            )
            temporary.write_text(
                json.dumps(mirror, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, self.legacy_path)
        except Exception:
            # Compatibility mirror failure must never corrupt the authoritative
            # registry or turn into a silent routing fallback.
            pass

    def _ensure_file(self) -> None:
        with self._lock:
            with self._file_guard():
                if self.path.exists():
                    data = self._load_unlocked()
                else:
                    data = self._empty()

                changed = not self.path.exists()

                if (
                    self.legacy_path != self.path
                    and self.legacy_path.exists()
                ):
                    try:
                        legacy = json.loads(
                            self.legacy_path.read_text(encoding="utf-8")
                        )
                        legacy_sessions = legacy.get("sessions")
                        if isinstance(legacy_sessions, dict):
                            for session, row in legacy_sessions.items():
                                if (
                                    session not in data["sessions"]
                                    and isinstance(row, dict)
                                ):
                                    data["sessions"][session] = dict(row)
                                    changed = True
                    except Exception:
                        # A broken legacy mirror is not authoritative.
                        pass

                if changed:
                    self._save_unlocked(data)
                else:
                    self._write_legacy_mirror_unlocked(data)

    def _audit(self, event: str, **fields: Any) -> None:
        safe = {
            "time": time.time(),
            "event": event,
            "device_id": self.device_id or None,
            **fields,
        }
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        with self.audit_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(safe, ensure_ascii=False) + "\n")

    def _local_device_row(self, now_value: float | None = None) -> dict[str, Any]:
        now_value = time.time() if now_value is None else now_value
        return {
            "device_id": self.device_id or None,
            "device_name": self.device_name or None,
            "device_alias": self.device_alias or None,
            "short_device_id": self.device_id[:8] if self.device_id else None,
            "fingerprint": self.fingerprint or None,
            "short_fingerprint": (
                self.fingerprint[:12].upper() if self.fingerprint else None
            ),
            "installation_id": self.installation_id or None,
            "trusted": self.trust_state == "trusted",
            "trust_state": self.trust_state,
            "last_seen": now_value,
            "router_id": self.router_id or None,
            "route_url": self.route_url or None,
        }

    def _public_device(
        self,
        row: dict[str, Any],
        *,
        now_value: float | None = None,
    ) -> dict[str, Any]:
        now_value = time.time() if now_value is None else now_value
        last_seen = float(row.get("last_seen") or 0)
        trusted = bool(row.get("trusted"))
        online = (
            trusted
            and last_seen > 0
            and (now_value - last_seen) <= self.device_ttl_seconds
        )
        return {
            "device_id": row.get("device_id"),
            "device_name": row.get("device_name"),
            "device_alias": row.get("device_alias"),
            "short_device_id": row.get("short_device_id"),
            "fingerprint": row.get("fingerprint"),
            "short_fingerprint": row.get("short_fingerprint"),
            "installation_id": row.get("installation_id"),
            "trusted": trusted,
            "trust_state": row.get("trust_state")
            or ("trusted" if trusted else "unverified"),
            "online": online,
            "last_seen": last_seen or None,
            "router_id": row.get("router_id"),
            "route_available": bool(row.get("route_url")),
        }

    def _register_local_unlocked(
        self,
        data: dict[str, Any],
        now_value: float | None = None,
    ) -> bool:
        if not self.device_id:
            return False
        now_value = time.time() if now_value is None else now_value
        row = self._local_device_row(now_value)
        previous = data["devices"].get(self.device_id)
        if isinstance(previous, dict):
            # Preserve fields introduced by a future compatible control-plane
            # implementation while refreshing authoritative local metadata.
            merged = dict(previous)
            merged.update(row)
            row = merged
        data["devices"][self.device_id] = row
        return previous != row

    def heartbeat(self) -> dict[str, Any]:
        if not self.device_id:
            return {"ok": False, "device_id": None}
        with self._lock:
            with self._file_guard():
                data = self._load_unlocked()
                self._register_local_unlocked(data)
                self._save_unlocked(data)
                row = dict(data["devices"][self.device_id])
        return {
            "ok": True,
            "device": self._public_device(row),
            "registry_scope": self.registry_scope,
            "authoritative": True,
        }

    def local_device(self) -> dict[str, Any]:
        self.heartbeat()
        with self._lock:
            data = self._load_unlocked()
            row = data["devices"].get(self.device_id)
        if not isinstance(row, dict):
            return self._public_device(self._local_device_row())
        return self._public_device(row)

    def status(self, session_id: str = "") -> dict[str, Any]:
        binding = self.get(session_id) if session_id else None
        return {
            "ok": True,
            "schema_version": self.SCHEMA_VERSION,
            "enforcement_mode": self.enforcement_mode,
            "device": self.local_device(),
            "session_binding": binding,
            "binding_store": str(self.path),
            "legacy_binding_mirror": str(self.legacy_path),
            "audit_log": str(self.audit_path),
            "registry_scope": self.registry_scope,
            "authority_model": "single-device-local-authority",
            "shared_authority_configured": False,
            "legacy_shared_control_plane_ignored": self.legacy_shared_control_plane_ignored,
            "device_ttl_seconds": self.device_ttl_seconds,
            "no_silent_fallback": True,
            "successful_execution_invariant": "executed_on == target_device_id",
        }

    def list_devices(self) -> dict[str, Any]:
        # Local Zagent exposes only its own device. Global inventory belongs to
        # Zagent Cloud and must never be inferred from this local registry.
        device = self.local_device()
        devices = [device] if device.get("device_id") else []
        return {
            "devices": devices,
            "count": len(devices),
            "registry_scope": self.registry_scope,
            "authoritative": True,
            "inventory_authoritative": True,
            "inventory_scope": "local-device-only",
            "requires_operator_prompt_when_unbound": True,
            "operator_prompt": "Siapa yang menggunakan plugin sekarang?",
            "multi_device_control_plane_registry": "not-in-local-zagent",
            "cross_device_routing": False,
            "zagent_cloud_required_for_multi_device": True,
        }

    def rename_alias(self, device_alias: Any) -> dict[str, Any]:
        value = str(device_alias or "").strip()
        if not value or len(value) > 80 or any(ord(ch) < 32 for ch in value):
            raise IdentityRoutingError(
                ERROR_DEVICE_NOT_TRUSTED,
                "device_alias must be 1-80 printable characters.",
            )
        previous = self.device_alias
        self.device_alias = value
        self.heartbeat()
        self._audit(
            "device_alias_changed",
            previous_device_alias=previous,
            device_alias=value,
        )
        return self.status()

    def _normalize_session(self, session_id: Any) -> str:
        value = str(session_id or "").strip()
        if not value:
            raise IdentityRoutingError(
                ERROR_SESSION_IDENTITY_REQUIRED,
                "A durable session_id is required.",
            )
        if len(value) > 256:
            raise IdentityRoutingError(
                ERROR_SESSION_IDENTITY_REQUIRED,
                "session_id is too long.",
            )
        return value

    def _normalize_operator(self, operator_alias: Any) -> str:
        value = str(operator_alias or "").strip()
        if not value:
            raise IdentityRoutingError(
                ERROR_SESSION_IDENTITY_REQUIRED,
                "operator_alias is required before binding a session.",
            )
        if len(value) > 120 or any(ord(ch) < 32 for ch in value):
            raise IdentityRoutingError(
                ERROR_SESSION_IDENTITY_REQUIRED,
                "operator_alias is invalid.",
            )
        return value

    @staticmethod
    def _normalized_alias(value: Any) -> str:
        return str(value or "").strip().casefold()

    def _validate_operator_device_alias(
        self,
        operator_alias: Any,
        device_alias: Any = None,
    ) -> str:
        operator = self._normalize_operator(operator_alias)
        bound_alias = str(device_alias or self.device_alias or "").strip()
        if not bound_alias:
            raise IdentityRoutingError(
                ERROR_DEVICE_NOT_TRUSTED,
                "Local device_alias is unavailable; operator/device binding cannot be verified.",
            )
        if self._normalized_alias(operator) != self._normalized_alias(bound_alias):
            self._audit(
                "operator_device_alias_mismatch_rejected",
                operator_alias=operator,
                device_alias=bound_alias,
            )
            raise IdentityRoutingError(
                ERROR_OPERATOR_DEVICE_ALIAS_MISMATCH,
                f"operator_alias '{operator}' must match local device_alias '{bound_alias}'.",
            )
        return operator

    def _normalize_target(self, target_device_id: Any) -> str:
        target = str(target_device_id or "").strip().lower()
        if not target:
            raise IdentityRoutingError(
                ERROR_SESSION_IDENTITY_REQUIRED,
                "target_device_id is required.",
            )
        return target

    def _require_target_unlocked(
        self,
        data: dict[str, Any],
        target_device_id: Any,
        *,
        now_value: float | None = None,
        require_online: bool = True,
    ) -> tuple[str, dict[str, Any]]:
        target = self._normalize_target(target_device_id)
        if target != self.device_id:
            raise IdentityRoutingError(
                ERROR_IDENTITY_MISMATCH,
                "Local Zagent is a single-device authority; remote target binding/routing belongs to Zagent Cloud.",
            )
        now_value = time.time() if now_value is None else now_value
        row = data["devices"].get(target)
        if not isinstance(row, dict):
            raise IdentityRoutingError(
                ERROR_DEVICE_NOT_TRUSTED,
                f"target_device_id '{target}' is not enrolled in the authoritative registry.",
            )
        if not bool(row.get("trusted")):
            raise IdentityRoutingError(
                ERROR_DEVICE_NOT_TRUSTED,
                f"target_device_id '{target}' is not trusted.",
            )
        last_seen = float(row.get("last_seen") or 0)
        online = (
            last_seen > 0
            and (now_value - last_seen) <= self.device_ttl_seconds
        )
        if require_online and not online:
            raise IdentityRoutingError(
                ERROR_TARGET_DEVICE_OFFLINE,
                f"target_device_id '{target}' is trusted but offline or stale.",
            )
        return target, dict(row)

    def _validate_registry_target(
        self,
        target_device_id: Any,
        *,
        require_online: bool = True,
    ) -> str:
        self.heartbeat()
        with self._lock:
            data = self._load_unlocked()
            target, _ = self._require_target_unlocked(
                data,
                target_device_id,
                require_online=require_online,
            )
            return target

    def _validate_local_target(self, target_device_id: Any) -> str:
        target = self._normalize_target(target_device_id)
        if not self.device_id:
            raise IdentityRoutingError(
                ERROR_DEVICE_NOT_TRUSTED,
                "This Zagent router has no local device identity.",
            )
        if self.trust_state != "trusted":
            raise IdentityRoutingError(
                ERROR_DEVICE_NOT_TRUSTED,
                "Local device identity has not completed trusted enrollment.",
            )
        if target != self.device_id:
            self._audit(
                "local_target_mismatch_rejected",
                target_device_id=target,
                executed_on=self.device_id,
            )
            raise IdentityRoutingError(
                ERROR_IDENTITY_MISMATCH,
                f"target_device_id '{target}' does not match this Zagent device '{self.device_id}'.",
            )
        return target

    def verify_target(self, target_device_id: Any) -> dict[str, Any]:
        target = self._validate_local_target(target_device_id)
        public = self.local_device()
        return {
            "ok": True,
            "target_device_id": target,
            "trusted": True,
            "online": bool(public.get("online")),
            "local_match": True,
            "device": public,
            "registry_scope": self.registry_scope,
            "authority_model": "single-device-local-authority",
        }

    def route_for_target(self, target_device_id: Any) -> dict[str, Any]:
        target = self._validate_local_target(target_device_id)
        return {
            "target_device_id": target,
            "local": True,
            "route_url": "",
            "router_id": self.router_id or "",
            "device": self.local_device(),
            "cross_device_routing": False,
            "zagent_cloud_required_for_remote": True,
        }

    def get(self, session_id: Any) -> dict[str, Any] | None:
        session = self._normalize_session(session_id)
        with self._lock:
            data = self._load_unlocked()
            row = data["sessions"].get(session)
            if not isinstance(row, dict):
                return None
            return dict(row)

    def list_bindings(self) -> dict[str, Any]:
        with self._lock:
            data = self._load_unlocked()
            rows = [
                dict(value)
                for value in data["sessions"].values()
                if isinstance(value, dict)
            ]
        rows.sort(
            key=lambda item: float(item.get("updated_at") or 0),
            reverse=True,
        )
        return {
            "bindings": rows,
            "count": len(rows),
            "registry_scope": self.registry_scope,
            "authoritative": True,
        }

    def bind(
        self,
        session_id: Any,
        operator_alias: Any,
        target_device_id: Any,
    ) -> dict[str, Any]:
        session = self._normalize_session(session_id)
        operator = self._normalize_operator(operator_alias)
        now_value = time.time()

        with self._lock:
            with self._file_guard():
                data = self._load_unlocked()
                self._register_local_unlocked(data, now_value)
                target, target_row = self._require_target_unlocked(
                    data,
                    target_device_id,
                    now_value=now_value,
                    require_online=True,
                )
                operator = self._validate_operator_device_alias(
                    operator,
                    target_row.get("device_alias") or self.device_alias,
                )

                existing = data["sessions"].get(session)
                if isinstance(existing, dict):
                    same_target = (
                        str(
                            existing.get("target_device_id") or ""
                        ).lower()
                        == target
                    )
                    same_operator = (
                        self._normalized_alias(existing.get("operator_alias"))
                        == self._normalized_alias(operator)
                    )
                    if same_target and same_operator:
                        return dict(existing)
                    raise IdentityRoutingError(
                        ERROR_SESSION_BINDING_CONFLICT,
                        "Session is already bound. Use identity_session_switch or unbind explicitly.",
                    )

                row = {
                    "session_id": session,
                    "operator_alias": operator,
                    "target_device_id": target,
                    "device_alias": target_row.get("device_alias"),
                    "device_fingerprint": target_row.get(
                        "short_fingerprint"
                    )
                    or (
                        str(target_row.get("fingerprint") or "")[:12].upper()
                    ),
                    "revision": 1,
                    "bound_at": now_value,
                    "updated_at": now_value,
                    "binding_state": "bound",
                    "authority": "local-device",
                }
                data["sessions"][session] = row
                self._save_unlocked(data)

        self._audit(
            "session_bound",
            session_id=session,
            operator_alias=operator,
            target_device_id=target,
            revision=1,
            registry_scope=self.registry_scope,
        )
        return dict(row)

    def unbind(self, session_id: Any) -> dict[str, Any]:
        session = self._normalize_session(session_id)
        with self._lock:
            with self._file_guard():
                data = self._load_unlocked()
                existing = data["sessions"].pop(session, None)
                if existing is None:
                    return {
                        "ok": True,
                        "session_id": session,
                        "unbound": False,
                        "already_unbound": True,
                    }
                self._save_unlocked(data)

        self._audit(
            "session_unbound",
            session_id=session,
            operator_alias=str(existing.get("operator_alias") or ""),
            target_device_id=str(
                existing.get("target_device_id") or ""
            ),
            revision=int(existing.get("revision") or 1),
        )
        return {
            "ok": True,
            "session_id": session,
            "unbound": True,
            "previous_binding": dict(existing),
        }

    def switch(
        self,
        session_id: Any,
        target_device_id: Any,
        *,
        confirmation: bool,
    ) -> dict[str, Any]:
        session = self._normalize_session(session_id)
        if confirmation is not True:
            raise IdentityRoutingError(
                ERROR_DEVICE_SWITCH_CONFIRMATION_REQUIRED,
                "Device switch requires explicit confirmation.",
            )
        now_value = time.time()

        with self._lock:
            with self._file_guard():
                data = self._load_unlocked()
                self._register_local_unlocked(data, now_value)
                target, target_row = self._require_target_unlocked(
                    data,
                    target_device_id,
                    now_value=now_value,
                    require_online=True,
                )

                existing = data["sessions"].get(session)
                if not isinstance(existing, dict):
                    raise IdentityRoutingError(
                        ERROR_SESSION_IDENTITY_REQUIRED,
                        "Session is not currently bound.",
                    )

                old_target = str(
                    existing.get("target_device_id") or ""
                ).lower()
                self._validate_operator_device_alias(
                    existing.get("operator_alias"),
                    target_row.get("device_alias") or self.device_alias,
                )
                if old_target == target:
                    return dict(existing)

                updated = dict(existing)
                updated["target_device_id"] = target
                updated["device_alias"] = target_row.get("device_alias")
                updated["device_fingerprint"] = target_row.get(
                    "short_fingerprint"
                ) or (
                    str(target_row.get("fingerprint") or "")[:12].upper()
                )
                updated["revision"] = int(
                    existing.get("revision") or 1
                ) + 1
                updated["updated_at"] = now_value
                updated["binding_state"] = "bound"
                updated["authority"] = "local-device"
                data["sessions"][session] = updated
                self._save_unlocked(data)

        self._audit(
            "session_switched",
            session_id=session,
            operator_alias=str(updated.get("operator_alias") or ""),
            previous_target_device_id=old_target,
            target_device_id=target,
            revision=updated["revision"],
        )
        return dict(updated)

    def cleanup(self, max_age_seconds: int) -> dict[str, Any]:
        age = max(3600, int(max_age_seconds))
        cutoff = time.time() - age
        removed: list[str] = []

        with self._lock:
            with self._file_guard():
                data = self._load_unlocked()
                for session, row in list(data["sessions"].items()):
                    if not isinstance(row, dict):
                        removed.append(session)
                        data["sessions"].pop(session, None)
                        continue
                    updated = float(
                        row.get("updated_at")
                        or row.get("bound_at")
                        or 0
                    )
                    if updated and updated < cutoff:
                        removed.append(session)
                        data["sessions"].pop(session, None)
                if removed:
                    self._save_unlocked(data)

        for session in removed:
            self._audit(
                "session_binding_expired",
                session_id=session,
            )
        return {"removed": removed, "count": len(removed)}

    def resolve_target(
        self,
        session_id: Any,
        supplied_target_device_id: Any = None,
    ) -> dict[str, Any]:
        session = str(session_id or "").strip()
        supplied = str(
            supplied_target_device_id or ""
        ).strip().lower()

        binding = None
        if session:
            try:
                binding = self.get(session)
            except IdentityRoutingError:
                binding = None

        if binding is not None:
            target = str(
                binding.get("target_device_id") or ""
            ).lower()
            self._validate_local_target(target)
            self._validate_operator_device_alias(
                binding.get("operator_alias"),
                binding.get("device_alias") or self.device_alias,
            )
            if supplied and supplied != target:
                self._audit(
                    "target_mismatch_rejected",
                    session_id=session,
                    supplied_target_device_id=supplied,
                    bound_target_device_id=target,
                )
                raise IdentityRoutingError(
                    ERROR_IDENTITY_MISMATCH,
                    f"supplied target_device_id '{supplied}' conflicts with the session binding '{target}'.",
                )
            return {
                "target_device_id": target,
                "binding": binding,
                "binding_source": "local-device-session",
                "implicit": False,
                "local_target": target == self.device_id,
            }

        if self.enforcement_mode == "mandatory":
            raise IdentityRoutingError(
                ERROR_SESSION_IDENTITY_REQUIRED,
                "Session has no active device binding. Call identity_session_bind first.",
            )

        if supplied:
            target = self._validate_local_target(supplied)
            return {
                "target_device_id": target,
                "binding": None,
                "binding_source": "explicit-compatibility",
                "implicit": False,
                "local_target": target == self.device_id,
            }

        if not self.device_id:
            raise IdentityRoutingError(
                ERROR_TARGET_DEVICE_OFFLINE,
                "No local target device is available.",
            )

        self._validate_local_target(self.device_id)
        return {
            "target_device_id": self.device_id,
            "binding": None,
            "binding_source": "legacy-implicit",
            "implicit": True,
            "local_target": True,
        }

    def create_challenge(self) -> dict[str, Any]:
        if not self.device_id or not self.device_binding:
            raise IdentityRoutingError(
                ERROR_DEVICE_NOT_TRUSTED,
                "Protected device binding is unavailable.",
            )
        challenge_id = "chl_" + secrets.token_hex(16)
        nonce = secrets.token_hex(32)
        expires_at = time.time() + 60
        with self._lock:
            self._challenges[challenge_id] = {
                "nonce": nonce,
                "expires_at": expires_at,
                "used": False,
            }
        return {
            "challenge_id": challenge_id,
            "device_id": self.device_id,
            "nonce": nonce,
            "expires_at": expires_at,
            "algorithm": "HMAC-SHA256",
        }

    def create_local_proof(self, challenge_id: Any) -> dict[str, Any]:
        challenge = self._challenge(challenge_id)
        proof = self._proof_for_nonce(str(challenge["nonce"]))
        return {
            "challenge_id": str(challenge_id),
            "device_id": self.device_id,
            "proof": proof,
            "algorithm": "HMAC-SHA256",
        }

    def verify_challenge(
        self,
        challenge_id: Any,
        proof: Any,
    ) -> dict[str, Any]:
        challenge_key = str(challenge_id or "").strip()
        challenge = self._challenge(challenge_key)
        if bool(challenge.get("used")):
            raise IdentityRoutingError(
                ERROR_IDENTITY_PROOF_FAILED,
                "Challenge has already been consumed.",
            )
        expected = self._proof_for_nonce(str(challenge["nonce"]))
        supplied = str(proof or "").strip().lower()
        ok = hmac.compare_digest(expected, supplied)
        with self._lock:
            challenge["used"] = True
        if not ok:
            self._audit(
                "identity_proof_failed",
                challenge_id=challenge_key,
            )
            raise IdentityRoutingError(
                ERROR_IDENTITY_PROOF_FAILED,
                "Challenge response proof did not verify.",
            )
        self._audit(
            "identity_proof_verified",
            challenge_id=challenge_key,
        )
        return {
            "ok": True,
            "device_id": self.device_id,
            "trusted": True,
            "trust_state": "trusted",
            "challenge_id": challenge_key,
        }

    def _challenge(self, challenge_id: Any) -> dict[str, Any]:
        key = str(challenge_id or "").strip()
        if not key:
            raise IdentityRoutingError(
                ERROR_IDENTITY_PROOF_FAILED,
                "challenge_id is required.",
            )
        with self._lock:
            challenge = self._challenges.get(key)
            if not isinstance(challenge, dict):
                raise IdentityRoutingError(
                    ERROR_IDENTITY_PROOF_FAILED,
                    "Challenge is unknown.",
                )
            if float(challenge.get("expires_at") or 0) < time.time():
                self._challenges.pop(key, None)
                raise IdentityRoutingError(
                    ERROR_IDENTITY_PROOF_FAILED,
                    "Challenge has expired.",
                )
            return challenge

    def _proof_for_nonce(self, nonce: str) -> str:
        try:
            key = bytes.fromhex(self.device_binding)
        except ValueError as exc:
            raise IdentityRoutingError(
                ERROR_IDENTITY_PROOF_FAILED,
                "Protected device binding is malformed.",
            ) from exc
        payload = (
            "zagent-identity-challenge:"
            + self.device_id
            + ":"
            + nonce
        ).encode("utf-8")
        return hmac.new(key, payload, hashlib.sha256).hexdigest()
