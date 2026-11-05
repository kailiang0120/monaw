# Reviewing and undoing workspace changes

The **Changes** button in the app bar shows added and removed line totals. Open it
to review a run on a full-width page, select a file, and inspect its diff. Removed
lines are red and added lines are green. **Back to chat** returns to the conversation.

Monaw uses Git to save private workspace checkpoints before and after desktop
runs. This captures changes made by file tools, patches, and commands. It works in
ordinary folders as well as Git repositories. Git must be installed and available
to the backend. Monaw does not create commits in your project or change its branch,
index, or staged changes.

**Undo** previews the inverse diff. Choose **Undo** to confirm or **Cancel** to
leave the file unchanged. The restored contents come from the start of that run,
including any uncommitted edits that were already present. Later file changes,
revoked permissions, overlapping runs, and interrupted Undo attempts prevent
automatic restoration. This is a workspace comparison: edits made by another
application during the run can also appear. Inspect the diff before confirming.

Checkpoints only cover readable ordinary files inside the workspace. Ignored
files, linked files, Git metadata, and unsupported files are excluded. Capture is
bounded to 2,000 paths, 64 MB total, and the existing 8 MB per-file fingerprint
limit. An incomplete or unavailable checkpoint is reported explicitly. Binary
files have no text line counts. Deleted files whose parent directory is missing
cannot be restored automatically. On non-Windows platforms, Undo of a newly
created file is unavailable because identity-bound deletion is not supported.

Private Git objects are stored locally under the runtime `git-checkpoints`
directory. Terminal-run checkpoints expire after 30 days and are reclaimed by
runtime retention; deleting a conversation also reclaims its checkpoints. The
privacy data cleanup removes terminal checkpoints. Recoverable runs retain their
baseline until they are resolved. Project files are never deleted by retention.

Runs created before this feature have no Git baseline. Their existing supported
file-operation Undo records remain available as a fallback; missing historical
versions cannot be reconstructed safely.
