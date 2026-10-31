# PyInstaller spec for the packaged Windows backend.
#
# Build from the backend folder:
#     uv run --locked --group packaging pyinstaller packaging/monaw-backend.spec --noconfirm
# Output: dist/monaw-backend/monaw-backend.exe with its _internal folder.
#
# The app package is also copied as plain files, because skills are loaded
# from their folders at runtime (SKILL.md and tools.py), migrations and
# prompts are read from disk, and modules locate resources from __file__.

from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules

backend = Path(SPECPATH).parent
app_dir = backend / "app"

datas = []
for path in app_dir.rglob("*"):
    if not path.is_file() or "__pycache__" in path.parts or "tests" in path.relative_to(app_dir).parts:
        continue
    datas.append((str(path), str(path.parent.relative_to(backend))))

binaries = []
hiddenimports = collect_submodules("app", filter=lambda name: ".tests" not in name)
# Packages that load data files, plugins or native libraries at runtime.
# mcp.cli is the MCP command-line tool, which needs typer and is not used here.
for package in ("tiktoken", "tiktoken_ext", "scrapling", "openai_codex", "mcp", "uvicorn"):
    package_datas, package_binaries, package_hidden = collect_all(
        package, filter_submodules=lambda name: not name.startswith("mcp.cli")
    )
    datas += package_datas
    binaries += package_binaries
    hiddenimports += package_hidden

a = Analysis(
    [str(backend / "monaw_backend.py")],
    pathex=[str(backend)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["pytest", "_pytest", "tkinter", "mcp.cli"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="monaw-backend",
    # A console program so stdout/stderr reach Electron's log; Electron starts it hidden.
    console=True,
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="monaw-backend", upx=False)
