# UI index

- `shared-ui/index.html`: app shell, project sidebar, workspace tabs, chat composer, and Agent Console containers.
- `shared-ui/app.js`: state hydration, API requests, project/chat navigation, Quest/Map/Chat views, context badge, and run observability.
- `shared-ui/styles.css`: theme tokens, responsive layout, and component styles.

The browser UI talks to the FastAPI service on the same origin. Keep API-backed state changes in sync with visible status and review gates.
