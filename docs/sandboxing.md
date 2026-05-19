# Sandboxing

Monaw routes shell execution through the sandbox layer before a process starts. Approval and access grants still run first; sandbox selection and environment hardening run after those gates.

For the approval, permission profile, and access-grant layer that runs before sandbox selection, see [permissions.md](permissions.md).

## Modes

- `disabled` / `off`: compatibility mode. Sync `exec` uses `local_direct`, which is not isolated.
- `auto`: chooses by command profile. Untrusted commands use Docker when available and are blocked when a strong backend is required but unavailable. Standard and host-required commands prefer `local_restricted`, then `local_direct` for compatibility.
- `enforce`: requires a strong backend. If Docker is unavailable, sync `exec` is blocked with `sandbox_backend_unavailable`.
- `docker`: explicitly require Docker.
- `local_restricted`: explicitly use advisory local execution.

## Backends

- `local_direct`: runs on the host with scrubbed environment variables. It provides no filesystem, process, or network isolation.
- `local_restricted`: runs on the host with process cleanup and timeout handling. This is advisory only and does not isolate filesystem or network access.
- `docker`: runs sync bash commands in a named container with `--network none`, `--cap-drop ALL`, optional `--security-opt no-new-privileges`, optional read-only root filesystem, tmpfs temp, Docker pull policy, timeout cleanup, and CPU/memory/PID limits.

## Environment

Subprocesses no longer inherit the full host environment. Local backends inherit only a small OS allowlist, and secret-looking host variables such as `OPENAI_API_KEY` are stripped. Docker containers do not inherit host environment variables; only explicit non-secret env values are passed into the container. Explicit env keys that look like secrets are blocked with `secret_env_not_supported`.

## Filesystem And Artifacts

Sandbox path handling is centralized in `app.agent.sandbox.path_policy`. Copy-in/copy-out helpers canonicalize paths, validate run IDs, and enforce allowed roots and size limits. The current sync runners do not bind arbitrary host paths into Docker by default. Runner output is capped by sandbox resource settings, artifacts are written under `backend/.runtime`, and a manifest path is included in sandbox metadata.

## Troubleshooting

- Docker not installed or not running: use `auto` or `disabled`, or install/start Docker before using `docker` or `enforce`.
- Enforce mode blocks commands: Docker is unavailable or disabled.
- Command cannot see expected files: Docker does not mount arbitrary host paths by default.
- Bash command fails in Docker: the configured image must include `bash`.
- Network calls fail in Docker: the strong backend defaults to no network.

Sandbox metadata in tool results and audit events is intended to be honest: local direct is never reported as isolated, and local restricted is reported as advisory.
