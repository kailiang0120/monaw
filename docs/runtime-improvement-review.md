# Runtime improvement review

Scope: runtime correctness, persistence, resource limits and everyday task flows.
Release, packaging and distribution changes are outside this review.

## Whole-task reliability follow-up

Three additional improvements address work that spans several tool batches:

| Area | Updated behavior |
| --- | --- |
| Task outcomes | Persist original goals, pending/failed outcomes, and returned evidence independently of the current batch. Preserve them through pause, restart, and compaction; reject stale updates and unsupported completion claims. |
| Useful progress | Compare observation results across rounds, give one recovery hint when results stop changing, then pause a bounded loop. Status polling gets a longer finite window. |
| Outcome evaluations | Run six isolated tasks against real tools and durable state; grade exact files, read-back, missing-input handling, recovery, and duplicate effects. Report model requests, tokens, and duration. |

The live long-context evaluation exposed a planning dead end: a read-back check
was labeled as a file mutation, and immutable kind metadata prevented correction.
Unfinished outcomes can now correct that classification with a recorded reason
without replacing their acceptance criteria or repeating completed file edits.
See [task outcomes and evaluations](harness-evaluations.md) for the protocol,
commands, evidence boundaries, and limits.

## Turn completion follow-up

The harness runs provider requests through `ModelRequestRunner`, checks each
response with `TurnResponsePolicy`, executes accepted calls through
`ToolBatchRunner`, and saves one outcome through `TurnLifecycle`. Tool schemas,
permissions, durable operation journals and cancellation remain separate from
the decision to finish a turn.

The completion review found several cases where the user-visible ending did not
match the available evidence:

| Trigger | Previous behavior | Updated behavior |
| --- | --- | --- |
| Empty or reasoning-only response | Saved a successful `Done.` | Ask once for an answer or necessary action, then pause if the response is still unusable. Alternating empty output and progress cannot reset this limit. |
| Gemini rejects a response or produces an invalid function call | Normalized the ending to an ordinary stop | Preserve blocked or incomplete status, suppress its calls, and keep the task resumable. Record the original provider reason in diagnostics. |
| OpenAI refusal or content filtering | Could lose refusal text or label filtering as an output-length limit | Preserve the refusal and distinguish a blocked response from truncation. |
| Text promises another action | Forced the next provider request to call a tool | Reprompt once with automatic tool choice, allowing a clarification or final answer without an unrelated action. Check closing prose in longer replies as well. |
| JSON answer contains `summary` or `answer` | Replaced the entire JSON value with that field | Preserve ordinary JSON verbatim. Only explicit legacy `final_answer` envelopes are unwrapped. |
| Pause arrives during a guard's final summary | Waited for the summary request to finish | Cancel that request through the same pause boundary as the main model call. |
| Guard summary is truncated or promises more work | Replaced the pause notice with the unusable summary | Keep the original notice and exclude the rejected summary from saved provider history. |
| Pause or Stop arrives while the final outcome is being saved | Accepted a control after completion had been selected; Pause could turn a successful run into `needs_review` | Reject new controls once finalization owns the outcome. Direct pause requests obey the same boundary, and snapshots stop advertising unavailable controls. |

Normal final answers do not acquire an additional model-verification call.
Existing token, iteration and wall-clock budgets still apply, including to the
single repair attempt. A guard summary cannot change a paused run into a
completed one.

Live checks used Gemini Flash-Lite with the real Electron app and backend, an
isolated profile and synthetic files. They verified exact JSON output, a
read/write/approval/read-back task, and a same-conversation follow-up that reused
the verified results. An initial lower test allowance also exercised
the token-budget pause; the file task completed with sufficient allowance.

The broader runtime suite passed 581 checks. After the desktop workflow exposed
the late-control race, 129 focused completion, lifecycle and Run API checks
passed, including deterministic Pause/Stop requests during persistence. The
affected desktop Pause/Continue workflow passed three consecutive runs; the
other 18 desktop workflows passed in the full application run. These test sets
overlap. The desktop pause test now waits for an accepted command before
releasing its model barrier, so it tests an intentional pause rather than racing
the final answer.

These checks improve the completion protocol; they do not prove semantic task
completion for every prompt. The model still has to judge whether its verified
results satisfy the user's request. The progress-text detector is a bounded
heuristic, not an authorization decision or a general language classifier.

## Changes

| Area | Problem | Improvement |
| --- | --- | --- |
| Workspace checkpoints | Synchronous Git work stalled the event loop and lease heartbeats. | One dedicated worker captures checkpoints; cancellation drains it before releasing ownership. |
| Browser targeting | Interleaved conversations shared a selected tab. | Existing-page tools require an explicit `pageId` on every call. |
| Provider completion | Unfinished streams could expose callable tools; retrying visible output could append a second answer. | Require terminal completion for OpenAI and Gemini, reject content after completion, discard incomplete tool calls, and stop retries once text or reasoning is visible. |
| Tool contracts | Argument coercion discarded permitted extra fields and collapsed schema alternatives. | Preserve arguments, validate full JSON Schema locally, and send original schemas through Gemini's JSON Schema field. |
| Event buffers | Merged deltas could exceed replay limits and the renderer retained an entire run. | Bound merged entries and replay/backlog bytes; keep the latest 500 renderer events with batched publication. |
| Chat history | Only the first 100 chats were reachable; history searches scanned message content. | Page in SQLite and search titles, messages and folders through a native history dialog backed by transactional FTS5 trigram indexes. |
| Scheduled context | Global model changes could silently change later executions. | Save workspace and model selection in task configuration and accepted occurrence snapshots, with an explicit follow-default option. |
| Notifications | A completed task's failed delivery was lost. | Commit a durable notification outbox with the result and retry delivery independently, preserving acknowledged chunks. |
| Model usage | Compaction, helper calls and failed attempts were absent from turn accounting. | Share one request ledger and reserve input/output allowance before provider attempts; give background learning a bounded allocation from the remaining turn allowance. |
| Sidebar accessibility | Invisible collapsed/expanded controls remained keyboard targets. | Mark the hidden panel `inert` and transfer focus when collapsing or expanding. |
| Browser form destinations | A submit handler could change the destination after approval. | Install a native `form-action` policy in the approved frame's isolated world before input; Chrome enforces it during navigation, including delayed and popup submissions. |
| Provider capabilities | One output allowance was assumed for every provider and model. | Model capability records distinguish account calls, conservative API defaults and cached Gemini output limits; enforce numeric thinking minimums before dispatch. |
| Usage estimates | Images used a fixed estimate, screenshot encodings could count as text, and hidden reasoning was missed. | Read image dimensions locally, count tool-result images separately, retain unreported output reservations, and avoid double-counting provider reasoning/image subtotals. |
| Module boundaries | History SQL and provider-specific callback branches lived in broad orchestration modules. | Extract the history reader, form navigation policy, capability lookup and image estimator; give every provider the same answer/reasoning callback interface. |
| Compatibility checks | Browser checks could skip when Chrome was unavailable; fake responses did not prove SDK serialization. | Require isolated real-browser checks for the declared minimum Chrome and current stable, with actual OpenAI/Gemini SDKs decoding fixture HTTP/SSE on Python 3.11 and 3.12 in CI. |
| Pause/resume ordering | Timestamp precision or clock changes could make an old transcript appear to contain a later opaque action. | Record the returned operation's event sequence and the checkpoint's sequence in their owning transactions; continuation requires durable ordering evidence. |

## Usage and delivery limits

When a turn has a token limit, API requests reserve estimated input plus up to
16,384 output tokens, capped by the remaining allowance and provider capability.
Unknown OpenAI models use a conservative 4,096 allowance; Gemini falls back to
8,192 when its model metadata cannot be read within three seconds. Successful
metadata is cached per client/model for ten minutes; failures for one minute.
A request that cannot fit pauses the turn before generation. Successful responses
settle the reservation using reported usage. Missing usage increments
`estimated_requests` in trace metadata. Failed attempts and responses with
unreported hidden reasoning retain their entire reserved output allowance;
other completed responses use the visible-output estimate. This can deliberately
pause a limited turn early after a provider outage.
Input estimates and provider billing can differ; this is a runtime guardrail,
not a billing guarantee. ChatGPT account requests do not support the API output
cap and can exceed the reserved allowance in one request; subsequent requests
still stop when the budget is exhausted. Account usage remains classified
separately from API charges.

Automatic compaction belongs to the main turn's ledger. Manual `/compact` and
background learning have separate observability records. Background learning is
limited to two concurrent jobs per memory store, 60 seconds, and at most 32,768
tokens from the remaining parent allowance (or that fixed allowance when the
turn is unlimited). It is skipped when no allowance or worker slot remains.

Notification delivery is at least once. A crash between Telegram accepting a
chunk and SQLite saving its acknowledgement can duplicate that chunk. The
outbox does not repeat the agent execution. See [scheduling](scheduling.md).

## Scope and remaining platform limits

The five follow-up areas from the initial review are implemented in this branch.
The browser regression is now a passing test rather than an expected failure.
The form guard follows [native CSP matching rules](https://www.w3.org/TR/CSP3/#directive-form-action):
queries are not matched and a trailing slash denotes a path prefix. It protects
HTML form navigation, not arbitrary page JavaScript requests. The policy lasts
until document replacement; submitting to a different destination requires a
reload and a fresh review. Unsupported destinations/documents fail before input.
Browser-reported violations are surfaced when observed; a delayed violation can
occur after the tool result has returned, while Chrome still blocks the request.

[SQLite's trigram index](https://www.sqlite.org/fts5.html#the_trigram_tokenizer)
handles literal searches of at least three characters. Shorter searches retain
the previous scan so two-character CJK terms still work. Index creation backfills
existing history; SQLite triggers keep inserts, edits, deletes and rollbacks in
the same transaction as the source records. The history API remains paginated.

Contract checks exercise the installed SDKs with fixture HTTP/SSE responses;
they do not use live credentials or establish live-service availability. CI runs
the browser boundary tests against both supported endpoint versions, rather than
claiming every intervening Chrome version was tested. The fixture accepts
`MONAW_TEST_CHROME` and `MONAW_REQUIRE_BROWSER_TESTS=1` to select a binary and make
missing-browser skips fail. `scripts/fetch-test-chrome.py` resolves the manifest's
minimum or current stable from Google's Chrome for Testing feed into an isolated
directory without changing the user's browser.
On Windows it runs Chrome's bundled configuration-only helper to grant its
sandbox capabilities access to the extracted browser files. This fixes network
service crashes on CI directories with restrictive inherited permissions while
keeping the browser sandbox enabled.
The fixture owns a dedicated DevTools target and reports browser version and
network evidence when a test navigation fails.

Navigation readiness checks the returned document loader and its ready state.
A delayed load event from the previous page cannot finish that bounded wait;
readiness includes the requested document's child frames. Browser
contract failures include the observed snapshot to distinguish loading failures
from missing controls.

Image estimates use local header dimensions and known tile-model rules;
other models use a conservative size-based heuristic. Remote image URLs use a
fallback and are never fetched for estimation. Provider usage remains authoritative.
See [OpenAI image tokenization](https://developers.openai.com/api/docs/guides/images-vision),
[Gemini model limits](https://ai.google.dev/api/models), and
[Gemini completion/usage fields](https://ai.google.dev/api/generate-content).
Exact remote billing and exactly-once external notification delivery remain
outside what local code can guarantee.

Migration 0028 leaves legacy opaque-action checkpoints without ordering evidence
non-resumable. It preserves their transcript and operation history rather than
guessing that resuming is safe. New checkpoints remain resumable after clock
changes and event-log pruning because both sequence values are stored durably.

Focused regressions cover interrupted streams, nested schemas, bounded replay,
checkpoint cancellation, old-history paging/search, model budget exhaustion,
schema upgrades and notification recovery across a database restart. The
branch was verified locally with 1,834 backend tests passing (8 environment-dependent
skips), 282 renderer tests, 44 Electron unit tests, type checks and renderer builds,
and all 18 isolated application workflows. Real-browser checks passed on Chrome
125.0.6422.141 and stable 158.0.8127.88. The SDK contract fixtures, generated API
documentation check, and compilation/import/health check for all 333 backend
modules also passed. Live provider availability and GitHub-hosted CI are separate
from this local evidence.
