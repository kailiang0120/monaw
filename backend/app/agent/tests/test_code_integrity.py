"""Static checks that catch code which only fails when its line runs."""

from __future__ import annotations

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
