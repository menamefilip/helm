"""M18b: terminal output panel — tail the runtime stdout log without bloating UI."""

import sys

from app.db import connect
from tests.test_api import make_client, _any_project


def _run(client, tmp_path, prompt, script):
    from app.main import _execute_runtime_process, _finalize_runtime_bridge

    chat_id = _any_project(client)["chats"][0]["id"]
    run = client.post(f"/api/chats/{chat_id}/runs", json={"mode": "work", "prompt": prompt}).json()
    with connect(tmp_path / "workspace.db") as conn:
        row = dict(conn.execute("SELECT * FROM runs WHERE id = ?", (run["id"],)).fetchone())
        result = _execute_runtime_process(row, [sys.executable, "-c", script], timeout_seconds=30)
        _finalize_runtime_bridge(conn, row, result)
    return run


class TestTerminalEndpoint:
    def test_returns_tail_of_runtime_output(self, tmp_path):
        client, _ = make_client(tmp_path)
        run = _run(client, tmp_path, "term", "print('hello from runtime')")
        data = client.get(f"/api/runs/{run['id']}/terminal").json()
        assert "hello from runtime" in data["output"]

    def test_tail_is_bounded_by_default(self, tmp_path):
        """Must not dump a 200KB log into the panel — that's the 'preplnené' problem."""
        client, _ = make_client(tmp_path)
        run = _run(client, tmp_path, "big", "\n".join([f"print('line {i}')" for i in range(3000)]))
        data = client.get(f"/api/runs/{run['id']}/terminal").json()
        assert data["line_count"] <= 200
        assert data["truncated"] is True
        # newest lines are what matter when debugging
        assert "line 2999" in data["output"]

    def test_lines_param_is_clamped(self, tmp_path):
        client, _ = make_client(tmp_path)
        run = _run(client, tmp_path, "clamp", "print('x')")
        data = client.get(f"/api/runs/{run['id']}/terminal?lines=99999").json()
        assert data["line_count"] <= 1000

    def test_secrets_are_scrubbed(self, tmp_path):
        client, _ = make_client(tmp_path)
        run = _run(client, tmp_path, "leak", "print('api_key=sk-test42')")
        data = client.get(f"/api/runs/{run['id']}/terminal").json()
        assert "sk-test42" not in data["output"]

    def test_missing_log_returns_empty_not_error(self, tmp_path):
        client, _ = make_client(tmp_path)
        chat_id = _any_project(client)["chats"][0]["id"]
        run = client.post(f"/api/chats/{chat_id}/runs", json={"mode": "work", "prompt": "no runtime yet"}).json()
        response = client.get(f"/api/runs/{run['id']}/terminal")
        assert response.status_code == 200
        assert response.json()["output"] == ""

    def test_unknown_run_is_404(self, tmp_path):
        client, _ = make_client(tmp_path)
        assert client.get("/api/runs/99999/terminal").status_code == 404

    def test_stderr_tail_returned_when_present(self, tmp_path):
        """M24: stderr is shown separately in the terminal panel."""
        client, _ = make_client(tmp_path)
        script = "import sys; print('out'); print('err', file=sys.stderr)"
        run = _run(client, tmp_path, "stderr", script)
        data = client.get(f"/api/runs/{run['id']}/terminal").json()
        assert data.get("stderr_tail") is not None  # field exists
        # stdout should contain 'out'
        assert "out" in data["output"]

    def test_terminal_includes_run_status(self, tmp_path):
        """Terminal response includes run status for UI badge."""
        client, _ = make_client(tmp_path)
        run = _run(client, tmp_path, "status", "print('x')")
        data = client.get(f"/api/runs/{run['id']}/terminal").json()
        assert "status" in data


class TestTerminalUiWiring:
    def test_terminal_tab_exists_in_activity_tabs(self):
        from pathlib import Path
        root = Path(__file__).resolve().parents[1]
        html = (root / "shared-ui" / "index.html").read_text(encoding="utf-8")
        # reuses the existing Tools/Actions tab strip instead of a new panel
        assert 'data-console-activity-tab="terminal"' in html

    def test_terminal_js_polls_endpoint(self):
        from pathlib import Path
        root = Path(__file__).resolve().parents[1]
        js = (root / "shared-ui" / "app.js").read_text(encoding="utf-8")
        assert "/terminal" in js
        assert "renderTerminalOutput" in js

    def test_stage_progress_ui_exists(self):
        """Stage progress stepper in progress panel."""
        from pathlib import Path
        root = Path(__file__).resolve().parents[1]
        html = (root / "shared-ui" / "index.html").read_text(encoding="utf-8")
        js = (root / "shared-ui" / "app.js").read_text(encoding="utf-8")
        css = (root / "shared-ui" / "styles.css").read_text(encoding="utf-8")
        assert 'data-stage-progress' in html
        assert "renderProgressPanel" in js
        assert ".stage-progress" in css
