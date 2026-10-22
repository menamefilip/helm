import json
import os
from pathlib import Path
import sys
import time

from fastapi.testclient import TestClient

from app.db import connect, init_db
from app.main import create_app, _runtime_command
from app.workspace import PROJECT_MAP_DOC_PATHS, load_retrieved_context_chunks, render_retrieved_context


def make_client(tmp_path, **app_kwargs):
    workspace_root = tmp_path / "helm"
    app = create_app(db_path=tmp_path / "workspace.db", workspace_root=workspace_root, **app_kwargs)
    return TestClient(app), workspace_root


def wait_for_run_status(client, run_id: int, expected: str, timeout: float = 5.0):
    """Wait for a run status.

    Since M18 a clean run no longer parks in `review` — it auto-advances to
    completed/final_output. Tests that wait for `review` therefore accept any
    terminal success state, so they assert "the runtime finished OK" rather than
    pinning an intermediate stage.
    """
    terminal_success = {"review", "approved", "completed"}
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        response = client.get(f"/api/runs/{run_id}/stages")
        assert response.status_code == 200
        last = response.json()["run"]
        if last["status"] == expected:
            return last
        if expected == "review" and last["status"] in terminal_success:
            return last
        time.sleep(0.05)
    raise AssertionError(f"Run {run_id} did not reach {expected}; last={last}")


def test_health_returns_ok(tmp_path):
    client, _ = make_client(tmp_path)

    response = client.get("/health")

    assert response.status_code == 200
    payload = response.json()
    # Asserted by subset, not equality: /health also reports which instance
    # answered (`env`, `label`) since the M23 dev/prod split, and an exact-match
    # assertion here would block any future additive field.
    assert payload["status"] == "ok"
    assert payload["service"] == "helm-api"
    assert payload["env"] in {"dev", "live"}
    assert payload["label"]


def test_index_serves_shared_helm_ui(tmp_path):
    client, _ = make_client(tmp_path)

    response = client.get("/")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "Helm" in response.text
    assert 'href="styles.css"' in response.text
    assert 'src="app.js"' in response.text
    assert 'data-resize-agent-console role="separator"' in response.text
    assert 'tabindex="0"' in response.text
    assert 'aria-orientation="vertical"' in response.text


def test_shared_ui_split_css_and_js_are_served(tmp_path):
    client, _ = make_client(tmp_path)

    css = client.get("/styles.css")
    js = client.get("/app.js")

    assert css.status_code == 200
    assert "text/css" in css.headers["content-type"]
    assert "Agent Console" in css.text
    assert js.status_code == 200
    assert "javascript" in js.headers["content-type"]
    assert "renderRunEvents" in js.text



def test_agent_console_has_separate_tools_and_actions_tabs(tmp_path):
    client, _ = make_client(tmp_path)

    html = client.get("/").text
    js = client.get("/app.js").text

    assert 'data-console-activity-tab="tools"' in html
    assert 'data-tools-tab-count' in html
    assert 'data-console-activity-tab="actions"' in html
    assert 'data-actions-tab-count' in html
    assert "function isToolCallEvent" in js
    assert "['tool_call', 'tool_activity'].includes(event?.type)" in js
    assert "function isActionishEvent" in js
    assert "no tool events" in js


def test_shared_ui_assets_are_served(tmp_path):
    client, _ = make_client(tmp_path)

    response = client.get("/assets/helm-logo.png")

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.content.startswith(b"\x89PNG")


def test_state_returns_helm_only_seeded_project_and_modes(tmp_path):
    client, _ = make_client(tmp_path)

    response = client.get("/api/state")

    assert response.status_code == 200
    data = response.json()
    assert data["modes"] == ["learning", "work", "sideproject"]
    assert [project["slug"] for project in data["projects"]] == ["helm"]
    assert data["projects"][0]["name"] == "Helm"
    assert data["active_project_slug"] == "helm"


def test_mock_seed_cleanup_preserves_projects_with_user_data(tmp_path):
    db_path = tmp_path / "workspace.db"
    init_db(db_path)
    with connect(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO projects(name, slug, description, root_path, mode_default) VALUES (?, ?, ?, ?, ?)",
            ("ESP32 Pentest", "esp32-pentest", "Práca", "", "work"),
        )
        project_id = cur.lastrowid
        chat = conn.execute("INSERT INTO chats(project_id, title, mode) VALUES (?, ?, ?)", (project_id, "Main", "work"))
        conn.execute("INSERT INTO messages(chat_id, role, content) VALUES (?, ?, ?)", (chat.lastrowid, "user", "real data"))

    init_db(db_path)

    with connect(db_path) as conn:
        preserved = conn.execute("SELECT * FROM projects WHERE slug = 'esp32-pentest'").fetchone()
        assert preserved is not None
        assert conn.execute("SELECT COUNT(*) AS c FROM messages").fetchone()["c"] == 1


def test_state_can_filter_projects_by_mode(tmp_path):
    client, _ = make_client(tmp_path)

    learning = client.get("/api/state", params={"mode": "learning"})
    sideproject = client.get("/api/state", params={"mode": "sideproject"})

    assert learning.status_code == 200
    assert learning.json()["projects"] == []
    assert learning.json()["active_project_slug"] is None
    assert sideproject.status_code == 200
    assert [project["slug"] for project in sideproject.json()["projects"]] == ["helm"]


def test_settings_expose_and_persist_appearance_theme(tmp_path):
    client, _ = make_client(tmp_path)

    initial = client.get("/api/settings")
    assert initial.status_code == 200
    assert initial.json()["theme_id"] == "helm-default"
    assert initial.json()["themes"] == [
        {"id": "helm-default", "name": "Helm Default"},
        {"id": "helm-rpg", "name": "Helm RPG"},
    ]

    updated = client.patch("/api/settings", json={"theme_id": "helm-rpg"})
    assert updated.status_code == 200
    assert updated.json()["theme_id"] == "helm-rpg"
    assert updated.json()["safety_policy_enabled"] is True
    assert client.get("/api/settings").json()["theme_id"] == "helm-rpg"

    invalid = client.patch("/api/settings", json={"theme_id": "unknown-theme"})
    assert invalid.status_code == 422


def test_can_create_new_chat_inside_project(tmp_path):
    client, _ = make_client(tmp_path)
    helm_project = next(project for project in client.get("/api/state").json()["projects"] if project["slug"] == "helm")

    created = client.post(f"/api/projects/{helm_project['id']}/chats", json={"title": "M8 UX test", "mode": "sideproject"})

    assert created.status_code == 201
    chat = created.json()
    assert chat["title"] == "M8 UX test"
    assert chat["mode"] == "sideproject"
    state = client.get("/api/state", params={"mode": "sideproject"}).json()
    helm = next(project for project in state["projects"] if project["slug"] == "helm")
    assert "M8 UX test" in [row["title"] for row in helm["chats"]]


def test_can_create_project_with_workspace_folder_for_mode(tmp_path):
    client, workspace_root = make_client(tmp_path)

    created = client.post(
        "/api/projects",
        json={"name": "Course Planning", "mode_default": "learning", "description": "Study workspace"},
    )

    assert created.status_code == 201
    project = created.json()
    assert project["slug"] == "course-planning"
    assert project["mode_default"] == "learning"
    assert project["root_path"] == str(workspace_root / "domains" / "school" / "active" / "course-planning")
    assert (workspace_root / "domains" / "school" / "active" / "course-planning" / "CONTEXT.md").exists()
    state = client.get("/api/state", params={"mode": "learning"}).json()
    assert "course-planning" in [row["slug"] for row in state["projects"]]
    new_project = next(row for row in state["projects"] if row["slug"] == "course-planning")
    assert [chat["title"] for chat in new_project["chats"]] == ["Main"]


def test_create_project_ignores_client_supplied_root_path(tmp_path):
    client, workspace_root = make_client(tmp_path)

    created = client.post(
        "/api/projects",
        json={"name": "Unsafe Root", "mode_default": "work", "root_path": "/tmp/../../etc"},
    )

    assert created.status_code == 201
    project = created.json()
    assert project["root_path"] == str(workspace_root / "domains" / "ai-it" / "active" / "unsafe-root")
    assert ".." not in project["root_path"]


def test_create_chat_uses_project_mode_even_if_client_sends_other_mode(tmp_path):
    client, _ = make_client(tmp_path)
    helm_project = next(project for project in client.get("/api/state").json()["projects"] if project["slug"] == "helm")

    created = client.post(f"/api/projects/{helm_project['id']}/chats", json={"title": "Cross mode attempt", "mode": "learning"})

    assert created.status_code == 201
    assert created.json()["mode"] == "sideproject"
    side_state = client.get("/api/state", params={"mode": "sideproject"}).json()
    helm = next(project for project in side_state["projects"] if project["slug"] == "helm")
    assert "Cross mode attempt" in [chat["title"] for chat in helm["chats"]]


def test_can_create_and_list_chat_messages(tmp_path):
    client, _ = make_client(tmp_path)
    state = client.get("/api/state").json()
    chat_id = state["projects"][0]["chats"][0]["id"]

    created = client.post(
        f"/api/chats/{chat_id}/messages",
        json={"role": "user", "content": "Skontroluj platformio.ini"},
    )

    assert created.status_code == 201
    assert created.json()["content"] == "Skontroluj platformio.ini"

    listed = client.get(f"/api/chats/{chat_id}/messages")
    assert listed.status_code == 200
    assert [m["content"] for m in listed.json()["messages"]] == ["Skontroluj platformio.ini"]


def test_workspace_tree_bootstraps_helm_structure(tmp_path):
    client, workspace_root = make_client(tmp_path)

    response = client.get("/api/workspace/tree")

    assert response.status_code == 200
    data = response.json()
    assert data["root"] == str(workspace_root)
    paths = {item["path"] for item in data["items"]}
    assert "system/policies/tutor-ai.md" not in paths
    assert "system/stages/tutor-task.md" not in paths
    assert "domains/school/CONTEXT.md" in paths
    assert "domains/ai-it/CONTEXT.md" in paths
    assert "runs" in paths


def test_start_run_creates_initial_events_and_artifacts(tmp_path):
    client, workspace_root = make_client(tmp_path)
    chat_id = client.get("/api/state").json()["projects"][0]["chats"][0]["id"]

    response = client.post(f"/api/chats/{chat_id}/runs", json={"mode": "work", "prompt": "run diagnostics"})

    assert response.status_code == 201
    run = response.json()
    assert run["status"] == "queued"
    assert run["mode"] == "sideproject"
    assert run["artifact_path"].startswith(str(workspace_root / "runs"))

    events = client.get(f"/api/runs/{run['id']}/events")
    assert events.status_code == 200
    event_rows = events.json()["events"]
    event_types = [event["type"] for event in event_rows]
    assert event_types[:3] == ["run_created", "safety_note", "artifacts_initialized"]
    assert "run_contract_initialized" in event_types
    assert "workflow_policy_loaded" in event_types
    assert event_types[-1] == "stage_started"
    payloads = {event["type"]: json.loads(event["payload_json"]) for event in event_rows}
    assert payloads["run_created"]["mode"] == "sideproject"
    assert "Externé akcie" in payloads["safety_note"]["message"]
    assert payloads["artifacts_initialized"]["artifact_path"].startswith(str(workspace_root / "runs"))
    assert payloads["run_contract_initialized"]["artifact"] == "run-contract.md"

    artifacts = client.get(f"/api/runs/{run['id']}/artifacts")
    assert artifacts.status_code == 200
    artifact_names = {item["name"] for item in artifacts.json()["artifacts"]}
    assert {"input.md", "run-contract.md", "plan.md", "output.md", "review.md", "events.jsonl"}.issubset(artifact_names)


def test_run_creation_loads_workflow_governance_into_input_contract_and_events(tmp_path):
    client, workspace_root = make_client(tmp_path)
    docs = workspace_root / "docs"
    docs.mkdir()
    (docs / "HELM_SYSTEM_MAP.md").write_text("# HELM_SYSTEM_MAP\n\nUnique source routing beacon.", encoding="utf-8")
    (docs / "HELM_CONTEXT.md").write_text("# HELM_CONTEXT\n\nUnique Helm architecture beacon.", encoding="utf-8")
    (docs / "FILE_INDEX.md").write_text("# FILE_INDEX\n\nUnique file navigation beacon.", encoding="utf-8")
    (docs / "API_INDEX.md").write_text("# API_INDEX\n\nUnique API endpoint beacon.", encoding="utf-8")
    (docs / "UI_INDEX.md").write_text("# UI_INDEX\n\nUnique UI symbol beacon.", encoding="utf-8")
    (docs / "TOKEN_USAGE_OPTIMIZATION.md").write_text("# TOKEN_USAGE_OPTIMIZATION\n\nUnique chunking discipline beacon.", encoding="utf-8")
    chat_id = client.get("/api/state").json()["projects"][0]["chats"][0]["id"]

    run = client.post(f"/api/chats/{chat_id}/runs", json={"mode": "sideproject", "prompt": "build reliability layer", "context_tier": "L"}).json()

    input_md = client.get(f"/api/runs/{run['id']}/artifacts/input.md").json()["content"]
    assert "## Workflow governance" in input_md
    assert "system/policies/run-contract.md" in input_md
    assert "system/policies/milestone-context-reset.md" in input_md
    assert "system/policies/autonomy-lanes.md" in input_md
    assert "system/policies/knowledge-routing.md" in input_md
    assert "system/policies/ui-architecture.md" in input_md
    assert "system/policies/environment-routing.md" in input_md
    assert "system/failure-patterns.md" in input_md
    assert "Green lane" in input_md
    assert "Store only stable user or environment facts in memory" in input_md
    assert "The center workspace holds the user-facing chat and project views" in input_md
    assert "The Agent Console shows run state" in input_md
    assert "Do not duplicate chat inside the Agent Console" in input_md
    assert "Keep development and stable environments separate" in input_md
    assert "HELM_RUNTIME_COMMAND" in input_md
    assert "## Context navigation maps" in input_md
    assert "Use these project maps before broad source inspection" in input_md
    assert "docs/HELM_SYSTEM_MAP.md" in input_md
    assert "docs/HELM_CONTEXT.md" in input_md
    assert "docs/FILE_INDEX.md" in input_md
    assert "docs/API_INDEX.md" in input_md
    assert "docs/UI_INDEX.md" in input_md
    assert "docs/TOKEN_USAGE_OPTIMIZATION.md" in input_md
    assert "read only targeted chunks instead of full large files" in input_md
    assert "Unique source routing beacon" in input_md
    assert "Unique Helm architecture beacon" in input_md
    assert "Unique file navigation beacon" in input_md
    assert "Unique API endpoint beacon" in input_md
    assert "Unique UI symbol beacon" in input_md
    assert "Unique chunking discipline beacon" in input_md

    run_contract = client.get(f"/api/runs/{run['id']}/artifacts/run-contract.md").json()["content"]
    assert "# Run Contract" in run_contract
    assert "## Goal" in run_contract
    assert "build reliability layer" in run_contract
    assert "## Autonomy Lane" in run_contract
    assert "## Persistence Routing" in run_contract
    assert "## Next Milestone Link" in run_contract

    events = client.get(f"/api/runs/{run['id']}/events").json()["events"]
    event_types = [event["type"] for event in events]
    assert "run_contract_initialized" in event_types
    assert "context_map_loaded" in event_types
    workflow_payloads = [json.loads(event["payload_json"]) for event in events if event["type"] == "workflow_policy_loaded"]
    map_payloads = [json.loads(event["payload_json"]) for event in events if event["type"] == "context_map_loaded"]
    assert {payload["path"] for payload in map_payloads} >= {
        "docs/HELM_SYSTEM_MAP.md",
        "docs/HELM_CONTEXT.md",
        "docs/FILE_INDEX.md",
        "docs/API_INDEX.md",
        "docs/UI_INDEX.md",
        "docs/TOKEN_USAGE_OPTIMIZATION.md",
    }
    assert {payload["source"] for payload in map_payloads} == {"workspace"}
    input_section_order = [input_md.index(f"### {path}") for path in PROJECT_MAP_DOC_PATHS]
    assert input_section_order == sorted(input_section_order)
    assert [payload["path"] for payload in map_payloads] == PROJECT_MAP_DOC_PATHS
    assert all(payload["source_path"].startswith(str(workspace_root / "docs")) for payload in map_payloads)
    assert all(payload["truncated"] is False for payload in map_payloads)
    assert {payload["path"] for payload in workflow_payloads} >= {
        "system/policies/run-contract.md",
        "system/policies/milestone-context-reset.md",
        "system/policies/autonomy-lanes.md",
        "system/policies/knowledge-routing.md",
        "system/policies/ui-architecture.md",
        "system/policies/environment-routing.md",
        "system/failure-patterns.md",
    }

    update = client.put(f"/api/runs/{run['id']}/artifacts/run-contract.md", json={"content": run_contract + "\n## Reviewer\nRequired.\n"})
    assert update.status_code == 200
    versions = client.get(f"/api/runs/{run['id']}/artifact-versions/run-contract.md")
    assert versions.status_code == 200
    assert versions.json()["versions"]

    audit_events = [json.loads(line)["type"] for line in client.get(f"/api/runs/{run['id']}/artifacts/events.jsonl").json()["content"].splitlines()]
    assert audit_events.count("run_contract_initialized") == 1


def test_run_creation_injects_hybrid_retrieved_chunks_and_events(tmp_path):
    client, workspace_root = make_client(tmp_path)
    (workspace_root / "app").mkdir()
    (workspace_root / "shared-ui").mkdir()
    (workspace_root / "tests").mkdir()
    (workspace_root / "app" / "main.py").write_text(
        "def unrelated():\n    return 'ignore me'\n\n"
        "def runtime_event_polling_bridge():\n"
        "    agent_console_runtime_event_polling = 'UNIQUE_BACKEND_RUNTIME_EVENT_BEACON'\n"
        "    return agent_console_runtime_event_polling\n",
        encoding="utf-8",
    )
    (workspace_root / "shared-ui" / "app.js").write_text(
        "function renderRunEvents(events) {\n"
        "  const agentConsolePolling = 'UNIQUE_UI_AGENT_CONSOLE_BEACON';\n"
        "  return events.map((event) => agentConsolePolling + event.type);\n"
        "}\n",
        encoding="utf-8",
    )
    (workspace_root / "tests" / "test_api.py").write_text(
        "def test_runtime_event_polling_regression():\n"
        "    assert 'UNIQUE_TEST_POLLING_BEACON'\n",
        encoding="utf-8",
    )
    chat_id = client.get("/api/state").json()["projects"][0]["chats"][0]["id"]

    run = client.post(
        f"/api/chats/{chat_id}/runs",
        json={"mode": "sideproject", "prompt": "fix Agent Console runtime event polling regression"},
    ).json()

    input_md = client.get(f"/api/runs/{run['id']}/artifacts/input.md").json()["content"]
    assert "## Retrieved relevant chunks" in input_md
    assert "Hybrid local retriever output" in input_md
    assert "UNIQUE_BACKEND_RUNTIME_EVENT_BEACON" in input_md
    assert "UNIQUE_UI_AGENT_CONSOLE_BEACON" in input_md
    assert "UNIQUE_TEST_POLLING_BEACON" in input_md
    assert "def unrelated" not in input_md
    events = client.get(f"/api/runs/{run['id']}/events").json()["events"]
    retrieved_payloads = [json.loads(event["payload_json"]) for event in events if event["type"] == "retrieved_context_chunk"]
    assert {payload["path"] for payload in retrieved_payloads} >= {"app/main.py", "shared-ui/app.js", "tests/test_api.py"}
    assert all(payload["score"] > 0 for payload in retrieved_payloads)
    assert all("start_line" in payload and "end_line" in payload for payload in retrieved_payloads)


def test_hybrid_retrieval_allows_helm_live_root_fallback(tmp_path, monkeypatch):
    fallback_root = tmp_path / "repo"
    (fallback_root / "app").mkdir(parents=True)
    (fallback_root / "app" / "main.py").write_text(
        "def runtime_event_polling_bridge():\n"
        "    agent_console_runtime_event_polling = 'LIVE_ROOT_FALLBACK_BEACON'\n"
        "    return agent_console_runtime_event_polling\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HELM_PROJECT_DOCS_ROOT", str(fallback_root))
    monkeypatch.delenv("HELM_WORKSPACE_ROOT", raising=False)

    chunks = load_retrieved_context_chunks(
        str(Path.home() / "ObsidianVault" / "helm" / "workspace-live"),
        "fix Agent Console runtime event polling regression",
    )

    assert chunks
    assert chunks[0]["source_path"] == str(fallback_root / "app" / "main.py")
    assert "LIVE_ROOT_FALLBACK_BEACON" in str(chunks[0]["content"])


def test_retrieved_context_uses_safe_markdown_fences_for_embedded_code_blocks():
    rendered = render_retrieved_context([
        {
            "path": "docs/HELM_SYSTEM_MAP.md",
            "source_path": "/tmp/HELM_SYSTEM_MAP.md",
            "start_line": 1,
            "end_line": 5,
            "score": 9,
            "content": "# Example\n```bash\npytest -q\n```",
        }
    ])

    assert "````\n# Example\n```bash\npytest -q\n```\n````" in rendered


def test_run_creation_truncates_large_context_map_doc_in_input_and_events(tmp_path):
    client, workspace_root = make_client(tmp_path)
    docs = workspace_root / "docs"
    docs.mkdir()
    large_ui_index = "# UI_INDEX\n\n" + ("A" * 2600) + "TAIL_SHOULD_NOT_APPEAR"
    (docs / "UI_INDEX.md").write_text(large_ui_index, encoding="utf-8")
    chat_id = client.get("/api/state").json()["projects"][0]["chats"][0]["id"]

    run = client.post(f"/api/chats/{chat_id}/runs", json={"mode": "sideproject", "prompt": "truncate map", "context_tier": "L"}).json()

    input_md = client.get(f"/api/runs/{run['id']}/artifacts/input.md").json()["content"]
    assert "docs/UI_INDEX.md" in input_md
    assert "[truncated — read targeted chunks from this file when needed]" in input_md
    assert "TAIL_SHOULD_NOT_APPEAR" not in input_md
    events = client.get(f"/api/runs/{run['id']}/events").json()["events"]
    map_payloads = [json.loads(event["payload_json"]) for event in events if event["type"] == "context_map_loaded"]
    ui_payload = next(payload for payload in map_payloads if payload["path"] == "docs/UI_INDEX.md")
    assert ui_payload["truncated"] is True
    assert ui_payload["original_size"] == len(large_ui_index)
    assert ui_payload["size"] < ui_payload["original_size"]


def test_run_creation_truncates_large_workflow_policy_in_input(tmp_path):
    client, workspace_root = make_client(tmp_path)
    large_policy = workspace_root / "system" / "policies" / "knowledge-routing.md"
    large_policy.write_text("# Knowledge Routing\n\n" + ("A" * 5000), encoding="utf-8")
    chat_id = client.get("/api/state").json()["projects"][0]["chats"][0]["id"]

    run = client.post(f"/api/chats/{chat_id}/runs", json={"mode": "sideproject", "prompt": "truncate policy", "context_tier": "L"}).json()

    input_md = client.get(f"/api/runs/{run['id']}/artifacts/input.md").json()["content"]
    assert "system/policies/knowledge-routing.md" in input_md
    assert "[truncated]" in input_md


def test_run_creation_loads_runtime_contracts_into_input_and_events(tmp_path, monkeypatch):
    contracts = tmp_path / "contracts"
    contracts.mkdir()
    soul = contracts / "SOUL.md"
    handoff = contracts / "session-health-and-handoff.md"
    dox = contracts / "AGENTS.md"
    soul.write_text("# Agent Style\n\nStart each response with a concise greeting.\n", encoding="utf-8")
    handoff.write_text("# Session health\n\nAfter 2 compressions create a handoff.\n", encoding="utf-8")
    dox.write_text("# DOX-lite\n\nKeep contracts concise and operational.\n", encoding="utf-8")
    monkeypatch.setenv("HELM_RUNTIME_CONTRACT_PATHS", os.pathsep.join(str(path) for path in [soul, handoff, dox]))
    client, _ = make_client(tmp_path)
    chat_id = client.get("/api/state").json()["projects"][0]["chats"][0]["id"]

    run = client.post(f"/api/chats/{chat_id}/runs", json={"mode": "sideproject", "prompt": "follow contracts"}).json()

    input_md = client.get(f"/api/runs/{run['id']}/artifacts/input.md").json()["content"]
    assert "## Runtime contracts" in input_md
    assert "Start each response with a concise greeting." in input_md
    assert "After 2 compressions create a handoff" in input_md
    assert "Keep contracts concise and operational" in input_md
    events = client.get(f"/api/runs/{run['id']}/events").json()["events"]
    loaded = [json.loads(event["payload_json"]) for event in events if event["type"] == "runtime_contract_loaded"]
    assert [Path(event["path"]).name for event in loaded] == ["SOUL.md", "session-health-and-handoff.md", "AGENTS.md"]


def test_latest_run_endpoint_returns_null_then_latest_run(tmp_path):
    client, _ = make_client(tmp_path)
    chat_id = client.get("/api/state").json()["projects"][0]["chats"][0]["id"]

    empty = client.get(f"/api/chats/{chat_id}/runs/latest")
    assert empty.status_code == 200
    assert empty.json() == {"run": None}

    first = client.post(f"/api/chats/{chat_id}/runs", json={"mode": "work", "prompt": "first"}).json()
    second = client.post(f"/api/chats/{chat_id}/runs", json={"mode": "work", "prompt": "second"}).json()

    latest = client.get(f"/api/chats/{chat_id}/runs/latest")
    assert latest.status_code == 200
    assert latest.json()["run"]["id"] == second["id"]
    assert latest.json()["run"]["id"] != first["id"]
    assert latest.json()["run"]["prompt"] == "second"


def test_message_then_run_creation_path_for_chat(tmp_path):
    client, _ = make_client(tmp_path)
    chat_id = client.get("/api/state").json()["projects"][0]["chats"][0]["id"]

    message = client.post(f"/api/chats/{chat_id}/messages", json={"role": "user", "content": "Navrhni ďalší krok"})
    assert message.status_code == 201

    run = client.post(f"/api/chats/{chat_id}/runs", json={"mode": "work", "prompt": message.json()["content"]})
    assert run.status_code == 201
    assert run.json()["prompt"] == "Navrhni ďalší krok"

    messages = client.get(f"/api/chats/{chat_id}/messages").json()["messages"]
    latest = client.get(f"/api/chats/{chat_id}/runs/latest").json()["run"]
    assert messages[-1]["content"] == "Navrhni ďalší krok"
    assert latest["id"] == run.json()["id"]


def test_can_read_and_update_run_artifact(tmp_path):
    client, _ = make_client(tmp_path)
    chat_id = client.get("/api/state").json()["projects"][0]["chats"][0]["id"]
    run = client.post(f"/api/chats/{chat_id}/runs", json={"mode": "work", "prompt": "run diagnostics"}).json()

    read_before = client.get(f"/api/runs/{run['id']}/artifacts/input.md")
    assert read_before.status_code == 200
    assert "run diagnostics" in read_before.json()["content"]

    updated = client.put(
        f"/api/runs/{run['id']}/artifacts/review.md",
        json={"content": "# Review\n\nSchválené."},
    )
    assert updated.status_code == 200
    assert updated.json()["name"] == "review.md"

    read_after = client.get(f"/api/runs/{run['id']}/artifacts/review.md")
    assert read_after.json()["content"] == "# Review\n\nSchválené."


def test_artifact_updates_create_readable_versions(tmp_path):
    client, _ = make_client(tmp_path)
    chat_id = client.get("/api/state").json()["projects"][0]["chats"][0]["id"]
    run = client.post(f"/api/chats/{chat_id}/runs", json={"mode": "work", "prompt": "version backups"}).json()

    original = client.get(f"/api/runs/{run['id']}/artifacts/plan.md").json()["content"]

    first = client.put(f"/api/runs/{run['id']}/artifacts/plan.md", json={"content": "# Plan\n\nFirst edit."})
    assert first.status_code == 200
    first_saved = first.json()["version_saved"]
    assert first_saved["artifact"] == "plan.md"
    assert first_saved["path"].startswith(".versions/plan.md/")

    versions_after_first = client.get(f"/api/runs/{run['id']}/artifact-versions/plan.md")
    assert versions_after_first.status_code == 200
    versions = versions_after_first.json()["versions"]
    assert [version["id"] for version in versions] == [first_saved["id"]]

    original_version = client.get(f"/api/runs/{run['id']}/artifact-versions/plan.md", params={"version": first_saved["id"]})
    assert original_version.status_code == 200
    assert original_version.json()["content"] == original

    second = client.put(f"/api/runs/{run['id']}/artifacts/plan.md", json={"content": "# Plan\n\nSecond edit."})
    assert second.status_code == 200
    second_saved = second.json()["version_saved"]
    assert second_saved["id"] != first_saved["id"]

    versions_after_second = client.get(f"/api/runs/{run['id']}/artifact-versions/plan.md").json()["versions"]
    assert [version["id"] for version in versions_after_second] == [first_saved["id"], second_saved["id"]]

    previous_version = client.get(f"/api/runs/{run['id']}/artifact-versions/plan.md", params={"version": second_saved["id"]})
    assert previous_version.status_code == 200
    assert previous_version.json()["content"] == "# Plan\n\nFirst edit."


def test_version_files_are_hidden_from_normal_artifact_list(tmp_path):
    client, _ = make_client(tmp_path)
    chat_id = client.get("/api/state").json()["projects"][0]["chats"][0]["id"]
    run = client.post(f"/api/chats/{chat_id}/runs", json={"mode": "work", "prompt": "hidden versions"}).json()

    updated = client.put(f"/api/runs/{run['id']}/artifacts/review.md", json={"content": "# Review\n\nEdited."})
    assert updated.status_code == 200

    artifacts = client.get(f"/api/runs/{run['id']}/artifacts")
    assert artifacts.status_code == 200
    names = {item["name"] for item in artifacts.json()["artifacts"]}
    assert ".versions" not in names
    assert not any(".versions" in name for name in names)


def test_events_jsonl_is_readable_but_not_writable(tmp_path):
    client, _ = make_client(tmp_path)
    chat_id = client.get("/api/state").json()["projects"][0]["chats"][0]["id"]
    run = client.post(f"/api/chats/{chat_id}/runs", json={"mode": "work", "prompt": "run diagnostics"}).json()

    read_events = client.get(f"/api/runs/{run['id']}/artifacts/events.jsonl")
    assert read_events.status_code == 200
    original_content = read_events.json()["content"]
    assert "artifacts_initialized" in original_content

    blocked = client.put(
        f"/api/runs/{run['id']}/artifacts/events.jsonl",
        json={"content": "corrupt audit log"},
    )
    assert blocked.status_code == 403

    read_after = client.get(f"/api/runs/{run['id']}/artifacts/events.jsonl")
    assert read_after.json()["content"] == original_content

    versions = client.get(f"/api/runs/{run['id']}/artifact-versions/events.jsonl")
    assert versions.status_code == 403


def test_put_artifact_path_traversal_is_blocked(tmp_path):
    client, _ = make_client(tmp_path)
    chat_id = client.get("/api/state").json()["projects"][0]["chats"][0]["id"]
    run = client.post(f"/api/chats/{chat_id}/runs", json={"mode": "work", "prompt": "run diagnostics"}).json()

    response = client.put(f"/api/runs/{run['id']}/artifacts/../review.md", json={"content": "bad"})

    assert response.status_code in {400, 403, 404}


def test_review_gate_endpoints_record_events_and_status(tmp_path):
    client, _ = make_client(tmp_path)
    chat_id = client.get("/api/state").json()["projects"][0]["chats"][0]["id"]
    run = client.post(f"/api/chats/{chat_id}/runs", json={"mode": "work", "prompt": "run diagnostics"}).json()

    approved = client.post(f"/api/runs/{run['id']}/approve", json={"note": "pokračuj"})
    assert approved.status_code == 200
    assert approved.json()["status"] == "approved"
    latest_after_approve = client.get(f"/api/chats/{chat_id}/runs/latest")
    assert latest_after_approve.status_code == 200
    assert latest_after_approve.json()["run"]["id"] == run["id"]
    assert latest_after_approve.json()["run"]["status"] == "approved"

    rerun = client.post(f"/api/runs/{run['id']}/rerun-stage", json={"stage": "plan", "reason": "uprav scope"})
    assert rerun.status_code == 200
    assert rerun.json()["status"] == "queued"
    latest_after_rerun = client.get(f"/api/chats/{chat_id}/runs/latest")
    assert latest_after_rerun.status_code == 200
    assert latest_after_rerun.json()["run"]["id"] == run["id"]
    assert latest_after_rerun.json()["run"]["status"] == "queued"

    events = client.get(f"/api/runs/{run['id']}/events").json()["events"]
    assert [event["type"] for event in events][-2:] == ["approved", "rerun_stage_requested"]


def test_artifact_path_traversal_is_blocked(tmp_path):
    client, _ = make_client(tmp_path)
    chat_id = client.get("/api/state").json()["projects"][0]["chats"][0]["id"]
    run = client.post(f"/api/chats/{chat_id}/runs", json={"mode": "work", "prompt": "run diagnostics"}).json()

    response = client.get(f"/api/runs/{run['id']}/artifacts/../secret.txt")

    assert response.status_code in {400, 404}


def test_tutor_policy_endpoint_is_removed(tmp_path):
    client, _ = make_client(tmp_path)

    response = client.post("/api/tutor/classify", json={"text": "Sprav mi hotovú prezentáciu z dejepisu"})

    assert response.status_code == 404


def test_shared_ui_does_not_expose_learning_tutor_gate(tmp_path):
    client, _ = make_client(tmp_path)

    response = client.get("/")

    assert response.status_code == 200
    html = response.text
    assert "data-tutor-boundary" not in html
    assert "/api/tutor/classify" not in html
    assert "Tutor AI boundary" not in html


def test_learning_run_no_longer_blocks_finished_schoolwork(tmp_path):
    client, _ = make_client(tmp_path)
    project = client.post("/api/projects", json={"name": "Dejepis", "mode_default": "learning"}).json()
    state = client.get("/api/state", params={"mode": "learning"}).json()
    chat_id = next(row for row in state["projects"] if row["id"] == project["id"])["chats"][0]["id"]

    response = client.post(f"/api/chats/{chat_id}/runs", json={"mode": "learning", "prompt": "Sprav mi hotovú prezentáciu z dejepisu"})

    assert response.status_code == 201
    run = response.json()
    assert run["mode"] == "learning"
    latest = client.get(f"/api/chats/{chat_id}/runs/latest").json()["run"]
    assert latest["id"] == run["id"]


def test_learning_run_does_not_inject_tutor_workflow_contract_or_event(tmp_path):
    client, workspace_root = make_client(tmp_path)
    project = client.post("/api/projects", json={"name": "Matika learning", "mode_default": "learning"}).json()
    state = client.get("/api/state", params={"mode": "learning"}).json()
    chat_id = next(row for row in state["projects"] if row["id"] == project["id"])["chats"][0]["id"]

    response = client.post(f"/api/chats/{chat_id}/runs", json={"mode": "learning", "prompt": "Vysvetli mi kvadratické rovnice a daj mi otázky"})

    assert response.status_code == 201
    run = response.json()
    input_md = client.get(f"/api/runs/{run['id']}/artifacts/input.md").json()["content"]
    assert "## Tutor workflow" not in input_md
    assert "Classification: safe_tutor" not in input_md

    run_contract = client.get(f"/api/runs/{run['id']}/artifacts/run-contract.md").json()["content"]
    assert "## Tutor Workflow" not in run_contract
    assert "Hotový školský výstup" not in run_contract

    events = client.get(f"/api/runs/{run['id']}/events").json()["events"]
    event_types = [event["type"] for event in events]
    assert "tutor_workflow_initialized" not in event_types
    assert (workspace_root / "runs").exists()


def test_client_cannot_bypass_mode_by_posting_learning_mode_to_non_learning_chat(tmp_path):
    client, _ = make_client(tmp_path)
    chat_id = client.get("/api/state").json()["projects"][0]["chats"][0]["id"]

    response = client.post(f"/api/chats/{chat_id}/runs", json={"mode": "learning", "prompt": "Sprav mi hotovú prezentáciu z dejepisu"})

    assert response.status_code == 201
    assert response.json()["mode"] == "sideproject"


def test_run_creation_initializes_stage_workflow(tmp_path):
    client, _ = make_client(tmp_path)
    chat_id = client.get("/api/state").json()["projects"][0]["chats"][0]["id"]

    run = client.post(f"/api/chats/{chat_id}/runs", json={"mode": "work", "prompt": "stage flow"}).json()

    assert run["current_stage"] == "input"
    assert run["stage_status"] == "active"
    stages = client.get(f"/api/runs/{run['id']}/stages")
    assert stages.status_code == 200
    stage_rows = stages.json()["stages"]
    assert [stage["name"] for stage in stage_rows] == ["input", "plan", "execute", "review", "final_output"]
    assert [stage["status"] for stage in stage_rows] == ["active", "pending", "pending", "pending", "pending"]


def test_advance_stage_moves_forward_and_records_events(tmp_path):
    client, _ = make_client(tmp_path)
    chat_id = client.get("/api/state").json()["projects"][0]["chats"][0]["id"]
    run = client.post(f"/api/chats/{chat_id}/runs", json={"mode": "work", "prompt": "advance me"}).json()

    advanced = client.post(f"/api/runs/{run['id']}/advance-stage", json={"note": "input captured"})

    assert advanced.status_code == 200
    data = advanced.json()
    assert data["run"]["current_stage"] == "plan"
    assert data["run"]["status"] == "running"
    assert [stage["status"] for stage in data["stages"]] == ["completed", "active", "pending", "pending", "pending"]

    events = client.get(f"/api/runs/{run['id']}/events").json()["events"]
    event_types = [event["type"] for event in events]
    assert "stage_advanced" in event_types
    assert event_types[-1] == "stage_started"
    payloads = [json.loads(event["payload_json"]) for event in events if event["type"] == "stage_advanced"]
    assert payloads[-1]["from"] == "input"
    assert payloads[-1]["to"] == "plan"

    audit_log = client.get(f"/api/runs/{run['id']}/artifacts/events.jsonl").json()["content"]
    assert "stage_advanced" in audit_log
    assert "stage_started" in audit_log


def test_advance_stage_can_finalize_run(tmp_path):
    client, _ = make_client(tmp_path)
    chat_id = client.get("/api/state").json()["projects"][0]["chats"][0]["id"]
    run = client.post(f"/api/chats/{chat_id}/runs", json={"mode": "work", "prompt": "finish me"}).json()

    for _ in range(5):
        response = client.post(f"/api/runs/{run['id']}/advance-stage", json={})
        assert response.status_code == 200

    data = response.json()
    assert data["run"]["current_stage"] == "final_output"
    assert data["run"]["status"] == "completed"
    assert [stage["status"] for stage in data["stages"]] == ["completed", "completed", "completed", "completed", "completed"]


def test_rerun_stage_resets_current_stage(tmp_path):
    client, _ = make_client(tmp_path)
    chat_id = client.get("/api/state").json()["projects"][0]["chats"][0]["id"]
    run = client.post(f"/api/chats/{chat_id}/runs", json={"mode": "work", "prompt": "rerun flow"}).json()
    client.post(f"/api/runs/{run['id']}/advance-stage", json={})
    client.post(f"/api/runs/{run['id']}/advance-stage", json={})

    rerun = client.post(f"/api/runs/{run['id']}/rerun-stage", json={"stage": "plan", "reason": "scope changed"})

    assert rerun.status_code == 200
    assert rerun.json()["current_stage"] == "plan"
    assert rerun.json()["stage_status"] == "active"
    assert rerun.json()["status"] == "queued"
    stages = client.get(f"/api/runs/{run['id']}/stages").json()["stages"]
    assert [stage["status"] for stage in stages] == ["completed", "active", "pending", "pending", "pending"]


def test_approve_review_stage_moves_to_final_output(tmp_path):
    client, _ = make_client(tmp_path)
    chat_id = client.get("/api/state").json()["projects"][0]["chats"][0]["id"]
    run = client.post(f"/api/chats/{chat_id}/runs", json={"mode": "work", "prompt": "review gate"}).json()
    for _ in range(3):
        client.post(f"/api/runs/{run['id']}/advance-stage", json={})

    at_review = client.get(f"/api/runs/{run['id']}/stages").json()["run"]
    assert at_review["current_stage"] == "review"

    approved = client.post(f"/api/runs/{run['id']}/approve", json={"note": "ship it"})

    assert approved.status_code == 200
    assert approved.json()["status"] == "approved"
    assert approved.json()["current_stage"] == "final_output"


def test_runtime_start_without_confirmation_runs_immediately(tmp_path):
    fake_runtime = tmp_path / "direct_runtime.py"
    fake_runtime.write_text("print('Direct runtime odpoveď.')\n", encoding="utf-8")
    client, _ = make_client(tmp_path, runtime_command=[sys.executable, str(fake_runtime)])
    chat_id = client.get("/api/state").json()["projects"][0]["chats"][0]["id"]
    run = client.post(f"/api/chats/{chat_id}/runs", json={"mode": "work", "prompt": "direct runtime"}).json()

    response = client.post(f"/api/runs/{run['id']}/runtime/start", json={"timeout_seconds": 10})

    assert response.status_code == 202
    data = response.json()
    assert "approval" not in data
    assert "requires_approval" not in data
    assert data["run"]["status"] == "running"
    assert data["runtime"]["background"] is True
    wait_for_run_status(client, run["id"], "review")
    messages = client.get(f"/api/chats/{chat_id}/messages").json()["messages"]
    assert messages[-1]["content"] == "Direct runtime odpoveď."
    events = client.get(f"/api/runs/{run['id']}/events").json()["events"]
    event_types = [event["type"] for event in events]
    assert "runtime_started" in event_types
    assert "runtime_completed" in event_types
    assert "approval_requested" not in event_types
    assert "approval_approved" not in event_types
    assert "approval_denied" not in event_types


def test_runtime_start_accepts_long_helm_timeout_for_complex_tasks(tmp_path):
    fake_runtime = tmp_path / "sleepy_runtime.py"
    fake_runtime.write_text("print('timeout accepted')\n", encoding="utf-8")
    client, _ = make_client(tmp_path, runtime_command=[sys.executable, str(fake_runtime)])
    project = client.get("/api/state?mode=sideproject").json()["projects"][0]
    chat_id = project["chats"][0]["id"]
    run = client.post(f"/api/chats/{chat_id}/runs", json={"mode": "sideproject", "prompt": "complex ui redesign"}).json()

    response = client.post(
        f"/api/runs/{run['id']}/runtime/start",
        json={"timeout_seconds": 900, "confirmed_external_runtime": False},
    )

    assert response.status_code == 202
    assert response.json()["runtime"]["background"] is True


def test_approval_endpoints_are_removed(tmp_path):
    client, _ = make_client(tmp_path, runtime_command=[sys.executable, "-c", "print('ok')"])
    chat_id = client.get("/api/state").json()["projects"][0]["chats"][0]["id"]
    run = client.post(f"/api/chats/{chat_id}/runs", json={"mode": "work", "prompt": "no approvals"}).json()

    assert client.get("/api/approvals").status_code == 404
    assert client.get(f"/api/runs/{run['id']}/approvals").status_code == 404
    assert client.post(f"/api/runs/{run['id']}/approvals/1/approve", json={"note": "x"}).status_code == 404
    assert client.post(f"/api/runs/{run['id']}/approvals/1/deny", json={"note": "x"}).status_code == 404


def test_chat_run_loop_persists_user_message_starts_runtime_and_saves_reply(tmp_path):
    fake_runtime = tmp_path / "chat_run_runtime.py"
    fake_runtime.write_text("import os\nprint('Bob odpovedá na: ' + os.environ['HELM_RUN_PROMPT'])\n", encoding="utf-8")
    client, _ = make_client(tmp_path, runtime_command=[sys.executable, str(fake_runtime)])
    chat_id = client.get("/api/state").json()["projects"][0]["chats"][0]["id"]

    response = client.post(
        f"/api/chats/{chat_id}/run-loop",
        json={"content": "Sprav krátky smoke test", "start_runtime": True, "timeout_seconds": 10},
    )

    assert response.status_code == 201
    data = response.json()
    assert "approval" not in data
    assert "requires_approval" not in data
    assert data["message"]["role"] == "user"
    assert data["message"]["content"] == "Sprav krátky smoke test"
    assert data["run"]["prompt"] == "Sprav krátky smoke test"
    assert data["run"]["status"] == "running"
    assert data["runtime"]["background"] is True
    wait_for_run_status(client, data["run"]["id"], "review")
    messages = client.get(f"/api/chats/{chat_id}/messages").json()["messages"]
    assert [(message["role"], message["content"]) for message in messages] == [
        ("user", "Sprav krátky smoke test"),
        ("assistant", "Bob odpovedá na: Sprav krátky smoke test"),
    ]
    events = client.get(f"/api/runs/{data['run']['id']}/events").json()["events"]
    event_types = [event["type"] for event in events]
    assert "runtime_started" in event_types
    assert "runtime_completed" in event_types
    assert "approval_requested" not in event_types


def test_chat_context_meter_returns_real_counts(tmp_path):
    client, _ = make_client(tmp_path)
    state = client.get("/api/state").json()
    project = state["projects"][0]
    chat_id = project["chats"][0]["id"]
    client.post(f"/api/chats/{chat_id}/messages", json={"role": "user", "content": "abc"})
    client.post(f"/api/projects/{project['id']}/context", json={"content": "context text"})

    response = client.get(f"/api/chats/{chat_id}/context-meter")

    assert response.status_code == 200
    data = response.json()
    assert data["used_chars"] >= len("abc") + len("context text")
    assert data["limit_chars"] > data["used_chars"]
    assert 0 < data["percent"] <= 100


def test_default_runtime_command_is_unconfigured_without_override(monkeypatch):
    monkeypatch.delenv("HELM_RUNTIME_COMMAND", raising=False)
    assert _runtime_command() == ()


def test_runtime_command_env_override_still_takes_precedence(monkeypatch):
    monkeypatch.setenv("HELM_RUNTIME_COMMAND", "custom-agent -z")
    assert _runtime_command() == ("custom-agent", "-z")


def test_runtime_start_requires_configured_command(tmp_path, monkeypatch):
    monkeypatch.delenv("HELM_RUNTIME_COMMAND", raising=False)
    client, _ = make_client(tmp_path)
    chat_id = client.get("/api/state").json()["projects"][0]["chats"][0]["id"]
    run = client.post(f"/api/chats/{chat_id}/runs", json={"mode": "work", "prompt": "test"}).json()

    response = client.post(
        f"/api/runs/{run['id']}/runtime/start",
        json={"timeout_seconds": 10, "confirmed_external_runtime": True},
    )

    assert response.status_code == 503
    assert "HELM_RUNTIME_COMMAND" in response.json()["detail"]


def test_runtime_bridge_dispatches_input_path_launcher_to_runtime_command(tmp_path):
    fake_runtime = tmp_path / "capture_runtime_input.py"
    captured_prompt = tmp_path / "captured_prompt.md"
    captured_input = tmp_path / "captured_input.md"
    fake_runtime.write_text(
        "import os, pathlib, sys\n"
        f"pathlib.Path({str(captured_prompt)!r}).write_text(sys.argv[1], encoding='utf-8')\n"
        f"pathlib.Path({str(captured_input)!r}).write_text(pathlib.Path(os.environ['HELM_INPUT_PATH']).read_text(encoding='utf-8'), encoding='utf-8')\n"
        "print('captured runtime launcher')\n",
        encoding="utf-8",
    )
    client, workspace_root = make_client(tmp_path, runtime_command=[sys.executable, str(fake_runtime)])
    (workspace_root / "app").mkdir()
    (workspace_root / "app" / "main.py").write_text(
        "def migrate_runtime_bridge():\n"
        "    return 'UNIQUE_DISPATCH_RETRIEVED_CHUNK'\n",
        encoding="utf-8",
    )
    chat_id = client.get("/api/state").json()["projects"][0]["chats"][0]["id"]
    run = client.post(f"/api/chats/{chat_id}/runs", json={"mode": "sideproject", "prompt": "Migrate Helm runtime"}).json()

    started = client.post(f"/api/runs/{run['id']}/runtime/start", json={"timeout_seconds": 10, "confirmed_external_runtime": True})

    assert started.status_code == 202
    wait_for_run_status(client, run["id"], "review")
    launcher_prompt = captured_prompt.read_text(encoding="utf-8")
    dispatched_input = captured_input.read_text(encoding="utf-8")
    assert "read the complete Helm runtime input" in launcher_prompt
    assert "HELM_INPUT_PATH" in launcher_prompt
    assert "context navigation maps" in launcher_prompt
    assert "hybrid retrieved source chunks" in launcher_prompt
    assert "HELM_SYSTEM_MAP" in launcher_prompt
    assert "file/API/UI indexes" in launcher_prompt
    assert "input.md" in launcher_prompt
    assert "HELM_RUNTIME_EVENTS_JSONL" in launcher_prompt
    assert "never raw chain-of-thought" in launcher_prompt
    assert len(launcher_prompt) < 1200
    assert dispatched_input.startswith("# Input")
    assert "## Context navigation maps" in dispatched_input
    assert "docs/HELM_SYSTEM_MAP.md" in dispatched_input
    assert "## Retrieved relevant chunks" in dispatched_input
    assert "UNIQUE_DISPATCH_RETRIEVED_CHUNK" in dispatched_input
    assert "## Workflow governance" in dispatched_input
    assert "## Runtime contracts" in dispatched_input
    assert "system/policies/environment-routing.md" in dispatched_input
    assert "Prompt:\n\nMigrate Helm runtime" in dispatched_input


def test_runtime_bridge_runs_configured_command_and_writes_output_artifact(tmp_path):
    fake_runtime = tmp_path / "fake_runtime.py"
    fake_runtime.write_text(
        "import os\n"
        "print(f\"Bob runtime saw run {os.environ['HELM_RUN_ID']}\")\n"
        "print(f\"Prompt: {os.environ['HELM_RUN_PROMPT']}\")\n"
        "print(f\"Input exists: {os.path.exists(os.environ['HELM_INPUT_PATH'])}\")\n"
        "print(f\"Run contract exists: {os.path.exists(os.environ['HELM_RUN_CONTRACT_PATH'])}\")\n",
        encoding="utf-8",
    )
    client, _ = make_client(tmp_path, runtime_command=[sys.executable, str(fake_runtime)])
    helm_project = next(project for project in client.get("/api/state").json()["projects"] if project["slug"] == "helm")
    chat_id = helm_project["chats"][0]["id"]
    run = client.post(f"/api/chats/{chat_id}/runs", json={"mode": "sideproject", "prompt": "M7 runtime smoke"}).json()

    started = client.post(
        f"/api/runs/{run['id']}/runtime/start",
        json={"timeout_seconds": 10, "confirmed_external_runtime": True},
    )

    assert started.status_code == 202
    data = started.json()
    assert data["run"]["status"] == "running"
    assert data["runtime"]["background"] is True
    final_run = wait_for_run_status(client, run["id"], "review")
    # M18: a clean run auto-advances past review to final_output
    assert final_run["current_stage"] == "final_output"

    output = client.get(f"/api/runs/{run['id']}/artifacts/output.md")
    assert "Bob runtime saw run" in output.json()["content"]
    assert "Input exists: True" in output.json()["content"]
    assert "Run contract exists: True" in output.json()["content"]
    assert output.status_code == 200
    assert "# Runtime output" in output.json()["content"]
    assert "M7 runtime smoke" in output.json()["content"]

    events = client.get(f"/api/runs/{run['id']}/events").json()["events"]
    event_types = [event["type"] for event in events]
    expected_sequence = ["runtime_started", "stage_started", "runtime_completed", "stage_started"]
    start = event_types.index("runtime_started")
    assert event_types[start : start + len(expected_sequence)] == expected_sequence
    # auto-summary (M15a) fires after the review stage starts
    assert "run_summary_generated" in event_types
    completed_payload = json.loads(next(e for e in events if e["type"] == "runtime_completed")["payload_json"])
    assert completed_payload["exit_code"] == 0


def test_runtime_bridge_ingests_real_tool_activity_events(tmp_path):
    fake_runtime = tmp_path / "event_runtime.py"
    fake_runtime.write_text(
        "import json, os\n"
        "path = os.environ['HELM_RUNTIME_EVENTS_JSONL']\n"
        "with open(path, 'a', encoding='utf-8') as f:\n"
        "    f.write(json.dumps({'type': 'thought_summary', 'payload': {'message': 'Planning next safe local step'}}) + '\\n')\n"
        "    f.write(json.dumps({'type': 'tool_activity', 'payload': {'name': 'read_file', 'path': 'system/policies/dox-lite.md', 'status': 'ok'}}) + '\\n')\n"
        "print('Runtime with events done.')\n",
        encoding="utf-8",
    )
    client, _ = make_client(tmp_path, runtime_command=[sys.executable, str(fake_runtime)])
    chat_id = client.get("/api/state").json()["projects"][0]["chats"][0]["id"]
    run = client.post(f"/api/chats/{chat_id}/runs", json={"mode": "sideproject", "prompt": "emit console events"}).json()

    started = client.post(f"/api/runs/{run['id']}/runtime/start", json={"timeout_seconds": 10, "confirmed_external_runtime": True})

    assert started.status_code == 202
    wait_for_run_status(client, run["id"], "review")
    events = client.get(f"/api/runs/{run['id']}/events").json()["events"]
    duplicate_check = client.get(f"/api/runs/{run['id']}/events").json()["events"]
    assert len(duplicate_check) == len(events)
    payloads = {event["type"]: json.loads(event["payload_json"]) for event in events}
    assert payloads["thought_summary"]["message"] == "Planning next safe local step"
    assert payloads["tool_activity"]["name"] == "read_file"
    assert payloads["tool_activity"]["path"] == "system/policies/dox-lite.md"
    audit_log = client.get(f"/api/runs/{run['id']}/artifacts/events.jsonl").json()["content"]
    assert "tool_activity" in audit_log


def test_runtime_bridge_appends_plain_assistant_message_to_chat(tmp_path):
    fake_runtime = tmp_path / "plain_runtime.py"
    fake_runtime.write_text("print('Hello, this is a plain response.')\n", encoding="utf-8")
    client, _ = make_client(tmp_path, runtime_command=[sys.executable, str(fake_runtime)])
    chat_id = client.get("/api/state").json()["projects"][0]["chats"][0]["id"]
    user_message = client.post(f"/api/chats/{chat_id}/messages", json={"role": "user", "content": "ahoj"})
    assert user_message.status_code == 201
    run = client.post(f"/api/chats/{chat_id}/runs", json={"mode": "work", "prompt": "ahoj"}).json()

    started = client.post(
        f"/api/runs/{run['id']}/runtime/start",
        json={"timeout_seconds": 10, "confirmed_external_runtime": True},
    )

    assert started.status_code == 202
    wait_for_run_status(client, run["id"], "review")
    messages = client.get(f"/api/chats/{chat_id}/messages").json()["messages"]
    assert [(message["role"], message["content"]) for message in messages] == [
        ("user", "ahoj"),
        ("assistant", "Hello, this is a plain response."),
    ]


def test_runtime_bridge_does_not_append_duplicate_assistant_message_on_second_start(tmp_path):
    fake_runtime = tmp_path / "plain_runtime.py"
    fake_runtime.write_text("import time\ntime.sleep(0.4)\nprint('Jedna odpoveď.')\n", encoding="utf-8")
    client, _ = make_client(tmp_path, runtime_command=[sys.executable, str(fake_runtime)])
    chat_id = client.get("/api/state").json()["projects"][0]["chats"][0]["id"]
    client.post(f"/api/chats/{chat_id}/messages", json={"role": "user", "content": "ahoj"})
    run = client.post(f"/api/chats/{chat_id}/runs", json={"mode": "work", "prompt": "ahoj"}).json()
    first = client.post(
        f"/api/runs/{run['id']}/runtime/start",
        json={"timeout_seconds": 10, "confirmed_external_runtime": True},
    )
    assert first.status_code == 202

    second = client.post(
        f"/api/runs/{run['id']}/runtime/start",
        json={"timeout_seconds": 10, "confirmed_external_runtime": True},
    )

    assert second.status_code == 409
    wait_for_run_status(client, run["id"], "review")
    messages = client.get(f"/api/chats/{chat_id}/messages").json()["messages"]
    assert [message["role"] for message in messages] == ["user", "assistant"]


def test_runtime_bridge_records_failure_and_keeps_audit_log(tmp_path):
    failing_runtime = tmp_path / "failing_runtime.py"
    failing_runtime.write_text("import sys\nprint('boom before exit')\nsys.exit(7)\n", encoding="utf-8")
    client, _ = make_client(tmp_path, runtime_command=[sys.executable, str(failing_runtime)])
    chat_id = client.get("/api/state").json()["projects"][0]["chats"][0]["id"]
    run = client.post(f"/api/chats/{chat_id}/runs", json={"mode": "work", "prompt": "fail runtime"}).json()

    started = client.post(
        f"/api/runs/{run['id']}/runtime/start",
        json={"timeout_seconds": 10, "confirmed_external_runtime": True},
    )

    assert started.status_code == 202
    data = started.json()
    assert data["run"]["status"] == "running"
    final_run = wait_for_run_status(client, run["id"], "failed")
    assert final_run["current_stage"] == "execute"
    assert "Runtime bridge failed" in final_run["error"]

    output = client.get(f"/api/runs/{run['id']}/artifacts/output.md").json()["content"]
    assert "boom before exit" in output

    audit_log = client.get(f"/api/runs/{run['id']}/artifacts/events.jsonl").json()["content"]
    assert "runtime_started" in audit_log
    assert "runtime_failed" in audit_log

def test_project_workspace_returns_context_sections_and_project_run_history(tmp_path):
    client, _ = make_client(tmp_path)
    state = client.get("/api/state", params={"mode": "sideproject"}).json()
    helm = next(project for project in state["projects"] if project["slug"] == "helm")
    chat_id = helm["chats"][0]["id"]
    run = client.post(f"/api/chats/{chat_id}/runs", json={"mode": "work", "prompt": "M9 history"}).json()

    response = client.get(f"/api/projects/{helm['id']}/workspace")

    assert response.status_code == 200
    data = response.json()
    assert data["project"]["id"] == helm["id"]
    assert set(data) >= {"project", "context", "goals", "tasks", "files", "notes", "stage_defaults", "run_history"}
    assert "CONTEXT.md" in data["context"]["path"]
    assert [row["id"] for row in data["run_history"]] == [run["id"]]


def test_can_update_project_context_file(tmp_path):
    client, workspace_root = make_client(tmp_path)
    project = client.post("/api/projects", json={"name": "Context Project", "mode_default": "work"}).json()
    content = "# Context Project\n\nCurrent focus: M9 workspace."

    updated = client.put(f"/api/projects/{project['id']}/context", json={"content": content})

    assert updated.status_code == 200
    data = updated.json()
    assert data["content"] == content
    expected = workspace_root / "domains" / "ai-it" / "active" / "context-project" / "CONTEXT.md"
    assert data["path"] == str(expected)
    assert expected.read_text(encoding="utf-8") == content


def test_project_goals_tasks_notes_files_and_stage_defaults_are_scoped(tmp_path):
    client, _ = make_client(tmp_path)
    alpha = client.post("/api/projects", json={"name": "Alpha", "mode_default": "work"}).json()
    beta = client.post("/api/projects", json={"name": "Beta", "mode_default": "work"}).json()

    goal = client.post(f"/api/projects/{alpha['id']}/goals", json={"title": "Ship M9"})
    task = client.post(f"/api/projects/{alpha['id']}/tasks", json={"title": "Write tests", "priority": 1})
    note = client.post(f"/api/projects/{alpha['id']}/notes", json={"title": "Decision", "content": "Keep M9 small"})
    file_row = client.post(f"/api/projects/{alpha['id']}/files", json={"path": "CONTEXT.md", "label": "Project context"})
    default = client.put(
        f"/api/projects/{alpha['id']}/stage-defaults/plan",
        json={"instructions": "Inspect project context first", "artifact_template": "# Plan\n"},
    )
    completed = client.patch(f"/api/projects/{alpha['id']}/tasks/{task.json()['id']}", json={"status": "done"})

    assert goal.status_code == task.status_code == note.status_code == file_row.status_code == 201
    assert default.status_code == 200
    assert completed.status_code == 200
    assert completed.json()["status"] == "done"

    alpha_workspace = client.get(f"/api/projects/{alpha['id']}/workspace").json()
    beta_workspace = client.get(f"/api/projects/{beta['id']}/workspace").json()
    assert [row["title"] for row in alpha_workspace["goals"]] == ["Ship M9"]
    assert [row["status"] for row in alpha_workspace["tasks"]] == ["done"]
    assert [row["title"] for row in alpha_workspace["notes"]] == ["Decision"]
    assert [row["path"] for row in alpha_workspace["files"]] == ["CONTEXT.md"]
    assert next(row for row in alpha_workspace["stage_defaults"] if row["stage"] == "plan")["instructions"] == "Inspect project context first"
    assert beta_workspace["goals"] == []
    assert beta_workspace["tasks"] == []
    assert beta_workspace["notes"] == []
    assert beta_workspace["files"] == []


def test_project_file_path_traversal_is_blocked(tmp_path):
    client, _ = make_client(tmp_path)
    project = client.post("/api/projects", json={"name": "Safe Files", "mode_default": "work"}).json()

    response = client.post(f"/api/projects/{project['id']}/files", json={"path": "../secret.txt", "label": "bad"})

    assert response.status_code == 400


def test_run_creation_uses_chat_project_mode_and_includes_project_context(tmp_path):
    client, workspace_root = make_client(tmp_path)
    project = client.post("/api/projects", json={"name": "School Context", "mode_default": "learning"}).json()
    context_path = workspace_root / "domains" / "school" / "active" / "school-context" / "CONTEXT.md"
    context_path.write_text("# School Context\n\nSCHOOL_ONLY_CONTEXT", encoding="utf-8")
    state = client.get("/api/state", params={"mode": "learning"}).json()
    chat_id = next(row for row in state["projects"] if row["id"] == project["id"])["chats"][0]["id"]

    run = client.post(f"/api/chats/{chat_id}/runs", json={"mode": "work", "prompt": "use context"})

    assert run.status_code == 201
    data = run.json()
    assert data["mode"] == "learning"
    input_md = client.get(f"/api/runs/{data['id']}/artifacts/input.md").json()["content"]
    assert "SCHOOL_ONLY_CONTEXT" in input_md

def test_seeded_project_workspace_context_paths_are_under_workspace_root(tmp_path):
    client, workspace_root = make_client(tmp_path)
    projects = client.get("/api/state").json()["projects"]

    for project in projects:
        root = Path(project["root_path"])
        assert root == workspace_root or workspace_root in root.parents
        workspace = client.get(f"/api/projects/{project['id']}/workspace").json()
        context_path = Path(workspace["context"]["path"])
        assert context_path == root / "CONTEXT.md"
        assert context_path == workspace_root or workspace_root in context_path.parents



def test_everyday_workspace_starts_empty_and_exposes_next_action(tmp_path):
    client, _ = make_client(tmp_path)

    response = client.get("/api/everyday")

    assert response.status_code == 200
    data = response.json()
    assert data["today"] == []
    assert data["inbox"] == []
    assert data["waiting"] == []
    assert data["later"] == []
    assert data["open_loops"] == []
    assert data["next_action"] == {"title": "Zachyť prvú vec do Inboxu", "source": "empty"}


def test_can_quick_capture_today_item_and_complete_it(tmp_path):
    client, _ = make_client(tmp_path)

    created = client.post("/api/everyday/items", json={"title": "Otestovať Helm UI", "bucket": "today", "kind": "task", "priority": 1})

    assert created.status_code == 201
    item = created.json()
    assert item["title"] == "Otestovať Helm UI"
    assert item["bucket"] == "today"
    assert item["status"] == "open"
    assert item["project_id"] is None

    listed = client.get("/api/everyday").json()
    assert [row["title"] for row in listed["today"]] == ["Otestovať Helm UI"]
    assert listed["next_action"]["title"] == "Otestovať Helm UI"

    done = client.patch(f"/api/everyday/items/{item['id']}", json={"status": "done"})

    assert done.status_code == 200
    assert done.json()["status"] == "done"
    assert client.get("/api/everyday").json()["today"] == []


def test_everyday_item_can_be_project_linked_without_becoming_project_task(tmp_path):
    client, _ = make_client(tmp_path)
    helm = next(project for project in client.get("/api/state").json()["projects"] if project["slug"] == "helm")

    created = client.post("/api/everyday/items", json={"title": "Nápad na M9+", "bucket": "inbox", "kind": "note", "project_id": helm["id"]})

    assert created.status_code == 201
    item = created.json()
    assert item["project_id"] == helm["id"]
    assert item["project_name"] == "Helm"
    workspace = client.get(f"/api/projects/{helm['id']}/workspace").json()
    assert "Nápad na M9+" not in [task["title"] for task in workspace["tasks"]]


def test_everyday_rejects_unknown_bucket_and_missing_item(tmp_path):
    client, _ = make_client(tmp_path)

    bad = client.post("/api/everyday/items", json={"title": "bad", "bucket": "project"})
    blank = client.post("/api/everyday/items", json={"title": "   ", "bucket": "inbox"})
    missing = client.patch("/api/everyday/items/9999", json={"status": "done"})

    assert bad.status_code == 422
    assert blank.status_code == 422
    assert missing.status_code == 404


def test_m10_can_create_goal_vision_and_hierarchical_project_goals(tmp_path):
    client, _ = make_client(tmp_path)
    project = client.get("/api/state", params={"mode": "sideproject"}).json()["projects"][0]

    vision_response = client.post(
        "/api/goals/visions",
        json={"title": "AI/IT smer", "body": "Vybudovať vlastný AI operačný systém."},
    )
    assert vision_response.status_code == 201
    vision = vision_response.json()
    assert vision["title"] == "AI/IT smer"
    assert vision["status"] == "active"

    outcome = client.post(
        f"/api/projects/{project['id']}/goals",
        json={
            "title": "Helm použiteľný každý deň",
            "description": "Osobný AI OS pre build aj plánovanie.",
            "horizon": "outcome",
            "vision_id": vision["id"],
            "target_date": "2026-07-01",
        },
    )
    assert outcome.status_code == 201
    outcome_goal = outcome.json()
    assert outcome_goal["horizon"] == "outcome"
    assert outcome_goal["vision_id"] == vision["id"]
    assert outcome_goal["parent_goal_id"] is None

    weekly = client.post(
        f"/api/projects/{project['id']}/goals",
        json={
            "title": "M10 Goal System MVP",
            "horizon": "week",
            "parent_goal_id": outcome_goal["id"],
            "vision_id": vision["id"],
        },
    )
    assert weekly.status_code == 201
    weekly_goal = weekly.json()
    assert weekly_goal["parent_goal_id"] == outcome_goal["id"]
    assert weekly_goal["horizon"] == "week"

    workspace = client.get(f"/api/projects/{project['id']}/workspace").json()
    goal_titles = [goal["title"] for goal in workspace["goals"]]
    assert goal_titles == ["Helm použiteľný každý deň", "M10 Goal System MVP"]
    assert workspace["goals"][0]["vision_title"] == "AI/IT smer"
    assert workspace["goals"][1]["parent_goal_title"] == "Helm použiteľný každý deň"


def test_m10_daily_plan_can_link_everyday_item_to_goal(tmp_path):
    client, _ = make_client(tmp_path)
    project = client.get("/api/state", params={"mode": "sideproject"}).json()["projects"][0]
    goal = client.post(
        f"/api/projects/{project['id']}/goals",
        json={"title": "Dokončiť M10a", "horizon": "week"},
    ).json()
    item = client.post(
        "/api/everyday/items",
        json={"title": "Spraviť Daily Focus UI", "bucket": "today", "kind": "task", "project_id": project["id"]},
    ).json()

    plan_response = client.put(
        "/api/daily-plan/2026-06-06",
        json={"focus": "M10 Goal System", "notes": "Najprv goal hierarchy, potom UI."},
    )
    assert plan_response.status_code == 200
    plan = plan_response.json()
    assert plan["date"] == "2026-06-06"
    assert plan["focus"] == "M10 Goal System"

    linked = client.post(
        f"/api/daily-plan/2026-06-06/items",
        json={
            "title": "Daily Focus UI",
            "goal_id": goal["id"],
            "everyday_item_id": item["id"],
            "status": "open",
            "sort_order": 1,
        },
    )
    assert linked.status_code == 201
    row = linked.json()
    assert row["goal_title"] == "Dokončiť M10a"
    assert row["everyday_title"] == "Spraviť Daily Focus UI"

    everyday_update = client.patch(f"/api/everyday/items/{item['id']}", json={"goal_id": goal["id"]})
    assert everyday_update.status_code == 200
    assert everyday_update.json()["goal_id"] == goal["id"]
    assert everyday_update.json()["goal_title"] == "Dokončiť M10a"

    loaded = client.get("/api/daily-plan/2026-06-06")
    assert loaded.status_code == 200
    data = loaded.json()
    assert data["plan"]["focus"] == "M10 Goal System"
    assert data["items"][0]["goal_title"] == "Dokončiť M10a"
    assert data["items"][0]["everyday_title"] == "Spraviť Daily Focus UI"


def test_m10_rejects_goal_parent_from_other_project(tmp_path):
    client, _ = make_client(tmp_path)
    helm = client.get("/api/state", params={"mode": "sideproject"}).json()["projects"][0]
    other = client.post("/api/projects", json={"name": "Škola", "mode_default": "learning"}).json()
    other_goal = client.post(f"/api/projects/{other['id']}/goals", json={"title": "Course Completion", "horizon": "outcome"}).json()

    response = client.post(
        f"/api/projects/{helm['id']}/goals",
        json={"title": "Nesprávny child", "horizon": "week", "parent_goal_id": other_goal["id"]},
    )

    assert response.status_code == 400
    assert "parent" in response.json()["detail"].lower()


def test_m10_rejects_blank_titles_and_invalid_daily_plan_date(tmp_path):
    client, _ = make_client(tmp_path)
    project = client.post("/api/projects", json={"name": "M10 regression", "mode_default": "sideproject"}).json()

    assert client.post("/api/goals/visions", json={"title": "   "}).status_code == 422
    assert client.post(f"/api/projects/{project['id']}/goals", json={"title": "   "}).status_code == 422
    assert client.put("/api/daily-plan/2026-99-99", json={"focus": "x", "notes": ""}).status_code == 400


def test_m10_patch_nulls_do_not_return_500(tmp_path):
    client, _ = make_client(tmp_path)
    project = client.post("/api/projects", json={"name": "M10 regression", "mode_default": "sideproject"}).json()
    goal = client.post(f"/api/projects/{project['id']}/goals", json={"title": "Null guard"}).json()
    plan_item = client.post("/api/daily-plan/2026-06-06/items", json={"title": "Plan item", "goal_id": goal["id"]}).json()

    assert client.patch(f"/api/projects/{project['id']}/goals/{goal['id']}", json={"description": None}).status_code in (400, 422)
    assert client.patch(f"/api/daily-plan/items/{plan_item['id']}", json={"title": None}).status_code in (400, 422)


def test_m10_rejects_indirect_goal_parent_cycle(tmp_path):
    client, _ = make_client(tmp_path)
    project = client.post("/api/projects", json={"name": "M10 regression", "mode_default": "sideproject"}).json()
    goal_a = client.post(f"/api/projects/{project['id']}/goals", json={"title": "A"}).json()
    goal_b = client.post(f"/api/projects/{project['id']}/goals", json={"title": "B", "parent_goal_id": goal_a["id"]}).json()

    response = client.patch(f"/api/projects/{project['id']}/goals/{goal_a['id']}", json={"parent_goal_id": goal_b["id"]})
    assert response.status_code == 400


def test_context_packet_minimal_chat_has_deterministic_sections(tmp_path):
    client, _ = make_client(tmp_path)
    project = client.get("/api/state").json()["projects"][0]
    chat = project["chats"][0]

    response = client.get(f"/api/chats/{chat['id']}/context-packet")

    assert response.status_code == 200
    data = response.json()
    assert data["chat_id"] == chat["id"]
    assert data["project_id"] == project["id"]
    assert data["char_count"] == len(data["packet_markdown"])
    assert data["char_count"] <= data["max_chars"]
    assert [section["key"] for section in data["sections"]] == [
        "identity",
        "project",
        "memory",
        "team",
        "goals",
        "daily",
        "recent_messages",
        "runs",
        "safety",
        "files",
    ]
    assert "Safety policy: ON" in data["packet_markdown"]
    assert "Ask before external action" in data["packet_markdown"]


def test_context_packet_includes_goal_hierarchy_and_daily_focus(tmp_path):
    client, _ = make_client(tmp_path)
    project = client.get("/api/state", params={"mode": "sideproject"}).json()["projects"][0]
    chat_id = project["chats"][0]["id"]
    vision = client.post("/api/goals/visions", json={"title": "AI/IT smer", "body": "Build useful AI systems."}).json()
    outcome = client.post(
        f"/api/projects/{project['id']}/goals",
        json={"title": "Helm použiteľný každý deň", "horizon": "outcome", "vision_id": vision["id"]},
    ).json()
    weekly = client.post(
        f"/api/projects/{project['id']}/goals",
        json={"title": "M13-light context packet", "horizon": "week", "parent_goal_id": outcome["id"], "vision_id": vision["id"]},
    ).json()
    client.put("/api/daily-plan/2026-06-07", json={"focus": "Context Engine Minimum", "notes": "Keep it deterministic."})
    everyday = client.post(
        "/api/everyday/items",
        json={"title": "Verify context packet", "bucket": "today", "project_id": project["id"], "goal_id": weekly["id"]},
    ).json()
    client.post(
        "/api/daily-plan/2026-06-07/items",
        json={"title": "Write RED tests", "goal_id": weekly["id"], "everyday_item_id": everyday["id"]},
    )

    response = client.get(f"/api/chats/{chat_id}/context-packet", params={"date": "2026-06-07"})

    assert response.status_code == 200
    packet = response.json()["packet_markdown"]
    assert "Vision: AI/IT smer" in packet
    assert "[outcome] Helm použiteľný každý deň" in packet
    assert "[week] M13-light context packet" in packet
    assert "parent: Helm použiteľný každý deň" in packet
    assert "Focus: Context Engine Minimum" in packet
    assert "Write RED tests" in packet
    assert "Verify context packet" in packet


def test_context_packet_includes_recent_messages_runs_and_excludes_artifact_contents(tmp_path):
    client, _ = make_client(tmp_path)
    chat_id = client.get("/api/state").json()["projects"][0]["chats"][0]["id"]
    client.post(f"/api/chats/{chat_id}/messages", json={"role": "user", "content": "Please inspect secrets"})
    client.post(f"/api/chats/{chat_id}/messages", json={"role": "assistant", "content": "I will inspect safely."})
    run = client.post(f"/api/chats/{chat_id}/runs", json={"prompt": "Generate artifact"}).json()
    output = Path(run["artifact_path"]) / "output.md"
    output.write_text("RAW_ARTIFACT_SECRET_SHOULD_NOT_LEAK", encoding="utf-8")

    response = client.get(f"/api/chats/{chat_id}/context-packet")

    assert response.status_code == 200
    packet = response.json()["packet_markdown"]
    assert "user: Please inspect secrets" in packet
    assert "assistant: I will inspect safely." in packet
    assert f"Run #{run['id']}" in packet
    assert run["artifact_path"] in packet
    assert "RAW_ARTIFACT_SECRET_SHOULD_NOT_LEAK" not in packet


def test_context_packet_respects_max_chars_and_invalid_chat_returns_404(tmp_path):
    client, _ = make_client(tmp_path)
    chat_id = client.get("/api/state").json()["projects"][0]["chats"][0]["id"]
    client.post(f"/api/chats/{chat_id}/messages", json={"role": "user", "content": "x" * 5000})

    capped = client.get(f"/api/chats/{chat_id}/context-packet", params={"max_chars": 800})
    missing = client.get("/api/chats/999999/context-packet")

    assert capped.status_code == 200
    data = capped.json()
    assert data["max_chars"] == 800
    assert data["char_count"] == len(data["packet_markdown"])
    assert data["char_count"] <= 800
    assert len("\n".join(section["content"] for section in data["sections"])) <= 800
    assert "truncated" in data["packet_markdown"].lower()
    assert missing.status_code == 404


def test_context_packet_include_flags_and_invalid_date(tmp_path):
    client, _ = make_client(tmp_path)
    chat_id = client.get("/api/state").json()["projects"][0]["chats"][0]["id"]
    client.post(f"/api/chats/{chat_id}/messages", json={"role": "user", "content": "visible only when included"})
    client.post(f"/api/chats/{chat_id}/runs", json={"prompt": "visible run only when included"})

    response = client.get(
        f"/api/chats/{chat_id}/context-packet",
        params={"include_recent_messages": "false", "include_runs": "false"},
    )
    invalid_date = client.get(f"/api/chats/{chat_id}/context-packet", params={"date": "2026-99-99"})

    assert response.status_code == 200
    sections = {section["key"]: section["content"] for section in response.json()["sections"]}
    assert sections["recent_messages"] == "Skipped by request."
    assert sections["runs"] == "Skipped by request."
    assert "visible only when included" not in response.json()["packet_markdown"]
    assert "visible run only when included" not in response.json()["packet_markdown"]
    assert invalid_date.status_code == 400


def test_telegram_inbox_local_capture_creates_everyday_item_without_external_send(tmp_path):
    client, _ = make_client(tmp_path)
    chat_id = client.get("/api/state").json()["projects"][0]["chats"][0]["id"]

    created = client.post(
        "/api/telegram/inbox",
        json={
            "source": "helm_ui_simulation",
            "sender_name": "Telegram local simulation",
            "content": "Zapíš tréningový postreh\nDruhá veta",
            "chat_id": chat_id,
            "metadata": {"local_only": True, "dry_run": True},
        },
    )

    assert created.status_code == 201
    inbox = created.json()
    assert inbox["status"] == "new"
    assert inbox["metadata"] == {"local_only": True, "dry_run": True}

    captured = client.post(f"/api/telegram/inbox/{inbox['id']}/capture", json={"bucket": "today"})

    assert captured.status_code == 201
    payload = captured.json()
    assert payload["inbox_item"]["status"] == "captured"
    assert payload["everyday_item"]["title"] == "Zapíš tréningový postreh"
    assert payload["everyday_item"]["bucket"] == "today"
    sync = client.get("/api/telegram/sync").json()
    assert sync["mode"] == "practical_autonomy_read_only"
    assert sync["external_send"] == "blocked_until_explicit_approval"
    assert sync["counts"]["inbox_new"] == 0


def test_telegram_outbox_drafts_are_local_only_and_editable(tmp_path):
    client, _ = make_client(tmp_path)
    chat_id = client.get("/api/state").json()["projects"][0]["chats"][0]["id"]

    created = client.post(
        "/api/telegram/outbox",
        json={"content": "Lokálny draft pre Telegram", "target_label": "Telegram Home", "chat_id": chat_id},
    )

    assert created.status_code == 201
    draft = created.json()
    assert draft["status"] == "draft"
    assert draft["external_send"] == "blocked_until_explicit_approval"
    assert draft["metadata"]["external_send_blocked"] is True

    updated = client.patch(f"/api/telegram/outbox/{draft['id']}", json={"status": "ready", "target_label": "Telegram Home draft"})

    assert updated.status_code == 200
    assert updated.json()["status"] == "ready"
    assert updated.json()["target_label"] == "Telegram Home draft"
    sync = client.get("/api/telegram/sync").json()
    assert sync["counts"]["outbox_draft"] == 0
    assert sync["outbox"][0]["external_send"] == "blocked_until_explicit_approval"


def test_run_telegram_report_creates_report_message_and_outbox_draft(tmp_path):
    client, _ = make_client(tmp_path)
    chat_id = client.get("/api/state").json()["projects"][0]["chats"][0]["id"]
    run = client.post(f"/api/chats/{chat_id}/runs", json={"prompt": "Sprav Helm M12 sync report"}).json()

    response = client.post(
        f"/api/runs/{run['id']}/telegram-report",
        json={"target_label": "Telegram Home (draft)", "include_artifact_path": True},
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["external_send"] == "blocked_until_explicit_approval"
    assert payload["message"]["role"] == "report"
    assert "Local draft only" in payload["message"]["content"]
    outbox = payload["outbox_item"]
    assert outbox["kind"] == "run_report"
    assert outbox["status"] == "draft"
    assert outbox["run_id"] == run["id"]
    assert outbox["message_id"] == payload["message"]["id"]
    assert outbox["metadata"]["requires_explicit_approval"] is True
    events = client.get(f"/api/runs/{run['id']}/events").json()["events"]
    assert "telegram_report_drafted" in [event["type"] for event in events]


def test_telegram_sync_rejects_blank_null_duplicate_and_repeat_capture_edges(tmp_path):
    client, _ = make_client(tmp_path)

    blank = client.post("/api/telegram/inbox", json={"content": "   "})
    assert blank.status_code == 422

    created = client.post(
        "/api/telegram/inbox",
        json={"content": "External duplicate", "external_chat_id": "chat-1", "external_message_id": "msg-1"},
    )
    assert created.status_code == 201
    duplicate = client.post(
        "/api/telegram/inbox",
        json={"content": "External duplicate retry", "external_chat_id": "chat-1", "external_message_id": "msg-1"},
    )
    assert duplicate.status_code == 409

    null_status = client.patch(f"/api/telegram/inbox/{created.json()['id']}", json={"status": None})
    assert null_status.status_code == 400

    first_capture = client.post(f"/api/telegram/inbox/{created.json()['id']}/capture", json={"bucket": "inbox"})
    second_capture = client.post(f"/api/telegram/inbox/{created.json()['id']}/capture", json={"bucket": "today"})
    assert first_capture.status_code == 201
    assert second_capture.status_code == 200
    assert second_capture.json()["everyday_item"]["id"] == first_capture.json()["everyday_item"]["id"]

    outbox = client.post(
        "/api/telegram/outbox",
        json={"content": "Cannot weaken metadata", "metadata": {"external_send_blocked": False}},
    )
    assert outbox.status_code == 201
    assert outbox.json()["metadata"]["external_send_blocked"] is True
    assert outbox.json()["metadata"]["local_only"] is True
    outbox_null = client.patch(f"/api/telegram/outbox/{outbox.json()['id']}", json={"content": None})
    assert outbox_null.status_code == 400


def test_telegram_sync_exposes_practical_autonomy_read_status_and_blocks_send(tmp_path):
    client, _ = make_client(tmp_path)

    sync = client.get("/api/telegram/sync")

    assert sync.status_code == 200
    payload = sync.json()
    assert payload["mode"] == "practical_autonomy_read_only"
    assert payload["auto_sync"]["enabled"] is True
    assert payload["auto_sync"]["read_allowed"] is True
    assert payload["auto_sync"]["auto_send_allowed"] is False
    assert payload["external_send"] == "blocked_until_explicit_approval"

    updated = client.patch("/api/telegram/sync/settings", json={"auto_sync_enabled": False, "sync_interval_seconds": 180})
    assert updated.status_code == 200
    assert updated.json()["auto_sync"]["enabled"] is False
    assert updated.json()["auto_sync"]["interval_seconds"] == 180
    assert updated.json()["auto_sync"]["auto_send_allowed"] is False


def test_memory_items_can_be_created_listed_and_added_to_context_packet(tmp_path):
    client, _ = make_client(tmp_path)
    state = client.get("/api/state").json()
    project_id = state["projects"][0]["id"]
    chat_id = state["projects"][0]["chats"][0]["id"]

    created = client.post(
        "/api/memory",
        json={
            "scope": "project",
            "kind": "decision",
            "content": "Telegram is the primary low-friction capture surface.",
            "project_id": project_id,
            "chat_id": chat_id,
            "source": "telegram",
            "confidence": 4,
        },
    )

    assert created.status_code == 201
    item = created.json()
    assert item["status"] == "active"
    assert item["metadata"] == {}

    listed = client.get("/api/memory", params={"project_id": project_id}).json()["items"]
    assert [row["content"] for row in listed] == ["Telegram is the primary low-friction capture surface."]

    packet = client.get(f"/api/chats/{chat_id}/context-packet").json()
    sections = {section["key"]: section["content"] for section in packet["sections"]}
    assert "memory" in sections
    assert "Telegram is the primary" in sections["memory"]

    archived = client.patch(f"/api/memory/{item['id']}", json={"status": "archived"})
    assert archived.status_code == 200
    packet_after_archive = client.get(f"/api/chats/{chat_id}/context-packet").json()
    sections_after_archive = {section["key"]: section["content"] for section in packet_after_archive["sections"]}
    assert "Telegram is the primary" not in sections_after_archive["memory"]


def test_memory_rejects_invalid_scope_blank_content_and_cross_project_chat(tmp_path):
    client, _ = make_client(tmp_path)
    state = client.get("/api/state").json()
    project_id = state["projects"][0]["id"]
    other = client.post("/api/projects", json={"name": "Other", "mode_default": "work"}).json()
    other_state = client.get("/api/state").json()
    other_chat_id = next(project for project in other_state["projects"] if project["id"] == other["id"])["chats"][0]["id"]

    assert client.post("/api/memory", json={"content": "x", "scope": "invalid"}).status_code == 422
    assert client.post("/api/memory", json={"content": "   "}).status_code == 422
    cross = client.post(
        "/api/memory",
        json={"content": "Cross project should fail", "project_id": project_id, "chat_id": other_chat_id},
    )
    assert cross.status_code == 400


def test_team_workflow_can_create_steps_advance_and_appear_in_context(tmp_path):
    client, _ = make_client(tmp_path)
    state = client.get("/api/state").json()
    project_id = state["projects"][0]["id"]
    chat_id = state["projects"][0]["chats"][0]["id"]
    run = client.post(f"/api/chats/{chat_id}/runs", json={"prompt": "TEAM workflow smoke"}).json()

    created = client.post(
        f"/api/projects/{project_id}/team-workflows",
        json={"title": "M12.5 Practical Autonomy", "chat_id": chat_id, "run_id": run["id"]},
    )
    assert created.status_code == 201
    workflow = created.json()
    assert workflow["status"] == "active"
    assert workflow["steps"] == []

    step = client.post(
        f"/api/team-workflows/{workflow['id']}/steps",
        json={"role": "reviewer", "title": "Verify no auto-send", "status": "active"},
    )
    assert step.status_code == 201
    assert step.json()["role"] == "reviewer"

    advanced = client.patch(f"/api/team-workflows/{workflow['id']}/steps/{step.json()['id']}", json={"status": "done"})
    assert advanced.status_code == 200
    assert advanced.json()["status"] == "done"

    listed = client.get(f"/api/projects/{project_id}/team-workflows").json()["workflows"]
    assert listed[0]["steps"][0]["status"] == "done"

    packet = client.get(f"/api/chats/{chat_id}/context-packet").json()
    sections = {section["key"]: section["content"] for section in packet["sections"]}
    assert "team" in sections
    assert "M12.5 Practical Autonomy" in sections["team"]
    assert "Verify no auto-send" in sections["team"]

    events = client.get(f"/api/runs/{run['id']}/events").json()["events"]
    event_types = [event["type"] for event in events]
    assert "team_workflow_created" in event_types
    assert "team_workflow_step_updated" in event_types


def test_team_workflow_template_initializes_m14_roles_and_verification_boundary(tmp_path):
    client, _ = make_client(tmp_path)
    state = client.get("/api/state").json()
    project_id = state["projects"][0]["id"]
    chat_id = state["projects"][0]["chats"][0]["id"]
    run = client.post(f"/api/chats/{chat_id}/runs", json={"prompt": "Build TEAM workflow skeleton"}).json()

    response = client.post(
        f"/api/runs/{run['id']}/team-workflow-template",
        json={"title": "M14 TEAM workflow", "project_id": project_id},
    )

    assert response.status_code == 201
    workflow = response.json()
    assert workflow["run_id"] == run["id"]
    assert workflow["chat_id"] == chat_id
    assert workflow["current_role"] == "planner"
    steps = workflow["steps"]
    assert [step["role"] for step in steps] == ["planner", "executor", "reviewer", "verifier"]
    assert [step["status"] for step in steps] == ["active", "pending", "pending", "pending"]
    assert "Self-report" in steps[2]["notes"]
    assert "verified evidence" in steps[3]["notes"]

    events = client.get(f"/api/runs/{run['id']}/events").json()["events"]
    payloads = [json.loads(event["payload_json"]) for event in events if event["type"] == "team_workflow_template_initialized"]
    assert payloads[-1]["workflow_id"] == workflow["id"]
    assert payloads[-1]["roles"] == ["planner", "executor", "reviewer", "verifier"]

    executor_step = next(step for step in steps if step["role"] == "executor")
    updated = client.patch(
        f"/api/team-workflows/{workflow['id']}/steps/{executor_step['id']}",
        json={"status": "active"},
    )
    assert updated.status_code == 200
    listed = client.get(f"/api/projects/{project_id}/team-workflows", params={"status": "all"}).json()["workflows"]
    current = next(item for item in listed if item["id"] == workflow["id"])
    assert current["current_role"] == "executor"
    step_statuses = {step["role"]: step["status"] for step in current["steps"]}
    assert step_statuses["planner"] == "pending"
    assert step_statuses["executor"] == "active"

    renamed = client.patch(
        f"/api/team-workflows/{workflow['id']}/steps/{executor_step['id']}",
        json={"role": "reviewer"},
    )
    assert renamed.status_code == 200
    listed = client.get(f"/api/projects/{project_id}/team-workflows", params={"status": "all"}).json()["workflows"]
    current = next(item for item in listed if item["id"] == workflow["id"])
    assert current["current_role"] == "reviewer"

    created = client.post(
        f"/api/team-workflows/{workflow['id']}/steps",
        json={"role": "verifier", "title": "Final verification", "status": "active", "sort_order": 99},
    )
    assert created.status_code == 201
    listed = client.get(f"/api/projects/{project_id}/team-workflows", params={"status": "all"}).json()["workflows"]
    current = next(item for item in listed if item["id"] == workflow["id"])
    assert current["current_role"] == "verifier"
    active_steps = [step for step in current["steps"] if step["status"] == "active"]
    assert len(active_steps) == 1

    completed_active = client.patch(
        f"/api/team-workflows/{workflow['id']}/steps/{active_steps[0]['id']}",
        json={"status": "done"},
    )
    assert completed_active.status_code == 200
    listed = client.get(f"/api/projects/{project_id}/team-workflows", params={"status": "all"}).json()["workflows"]
    current = next(item for item in listed if item["id"] == workflow["id"])
    assert current["current_role"] == "lead"
    assert all(step["status"] != "active" for step in current["steps"])


def test_team_workflow_steps_track_agent_task_execution_bridge(tmp_path):
    client, _ = make_client(tmp_path)
    state = client.get("/api/state").json()
    project_id = state["projects"][0]["id"]
    chat_id = state["projects"][0]["chats"][0]["id"]
    run = client.post(f"/api/chats/{chat_id}/runs", json={"prompt": "M14c execution bridge"}).json()
    workflow = client.post(
        f"/api/runs/{run['id']}/team-workflow-template",
        json={"title": "M14c TEAM execution bridge", "project_id": project_id},
    ).json()
    executor = next(step for step in workflow["steps"] if step["role"] == "executor")

    assert executor["agent_goal"] == "Implement or execute assigned work"
    assert executor["agent_status"] == "queued"
    assert executor["self_report"] == ""
    assert executor["verified_evidence"] == ""

    updated = client.patch(
        f"/api/team-workflows/{workflow['id']}/steps/{executor['id']}",
        json={
            "status": "done",
            "agent_status": "done",
            "self_report": "Executor says the bridge is wired.",
            "verified_evidence": "pytest tests/test_api.py::test_team_workflow_steps_track_agent_task_execution_bridge -q",
        },
    )

    assert updated.status_code == 200
    step = updated.json()
    assert step["status"] == "done"
    assert step["agent_status"] == "done"
    assert "Executor says" in step["self_report"]
    assert "pytest" in step["verified_evidence"]

    listed = client.get(f"/api/projects/{project_id}/team-workflows", params={"status": "all"}).json()["workflows"]
    listed_executor = next(step for step in listed[0]["steps"] if step["role"] == "executor")
    assert listed_executor["self_report"].startswith("Executor says")
    assert listed_executor["verified_evidence"].startswith("pytest")

    events = client.get(f"/api/runs/{run['id']}/events").json()["events"]
    update_payloads = [json.loads(event["payload_json"]) for event in events if event["type"] == "team_workflow_step_updated"]
    assert "self_report" not in update_payloads[-1]
    assert "verified_evidence" not in update_payloads[-1]
    assert update_payloads[-1]["self_report_present"] is True
    assert update_payloads[-1]["verified_evidence_present"] is True
    agent_payloads = [json.loads(event["payload_json"]) for event in events if event["type"] == "team_agent_task_updated"]
    assert agent_payloads[-1]["step_id"] == executor["id"]
    assert agent_payloads[-1]["agent_status"] == "done"
    assert agent_payloads[-1]["self_report_present"] is True
    assert agent_payloads[-1]["verified_evidence_present"] is True


def test_agent_console_m26_uses_activity_tabs_and_single_progress_panel(tmp_path):
    client, _ = make_client(tmp_path)

    html = client.get("/").text
    js = client.get("/app.js").text
    css = client.get("/styles.css").text

    assert 'data-console-mode="basic"' in html
    assert 'data-console-mode="expert"' in html
    assert 'data-console-activity-tab="thinking"' in html
    assert 'data-console-activity-tab="diff"' in html
    assert 'data-progress-panel' in html
    assert 'data-stage-progress' in html
    assert "function renderProgressPanel" in js
    assert "refreshDiffOutput" in js
    assert "progress-panel" in css
    # M26 intentionally removed the duplicate standalone Expert and TEAM panels.
    assert 'data-console-expert-compact' not in html
    assert 'data-agent-task-bridge' not in html
    assert 'data-team-workflow-panel' not in html


def test_team_workflow_rejects_invalid_role_status_and_cross_project_links(tmp_path):
    client, _ = make_client(tmp_path)
    state = client.get("/api/state").json()
    project_id = state["projects"][0]["id"]
    other_project = client.post("/api/projects", json={"name": "Other Team", "mode_default": "work"}).json()
    other_state = client.get("/api/state").json()
    other_chat_id = next(project for project in other_state["projects"] if project["id"] == other_project["id"])["chats"][0]["id"]

    cross = client.post(
        f"/api/projects/{project_id}/team-workflows",
        json={"title": "Cross project", "chat_id": other_chat_id},
    )
    assert cross.status_code == 400

    workflow = client.post(f"/api/projects/{project_id}/team-workflows", json={"title": "Valid"}).json()
    invalid_role = client.post(f"/api/team-workflows/{workflow['id']}/steps", json={"role": "chaos", "title": "Bad"})
    assert invalid_role.status_code == 422
    invalid_status = client.patch(f"/api/team-workflows/{workflow['id']}/steps/999", json={"status": "maybe"})
    assert invalid_status.status_code == 422


def test_helm_ui_removes_today_workspace_and_keeps_agent_status_strip():
    shared_ui = Path(__file__).resolve().parents[1] / "shared-ui"
    text = (shared_ui / "index.html").read_text(encoding="utf-8")

    removed_today_selectors = [
        "data-everyday-workspace",
        "data-toggle-everyday-panel",
        "data-quick-capture-input",
        "data-daily-focus-input",
        "data-everyday-today",
        "data-everyday-inbox",
        "Today / Inbox",
    ]
    for selector in removed_today_selectors:
        assert selector not in text

    assert "data-agent-status-strip" in text
    assert "data-status-telegram" in text
    assert "data-status-memory" in text
    assert "data-status-team" in text
    # The standalone TEAM workflow console panel is intentionally omitted.
    assert "data-team-workflow-panel" not in text
    assert "data-team-workflow-body" not in text

    app_js = (shared_ui / "app.js").read_text(encoding="utf-8")
    assert "function renderProgressPanel" in app_js
    assert 'data-console-activity-tab="thinking"' in text


def test_project_delete_confirmation_explains_helm_data_loss_and_keeps_files():
    source_app_js = (Path(__file__).resolve().parents[1] / "shared-ui" / "app.js").read_text(encoding="utf-8")

    assert "Z Helmu sa odstránia jeho projektové dáta" in source_app_js
    assert "Denné položky a Telegram inbox/outbox záznamy zostanú" in source_app_js
    assert "bez väzby na projekt/chat/správu/run" in source_app_js
    assert "Priečinok a súbory na disku zostanú nedotknuté" in source_app_js


def test_memory_update_rejects_cross_project_link_after_partial_patch(tmp_path):
    client, _ = make_client(tmp_path)
    state = client.get("/api/state").json()
    project_a = state["projects"][0]
    project_b = client.post("/api/projects", json={"name": "Memory Other", "mode_default": "work"}).json()
    state = client.get("/api/state").json()
    chat_b = next(project for project in state["projects"] if project["id"] == project_b["id"])["chats"][0]

    memory = client.post(
        "/api/memory",
        json={"content": "B-linked memory", "chat_id": chat_b["id"], "scope": "chat", "kind": "decision"},
    )
    assert memory.status_code == 201

    cross_patch = client.patch(f"/api/memory/{memory.json()['id']}", json={"project_id": project_a["id"]})
    assert cross_patch.status_code == 400


# ── M15a — Persistent project run summaries ──────────────────────────────


def test_create_run_summary_requires_valid_run(tmp_path):
    client, _ = make_client(tmp_path)
    response = client.post("/api/runs/9999/summary", json={"summary": "test"})
    assert response.status_code == 404


def test_create_run_summary_persists_and_returns(tmp_path):
    client, _ = make_client(tmp_path)
    helm = _any_project(client)
    chat_id = helm["chats"][0]["id"]
    run = client.post(f"/api/chats/{chat_id}/runs", json={"mode": "work", "prompt": "test run"}).json()

    payload = {
        "summary": "Fixed the context truncation bug. Root cause was off-by-one in _cap_context_sections.",
        "changes": "Fixed _cap_context_sections off-by-one",
        "tests": "Added test_cap_context_sections_edge_case",
        "next_actions": "Review M15a UI after sync",
    }
    created = client.post(f"/api/runs/{run['id']}/summary", json=payload)

    assert created.status_code == 201
    data = created.json()
    assert data["run_id"] == run["id"]
    assert data["summary"] == payload["summary"]
    assert data["changes"] == payload["changes"]
    assert data["tests"] == payload["tests"]
    assert data["next_actions"] == payload["next_actions"]
    assert "id" in data
    assert "created_at" in data


def test_get_run_summary_returns_404_when_missing(tmp_path):
    client, _ = make_client(tmp_path)
    helm = _any_project(client)
    chat_id = helm["chats"][0]["id"]
    run = client.post(f"/api/chats/{chat_id}/runs", json={"mode": "work", "prompt": "find me"}).json()

    response = client.get(f"/api/runs/{run['id']}/summary")
    assert response.status_code == 404


def test_get_run_summary_returns_created_summary(tmp_path):
    client, _ = make_client(tmp_path)
    helm = _any_project(client)
    chat_id = helm["chats"][0]["id"]
    run = client.post(f"/api/chats/{chat_id}/runs", json={"mode": "work", "prompt": "summarize me"}).json()

    payload = {"summary": "Added M15a persistent summaries.", "changes": "New table + API", "tests": "tested", "next_actions": None}
    client.post(f"/api/runs/{run['id']}/summary", json=payload)

    fetched = client.get(f"/api/runs/{run['id']}/summary")
    assert fetched.status_code == 200
    data = fetched.json()
    assert data["summary"] == payload["summary"]
    assert data["changes"] == payload["changes"]
    assert data["next_actions"] == ""


def test_update_run_summary_replaces_existing(tmp_path):
    client, _ = make_client(tmp_path)
    helm = _any_project(client)
    chat_id = helm["chats"][0]["id"]
    run = client.post(f"/api/chats/{chat_id}/runs", json={"mode": "work", "prompt": "update me"}).json()

    client.post(f"/api/runs/{run['id']}/summary", json={"summary": "First version", "changes": "initial", "tests": "", "next_actions": None})
    updated = client.post(f"/api/runs/{run['id']}/summary", json={"summary": "Second version", "changes": "revised", "tests": "", "next_actions": "Ship it"})

    assert updated.status_code == 200  # 200 = update, not 201
    data = updated.json()
    assert data["summary"] == "Second version"
    assert data["next_actions"] == "Ship it"


def test_project_workspace_includes_run_summaries(tmp_path):
    client, _ = make_client(tmp_path)
    helm = _any_project(client)
    chat_id = helm["chats"][0]["id"]
    run = client.post(f"/api/chats/{chat_id}/runs", json={"mode": "work", "prompt": "summary for ws"}).json()
    client.post(f"/api/runs/{run['id']}/summary", json={"summary": "WS test", "changes": "UI", "tests": "browser smoke", "next_actions": None})

    ws = client.get(f"/api/projects/{helm['id']}/workspace").json()
    assert "run_summaries" in ws
    summaries = ws["run_summaries"]
    assert len(summaries) >= 1
    assert summaries[0]["run_id"] == run["id"]
    assert summaries[0]["summary"] == "WS test"


def test_list_project_run_summaries(tmp_path):
    client, _ = make_client(tmp_path)
    helm = _any_project(client)
    chat_id = helm["chats"][0]["id"]
    run_a = client.post(f"/api/chats/{chat_id}/runs", json={"mode": "work", "prompt": "run A"}).json()
    run_b = client.post(f"/api/chats/{chat_id}/runs", json={"mode": "work", "prompt": "run B"}).json()
    client.post(f"/api/runs/{run_a['id']}/summary", json={"summary": "First run", "changes": "", "tests": "", "next_actions": None})
    client.post(f"/api/runs/{run_b['id']}/summary", json={"summary": "Second run", "changes": "", "tests": "", "next_actions": "Deploy"})

    response = client.get(f"/api/projects/{helm['id']}/run-summaries")
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 2
    assert data[0]["run_id"] == run_b["id"]  # newest first
    assert data[1]["run_id"] == run_a["id"]


def test_run_summaries_appear_in_context_packet(tmp_path):
    client, _ = make_client(tmp_path)
    helm = _any_project(client)
    chat_id = helm["chats"][0]["id"]
    run = client.post(f"/api/chats/{chat_id}/runs", json={"mode": "work", "prompt": "ctx test"}).json()
    client.post(f"/api/runs/{run['id']}/summary", json={"summary": "Context packet test", "changes": "API", "tests": "pytest", "next_actions": "Verify"})

    packet = client.get(f"/api/chats/{chat_id}/context-packet", params={"include_runs": True}).json()
    packet_text = packet["packet_markdown"]
    assert "Context packet test" in packet_text


# ── M14d Real TEAM spawn bridge tests ────────────────────────────────────


def test_team_workflow_spawn_status_default_is_none(tmp_path):
    client, _ = make_client(tmp_path)
    helm = _any_project(client)
    chat_id = helm["chats"][0]["id"]
    run = client.post(f"/api/chats/{chat_id}/runs", json={"mode": "work", "prompt": "spawn test"}).json()
    tpl = client.post(f"/api/runs/{run['id']}/team-workflow-template", json={"title": "Spawn"}).json()
    steps = tpl["steps"]
    executor = next(s for s in steps if s["role"] == "executor")
    status = client.get(f"/api/team-workflows/{tpl['id']}/steps/{executor['id']}/spawn").json()
    assert status["spawn_status"] == "none"
    assert status["spawn_command"] == ""
    assert status["spawn_pid"] is None
    assert status["agent_status"] == "queued"


def test_team_workflow_spawn_requires_configured_command(tmp_path, monkeypatch):
    monkeypatch.delenv("HELM_RUNTIME_COMMAND", raising=False)
    client, _ = make_client(tmp_path)
    helm = _any_project(client)
    chat_id = helm["chats"][0]["id"]
    run = client.post(f"/api/chats/{chat_id}/runs", json={"mode": "work", "prompt": "spawn without runtime"}).json()
    template = client.post(f"/api/runs/{run['id']}/team-workflow-template", json={"title": "Runtime guard"}).json()
    executor = next(step for step in template["steps"] if step["role"] == "executor")

    response = client.post(f"/api/team-workflows/{template['id']}/steps/{executor['id']}/spawn")

    assert response.status_code == 503
    status = client.get(f"/api/team-workflows/{template['id']}/steps/{executor['id']}/spawn").json()
    assert status["spawn_status"] == "none"
    assert status["spawn_command"] == ""


def test_team_workflow_spawn_rejects_non_executor_role(tmp_path):
    client, _ = make_client(tmp_path)
    helm = _any_project(client)
    chat_id = helm["chats"][0]["id"]
    run = client.post(f"/api/chats/{chat_id}/runs", json={"mode": "work", "prompt": "spawn role"}).json()
    tpl = client.post(f"/api/runs/{run['id']}/team-workflow-template", json={"title": "Role"}).json()
    planner = next(s for s in tpl["steps"] if s["role"] == "planner")
    resp = client.post(f"/api/team-workflows/{tpl['id']}/steps/{planner['id']}/spawn")
    assert resp.status_code == 400


def test_team_workflow_spawn_requires_active_run(tmp_path):
    client, _ = make_client(tmp_path)
    helm = _any_project(client)
    workflow = client.post(f"/api/projects/{helm['id']}/team-workflows", json={"title": "No run"}).json()
    resp = client.post(f"/api/team-workflows/{workflow['id']}/steps", json={"role": "executor", "title": "Bad"})
    assert resp.status_code == 201
    step_id = resp.json()["id"]
    spawn = client.post(f"/api/team-workflows/{workflow['id']}/steps/{step_id}/spawn")
    assert spawn.status_code == 400


# ── Token refresh tests ──────────────────────────────────────────────────


def test_token_refresh_status_default_not_waiting(tmp_path):
    client, _ = make_client(tmp_path)
    helm = _any_project(client)
    chat_id = helm["chats"][0]["id"]
    run = client.post(f"/api/chats/{chat_id}/runs", json={"mode": "work", "prompt": "token status"}).json()
    resp = client.get(f"/api/runs/{run['id']}/token-refresh")
    assert resp.status_code == 200
    data = resp.json()
    assert data["waiting_for_tokens"] is False
    assert data["token_refresh_at"] is None
    assert data["seconds_remaining"] == 0


def test_is_token_exhaustion_error_detects_patterns():
    from app.main import _is_token_exhaustion_error
    assert _is_token_exhaustion_error({"exit_code": 1, "stderr": "Error: rate_limit exceeded", "stdout": ""}) is True
    assert _is_token_exhaustion_error({"exit_code": 1, "stderr": "", "stdout": "HTTP 429 Too Many Requests"}) is True
    assert _is_token_exhaustion_error({"exit_code": 1, "stderr": "context_length_exceeded", "stdout": ""}) is True
    assert _is_token_exhaustion_error({"exit_code": 0, "stderr": "rate_limit", "stdout": ""}) is False
    assert _is_token_exhaustion_error({"exit_code": 1, "stderr": "SyntaxError", "stdout": ""}) is False


def _any_project(client):
    """Return any project from the state (creates one if none exist)."""
    state = client.get("/api/state", params={"mode": "sideproject"}).json()
    if state["projects"]:
        return state["projects"][0]
    created = client.post("/api/projects", json={"name": "Helper Project", "mode_default": "work"}).json()
    state = client.get("/api/state", params={"mode": "work"}).json()
    return next(p for p in state["projects"] if p["id"] == created["id"])


# ── M16 P0.2: runtime tool-event harvest from agent state DB ──────────────


def _make_runtime_state_db(path: Path, artifact_path: str, tool_calls: list[dict]) -> None:
    """Build a minimal Hermes-profile-shaped state.db with tool calls.

    NOTE: this fixture writes the assistant `tool_calls` JSON shape only. Real
    Hermes also records executed tools as role='tool' rows with tool_calls NULL;
    see tests/test_tool_harvest_fix.py for that shape (it is the common case and
    was silently dropped until M19).
    """
    import sqlite3

    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, system_prompt TEXT, started_at REAL)"
    )
    conn.execute(
        "CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT, "
        "content TEXT, tool_calls TEXT, tool_name TEXT, timestamp REAL)"
    )
    conn.execute(
        "INSERT INTO sessions(id, source, system_prompt, started_at) VALUES (?,?,?,?)",
        ("sess-1", "cli", f"Current working directory: {artifact_path}\n", 1000.0),
    )
    for index, call in enumerate(tool_calls):
        conn.execute(
            "INSERT INTO messages(session_id, role, content, tool_calls, tool_name, timestamp) "
            "VALUES (?,?,?,?,?,?)",
            (
                "sess-1",
                "assistant",
                "",
                json.dumps([{"function": {"name": call["name"], "arguments": json.dumps(call.get("args", {}))}}]),
                None,
                1000.0 + index,
            ),
        )
    conn.commit()
    conn.close()


def test_runtime_tool_events_are_harvested_from_agent_state_db(tmp_path, monkeypatch):
    """Tools tab must not show 0: Helm harvests real tool calls from the runtime
    agent's own state DB instead of relying on the agent voluntarily writing events."""
    client, _ = make_client(tmp_path)
    state = client.get("/api/state").json()
    chat_id = state["projects"][0]["chats"][0]["id"]
    run = client.post(f"/api/chats/{chat_id}/runs", json={"prompt": "M16 tool harvest"}).json()

    artifact_path = run["artifact_path"]
    runtime_db = tmp_path / "runtime-state.db"
    _make_runtime_state_db(
        runtime_db,
        artifact_path,
        [
            {"name": "read_file", "args": {"path": "app/main.py"}},
            {"name": "terminal", "args": {"command": "pytest -q"}},
            {"name": "read_file", "args": {"path": "app/db.py"}},
        ],
    )
    monkeypatch.setenv("HELM_RUNTIME_STATE_DB", str(runtime_db))

    events = client.get(f"/api/runs/{run['id']}/events").json()["events"]
    tool_events = [e for e in events if e["type"] == "tool_activity"]

    assert len(tool_events) == 3, f"expected 3 harvested tool events, got {len(tool_events)}"
    payloads = [json.loads(e["payload_json"]) for e in tool_events]
    assert [p["tool"] for p in payloads] == ["read_file", "terminal", "read_file"]
    assert payloads[0]["source"] == "runtime_state_db"
    assert "app/main.py" in str(payloads[0].get("summary") or "")


def test_runtime_tool_event_harvest_is_idempotent(tmp_path, monkeypatch):
    """Repeated polling must not duplicate harvested tool events."""
    client, _ = make_client(tmp_path)
    state = client.get("/api/state").json()
    chat_id = state["projects"][0]["chats"][0]["id"]
    run = client.post(f"/api/chats/{chat_id}/runs", json={"prompt": "M16 idempotent"}).json()

    runtime_db = tmp_path / "runtime-state-2.db"
    _make_runtime_state_db(runtime_db, run["artifact_path"], [{"name": "todo", "args": {}}])
    monkeypatch.setenv("HELM_RUNTIME_STATE_DB", str(runtime_db))

    client.get(f"/api/runs/{run['id']}/events")
    client.get(f"/api/runs/{run['id']}/events")
    events = client.get(f"/api/runs/{run['id']}/events").json()["events"]

    assert len([e for e in events if e["type"] == "tool_activity"]) == 1


def test_runtime_tool_harvest_ignores_unrelated_sessions(tmp_path, monkeypatch):
    """Sessions whose cwd does not match this run's artifact path must be ignored."""
    client, _ = make_client(tmp_path)
    state = client.get("/api/state").json()
    chat_id = state["projects"][0]["chats"][0]["id"]
    run = client.post(f"/api/chats/{chat_id}/runs", json={"prompt": "M16 scoping"}).json()

    runtime_db = tmp_path / "runtime-state-3.db"
    _make_runtime_state_db(runtime_db, "/some/other/run/path", [{"name": "read_file", "args": {}}])
    monkeypatch.setenv("HELM_RUNTIME_STATE_DB", str(runtime_db))

    events = client.get(f"/api/runs/{run['id']}/events").json()["events"]
    assert [e for e in events if e["type"] == "tool_activity"] == []


# ── M16 P0.3/P1: live run progress, diff view, file tree ─────────────────


def test_run_live_progress_streams_partial_stdout_and_tool_counts(tmp_path, monkeypatch):
    """Agent Console must show progress while a run is still executing."""
    client, _ = make_client(tmp_path)
    state = client.get("/api/state").json()
    chat_id = state["projects"][0]["chats"][0]["id"]
    run = client.post(f"/api/chats/{chat_id}/runs", json={"prompt": "M16 live progress"}).json()
    artifact_path = Path(run["artifact_path"])

    (artifact_path / "runtime-stdout.log").write_text("step 1\nstep 2\nstep 3\n", encoding="utf-8")
    runtime_db = tmp_path / "live-state.db"
    _make_runtime_state_db(
        runtime_db,
        str(artifact_path),
        [{"name": "read_file", "args": {"path": "a.py"}}, {"name": "terminal", "args": {"command": "ls"}}],
    )
    monkeypatch.setenv("HELM_RUNTIME_STATE_DB", str(runtime_db))

    resp = client.get(f"/api/runs/{run['id']}/live")
    assert resp.status_code == 200
    data = resp.json()
    assert data["run_id"] == run["id"]
    assert "step 3" in data["stdout_tail"]
    assert data["tool_count"] == 2
    assert data["tools_recent"][0]["tool"] == "read_file"
    assert data["status"] == run["status"]


def test_run_live_progress_returns_404_for_unknown_run(tmp_path):
    client, _ = make_client(tmp_path)
    assert client.get("/api/runs/999999/live").status_code == 404


def test_run_diff_returns_repo_changes_with_stat_and_patch(tmp_path):
    """A run-scoped git diff makes code changes visible for review."""
    client, _ = make_client(tmp_path)
    state = client.get("/api/state").json()
    chat_id = state["projects"][0]["chats"][0]["id"]
    run = client.post(f"/api/chats/{chat_id}/runs", json={"prompt": "M16 diff"}).json()

    resp = client.get(f"/api/runs/{run['id']}/diff")
    assert resp.status_code == 200
    data = resp.json()
    assert data["run_id"] == run["id"]
    assert "repo" in data
    assert "files" in data and isinstance(data["files"], list)
    assert "available" in data


def test_workspace_tree_lists_entries_with_bounded_depth(tmp_path):
    """File tree API must return real entries so MAP tab is not a mockup."""
    client, workspace_root = make_client(tmp_path)
    (workspace_root / "src").mkdir(parents=True, exist_ok=True)
    (workspace_root / "src" / "alpha.py").write_text("print('a')\n", encoding="utf-8")
    (workspace_root / "README.md").write_text("# hi\n", encoding="utf-8")

    resp = client.get("/api/workspace/tree", params={"depth": 2})
    assert resp.status_code == 200
    data = resp.json()
    assert data["root"]
    assert isinstance(data["entries"], list)
    names = {entry["name"] for entry in data["entries"]}
    assert "src" in names or "README.md" in names
    for entry in data["entries"]:
        assert entry["type"] in {"dir", "file"}
        assert "path" in entry


def test_workspace_tree_rejects_path_traversal(tmp_path):
    """Path traversal must not escape the workspace root."""
    client, _ = make_client(tmp_path)
    resp = client.get("/api/workspace/tree", params={"path": "../../etc"})
    assert resp.status_code == 400


def test_runtime_process_tees_stdout_to_log_during_execution(tmp_path):
    """Streaming requires incremental stdout on disk, not buffered-until-exit."""
    from app.main import _execute_runtime_process

    artifact_path = tmp_path / "run-artifacts"
    artifact_path.mkdir()
    run = {"id": 1, "artifact_path": str(artifact_path), "mode": "work", "prompt": "hello"}

    script = "import sys; print('alpha'); print('beta'); sys.stderr.write('warn\\n')"
    result = _execute_runtime_process(run, [sys.executable, "-c", script], timeout_seconds=30)

    assert result["exit_code"] == 0
    assert "alpha" in str(result["stdout"])
    assert "beta" in str(result["stdout"])
    assert "warn" in str(result["stderr"])

    log = artifact_path / "runtime-stdout.log"
    assert log.exists(), "runtime-stdout.log must be written for live streaming"
    body = log.read_text(encoding="utf-8")
    assert "alpha" in body and "beta" in body


def test_runtime_process_survives_large_stderr_without_deadlock(tmp_path):
    """Regression: stderr=PIPE drained only after the stdout loop deadlocks
    once the child fills the ~64KB stderr pipe buffer. AI runtimes are verbose
    on stderr, so this must never hang."""
    from app.main import _execute_runtime_process

    artifact_path = tmp_path / "art-stderr"
    artifact_path.mkdir()
    run = {"id": 1, "artifact_path": str(artifact_path), "mode": "work", "prompt": "x"}

    script = "import sys; sys.stderr.write('x' * 200000); sys.stderr.flush(); print('done')"
    result = _execute_runtime_process(run, [sys.executable, "-c", script], timeout_seconds=30)

    assert result["timed_out"] is False
    assert result["exit_code"] == 0
    assert "done" in str(result["stdout"])
    assert len(str(result["stderr"])) >= 200000


def test_runtime_process_timeout_fires_on_slow_child(tmp_path):
    """Regression: proc.wait(timeout=...) after an unbounded stdout loop meant
    the wall-clock timeout could never fire. A hung runtime must be killed."""
    from app.main import _execute_runtime_process

    artifact_path = tmp_path / "art-timeout"
    artifact_path.mkdir()
    run = {"id": 2, "artifact_path": str(artifact_path), "mode": "work", "prompt": "x"}

    script = "import time, sys; print('start'); sys.stdout.flush(); time.sleep(30)"
    started = time.monotonic()
    result = _execute_runtime_process(run, [sys.executable, "-c", script], timeout_seconds=3)
    elapsed = time.monotonic() - started

    assert result["timed_out"] is True
    assert elapsed < 15, f"timeout did not fire promptly (took {elapsed:.1f}s)"


def test_tool_call_summary_redacts_sensitive_values():
    """Tool arguments can carry secrets; summaries are stored and served."""
    from app.main import _tool_call_summary

    summary = _tool_call_summary("terminal", {"command": "curl -H 'Authorization: Bearer sk-test42'"})
    assert "sk-test42" not in summary
    assert "command" in summary

    url_summary = _tool_call_summary("web_fetch", {"url": "https://api.example.com/?token=abc123secret"})
    assert "abc123secret" not in url_summary

    # Non-sensitive keys stay readable for observability
    path_summary = _tool_call_summary("read_file", {"path": "app/main.py"})
    assert "app/main.py" in path_summary


def test_workspace_tree_survives_symlink_cycle(tmp_path):
    """A symlink pointing at an ancestor must not cause runaway traversal."""
    client, workspace_root = make_client(tmp_path)
    nested = workspace_root / "pkg"
    nested.mkdir(parents=True, exist_ok=True)
    (nested / "mod.py").write_text("x = 1\n", encoding="utf-8")
    try:
        (nested / "loop").symlink_to(workspace_root)
    except (OSError, NotImplementedError):
        return  # platform without symlink support

    resp = client.get("/api/workspace/tree", params={"depth": 4})
    assert resp.status_code == 200
    entries = resp.json()["entries"]
    assert len(entries) < 400


def test_workspace_tree_with_project_id_uses_project_root(tmp_path):
    """When project_id is given, tree must return project's root, not default."""
    import sqlite3
    client, workspace_root = make_client(tmp_path)
    state = client.get("/api/state").json()
    project_id = state["projects"][0]["id"]

    # Create a separate project workspace folder
    project_root = tmp_path / "custom-project"
    project_root.mkdir(parents=True, exist_ok=True)
    (project_root / "custom-file.txt").write_text("project data\n", encoding="utf-8")

    # Update project root_path directly in DB (no public PATCH endpoint exists)
    db_path = str(tmp_path / "workspace.db")
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE projects SET root_path = ? WHERE id = ?", (str(project_root), project_id))
    conn.commit()
    conn.close()

    # Request tree with project_id
    resp = client.get("/api/workspace/tree", params={"project_id": project_id, "depth": 1})
    assert resp.status_code == 200
    data = resp.json()
    assert str(project_root) in data["root"]
    names = {entry["name"] for entry in data["entries"]}
    assert "custom-file.txt" in names


def test_delete_chat_removes_chat_and_messages(tmp_path):
    """DELETE /api/projects/{id}/chats/{id} must cascade to messages/runs."""
    client, _ = make_client(tmp_path)
    state = client.get("/api/state").json()
    project_id = state["projects"][0]["id"]
    chat_id = state["projects"][0]["chats"][0]["id"]

    # Create a second chat to delete
    new_chat = client.post(f"/api/projects/{project_id}/chats", json={"title": "Doomed"}).json()
    new_id = new_chat["id"]

    resp = client.delete(f"/api/projects/{project_id}/chats/{new_id}")
    assert resp.status_code == 200
    assert resp.json()["deleted"] is True

    # Original chat must still exist
    state2 = client.get("/api/state").json()
    remaining = [c["id"] for p in state2["projects"] for c in p["chats"]]
    assert chat_id in remaining
    assert new_id not in remaining


def test_delete_project_cascades_to_all_chats(tmp_path):
    """DELETE /api/projects/{id} must remove project and all its chats."""
    client, _ = make_client(tmp_path)
    state = client.get("/api/state").json()
    project_id = state["projects"][0]["id"]

    resp = client.delete(f"/api/projects/{project_id}")
    assert resp.status_code == 200
    assert resp.json()["deleted"] is True

    state2 = client.get("/api/state").json()
    ids = [p["id"] for p in state2["projects"]]
    assert project_id not in ids


def test_browse_lists_subdirectories(tmp_path, monkeypatch):
    """GET /api/browse should list subdirectories of a given path."""
    # Create a test directory structure inside home (browse is restricted to home)
    test_dir = tmp_path / "browse_test"
    test_dir.mkdir()
    (test_dir / "sub1").mkdir()
    (test_dir / "sub2").mkdir()
    (test_dir / "file.txt").write_text("not a dir")

    # Make the app think tmp_path is inside home
    monkeypatch.setattr("app.main.Path.home", classmethod(lambda cls: tmp_path))

    client, _ = make_client(tmp_path)
    resp = client.get("/api/browse", params={"path": str(test_dir)})
    assert resp.status_code == 200
    data = resp.json()
    assert data["path"] == str(test_dir)
    names = [d["name"] for d in data["directories"]]
    assert "sub1" in names
    assert "sub2" in names
    assert "file.txt" not in names


def test_browse_blocks_paths_outside_home(tmp_path, monkeypatch):
    """GET /api/browse must reject paths outside home directory."""
    # Set home to tmp_path, then try to access /etc which is outside
    monkeypatch.setattr("app.main.Path.home", classmethod(lambda cls: tmp_path))

    client, _ = make_client(tmp_path)
    resp = client.get("/api/browse", params={"path": "/etc"})
    assert resp.status_code == 403


def test_browse_allows_home_path_behind_symlink(tmp_path, monkeypatch):
    """Resolve both HOME and the requested path before containment checks."""
    real_home = tmp_path / "real-home"
    real_home.mkdir()
    project = real_home / "project"
    project.mkdir()
    home_alias = tmp_path / "home-link"
    home_alias.symlink_to(real_home, target_is_directory=True)
    monkeypatch.setattr("app.main.Path.home", classmethod(lambda cls: home_alias))

    client, _ = make_client(tmp_path)
    response = client.get("/api/browse", params={"path": str(home_alias / "project")})

    assert response.status_code == 200
    assert response.json()["path"] == str(project)


def test_browse_rejects_symlink_escape(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    escape = home / "outside-link"
    escape.symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr("app.main.Path.home", classmethod(lambda cls: home))

    client, _ = make_client(tmp_path)
    response = client.get("/api/browse", params={"path": str(escape)})

    assert response.status_code == 403


def test_open_existing_folder_registers_project(tmp_path, monkeypatch):
    """POST /api/projects/open should register an existing directory as a project."""
    # Create an existing directory to open
    existing = tmp_path / "my-existing-project"
    existing.mkdir()
    (existing / "README.md").write_text("# My Project")

    # Make the app think tmp_path is inside home
    monkeypatch.setattr("app.main.Path.home", classmethod(lambda cls: tmp_path))

    client, _ = make_client(tmp_path)
    resp = client.post("/api/projects/open", json={
        "path": str(existing),
        "name": "Môj existujúci projekt",
        "mode_default": "work",
    })
    assert resp.status_code == 201
    project = resp.json()
    assert project["name"] == "Môj existujúci projekt"
    assert project["slug"] == "môj-existujúci-projekt"
    assert project["root_path"] == str(existing)
    # Should have a Main chat
    state = client.get("/api/state").json()
    found = next((p for p in state["projects"] if p["slug"] == "môj-existujúci-projekt"), None)
    assert found is not None
    assert [c["title"] for c in found["chats"]] == ["Main"]


def test_open_existing_folder_defaults_name_to_dir_name(tmp_path, monkeypatch):
    """POST /api/projects/open should use directory name if no name given."""
    existing = tmp_path / "auto-named"
    existing.mkdir()

    # Make the app think tmp_path is inside home
    monkeypatch.setattr("app.main.Path.home", classmethod(lambda cls: tmp_path))

    client, _ = make_client(tmp_path)
    resp = client.post("/api/projects/open", json={"path": str(existing)})
    assert resp.status_code == 201
    assert resp.json()["name"] == "auto-named"


def test_open_existing_folder_rejects_nonexistent_path(tmp_path, monkeypatch):
    """POST /api/projects/open must reject paths that don't exist."""
    monkeypatch.setattr("app.main.Path.home", classmethod(lambda cls: tmp_path))

    client, _ = make_client(tmp_path)
    resp = client.post("/api/projects/open", json={"path": str(tmp_path / "nonexistent")})
    assert resp.status_code == 404


def test_open_existing_folder_rejects_path_outside_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.setattr("app.main.Path.home", classmethod(lambda cls: home))

    client, _ = make_client(tmp_path)
    resp = client.post("/api/projects/open", json={"path": str(outside)})

    assert resp.status_code == 403


def test_open_existing_folder_rejects_home_directory(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr("app.main.Path.home", classmethod(lambda cls: home))

    client, _ = make_client(tmp_path)
    resp = client.post("/api/projects/open", json={"path": str(home)})

    assert resp.status_code == 403


def test_open_existing_folder_rejects_symlink_escape(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    link = home / "linked-project"
    link.symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr("app.main.Path.home", classmethod(lambda cls: home))

    client, _ = make_client(tmp_path)
    resp = client.post("/api/projects/open", json={"path": str(link)})

    assert resp.status_code == 403


def test_delete_project_preserves_opened_folder_contents(tmp_path, monkeypatch):
    home = tmp_path / "home"
    existing = home / "imported-project"
    existing.mkdir(parents=True)
    marker = existing / "keep.txt"
    marker.write_text("user data", encoding="utf-8")
    monkeypatch.setattr("app.main.Path.home", classmethod(lambda cls: home))

    client, _ = make_client(tmp_path)
    opened = client.post("/api/projects/open", json={"path": str(existing)})
    assert opened.status_code == 201

    deleted = client.delete(f"/api/projects/{opened.json()['id']}")

    assert deleted.status_code == 200
    assert marker.read_text(encoding="utf-8") == "user data"


def test_new_chat_gets_unique_session_id(tmp_path):
    """Each new chat must receive a unique hermes_session_id."""
    client, _ = make_client(tmp_path)
    state = client.get("/api/state").json()
    project_id = state["projects"][0]["id"]

    chat1 = client.post(f"/api/projects/{project_id}/chats", json={"title": "Chat A"}).json()
    chat2 = client.post(f"/api/projects/{project_id}/chats", json={"title": "Chat B"}).json()

    assert chat1["hermes_session_id"]
    assert chat2["hermes_session_id"]
    assert chat1["hermes_session_id"] != chat2["hermes_session_id"]


def test_run_diff_declares_repo_wide_scope(tmp_path):
    """The diff endpoint is repo-wide, not run-isolated; scope must say so."""
    client, _ = make_client(tmp_path)
    state = client.get("/api/state").json()
    chat_id = state["projects"][0]["chats"][0]["id"]
    run = client.post(f"/api/chats/{chat_id}/runs", json={"prompt": "scope"}).json()

    data = client.get(f"/api/runs/{run['id']}/diff").json()
    assert data["scope"] == "repo_working_tree"


# ── M15a: automatic run summaries ─────────────────────────────────────────


def test_summary_generated_automatically_on_runtime_success(tmp_path):
    """A completed runtime run must produce a project_run_summaries row without any HTTP POST."""
    client, workspace_root = make_client(tmp_path)
    helm = _any_project(client)
    chat_id = helm["chats"][0]["id"]
    run = client.post(f"/api/chats/{chat_id}/runs", json={"mode": "work", "prompt": "add feature X"}).json()

    import sys
    from app.main import _execute_runtime_process, _finalize_runtime_bridge
    from app.db import connect

    script = "import sys; print('Implemented feature X'); print('3 passed'); sys.exit(0)"
    with connect(tmp_path / "workspace.db") as conn:
        row = conn.execute("SELECT * FROM runs WHERE id = ?", (run["id"],)).fetchone()
        run_row = dict(row)
        result = _execute_runtime_process(run_row, [sys.executable, "-c", script], timeout_seconds=30)
        _finalize_runtime_bridge(conn, run_row, result)

    response = client.get(f"/api/runs/{run['id']}/summary")
    assert response.status_code == 200, "auto-summary should exist after successful runtime"
    data = response.json()
    assert data["summary"], "summary text must not be empty"
    assert data["run_id"] == run["id"]


def test_auto_summary_is_idempotent_and_does_not_clobber_manual(tmp_path):
    """Manual summary must win over a later auto-generated one."""
    client, workspace_root = make_client(tmp_path)
    helm = _any_project(client)
    chat_id = helm["chats"][0]["id"]
    run = client.post(f"/api/chats/{chat_id}/runs", json={"mode": "work", "prompt": "manual first"}).json()
    client.post(f"/api/runs/{run['id']}/summary", json={"summary": "HUMAN WROTE THIS", "changes": "", "tests": "", "next_actions": None})

    import sys
    from app.main import _execute_runtime_process, _finalize_runtime_bridge
    from app.db import connect

    with connect(tmp_path / "workspace.db") as conn:
        run_row = dict(conn.execute("SELECT * FROM runs WHERE id = ?", (run["id"],)).fetchone())
        result = _execute_runtime_process(run_row, [sys.executable, "-c", "print('auto text')"], timeout_seconds=30)
        _finalize_runtime_bridge(conn, run_row, result)

    data = client.get(f"/api/runs/{run['id']}/summary").json()
    assert data["summary"] == "HUMAN WROTE THIS", "auto-summary must not overwrite a human-authored summary"


def test_no_auto_summary_on_failed_run(tmp_path):
    """Failed runs should not produce a misleading success summary."""
    client, workspace_root = make_client(tmp_path)
    helm = _any_project(client)
    chat_id = helm["chats"][0]["id"]
    run = client.post(f"/api/chats/{chat_id}/runs", json={"mode": "work", "prompt": "will fail"}).json()

    import sys
    from app.main import _execute_runtime_process, _finalize_runtime_bridge
    from app.db import connect

    with connect(tmp_path / "workspace.db") as conn:
        run_row = dict(conn.execute("SELECT * FROM runs WHERE id = ?", (run["id"],)).fetchone())
        result = _execute_runtime_process(run_row, [sys.executable, "-c", "import sys; sys.exit(1)"], timeout_seconds=30)
        _finalize_runtime_bridge(conn, run_row, result)

    assert client.get(f"/api/runs/{run['id']}/summary").status_code == 404


def test_no_auto_summary_when_runtime_never_started(tmp_path):
    """A run that never dispatched must not get a summary.

    M20 regression: the M19 backfill produced a summary for live run 19, whose
    runtime never started. Its `output.md` was still the untouched template, so
    the stored summary literally read "_TODO: vystup stage._" and would have been
    re-injected into later prompts as if it were a real result.
    """
    client, _ = make_client(tmp_path)
    helm = _any_project(client)
    chat_id = helm["chats"][0]["id"]
    run = client.post(f"/api/chats/{chat_id}/runs", json={"mode": "work", "prompt": "co je helm?"}).json()

    from app.main import _auto_run_summary
    from app.db import connect

    with connect(tmp_path / "workspace.db") as conn:
        run_row = dict(conn.execute("SELECT * FROM runs WHERE id = ?", (run["id"],)).fetchone())
        # No runtime_started event exists for this run.
        _auto_run_summary(conn, run_row, {"stdout": "", "stderr": "", "exit_code": 0, "timed_out": False})

    assert client.get(f"/api/runs/{run['id']}/summary").status_code == 404, (
        "a run whose runtime never started must not get an auto summary"
    )


def test_auto_summary_changes_omits_files_without_a_run_baseline(tmp_path):
    """`changes` must describe the run, not the developer's working tree.

    M20 regression: `_repo_diff_payload` ignores run_id and shells out to
    `git diff HEAD` against the real repo (it resolves via `__file__`, so
    tmp_path does not isolate it). Every backfilled summary therefore claimed the
    same two files (app/main.py, tests/test_api.py) -- files that were merely
    dirty in the repo at backfill time.

    Deterministic form: a run with no recorded baseline cannot know what it
    changed, so it must report no file list at all. Fabricated provenance is
    worse than an empty field.
    """
    client, _ = make_client(tmp_path)
    helm = _any_project(client)
    chat_id = helm["chats"][0]["id"]
    run = client.post(f"/api/chats/{chat_id}/runs", json={"mode": "work", "prompt": "quick question"}).json()

    import sys
    from app.main import _execute_runtime_process, _finalize_runtime_bridge
    from app.db import connect

    with connect(tmp_path / "workspace.db") as conn:
        # Simulate a legacy/pre-M20 run: no baseline was captured.
        conn.execute("UPDATE runs SET base_commit = NULL WHERE id = ?", (run["id"],))
        conn.commit()
        run_row = dict(conn.execute("SELECT * FROM runs WHERE id = ?", (run["id"],)).fetchone())
        result = _execute_runtime_process(
            run_row, [sys.executable, "-c", "print('done, no files touched')"], timeout_seconds=30
        )
        _finalize_runtime_bridge(conn, run_row, result)

    resp = client.get(f"/api/runs/{run['id']}/summary")
    assert resp.status_code == 200
    changes = resp.json()["changes"]
    assert "file(s)" not in changes, (
        f"run without a baseline must not claim a file list, got {changes!r}"
    )
    assert "app/main.py" not in changes, (
        f"summary attributed unrelated repo files to this run: {changes!r}"
    )


def test_auto_summary_extracts_test_results(tmp_path):
    """Test counts in runtime stdout should land in the tests column."""
    client, workspace_root = make_client(tmp_path)
    helm = _any_project(client)
    chat_id = helm["chats"][0]["id"]
    run = client.post(f"/api/chats/{chat_id}/runs", json={"mode": "work", "prompt": "run tests"}).json()

    import sys
    from app.main import _execute_runtime_process, _finalize_runtime_bridge
    from app.db import connect

    with connect(tmp_path / "workspace.db") as conn:
        run_row = dict(conn.execute("SELECT * FROM runs WHERE id = ?", (run["id"],)).fetchone())
        result = _execute_runtime_process(run_row, [sys.executable, "-c", "print('127 passed in 4.40s')"], timeout_seconds=30)
        _finalize_runtime_bridge(conn, run_row, result)

    data = client.get(f"/api/runs/{run['id']}/summary").json()
    assert "127 passed" in data["tests"], f"expected test counts harvested, got {data['tests']!r}"
