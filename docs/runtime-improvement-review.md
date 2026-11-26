# Runtime improvement review

Scope: runtime correctness, persistence, resource limits and everyday task flows.
Release, packaging and distribution changes are outside this review.

## Changes

| Area | Problem | Improvement |
| --- | --- | --- |
| Workspace checkpoints | Synchronous Git work stalled the event loop and lease heartbeats. | One dedicated worker captures checkpoints; cancellation drains it before releasing ownership. |
| Browser targeting | Interleaved conversations shared a selected tab. | Existing-page tools require an explicit `pageId` on every call. |
| Provider completion | An unfinished OpenAI stream could expose callable tools; retrying visible output could append a second answer. | Require terminal completion before accepting streamed tools, discard incomplete tool calls, and stop retries once text or reasoning is visible. |
| Tool contracts | Argument coercion discarded permitted extra fields and collapsed schema alternatives. | Preserve arguments, validate full JSON Schema locally, and send original schemas through Gemini's JSON Schema field. |
| Event buffers | Merged deltas could exceed replay limits and the renderer retained an entire run. | Bound merged entries and replay/backlog bytes; keep the latest 500 renderer events with batched publication. |
| Chat history | Only the first 100 chats were reachable; the backend loaded all metadata before slicing. | Query pages in SQLite and search titles, messages and folders through a native history dialog. |
| Scheduled context | Global model changes could silently change later executions. | Save workspace and model selection in task configuration and accepted occurrence snapshots, with an explicit follow-default option. |
| Notifications | A completed task's failed delivery was lost. | Commit a durable notification outbox with the result and retry delivery independently, preserving acknowledged chunks. |
| Model usage | Compaction, helper calls and failed attempts were absent from turn accounting. | Share one request ledger and reserve input/output allowance before provider attempts; give background learning a bounded allocation from the remaining turn allowance. |
| Sidebar accessibility | Invisible collapsed/expanded controls remained keyboard targets. | Mark the hidden panel `inert` and transfer focus when collapsing or expanding. |

## Usage and delivery limits

When a turn has a token limit, API requests reserve estimated input plus up to
16,384 output tokens, capped by the remaining allowance. A request that cannot
fit pauses the turn before contacting the provider. Successful responses settle
the reservation using reported usage. Missing usage, including failed attempts,
uses a local estimate and increments `estimated_requests` in trace metadata.
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

## Further improvements

1. Enforce approved browser destinations at the network request boundary. The
   existing expected-failure adversarial test shows that a page's submit handler
   can change its destination after approval. Pre-action inspection alone cannot
   prevent that change; explicit page targeting does not resolve this separate
   gap.
2. Add a full-text search index when conversation volume warrants it. Current
   search is paged and bounded in response size, but matching message content
   still scans candidates with `instr`; it is not indexed full-text search.
3. Expand real-browser and provider contract checks across supported versions.
   Unit fixtures cover failure boundaries, but do not establish live-provider
   behavior or every Chrome accessibility-tree variation.
4. Continue separating persistence, provider transport and orchestration behind
   explicit interfaces. This change extracts usage and notification state; the
   remaining runtime and database modules still have broad responsibilities.
5. Add provider-specific output-cap metadata and better usage estimation for
   images, hidden reasoning and account responses. Exact remote billing and
   exactly-once external notification delivery cannot be inferred locally.

Focused regressions cover interrupted streams, nested schemas, bounded replay,
checkpoint cancellation, old-history paging/search, model budget exhaustion,
schema upgrades and notification recovery across a database restart. The
repository's full backend and frontend verification remains required before
merging.
