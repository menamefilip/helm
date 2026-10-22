# File index

- `app/main.py`: FastAPI application, routes, run lifecycle, runtime adapter.
- `app/db.py`: SQLite schema and data-access helpers.
- `app/schemas.py`: request and response validation.
- `app/workspace.py`: local workspace, context retrieval, context tiers, artifacts, runtime contracts.
- `app/sse.py`: server-sent event broadcaster.
- `shared-ui/index.html`: application structure and view containers.
- `shared-ui/app.js`: API calls, project/chat state, run console, and interactions.
- `shared-ui/styles.css`: responsive layout and visual styling.
- `tests/test_api.py`: API behavior and safety regressions.
- `tests/test_context_tiers.py`: deterministic context-tier classification.
- `tests/test_run_lifecycle.py`: run completion and review gates.
- `tests/test_shared_ui_static.py`: static UI wiring checks.
- `tests/test_sqlite_backup.py`: SQLite online-backup behavior.
