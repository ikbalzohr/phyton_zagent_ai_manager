from __future__ import annotations
import inspect
import json
from pathlib import Path
from typing import get_type_hints
from mcp.server.fastmcp import FastMCP, Context
from mcp.types import ToolAnnotations
from app.mcp import legacy
from app.core.execution import Engine, READ
from app.core.batch import copy_batch_between_profiles
from app.core import updater
from app.profiles import manager

from app.paths import CONFIG_DIR
SETTINGS = json.loads((CONFIG_DIR / 'settings.json').read_text(encoding='utf-8'))
HOST, PORT = SETTINGS['host'], int(SETTINGS['port'])


mcp = FastMCP('Zagent Files 3.3.0-v3', host=HOST, port=PORT, stateless_http=True,
              json_response=True, streamable_http_path='/mcp')
operations = {name[6:]: fn for name,fn in vars(legacy).items()
              if name.startswith('files_') and callable(fn)}
operations['copy_batch_between_profiles'] = copy_batch_between_profiles
engine = Engine(operations)


def metadata_from(ctx, metadata):
    merged = {}
    try:
        request_meta = ctx.request_context.meta
        if request_meta:
            raw = request_meta.model_dump()
            merged.update({k.removeprefix('zagent.'):v for k,v in raw.items() if k.startswith('zagent.')})
        merged.setdefault('request_id', ctx.request_id)
    except (ValueError, AttributeError):
        pass
    merged.update(metadata or {})
    return merged


def register(op, fn):
    hints = get_type_hints(fn)
    params = [p.replace(annotation=hints.get(p.name, p.annotation))
              for p in inspect.signature(fn).parameters.values()]
    params += [inspect.Parameter('metadata', inspect.Parameter.KEYWORD_ONLY, default=None, annotation=dict | None),
               inspect.Parameter('ctx', inspect.Parameter.KEYWORD_ONLY, default=None, annotation=Context)]
    async def wrapped(**kwargs):
        meta = metadata_from(kwargs.pop('ctx', None), kwargs.pop('metadata', None))
        return await engine.execute(op, kwargs, meta)
    wrapped.__name__ = 'files_' + op
    wrapped.__signature__ = inspect.Signature(params, return_annotation=dict)
    wrapped.__annotations__ = {p.name:p.annotation for p in params}
    wrapped.__annotations__['return'] = dict
    cap = engine.capability(op)
    mcp.add_tool(wrapped, description='Auto-session '+op+'. Tunnel supplies stable session metadata; approved profiles are mounted/recovered automatically.',
                 annotations=ToolAnnotations(readOnlyHint=op in READ, destructiveHint=op not in READ,
                                             idempotentHint=op in READ, openWorldHint=False),
                 meta={'zagent.capability':cap})


for operation, function in operations.items():
    register(operation, function)


@mcp.tool()
def files_session_open(session_id: str, profiles: list[str], active_profile: str | None = None) -> dict:
    """Mount already-approved profiles in a durable session. Does not grant new permissions."""
    return engine.session(session_id, profiles, active_profile)


@mcp.tool()
def files_session_status(session_id: str, profile_id: str | None = None) -> dict:
    """Return or auto-recover a durable session. profile_id seeds only a missing session."""
    return engine.ensure_session(session_id, profile_id)


@mcp.tool()
def files_session_list() -> list[dict]:
    """List durable Files sessions and their independent active profiles."""
    return engine.session_list()


@mcp.tool()
def files_session_switch(session_id: str, profile_id: str) -> dict:
    """Explicitly switch one Files session without changing the global default."""
    result = engine.switch_session(session_id, profile_id)
    return {
        **result,
        'scope': 'session',
        'global_default_profile': manager.default_profile_name(),
    }


@mcp.tool()
async def files_execute(operation: str, arguments: dict, metadata: dict, ctx: Context) -> dict:
    """Tunnel async adapter. Put stable session/task/request/trace/idempotency metadata in arguments."""
    return await engine.execute(operation, arguments, metadata_from(ctx, metadata))


@mcp.tool()
def files_operation_status(operation_id: str, session_id: str) -> dict:
    """Read local checkpoint/result after disconnect. Session ownership is enforced."""
    return engine.journal.get(operation_id, session_id)


@mcp.tool()
def files_operation_cancel(session_id: str, operation_id: str | None = None,
                           task_id: str | None = None) -> dict:
    """Cancel a checkpointed batch by local operation_id or Tunnel task_id."""
    return engine.cancel(operation_id=operation_id, session_id=session_id, task_id=task_id)


@mcp.tool()
def files_operation_list(session_id: str, limit: int = 50) -> list[dict]:
    """Locate running operations and checkpoints by owning session after disconnect."""
    with engine.journal.lock:
        ids = engine.journal.db.execute('SELECT operation_id FROM operations WHERE session_id=? ORDER BY updated DESC LIMIT ?',
                                       (session_id, max(1,min(limit,100)))).fetchall()
    return [engine.journal.get(row[0], session_id) for row in ids]


@mcp.tool()
def files_capabilities() -> dict:
    return {'service':'Zagent Files', 'version':'3.3.0-v3', 'session_scoped_profiles':True,
            'global_default_is_new_session_seed_only':True,
            'explicit_session_switch':True,
            'automatic_session_recovery':True,
            'auto_mount_policy':'approved profiles only; no permission escalation; existing session active profile is never changed by transport profile hints',
            'native_fast_path':['ping','exists','read','write','append','patch','list','tree','stat','search','hash','diff'],
            'sqlite_wal':True, 'worker_threads':8, 'background_limit':6,
            'global_orchestrator':'Zagent Tunnel 4.1.0-zagent.1',
            'metadata_transport':['arguments.metadata','params._meta.zagent.*','Tunnel ExecContext auto-bridge'],
            'tunnel_session_orchestrator':'stable session_id/profile_id forwarding with durable session ownership in Zagent Files',
            'locking':'canonical path + ancestor/descendant reader/writer, atomic multi-resource acquisition',
            'disconnect':'generic operations continue safely; cancellable batch receives cooperative cancel',
            'restart':'completed replay; checkpointed batch resume; ambiguous generic operations orphaned',
            'self_update':True,
            'self_update_scope':'staging/validation local; apply/rollback owned by Zagent Core',
            'update_owner':'Zagent Core',
            'update_apply_mode':'core_broker',
            'operations':{op:engine.capability(op) for op in operations}}



@mcp.tool()
def files_update_capabilities():
    return updater.capabilities()


@mcp.tool()
def files_update_status():
    return updater.status()


@mcp.tool()
def files_update_prepare(update_id: str, description: str = "", target_version: str = ""):
    return updater.prepare(update_id, description, target_version)


@mcp.tool()
def files_update_stage_text(update_id: str, path: str, content: str,
                            encoding: str = "utf-8", expected_sha256: str = ""):
    return updater.stage_text(update_id, path, content, encoding, expected_sha256)


@mcp.tool()
def files_update_stage_b64(update_id: str, path: str, content_b64: str,
                           expected_sha256: str = ""):
    return updater.stage_b64(update_id, path, content_b64, expected_sha256)


@mcp.tool()
def files_update_mark_delete(update_id: str, path: str):
    return updater.mark_delete(update_id, path)


@mcp.tool()
def files_update_validate(update_id: str):
    return updater.validate(update_id)


@mcp.tool()
def files_update_apply(update_id: str):
    return updater.apply(update_id)


@mcp.tool()
def files_update_history(limit: int = 20):
    return updater.history(limit)


@mcp.tool()
def files_update_rollback(update_id: str):
    return updater.rollback(update_id)


@mcp.tool()
def files_update_abort(update_id: str):
    return updater.abort(update_id)

if __name__ == '__main__':
    mcp.run(transport='streamable-http')