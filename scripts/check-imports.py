"""Verify that every backend module in a working tree compiles and imports.

This is the per-commit integrity check. It does not run tests; it answers a
narrower question that a full-suite run at HEAD cannot: is this single commit
internally consistent? A commit that references a module which only lands in a
later commit still merges cleanly and still passes CI at HEAD, but it cannot be
built, bisected, or reverted on its own.

Usage:
    python scripts/check-imports.py [--tree <repo-root>]

Imports run in this process, so a failure in a package `__init__` cascades to
its submodules. The report groups failures by error text so the root cause is
the first thing on screen.
"""

from __future__ import annotations

import argparse
import importlib
import os
import sys
import tempfile
from collections import defaultdict
from pathlib import Path


def _module_names(package_root: Path) -> list[str]:
    """Return importable dotted names for every .py file under the package."""
    names: list[str] = []
    for path in sorted(package_root.rglob("*.py")):
        relative = path.relative_to(package_root.parent).with_suffix("")
        parts = list(relative.parts)
        if any(part.startswith(".") for part in parts):
            continue
        if parts[-1] == "__init__":
            parts = parts[:-1]
        if parts:
            names.append(".".join(parts))
    # Import shallow modules first so a broken package __init__ is reported
    # before the submodules that inherit its failure.
    return sorted(names, key=lambda name: (name.count("."), name))


def _compile_failures(package_root: Path) -> list[tuple[str, str]]:
    failures: list[tuple[str, str]] = []
    for path in sorted(package_root.rglob("*.py")):
        # utf-8-sig, not utf-8: Python's own source loader strips a BOM, so a
        # BOM must not be reported here as an invalid character.
        source = path.read_text(encoding="utf-8-sig", errors="replace")
        try:
            compile(source, str(path), "exec")
        except SyntaxError as exc:
            location = f"{path}:{exc.lineno or 0}"
            failures.append((location, f"{type(exc).__name__}: {exc.msg}"))
    return failures


def _import_failures(names: list[str]) -> list[tuple[str, str]]:
    failures: list[tuple[str, str]] = []
    for name in names:
        try:
            importlib.import_module(name)
        except BaseException as exc:  # noqa: BLE001 - report, never abort the sweep
            failures.append((name, f"{type(exc).__name__}: {exc}"))
    return failures


def _report(title: str, failures: list[tuple[str, str]], *, limit: int = 12) -> None:
    grouped: dict[str, list[str]] = defaultdict(list)
    for subject, error in failures:
        grouped[error].append(subject)
    print(f"{title}: {len(failures)} failure(s) across {len(grouped)} distinct error(s)")
    for error, subjects in sorted(grouped.items(), key=lambda item: -len(item[1])):
        shown = ", ".join(subjects[:limit])
        remainder = f" (+{len(subjects) - limit} more)" if len(subjects) > limit else ""
        print(f"  {error}")
        print(f"    {shown}{remainder}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--tree",
        default=str(Path(__file__).resolve().parents[1]),
        help="Repository root to check (defaults to this script's repository).",
    )
    args = parser.parse_args()

    tree = Path(args.tree).resolve()
    backend = tree / "backend"
    package_root = backend / "app"
    if not package_root.is_dir():
        print(f"No backend/app package under {tree}", file=sys.stderr)
        return 2

    compile_failures = _compile_failures(package_root)
    if compile_failures:
        _report("Syntax", compile_failures)
        return 1

    # Import for side effects only inside a throwaway home: several modules
    # create runtime directories at import time.
    with tempfile.TemporaryDirectory(prefix="monaw-import-check-") as home:
        os.environ["MONAW_HOME"] = home
        sys.path.insert(0, str(backend))
        names = _module_names(package_root)
        failures = _import_failures(names)

    if failures:
        _report("Import", failures)
        print(f"\nChecked {len(names)} module(s) under {package_root}")
        return 1

    print(f"OK: {len(names)} module(s) under {package_root} compile and import")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
