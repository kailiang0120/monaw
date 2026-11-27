# GitHub connector

Monaw connects to GitHub.com using the public **Monaw Agent** GitHub App. Each person signs in with their own account and chooses the repositories the app can access. Installing Monaw does not share the app owner's repositories or credentials with other users.

## Connect from Monaw

1. Type `/connector` in the composer and press Enter. You can also open Settings → Tools → Connectors.
2. Select **Choose repositories on GitHub**. Install Monaw Agent on your personal account or an organization you manage, and select the repositories to allow. An organization may require an owner's approval.
3. Return to Monaw, select **Connect GitHub**, then **Continue on GitHub**. Enter the displayed code and authorize your account. Monaw waits for the authorization and shows the connected username when complete.

The repository installation is normally a one-time setup. Sign-in codes expire; Cancel or closing the connector panel stops the local attempt. Start again if a code expires. Never paste an access token, client secret or private key into chat.

Read capabilities are enabled by default. Turn on **Allow changes** for a connected account to expose issue, comment and pull request creation. Each external change still needs a separate approval showing the target and proposed content, including in Full Access. Background scheduled tasks cannot approve these changes themselves.

The slash menu contains `/compact` and `/connector`. Arrow keys select a command, Enter runs it, Tab completes it, and Escape dismisses it. `/connector` runs locally without a model request and preserves draft attachments. Shift+Enter still inserts a newline.

## Available actions

| Capability | Behavior |
| --- | --- |
| List repositories | Page through repositories accessible to the authenticated account/token. |
| Search issues | Search issues and pull requests; use `repo:owner/name` to narrow results. |
| Read issue | Read an issue or PR discussion and a page of comments. |
| List/read pull requests | Read PR metadata and a page of changed files and patches. |
| Read file | Read a UTF-8 file up to 128 KiB, or list a directory at an optional ref. |
| Read workflow runs/jobs | Read GitHub Actions run, job and step status. |
| Create issue | Create the approved title and body in the selected repository. |
| Create comment | Post the approved body on an issue or pull request. |
| Create pull request | Open a PR between existing remote branches; draft defaults to true. |

Examples: “List my GitHub repositories”, “Summarize the open PRs in owner/repo”, or “Draft an issue for this bug in owner/repo”. With multiple connected accounts, the agent must select an explicit account; ask it to use a particular username if needed.

This connector does not push commits, merge PRs, edit repository files, rerun workflows, manage repository settings, or access GitHub Enterprise Server. It also does not provide webhook synchronization or a local repository index. Git operations in your workspace remain separate.

## Registration for maintainers

The bundled public registration is in `backend/app/connectors/github-app.json`. It contains only a public client ID and app slug. The backend packaging spec copies this resource into the Windows distribution.

To use your own registration:

1. Open GitHub → Settings → Developer settings → GitHub Apps → New GitHub App.
2. Give it a unique name and a homepage URL. The name “Monaw” is reserved; the project registration uses “Monaw Agent”.
3. Enable **Device Flow** and keep expiring user authorization tokens enabled. Device flow does not require a redirect URI. Leave wildcard callbacks and OAuth during installation disabled.
4. Disable the webhook, since this connector does not receive events.
5. Set repository permissions as follows. Leave organization, account and enterprise permissions unset.

   | Permission | Access |
   | --- | --- |
   | Actions | Read-only |
   | Contents | Read-only |
   | Metadata | Read-only, mandatory |
   | Issues | Read and write |
   | Pull requests | Read and write |

6. Choose **Any account** to allow other users to install the app, then create it.
7. GitHub requires a private key before installation. Generate and securely retain that key as the owner. Do not add it to the repository, installer, client configuration, or a user's Monaw installation.
8. Put the **Client ID** (not numeric App ID) and app slug into the public JSON resource, or set `MONAW_GITHUB_CLIENT_ID` and `MONAW_GITHUB_APP_SLUG` before starting the backend. Environment values override the bundled registration. Restart the backend after changing these values.

The private key authenticates the app itself for app/installation token flows. This desktop integration uses user tokens from device flow instead, so it does not read or distribute that key. A client secret is also unnecessary for device-flow token refresh. The encrypted credential record retains the original client ID, allowing existing connections to refresh if a build later changes its default registration.

## Storage and authorization

Tokens stay in the backend, encrypted with Windows user-scoped DPAPI and bound to the provider, account ID, GitHub user ID, tenant and authentication method. Public API responses expose account metadata and the short verification code, never device codes or access/refresh tokens. Credentials cannot be moved to another Windows user by copying the database. Other operating systems need a supported secure cipher implementation before connections can be persisted.

The provider verifies identity with GitHub's `/user` endpoint. `github:authenticated` is an internal identity marker, not a claim of repository permissions. GitHub still checks app permissions, installation selection and user permissions on each request. Public repositories may be readable according to GitHub's own authorization rules.

Refreshing tokens is serialized per account. Rotated tokens are saved atomically. Interrupted or rejected refreshes require reconnecting rather than repeatedly spending an old refresh token. No private app key or client secret is required by the implemented device flow.

Requests use fixed GitHub origins, reject redirects, and have a maximum 20-second socket timeout bounded by the operation's remaining deadline. Paginated reads request 30 records at a time. Monaw returns the next page number instead of following arbitrary response links. Responses larger than 2 MiB, or projected tool results larger than 256 KiB, are rejected; narrow the request if needed.

Writes are sent once. If the response is lost or unreadable, the result is marked uncertain: check GitHub before trying the action again. Approval replay also checks the account revision and adapter schema, so disconnecting or changing permissions invalidates an outstanding approval.

## Disconnect and troubleshoot

- **Disconnect** deletes local tokens and disables the account. It leaves the account row available for reconnection. It does not uninstall the GitHub App or revoke its authorization on GitHub; manage those separately in GitHub settings.
- **Connected but repository missing:** use Choose repositories on GitHub, check the installation and repository selection, and check whether your organization requires approval. A connected status confirms stored authentication, not permission to every repository.
- **Sign in again:** the token was revoked, refresh expired/failed, or a prior refresh was interrupted. Reconnect with the same GitHub account. A different account cannot replace an existing connection's identity.
- **Code expired:** start another sign-in. Closing the dialog cancels local polling; previously displayed codes should no longer be used.
- **Permission denied:** GitHub may return 403 or 404 for inaccessible repositories. Check both GitHub installation permissions and Monaw's Allow changes switch.
- **Rate limited:** wait before retrying. The agent receives safe retry information; mutations are never automatically retried.
- **Sign-in unavailable:** verify the bundled registration or the two environment overrides. The public configuration endpoint reports whether device sign-in is configured.
- **Secure storage unavailable:** verify Windows DPAPI support and run under the intended Windows account. Monaw does not fall back to plaintext token storage.

## Management endpoints

All routes use the existing authenticated local control plane. Ordinary renderer sessions have settings access but not the separate credential-import scope.

| Route | Purpose |
| --- | --- |
| `GET /api/connectors/github/configuration` | Return sign-in availability and the public installation link. |
| `POST /api/connectors/github/login` | Start sign-in; optionally specify an account/revision for reconnecting. |
| `POST /api/connectors/github/login/{id}/poll` | Poll one session-owned attempt while respecting the server interval. |
| `DELETE /api/connectors/github/login/{id}` | Cancel the current session's attempt. |
| `POST /api/connectors/github/token` | Reserved trusted-client PAT import; requires `connectors:credentials`. No renderer bridge or PAT form is shipped. |

The common account routes are described in [Connector foundation](connectors.md). Gmail and Outlook can add provider-specific authentication without changing the account store, policy engine or generic connector command.

## References

- [GitHub App device flow](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-a-user-access-token-for-a-github-app)
- [Refreshing user tokens](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/refreshing-user-access-tokens)
- [Public and private GitHub Apps](https://docs.github.com/en/apps/creating-github-apps/registering-a-github-app/making-a-github-app-public-or-private)
- [GitHub repository REST endpoints](https://docs.github.com/en/rest/repos/repos)
