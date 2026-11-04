from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

os.environ.setdefault("MONAW_CONTROL_SECRET", "documentation-control-secret-32-chars")

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPO_ROOT / "backend"
DOC_PATH = REPO_ROOT / "docs" / "api.md"

sys.path.insert(0, str(BACKEND_ROOT))

from fastapi.routing import APIRoute  # noqa: E402

from app.main import app  # noqa: E402
from app.security.control_plane import _required_scope  # noqa: E402


def _iter_api_routes():
    for route in app.routes:
        if isinstance(route, APIRoute):
            yield route
            continue

        effective_route_contexts = getattr(route, "effective_route_contexts", None)
        if not callable(effective_route_contexts):
            continue
        for context in effective_route_contexts():
            if isinstance(getattr(context, "original_route", None), APIRoute):
                yield context


def _route_scope(path: str, methods: set[str]) -> str:
    method = sorted(methods)[0] if methods else "GET"
    if not path.startswith("/api"):
        return "unauthenticated"
    return _required_scope(method, path)


def generate() -> str:
    rows: list[tuple[str, str, str, str]] = []
    for route in _iter_api_routes():
        methods = {method for method in route.methods or set() if method not in {"HEAD", "OPTIONS"}}
        if not methods:
            continue
        response_model = getattr(route.response_model, "__name__", None) if route.response_model else ""
        rows.append((
            ", ".join(sorted(methods)),
            route.path,
            _route_scope(route.path, methods),
            response_model or "stream/file/none",
        ))
    rows.sort(key=lambda item: (item[1], item[0]))
    lines = [
        "# API Route Reference",
        "",
        "This file is generated from FastAPI route definitions. Update it with:",
        "",
        "```powershell",
        "uv run --project backend python scripts/generate-api-docs.py",
        "```",
        "",
        "The backend API is a privileged local control plane. All `/api` routes",
        "require an Electron-minted bearer session. `/health` is the only",
        "unauthenticated runtime endpoint.",
        "",
        "| Method | Path | Required scope | Response model |",
        "| --- | --- | --- | --- |",
    ]
    for method, path, scope, model in rows:
        lines.append(f"| `{method}` | `{path}` | `{scope}` | `{model}` |")
    lines.extend(
        [
            "",
            "## Run and recovery routes",
            "",
            "Run routes apply to desktop Runs owned by the signed Monaw profile. Each request checks the current profile and the conversation's workspace; `runs:read` and `runs:control` are separate scopes. Use `available_actions` and the current revision returned by the server as the authority for controls. Historical conversations are not backfilled into Runs during an upgrade.",
            "",
            "Recovery verifies persisted state before offering Continue. It does not replay an interrupted tool call. Supported native file effects can be checked against private snapshots; Shell, Browser, MCP, and other opaque effects can remain `needs_review` until the user inspects them. A `verify` request is read-only. Run events and operation evidence are projected onto bounded public fields; file paths, file contents, snapshot references, and raw content hashes are not returned.",
            "",
            "Run events are stored in `agent.db` and currently have no automatic age-based retention. The `410 events_expired` response means the requested sequence has a gap in the rows that remain; it is not evidence of a configured retention period. Deleting a conversation cascades its Run, operation, and event rows, while old unreferenced snapshot files are reclaimed separately by snapshot cleanup.",
            "",
            "## File-mutation routes",
            "",
            "Preview and Apply currently cover one ordinary file write in an active desktop Run. Preview records and their idempotency results are in memory, bound to the current control session, and expire after 15 minutes; they cannot be replayed after a backend restart. Apply checks current permissions and the preview's file identity and fingerprint before writing.",
            "",
            "Undo is available only for a completed, verified, supported native file operation with live evidence and matching current file state. The current permission decision must allow the inverse without confirmation or an access grant; the API has no second approval path and does not reuse once/session grants from the forward operation. A conflict leaves the changed file untouched; a missing or expired snapshot makes Undo unavailable. Undo for deleting a newly created file is unavailable outside Windows. The routes do not provide general-purpose restore or Undo for Shell, Browser, MCP, Copy, the separate `fs_rename` tool, directory-tree, link, or unsupported metadata changes. See [operations.md](operations.md) for snapshot storage and retention limits.",
            "",
            "`POST /api/privacy/delete-data` clears diagnostics and other retained runtime artifacts described in [operations.md](operations.md). It does not delete conversations, Run history, workspaces, settings, saved credentials, or recovery snapshots.",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="Fail if docs/api.md is stale")
    args = parser.parse_args()
    content = generate()
    if args.check:
        existing = DOC_PATH.read_text(encoding="utf-8") if DOC_PATH.exists() else ""
        if existing.replace("\r\n", "\n") != content:
            print(
                "docs/api.md is stale; run "
                "uv run --project backend python scripts/generate-api-docs.py",
                file=sys.stderr,
            )
            return 1
        return 0
    DOC_PATH.write_text(content, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
