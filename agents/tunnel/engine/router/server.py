from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
import uvicorn

from .config import load_tunnel_config
from .access import PluginAccessStore
from .identity import (
    ERROR_DEVICE_NOT_TRUSTED,
    ERROR_IDENTITY_MISMATCH,
    ERROR_OPERATOR_DEVICE_ALIAS_MISMATCH,
    ERROR_TARGET_DEVICE_OFFLINE,
    IdentityBindingStore,
    IdentityRoutingError,
)
from .orchestration import Orchestrator
from .registry import MCPRegistry
from .workflow_domain import WorkflowDomainJournal
from .workflow_runtime import WorkflowRuntimeService

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("zagent.tunnel.router")

registry = MCPRegistry()

_DEVICE_ID = os.environ.get("ZAGENT_DEVICE_ID", "").strip().lower()
_DEVICE_NAME = os.environ.get("ZAGENT_DEVICE_NAME", "").strip()
_DEVICE_ALIAS = os.environ.get("ZAGENT_DEVICE_ALIAS", "").strip()
_DEVICE_FINGERPRINT = os.environ.get("ZAGENT_DEVICE_FINGERPRINT", "").strip()
_INSTALLATION_ID = os.environ.get("ZAGENT_INSTALLATION_ID", "").strip()
_DEVICE_BINDING = os.environ.get("ZAGENT_DEVICE_BINDING", "").strip().lower()
_IDENTITY_ENFORCEMENT = os.environ.get(
    "ZAGENT_IDENTITY_ENFORCEMENT", "mandatory"
).strip().lower()

_PLUGIN_AUDIENCE = os.environ.get(
    "ZAGENT_PLUGIN_AUDIENCE", "zagent-production"
).strip().lower()
_PLUGIN_DISPLAY_NAME = os.environ.get(
    "ZAGENT_PLUGIN_DISPLAY_NAME", "Zagent"
).strip()
_PLUGIN_ACCESS_MODE = os.environ.get(
    "ZAGENT_PLUGIN_ACCESS_MODE",
    "production-grant" if _PLUGIN_AUDIENCE == "zagent-dev" else "manager-approval",
).strip().lower()
_PLUGIN_ACCESS_ENFORCEMENT = os.environ.get(
    "ZAGENT_PLUGIN_ACCESS_ENFORCEMENT", "disabled"
).strip().lower()
_PLUGIN_FALLBACK = os.environ.get(
    "ZAGENT_PLUGIN_FALLBACK",
    "",
).strip()
if _PLUGIN_AUDIENCE in {"zagent-production", "tryplugin"}:
    # Local Production/Try sessions are isolated. Legacy environment settings
    # must not resurrect cross-plugin fallback.
    _PLUGIN_FALLBACK = ""
_PLUGIN_PARENT_AUDIENCE = os.environ.get(
    "ZAGENT_PLUGIN_PARENT_AUDIENCE", "zagent-production"
).strip().lower()


def _with_device_provenance(result: Any, ctx: Any = None) -> Any:
    if not isinstance(result, dict):
        return result
    enriched = dict(result)
    meta = dict(enriched.get("_meta") or {})
    if _DEVICE_ID:
        meta["zagent.device_id"] = _DEVICE_ID
        meta["zagent.executed_on"] = _DEVICE_ID
    if ctx is not None:
        meta["zagent.target_device_id"] = getattr(ctx, "target_device_id", "") or None
        meta["zagent.session_id"] = getattr(ctx, "session_id", "") or None
        meta["zagent.request_id"] = getattr(ctx, "request_id", "") or None
        meta["zagent.task_id"] = getattr(ctx, "task_id", "") or None
        meta["zagent.trace_id"] = getattr(ctx, "trace_id", "") or None
    enriched["_meta"] = meta
    return enriched



async def _executor(service_id: str, tool_name: str, arguments: dict[str, Any], ctx):
    target = str(getattr(ctx, "target_device_id", "") or "").strip().lower()
    if target and target != _DEVICE_ID:
        raise IdentityRoutingError(
            ERROR_IDENTITY_MISMATCH,
            "Local Zagent cannot relay execution to another physical device; cross-device routing belongs to Zagent Cloud.",
        )
    if target:
        identity_store._validate_local_target(target)
    if service_id == "__workflow__":
        result = await workflow_runtime.execute_internal(
            tool_name,
            arguments,
            ctx,
        )
    else:
        result = await registry.invoke(service_id, tool_name, arguments, ctx)
    return _with_device_provenance(result, ctx)


orchestrator = Orchestrator(_executor)

SERVER_INFO = {"name": "Zagent Tunnel", "version": "4.1.0-zagent.3"}
MODERN_PROTOCOL = "2026-07-28"
HANDSHAKE_PROTOCOLS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
SERVER_INSTRUCTIONS = (
    "Zagent Tunnel v4 routes approved tools to local Zagent MCP services and provides "
    "multi-chat session isolation, async persistent tasks, priority scheduling, "
    "resource locking, recovery, telemetry, and durable profile-aware session orchestration. "
    "For user-visible progress updates during Zagent work, mirror the exact visible progress "
    "text through zagent_telemetry_status(progress_message=...). Never send private or hidden "
    "reasoning to progress_message."
)

_last_mcp_activity = 0.0
_last_tool_call_activity = 0.0

_ROUTER_ROOT = Path(__file__).resolve().parents[1]
_AGENT_ROOT = Path(__file__).resolve().parents[1]
_RUNTIME_DIR = Path(os.environ.get("ZAGENT_TUNNEL_RUNTIME_DIR", str(_ROUTER_ROOT / "runtime"))).resolve()
_CHAT_REGISTRY_PATH = _RUNTIME_DIR / "chat_registry.json"
_MANAGER_CHAT_LOG_PATH = Path(os.environ.get("ZAGENT_CHAT_LOG_PATH", str(_RUNTIME_DIR / "chat_log.jsonl"))).resolve()
_WORKFLOW_DB_PATH = Path(os.environ.get("ZAGENT_WORKFLOW_DB_PATH", str(_RUNTIME_DIR / "workflow_tasks.db"))).resolve()
workflow_journal = WorkflowDomainJournal(_WORKFLOW_DB_PATH)
workflow_journal.recover_incomplete_runs()
workflow_runtime = WorkflowRuntimeService(
    workflow_journal,
    orchestrator,
    service_id="zagent_arcmap",
    device_id=_DEVICE_ID,
)
_CHAT_REGISTRY_LOCK = threading.Lock()

_default_access_root = (
    Path(os.environ.get("LOCALAPPDATA", str(_RUNTIME_DIR)))
    / "Zagent"
    / "PluginAccess"
)
_plugin_access_root_raw = str(
    os.environ.get("ZAGENT_PLUGIN_ACCESS_ROOT", "")
).strip()
_PLUGIN_ACCESS_ROOT = Path(
    _plugin_access_root_raw or str(_default_access_root)
).expanduser().resolve()

access_store = PluginAccessStore(
    _PLUGIN_ACCESS_ROOT,
    audience=_PLUGIN_AUDIENCE,
    display_name=_PLUGIN_DISPLAY_NAME,
    device_id=_DEVICE_ID,
    device_alias=_DEVICE_ALIAS or _DEVICE_NAME,
    mode=_PLUGIN_ACCESS_MODE,
    enforcement=_PLUGIN_ACCESS_ENFORCEMENT,
    fallback_plugin=_PLUGIN_FALLBACK,
    parent_audience=_PLUGIN_PARENT_AUDIENCE,
)

identity_store = IdentityBindingStore(
    _RUNTIME_DIR,
    device_id=_DEVICE_ID,
    device_name=_DEVICE_NAME,
    device_alias=_DEVICE_ALIAS,
    fingerprint=_DEVICE_FINGERPRINT,
    installation_id=_INSTALLATION_ID,
    device_binding=_DEVICE_BINDING,
    enforcement_mode=_IDENTITY_ENFORCEMENT,
)
try:
    _binding_ttl_days = int(
        os.environ.get("ZAGENT_IDENTITY_BINDING_TTL_DAYS", "30")
    )
except ValueError:
    _binding_ttl_days = 30
if _binding_ttl_days > 0:
    identity_store.cleanup(
        max(1, _binding_ttl_days) * 24 * 60 * 60
    )


def _load_chat_registry() -> dict[str, Any]:
    try:
        data = json.loads(_CHAT_REGISTRY_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data.setdefault("version", 1)
            data.setdefault("next_alias", 1)
            data.setdefault("chats", {})
            return data
    except Exception:
        pass
    return {"version": 1, "next_alias": 1, "chats": {}}


def _save_chat_registry(data: dict[str, Any]) -> None:
    _CHAT_REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = _CHAT_REGISTRY_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(_CHAT_REGISTRY_PATH)


def _derive_chat_session(raw_session: str) -> str:
    digest = hashlib.sha256(raw_session.encode("utf-8", errors="ignore")).hexdigest()[:16]
    return f"chat_{digest}"


def _resolve_chat_identity(request: Request, params: dict[str, Any], tool_name: str = "") -> dict[str, Any]:
    request_meta = params.get("_meta") if isinstance(params, dict) else {}
    if not isinstance(request_meta, dict):
        request_meta = {}

    raw_session = str(
        request_meta.get("openai/session")
        or request.headers.get("x-openai-session")
        or ""
    ).strip()

    explicit_title = str(
        request_meta.get("openai/conversationTitle")
        or request_meta.get("openai/chatTitle")
        or request_meta.get("conversation_title")
        or request.headers.get("x-openai-conversation-title")
        or ""
    ).strip()

    if not raw_session:
        return {
            "session_id": "",
            "alias": "",
            "title": explicit_title,
            "display": explicit_title,
            "host_session_available": False,
        }

    session_id = _derive_chat_session(raw_session)
    now_value = time.time()

    with _CHAT_REGISTRY_LOCK:
        data = _load_chat_registry()
        chats = data.setdefault("chats", {})
        row = chats.get(session_id)
        if not isinstance(row, dict):
            number = int(data.get("next_alias", 1) or 1)
            alias = f"Chat {number:02d}"
            data["next_alias"] = number + 1
            row = {
                "session_id": session_id,
                "alias": alias,
                "title": "",
                "title_source": "unavailable_from_host",
                "first_seen": now_value,
                "last_seen": now_value,
                "last_tool": tool_name,
            }
            chats[session_id] = row
        else:
            row["last_seen"] = now_value
            if tool_name:
                row["last_tool"] = tool_name

        if explicit_title:
            row["title"] = explicit_title
            row["title_source"] = "host_metadata"

        _save_chat_registry(data)

    alias = str(row.get("alias") or session_id)
    title = str(row.get("title") or "")
    display = f"{alias} · {title}" if title else alias
    return {
        "session_id": session_id,
        "alias": alias,
        "title": title,
        "display": display,
        "host_session_available": True,
    }


def _authoritative_session_id(
    host_identity: dict[str, Any] | None,
    *candidates: Any,
) -> str:
    host = host_identity or {}
    host_session = (
        str(host.get("session_id") or "").strip()
        if bool(host.get("host_session_available"))
        else ""
    )
    explicit = ""
    for candidate in candidates:
        value = str(candidate or "").strip()
        if value:
            explicit = value
            break

    if host_session:
        if explicit and explicit != host_session:
            raise IdentityRoutingError(
                "SESSION_BINDING_CONFLICT",
                "The requested session_id does not match the authoritative host chat session.",
            )
        return host_session

    return explicit


def _chat_registry_public() -> dict[str, Any]:
    with _CHAT_REGISTRY_LOCK:
        data = _load_chat_registry()
    rows = []
    for row in data.get("chats", {}).values():
        if not isinstance(row, dict):
            continue
        rows.append({
            "session_id": str(row.get("session_id") or ""),
            "alias": str(row.get("alias") or ""),
            "title": str(row.get("title") or ""),
            "title_source": str(row.get("title_source") or ""),
            "first_seen": row.get("first_seen"),
            "last_seen": row.get("last_seen"),
            "last_tool": str(row.get("last_tool") or ""),
        })
    rows.sort(key=lambda item: float(item.get("first_seen") or 0))
    return {"chats": rows, "count": len(rows)}


def _activity_summary(tool_name: str, arguments: dict[str, Any]) -> str:
    if not isinstance(arguments, dict):
        return tool_name
    for key in ("path", "url", "selector", "cwd", "profile", "name", "output_path", "targetId"):
        value = arguments.get(key)
        if value not in (None, ""):
            text = str(value).replace("\r", " ").replace("\n", " ").strip()
            if len(text) > 140:
                text = text[:137] + "..."
            return f"{tool_name} · {text}"
    script = arguments.get("script")
    if isinstance(script, str) and script.strip():
        line = script.strip().splitlines()[0].strip()
        if len(line) > 140:
            line = line[:137] + "..."
        return f"{tool_name} · {line}"
    return tool_name


def _should_auto_log(tool_name: str, arguments: dict[str, Any]) -> bool:
    lower = (tool_name or "").lower()
    if lower in {
        "files_ping", "zagent_router_status", "zagent_task_status",
        "zagent_task_list", "zagent_telemetry_status",
    }:
        return False
    if lower == "files_powershell_run":
        script = str((arguments or {}).get("script") or "").lower()
        if "append-chat-log.ps1" in script:
            return False
    return True


def _append_auto_chat_activity(identity: dict[str, Any], service_id: str, tool_name: str, arguments: dict[str, Any]) -> None:
    if not identity.get("session_id") or not _should_auto_log(tool_name, arguments):
        return
    service_label = {
        "zagent_files": "Files",
        "zagent_browser": "Web",
        "zagent_arcmap": "ArcMap",
    }.get(service_id, service_id or "Zagent")
    payload = {
        "time": datetime.now().astimezone().isoformat(timespec="seconds"),
        "role": "activity",
        "source": "ChatGPT Activity",
        "message": f"{service_label} · {_activity_summary(tool_name, arguments)}",
        "chat_id": identity.get("session_id"),
        "chat_alias": identity.get("alias"),
        "chat_title": identity.get("title"),
        "tool": tool_name,
        "agent": service_label,
    }
    try:
        _MANAGER_CHAT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _MANAGER_CHAT_LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _append_chat_progress(identity: dict[str, Any], message: Any) -> bool:
    session_id = str(identity.get("session_id") or "").strip()
    text = str(message or "").strip()

    if not session_id or not text:
        return False

    if len(text) > 8000:
        text = text[:7997] + "..."

    payload = {
        "time": datetime.now().astimezone().isoformat(timespec="seconds"),
        "role": "assistant",
        "source": "ChatGPT",
        "message": text,
        "chat_id": session_id,
        "chat_alias": identity.get("alias"),
        "chat_title": identity.get("title"),
        "tool": "",
        "agent": "ChatGPT",
    }

    try:
        _MANAGER_CHAT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _MANAGER_CHAT_LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
        return True
    except Exception:
        return False


def _install_windows_transport_noise_filter():
    loop = asyncio.get_running_loop()
    previous = loop.get_exception_handler()

    def handler(event_loop, context):
        exc = context.get("exception")
        handle = str(context.get("handle") or "")
        message = str(context.get("message") or "")

        is_windows_reset = (
            isinstance(exc, ConnectionResetError)
            and getattr(exc, "winerror", None) == 10054
        )
        is_proactor_callback = (
            "_ProactorBasePipeTransport" in handle
            or "_call_connection_lost" in handle
            or "_call_connection_lost" in message
        )

        if is_windows_reset and is_proactor_callback:
            log.debug(
                "Suppressed expected Windows Proactor connection reset during "
                "cooperative upstream cancellation."
            )
            return

        if previous is not None:
            previous(event_loop, context)
        else:
            event_loop.default_exception_handler(context)

    loop.set_exception_handler(handler)
    return loop, previous


async def _identity_heartbeat_loop() -> None:
    while True:
        await asyncio.sleep(identity_store.heartbeat_interval_seconds)
        try:
            identity_store.heartbeat()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("Identity control-plane heartbeat failed: %s", exc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    loop, previous_exception_handler = _install_windows_transport_noise_filter()
    identity_heartbeat_task: asyncio.Task | None = None
    try:
        try:
            registry.reload_config()
            await registry.refresh_all()
        except Exception as exc:
            log.warning("Initial discovery failed: %s", exc)
        orch_cfg = load_tunnel_config().get("orchestration", {})
        orchestrator.configure(orch_cfg)
        await orchestrator.start(
            workers=int(orch_cfg.get("workers", 8)),
            fast_lane_workers=int(orch_cfg.get("fast_lane_workers", 2)),
        )
        identity_store.heartbeat()
        identity_heartbeat_task = asyncio.create_task(
            _identity_heartbeat_loop(),
            name="zagent-identity-control-plane-heartbeat",
        )
        yield
    finally:
        if identity_heartbeat_task is not None:
            identity_heartbeat_task.cancel()
            await asyncio.gather(identity_heartbeat_task, return_exceptions=True)
        await orchestrator.stop()
        await registry.close()
        await asyncio.sleep(0)
        loop.set_exception_handler(previous_exception_handler)


app = FastAPI(title="Zagent Tunnel v4 Router Core", version="4.1.0", lifespan=lifespan)


def _touch_client() -> None:
    global _last_mcp_activity
    _last_mcp_activity = time.time()


def _touch_tool_call() -> None:
    global _last_tool_call_activity
    _last_tool_call_activity = time.time()


def _status() -> dict[str, Any]:
    data = registry.status()
    now = time.time()
    cfg = load_tunnel_config()
    plugin_cfg = cfg.get("plugin", {})
    plugin_timeout = max(30, int(plugin_cfg.get("activity_timeout_seconds", 180)))
    plugin_configured = bool(plugin_cfg.get("configured", False))
    data["tool_count"] = len(registry.tool_index) + len(_management_tools())
    identity_status = identity_store.status()
    data["identity"] = {
        "device_id": _DEVICE_ID or None,
        "device_name": _DEVICE_NAME or None,
        "device_alias": _DEVICE_ALIAS or _DEVICE_NAME or None,
        "fingerprint": _DEVICE_FINGERPRINT or None,
        "installation_id": _INSTALLATION_ID or None,
        "trust_state": identity_store.trust_state,
        "target_validation": (
            "mandatory-session-binding"
            if identity_store.enforcement_mode == "mandatory"
            else "compatibility"
        ),
        "execution_provenance": True,
        "session_binding_count": identity_store.list_bindings()["count"],
        "no_silent_fallback": True,
        "successful_execution_invariant": "executed_on == target_device_id",
        "binding_store": identity_status["binding_store"],
    }
    data["router"] = {
        "last_mcp_activity": _last_mcp_activity or None,
        "route_active": bool(_last_mcp_activity and now - _last_mcp_activity <= 180),
    }
    data["plugin"] = {
        "name": str(plugin_cfg.get("name", "Zagent Plugin")),
        "configured": plugin_configured,
        "last_tool_call_activity": _last_tool_call_activity or None,
        "connected": bool(
            plugin_configured
            and _last_tool_call_activity
            and now - _last_tool_call_activity <= plugin_timeout
        ),
    }
    access_policy = access_store.policy()
    data["plugin_access"] = {
        **access_policy,
        "pending_requests": len(access_store.pending_requests()),
        "manager_resolved_only": True,
        "approval_scope": "chat-session",
    }
    data["orchestration"] = {
        **orchestrator.runtime_status(),
        "telemetry_events": len(orchestrator.telemetry.events),
        "task_db": str(orchestrator.journal.path),
        "sqlite_wal": True,
        "fast_path": True,
        "async_task_path": True,
        "session_orchestrator": True,
        "profile_affinity": True,
        "durable_files_sessions": True,
        "native_files_fast_path": True,
        "lifecycle_states": [
            "queued",
            "waiting_capacity",
            "waiting_lock",
            "running",
            "cancelling",
            "succeeded",
            "failed",
            "cancelled",
            "recovering",
        ],
    }
    return data


def _modern_meta() -> dict[str, Any]:
    return {"io.modelcontextprotocol/serverInfo": SERVER_INFO}


def rpc_result(req_id: Any, result: Any, modern: bool = False):
    if modern and isinstance(result, dict):
        result = dict(result)
        meta = dict(result.get("_meta") or {})
        meta.update(_modern_meta())
        result["_meta"] = meta
    return JSONResponse({"jsonrpc": "2.0", "id": req_id, "result": result})


def rpc_error(req_id: Any, code: int, message: str):
    return JSONResponse(
        {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}
    )


def _is_modern_request(request: Request, method: str, params: dict[str, Any]) -> bool:
    if method == "server/discover":
        return True
    header_version = (request.headers.get("mcp-protocol-version") or "").strip()
    if header_version == MODERN_PROTOCOL:
        return True
    meta = params.get("_meta") if isinstance(params, dict) else None
    return isinstance(meta, dict) and meta.get("io.modelcontextprotocol/protocolVersion") == MODERN_PROTOCOL


def _negotiate_handshake_version(requested: str) -> str:
    return requested if requested in HANDSHAKE_PROTOCOLS else HANDSHAKE_PROTOCOLS[0]


def _text_result(data: Any) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False, indent=2)}]}


def _management_tools() -> list[dict[str, Any]]:
    return [
        {
            "name": "zagent_access_bootstrap",
            "description": "Check chat-session plugin access before identity/bootstrap. Unapproved Production/TryPlugin sessions require operator prompt; DEV requires a Production grant.",
            "inputSchema": {
                "type": "object",
                "properties": {"session_id": {"type": "string"}},
                "additionalProperties": False,
            },
        },
        {
            "name": "zagent_access_status",
            "description": "Return plugin access state for this chat session.",
            "inputSchema": {
                "type": "object",
                "properties": {"session_id": {"type": "string"}},
                "additionalProperties": False,
            },
        },
        {
            "name": "zagent_access_request",
            "description": "Create a local Zagent Manager approval request for this chat session after the user explicitly provides operator_alias.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "session_id": {"type": "string"},
                    "operator_alias": {"type": "string"},
                },
                "required": ["operator_alias"],
                "additionalProperties": False,
            },
        },
        {
            "name": "zagent_access_request_status",
            "description": "Read one existing plugin access request status. This tool cannot approve or deny.",
            "inputSchema": {
                "type": "object",
                "properties": {"request_id": {"type": "string"}},
                "required": ["request_id"],
                "additionalProperties": False,
            },
        },
        {
            "name": "zagent_access_wait",
            "description": "Wait on the same Plugin Access request_id for up to 60 seconds, polling locally until APPROVED/DENIED/REVOKED or APPROVAL_TIMEOUT.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "request_id": {"type": "string"},
                    "timeout_seconds": {"type": "number", "minimum": 0.1, "maximum": 60.0},
                    "poll_interval_seconds": {"type": "number", "minimum": 0.1, "maximum": 2.0},
                },
                "required": ["request_id"],
                "additionalProperties": False,
            },
        },
        {
            "name": "zagent_access_revoke",
            "description": "Revoke this chat session plugin access. This does not approve any session.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "session_id": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "additionalProperties": False,
            },
        },
        {
            "name": "zagent_access_dev_grant_create",
            "description": "From an approved Zagent Production session, create the local grant required by Zagent Dev Plugin for the same chat session.",
            "inputSchema": {
                "type": "object",
                "properties": {"session_id": {"type": "string"}},
                "additionalProperties": False,
            },
        },
        {
            "name": "zagent_access_dev_grant_get",
            "description": "Read the local Production-to-DEV grant state for this chat session.",
            "inputSchema": {
                "type": "object",
                "properties": {"session_id": {"type": "string"}},
                "additionalProperties": False,
            },
        },
        {
            "name": "zagent_access_dev_child_bind",
            "description": "Bind the current DEV chat session to a previously created Production DEV grant.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "parent_session_id": {"type": "string"},
                    "grant_id": {"type": "string"},
                },
                "required": ["parent_session_id", "grant_id"],
                "additionalProperties": False,
            },
        },
        {
            "name": "zagent_router_status",
            "description": "Show Zagent Tunnel v4 Router Core, services, task engine, queue and telemetry status.",
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        },
        {
            "name": "zagent_router_refresh",
            "description": "Rediscover tools from enabled local MCP services behind Zagent Tunnel v4.",
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        },
        {
            "name": "zagent_identity_status",
            "description": "Return authoritative Zagent device identity, trust state, enforcement mode, and this chat session binding when available.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "session_id": {"type": "string"},
                },
                "additionalProperties": False,
            },
        },
        {
            "name": "zagent_identity_bootstrap",
            "description": "Bootstrap identity for the current chat using the local single-device authority. Bound sessions return immediately; unbound sessions require operator confirmation before local binding.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "session_id": {"type": "string"},
                },
                "additionalProperties": False,
            },
        },
        {
            "name": "zagent_identity_list_devices",
            "description": "Return the authoritative local Zagent device only. Global multi-device inventory belongs to Zagent Cloud.",
            "inputSchema": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        },
        {
            "name": "zagent_identity_session_get",
            "description": "Get the authoritative device/operator binding for one chat session.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "session_id": {"type": "string"},
                },
                "additionalProperties": False,
            },
        },
        {
            "name": "zagent_identity_session_bind",
            "description": "Bind one chat session to the local trusted Zagent device. operator_alias must match local device_alias; remote targets fail closed.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "session_id": {"type": "string"},
                    "operator_alias": {"type": "string"},
                    "target_device_id": {"type": "string"},
                },
                "required": ["operator_alias", "target_device_id"],
                "additionalProperties": False,
            },
        },
        {
            "name": "zagent_identity_session_unbind",
            "description": "Explicitly remove this chat session's active identity/device binding.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "session_id": {"type": "string"},
                },
                "additionalProperties": False,
            },
        },
        {
            "name": "zagent_identity_session_switch",
            "description": "Validate an explicit local-device switch request. Local Zagent cannot switch across physical devices; cross-device switching belongs to Zagent Cloud.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "session_id": {"type": "string"},
                    "target_device_id": {"type": "string"},
                    "confirmation": {"type": "boolean"},
                },
                "required": ["target_device_id", "confirmation"],
                "additionalProperties": False,
            },
        },
        {
            "name": "zagent_identity_verify_target",
            "description": "Verify that target_device_id resolves exactly to this trusted Zagent device without executing an agent tool.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "target_device_id": {"type": "string"},
                },
                "required": ["target_device_id"],
                "additionalProperties": False,
            },
        },
        {
            "name": "zagent_task_submit",
            "description": "Submit a medium/heavy tool call to Zagent Tunnel v4 persistent async task engine and immediately return task_id.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "tool_name": {"type": "string"},
                    "arguments": {"type": "object"},
                    "session_id": {"type": "string"},
                    "profile_id": {"type": "string"},
                    "request_id": {"type": "string"},
                    "trace_id": {"type": "string"},
                    "idempotency_key": {"type": "string"},
                    "resource_key": {"type": "string"},
                    "access_mode": {"type": "string", "enum": ["read", "write"]},
                    "priority": {"type": "integer", "minimum": 0, "maximum": 3},
                    "target_device_id": {"type": "string"},
                },
                "required": ["tool_name"],
                "additionalProperties": False,
            },
        },
        {
            "name": "zagent_task_status",
            "description": "Get persistent task status by task_id after disconnect/reconnect or during execution.",
            "inputSchema": {
                "type": "object",
                "properties": {"task_id": {"type": "string"}},
                "required": ["task_id"],
                "additionalProperties": False,
            },
        },
        {
            "name": "zagent_task_result",
            "description": "Get persistent task result by task_id.",
            "inputSchema": {
                "type": "object",
                "properties": {"task_id": {"type": "string"}},
                "required": ["task_id"],
                "additionalProperties": False,
            },
        },
        {
            "name": "zagent_task_cancel",
            "description": "Cancel a queued/running persistent task by task_id when cancellation is still possible.",
            "inputSchema": {
                "type": "object",
                "properties": {"task_id": {"type": "string"}},
                "required": ["task_id"],
                "additionalProperties": False,
            },
        },
        {
            "name": "zagent_task_list",
            "description": "List recent persistent tasks, optionally scoped to session_id.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "session_id": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 200},
                },
                "additionalProperties": False,
            },
        },
        {
            "name": "zagent_telemetry_status",
            "description": (
                "Show recent stage-level performance telemetry for routing and task execution. "
                "Optionally mirror one user-visible ChatGPT progress message into Zagent Manager "
                "Log Chat. Never use progress_message for private or hidden reasoning."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "progress_message": {
                        "type": "string",
                        "maxLength": 8000,
                    }
                },
                "additionalProperties": False,
            },
        },
    ]


def _policy_for(public_name: str, arguments: dict[str, Any], supplied: dict[str, Any] | None = None) -> dict[str, Any]:
    supplied = supplied or {}
    mapping = registry.get_mapping(public_name)
    service_id, upstream_name = mapping
    lower = upstream_name.lower()
    if service_id in {"zagent_files", "zagent_files_v2", "zagent_files_v3"} and lower.startswith("files_"):
        lower = lower[6:]

    read_prefixes = (
        "ping", "status", "get", "read", "list", "tree", "stat", "exists", "search",
        "hash", "diff", "inspect", "find", "runtime_info", "capabilities", "tools",
    )
    access_mode = str(supplied.get("access_mode") or ("read" if lower.startswith(read_prefixes) else "write"))

    if "resource_key" in supplied and supplied["resource_key"]:
        resource_key = str(supplied["resource_key"])
    elif service_id == "zagent_arcmap" and access_mode == "write":
        resource_key = "arcmap:state"
    elif service_id == "zagent_browser":
        resource_key = f"browser:{arguments.get('page_id') or arguments.get('context_id') or 'default'}"
    elif service_id in {"zagent_files", "zagent_files_v2", "zagent_files_v3"}:
        nested = arguments.get("arguments") if isinstance(arguments.get("arguments"), dict) else {}
        path = (arguments.get("path") or arguments.get("src") or arguments.get("source_path")
                or arguments.get("dst") or arguments.get("destination_path")
                or nested.get("path") or nested.get("src") or nested.get("source_path")
                or nested.get("dst") or nested.get("destination_path") or "profile")
        prefix = "filesv2" if service_id == "zagent_files_v2" else ("filesv3" if service_id == "zagent_files_v3" else "files")
        resource_key = f"{prefix}:{path}"
    else:
        resource_key = f"{service_id}:{upstream_name}"

    return {
        "service_id": service_id,
        "upstream_name": upstream_name,
        "access_mode": access_mode,
        "resource_key": resource_key,
        "priority": int(supplied.get("priority", 1 if access_mode == "read" else 2)),
    }


_ACCESS_BOOTSTRAP_TOOLS = {
    "zagent_access_bootstrap",
    "zagent_access_status",
    "zagent_access_request",
    "zagent_access_request_status",
    "zagent_access_wait",
    "zagent_access_revoke",
    "zagent_access_dev_grant_get",
    "zagent_access_dev_child_bind",
}


def _plugin_access_session_id(
    host_identity: dict[str, Any] | None,
    *candidates: Any,
) -> str:
    return _authoritative_session_id(host_identity, *candidates)


def _require_plugin_access(
    host_identity: dict[str, Any] | None,
    *session_candidates: Any,
) -> dict[str, Any]:
    session_id = _plugin_access_session_id(
        host_identity,
        *session_candidates,
    )
    authorization = access_store.authorization(session_id)
    if not authorization.get("allowed"):
        raise IdentityRoutingError(
            str(authorization.get("code") or "PLUGIN_ACCESS_REQUIRED"),
            str(
                authorization.get("message")
                or "Plugin access approval is required."
            ),
        )
    return authorization


async def _call_management(name: str, arguments: dict[str, Any], host_identity: dict[str, Any] | None = None) -> dict[str, Any] | None:
    if name == "zagent_access_bootstrap":
        session_id = _plugin_access_session_id(
            host_identity,
            arguments.get("session_id"),
        )
        return _text_result(access_store.bootstrap(session_id))
    if name == "zagent_access_status":
        session_id = _plugin_access_session_id(
            host_identity,
            arguments.get("session_id"),
        )
        return _text_result(access_store.status(session_id))
    if name == "zagent_access_request":
        session_id = _plugin_access_session_id(
            host_identity,
            arguments.get("session_id"),
        )
        return _text_result(
            access_store.request_access(
                session_id,
                str(arguments.get("operator_alias") or ""),
            )
        )
    if name == "zagent_access_request_status":
        return _text_result(
            access_store.request_status(
                str(arguments.get("request_id") or "")
            )
        )
    if name == "zagent_access_wait":
        request_id = str(arguments.get("request_id") or "")
        timeout_seconds = float(arguments.get("timeout_seconds") or 60.0)
        poll_interval_seconds = float(arguments.get("poll_interval_seconds") or 1.0)
        result = await asyncio.to_thread(
            access_store.wait_for_request,
            request_id,
            timeout_seconds=timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
        )
        return _text_result(result)
    if name == "zagent_access_revoke":
        session_id = _plugin_access_session_id(
            host_identity,
            arguments.get("session_id"),
        )
        return _text_result(
            access_store.revoke(
                session_id,
                str(arguments.get("reason") or ""),
            )
        )
    if name == "zagent_access_dev_grant_get":
        session_id = _plugin_access_session_id(
            host_identity,
            arguments.get("session_id"),
        )
        return _text_result(
            access_store.dev_grant_status(session_id)
        )
    if name == "zagent_access_dev_grant_create":
        session_id = _plugin_access_session_id(
            host_identity,
            arguments.get("session_id"),
        )
        _require_plugin_access(host_identity, session_id)
        return _text_result(
            access_store.create_dev_grant(session_id)
        )
    if name == "zagent_access_dev_child_bind":
        child_session_id = _plugin_access_session_id(host_identity)
        return _text_result(
            access_store.bind_dev_child(
                child_session_id,
                str(arguments.get("parent_session_id") or ""),
                str(arguments.get("grant_id") or ""),
            )
        )

    management_names = {
        item["name"]
        for item in _management_tools()
    }
    if name in management_names and name not in _ACCESS_BOOTSTRAP_TOOLS:
        _require_plugin_access(
            host_identity,
            arguments.get("session_id"),
        )

    if name == "zagent_router_status":
        return _text_result(_status())
    if name == "zagent_router_refresh":
        registry.reload_config()
        result = await registry.refresh_all()
        return _text_result({"refresh": result, "status": _status()})
    if name == "zagent_identity_status":
        session_id = _authoritative_session_id(
            host_identity,
            arguments.get("session_id"),
        )
        return _text_result(identity_store.status(session_id))
    if name == "zagent_identity_bootstrap":
        session_id = _authoritative_session_id(
            host_identity,
            arguments.get("session_id"),
        )
        binding = identity_store.get(session_id)
        if binding is not None:
            resolution = identity_store.resolve_target(session_id)
            binding = resolution.get("binding") or binding
            if access_store.enabled and _PLUGIN_AUDIENCE in {"zagent-production", "tryplugin"}:
                access_store.lock_session_to_current_audience(
                    session_id,
                    operator_alias=str(binding.get("operator_alias") or ""),
                    target_device_id=str(binding.get("target_device_id") or ""),
                    device_alias=str(binding.get("device_alias") or _DEVICE_ALIAS or _DEVICE_NAME),
                )
            return _text_result({
                "ok": True,
                "session_id": str(session_id),
                "bound": True,
                "binding": binding,
                "device_discovery_performed": False,
                "requires_operator_prompt": False,
                "operator_prompt": None,
                "bootstrap_policy": "binding-valid-skip-prompt",
                "authority_model": "single-device-local-authority",
            })

        inventory = identity_store.list_devices()
        return _text_result({
            "ok": True,
            "session_id": str(session_id),
            "bound": False,
            "binding": None,
            "device_discovery_performed": True,
            "device_inventory": inventory,
            "visible_device_count": int(inventory.get("count") or 0),
            "inventory_authoritative": bool(
                inventory.get("inventory_authoritative")
            ),
            "requires_operator_prompt": True,
            "operator_prompt": "Siapa yang menggunakan plugin sekarang?",
            "bootstrap_policy": "discover-then-always-ask-operator",
        })
    if name == "zagent_identity_list_devices":
        return _text_result(identity_store.list_devices())
    if name == "zagent_identity_session_get":
        session_id = _authoritative_session_id(
            host_identity,
            arguments.get("session_id"),
        )
        binding = identity_store.get(session_id)
        return _text_result({
            "ok": True,
            "session_id": str(session_id),
            "bound": binding is not None,
            "binding": binding,
            "requires_operator_prompt": binding is None,
            "operator_prompt": (
                "Siapa yang menggunakan plugin sekarang?"
                if binding is None
                else None
            ),
            "bootstrap_policy": (
                "ask-operator-before-bind"
                if binding is None
                else "binding-valid-skip-prompt"
            ),
        })
    if name == "zagent_identity_session_bind":
        session_id = _authoritative_session_id(
            host_identity,
            arguments.get("session_id"),
        )
        binding = identity_store.bind(
            session_id,
            arguments.get("operator_alias"),
            arguments.get("target_device_id"),
        )
        if access_store.enabled and _PLUGIN_AUDIENCE in {"zagent-production", "tryplugin"}:
            access_store.lock_session_to_current_audience(
                session_id,
                operator_alias=str(binding.get("operator_alias") or ""),
                target_device_id=str(binding.get("target_device_id") or ""),
                device_alias=str(binding.get("device_alias") or _DEVICE_ALIAS or _DEVICE_NAME),
            )
        return _text_result({
            "ok": True,
            "bound": True,
            "binding": binding,
            "session_plugin_lock": _PLUGIN_AUDIENCE,
        })
    if name == "zagent_identity_session_unbind":
        session_id = _authoritative_session_id(
            host_identity,
            arguments.get("session_id"),
        )
        result = identity_store.unbind(session_id)
        if access_store.enabled and _PLUGIN_AUDIENCE in {"zagent-production", "tryplugin"}:
            access_store.release_session_audience_lock(session_id)
        return _text_result(result)
    if name == "zagent_identity_session_switch":
        session_id = _authoritative_session_id(
            host_identity,
            arguments.get("session_id"),
        )
        binding = identity_store.switch(
            session_id,
            arguments.get("target_device_id"),
            confirmation=arguments.get("confirmation") is True,
        )
        return _text_result({
            "ok": True,
            "switched": True,
            "binding": binding,
        })
    if name == "zagent_identity_verify_target":
        return _text_result(
            identity_store.verify_target(
                arguments.get("target_device_id")
            )
        )
    if name == "zagent_task_submit":
        public_name = str(arguments.get("tool_name", ""))
        tool_args = arguments.get("arguments") or {}
        tool_meta = (
            dict(tool_args.get("metadata") or {})
            if isinstance(tool_args.get("metadata"), dict)
            else {}
        )
        policy = _policy_for(public_name, tool_args, arguments)
        supplied_target_device_id = (
            arguments.get("target_device_id")
            or tool_meta.get("target_device_id")
            or tool_meta.get("device_id")
        )
        session_id = _authoritative_session_id(
            host_identity,
            arguments.get("session_id"),
            tool_meta.get("session_id"),
        )
        resolution = identity_store.resolve_target(
            session_id,
            supplied_target_device_id,
        )
        target_device_id = resolution["target_device_id"]
        binding = resolution.get("binding") or {}
        meta = {
            "session_id": session_id,
            "profile_id": arguments.get("profile_id") or tool_meta.get("profile_id"),
            "request_id": arguments.get("request_id") or tool_meta.get("request_id"),
            "trace_id": arguments.get("trace_id") or tool_meta.get("trace_id"),
            "idempotency_key": arguments.get("idempotency_key") or tool_meta.get("idempotency_key"),
            "resource_key": arguments.get("resource_key") or tool_meta.get("resource_key") or policy["resource_key"],
            "access_mode": arguments.get("access_mode") or tool_meta.get("access_mode") or policy["access_mode"],
            "priority": arguments.get("priority", tool_meta.get("priority", policy["priority"])),
            "target_device_id": target_device_id,
            "executed_on": _DEVICE_ID or None,
            "operator_alias": binding.get("operator_alias"),
            "binding_revision": binding.get("revision"),
            "binding_source": resolution.get("binding_source"),
        }
        progress_message = str(tool_meta.get("progress_message") or "").strip()
        if progress_message and host_identity:
            _append_chat_progress(
                host_identity,
                progress_message,
            )

        if host_identity:
            _append_auto_chat_activity(
                host_identity, policy["service_id"], policy["upstream_name"], tool_args
            )
        data = await orchestrator.submit(
            policy["service_id"], policy["upstream_name"], tool_args, meta
        )
        return _text_result(data)
    if name == "zagent_task_status":
        task_id = str(arguments.get("task_id", ""))
        result = await orchestrator.status(task_id)
        host_session = _authoritative_session_id(host_identity)
        task = result.get("task") if isinstance(result, dict) else None
        if (
            host_session
            and isinstance(task, dict)
            and str(task.get("session_id") or "") != host_session
        ):
            raise IdentityRoutingError(
                "SESSION_BINDING_CONFLICT",
                "Task belongs to a different chat session.",
            )
        return _text_result(result)
    if name == "zagent_task_result":
        task_id = str(arguments.get("task_id", ""))
        status = await orchestrator.status(task_id)
        host_session = _authoritative_session_id(host_identity)
        task = status.get("task") if isinstance(status, dict) else None
        if (
            host_session
            and isinstance(task, dict)
            and str(task.get("session_id") or "") != host_session
        ):
            raise IdentityRoutingError(
                "SESSION_BINDING_CONFLICT",
                "Task belongs to a different chat session.",
            )
        return _text_result(await orchestrator.result(task_id))
    if name == "zagent_task_cancel":
        task_id = str(arguments.get("task_id", ""))
        status = await orchestrator.status(task_id)
        host_session = _authoritative_session_id(host_identity)
        task = status.get("task") if isinstance(status, dict) else None
        if (
            host_session
            and isinstance(task, dict)
            and str(task.get("session_id") or "") != host_session
        ):
            raise IdentityRoutingError(
                "SESSION_BINDING_CONFLICT",
                "Task belongs to a different chat session.",
            )
        return _text_result(await orchestrator.cancel(task_id))
    if name == "zagent_task_list":
        session_id = _authoritative_session_id(
            host_identity,
            arguments.get("session_id"),
        )
        return _text_result(await orchestrator.list_tasks(
            session_id=session_id or None,
            limit=int(arguments.get("limit", 50)),
        ))
    if name == "zagent_telemetry_status":
        progress_message = str(arguments.get("progress_message") or "").strip()
        mirrored = False

        if progress_message and host_identity:
            mirrored = _append_chat_progress(
                host_identity,
                progress_message,
            )

        data = orchestrator.telemetry.summary()

        if progress_message:
            data = {
                **data,
                "progress_mirrored": mirrored,
            }

        return _text_result(data)
    return None


@app.get("/router/access")
async def router_access():
    return {
        **access_store.policy(),
        "pending_requests": len(access_store.pending_requests()),
        "manager_resolved_only": True,
        "approval_scope": "chat-session",
    }


@app.get("/router/access/requests")
async def router_access_requests():
    return {
        "ok": True,
        "audience": access_store.audience,
        "display_name": access_store.display_name,
        "requests": access_store.pending_requests(),
        "count": len(access_store.pending_requests()),
    }


@app.post("/router/access/request/{request_id}/resolve")
async def router_access_resolve(request_id: str, request: Request):
    body = await request.json()
    action = str(body.get("action") or "").strip().lower()
    reason = str(body.get("reason") or "").strip()
    try:
        result = access_store.resolve_request(
            request_id,
            action,
            resolved_by="zagent_manager",
            reason=reason,
        )
    except ValueError as exc:
        return JSONResponse(
            status_code=400,
            content={
                "ok": False,
                "error": str(exc),
            },
        )
    if not result.get("ok") and result.get("code") == "PLUGIN_ACCESS_REQUEST_NOT_FOUND":
        return JSONResponse(status_code=404, content=result)
    return result


@app.get("/healthz")
async def healthz():
    return _status()


@app.get("/router/status")
async def router_status():
    return _status()


@app.get("/router/tasks")
async def router_tasks(session_id: str | None = None, limit: int = 50):
    return await orchestrator.list_tasks(session_id=session_id, limit=limit)


@app.post("/router/workflows/submit")
async def router_workflow_submit(request: Request):
    body = await request.json()
    return await workflow_runtime.submit(
        plan=body.get("plan") or {},
        owner=body.get("owner") or {},
        idempotency_key=str(body.get("idempotency_key") or ""),
        priority=int(body.get("priority", 1) or 1),
    )


@app.get("/router/workflows/{run_id}/status")
async def router_workflow_status(run_id: str, session_id: str | None = None):
    if not session_id:
        return JSONResponse(
            status_code=400,
            content={"ok": False, "error": "session_id is required for workflow isolation."},
        )
    result = await workflow_runtime.status(run_id, session_id)
    if not result.get("ok") and (result.get("errors") or [{}])[0].get("code") == "ACCESS_DENIED":
        return JSONResponse(status_code=403, content=result)
    return result


@app.post("/router/workflows/{run_id}/cancel")
async def router_workflow_cancel(run_id: str, request: Request):
    body = await request.json()
    session_id = str(body.get("session_id") or "")
    if not session_id:
        return JSONResponse(
            status_code=400,
            content={"ok": False, "error": "session_id is required for workflow isolation."},
        )
    return await workflow_runtime.cancel(run_id, session_id)


@app.post("/router/workflows/{run_id}/resume")
async def router_workflow_resume(run_id: str, request: Request):
    body = await request.json()
    session_id = str(body.get("session_id") or "")
    if not session_id:
        return JSONResponse(
            status_code=400,
            content={"ok": False, "error": "session_id is required for workflow isolation."},
        )
    return await workflow_runtime.resume(
        run_id,
        session_id,
        body.get("review_decision"),
    )


@app.get("/router/workflows/{run_id}/report")
async def router_workflow_report_v2(run_id: str, session_id: str | None = None):
    if not session_id:
        return JSONResponse(
            status_code=400,
            content={"ok": False, "error": "session_id is required for workflow isolation."},
        )
    result = await workflow_runtime.report(run_id, session_id)
    if not result.get("ok") and (result.get("errors") or [{}])[0].get("code") == "ACCESS_DENIED":
        return JSONResponse(status_code=403, content=result)
    return result


@app.get("/router/workflows")
async def router_workflows(session_id: str | None = None, limit: int = 100):
    if not session_id:
        return JSONResponse(
            status_code=400,
            content={"ok": False, "error": "session_id is required for workflow isolation."},
        )
    return await workflow_runtime.summary(
        session_id,
        limit=max(1, min(int(limit), 200)),
    )


@app.get("/router/workflows/{run_id}")
async def router_workflow_report(run_id: str, session_id: str | None = None):
    if not session_id:
        return JSONResponse(
            status_code=400,
            content={"ok": False, "error": "session_id is required for workflow isolation."},
        )
    result = await workflow_runtime.report(run_id, session_id)
    if not result.get("ok"):
        code = ((result.get("errors") or [{}])[0].get("code"))
        if code == "ACCESS_DENIED":
            return JSONResponse(status_code=403, content=result)
        if code == "RUN_NOT_FOUND":
            return JSONResponse(status_code=404, content=result)
        return result
    return {"ok": True, "report": result.get("data") or {}}


@app.get("/router/chats")
async def router_chats():
    return _chat_registry_public()


@app.get("/router/identity")
async def router_identity():
    return identity_store.status()


@app.get("/router/identity/bindings")
async def router_identity_bindings():
    return identity_store.list_bindings()


@app.get("/router/identity/devices")
async def router_identity_devices():
    return identity_store.list_devices()


@app.post("/router/internal/execute")
async def router_internal_execute(request: Request):
    # Compatibility endpoint only. Cross-device relay is a Zagent Cloud concern.
    return JSONResponse(
        status_code=410,
        content={
            "error": {
                "code": ERROR_IDENTITY_MISMATCH,
                "message": "Cross-device relay is disabled in local Zagent; use Zagent Cloud for multi-device routing.",
            }
        },
    )


@app.post("/router/identity/alias")
async def router_identity_alias(request: Request):
    body = await request.json()
    return identity_store.rename_alias(body.get("device_alias"))


@app.post("/router/identity/session/{session_id}/bind")
async def router_identity_session_bind_rest(session_id: str, request: Request):
    body = await request.json()
    return identity_store.bind(
        session_id,
        body.get("operator_alias"),
        body.get("target_device_id"),
    )


@app.post("/router/identity/session/{session_id}/switch")
async def router_identity_session_switch_rest(session_id: str, request: Request):
    body = await request.json()
    return identity_store.switch(
        session_id,
        body.get("target_device_id"),
        confirmation=body.get("confirmation") is True,
    )


@app.post("/router/identity/session/{session_id}/unbind")
async def router_identity_session_unbind(session_id: str):
    return identity_store.unbind(session_id)


@app.post("/router/identity/reverify")
async def router_identity_reverify():
    challenge = identity_store.create_challenge()
    proof = identity_store.create_local_proof(
        challenge["challenge_id"]
    )
    return identity_store.verify_challenge(
        challenge["challenge_id"],
        proof["proof"],
    )


@app.post("/router/identity/challenge")
async def router_identity_challenge():
    return identity_store.create_challenge()


@app.post("/router/identity/proof/{challenge_id}")
async def router_identity_proof(challenge_id: str):
    return identity_store.create_local_proof(challenge_id)


@app.post("/router/identity/verify")
async def router_identity_verify(request: Request):
    body = await request.json()
    return identity_store.verify_challenge(
        body.get("challenge_id"),
        body.get("proof"),
    )


@app.get("/router/tasks/{task_id}")
async def router_task_status(task_id: str):
    return await orchestrator.status(task_id)


@app.post("/router/tasks/{task_id}/cancel")
async def router_task_cancel(task_id: str):
    return await orchestrator.cancel(task_id)


@app.get("/router/telemetry")
async def router_telemetry():
    return orchestrator.telemetry.summary()


@app.post("/router/refresh")
async def router_refresh():
    registry.reload_config()
    result = await registry.refresh_all()
    return {"refresh": result, "status": _status()}


@app.post("/router/service/{service_id}/refresh")
async def router_service_refresh(service_id: str):
    registry.reload_config()
    if service_id not in registry.states:
        return JSONResponse({"ok": False, "error": "service not found"}, status_code=404)
    result = await registry.restart_service(service_id)
    return {"ok": True, "service_id": service_id, "result": result, "status": _status()}


@app.get("/router/service/{service_id}/tools")
async def router_service_tools(service_id: str):
    if service_id not in registry.states:
        return JSONResponse({"ok": False, "error": "service not found"}, status_code=404)
    return {"ok": True, "service_id": service_id, "tools": registry.service_public_tools(service_id)}


@app.get("/mcp")
async def mcp_get():
    return Response(status_code=405, headers={"Allow": "POST"})


@app.delete("/mcp")
async def mcp_delete():
    return Response(status_code=405, headers={"Allow": "POST"})


@app.post("/mcp")
async def mcp(request: Request):
    _touch_client()
    try:
        body = await request.json()
    except Exception:
        return rpc_error(None, -32700, "Parse error")

    if isinstance(body, list):
        return rpc_error(None, -32600, "Batch requests are not supported")
    if not isinstance(body, dict):
        return rpc_error(None, -32600, "Invalid Request")

    req_id = body.get("id")
    method = str(body.get("method", ""))
    params = body.get("params") or {}
    if not isinstance(params, dict):
        params = {}
    modern = _is_modern_request(request, method, params)

    try:
        if method == "server/discover":
            return rpc_result(req_id, {
                "supportedVersions": [MODERN_PROTOCOL, *HANDSHAKE_PROTOCOLS],
                "capabilities": {"tools": {"listChanged": True}},
                "instructions": SERVER_INSTRUCTIONS,
                "ttlMs": 1000,
                "cacheScope": "private",
            }, modern=True)

        if method == "initialize":
            requested = str(params.get("protocolVersion", "2025-11-25"))
            return rpc_result(req_id, {
                "protocolVersion": _negotiate_handshake_version(requested),
                "capabilities": {"tools": {"listChanged": True}},
                "serverInfo": SERVER_INFO,
                "instructions": SERVER_INSTRUCTIONS,
            })

        if method in {"notifications/initialized", "notifications/cancelled"}:
            return Response(status_code=202)
        if method == "ping":
            return rpc_result(req_id, {})
        if method == "tools/list":
            if not registry.tool_index:
                await registry.refresh_all()
            result = {"tools": [*registry.list_public_tools(), *_management_tools()]}
            if modern:
                result["ttlMs"] = 1000
                result["cacheScope"] = "private"
            return rpc_result(req_id, result, modern=modern)

        if method == "tools/call":
            name = str(params.get("name", ""))
            arguments = params.get("arguments") or {}
            host_identity = _resolve_chat_identity(request, params, name)
            mgmt = await _call_management(name, arguments, host_identity=host_identity)
            if mgmt is not None:
                _touch_tool_call()
                return rpc_result(req_id, mgmt, modern=modern)

            _require_plugin_access(host_identity)

            policy = _policy_for(name, arguments)
            request_meta = params.get("_meta") or {}
            argument_meta = (
                dict(arguments.get("metadata") or {})
                if isinstance(arguments.get("metadata"), dict)
                else {}
            )
            supplied_target_device_id = (
                request.headers.get("x-zagent-target-device-id")
                or request.headers.get("x-zagent-device-id")
                or request_meta.get("target_device_id")
                or request_meta.get("device_id")
                or argument_meta.get("target_device_id")
                or argument_meta.get("device_id")
            )
            session_id = _authoritative_session_id(
                host_identity,
                request.headers.get("x-zagent-session-id"),
                request_meta.get("session_id"),
                argument_meta.get("session_id"),
            )
            resolution = identity_store.resolve_target(
                session_id,
                supplied_target_device_id,
            )
            target_device_id = resolution["target_device_id"]
            binding = resolution.get("binding") or {}

            meta = {
                "session_id": session_id,
                "profile_id": (
                    request.headers.get("x-zagent-profile-id")
                    or request_meta.get("profile_id")
                    or argument_meta.get("profile_id")
                ),
                "request_id": str(
                    request_meta.get("request_id")
                    or argument_meta.get("request_id")
                    or req_id
                    or ""
                ),
                "trace_id": (
                    request.headers.get("x-zagent-trace-id")
                    or request_meta.get("trace_id")
                    or argument_meta.get("trace_id")
                ),
                "idempotency_key": (
                    request.headers.get("idempotency-key")
                    or request_meta.get("idempotency_key")
                    or argument_meta.get("idempotency_key")
                ),
                "resource_key": (
                    request_meta.get("resource_key")
                    or argument_meta.get("resource_key")
                    or policy["resource_key"]
                ),
                "access_mode": (
                    request_meta.get("access_mode")
                    or argument_meta.get("access_mode")
                    or policy["access_mode"]
                ),
                "priority": request_meta.get(
                    "priority", argument_meta.get("priority", policy["priority"])
                ),
                "target_device_id": target_device_id,
                "executed_on": _DEVICE_ID or None,
                "operator_alias": binding.get("operator_alias"),
                "binding_revision": binding.get("revision"),
                "binding_source": resolution.get("binding_source"),
            }
            progress_message = str(
                argument_meta.get("progress_message") or ""
            ).strip()

            if progress_message:
                _append_chat_progress(
                    host_identity,
                    progress_message,
                )

            _append_auto_chat_activity(
                host_identity, policy["service_id"], policy["upstream_name"], arguments
            )
            result = await orchestrator.execute_fast(
                policy["service_id"], policy["upstream_name"], arguments, meta
            )
            _touch_tool_call()
            return rpc_result(req_id, result, modern=modern)

        if method == "resources/list":
            return rpc_result(req_id, {"resources": []}, modern=modern)
        if method == "resources/templates/list":
            return rpc_result(req_id, {"resourceTemplates": []}, modern=modern)
        if method == "prompts/list":
            return rpc_result(req_id, {"prompts": []}, modern=modern)

        return rpc_error(req_id, -32601, f"Method not found: {method}")
    except IdentityRoutingError as exc:
        log.warning(
            "Identity routing rejected method=%s code=%s detail=%s",
            method,
            exc.code,
            exc.detail,
        )
        return rpc_error(req_id, -32041, str(exc))
    except KeyError as exc:
        return rpc_error(req_id, -32602, str(exc))
    except Exception as exc:
        log.exception("MCP request failed method=%s", method)
        return rpc_error(req_id, -32603, str(exc))


def run():
    cfg = load_tunnel_config()["router"]
    uvicorn.run(
        "router.server:app",
        host=cfg.get("host", "127.0.0.1"),
        port=int(cfg.get("port", 8016)),
        log_level=cfg.get("log_level", "info"),
        reload=False,
    )


if __name__ == "__main__":
    run()