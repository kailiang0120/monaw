# Installing the Windows build

The Windows installer runs Monaw without Python, uv, Node.js or Git. [Download the latest release](https://github.com/kailiang0120/monaw/releases/latest). To run Monaw from source instead, use the detailed [Windows source setup and startup guide](source-setup.md). To build the installer, see [packaging](packaging.md).

The installer is published with each [release](https://github.com/kailiang0120/monaw/releases) together with its SHA-256 checksum. It is unsigned. See [What is not verified yet](#what-is-not-verified-yet) for the remaining verification limits.

Jump to: [requirements](#requirements) · [install](#install) · [first launch](#first-launch) · [data locations](#where-your-data-lives) · [upgrade](#upgrade) · [backup](#backup-before-changing-versions) · [uninstall](#uninstall) · [startup problems](#if-monaw-does-not-start).

## Installer or source checkout?

| Method | Initial setup | Daily launch | Updates |
| --- | --- | --- | --- |
| Windows installer | Run the downloaded `Monaw-Setup-<version>-x64.exe`. | Windows Start menu > Monaw. | Settings > About, or run a newer installer. |
| Source checkout | Clone the repository and run `setup.bat`. | Run `start.bat` from that checkout. | Update the Git checkout, rerun setup when needed, and restart. |

The batch scripts belong to the source workflow. Do not run them inside the installed application folder. Source changes are not included in an already installed release until a new app build is installed.

## Requirements

- Windows 10 or 11, 64-bit (x64). Arm devices and 32-bit Windows are not supported.
- About 400 MB of disk space for the app, plus the Monaw home folder (`%USERPROFILE%\.monaw`).
- An API key for OpenAI or Google, or an OpenAI account, to use a model.
- Optional: Google Chrome 125 or later for browser control, and Docker Desktop for isolated shell commands.
- Voice input sends recordings to OpenAI or Google and needs an API key for one of them; an OpenAI account sign-in does not cover it.

Internet access is needed to download the installer and use remote models or integrations. The app stores your history locally; choosing a cloud model still sends the content needed for that request to the selected provider. Installing Monaw does not include model-provider access or external service subscriptions.

The disk-space figure is for the installed application. Attachments, screenshots, histories, logs, workspaces, and optional Docker resources need additional space.

## Install

1. Open the [latest release](https://github.com/kailiang0120/monaw/releases/latest), expand **Assets**, and download the versioned Windows x64 installer. Use the file ending in `.exe`, rather than a GitHub source-code ZIP, `.blockmap`, or updater metadata file.
2. In PowerShell, go to the download folder and compare the download against the SHA-256 checksum published for that exact release. For example, for version `1.2.0`:

   ```powershell
   Get-FileHash .\Monaw-Setup-1.2.0-x64.exe -Algorithm SHA256
   ```

   Replace the filename with the version you downloaded. Compare the full hash, not only its first few characters. A checksum detects a changed or incomplete download; it does not replace publisher signing.

3. Run the installer. Because it is unsigned, Windows SmartScreen may warn that it is from an unknown publisher; choose **More info** > **Run anyway** only after checking that you obtained the expected release and its checksum matches.
4. Follow the installer prompts. The installer is per-user by default and lets you change the application folder. A per-user install normally does not need administrator rights. The application folder is separate from your chat data folder.
5. Open **Monaw** from the Windows Start menu. The installed app starts its included backend automatically. You do not need a terminal or a separately running Python service.
6. Let the startup screen finish loading settings and chat history. On a first launch, the app may have no conversations yet; this is different from a backend startup error.

## First launch

### Connect a model

Open **Settings > Connections** and configure one of the supported routes:

| Route | What to configure |
| --- | --- |
| ChatGPT account | Complete the account sign-in flow offered in the app. This is separate from an OpenAI API-key connection. |
| OpenAI API | Save the API key for the OpenAI API account you intend to use. |
| Google Gemini API | Save the Google API key for the account/project you intend to use. |

Select an available model and reasoning effort in the chat composer. The presence of a saved connection does not guarantee remote service availability or access to every model. If a request fails, inspect its error and the selected connection before reinstalling the app.

Desktop configuration is managed in Settings; you do not need to create a `.env` file. Saved provider keys and the Telegram token are encrypted by Electron using Windows data protection. Account authentication state is also local private data; include it in your backup and privacy considerations.

### Choose a workspace and try a task

1. Open a new chat.
2. Click **Default workspace** above the message box if you want to select a project folder. This selection is available before the chat starts; the selector is hidden afterward. Use a new chat for a different workspace.
3. Choose an appropriate [permission mode](permissions.md). Workspace selection and command isolation are separate settings; see [sandboxing](sandboxing.md).
4. Use **Get started** to inspect the model connection, folder, and permissions, or send a read-only request such as: `List the main folders in this workspace and explain what they contain. Do not modify anything.`

Successful startup and a successful model request are separate checks. Monaw can open normally even before you configure a model connection.

### Add optional capabilities

- **Browser automation:** install/pair the Monaw Chrome extension under **Settings > Browser**. Follow the [browser guide](browser-automation.md); Chrome needs the extension loaded and enabled.
- **Portable skills:** open **Settings > Tools > Skills > Import** to inspect and install a GitHub source, local folder, ZIP, or `SKILL.md`. See [skills and plugins](skills.md) for file limits and compatibility. Imported skill instructions are available for the next message; their external dependencies are separate.
- **MCP:** configure servers and their credentials under **Settings > Tools > MCP**. See [MCP](mcp.md).
- **Web search and Telegram:** configure their optional credentials and access restrictions in Settings. See [Telegram](telegram.md).
- **Scheduled tasks:** configure the workspace, model, schedule, and overlap behavior described in [scheduling](scheduling.md).

OpenAI/Codex and Claude store plugins are not installed as complete native Monaw plugins. Portable skill folders can be imported; MCP connections, host hooks, and vendor account integrations need their own supported setup.

## Where your data lives

Monaw keeps application data in the Monaw home folder, not in the install folder:

```text
%USERPROFILE%\.monaw\
  runtime\agent.db        conversations, scheduled tasks and app state
  runtime\settings.json   settings
  runtime\backend.log     the local service log
  runtime\electron\       desktop profile and encrypted credential state
  memory\                 long-term memory
  skills\                 skills you created or imported
  browser\                the copy of the Monaw extension that Chrome loads
```

New chats without a selected project save their work in
`Documents\monaw\YYYY-MM-DD`, using your Windows Documents location
(including redirected or OneDrive Documents folders). Chats created on the same
local date share that working folder, saved before the first task starts and
reused after reopening or renaming the chat. These chats appear under **Default workspace**;
chat history is stored separately in `.monaw\runtime\agent.db`.
Existing chats keep their current workspace and files; no files are moved.
`AGENT_WORKSPACE_DIR` overrides the root under which new dated chat folders are created.
If Windows cannot provide the Documents location, the desktop app uses
`%USERPROFILE%\.monaw\workspace` as the root instead.

API keys in the desktop store are encrypted using Windows data protection for your user account; they are not saved as plaintext in `settings.json`. The encrypted desktop store is still part of your local private data. Copying it to another Windows account, computer, or Monaw-home path is not a supported way to transfer a signed-in identity. The control profile is also bound to its original home path. Expect to sign in or configure connections again when moving to a different environment.

`MONAW_HOME` relocates the application data folder; `AGENT_HOME` is its legacy alternative. `AGENT_RUNTIME_DIR` changes backend runtime data but does not relocate every Electron file. Prefer `MONAW_HOME` when you need a complete separate profile. See the [environment and path reference](source-setup.md#paths-and-environment-overrides) for these overrides. Workspaces are backed up separately from the application data folder.

Settings → Storage shows how much of this folder each kind of data uses and clears what is safe to clear. The activity log, logs and screenshots, command outputs and the app's own cache can be cleared without losing anything you made. Two leftovers from earlier versions appear only while they exist: the Chrome profile Monaw used to launch for the agent (`browserprofiles`, unused since Monaw drives your own Chrome through its extension) and the offline Whisper voice model (`models\faster-whisper`). Deleting all chats or uploaded files asks first and cannot be undone, and chats cannot be deleted while a reply is running. Memory, the ChatGPT sign-in runtime and the workspace are shown but not cleared here. After deleting chats or the activity log, Monaw compacts the database so the space returns to the disk.

## Upgrade

Monaw checks GitHub Releases for a newer stable version after startup and
periodically while it is running. Open **Settings > About** to see the installed
version or check manually. An available update downloads in the background;
choose **Restart to update** after finishing or pausing active work, and confirm
the restart. Draft and prerelease versions are not installed. The existing v1.0
installer has no updater and must be upgraded manually once.

To upgrade manually:

1. Close Monaw.
2. Back up `%USERPROFILE%\.monaw` and your workspaces, including `Documents\monaw` (see [backup and restore](operations.md#backup-and-restore)).
3. Download the newer version from GitHub Releases and verify the checksum for that exact file.
4. Run the newer installer. It replaces the app and leaves the Monaw home folder alone.
5. Start Monaw and confirm the version under **Settings > About**. Check that your conversation list, settings, and imported skills are present before resuming work.

A newer version may migrate the database when it starts. Older versions are not guaranteed to read a migrated database, so to go back, restore a backup instead of only reinstalling the older version. The current installer is unsigned: update downloads are checksum-verified, but publisher identity is not verified until code signing is configured.

An update does not need you to run `setup.bat`, reinstall a system Python, or reinstall Node.js. Those steps are for the source workflow. If you use both workflows, remember they share the default Monaw home; avoid switching versions against the same migrated data without a backup.

## Backup before changing versions

1. Finish or stop active tasks, then close Monaw so SQLite and settings are not changing while copied.
2. Copy the entire Monaw home folder to a separate backup location. Include any separately overridden runtime or memory folder.
3. Copy `Documents\monaw` and all selected project folders you want to preserve; they are not automatically included in a `.monaw` backup.
4. Record the installed version and any custom folder overrides so you can restore the matching layout.

Restore with Monaw closed, at the original home path on the same Windows account when restoring the whole desktop profile. Do not merge two live SQLite databases by copying individual files into an open app. For cross-machine moves, prefer workspace and memory files plus deliberate connection setup rather than assuming encrypted credentials will transfer. See [backup and restore](operations.md#backup-and-restore) for recovery and file-snapshot limits.

## Uninstall

Use **Windows Settings > Apps > Installed apps > Monaw > Uninstall**. Uninstalling keeps `%USERPROFILE%\.monaw`, including conversations, memory, settings, imported skills, and desktop credential state. Remove saved connections in Monaw Settings before uninstalling if you do not want to retain them.

If you also want to delete local application data, close Monaw and back up anything you need before removing the actual Monaw home folder. This is irreversible and also removes any work stored inside that folder. Default work under `Documents\monaw` and separately selected project folders remains outside it. Deleting local files does not revoke provider API keys or remote account sessions; manage those with the relevant provider when needed. See [full local deletion](operations.md#retention-and-full-local-deletion).

## If Monaw does not start

| What you see | What to do |
| --- | --- |
| "Port 8435 is already in use" | Another program uses the port Monaw needs. Monaw does not stop it. Close that program and start Monaw again. |
| "The Monaw service stopped while starting" | The message names the log file. Check that `%USERPROFILE%\.monaw` is writable and not full, then start Monaw again. |
| Starting Monaw again only brings back the open window | Monaw runs once per user. Use the existing window. |
| Anything else | In Settings > Observability > Support report, prepare a report. Read it, then share it if you choose. It leaves out keys, file paths and setting values. Turn on support mode first if you want it to include the service log. |

If no usable window opens, you can still inspect the backend log without opening Settings:

```powershell
Get-Content "$env:USERPROFILE\.monaw\runtime\backend.log" -Tail 100
```

Use your custom Monaw home when configured. Record the installed version, the exact startup message, and whether the problem started after an upgrade. Review log contents before sharing them; raw logs can contain private task information. A missing log may mean the app failed before the backend was launched.

For a port conflict, inspect the listener and identify its owner rather than stopping all Python or Electron processes:

```powershell
Get-NetTCPConnection -State Listen -LocalPort 8435 -ErrorAction SilentlyContinue |
    Select-Object LocalAddress, LocalPort, OwningProcess
```

The default port is `8435`; use the configured port when you have overridden it. Close the identified application yourself, then retry. If you are using `start.bat`, use the [source startup troubleshooting guide](source-setup.md#start-fails-or-the-window-does-not-appear), which also covers Vite, dependency checks, and launcher logs.

## What is not verified yet

- Upgrading with your own memory and saved credentials. An automated check installs v1.0.0, then the new version over it, on a clean Windows machine, and confirms settings, conversations and scheduled tasks are kept (see [packaging](packaging.md#upgrade-check-before-publishing-a-release)).
- Code signing.

Third-party license notices are in `THIRD_PARTY_NOTICES.txt` in the install folder (see [packaging](packaging.md#third-party-licenses)).
