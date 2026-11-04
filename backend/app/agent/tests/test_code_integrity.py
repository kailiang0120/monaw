"""Static checks that catch code which only fails when its line runs."""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

from pyflakes import api, messages, reporter


class _Collect(reporter.Reporter):
    def __init__(self) -> None:
        super().__init__(None, None)
        self.found: list[str] = []

    def flake(self, message) -> None:
        if isinstance(message, messages.UndefinedName):
            self.found.append(str(message))

    def syntaxError(self, filename, msg, lineno, offset, text) -> None:  # noqa: N802 - pyflakes API
        self.found.append(f"{filename}:{lineno}: {msg}")

    def unexpectedError(self, filename, msg) -> None:  # noqa: N802 - pyflakes API
        self.found.append(f"{filename}: {msg}")


def test_backend_code_uses_no_undefined_names():
    # A renamed constant (clipboard writes) and a missing route parameter
    # (observability replay) both shipped as NameErrors before this check.
    app_dir = Path(__file__).resolve().parents[2]
    collected = _Collect()
    sources = [str(path) for path in app_dir.rglob("*.py") if "tests" not in path.parts]
    api.checkRecursive(sources, collected)
    assert collected.found == []


def test_release_version_metadata_and_installer_filename_are_synchronized():
    repo_root = Path(__file__).resolve().parents[4]
    frontend_dir = repo_root / "frontend"
    backend_dir = repo_root / "backend"
    frontend_package = json.loads((frontend_dir / "package.json").read_text(encoding="utf-8"))
    frontend_lock = json.loads((frontend_dir / "package-lock.json").read_text(encoding="utf-8"))
    backend_project = tomllib.loads((backend_dir / "pyproject.toml").read_text(encoding="utf-8"))
    backend_lock = tomllib.loads((backend_dir / "uv.lock").read_text(encoding="utf-8"))

    version = frontend_package["version"]
    assert version == "1.1.0"
    assert frontend_lock["version"] == version
    assert frontend_lock["packages"][""]["version"] == version
    assert backend_project["project"]["version"] == version
    backend_root = [package for package in backend_lock["package"] if package.get("name") == "monaw-backend"]
    assert len(backend_root) == 1
    assert backend_root[0]["version"] == version

    from app.main import app

    assert app.version == version

    build = frontend_package["build"]
    assert build["nsis"]["artifactName"] == "Monaw-Setup-${version}-x64.${ext}"
    main_source = (backend_dir / "app" / "main.py").read_text(encoding="utf-8")
    diagnostics_source = (backend_dir / "app" / "api" / "routes" / "diagnostics.py").read_text(encoding="utf-8")
    assert re.search(r'\bversion\s*=\s*"' + re.escape(version) + r'"', main_source)
    assert re.search(r'"version"\s*:\s*"' + re.escape(version) + r'"', diagnostics_source)
    installation_doc = (repo_root / "docs" / "installation.md").read_text(encoding="utf-8")
    assert f"Monaw-Setup-{version}-x64.exe" in installation_doc
