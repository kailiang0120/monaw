"""Write THIRD_PARTY_NOTICES.txt for the Windows package.

Run from the backend folder, so the packaged dependencies and PyInstaller are installed:
    uv run --locked --group packaging python ../scripts/generate-third-party-notices.py

It covers what the installer ships besides Electron and Chromium, whose notices
electron-builder adds itself: the Python runtime, the PyInstaller bootloader, the
locked runtime Python packages, and the npm packages in app.asar or the renderer
bundle. License texts come from each package's own files. Texts a package leaves
out, and notes on statically linked libraries, are kept in third-party-notices/.
Fails if any shipped package has no license text.
"""

from __future__ import annotations

import argparse
import importlib.metadata as metadata
import json
import re
import subprocess
import sys
from pathlib import Path

from packaging.markers import Marker
from packaging.utils import canonicalize_name

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPO_ROOT / "backend"
FRONTEND_ROOT = REPO_ROOT / "frontend"
NOTES_ROOT = Path(__file__).resolve().parent / "third-party-notices"
DEFAULT_OUTPUT = FRONTEND_ROOT / "build" / "THIRD_PARTY_NOTICES.txt"

LICENSE_FILE = re.compile(r"^(licen[cs]e|copying|notice|authors)", re.IGNORECASE)
RULE = "=" * 78

# Packages that write their own code into the renderer bundle without being imported:
# Tailwind emits its base styles into the CSS.
RENDERER_BUILD_PACKAGES = ("tailwindcss",)


class Component:
    def __init__(self, name: str, version: str, license_name: str, texts: list[tuple[str, str]]):
        self.name = name
        self.version = version
        self.license_name = license_name
        self.texts = texts


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace").strip()


def note(group: str, name: str) -> list[tuple[str, str]]:
    path = NOTES_ROOT / group / f"{name}.txt"
    return [("Note from Monaw", read_text(path))] if path.is_file() else []


def python_license_name(dist: metadata.Distribution) -> str:
    expression = dist.metadata.get("License-Expression")
    if expression:
        return expression
    classifiers = [
        value.split("::")[-1].strip()
        for value in dist.metadata.get_all("Classifier") or []
        if value.startswith("License ::")
    ]
    if classifiers:
        return "; ".join(classifiers)
    return (dist.metadata.get("License") or "see license text").splitlines()[0][:80]


def python_license_texts(dist: metadata.Distribution) -> list[tuple[str, str]]:
    texts = []
    for file in sorted(dist.files or [], key=str):
        relative = str(file).replace("\\", "/")
        parts = relative.split("/")
        if not parts[0].endswith(".dist-info") or file.suffix in {".py", ".pyc"}:
            continue
        if LICENSE_FILE.match(parts[-1]):
            texts.append(("/".join(parts[1:]), read_text(Path(dist.locate_file(file)))))
    return texts


def locked_python_packages() -> list[tuple[str, str]]:
    """Runtime packages from uv.lock whose markers hold on this (Windows) machine."""
    exported = subprocess.run(
        ["uv", "export", "--locked", "--no-dev", "--no-hashes", "--no-emit-project", "--format", "requirements-txt"],
        cwd=BACKEND_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    packages = []
    for line in exported.splitlines():
        if not line or not line[0].isalnum():
            continue
        requirement, _, marker = line.partition(";")
        name, _, version = requirement.strip().partition("==")
        if marker.strip() and not Marker(marker.strip()).evaluate():
            continue
        packages.append((canonicalize_name(name.split("[")[0]), version))
    return sorted(packages)


def python_components() -> list[Component]:
    components = []
    for name, version in locked_python_packages():
        dist = metadata.distribution(name)
        if dist.version != version:
            raise SystemExit(f"{name} {dist.version} is installed but uv.lock has {version}; run uv sync --locked.")
        texts = python_license_texts(dist) + note("python", name)
        components.append(Component(name, version, python_license_name(dist), texts))
    return components


def runtime_components() -> list[Component]:
    python = Component(
        "Python",
        sys.version.split()[0],
        "PSF-2.0 and others",
        [
            ("LICENSE.txt", read_text(Path(sys.base_prefix) / "LICENSE.txt")),
            ("Incorporated software", read_text(NOTES_ROOT / "components" / "python-incorporated.txt")),
            ("Other libraries", read_text(NOTES_ROOT / "components" / "python-runtime-extra.txt")),
        ],
    )
    pyinstaller = metadata.distribution("pyinstaller")
    bootloader = Component(
        "PyInstaller bootloader (monaw-backend.exe)",
        pyinstaller.version,
        "GPL-2.0-or-later with the bootloader exception",
        python_license_texts(pyinstaller),
    )
    libxml = Component(
        "libxml2 and libxslt (in lxml)",
        "",
        "MIT",
        [("Copyright", read_text(NOTES_ROOT / "components" / "libxml2-libxslt.txt"))],
    )
    return [python, bootloader, libxml]


def npm_package_dir(name: str, start: Path) -> Path | None:
    directory = start
    while True:
        candidate = directory / "node_modules" / name
        if (candidate / "package.json").is_file():
            return candidate
        if directory.parent == directory:
            return None
        directory = directory.parent


def renderer_imports(declared: set[str]) -> set[str]:
    """Packages the shipped renderer and Electron code import."""
    found = set()
    pattern = re.compile(r"""(?:from|import)\s*\(?\s*['"]([^'"./][^'"]*)['"]""")
    for folder in ("src", "electron"):
        for path in (FRONTEND_ROOT / folder).rglob("*"):
            is_test = ".test." in path.name or "test" in path.relative_to(FRONTEND_ROOT).parts
            if path.suffix not in {".ts", ".tsx", ".js", ".cjs"} or is_test:
                continue
            for specifier in pattern.findall(path.read_text(encoding="utf-8")):
                parts = specifier.split("/")
                package = "/".join(parts[:2]) if specifier.startswith("@") else parts[0]
                if package in declared:
                    found.add(package)
    return found


def npm_components() -> list[Component]:
    manifest = json.loads((FRONTEND_ROOT / "package.json").read_text(encoding="utf-8"))
    runtime = set(manifest.get("dependencies", {}))
    declared = runtime | set(manifest.get("devDependencies", {}))
    roots = sorted((runtime | renderer_imports(declared)) - {"electron"})

    found: dict[str, Component] = {}

    def add(name: str, start: Path, with_dependencies: bool) -> None:
        directory = npm_package_dir(name, start)
        if directory is None:
            raise SystemExit(f"npm package {name} is not installed; run npm ci in frontend.")
        package = json.loads((directory / "package.json").read_text(encoding="utf-8"))
        key = f"{package['name']}@{package['version']}"
        if key in found:
            return
        license_name = package.get("license") or "see license text"
        if isinstance(license_name, dict):
            license_name = license_name.get("type", "see license text")
        texts = [
            (path.name, read_text(path))
            for path in sorted(directory.iterdir())
            if path.is_file() and LICENSE_FILE.match(path.name)
        ] + note("npm", package["name"])
        found[key] = Component(package["name"], package["version"], license_name, texts)
        if with_dependencies:
            for dependency in sorted({**package.get("dependencies", {}), **package.get("optionalDependencies", {})}):
                if npm_package_dir(dependency, directory) is not None:
                    add(dependency, directory, True)

    for name in roots:
        add(name, FRONTEND_ROOT, True)
    for name in RENDERER_BUILD_PACKAGES:
        add(name, FRONTEND_ROOT, False)
    return sorted(found.values(), key=lambda component: (component.name, component.version))


def render(version: str, sections: list[tuple[str, list[Component]]]) -> str:
    lines = [
        f"Third-party software notices for Monaw {version}",
        "",
        "Monaw is licensed under the Apache License 2.0 (LICENSE.monaw.txt). The installer",
        "also includes the third-party software listed below, each under its own license.",
        "Electron and Chromium notices are in LICENSE.electron.txt and LICENSES.chromium.html",
        "in this folder.",
        "",
        "Components under the GNU LGPL, shipped unmodified and replaceable:",
        "- python-telegram-bot (LGPL-3.0): resources/backend/_internal/telegram",
        "- GNU libiconv (LGPL-2.1-or-later), statically linked into lxml: resources/backend/_internal/lxml",
        "See their entries below for where to get the source.",
        "",
    ]
    for title, components in sections:
        lines += [RULE, title, RULE, ""]
        for component in components:
            lines.append(f"  {component.name} {component.version}".rstrip() + f"  ({component.license_name})")
        lines.append("")
    for title, components in sections:
        for component in components:
            lines += [RULE, f"{component.name} {component.version}".rstrip(), f"License: {component.license_name}", RULE, ""]
            for label, text in component.texts:
                lines += [f"--- {label} ---", "", text, ""]
    return "\n".join(lines).rstrip() + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    sections = [
        ("Runtime", runtime_components()),
        ("Python packages (backend)", python_components()),
        ("npm packages (desktop app)", npm_components()),
    ]
    missing = [component.name for _, components in sections for component in components if not component.texts]
    if missing:
        raise SystemExit(
            "No license text found for: " + ", ".join(missing) + ". Add one under scripts/third-party-notices/."
        )

    version = json.loads((FRONTEND_ROOT / "package.json").read_text(encoding="utf-8"))["version"]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render(version, sections), encoding="utf-8")
    count = sum(len(components) for _, components in sections)
    print(f"Wrote {count} components to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
