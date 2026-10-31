# Windows packaging

Monaw can be built as a Windows installer that runs without Python, uv, Node, npm, Vite or Git on the user's machine. The packaged app runs a frozen backend built with PyInstaller instead of the source backend.

This is the first packaging candidate: unsigned, x64 only, and not yet tested on a clean machine or through an upgrade. See "Not yet verified".

## Build

From the repository root on Windows, with the development prerequisites installed:

```powershell
.\scripts\package-windows.ps1            # frozen backend, then the installer
.\scripts\package-windows.ps1 -Unpacked  # frozen backend, then release\win-unpacked only
.\scripts\package-windows.ps1 -SkipBackend -Unpacked  # reuse backend\dist\monaw-backend
```

| Step | Tool | Output |
| --- | --- | --- |
| Frozen backend | PyInstaller (`packaging` dependency group, `backend/packaging/monaw-backend.spec`) | `backend/dist/monaw-backend/monaw-backend.exe` and `_internal/` |
| App and installer | electron-builder (`build` in `frontend/package.json`) | `frontend/release/win-unpacked/` and `frontend/release/Monaw-Setup-<version>-x64.exe` |

The installer is per-user by default, lets the user choose the folder, and keeps user data on uninstall.

## Layout and runtime differences

```text
Monaw.exe                          Electron
resources/app.asar                 Built renderer, Electron main and preload
resources/backend/monaw-backend.exe  Frozen backend
resources/backend/_internal/       Python runtime, libraries, and the app package as plain files
resources/browser-extension/       The Monaw Chrome extension that setup copies for the user
```

- Electron starts `resources/backend/monaw-backend.exe --host 127.0.0.1 --port <port>` when it exists (`frontend/electron/backend-launch.cjs`). Development, and a package built without the frozen backend, still run `uv run uvicorn app.main:app` from the source.
- The frozen executable also accepts `-m <module>` and `-c <code>`, because the backend starts helpers with `sys.executable` (the built-in browser server and the detached Chrome launcher). See `backend/monaw_backend.py`.
- The app package ships as plain files beside the frozen modules. Skills load from their folders, migrations and prompts are read from disk, and modules find resources relative to their own files.
- Electron points the backend at the bundled extension with `MONAW_BROWSER_EXTENSION_SOURCE`.
- All mutable data stays in the Monaw home (`~/.monaw` by default): settings, conversations, memory, runtime files, the installed copy of the extension, and skills the user creates (`~/.monaw/skills`). The install folder is never written to, so it can be replaced on upgrade.

## Measurements (first candidate, 31 October 2026)

Windows 11 x64 development machine; Python 3.11 backend, Electron 41.3.0, PyInstaller 6.22.3, electron-builder 26.18.0.

| Measurement | Result |
| --- | --- |
| Frozen backend build | 122 seconds |
| Unpacked app build (renderer + electron-builder `--dir`) | 25 seconds |
| Installer build | 87 seconds |
| Frozen backend | 381 MB, 1,422 files. Largest: ffmpeg from `imageio-ffmpeg` 84 MB, `av` libraries 63 MB, `ctranslate2` 60 MB, `onnxruntime` 35 MB |
| Installed size | 755 MB (backend 381 MB, `app.asar` 34 MB, Electron runtime the rest) |
| Installer | 220 MB |
| Frozen backend to `/health`, standalone | 1.3–1.7 seconds |
| Packaged app launch to healthy backend, first start | 4 seconds |
| Optional download | The local speech model (Whisper base, about 142 MB) downloads only when the user asks for it |

With `PATH` limited to the Windows folders (no Python, uv, Node, npm or Git), the frozen backend served `/health` and authenticated API calls. It loaded every built-in skill from disk, with web search unavailable only because no Tavily key was set. It started the built-in browser server through `-m` (connected, 20 tools) and ran `-c`. The packaged app started the frozen backend, the renderer made authenticated calls, and killing the app left no Monaw processes.

### Candidate `fdf05ae`

Built from commit `fdf05ae`: `Monaw-Setup-1.0.0-x64.exe`, 220.4 MB, SHA-256 `F7FB6CC5922A479DF52D25E0D4E7675E646915F8B8B3A6B01622D12656D81C7B`. With `measure-baseline.ps1`: cold start to a healthy backend 1.75 s, warm 1.61 s, no API requests while idle for 60 s, fresh database 4,096 bytes. The packaged-app checks passed (5 of 5).

### Candidate `378725e`

Built from commit `378725e`, the first commit on which GitHub-hosted CI passed every job, in 196 s: `Monaw-Setup-1.0.0-x64.exe`, 220.4 MB (231,089,429 bytes), SHA-256 `671FE209AC83B3064D9AF92BE19A75604CDBB53F08AC0EA4302945DAB20D9F5D`. Unsigned and not installed. With `measure-baseline.ps1 -SkipTests`: cold start to a healthy backend 1.76 s, warm 1.57 s, no API requests while idle for 60 s, fresh database 4,096 bytes. The packaged-app checks passed (5 of 5, 26.3 s). The version is still 1.0.0, so this file replaces the `fdf05ae` installer of the same name.

## Licensing to resolve before publishing

The ffmpeg executable bundled by `imageio-ffmpeg` is a GPLv3 build (`--enable-gpl --enable-version3`, with x264 and x265). Monaw is Apache-2.0. Distributing that binary in the installer carries GPLv3 obligations for it. The options are to ship an LGPL ffmpeg build, decode audio through the `av` libraries already bundled, or download ffmpeg on demand. Speech-to-text is its only user (`backend/app/agent/speech_to_text.py`). Other bundled native packages checked (`av`, `ctranslate2`, `onnxruntime`, `faster-whisper`, `pywin32`) use permissive licenses. A complete third-party notice file has not been produced.

## Not yet verified

- Installing on a clean Windows machine. The packaged-app checks below run the unpacked build, not an installed one.
- Upgrading from one installed version to the next while keeping conversations, memory, settings and stored credentials. There is no automatic update.
- Code signing. The installer and executables are unsigned, so Windows SmartScreen will warn.
- Model-driven workflows in the packaged app. The source workflows (`npm run test:app`) substitute the model provider through the Python path, which a frozen backend does not load, and the packaged build has no test hooks.

## Packaged-app checks

`npm run test:packaged` (from `frontend`) drives `release/win-unpacked/Monaw.exe`, or the app named by `MONAW_PACKAGED_APP`, with only the Windows folders on `PATH` and a Monaw home whose path contains spaces and non-ASCII characters. It checks:

- the first start and the first-run guide;
- settings kept across a restart;
- a second launch, which leaves the running app and its single backend alone;
- an occupied port, which is reported and the other program left running;
- a backend that stops while starting, which is reported.

Startup failures are also written to `runtime/backend.log`.

`scripts/measure-baseline.ps1` measures the packaged app's cold and warm start, idle API requests and fresh database size, and records the commit it measured.
