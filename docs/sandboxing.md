# Sandboxing

Monaw classifies each shell command before starting a process. The decision records the trust class, selected backend, isolation strength, network and filesystem policy, reason code, and whether an approval is required.

## Modes

- `off` / `disabled`: shell execution is disabled.
- `auto`: Docker is used for the narrow set of allowlisted local-inspection commands, self-contained Python snippets, and dangerous untrusted shapes that are safer when contained. Normal developer tooling, project scripts/imports, state-changing package commands, and PowerShell/cmd syntax use the advisory host runner only after explicit approval.
- `enforce`: Docker is required for Docker-compatible commands; an unavailable or unpinned image returns `sandbox_backend_unavailable` or `docker_image_not_pinned`. Incompatible shells and commands outside the image allowlist fail closed with an actionable blocked decision; there is no host fallback.
- `host`: use the host runner with an explicit approval and no-isolation warning.
- `docker`: require Docker explicitly; incompatible shells and commands outside the image allowlist fail closed instead of falling back to the host.
- `local_restricted`: use the advisory host runner with process-group cleanup and an explicit approval.

There is no WSL backend. Long-running sessions use the local host runners and are capped at a maximum lifetime; Docker sessions are not supported.

## Isolation guarantees

- `strong`: Docker container isolation with `--network none` when network denial is requested, dropped capabilities, PID/memory/CPU limits, and a read-only root filesystem by default.
- `advisory`: `local_restricted` host execution with process-tree cleanup and resource/output limits. It does not isolate the filesystem or network.
- `none`: `local_direct` host execution. It is selected only as a compatibility fallback and requires approval.

The shipped Docker image is pinned as `python:3.12-slim@sha256:<64-hex-digest>`. Settings can pull a tag and resolve it to an immutable digest. Resolution also probes which standard-library modules are actually importable in that exact image and caches the inventory under the immutable reference. The probe runs without network access, capabilities, or a writable root; each import uses a separate isolated interpreter. If the image is unpinned, capability status reports `docker_image_not_pinned` and the Docker runner will not start.

Docker runs use the pinned Python image, not the host's toolchain. Auto routing is fail-safe: a command is sent to Docker only when every shell segment uses a known-safe image executable and any Python `-c` code is self-contained or uses imports verified against the exact image inventory. The shipped 3.12-slim image has a static offline fallback; it excludes removed modules, Windows-only modules, and Tk modules omitted by the slim image. An unsupported image without a matching probe fails closed to the approval-required host runner rather than borrowing the host interpreter's inventory. Explicit `shell=bash` does not bypass this compatibility check. Unknown tools, project scripts/imports, process-spawning Python code, and state-changing package commands are routed to the approval-required host runner in auto mode and blocked in `docker`/`enforce` modes. Process detection covers the `os.exec*` and `os.spawn*` families plus `os.popen`, `os.posix_spawn*`, and subprocess convenience APIs. A self-contained `python -c` snippet and contained dangerous pipeline can remain in Docker. Docker denies network access by default; a requested allow mode uses Docker's normal bridge network and is not a network-isolated run. `direct_rw` writes through the allowed workspace bind, `copy_out` copies changed/new regular files back from a temporary workspace, and `discard` leaves container changes ephemeral.

## Workspace and writes

The agent workspace is an allowed bind root by default. `direct_rw` mounts the effective workdir read-write. `copy_out` executes in a temporary run workspace and safely copies only changed/new regular files back after the command; deletions are not propagated and symlinks are rejected, subject to configured bind-root, copy-in, and copy-out size limits. `discard` does not expose a host workdir to the container and all container changes are ephemeral.

## Sessions

`exec_start` starts a local long-running command, `exec_poll` reads output and artifact paths, `exec_write_stdin` re-applies command and permission policy to every input payload, and `exec_stop` terminates the process. A completed session remains readable until cleanup; stale sessions are killed and expired.

## Environment and troubleshooting

Subprocesses receive a minimal environment allowlist. Secret-looking explicit variables are rejected with `secret_env_not_supported`. All runners clamp execution time and captured output; Docker also applies container resource limits.

- `sandbox_backend_unavailable`: install/start Docker, resolve a pinned image, or choose host mode and approve the run.
- `docker_image_not_pinned`: use **Resolve & pull** in Settings or enter an immutable `name@sha256:<digest>` reference.
- `shell_execution_disabled`: change the mode from `off` only if shell execution is intended.
- `unsupported_shell_for_backend`: Docker supports bash commands only; `auto` reports an approval-required host fallback for incompatible shell syntax, while `docker`/`enforce` block it.
