# Helm context

This document describes the public source snapshot, not any maintainer-specific machine or deployment.

## Purpose

Helm is a local-first workspace for projects, planning, and supervised AI-agent runs. It combines a FastAPI/SQLite backend with a browser UI and an optional local runtime adapter.

## Main subsystems

- `app/main.py`: API routes, run stages, approval/review flow, and runtime lifecycle.
- `app/db.py`: SQLite schema and migrations.
- `app/workspace.py`: workspace files, project-map retrieval, context tiers, and runtime contracts.
- `app/schemas.py`: request validation.
- `shared-ui/`: browser application, styles, and logo.
- `tests/`: API, safety, lifecycle, context-selection, and UI regressions.

## Safety invariants

- Project deletion removes Helm-managed records, not the user's on-disk project root.
- Opening existing folders must stay within the configured allowed root after path resolution.
- Failed checks and external actions stay visible for review; process completion is not approval.
- Database, workspace, logs, credentials, and prompts remain local and are not committed.

## Verification

Run `pytest -q` and `node --check shared-ui/app.js`. Runtime tests use fake commands; a real Hermes-compatible command is optional and configured by the operator.
