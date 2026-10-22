# Security and local data

Helm is intended to run locally with a trusted user. Treat its database, workspace, prompts, run artifacts, and logs as private user data.

## Safe defaults

- Keep database files, workspace contents, logs, environment files, and backups out of Git.
- Bind the development server to `127.0.0.1`; do not expose it to a network without adding authentication, TLS, and an explicit threat review.
- Configure any agent/provider credentials in the local environment or the runtime's own credential store. Never commit them.
- Runtime-state database discovery is disabled by default; set `HELM_RUNTIME_STATE_DB` explicitly if tool-activity harvesting is needed.
- Project deletion removes Helm-managed records. It must not recursively delete the user's project root or files beneath it.
- Existing-folder access must resolve beneath the allowed workspace/home boundary and reject symlink escapes.
- External actions remain subject to explicit review/approval; an agent run completing is not itself authorization.

## Reporting a vulnerability

Do not open a public issue containing credentials, personal data, or a live exploit. If private vulnerability reporting is available on GitHub, use it; otherwise open a minimal issue asking the maintainer for a private reporting channel without including exploit details.

## Scope note

This repository does not implement a public multi-user authentication layer. Keep the service loopback-only unless you add and verify a suitable access-control boundary.
