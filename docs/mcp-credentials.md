# Custom MCP credentials

In the desktop app, save the server configuration, then open **Settings → Tools → MCP → Advanced → Encrypted credentials**. Set the environment-variable name for a local process or the HTTP-header name for an HTTP server. Enter the complete value, including an authorization scheme such as `Bearer` when required. Store/replace and delete apply immediately to the saved server and reconnect it. They are separate from ordinary configuration drafts. Stored values are never returned to the renderer.

Ordinary environment variables and headers remain configuration in `settings.json`. Use these only for nonsecret settings. Likely credential field names are hidden from ordinary API readback and cannot be added or changed through an ordinary settings save. Arbitrary names cannot be reliably classified: every existing field is offered for explicit migration, and unselected values remain plaintext. Arguments, URLs and command strings are not secret storage; remove embedded credentials from them and configure a scoped credential field instead.

## Binding and lifetime

Each saved custom server has a stable `server_id`. Older configurations derive a stable identity from their existing name until the next save; subsequent rename preserves the identity. A reference binds the identity, transport, `env`/`headers` target, and exact field name. References cannot be copied to another server through settings. Case-insensitive field collisions are rejected. Changing transport while references exist is rejected; create a new connection with separately entered credentials.

Electron stores records under a fixed MCP-specific namespace using OS-backed `safeStorage` encryption. The backend receives only the current scoped snapshot through authenticated loopback routes requiring `mcp:credentials`. That scope is deliberately absent from renderer sessions and ordinary development tokens. The backend keeps values in process memory, resolves them into connection-local subprocess or HTTP state, and never inserts them into general settings or `os.environ`.

Local MCP processes inherit runtime essentials such as PATH, Windows/system folders, user/application-data directories, temporary directories, and locale. Other parent environment variables, including provider keys and the control secret, are not inherited. Configure any additional required nonsecret values explicitly per server. Files accessible to the local program remain accessible under its existing host permissions; environment filtering does not isolate an external program from the host filesystem.

Connections with unresolved required references report `credentials_unavailable` and do not launch. Restart hydrates the vault before such connections can start. Rotation retires the previous manager and refreshes tools with the current value; retained old callbacks cannot reopen that connection. A reconnect invalidates reflected tool mappings and the runtime cache so later requests discover the current schema.

Deletion removes the encrypted value, revokes runtime resolution and closes the active connection. The required reference remains, so deletion cannot silently launch a connection without authentication. Restore the value at the same field or remove the server. Removing a server also revokes its runtime references. Closing a connection can interrupt an active call; do not assume an interrupted remote mutation was undone or automatically replay it.

## Migration and interruption recovery

Select **Encrypt FIELD** for each intended legacy field. The value is read only by Electron's main process. Migration:

1. Encrypts the selected value, decrypts it to verify correctness, writes it to the vault, and verifies the stored record.
2. Retains an encrypted `prepared` record with the binding and a digest of the selected legacy value.
3. Checks the current settings version and selected digest, then atomically replaces the canonical settings file with a scoped reference and removes that plaintext field.
4. Activates the encrypted record and hydrates/reconnects the connection.

An encryption or storage failure leaves the legacy field intact. A settings-version/digest mismatch refuses plaintext removal. After interruption, vault application resumes prepared records; if the reference was already committed, it reconciles without repeating removal. A changed or removed server binding leaves migration visibly incomplete and requires correcting the saved configuration and retrying. Other unselected legacy fields remain marked as plaintext. No plaintext backup/export copy is created by migration.

`[REDACTED]`, `[stored]` and masked placeholders cannot be saved as credentials. Omitted references are preserved during ordinary settings edits; replacing or deleting a value requires an explicit credential operation. Credential-bearing subprocess stderr uses bounded memory rather than a raw temporary log file. Diagnostics and observability redact known current and bounded previous values, including ordinary-looking explicitly designated credentials.

## Headless development

An owned headless launcher may use the same explicit per-server runtime protocol. Mint a short-lived signed control token with the dedicated `mcp:credentials` scope using the launcher's control secret; do not expose it to agent tools or a renderer. Ordinary renderer/development tokens cannot call these routes.

`GET /api/settings/mcp-credentials/inventory` returns saved identities, reference metadata, field names, and `settings_version`. A binding is:

```json
{"server_id":"32-lowercase-hex-characters","transport":"stdio","target":"env","key":"API_KEY"}
```

For a new required slot, POST `/commit` with `binding`, `expected_version`, and `legacy_digest: null`. For existing legacy plaintext, `/prepare` with the binding and expected version returns the selected value and digest to this privileged launcher only. Use an owned encrypted store and verify it before `/commit` with that digest. Never write the returned plaintext to a file, console, diagnostic bundle, or shell argument.

POST `/hydrate` with `{"values":[{"binding":{...},"value":"..."}]}` to supply the complete current snapshot. References absent from the snapshot are revoked; include other servers' current values when rotating one. Only references already committed to the matching saved server are accepted. POST `/revoke` with a binding for immediate revocation/teardown. Use in-memory request bodies from the launcher's secret source, and rehydrate on every backend restart. There is no backend plaintext persistence fallback or automatic reading of arbitrary environment keys.

## Recovery and compatibility

The encrypted vault is tied to the OS encryption context. The application workflow verifies decrypting and reconnecting after a real Electron/backend restart under the same Windows user. Cross-user or cross-machine restoration is not established. Keep an appropriate secure source from which credentials can be re-entered; copying ciphertext alone is not a verified portable backup.

Older binaries ignore credential references and cannot hydrate this format. Returning to one is not a verified downgrade path; do not reintroduce plaintext to make it work. Application/database rollback and credential recovery are separate operations. This batch uses synthetic disposable fixtures and does not migrate the developer's live Monaw configuration.
