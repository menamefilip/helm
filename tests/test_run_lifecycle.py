"""M18: successful runs should advance through their stages automatically.

A previous regression left some completed runs waiting in `review` for a manual
approval even when no review decision was needed.
"""

import sys

import pytest

from app.db import connect
from tests.test_api import make_client, _any_project


def _run_to_completion(client, tmp_path, prompt, script, timeout=30):
    from app.main import _execute_runtime_process, _finalize_runtime_bridge

    chat_id = _any_project(client)["chats"][0]["id"]
    run = client.post(f"/api/chats/{chat_id}/runs", json={"mode": "work", "prompt": prompt}).json()
    with connect(tmp_path / "workspace.db") as conn:
        row = dict(conn.execute("SELECT * FROM runs WHERE id = ?", (run["id"],)).fetchone())
        result = _execute_runtime_process(row, [sys.executable, "-c", script], timeout_seconds=timeout)
        _finalize_runtime_bridge(conn, row, result)
    return client.get(f"/api/runs/{run['id']}/stages").json()["run"]


class TestGreenRunsSelfComplete:
    def test_green_run_reaches_final_output(self, tmp_path):
        """A clean run with passing tests must not idle in review forever."""
        client, _ = make_client(tmp_path)
        run = _run_to_completion(client, tmp_path, "green work", "print('done'); print('5 passed in 1.0s')")
        assert run["current_stage"] == "final_output", f"stuck at {run['current_stage']}"
        assert run["status"] in {"approved", "completed"}

    def test_failed_run_does_not_self_complete(self, tmp_path):
        client, _ = make_client(tmp_path)
        run = _run_to_completion(client, tmp_path, "bad work", "import sys; print('boom'); sys.exit(1)")
        assert run["status"] == "failed"
        assert run["current_stage"] != "final_output"

    def test_run_reporting_test_failures_stays_in_review(self, tmp_path):
        """Exit 0 but failing tests is exactly the case a human must see."""
        client, _ = make_client(tmp_path)
        run = _run_to_completion(client, tmp_path, "flaky", "print('3 passed, 2 failed in 1s')")
        assert run["current_stage"] == "review"
        assert run["status"] == "review"

    def test_run_needing_attention_is_flagged(self, tmp_path):
        """The UI must distinguish 'waiting for user review' from an abandoned run."""
        client, _ = make_client(tmp_path)
        run = _run_to_completion(client, tmp_path, "flaky", "print('1 passed, 1 failed')")
        stages = client.get(f"/api/runs/{run['id']}/stages").json()
        assert stages.get("needs_attention") is True
        assert stages.get("attention_reason")

    def test_green_run_is_not_flagged_for_attention(self, tmp_path):
        client, _ = make_client(tmp_path)
        run = _run_to_completion(client, tmp_path, "green", "print('7 passed')")
        stages = client.get(f"/api/runs/{run['id']}/stages").json()
        assert stages.get("needs_attention") is False

    def test_auto_completion_is_recorded_as_event(self, tmp_path):
        client, _ = make_client(tmp_path)
        run = _run_to_completion(client, tmp_path, "green", "print('2 passed')")
        events = [e["type"] for e in client.get(f"/api/runs/{run['id']}/events").json()["events"]]
        assert "run_auto_completed" in events

    def test_summary_still_generated_for_auto_completed_run(self, tmp_path):
        """M15a must keep working through the new lifecycle path."""
        client, _ = make_client(tmp_path)
        run = _run_to_completion(client, tmp_path, "green", "print('9 passed in 2s')")
        data = client.get(f"/api/runs/{run['id']}/summary").json()
        assert "9 passed" in data["tests"]


class TestSafetyGate:
    def test_external_action_run_requires_manual_approval(self, tmp_path):
        """External actions must not auto-complete without explicit approval."""
        client, _ = make_client(tmp_path)
        run = _run_to_completion(
            client, tmp_path, "publish it",
            "print('POST https://api.example.com/send -> 200'); print('1 passed')",
        )
        assert run["current_stage"] == "review", "external action must not auto-complete"

    def test_manual_approve_still_works(self, tmp_path):
        client, _ = make_client(tmp_path)
        run = _run_to_completion(client, tmp_path, "flaky", "print('1 failed')")
        approved = client.post(f"/api/runs/{run['id']}/approve", json={"note": "checked"}).json()
        assert approved["current_stage"] == "final_output"


class TestStuckRunRecovery:
    def test_endpoint_lists_stuck_runs(self, tmp_path):
        client, _ = make_client(tmp_path)
        _run_to_completion(client, tmp_path, "flaky", "print('1 failed')")
        data = client.get("/api/runs/needs-attention").json()
        assert data["count"] >= 1
        assert data["runs"][0]["attention_reason"]
