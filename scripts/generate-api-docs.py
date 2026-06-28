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


def _route_scope(path: str, methods: set[str]) -> str:
    method = sorted(methods)[0] if methods else "GET"
    if not path.startswith("/api"):
        return "unauthenticated"
    if path.startswith("/api/settings"):
        return "settings:read" if method == "GET" else "settings:write"
    if path.startswith("/api/sandbox"):
        return "settings:read"
    if path.startswith("/api/approvals") or path.startswith("/api/access-grants"):
        return "approval:resolve"
    if path.startswith("/api/diagnostics") or path.startswith("/api/observability"):
        return "diagnostics:read" if method == "GET" else "diagnostics:control"
    if path.startswith("/api/files"):
        return "files:read"
    return "agent:run"


def generate() -> str:
    rows: list[tuple[str, str, str, str]] = []
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
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
        "python scripts/generate-api-docs.py",
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
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="Fail if docs/api.md is stale")
    args = parser.parse_args()
    content = generate()
    if args.check:
        existing = DOC_PATH.read_text(encoding="utf-8") if DOC_PATH.exists() else ""
        if existing.replace("\r\n", "\n") != content:
            print("docs/api.md is stale; run python scripts/generate-api-docs.py", file=sys.stderr)
            return 1
        return 0
    DOC_PATH.write_text(content, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
