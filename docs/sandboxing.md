# Sandboxing

Monaw classifies each shell command before starting a process. The decision records the trust class, selected backend, isolation strength, network and filesystem policy, reason code, and whether an approval is required.

## Modes

- `off` / `disabled`: shell execution is disabled.
- `auto`: Docker is preferred for normal and untrusted commands. If Docker is unavailable, normal commands use the advisory host runner and untrusted commands use that runner only after an explicit approval.
- `enforce`: every command requires Docker; an unavailable or unpinned image returns `sandbox_backend_unavailable` or `docker_image_not_pinned`.
- `host`: use the host runner with an explicit approval and no-isolation warning.
- `docker`: require Docker explicitly.
- `local_restricted`: use the advisory host runner with process-group cleanup and an explicit approval.

There is no WSL backend. Long-running sessions use the local host runners and are capped at a maximum lifetime; Docker sessions are not supported.

## Isolation guarantees

- `strong`: Docker container isolation with `--network none` when network denial is requested, dropped capabilities, PID/memory/CPU limits, and a read-only root filesystem by default.
- `advisory`: `local_restricted` host execution with process-tree cleanup and resource/output limits. It does not isolate the filesystem or network.
- `none`: `local_direct` host execution. It is selected only as a compatibility fallback and requires approval.

The shipped Docker image is pinned as `python:3.12-slim@sha256:<64-hex-digest>`. Settings can pull a tag and resolve it to an immutable digest. If the image is unpinned, capability status reports `docker_image_not_pinned` and the Docker runner will not start.

## Workspace and writes

The agent workspace is an allowed bind root by default. `direct_rw` mounts the effective workdir read-write. `copy_out` executes in a temporary run workspace and safely copies files back after the command, subject to the configured bind-root and copy-size policy. `discard` does not expose a host workdir to the container.

## Sessions

`exec_start` starts a local long-running command, `exec_poll` reads output and artifact paths, `exec_write_stdin` re-applies command and permission policy to every input payload, and `exec_stop` terminates the process. A completed session remains readable until cleanup; stale sessions are killed and expired.

## Environment and troubleshooting

Subprocesses receive a minimal environment allowlist. Secret-looking explicit variables are rejected with `secret_env_not_supported`. All runners clamp execution time and captured output; Docker also applies container resource limits.

- `sandbox_backend_unavailable`: install/start Docker, resolve a pinned image, or choose host mode and approve the run.
- `docker_image_not_pinned`: use **Resolve & pull** in Settings or enter an immutable `name@sha256:<digest>` reference.
- `shell_execution_disabled`: change the mode from `off` only if shell execution is intended.
- `unsupported_shell_for_backend`: Docker currently supports `bash` commands only.
