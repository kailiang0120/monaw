# Conversation compaction

Compaction reduces the history sent to the model. It does not delete the original
messages or tool results in SQLite. Durable task state is stored separately from
generated summaries, and is included in every task request.

| Context | What reaches the next request |
| --- | --- |
| Original task goal | The original saved text, outside the summary. It is saved before the first model request, including when no plan or tool result exists yet. Existing secret redaction still applies. |
| Current progress | Every outcome's ID, acceptance criteria, targets, status, reason, and evidence references. Done, pending, failed, blocked, and waiting-for-user states are retained. |
| User requests and corrections for an open task | Exact saved user messages and applied steering since the task's history boundary. They are restored directly from message storage, independently of the generated summary, until the task completes or is explicitly replaced. |
| Failed or paused turns in the active task | Their complete saved requests, applied steering, assistant responses, final statuses, and all saved tool inputs and outputs. No five-result limit or result-preview truncation applies to protected turns. |
| Failed tool rounds in the running turn | The call and all sibling results stay together and are excluded from shortening and summarization for the rest of that turn. |
| Current user instructions and latest tool round | Live compaction preserves instructions and does not split a tool-call/result round. Successful large observations may be shortened when the context budget requires it. |
| Older completed conversation and successful observations | Eligible for a generated background summary. Long successful tool results may use bounded excerpts; their originals remain in saved tool history. |
| Earlier task evidence | Referenced evidence and the latest recorded mutations remain in the task ledger, alongside 64 recent unreferenced results. The prompt includes eight recent evidence records; `task_plan` with `steps=[]` retrieves the saved ledger. |

## Across turns and restarts

1. Save the task's original goal before preparing context or calling the model.
2. Locate failed or paused turns in that task using their saved turn checkpoints.
   An explicit replacement task starts a new history boundary.
3. A between-turn compaction checkpoint stops before the earliest protected turn. A later
   clarification reply does not remove protection while the task remains open.
4. Restore the entire suffix after the compaction checkpoint, including its newest
   exchange. Twenty-four messages triggers a compaction attempt; it is not a
   restoration cutoff. Restore exact open-task user inputs from before the
   checkpoint separately, even if the summary omitted a constraint.
5. Restore protected failed turns even when they precede the checkpoint.
   Matching Gemini history retains its native signed parts and paired tool
   results. Switching providers or models uses the saved visible text and full
   tool evidence instead.
6. Inject task state separately. Summaries are background; they cannot replace
   the exact goal, outcome state, retained user instructions, or failure records.

New failures add to protected history. In particular, a context-budget pause must
not displace the earlier tool failure it could not fit. Successful intervening
assistant work can still be compacted by the live context fitter. Once the task
and a reply complete successfully, or the user explicitly replaces the task,
the earlier failures can be summarized as historical work. Reading a failure
checkpoint never re-executes a tool. Legacy chats without task state retain the
failure chain since their last successful reply.

A failed summarizer leaves its checkpoint unchanged. Monaw can continue with the
complete restored context if it fits the selected model's input budget. Otherwise
the turn pauses with `context_budget_exceeded`; it does not choose a recent or
earlier slice that could omit constraints or the latest reply. A subsequent turn
can retry compaction after the provider recovers. Completed-task history can be
summarized normally; exact user-input retention follows the active task boundary
rather than preserving every earlier conversation instruction indefinitely.

## Budget and storage limits

The task ledger allows 32 outcomes and a 256 KB snapshot. Protected history
restoration allows up to 1,000 messages, counting both the uncompacted suffix and
the earlier retained user inputs. Exceeding that guard pauses restoration rather
than shortening the suffix. Live context also reserves room for the
model's reply and counts the system prompt, tool schemas, messages, and images.
If protected material cannot fit, Monaw pauses with a context-budget error.
It does not silently shorten that material or send an oversized model request.
Using a model with a larger context or reducing selected tools can help with
the token limit; the saved transcript remains available if the task needs to be
split into a new chat.

"Exact" means the saved, permitted records: existing tool-secret redaction,
private-input handling, and attachment access checks still apply. Missing or
inaccessible image bytes are not reconstructed. Compaction does not promise
retention of private model reasoning or external state that was never recorded.

## Completion failures

The final answer checkpoint, answer-outcome completion, and owned durable run
transition share one SQLite transaction. The workspace checkpoint finishes before
that transaction starts. If any required write or the run transition fails, the
entire completion rolls back, including in-memory task changes. The harness then
makes one attempt to commit the error checkpoint and failed run together, without
repeating model requests, tool actions, or task completion. The saved answer and
final event therefore report the same failure instead of retaining a generated
success reply as a completed message.

If run-state storage remains unavailable, Monaw attempts to save an error-only
transcript and mark the run `needs_review`. If even that cannot be saved, it
explicitly reports that history and run state could not be saved; it cannot
guarantee a new persisted status during a continuing storage outage. It never
commits successful answer delivery before the required durable transition.
Error-only checkpoints still verify the same owner, attempt and unexpired lease
inside their transaction. A worker that lost ownership cannot append history,
seal a checkpoint or change the conversation watermark for the newer attempt.

Core regressions exercise real SQLite restart, steering between tool results,
failure history beyond the recent-message window, full long error outputs,
failed summaries with and without existing checkpoints, exact corrections after
a lossy summary, provider-wire serialization, protected-budget exhaustion, and an injected task
write failure. See [harness evaluations](harness-evaluations.md) for the isolated
end-to-end task suite.
