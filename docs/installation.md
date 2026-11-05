# Installing the Windows build

The Windows installer runs Monaw without Python, uv, Node.js or Git. [Download the latest release](https://github.com/kailiang0120/monaw/releases/latest). To run Monaw from source instead, see [Development in the README](../README.md#development). To build the installer, see [packaging](packaging.md).

The installer is published with each [release](https://github.com/kailiang0120/monaw/releases) together with its SHA-256 checksum. It is unsigned. See [What is not verified yet](#what-is-not-verified-yet) for the remaining verification limits.

## Requirements

- Windows 10 or 11, 64-bit (x64). Arm devices and 32-bit Windows are not supported.
- About 400 MB of disk space for the app, plus the Monaw home folder (`%USERPROFILE%\.monaw`).
- An API key for OpenAI or Google, or an OpenAI account, to use a model.
- Optional: Google Chrome 125 or later for browser control, and Docker Desktop for isolated shell commands.
- Voice input sends recordings to OpenAI or Google and needs an API key for one of them; an OpenAI account sign-in does not cover it.

## Install

1. Check the download against the published SHA-256 checksum:

   ```powershell
   Get-FileHash .\Monaw-Setup-1.1.0-x64.exe -Algorithm SHA256
   ```

2. Run the installer. Because it is unsigned, Windows SmartScreen may warn that it is from an unknown publisher; choose **More info** > **Run anyway** only if the checksum matches.
3. The installer is per-user by default and lets you change the folder. It does not need administrator rights for a per-user install.
4. Start Monaw. In a new chat, **Get started** checks that a model is connected, shows your working folder and permission mode, and offers a read-only first task.

## Where your data lives

Monaw keeps application data in the Monaw home folder, not in the install folder:

```text
%USERPROFILE%\.monaw\
  runtime\agent.db        conversations, scheduled tasks and app state
  runtime\settings.json   settings
  runtime\backend.log     the local service log
  memory\                 long-term memory
  skills\                 skills you created with the skill creator
  browser\                the copy of the Monaw extension that Chrome loads
```

New chats without a selected project save their work in
`Documents\monaw\YYYY-MM-DD\chat-<id>`, using your Windows Documents location
(including redirected or OneDrive Documents folders). Each chat has its own
folder, saved before the first task starts and reused after reopening or renaming
the chat. Resetting a selected project returns to that chat's default folder.
Existing chats keep their current workspace and files; no files are moved.
`AGENT_WORKSPACE_DIR` overrides the root under which new dated chat folders are created.
If Windows cannot provide the Documents location, the desktop app uses
`%USERPROFILE%\.monaw\workspace` as the root instead.

API keys are stored with Windows data protection for your user account, not in these files.

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
3. Run the newer installer. It replaces the app and leaves the Monaw home folder alone.

A newer version may migrate the database when it starts. Older versions are not guaranteed to read a migrated database, so to go back, restore a backup instead of only reinstalling the older version. The current installer is unsigned: update downloads are checksum-verified, but publisher identity is not verified until code signing is configured.

## Uninstall

Use **Settings > Apps > Installed apps > Monaw > Uninstall**. Uninstalling keeps `%USERPROFILE%\.monaw`; delete that folder too if you want to remove your conversations, memory and settings. Stored API keys can be removed in Settings before uninstalling.

## If Monaw does not start

| What you see | What to do |
| --- | --- |
| "Port 8435 is already in use" | Another program uses the port Monaw needs. Monaw does not stop it. Close that program and start Monaw again. |
| "The Monaw service stopped while starting" | The message names the log file. Check that `%USERPROFILE%\.monaw` is writable and not full, then start Monaw again. |
| Starting Monaw again only brings back the open window | Monaw runs once per user. Use the existing window. |
| Anything else | In Settings > Observability > Support report, prepare a report. Read it, then share it if you choose. It leaves out keys, file paths and setting values. Turn on support mode first if you want it to include the service log. |

## What is not verified yet

- Upgrading with your own memory and saved credentials. An automated check installs v1.0.0, then the new version over it, on a clean Windows machine, and confirms settings, conversations and scheduled tasks are kept (see [packaging](packaging.md#upgrade-check-before-publishing-a-release)).
- Code signing.

Third-party license notices are in `THIRD_PARTY_NOTICES.txt` in the install folder (see [packaging](packaging.md#third-party-licenses)).
