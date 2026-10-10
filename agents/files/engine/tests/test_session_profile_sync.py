from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ENGINE_ROOT = Path(__file__).resolve().parents[1]
if str(ENGINE_ROOT) not in sys.path:
    sys.path.insert(0, str(ENGINE_ROOT))


def write_profiles(config_dir: Path, root_a: Path, root_b: Path) -> None:
    config_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": "2.0",
        "active_profile": "A",
        "profiles": [
            {
                "name": "A",
                "root": str(root_a),
                "mode": "read-write",
                "description": "test A",
                "enabled": True,
                "authorized_by_user": True,
                "authorization_source": "test",
            },
            {
                "name": "B",
                "root": str(root_b),
                "mode": "read-write",
                "description": "test B",
                "enabled": True,
                "authorized_by_user": True,
                "authorization_source": "test",
            },
        ],
    }
    (config_dir / "profiles.json").write_text(
        json.dumps(payload, indent=2),
        encoding="utf-8",
    )


def close_engine(engine) -> None:
    engine.pool.shutdown(wait=True, cancel_futures=True)
    engine.journal.close()


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="zagent-v3-session-sync-") as tmp:
        base = Path(tmp)
        data = base / "data"
        config = data / "config"
        runtime = data / "runtime"
        logs = data / "logs"
        backups = runtime / "backups"
        updates = data / "updates"
        root_a = base / "root-a"
        root_b = base / "root-b"
        root_a.mkdir()
        root_b.mkdir()

        os.environ["ZAGENT_FILES_DATA_ROOT"] = str(data)
        os.environ["ZAGENT_FILES_CONFIG_DIR"] = str(config)
        os.environ["ZAGENT_FILES_RUNTIME_DIR"] = str(runtime)
        os.environ["ZAGENT_FILES_LOG_DIR"] = str(logs)
        os.environ["ZAGENT_FILES_BACKUP_ROOT"] = str(backups)
        os.environ["ZAGENT_FILES_UPDATE_STATE_ROOT"] = str(updates)

        write_profiles(config, root_a, root_b)
        (config / "settings.json").write_text(
            json.dumps({
                "host": "127.0.0.1",
                "port": 8011,
                "max_read_bytes": 10485760,
                "max_write_bytes": 52428800,
                "secret_names": [],
            }, indent=2),
            encoding="utf-8",
        )

        from app.core.execution import Engine
        from app.profiles import manager

        engine = Engine({}, runtime=runtime)

        # 1. A new session may be seeded by the caller/default.
        chat_a = engine.ensure_session("chat-a", "A")
        assert chat_a["active_profile"] == "A", chat_a

        # 2. Changing the global default must not move an existing chat.
        manager.switch_default_profile("B")
        assert manager.default_profile_name() == "B"
        chat_a_after_default = engine.ensure_session("chat-a", "B")
        assert chat_a_after_default["active_profile"] == "A", chat_a_after_default

        # 3. A NEW session inherits the new global default.
        chat_b = engine.ensure_session("chat-b")
        assert chat_b["active_profile"] == "B", chat_b

        # 4. Mounting another profile for cross-profile work does not switch root.
        mounted = engine.mount_profile("chat-a", "B")
        assert mounted["active_profile"] == "A", mounted
        assert set(mounted["profiles"]) == {"A", "B"}, mounted

        # 5. Only an explicit session switch changes an existing session.
        switched = engine.switch_session("chat-a", "B")
        assert switched["active_profile"] == "B", switched
        assert manager.default_profile_name() == "B"

        # 6. Two chats retain independent active profiles.
        chat_c = engine.ensure_session("chat-c", "A")
        assert chat_c["active_profile"] == "A", chat_c
        assert engine.ensure_session("chat-b", "A")["active_profile"] == "B"
        assert engine.ensure_session("chat-c", "B")["active_profile"] == "A"

        # 7. Legacy/local Manager traffic is not shown as an interactive chat session.
        engine.ensure_session("legacy", "A")
        visible_ids = {row["session_id"] for row in engine.session_list()}
        assert "legacy" not in visible_ids
        assert {"chat-a", "chat-b", "chat-c"}.issubset(visible_ids)

        # 8. Restart/recovery preserves per-session active roots.
        close_engine(engine)
        engine = Engine({}, runtime=runtime)
        try:
            assert engine.ensure_session("chat-a", "A")["active_profile"] == "B"
            assert engine.ensure_session("chat-b", "A")["active_profile"] == "B"
            assert engine.ensure_session("chat-c", "B")["active_profile"] == "A"

            # 9. A brand-new chat after restart still follows the global default.
            chat_d = engine.ensure_session("chat-d")
            assert chat_d["active_profile"] == "B", chat_d

            rows = {row["session_id"]: row for row in engine.session_list()}
            assert rows["chat-a"]["active_profile"] == "B"
            assert rows["chat-b"]["active_profile"] == "B"
            assert rows["chat-c"]["active_profile"] == "A"
            assert rows["chat-d"]["active_profile"] == "B"
        finally:
            close_engine(engine)

    print("PASS: Zagent v3 Files active-root/session-sync regression")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
