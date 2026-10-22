"""Regression coverage for both supported runtime tool-event shapes.

Runtime adapters may represent executed tools as assistant tool-call records or
as separate tool-role messages. Both shapes must be harvested and scoped to the
correct run.
"""

import os
import sqlite3
from pathlib import Path

from tests.test_api import make_client, _any_project


def _make_realistic_state_db(path: Path, artifact_path: str, tools: list[str], assistant_calls: int = 0) -> None:
    """Mirror the REAL Hermes schema: executed tools are role='tool' rows."""
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, system_prompt TEXT, started_at REAL)"
    )
    conn.execute(
        "CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT, "
        "content TEXT, tool_calls TEXT, tool_name TEXT, tool_call_id TEXT, timestamp REAL)"
    )
    conn.execute(
        "INSERT INTO sessions(id, source, system_prompt, started_at) VALUES (?,?,?,?)",
        ("sess-1", "cli", f"Current working directory: {artifact_path}\n", 1000.0),
    )
    # assistant rows carrying tool_calls JSON (the shape M16 already handled)
    for i in range(assistant_calls):
        conn.execute(
            "INSERT INTO messages(session_id, role, content, tool_calls, tool_name, timestamp) VALUES (?,?,?,?,?,?)",
            ("sess-1", "assistant", "", '[{"function":{"name":"planned_tool","arguments":"{}"}}]', None, 1000.0 + i),
        )
    # role='tool' rows -- what actually happens at runtime, tool_calls is NULL
    for i, name in enumerate(tools):
        conn.execute(
            "INSERT INTO messages(session_id, role, content, tool_calls, tool_name, timestamp) VALUES (?,?,?,?,?,?)",
            ("sess-1", "tool", f"output of {name}", None, name, 2000.0 + i),
        )
    conn.commit()
    conn.close()


def _tool_events(client, run_id):
    events = client.get(f"/api/runs/{run_id}/events").json()["events"]
    return [e for e in events if e["type"] == "tool_activity"]


class TestRoleToolRowsAreHarvested:
    def test_executed_tools_are_not_dropped(self, tmp_path, monkeypatch):
        """The regression: 76 role='tool' rows must not collapse to 0."""
        state_db = tmp_path / "state.db"
        client, _ = make_client(tmp_path)
        chat_id = _any_project(client)["chats"][0]["id"]
        run = client.post(f"/api/chats/{chat_id}/runs", json={"mode": "work", "prompt": "harvest me"}).json()

        tools = ["read_file", "skill_view", "todo", "terminal", "patch", "write_file"]
        _make_realistic_state_db(state_db, run["artifact_path"], tools)
        monkeypatch.setattr("app.main._runtime_state_db_path", lambda: state_db)

        assert len(_tool_events(client, run["id"])) == len(tools)

    def test_tool_names_are_preserved(self, tmp_path, monkeypatch):
        state_db = tmp_path / "state.db"
        client, _ = make_client(tmp_path)
        chat_id = _any_project(client)["chats"][0]["id"]
        run = client.post(f"/api/chats/{chat_id}/runs", json={"mode": "work", "prompt": "names"}).json()
        _make_realistic_state_db(state_db, run["artifact_path"], ["read_file", "terminal"])
        monkeypatch.setattr("app.main._runtime_state_db_path", lambda: state_db)

        names = sorted(__import__("json").loads(e["payload_json"])["tool"] for e in _tool_events(client, run["id"]))
        assert names == ["read_file", "terminal"]

    def test_both_shapes_counted_without_double_count(self, tmp_path, monkeypatch):
        """assistant tool_calls AND role='tool' rows are distinct messages."""
        state_db = tmp_path / "state.db"
        client, _ = make_client(tmp_path)
        chat_id = _any_project(client)["chats"][0]["id"]
        run = client.post(f"/api/chats/{chat_id}/runs", json={"mode": "work", "prompt": "both"}).json()
        _make_realistic_state_db(state_db, run["artifact_path"], ["read_file", "terminal"], assistant_calls=3)
        monkeypatch.setattr("app.main._runtime_state_db_path", lambda: state_db)

        assert len(_tool_events(client, run["id"])) == 5

    def test_harvest_stays_idempotent(self, tmp_path, monkeypatch):
        state_db = tmp_path / "state.db"
        client, _ = make_client(tmp_path)
        chat_id = _any_project(client)["chats"][0]["id"]
        run = client.post(f"/api/chats/{chat_id}/runs", json={"mode": "work", "prompt": "idem"}).json()
        _make_realistic_state_db(state_db, run["artifact_path"], ["read_file", "terminal", "patch"])
        monkeypatch.setattr("app.main._runtime_state_db_path", lambda: state_db)

        first = len(_tool_events(client, run["id"]))
        for _ in range(3):
            client.get(f"/api/runs/{run['id']}/events")
            client.get(f"/api/runs/{run['id']}/live")
        assert len(_tool_events(client, run["id"])) == first == 3

    def test_other_runs_sessions_are_not_mixed_in(self, tmp_path, monkeypatch):
        """Scoping must stay per-artifact-path."""
        state_db = tmp_path / "state.db"
        client, _ = make_client(tmp_path)
        chat_id = _any_project(client)["chats"][0]["id"]
        mine = client.post(f"/api/chats/{chat_id}/runs", json={"mode": "work", "prompt": "mine"}).json()
        other = client.post(f"/api/chats/{chat_id}/runs", json={"mode": "work", "prompt": "other"}).json()
        _make_realistic_state_db(state_db, mine["artifact_path"], ["read_file", "terminal"])
        monkeypatch.setattr("app.main._runtime_state_db_path", lambda: state_db)

        assert len(_tool_events(client, mine["id"])) == 2
        assert len(_tool_events(client, other["id"])) == 0


class TestBackfillDryRunSafety:
    """A dry run must not write, even when the harvest helper commits internally."""

    def test_dry_run_leaves_db_byte_identical(self, tmp_path, monkeypatch):
        import hashlib
        import subprocess
        import sys as _sys
        from pathlib import Path as _Path

        state_db = tmp_path / "state.db"
        client, _ = make_client(tmp_path)
        chat_id = _any_project(client)["chats"][0]["id"]
        run = client.post(f"/api/chats/{chat_id}/runs", json={"mode": "work", "prompt": "dry"}).json()
        _make_realistic_state_db(state_db, run["artifact_path"], ["read_file", "terminal", "patch"])

        db = tmp_path / "workspace.db"
        before = hashlib.md5(db.read_bytes()).hexdigest()

        repo = _Path(__file__).resolve().parents[1]
        proc = subprocess.run(
            [_sys.executable, str(repo / "scripts" / "backfill_tool_harvest.py"), "--db", str(db), "--summaries"],
            capture_output=True, text=True, timeout=120,
            env={**os.environ, "HELM_RUNTIME_STATE_DB": str(state_db)},
        )
        assert proc.returncode == 0, proc.stderr
        assert '"mode": "dry-run"' in proc.stdout
        assert hashlib.md5(db.read_bytes()).hexdigest() == before, "dry run modified the database"
