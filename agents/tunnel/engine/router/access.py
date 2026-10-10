from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any


STATE_PENDING = "PENDING"
STATE_APPROVED = "APPROVED"
STATE_DENIED = "DENIED"
STATE_REVOKED = "REVOKED"


class PluginAccessStore:
    """Local durable session approval guard shared by Production/DEV on one Windows user."""

    def __init__(
        self,
        root: str | Path,
        *,
        audience: str,
        display_name: str,
        device_id: str,
        device_alias: str,
        mode: str = "manager-approval",
        enforcement: str = "mandatory",
        fallback_plugin: str = "",
        parent_audience: str = "zagent-production",
    ) -> None:
        self.root = Path(root).expanduser().resolve()
        self.audience = (audience or "zagent-production").strip().lower()
        self.display_name = (display_name or self.audience).strip()
        self.device_id = (device_id or "").strip().lower()
        self.device_alias = (device_alias or "").strip()
        self.mode = (mode or "manager-approval").strip().lower()
        self.enforcement = (enforcement or "mandatory").strip().lower()
        self.fallback_plugin = (fallback_plugin or "").strip()
        self.parent_audience = (parent_audience or "zagent-production").strip().lower()
        self._gate = threading.RLock()

        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "sessions" / self.audience).mkdir(parents=True, exist_ok=True)
        (self.root / "requests" / self.audience).mkdir(parents=True, exist_ok=True)
        (self.root / "dev-grants").mkdir(parents=True, exist_ok=True)
        (self.root / "dev-links").mkdir(parents=True, exist_ok=True)
        (self.root / "session-locks").mkdir(parents=True, exist_ok=True)

    @property
    def enabled(self) -> bool:
        return self.enforcement not in {"disabled", "off", "false", "0"}

    @property
    def requires_production_grant(self) -> bool:
        return self.mode == "production-grant" or self.audience == "zagent-dev"

    def policy(self) -> dict[str, Any]:
        return {
            "audience": self.audience,
            "display_name": self.display_name,
            "mode": self.mode,
            "enforcement": self.enforcement,
            "enabled": self.enabled,
            "fallback_plugin": self.fallback_plugin or None,
            "parent_audience": self.parent_audience if self.requires_production_grant else None,
            "device_id": self.device_id or None,
            "device_alias": self.device_alias or None,
        }

    @staticmethod
    def _session_key(session_id: str) -> str:
        raw = (session_id or "").strip()
        if not raw:
            return ""
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _session_path(self, session_id: str) -> Path:
        key = self._session_key(session_id)
        if not key:
            raise ValueError("session_id is required")
        return self.root / "sessions" / self.audience / f"{key}.json"

    def _request_path(self, request_id: str) -> Path:
        rid = (request_id or "").strip()
        if not rid:
            raise ValueError("request_id is required")
        safe = "".join(ch for ch in rid if ch.isalnum() or ch in "-_")
        if safe != rid:
            raise ValueError("request_id contains invalid characters")
        return self.root / "requests" / self.audience / f"{safe}.json"

    def _grant_path(self, session_id: str) -> Path:
        key = self._session_key(session_id)
        if not key:
            raise ValueError("session_id is required")
        return self.root / "dev-grants" / f"{key}.json"

    def _dev_link_path(self, child_session_id: str) -> Path:
        key = self._session_key(child_session_id)
        if not key:
            raise ValueError("child_session_id is required")
        return self.root / "dev-links" / f"{key}.json"

    def _audience_lock_path(self, session_id: str) -> Path:
        key = self._session_key(session_id)
        if not key:
            raise ValueError("session_id is required")
        return self.root / "session-locks" / f"{key}.json"

    def session_audience_lock(self, session_id: str) -> dict[str, Any] | None:
        try:
            record = self._load(self._audience_lock_path(session_id))
        except ValueError:
            return None
        if not record:
            return None
        if str(record.get("session_id") or "") != str(session_id or "").strip():
            return None
        return record

    def lock_session_to_current_audience(
        self,
        session_id: str,
        *,
        operator_alias: str,
        target_device_id: str,
        device_alias: str,
    ) -> dict[str, Any]:
        record = {
            "schema_version": 1,
            "session_id": str(session_id or "").strip(),
            "plugin_audience": self.audience,
            "operator_alias": str(operator_alias or "").strip(),
            "target_device_id": str(target_device_id or "").strip().lower(),
            "device_alias": str(device_alias or "").strip(),
            "state": "BOUND",
            "updated_at": time.time(),
        }
        self._write(self._audience_lock_path(session_id), record)
        return record

    def release_session_audience_lock(self, session_id: str) -> dict[str, Any]:
        path = self._audience_lock_path(session_id)
        record = self._load(path)
        if record and str(record.get("plugin_audience") or "").lower() == self.audience:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            return {"ok": True, "released": True, "session_id": str(session_id or "").strip()}
        return {"ok": True, "released": False, "session_id": str(session_id or "").strip()}

    def _session_locked_to_other_plugin(self, session_id: str) -> dict[str, Any] | None:
        # Zagent Dev is a subordinate test plane and intentionally inherits the
        # approved Production session via a dedicated grant. The audience lock
        # is for preventing Production <-> Try Plugin crossover only.
        if self.audience == "zagent-dev":
            return None
        lock = self.session_audience_lock(session_id)
        if not lock:
            return None
        locked_audience = str(lock.get("plugin_audience") or "").strip().lower()
        if (
            self.audience in {"zagent-production", "tryplugin"}
            and locked_audience in {"zagent-production", "tryplugin"}
            and locked_audience != self.audience
        ):
            return lock
        return None

    @staticmethod
    def _load(path: Path) -> dict[str, Any] | None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else None
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return None

    @staticmethod
    def _write(path: Path, payload: dict[str, Any]) -> None:
        serialized = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        last_error: FileNotFoundError | None = None

        for attempt in range(3):
            path.parent.mkdir(parents=True, exist_ok=True)
            temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
            try:
                temp.write_text(serialized, encoding="utf-8")
                # A test/cleanup process may remove an ephemeral parent between
                # mkdir and replace. Recreate it before the atomic rename.
                path.parent.mkdir(parents=True, exist_ok=True)
                os.replace(temp, path)
                return
            except FileNotFoundError as exc:
                last_error = exc
                try:
                    temp.unlink(missing_ok=True)
                except OSError:
                    pass
                if attempt < 2:
                    time.sleep(0.01 * (attempt + 1))
                    continue
                raise

        if last_error is not None:
            raise last_error

    def _session_record(self, session_id: str) -> dict[str, Any] | None:
        try:
            record = self._load(self._session_path(session_id))
        except ValueError:
            return None
        if not record:
            return None
        if str(record.get("session_id") or "") != str(session_id or "").strip():
            return None
        if str(record.get("plugin_audience") or "").lower() != self.audience:
            return None
        return record

    def _dev_grant(self, session_id: str) -> dict[str, Any] | None:
        try:
            record = self._load(self._grant_path(session_id))
        except ValueError:
            return None
        if not record:
            return None
        if str(record.get("session_id") or "") != str(session_id or "").strip():
            return None
        if str(record.get("target_audience") or "").lower() != "zagent-dev":
            return None
        if str(record.get("state") or "").upper() != STATE_APPROVED:
            return None
        return record

    def _parent_production_record(self, session_id: str) -> dict[str, Any] | None:
        session = str(session_id or "").strip()
        key = self._session_key(session)
        if not key:
            return None
        record = self._load(
            self.root / "sessions" / self.parent_audience / f"{key}.json"
        )
        if not record:
            return None
        if str(record.get("session_id") or "") != session:
            return None
        if str(record.get("plugin_audience") or "").strip().lower() != self.parent_audience:
            return None
        if str(record.get("state") or "").upper() != STATE_APPROVED:
            return None
        return record

    def _trusted_dev_grant(self, parent_session_id: str, grant: dict[str, Any]) -> bool:
        parent = str(parent_session_id or "").strip()
        if not parent or not grant:
            return False
        if str(grant.get("session_id") or "").strip() != parent:
            return False
        if str(grant.get("parent_audience") or "").strip().lower() != self.parent_audience:
            return False
        if str(grant.get("target_audience") or "").strip().lower() != "zagent-dev":
            return False
        if str(grant.get("state") or "").upper() != STATE_APPROVED:
            return False

        parent_record = self._parent_production_record(parent)
        if parent_record is None:
            return False

        grant_operator = str(grant.get("operator_alias") or "").strip()
        parent_operator = str(parent_record.get("operator_alias") or "").strip()
        if grant_operator and parent_operator and grant_operator.casefold() != parent_operator.casefold():
            return False

        grant_issuer = str(grant.get("issuer_device_id") or "").strip().lower()
        parent_device = str(parent_record.get("device_id") or "").strip().lower()
        if grant_issuer and parent_device and grant_issuer != parent_device:
            return False

        try:
            grant_created = float(grant.get("created_at") or 0.0)
            parent_resolved = float(parent_record.get("resolved_at") or 0.0)
        except (TypeError, ValueError):
            return False
        if parent_resolved and (not grant_created or grant_created < parent_resolved):
            return False

        return True

    def _dev_link(self, child_session_id: str) -> dict[str, Any] | None:
        try:
            record = self._load(self._dev_link_path(child_session_id))
        except ValueError:
            return None
        if not record:
            return None
        if str(record.get("child_session_id") or "") != str(child_session_id or "").strip():
            return None
        if str(record.get("target_audience") or "").lower() != "zagent-dev":
            return None
        if str(record.get("state") or "").upper() != STATE_APPROVED:
            return None
        return record

    def _resolved_dev_grant(self, child_session_id: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        # Backward compatibility: older same-session grants remain valid, but
        # only while the parent Production approval chain is still trusted.
        direct = self._dev_grant(child_session_id)
        if direct is not None:
            if self._trusted_dev_grant(child_session_id, direct):
                return direct, None
            return None, None

        link = self._dev_link(child_session_id)
        if link is None:
            return None, None

        parent_session_id = str(link.get("parent_session_id") or "").strip()
        grant = self._dev_grant(parent_session_id)
        if grant is None:
            return None, link

        if str(link.get("grant_id") or "") != str(grant.get("grant_id") or ""):
            return None, link

        if not self._trusted_dev_grant(parent_session_id, grant):
            return None, link

        return grant, link

    def status(self, session_id: str) -> dict[str, Any]:
        session = (session_id or "").strip()
        foreign_lock = self._session_locked_to_other_plugin(session)
        if foreign_lock:
            locked_audience = str(foreign_lock.get("plugin_audience") or "").strip().lower()
            return {
                "ok": False,
                "session_id": session,
                "allowed": False,
                "state": "SESSION_PLUGIN_LOCKED",
                "code": "PLUGIN_SESSION_LOCKED",
                "message": f"Session is bound to plugin audience '{locked_audience}' and cannot activate '{self.audience}'.",
                "locked_plugin_audience": locked_audience,
                "requires_operator_prompt": False,
                "requires_manager_approval": False,
                **self.policy(),
            }
        if not self.enabled:
            return {
                "ok": True,
                "session_id": session,
                "allowed": True,
                "state": STATE_APPROVED,
                "approval_source": "access-guard-disabled",
                **self.policy(),
            }

        if self.requires_production_grant:
            grant, link = self._resolved_dev_grant(session)
            allowed = grant is not None
            return {
                "ok": True,
                "session_id": session,
                "allowed": allowed,
                "state": STATE_APPROVED if allowed else "PRODUCTION_GRANT_REQUIRED",
                "requires_operator_prompt": False,
                "requires_manager_approval": False,
                "requires_production_grant": not allowed,
                "operator_alias": (grant or {}).get("operator_alias"),
                "grant_id": (grant or {}).get("grant_id"),
                "parent_session_id": (
                    (link or {}).get("parent_session_id")
                    or ((grant or {}).get("session_id") if grant else None)
                ),
                "parent_audience": self.parent_audience,
                **self.policy(),
            }

        record = self._session_record(session)
        state = str((record or {}).get("state") or "UNREQUESTED").upper()
        return {
            "ok": True,
            "session_id": session,
            "allowed": state == STATE_APPROVED,
            "state": state,
            "operator_alias": (record or {}).get("operator_alias"),
            "request_id": (record or {}).get("request_id"),
            "requires_operator_prompt": state in {"UNREQUESTED", STATE_REVOKED},
            "requires_manager_approval": state == STATE_PENDING,
            "recommended_plugin": self.fallback_plugin or None,
            **self.policy(),
        }

    def bootstrap(self, session_id: str) -> dict[str, Any]:
        result = self.status(session_id)
        if self.requires_production_grant:
            result["operator_prompt"] = None
            result["bootstrap_policy"] = (
                "production-grant-valid"
                if result.get("allowed")
                else "dev-requires-production-grant"
            )
            return result

        state = str(result.get("state") or "")
        if state in {"UNREQUESTED", STATE_REVOKED}:
            result["operator_prompt"] = "Siapa yang menggunakan plugin sekarang?"
            result["bootstrap_policy"] = "ask-operator-then-request-manager-approval"
        elif state == STATE_PENDING:
            result["operator_prompt"] = None
            result["bootstrap_policy"] = "wait-for-manager-approval"
        elif state == STATE_DENIED:
            result["operator_prompt"] = None
            result["bootstrap_policy"] = "denied-fail-closed"
        else:
            result["operator_prompt"] = None
            result["bootstrap_policy"] = "approved-skip-prompt"
        return result

    def request_access(self, session_id: str, operator_alias: str) -> dict[str, Any]:
        if self.requires_production_grant:
            return {
                "ok": False,
                "code": "DEV_ACCESS_REQUIRES_PRODUCTION",
                "message": "Zagent Dev Plugin can only be activated by an approved Zagent Production session.",
                **self.status(session_id),
            }

        session = (session_id or "").strip()
        operator = (operator_alias or "").strip()
        if not session:
            raise ValueError("session_id is required")
        if not operator:
            raise ValueError("operator_alias is required")

        foreign_lock = self._session_locked_to_other_plugin(session)
        if foreign_lock:
            return {
                **self.status(session),
                "created": False,
                "pending": False,
            }

        if self.device_alias and operator.casefold() != self.device_alias.casefold():
            return {
                "ok": False,
                "created": False,
                "pending": False,
                "allowed": False,
                "state": "OPERATOR_DEVICE_ALIAS_MISMATCH",
                "code": "OPERATOR_DEVICE_ALIAS_MISMATCH",
                "message": f"operator_alias '{operator}' must match local device_alias '{self.device_alias}'.",
                "operator_alias": operator,
                "device_alias": self.device_alias,
                **self.policy(),
            }

        with self._gate:
            current = self._session_record(session)
            state = str((current or {}).get("state") or "").upper()

            if state == STATE_APPROVED:
                return {"ok": True, "created": False, **self.status(session)}

            if state == STATE_PENDING:
                return {
                    "ok": True,
                    "created": False,
                    "pending": True,
                    **self.status(session),
                }

            request_id = "PAC-" + uuid.uuid4().hex[:16].upper()
            now = time.time()
            record = {
                "schema_version": 1,
                "session_id": session,
                "plugin_audience": self.audience,
                "plugin_display_name": self.display_name,
                "operator_alias": operator,
                "device_id": self.device_id,
                "device_alias": self.device_alias,
                "state": STATE_PENDING,
                "request_id": request_id,
                "created_at": now,
                "updated_at": now,
                "resolved_at": None,
                "resolved_by": None,
                "reason": None,
                "recommended_plugin": self.fallback_plugin or None,
            }
            self._write(self._session_path(session), record)
            self._write(self._request_path(request_id), record)

        return {
            "ok": True,
            "created": True,
            "pending": True,
            **self.status(session),
        }

    def request_status(self, request_id: str) -> dict[str, Any]:
        record = self._load(self._request_path(request_id))
        if not record:
            return {
                "ok": False,
                "code": "PLUGIN_ACCESS_REQUEST_NOT_FOUND",
                "request_id": request_id,
                **self.policy(),
            }
        return {
            "ok": True,
            **record,
            "recommended_plugin": record.get("recommended_plugin") or self.fallback_plugin or None,
        }

    def wait_for_request(
        self,
        request_id: str,
        *,
        timeout_seconds: float = 60.0,
        poll_interval_seconds: float = 1.0,
    ) -> dict[str, Any]:
        timeout = max(0.1, min(float(timeout_seconds), 60.0))
        interval = max(0.1, min(float(poll_interval_seconds), 2.0))
        deadline = time.monotonic() + timeout
        transient_not_found_count = 0
        while True:
            status = self.request_status(request_id)
            if not status.get("ok"):
                # Atomic file replacement on Windows can expose a very short
                # FileNotFound window to another thread. Treat a few consecutive
                # misses as transient while the wait window is still active.
                if status.get("code") == "PLUGIN_ACCESS_REQUEST_NOT_FOUND":
                    transient_not_found_count += 1
                    remaining = deadline - time.monotonic()
                    if remaining > 0 and transient_not_found_count <= 3:
                        time.sleep(min(interval, remaining))
                        continue
                return status
            transient_not_found_count = 0
            state = str(status.get("state") or "").upper()
            if state in {STATE_APPROVED, STATE_DENIED, STATE_REVOKED}:
                return {
                    **status,
                    "wait_completed": True,
                    "timed_out": False,
                }
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return {
                    **status,
                    "ok": False,
                    "allowed": False,
                    "code": "APPROVAL_TIMEOUT",
                    "message": "Plugin access approval was not resolved within 60 seconds.",
                    "wait_completed": True,
                    "timed_out": True,
                }
            time.sleep(min(interval, remaining))

    def pending_requests(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        folder = self.root / "requests" / self.audience
        for path in folder.glob("PAC-*.json"):
            record = self._load(path)
            if not record:
                continue
            if str(record.get("state") or "").upper() != STATE_PENDING:
                continue
            rows.append(record)
        rows.sort(key=lambda row: float(row.get("created_at") or 0.0))
        return rows

    def resolve_request(
        self,
        request_id: str,
        action: str,
        *,
        resolved_by: str = "zagent_manager",
        reason: str = "",
    ) -> dict[str, Any]:
        decision = (action or "").strip().lower()
        if decision not in {"approve", "deny"}:
            raise ValueError("action must be approve or deny")

        with self._gate:
            request_path = self._request_path(request_id)
            record = self._load(request_path)
            if not record:
                return {
                    "ok": False,
                    "code": "PLUGIN_ACCESS_REQUEST_NOT_FOUND",
                    "request_id": request_id,
                }

            session = str(record.get("session_id") or "").strip()
            current_state = str(record.get("state") or "").upper()
            if current_state != STATE_PENDING:
                return {
                    "ok": current_state == STATE_APPROVED and decision == "approve",
                    "request_id": request_id,
                    "state": current_state,
                    "session_id": session,
                    "message": f"Plugin access request is already {current_state}.",
                }

            new_state = STATE_APPROVED if decision == "approve" else STATE_DENIED
            now = time.time()
            record.update(
                {
                    "state": new_state,
                    "updated_at": now,
                    "resolved_at": now,
                    "resolved_by": resolved_by,
                    "reason": reason or None,
                }
            )
            self._write(request_path, record)
            self._write(self._session_path(session), record)

            if new_state != STATE_APPROVED:
                grant_path = self._grant_path(session)
                if grant_path.exists():
                    grant = self._load(grant_path) or {}
                    grant.update(
                        {
                            "state": STATE_REVOKED,
                            "updated_at": now,
                            "revoked_reason": "Parent production access denied.",
                        }
                    )
                    self._write(grant_path, grant)

        return {
            "ok": True,
            "request_id": request_id,
            "session_id": session,
            "state": new_state,
            "allowed": new_state == STATE_APPROVED,
            "operator_alias": record.get("operator_alias"),
            "recommended_plugin": record.get("recommended_plugin") or self.fallback_plugin or None,
        }

    def revoke(self, session_id: str, reason: str = "") -> dict[str, Any]:
        session = (session_id or "").strip()
        with self._gate:
            record = self._session_record(session)
            if not record:
                return {
                    "ok": True,
                    "session_id": session,
                    "state": STATE_REVOKED,
                    "already_absent": True,
                }
            now = time.time()
            record.update(
                {
                    "state": STATE_REVOKED,
                    "updated_at": now,
                    "resolved_at": now,
                    "resolved_by": "remote-revoke",
                    "reason": reason or "Session access revoked.",
                }
            )
            self._write(self._session_path(session), record)

            request_id = str(record.get("request_id") or "")
            if request_id:
                try:
                    self._write(self._request_path(request_id), record)
                except ValueError:
                    pass

            grant_path = self._grant_path(session)
            if grant_path.exists():
                grant = self._load(grant_path) or {}
                grant.update(
                    {
                        "state": STATE_REVOKED,
                        "updated_at": now,
                        "revoked_reason": reason or "Parent production session revoked.",
                    }
                )
                self._write(grant_path, grant)

        return {
            "ok": True,
            "session_id": session,
            "state": STATE_REVOKED,
            "allowed": False,
        }

    def create_dev_grant(self, session_id: str) -> dict[str, Any]:
        if self.audience != self.parent_audience or self.requires_production_grant:
            return {
                "ok": False,
                "code": "DEV_ACCESS_REQUIRES_PRODUCTION",
                "message": "Only the approved Zagent Production audience can create a DEV grant.",
            }

        status = self.status(session_id)
        if not status.get("allowed"):
            return {
                **status,
                "ok": False,
                "code": "PLUGIN_ACCESS_REQUIRED",
                "message": "Production session must be approved before creating a DEV grant.",
            }

        operator = str(status.get("operator_alias") or "").strip()
        grant = {
            "schema_version": 1,
            "grant_id": "DEV-" + uuid.uuid4().hex[:16].upper(),
            "session_id": str(session_id).strip(),
            "parent_audience": self.audience,
            "target_audience": "zagent-dev",
            "operator_alias": operator,
            "issuer_device_id": self.device_id,
            "issuer_device_alias": self.device_alias,
            "state": STATE_APPROVED,
            "created_at": time.time(),
            "updated_at": time.time(),
        }
        self._write(self._grant_path(session_id), grant)
        return {"ok": True, "allowed": True, **grant}

    def bind_dev_child(
        self,
        child_session_id: str,
        parent_session_id: str,
        grant_id: str,
    ) -> dict[str, Any]:
        child = str(child_session_id or "").strip()
        parent = str(parent_session_id or "").strip()
        expected = str(grant_id or "").strip()
        grant = self._dev_grant(parent)
        if (
            not self.requires_production_grant
            or not child
            or not parent
            or not expected
            or grant is None
            or str(grant.get("grant_id") or "") != expected
        ):
            return {
                "ok": False,
                "allowed": False,
                "code": "DEV_PARENT_GRANT_INVALID",
            }

        if not self._trusted_dev_grant(parent, grant):
            return {
                "ok": False,
                "allowed": False,
                "code": "DEV_PARENT_TRUST_INVALID",
            }

        link = {
            "schema_version": 1,
            "child_session_id": child,
            "parent_session_id": parent,
            "parent_audience": self.parent_audience,
            "target_audience": "zagent-dev",
            "grant_id": expected,
            "operator_alias": grant.get("operator_alias"),
            "issuer_device_id": grant.get("issuer_device_id"),
            "state": STATE_APPROVED,
            "created_at": time.time(),
            "updated_at": time.time(),
        }
        self._write(self._dev_link_path(child), link)
        return {"ok": True, "allowed": True, **link}

    def dev_grant_status(self, session_id: str) -> dict[str, Any]:
        session = str(session_id or "").strip()
        grant, link = self._resolved_dev_grant(session)
        if not grant:
            return {
                "ok": False,
                "allowed": False,
                "code": "DEV_ACCESS_REQUIRES_PRODUCTION",
                "session_id": session,
                "target_audience": "zagent-dev",
                "parent_session_id": (link or {}).get("parent_session_id"),
            }
        return {
            "ok": True,
            "allowed": True,
            "session_id": session,
            "parent_session_id": (
                (link or {}).get("parent_session_id")
                or grant.get("session_id")
            ),
            **grant,
        }

    def authorization(self, session_id: str) -> dict[str, Any]:
        status = self.status(session_id)
        if status.get("allowed"):
            return status

        if self.requires_production_grant:
            return {
                **status,
                "code": "DEV_ACCESS_REQUIRES_PRODUCTION",
                "message": "Zagent Dev Plugin requires a grant from an approved Zagent Production session.",
            }

        state = str(status.get("state") or "")
        if state == STATE_PENDING:
            code = "PLUGIN_ACCESS_PENDING"
            message = "Plugin access is waiting for approval in Zagent Manager."
        elif state == STATE_DENIED:
            code = "PLUGIN_ACCESS_DENIED"
            message = "Plugin access was denied in Zagent Manager."
        elif state == STATE_REVOKED:
            code = "PLUGIN_ACCESS_REVOKED"
            message = "Plugin access for this chat session was revoked."
        else:
            code = "PLUGIN_ACCESS_REQUIRED"
            message = "Plugin access approval is required before using operational tools."

        return {
            **status,
            "code": code,
            "message": message,
        }
