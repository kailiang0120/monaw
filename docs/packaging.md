# Windows packaging

Monaw can be built as a Windows installer that runs without Python, uv, Node, npm, Vite or Git on the user's machine. The packaged app runs a frozen backend built with PyInstaller instead of the source backend.

The installer is unsigned and supports Windows x64. Build measurements, packaged-app checks, and upgrade-check results are recorded below; see "Not yet verified" for remaining verification limits.

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
THIRD_PARTY_NOTICES.txt            Third-party licenses (see "Third-party licenses")
LICENSE.monaw.txt                  Monaw's license
resources/app.asar                 Built renderer, Electron main and preload
resources/backend/monaw-backend.exe  Frozen backend
resources/backend/_internal/       Python runtime, libraries, and the app package as plain files
resources/browser-extension/       The Monaw Chrome extension that setup copies for the user
```

- Electron starts `resources/backend/monaw-backend.exe --host 127.0.0.1 --port <port>` when it exists (`frontend/electron/backend-launch.cjs`). Development, and a package built without the frozen backend, still run `uv run uvicorn app.main:app` from the source.
- The frozen executable also accepts `-m <module>` and `-c <code>`, because the backend starts helpers with `sys.executable` (the built-in browser server and the detached Chrome launcher). See `backend/monaw_backend.py`.
- The app package ships as plain files beside the frozen modules. Skills load from their folders, migrations and prompts are read from disk, and modules find resources relative to their own files.
- Electron points the backend at the bundled extension with `MONAW_BROWSER_EXTENSION_SOURCE`.
- Signing in with ChatGPT needs no bundled runtime: the backend runs OpenAI's sign-in flow itself and calls the Responses API (`app/agent/chatgpt_account.py`). The packaged-app check `reports ChatGPT sign-in status without development tools` covers it.
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

With `PATH` limited to the Windows folders (no Python, uv, Node, npm or Git), the frozen backend served `/health` and authenticated API calls. It loaded every built-in skill from disk, with web search unavailable only because no Tavily key was set. It started the built-in browser server through `-m` (connected, 20 tools) and ran `-c`. The packaged app started the frozen backend, the renderer made authenticated calls, and killing the app left no Monaw processes.

### Candidate `fdf05ae`

Built from commit `fdf05ae`: `Monaw-Setup-1.0.0-x64.exe`, 220.4 MB, SHA-256 `F7FB6CC5922A479DF52D25E0D4E7675E646915F8B8B3A6B01622D12656D81C7B`. With `measure-baseline.ps1`: cold start to a healthy backend 1.75 s, warm 1.61 s, no API requests while idle for 60 s, fresh database 4,096 bytes. The packaged-app checks passed (5 of 5).

### Candidate `378725e`

Built from commit `378725e`, the first commit on which GitHub-hosted CI passed every job, in 196 s: `Monaw-Setup-1.0.0-x64.exe`, 220.4 MB (231,089,429 bytes), SHA-256 `671FE209AC83B3064D9AF92BE19A75604CDBB53F08AC0EA4302945DAB20D9F5D`. Unsigned and not installed. With `measure-baseline.ps1 -SkipTests`: cold start to a healthy backend 1.76 s, warm 1.57 s, no API requests while idle for 60 s, fresh database 4,096 bytes. The packaged-app checks passed (5 of 5, 26.3 s). The version is still 1.0.0, so this file replaces the `fdf05ae` installer of the same name.

### Size reduction (cloud-only speech)

The measurements above are for the first candidates, which bundled local Whisper. Speech-to-text now goes to OpenAI or Google with the user's API key, so the frozen backend no longer carries `faster-whisper`, `ctranslate2`, `onnxruntime`, `av`, `imageio-ffmpeg`, `tokenizers`, `huggingface_hub` or `numpy`. The package also ships only Chromium's `en-US` language pack (`electronLanguages`), and `app.asar` no longer contains `node_modules` the renderer bundle already includes: React, the Markdown renderer and the icons moved to `devDependencies`, leaving `electron-store`, which the main process loads.

| Measurement | Before | After |
| --- | --- | --- |
| Installer | 220.4 MB | 133.2 MB (139,706,176 bytes) |
| Installed size | 755 MB | 390 MB |
| Frozen backend | 381 MB, 1,422 files | 86 MB, 1,228 files |
| `app.asar` | 34 MB | 6.1 MB |
| Chromium language packs | 55 | 1 |

Built from branch `drop-local-speech` in 168 s; SHA-256 `2AB73F4D8E202CF3580AAC9D5C56313CF559518F10DFDBD970FF9D030D27F4BD`. The packaged-app checks passed (5 of 5, 29.8 s). The frozen backend imports the OpenAI transcription client, the Gemini client and the MCP clients. With `measure-baseline.ps1 -SkipTests`: cold start 1.71 s, warm 1.58 s, no API requests while idle for 60 s, fresh database 4,096 bytes. A first run of the same measurement counted 16 idle requests per minute; a manual launch showed every request inside the first seconds of startup, and the repeat measured none.

After merging with the hybrid memory retrieval work, `main` built in 117 s: `Monaw-Setup-1.0.0-x64.exe`, 133.3 MB (139,743,918 bytes), SHA-256 `21425334263137A136D83F8F5512A53FC4116B1029F591B5A47B6FBE54E5E888`, installed size 390 MB. The packaged-app checks passed (5 of 5, 28.4 s), and the frozen backend passed the memory smoke check (semantic recall on the first query through a local embedding test server, workspace isolation, restart, forgetting and rebuilding the index). Two baseline runs: cold start 2.36 s and 1.76 s, warm 1.59 s and 1.73 s, fresh database 4,096 bytes; idle API requests 0 and 4 per minute. Across five runs today the idle count was 0, 16, 0, 0 and 4, which points to late startup requests landing in the observation window rather than polling; the baseline script does not yet separate the two.

### Release v1.0.0

The first v1.0.0 installer, built from `7d84272` (133.3 MB, SHA-256 `61E3F6BACB10E413B90B4E70D4648376B151475DD31316BE7F2F077E874D95BB`), left out the Codex runtime, so signing in with ChatGPT and chatting through that account failed with "missing its package metadata". It passed the packaged-app checks of the time because none of them reached Codex. A silent per-user install (`/S /D=<folder>`) of that build took 18 s; the installed app served `/health` 3.4 s after launch and refused an unauthenticated settings request, and a silent uninstall removed the app folder, the Start menu shortcut and the uninstall entry while keeping the Monaw home.

The replacement installer bundles `codex.exe` and `rg.exe`: `Monaw-Setup-1.0.0-x64.exe`, 207.8 MB (217,886,132 bytes), SHA-256 `355F30AF2E252C6E6AD9DFC48931EDAFDD21B080D7393AF508065F9E664715F3`, frozen backend 401 MB, unpacked app 705 MB. The packaged-app checks passed (6 of 6, 35.6 s), including the new ChatGPT status check. The frozen backend reported the account as not connected, returned an `auth.openai.com` sign-in link, and its Codex process ended with it. This installer was not installed on the development machine, which already had v1.0.0 installed.

Signing in with ChatGPT then moved to a direct sign-in, and `codex.exe` and `rg.exe` left the package. The next build, from the release commit: `Monaw-Setup-1.0.0-x64.exe`, 139.6 MB (139,588,315 bytes), SHA-256 `3CA24A52BC7F7085F52DE904C7CA82DFBDF8F1A53C2E97788A96ED4961CAE99F`, unpacked app 408 MB, no Codex files in the frozen backend. The packaged-app checks passed (6 of 6, 35.5 s). It went to a draft release for a test install, then replaced the published installer.

The PyInstaller spec leaves out `mcp.cli`, the MCP command-line tool, which needs `typer`; `typer` arrived only through `huggingface_hub`.

## Third-party licenses

`package-windows.ps1` runs `scripts/generate-third-party-notices.py`, which writes `frontend/build/THIRD_PARTY_NOTICES.txt`. The installer puts it next to `Monaw.exe` with Monaw's own license as `LICENSE.monaw.txt`. electron-builder adds `LICENSE.electron.txt` and `LICENSES.chromium.html` itself.

The file lists the Python runtime, the PyInstaller bootloader, every runtime package in `uv.lock` (no dev dependencies), the npm packages in `app.asar` and those the renderer imports, with each license text taken from the package's own files. The build fails if a shipped package has no license text. Texts a package leaves out, and notes on statically linked libraries, live in `scripts/third-party-notices/`: add one there when a new dependency trips the check. The npm list follows the imports in `frontend/src` and `frontend/electron`, so it can include a package the bundler drops, but not miss one.

Nearly everything is MIT, BSD, Apache-2.0, ISC or PSF. The rest:

| Component | License | How it is handled |
| --- | --- | --- |
| `python-telegram-bot` | LGPL-3.0 | Shipped unmodified as plain `.py` files under `_internal/telegram` (`module_collection_mode` in the spec), so it can be replaced. |
| GNU libiconv, statically linked into `lxml` on Windows | LGPL-2.1-or-later | The `lxml` modules are separate files that can be rebuilt and replaced; the notice points to the sources. |
| `certifi`, `orjson`, `tqdm` | MPL-2.0 (in part) | Unmodified; license text included. |
| `tld` | GPL-2.0, LGPL-2.1 or MPL-1.1 | Used under MPL-1.1. |
| `type-fest` | MIT or CC0-1.0 | Used under CC0-1.0. |

The GPLv3 ffmpeg build that `imageio-ffmpeg` used to bundle is gone with local speech-to-text.

## Not yet verified

- Installing on a clean Windows machine. The packaged-app checks below run the unpacked build, not an installed one.
- Upgrading from one installed version to the next. The upgrade check below covers settings, conversations and scheduled tasks; memory and stored credentials are not seeded. There is no automatic update.
- Code signing. The installer and executables are unsigned, so Windows SmartScreen will warn.
- Model-driven workflows in the packaged app, except through the live ChatGPT check below, which a person runs. The source workflows (`npm run test:app`) substitute the model provider through the Python path, which a frozen backend does not load, and the packaged build has no test hooks.

## Packaged-app checks

`npm run test:packaged` (from `frontend`) drives `release/win-unpacked/Monaw.exe`, or the app named by `MONAW_PACKAGED_APP`, with only the Windows folders on `PATH` and a Monaw home whose path contains spaces and non-ASCII characters. It checks:

- the first start and the first-run guide;
- settings kept across a restart;
- a second launch, which leaves the running app and its single backend alone;
- an occupied port, which is reported and the other program left running;
- a backend that stops while starting, which is reported.

Startup failures are also written to `runtime/backend.log`.

### Upgrade check (before publishing a release)

Run the **Upgrade check** workflow (Actions > Upgrade check > Run workflow) on the branch to release, with the tag users have now. On a clean Windows runner it builds the installer, downloads the older one from that release, and runs `scripts/check-upgrade.ps1`, which:

1. installs the older release silently into a folder with a space in its name;
2. starts its backend against a fresh Monaw home and saves a user name, a conversation with workspace and historical message, and a disabled scheduled task;
3. starts `Monaw.exe` until its backend is healthy;
4. checks the downloaded release against its published SHA-256, records the candidate SHA-256 and commit, installs the new build over it, and verifies the installed version, `THIRD_PARTY_NOTICES.txt` and changed `app.asar`;
5. checks the settings, history, workspace and disabled scheduled task are still there and the new `Monaw.exe` starts;
6. uninstalls and checks the Monaw home is kept.

First run, 1 November 2026, from the published v1.0.0 installer to `e1fb59b` ([run 41295028277](https://github.com/kailiang0120/monaw/actions/runs/41295028277)): passed. The v1.0.0 install took 29 s and the upgrade 13 s; both versions started from the installed folder, the seeded data survived, and uninstalling kept the Monaw home.

The script refuses to run where Monaw is installed, because both installers share one uninstall entry. `-AppDir frontend\release\win-unpacked` instead checks the same data survives a restart of a build folder, without installing anything.

### Live ChatGPT check (before publishing a release)

The first v1.0.0 installer passed every check above and still could not chat through a ChatGPT account. Before publishing a draft release, run the check that calls the real model, against the build or an installed copy:

```powershell
cd frontend
$env:MONAW_LIVE_CHATGPT = "1"
npm run test:packaged -- -g "ChatGPT account"
```

It starts the app with a fresh Monaw home, opens the ChatGPT sign-in page in your browser and waits up to 10 minutes for you to sign in. Then it asks the model to read a file in the workspace with a tool and reply with a code from it, and checks that the saved answer has the code and a tool call. It uses a little of the account's plan. Without `MONAW_LIVE_CHATGPT` it is skipped.

`scripts/measure-baseline.ps1` measures the packaged app's cold and warm start, idle API requests and fresh database size, and records the commit it measured.
