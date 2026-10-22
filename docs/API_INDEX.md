# API index

The API is served by `app/main.py` and uses JSON over HTTP.

- `GET /health`: service health and environment label.
- `GET /api/state`: projects, chats, and workspace state.
- `/api/projects/*`: project lifecycle, folders, goals, tasks, notes, and workspace data.
- `/api/chats/*`: messages, context packet, and run creation.
- `/api/runs/*`: stages, artifacts, events, diff, review, and optional runtime start.
- `/api/goals/*`, `/api/daily-plan/*`, `/api/everyday/*`: planning and goal data.
- `/api/memory/*`: project/user memory items.

For exact request schemas and behavior, inspect the route and schema definitions in `app/main.py` and `app/schemas.py`, then add tests in `tests/test_api.py`.
