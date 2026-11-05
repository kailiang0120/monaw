# Action security enforcement inventory

The central policy consumes facts collected by local adapters, not tool labels,
provider descriptions, model risk claims, or an assumed tool-name family.
`attest_action` seals a canonical snapshot in this process; `authorize_action`
evaluates current permissions and returns allow, deny, or an existing approval
broker ticket. The same adapter must collect fresh facts and consume that exact
one-use approval immediately before dispatch. Changed arguments, destination,
data scope, document revision, server/schema, run/operation, principal, session,
execution source, or expiry invalidate the approval.

| Boundary | Runtime enforcement | Remaining limitation |
|---|---|---|
| Built-in browser through MCP registry | Hidden context RPC supplies actual page/frame origin, document revision and target identity; parent central policy binds parameters; child checks the expected context again before dispatch | Chrome pages and extensions can initiate their own requests; this is agent-command mediation, not browser network isolation |
| Generic browser click, keyboard, dialog, JavaScript and upload | Same parent action gate; unknown critical target/context denied; runtime external/irreversible effects require human review; arbitrary scripts remain opaque execution | A button label or JavaScript keyword does not establish the business effect; opaque code is not certified safe |
| Local file selection for browser upload | Canonical workspace paths, content/identity/size, sensitivity, actual input and destination are bound and revalidated | CDP opens a path separately, so the final file-open race cannot be eliminated without handle-based transfer |
| Reflected MCP tools | Exact server identity, schema, arguments, observable destination and configured exact tool capability are bound; provider hints never prove read-only behavior | Third-party internals and network requests are opaque; explicitly trusted tools grant a capability, not evidence that it is harmless |
| MCP refresh/reconnect controls | Reload current configured server set, bind each complete configuration fingerprint, and re-enter central policy on approved replay before launch/contact | Configured server startup remains an opaque capability; its internals require separate isolation |
| Shell `exec`, `exec_start`, `exec_write_stdin` | Central gate after actual sandbox/environment/workdir selection; exact command/stdin/session and execution environment bound; existing hard sandbox, path and environment blocks remain | Arbitrary shell effects and destinations cannot be inferred reliably from command text. Full Access authorizes this opaque capability without ordinary prompts |
| Desktop action batches | Current window/app identity and observed state ID plus all exact actions bound; live geometry/window/observation checks occur again before input; existing credential-entry and screen-fallback controls remain | Generic desktop input may cause effects the adapter cannot observe semantically. Full Access permits ordinary opaque input |
| Process termination | Actual PID/name/create-time set bound as irreversible human-only action; PID identity checked again before termination | A process can exit during dispatch; already-terminated processes cannot be restored |
| Native filesystem mutations | Existing v1.1 permission, preview/plan fingerprint, snapshot and undo pipeline retained; sensitive-location checks at reachable read/write boundaries | Arbitrary shell/MCP file access does not become a native reversible mutation |
| Legacy controller helpers | Existing exact tool-name/direct approval validation and explicit delegated invocation edges retained | Internal compatibility helpers are not a second, independent authorization system; new adapters use the central contract |

Full Access runs ordinary scripts and actions without prompts. Proven external
mutations, uploads, security/payment/message actions, and irreversible actions
remain human-only. Default, Custom and Auto Review apply their configured
interaction/execute permissions. Unknown untrusted remote effects require review
unless the user configured that exact tool capability. Auto Review cannot settle
a human-only ticket, and sensitive material forces direct review before an LLM
review request is sent.

Approvals reuse the existing broker and resume engine. Private replay payloads
are distinct from public projections. Shell environment values stay in bounded
live-process receipts; only redacted values and an opaque receipt are persisted,
and a lost receipt fails closed. Public tickets omit arbitrary arguments, scripts,
form values and raw execution results. Public upload summaries expose basenames
and byte counts. URL credentials/query/path are excluded from durable security
events. The existing Journal records `action.security` decisions under its run
owner/attempt fence, linked by action, operation and ticket IDs.

Only the trusted built-in browser adapter can attest browser outcome evidence.
An external response cannot promote itself to verified by returning a matching
JSON shape. An uncertain outcome, or failed verification after dispatch, leaves
the durable operation uncertain and blocks later effects. Browser verification
is recorded separately from Journal's opaque-effect `returned` state; a timeout
or lost response never grants permission to replay a possible submission.

Main regression coverage lives in `test_action_policy.py`,
`test_action_approval_binding.py`, browser/MCP integration tests, and the existing
gate, resume, Journal, desktop and shell suites. It covers the authorization and
uncertainty main paths rather than adding a test for every individual UI feature.

The browser upload policy uses `browser.upload_workspace_roots` (empty means the
current conversation workspace), `browser.sensitive_paths`, and
`browser.max_upload_bytes`. Known credential directories, environment files,
private-key/certificate extensions and user-designated paths are blocked by the
controlled filesystem/upload adapters. Labels describe known locations and do
not certify the contents of an otherwise ordinary document. File fingerprints
cover full content, canonical path, device/inode, size and modification/change
timestamps; CDP's separate file open remains an explicit race limitation.

MCP `allowed_destinations` constrains exact observable HTTP(S) origins in the
configured endpoint and URL arguments. An empty list leaves the configured tool
trust/review boundary in place. A fresh `list_tools` immediately before transport
dispatch rejects changed schemas/annotations even when the tool count is
unchanged; configuration, manager replacement, tool allowlist and material args
are checked independently. The SDK/server may still implement hidden requests,
resolve recipients internally or redirect its own traffic. This mechanism is not
a packet-level firewall or an attestation of remote behavior.

Browser/MCP script and form payloads are structurally omitted from public input
events, history tool rows and ordinary traces; the actual dispatcher retains its
private arguments. Known secret values and credential patterns are redacted from
standard logging (including exception traces), UI events and support exports.
Pattern scanners cannot detect every sensitive value in arbitrary page content,
ordinary files or opaque remote output. They supplement location/capability
controls rather than serving as proof that a payload is safe.
