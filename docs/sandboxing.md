# Sandboxing

Monaw evaluates shell commands before process creation and produces a policy decision containing the command trust class, required isolation strength, selected backend, effective network/filesystem policy, reason code, and approval requirement.

## Modes

- `off` / legacy `disabled`: shell execution is disabled.
- `auto`: untrusted commands require Docker or another strong backend. Standard commands prefer strong isolation and may use a host runner only after explicit host-execution approval. Host-required commands always need that approval.
- `enforce`: every command requires strong isolation; unavailable isolation returns `sandbox_backend_unavailable`.
- `host`: explicit host execution. Every command requires approval and displays a no-isolation warning.
- `docker`: explicitly require Docker.
- `local_restricted`: explicit advisory host execution; it still requires host-execution approval.

## Isolation guarantees

- `strong`: Docker container process/filesystem isolation with enforced network denial when requested.
- `advisory`: `local_restricted` host execution with process cleanup and resource controls, but no filesystem or network isolation.
- `none`: `local_direct` host execution. It is never selected silently.

Docker images must be pinned as `name@sha256:<64-hex-digest>`. Unpinned images are reported unavailable and execution returns `docker_image_not_pinned`.

## Resource and environment controls

All runners clamp execution time and captured output. Docker additionally applies memory, CPU, and PID limits. Subprocesses receive a minimal environment allowlist; secret-looking explicit variables are rejected with `secret_env_not_supported`.

Docker network denial uses `--network none`, drops capabilities, enables no-new-privileges when configured, uses a read-only root filesystem by default, and mounts no arbitrary host paths. Local host runners never claim network or filesystem isolation.

Sandbox metadata and audit events record the selected backend, trust class, required isolation, effective restrictions, and reason code.

## Troubleshooting

- `sandbox_backend_unavailable`: install/start Docker, configure a pinned image digest, or explicitly choose host mode and approve the host run.
- `docker_image_not_pinned`: replace a tag such as `python:3.12-slim` with an immutable digest reference.
- `shell_execution_disabled`: change mode from `off` only if shell execution is intended.
- Docker commands currently require the `bash` shell.
