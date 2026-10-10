from contextvars import ContextVar
from mcp.server import MCPServer
import os
import time

correlation = ContextVar("zagent_arcmap_correlation", default={})

class CorrelatedMCPServer(MCPServer):
    async def call_tool(self, name, arguments, context=None):
        meta = {}
        if context is not None:
            try:
                raw = context.request_context.meta
                meta = raw.model_dump(by_alias=True) if hasattr(raw, "model_dump") else dict(raw or {})
            except (ValueError, TypeError):
                pass
        values = {key: meta.get("zagent." + key) for key in
                  ("session_id", "request_id", "trace_id", "device_id", "task_id",
                   "operator_alias", "idempotency_key", "resource_key")}
        values["mcp_receive_epoch"] = time.time()
        values["mcp_tool_name"] = name
        local = os.environ.get("ZAGENT_DEVICE_ID", "").strip()
        if local and values.get("device_id") and local != values["device_id"]:
            raise ValueError("IDENTITY_MISMATCH: ArcMap request targets another device.")
        token = correlation.set(values)
        try:
            return await super().call_tool(name, arguments, context)
        finally:
            correlation.reset(token)

def current():
    return dict(correlation.get())
