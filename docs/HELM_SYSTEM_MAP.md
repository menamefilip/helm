# Helm system map

## First-stop files

- `docs/HELM_CONTEXT.md`: product shape, safety invariants, and verification.
- `docs/FILE_INDEX.md`: file ownership by subsystem.
- `docs/API_INDEX.md`: API domains and key routes.
- `docs/UI_INDEX.md`: browser UI ownership.

## Typical change workflow

1. Read the relevant map and owning implementation.
2. Add or update a focused regression test.
3. Implement the smallest change that satisfies the behavior.
4. Run `pytest -q` and `node --check shared-ui/app.js` when UI code changed.
5. Review the diff for data-boundary, approval, and local-path regressions.

## Runtime boundary

Helm can dispatch to an optional local Hermes-compatible command configured with `HELM_RUNTIME_COMMAND`. Tests should use fake commands and isolated temporary databases/workspaces.
