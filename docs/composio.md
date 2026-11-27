# Composio setup

Monaw connects email, calendars, files, notes, messaging, and task apps through **your own Composio Platform project**. You save one project API key; Monaw prepares the authentication configuration and browser sign-in automatically. GitHub keeps its separate [native connector](github-connector.md).

For the enabled connectors, you do not need to install a connector runtime, run a server, register a provider OAuth app, or paste authentication configuration IDs. Composio supplies managed OAuth. Google connectors currently remain unavailable as described below. Account creation, any verification, and provider consent still require you to act in the browser. See [Composio managed authentication](https://docs.composio.dev/docs/authentication/programmatic-auth-configs).

## Google connector availability

On **27 November 2026**, Composio support confirmed that `https://www.googleapis.com/auth/gmail.readonly` is **not approved** for its managed Gmail OAuth app. There is no approval ETA. Monaw therefore marks Gmail **Unavailable** and blocks new or reconnect sign-in attempts before creating a remote session. Existing valid bindings are not revoked by this UI status. Monaw does not silently request broader Gmail access.

Strict read-only Gmail requires a custom Google OAuth app and any applicable Google verification. If Monaw operates that app for all users, its client secret must stay on a trusted backend, never inside a distributed desktop build. Each user's separate Composio project would need its own custom auth configuration, and new connections must use that configuration. This custom setup is not implemented in the current desktop flow; the Gmail card explains the limitation. Google Calendar and Google Drive are also greyed out after sign-in blocks reported in the current project. Their cards remain visible and explain the limitation; Google Docs and Sheets are included under Drive. The exact scope-specific cause for those two toolkits has not been confirmed by Composio support.

## First-time setup

1. Open **Settings → Tools → Connectors** in Monaw desktop. You can also type `/connector` in chat.
2. Click **Open Composio**. Sign in or create an account at the [Composio dashboard](https://dashboard.composio.dev/), choose **Platform**, and create or select a project. A dedicated project makes Monaw's connections and usage easier to identify. The consumer **Composio For You** product is separate; Monaw needs a Platform project key.
3. In the project's settings, open **API Keys**, create a project API key, and copy it. Dashboard labels can change; use the project key management page, not a provider access token or an auth configuration ID.
4. Return to Monaw, paste the key into **Platform API key**, and click **Save key**. Monaw checks that it can access the project before encrypting the key on this Windows device. A rejected replacement does not overwrite a working key.
5. Find an app by category or search, select its card, then click **Connect**. Monaw creates the managed authentication configuration, reuses it for subsequent connections, and opens Composio's hosted sign-in page. Its enabled tools are read-only; the provider's consent screen describes the OAuth grant.
6. Choose the intended provider account and review the consent screen. Complete any organization approval or verification required by that provider. Leave Monaw open while signing in; it checks completion automatically. If the browser did not open, click **Continue to [app]**.
7. When Monaw shows **Connected**, try asking it to list a few tasks, find a message, or browse files in the app you connected. Results are bounded; request smaller pages if a response is too large.

The in-app **Setup guide** repeats the essential steps without leaving settings. Key management requires the desktop app; opening the development Vite page in a browser alone cannot save a project key.

## What Monaw automates

| Step | Behavior |
| --- | --- |
| Validate and store the key | Checks project access and protects the saved key with Windows DPAPI through Monaw's credential vault. The settings API returns only whether a key is configured. |
| Identify this installation | Generates a stable local user identifier; prompts and browser callbacks cannot choose it. |
| Configure authentication | Creates and saves a managed auth configuration for the selected provider with explicit read scopes. |
| Start browser sign-in | Creates a restricted Composio session and opens a hosted Connect Link. No local OAuth callback or client secret setup is required. |
| Complete the connection | Polls the exact connection, checks ownership and provider identity, then enables Monaw's reviewed read operations. |
| Run a tool | Rechecks the saved connection and identity, pins the account in a restricted session, and executes one reviewed action. |

Composio account registration, API key creation, CAPTCHA, MFA, and consent remain browser steps. Monaw does not collect your Composio, Google, or Microsoft password.

## API key permissions

Use a project key authorized for connected account access, auth configuration creation, tool metadata reads, session management, and session tool execution. A scoped key needs the corresponding read/write permissions, including connection creation during sign-in. A key that can list accounts may pass the initial check but still lack permission to create a configuration or session; consult [Composio's project key permissions](https://docs.composio.dev/reference/authenticating-to-composio/project-api-key-permissions) when restricting it.

Never commit a key or distribute your key with a Monaw build. Each user supplies their own. Keep an independent copy in your password manager if needed: Monaw cannot display the saved key again.

## Available operations

| App | Monaw operations | Requested access |
| --- | --- | --- |
| Gmail (new sign-in unavailable) | Adapter supports profile, search/list messages, and message reads; managed consent is blocked | `openid`, `userinfo.email`, `gmail.readonly` |
| Outlook | Read profile, list mail folders, list messages, read a message | `User.Read`, `offline_access`, `Mail.Read` |
| Google Calendar (sign-in unavailable) | Read profile, list calendars, list events with time filters | `calendar.readonly` |
| Google Drive (sign-in unavailable) | Read profile, find files, file metadata, Google Docs content, Google Sheets ranges | `drive.readonly` |
| OneDrive | Read profile, browse folders, find files, file metadata | `User.Read`, `offline_access`, `Files.Read` |
| Notion | Verify integration identity, search pages, read page properties and blocks | Pages selected during consent; the managed Notion integration determines OAuth capabilities |
| Slack | Verify user/workspace, list conversations, search messages, read history | `search:read`; read/history scopes for public/private channels, DMs, and group DMs |
| Microsoft Teams | Read profile, list teams/channels, read channel messages | `User.Read`, `offline_access`, `Team.ReadBasic.All`, `Channel.ReadBasic.All`, `ChannelMessage.Read.All` |
| Todoist | Read profile, tasks and projects | `data:read` |
| Linear | Read profile, search issues and list projects | `read` |

Google Docs and Sheets are available through **Google Drive**, without separate connections. OneDrive currently exposes file discovery and metadata, not binary downloads. Teams covers channel messages, not personal chats. Notion only sees pages shared with its integration. These limits describe the actual enabled tools, not every operation offered in Composio's catalog.

Provider schemas and managed OAuth availability were checked against the [Composio toolkit catalog](https://docs.composio.dev/toolkits) and [managed OAuth reference](https://docs.composio.dev/toolkits/managed-auth) on **27 November 2026**. The automated tests validate local routing, input schemas and identity isolation; they do not prove that a particular user's organization will approve sign-in. Monaw does not widen scopes automatically when a provider blocks consent.


Google's abbreviated scope names above represent Google's full scope URLs in the implementation. `offline_access` lets Composio maintain the Microsoft connection. Monaw does not enable sending, deleting, or editing mail. Composio's larger catalog does not automatically become available to the agent. Future connectors require a reviewed Monaw adapter and permission schema. Organizational policies may still require administrator consent.

## Usage and cost

Usage belongs to your Composio account and plan. Open the dashboard to review current usage, connections, limits, and billing. As checked on **27 November 2026**, Platform Hobby advertises 100,000 total tool calls per month, including a 20,000 managed-tool-call allowance, and the first 1,000 managed connections. The managed allowance is part of the total, not extra. A connection is a linked account, not an allowance of 1,000 Monaw users. Quotas and plan terms can change; the [Platform pricing page](https://composio.dev/pricing/platform) is authoritative.

Monaw uses the Sessions execution API. Composio documents separate restrictions for legacy direct execution and proxy execution; Monaw does not use those paths for connector tools. Identity verification also executes a profile tool, so one connector operation can consume more than one tool call. Monaw does not pool users under a shared publisher key or promise that creating extra projects multiplies free allowances. Model-provider charges remain separate.

## Privacy, changing keys, and disconnecting

- The project key is encrypted locally. It is sent only to Composio's fixed API origin by the backend, never supplied as an agent tool argument or returned by a settings read.
- Composio stores and refreshes the connected provider credentials. Connector requests and results pass through Composio, and retrieved content can be supplied to the model used by the chat. See [Composio token custody](https://docs.composio.dev/docs/security/token-custody).
- **Change key** validates a replacement before saving it. Saving the same key preserves the current setup. A different key invalidates old account bindings; reconnect accounts using the new project key. Monaw verifies the provider identity again.
- **Disconnect** disables the account and removes its local credential binding. **Remove** forgets the disconnected local account record. **Remove key** forgets the project key and disables all local Composio accounts. GitHub is unaffected.
- These local actions do not revoke provider grants or delete connections in Composio. To remove cloud access, remove the connection in your Composio dashboard and revoke the app in your Google or Microsoft account settings as appropriate. Cancelling sign-in also does not undo consent already granted in the browser.
- Moving the encrypted database to another Windows user or machine does not transfer usable credentials. Save your key and connect again on the new device.

## Troubleshooting

| Symptom | What to check |
| --- | --- |
| Key will not save | Use a Platform project API key, check its permissions and network access, and use Monaw desktop. A revoked key cannot be reused. |
| Key saves but Connect fails | Check auth configuration and session creation permissions and Composio's service status. The initial key check only verifies project account access. |
| Browser does not open | Click the visible Continue link. Monaw accepts only the documented HTTPS Composio Connect Link origin. |
| Consent is blocked | Follow your Google Workspace or Microsoft organization policy; an administrator may need to approve access. |
| Sign-in expires | Cancel and start Connect again. Local linking attempts expire after ten minutes. |
| Wrong account or reconnect rejected | Sign in to the original mailbox when reconnecting. To use a different mailbox, remove the disconnected record and start a new connection. |
| Requests stop or are rate limited | Check Composio usage and provider limits. Free-plan limits are enforced by Composio; Monaw does not silently buy credits. |
| Result is too large | Request a smaller page or a narrower search. Responses are capped at 256 KiB. |
| Old OpenConnector account is disconnected | Remove its local record and connect through Composio. Old OAuth grants cannot authorize the replacement adapter. |

## Development, packaging, and migration

The backend pins the official generated `composio-client==2.1.36` SDK. `uv sync --locked` installs it; the Windows frozen backend collects it in the existing PyInstaller build. No Bun executable, connector companion, new listening port, or publisher-owned gateway is required. SDK loading and remote calls are deferred until connector setup or use, rather than chat startup.

Implementation files are `backend/app/connectors/composio.py`, `composio_gateway.py`, and `composio_catalog.py`. The provider-neutral account store, vault, grants, revision checks, and agent policy remain in [Monaw's connector foundation](connectors.md). Management routes are listed in [the API reference](api.md).

Migration `0032_composio_configuration.sql` adds encrypted configuration storage and disconnects records with old OpenConnector grants. It preserves their labels for removal and clears their local credential blobs. It does not delete remote accounts. Old ignored development build artifacts may remain on disk, but Monaw no longer launches or packages them. Existing provider grants can be revoked separately.

Only named read-only tool slugs are exposed. Sessions pin the connection and auth configuration; proxy execution, multi-execution, workbench, instant tools, and connection-management tools are disabled. The gateway checks live input metadata against reviewed argument aliases and rejects incompatible schemas. The SDK is pinned; the provider's hosted implementation is not version-pinned by this Sessions integration. Review upstream changes before expanding capabilities.

Core tests cover encrypted setup, invalid-key replacement, account binding, cancellation races, permission boundaries, argument translation, and the generated SDK's HTTP contract using fixtures. Renderer tests cover key handoff and sign-in controls. Real Gmail/Outlook consent and installed-build sign-in still require a tester's own project and provider account; fixture tests do not prove a live integration.

## Upstream references

Monaw uses [Composio](https://composio.dev/) as a hosted integration service and its official Python API client. This replaces the previous OpenConnector adapter/runtime integration.

- [Programmatic auth configurations](https://docs.composio.dev/docs/authentication/programmatic-auth-configs)
- [Session creation](https://docs.composio.dev/docs/sessions/create-and-reuse), [connections](https://docs.composio.dev/docs/sessions/connections), [tool access](https://docs.composio.dev/docs/sessions/tool-access), and [execution](https://docs.composio.dev/docs/sessions/executing-tools)
- [Gmail toolkit](https://docs.composio.dev/toolkits/gmail) and [Outlook toolkit](https://docs.composio.dev/toolkits/outlook)
- [Project API key permissions](https://docs.composio.dev/reference/authenticating-to-composio/project-api-key-permissions)
- [Credential custody](https://docs.composio.dev/docs/security/token-custody) and [Platform pricing](https://composio.dev/pricing/platform)

## Catalog categories and icons

The catalog groups apps by their primary purpose: **Email**, **Calendars**, **Files & documents**, **Notes & knowledge**, **Messaging**, **Tasks & projects**, and **Development**. GitHub remains the native development connector. Search matches app names, descriptions, and category labels; category filtering and returning from a connector preserve your current search.

Brand icons come from **[SVG Logos](https://github.com/gilbarbara/logos)** by Gil Barbara, distributed through **[Iconify](https://github.com/iconify/icon-sets)** as `@iconify-json/logos` (CC0-1.0). Iconify synchronizes its collections regularly. The exact npm version is locked; a weekly Dependabot rule proposes updates for review. Trademark rights remain with each service. Outlook is absent from this pack, so its existing SVG from [Composio's logo catalog](https://logos.composio.dev/api/outlook) is bundled separately. Sources and SHA-256 hashes are recorded in `frontend/src/assets/connectors/sources.json`.

Only the selected SVGs are exported and included in Monaw. No Iconify runtime, CDN request, favicon lookup, or credentials are involved in displaying them. Vite fingerprints the assets for caching. A fixed icon box prevents layout shifts; a light tile preserves the original brand colors in both themes, and a theme-aware plug icon replaces a missing or failed asset. SVGs render as images rather than injected HTML.

To refresh the pack and regenerate the selected assets:

```powershell
cd frontend
npm run icons:update
npm run verify
```

Review and commit the package version, lockfile, selected SVGs, and source hashes together. Normal renderer builds run `icons:build` from the installed, pinned package with no network access. To add an icon, add its exact pack name to `frontend/scripts/build-connector-icons.mjs`; do not add runtime URL construction. The selected pack's license is included in the Windows third-party notices.
