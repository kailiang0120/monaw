# Importing skills

Open **Settings → Tools → Skills → Import**. Enter a source, click **Check source**, select the skills, then click **Install**. Installed skills are enabled immediately for the next message and persist across restarts and app upgrades. They live under `~/.monaw/skills`, or the `skills` folder of a configured `MONAW_HOME`.

Supported sources:

- Public GitHub repositories: `owner/repo` or `https://github.com/owner/repo`.
- GitHub skill folders and SKILL.md links: `https://github.com/owner/repo/tree/main/skills/example` or the corresponding `/blob/main/skills/example/SKILL.md` URL.
- A local skill folder, a collection of skill folders, or a path to its SKILL.md. Browse opens the native desktop folder picker.
- ZIP archives and individual SKILL.md uploads. Uploads are limited to 6 MB; folders and downloaded/extracted files are limited to 25 MB and 2,000 entries. Use a local folder or upload only the selected skill when a repository is too large. For branch names containing an unescaped slash, use a ZIP or local folder.

A portable skill contains a UTF-8 `SKILL.md` with YAML `name` and `description` fields, followed by its instructions. The name uses lowercase letters, digits, and hyphens and is at most 64 characters; descriptions are at most 1,024 characters. Optional scripts, references, and assets inside the skill folder are preserved. Monaw supplies the installed folder path to the model so it can resolve these relative paths.

Installation previews use a snapshot of the source and expire after 15 minutes. Existing and built-in skill names cannot be overwritten. Imported packages are treated as instruction skills: installation does not run their scripts, load `tools.py` as a native Monaw module, grant their `allowed-tools` requests, or install external dependencies. File access and script execution continue through Monaw's normal permissions.

## Plugins from other products

OpenAI/Codex and Claude plugins may bundle portable skills alongside MCP connections, hooks, agents, commands, authentication requirements, and host-specific features. Monaw can import the SKILL.md folders from a public source repository or downloaded archive. This imports the skills, not the complete store plugin.

An independently available MCP server can be configured separately under **Settings → Tools → MCP** using its supported transport and credentials. Store account connections, subscriptions, hooks, and vendor-specific plugin UI are not automatically transferred to Monaw. Monaw does not access either vendor's private marketplace or reuse their account credentials.

References: [Agent Skills specification](https://agentskills.io/specification), [Vercel Skills installation](https://github.com/vercel-labs/skills), [Claude plugin manifest](https://code.claude.com/docs/en/plugins/manifest-reference), and [OpenAI plugins](https://learn.chatgpt.com/docs/plugins).
