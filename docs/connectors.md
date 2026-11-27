# Connector foundation

This is the shared runtime for GitHub and the categorized Composio app catalog. It implements account management, encrypted credential persistence, token refresh, tool registration, approval replay, and management API routes. GitHub uses native browser sign-in; email, calendar, file, note, messaging, and task connectors use a user-owned Composio Platform project behind the same account and policy contracts. Monaw automatically creates managed authentication configurations after the user saves a project key. Manage them through `/connector` or Settings → Tools → Connectors. See [GitHub setup](github-connector.md), [Composio setup and attribution](composio.md). Model connections, MCP configurations, and imported skills continue to use their existing paths.

## Architecture

```text
Provider sign-in adapter (GitHub device flow first)
    -> verify account identity and actual granted scopes
    -> ConnectorService.connect_verified()
    -> AccountStore + CredentialVault (one SQLite transaction)

Desktop chat / scheduled task / Telegram
    -> existing skill loader and tool search
    -> connector capability with an explicit account_id
    -> current account, scope, schema, and cancellation checks
    -> encrypted credential lookup / refresh
    -> existing action policy and exact approval replay
    -> provider adapter.invoke()
    -> bounded result page, source links, or safe structured error
```

The service lives in `backend/app/connectors`. It uses the existing `agent.db`, action policy, execution workers, tool search, cancellation context, and UI event stream. It does not create a second agent loop or a provider-specific scheduler. Loading the foundation does not contact a service or decrypt account credentials.

| Module | Responsibility |
| --- | --- |
| `models.py` | Versioned manifests, capabilities, identities, accounts, credentials, refresh responses, results, and errors. |
| `providers.py` | Trusted adapter registry and call context with a deadline and cancellation. |
| `store.py` | Atomic account/ciphertext writes, revision checks, and change notifications. |
| `vault.py` | Account-bound Windows DPAPI encryption; injectable cipher contract for future OS keyrings. |
| `auth.py` | Bounded, expiring, single-use authorization correlation and PKCE S256 challenges. |
| `service.py` | Account lifecycle, scope enforcement, refresh coordination, and policy-aware dispatch. |
| `tools.py` | Deferred tool schemas and exact approved replay through the normal runtime. |
| `api/routes/connectors.py` | Public account management without credential access or direct tool execution. |

## Provider contract

Providers are trusted bundled Python adapters registered explicitly with `get_connector_service().providers.register(adapter)`. A portable skill cannot register a provider through its manifest. Registration rejects duplicate provider IDs and invalid parameter schemas. The service bootstrap registers GitHub before the first chat runtime is built, without network requests during registration.

Every provider has a stable lowercase ID, display name, category, description, connection method, adapter version, supported authentication methods, transport (`api` or `mcp`), and capability definitions. Contract version 1 is explicit. `api` and `mcp` describe adapter implementations; declaring `mcp` does not automatically import an existing MCP connection or certify its tools as safe.

Every capability declares:

- A unique ID, concise description, and object JSON Schema.
- Its actual action: `read`, `create`, `update`, `delete`, `send_message`, or `upload`.
- The provider scopes needed to perform it. Empty means this capability has no scope requirement; it does not mean all permissions are granted.

Scope strings are provider-defined. GitHub's `github:authenticated` grant only attests a successful `/user` identity check; it does not assert permission to every repository or action. GitHub enforces the intersection of app permissions, selected installation repositories, and user access on every request. Local capability toggles further restrict access. Other adapters must likewise enforce resource-level permissions and tenant restrictions.

An adapter implements:

```python
def refresh(self, credentials: Credentials, context: CallContext) -> RefreshResult:
    """Return fresh tokens and any changed provider grants."""
    ...


def invoke(
    self,
    capability: Capability,
    arguments: dict,
    credentials: Credentials,
    context: CallContext,
) -> ConnectorResult:
    """Execute one validated action for context.account."""
    ...
```

Use `context.remaining()` before each network request and as the upper bound for its timeout. Also check it between pagination or polling steps. The foundation allows 45 seconds for refresh plus execution. Cancellation is cooperative; Python cannot forcibly interrupt arbitrary adapter code. Implementations must use bounded network timeouts and must not detach background writes.

Adapters own trusted endpoint selection, redirect restrictions, authentication headers, provider-specific error interpretation, MIME handling, attachment transfer, and remote account/resource validation. Never send credentials to a URL supplied in a model argument. Do not put client secrets or private signing keys into a distributed desktop bundle. Endpoint discovery, callback validation, and provider registration requirements must be reviewed when each sign-in adapter is implemented.

## Accounts and permissions

Each connected account has a random immutable ID. Its identity is `(provider_id, tenant, subject)`; labels and email display names are not identity keys. Two accounts for the same provider are supported, including identical subject values in different tenants. Duplicate identities are rejected instead of silently replacing a connection.

All connector tools require an explicit `account_id`. There is no implicit first account or fallback account. Tool discovery lists the eligible account IDs and labels. Credentials are never included in tool schemas or model inputs.

Provider-granted scopes and locally enabled capabilities are separate:

1. The provider verifies what the account may access.
2. The user enables a subset of the available capabilities.
3. The action policy decides whether a particular invocation may proceed.

Enabling a capability cannot add a missing provider scope. Refresh responses can report reduced scopes; unsupported capabilities are removed before an action is dispatched. Newly gained scopes do not automatically enable new capabilities. Account revisions change after edits, reconnect, refresh, and disconnect. Stale edits and pending approvals fail rather than applying to a changed account.

Reconnect uses the existing account ID and expected revision. It must verify the same provider, tenant, subject, and authentication method. Signing into a different account requires a new connection. Changing the label preserves identity and encrypted credential binding.

Disconnect deletes local credentials, disables capabilities, and keeps a disconnected identity for explicit reconnect. Remove deletes both the identity and ciphertext. Both work when the provider is offline or no longer installed. These operations **do not revoke the provider-side application grant**; future providers may add a separate revocation operation. Already dispatched remote work cannot be undone by disconnecting.

## Credential storage and recovery

Migration `0031_connector_accounts.sql` adds one table. Public metadata and the OS-encrypted credential blob are committed together. Saved credentials never enter ordinary settings JSON, public API responses, or command arguments. Electron starts the managed companion with independent OS-encrypted secrets passed through stdin. Its execution and management credentials reach the backend environment, but only the execution credential and routing binding enter imported account snapshots. Fixed session-owned OAuth routes use the management credential. Provider OAuth tokens stay in the companion's encrypted storage. An explicitly configured external runtime can still supply its own restricted execution token.

The Windows implementation uses user-scoped DPAPI with entropy derived from the account ID, provider, tenant, subject, and authentication method. Copying an encrypted blob onto another account fails decryption. Encryption is round-trip verified before committing. There is no plaintext fallback on unsupported platforms. Future macOS/Linux support must supply a real OS-keyring implementation of `SecretCipher`.

This uses the same Windows protection mechanism as Monaw's existing backend account authentication, through a separate connector namespace. It does not migrate or reuse the Electron model-key or MCP credential records. Keeping ciphertext in the same database transaction prevents partially saved connector identities and tokens.

Tokens are decrypted only for a call and registered with the existing process-wide secret redactor. `SecretStr` masks normal object representations. Provider errors are mapped to fixed public error codes, and known tokens are removed from returned tool results. Python strings cannot be reliably zeroed; this is not process-memory isolation. Local programs with equivalent user permissions are outside this boundary.

Back up the database through Monaw's normal data-backup procedure. Ciphertext alone is not a portable account backup across Windows users or machines. Reconnect accounts if OS decryption is unavailable. Removing a row does not promise secure erasure from historical backups or SQLite pages.

## Token refresh

- Calls for the same account are serialized with its edits and disconnects. Locks are bounded in number.
- A token within 60 seconds of expiry is refreshed before tool execution.
- `expires_at=None` means the provider did not supply an expiry; rejected tokens must still be reported as `needs_reauth`.
- The service durably records `refreshing` before contacting the provider.
- An omitted replacement refresh token preserves the previous refresh token.
- New tokens and changed grants are saved atomically before being used.
- Missing refresh tokens, revoked grants, and ambiguous refresh failures require reconnect.
- A crash during refresh leaves the account closed in `refreshing`; later calls require reconnect instead of replaying a possibly consumed refresh token.

This deliberately favors reconnect over guessing whether a rotating refresh token was consumed. There is no automatic retry of a refresh after an ambiguous response. The supported deployment is Monaw's single owning backend per profile. Revision checks reject competing writes from another DB connection, but parallel backend processes are not a distributed locking or dispatch system.

## Sign-in primitives

`AuthorizationSessions` holds at most eight attempts for ten minutes each, using a monotonic clock. Attempts include an unpredictable state, PKCE verifier/challenge, initiating control session, provider, issuer, exact callback URI, requested scopes, and optional account/revision for reconnect. The verifier stays in memory and is excluded from object representations. Completion checks all correlation fields and consumes the attempt once. Restarting the backend cancels pending sign-ins.

These are building blocks, not a complete OAuth implementation. Future provider sign-in adapters must validate their endpoint configuration, receive callbacks or poll device authorization, check issuer/state, exchange codes, verify the returned identity/grants, and call `connect_verified()`. Use the initiating account revision during reconnect. Never call `connect_verified()` with identity or scope assertions supplied by chat or an ordinary public request.

OAuth PKCE, device code, API key, and anonymous authentication are represented in the contracts. GitHub implements bounded device-code polling, backoff, cancellation, expiry and a sign-in UI. The desktop opens GitHub links in the external browser. There is no callback listener, token-read route or raw execution endpoint. Raw token submission is reserved for the separate `connectors:credentials` scope; ordinary renderer sessions cannot use it, and the UI currently supports browser sign-in only. Disconnect removes local credentials; remote authorization can be revoked in GitHub settings.

## Execution and recovery

Connector tools use the existing action policy. Reads are classified as read-only only for exact registered, trusted adapter capabilities. A provider-like tool name alone is insufficient. Mutations remain opaque external effects in the durable journal, so recovery must not automatically repeat them.

Monaw's current policy requires direct human review for external mutations, including in Full Access. Noninteractive runs cannot approve these actions. The foundation preserves that behavior. Future unattended-write support needs an explicit policy design; connecting an account must never grant it implicitly.

Approvals bind the account ID/revision, provider version, capability schema, arguments, and execution identity. Replay rechecks all of these before dispatch. Private connector arguments are omitted from ordinary tool previews. GitHub approvals explicitly display the account, repository, issue number, proposed text, and branch details where applicable. The generic decision audit omits this content preview; the approval itself retains it for review.

Results contain a bounded JSON page, an optional opaque pagination cursor, and HTTP(S) source links. Both arguments and results have a 256 KiB encoded limit. Downloads and large attachments should use the existing artifact/file mechanisms, with provider-specific size limits. Connector content is external data and must not be treated as user instructions or permission to invoke another tool.

Safe failures include `needs_reauth`, `scope_missing`, `account_changed`, `account_disabled`, `rate_limited`, `timeout`, `cancelled`, and `provider_error`. A rate limit may include `retry_after`; the foundation does not sleep or retry automatically. A timeout or invalid result after dispatching a mutation is marked `outcome_uncertain`. The provider may have completed the action. Inspect the remote outcome before retrying; an idempotency key is meaningful only if the provider actually supports it.

## Management API

| Route | Scope | Result |
| --- | --- | --- |
| `GET /api/connectors/providers` | `settings:read` | Registered manifests and contract version. |
| `GET /api/connectors/accounts` | `settings:read` | Account metadata and whether each adapter is available. |
| `PATCH /api/connectors/accounts/{id}` | `settings:write` | Update label, enabled state, and enabled capabilities. |
| `POST /api/connectors/accounts/{id}/disconnect` | `settings:write` | Delete local tokens and disable the connection. |
| `DELETE /api/connectors/accounts/{id}` | `settings:write` | Forget account metadata and local tokens. |

Writes require `expected_revision`. PATCH additionally requires `label`, `enabled`, and `enabled_capabilities`. Disconnect and DELETE use a JSON body containing the revision. Unknown fields are rejected. A revision conflict returns 409; reload the account before editing again. Account availability does not certify current remote connectivity; authentication is checked when a tool is used.

The existing SSE stream emits `connectors.changed` after each committed change, containing only account ID and revision. Future UI consumers should refetch the public account list. Runtime cache keys include connector account state and provider manifests, so the next message discovers new capabilities without restarting. Already-held tool callbacks still revalidate current access. New capabilities are not injected into an in-progress model request.

## Adding more providers

1. Register the application's provider credentials and allowed callback mechanisms outside the source tree.
2. Implement a bundled adapter with a fixed manifest and verified identity/grant mapping.
3. Add sign-in handlers using the authorization primitives and the encrypted account service.
4. Add a connector card and account management UI using the existing Monaw design primitives.
5. Start with read/search capabilities and verify account selection, pagination, expiry, revocation, and service limits.
6. Add mutations with exact action previews, appropriate scopes, and provider-specific outcome evidence.
7. Test with disposable accounts before documenting a connector as available.

Future additions such as calendar tools, multiple tenants, and new providers can use new capabilities and adapters. Webhooks, mailbox indexing/sync, push subscriptions, cloud-hosted jobs, and cross-device credential sync need separate persistence/lifecycle designs; they are not implied by this foundation.

## Validation

Core tests in `backend/app/agent/tests/test_connectors.py` cover account isolation, encryption binding, persistence, concurrent refresh, reduced scopes, interrupted refresh, revoked credentials, exact approval replay, stale access, background-write policy, cancellation, API scopes, and single-use authorization attempts. Windows also exercises real DPAPI encryption and rebinding rejection. Migration tests upgrade historical databases without losing conversations or schedules. Tests use synthetic adapters and disposable local databases, never live accounts.

```powershell
uv run --project backend pytest backend/app/agent/tests/test_connectors.py -q
```

References: [PKCE S256 (RFC 7636)](https://www.rfc-editor.org/rfc/rfc7636), [Windows DPAPI](https://learn.microsoft.com/en-us/windows/win32/api/dpapi/nf-dpapi-cryptprotectdata), [MCP configuration](mcp.md), [action permissions](permissions.md), [operations and backups](operations.md).
