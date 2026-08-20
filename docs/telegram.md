# Telegram

Monaw can expose the same local agent runtime through a Telegram bot. Telegram is optional.

## What Telegram Does

Telegram messages become Monaw conversations. The bot can continue chat history, switch conversations, switch models for a Telegram chat, start new sessions, approve permission prompts, and return files produced by the agent.

## Setup

1. Create a bot with BotFather.
2. Copy the bot token.
3. Open Monaw Settings -> Connections -> Telegram.
4. Paste the bot token.
5. Add allowed Telegram user IDs or chat IDs.
6. Restart the app or integration if needed.
7. Send `/start` to the bot.

Keep the bot token private.

## Authorization

Telegram access is controlled by allowlists.

| Setting | Meaning |
| --- | --- |
| `telegram_bot_token` | Bot token from BotFather. |
| `telegram_allowed_user_ids` | Comma, semicolon, or newline separated numeric Telegram user IDs. |
| `telegram_allowed_chat_ids` | Comma, semicolon, or newline separated numeric Telegram chat IDs. |
| `telegram_allow_all` | Allows all Telegram users. This is unsafe and should stay off. |

An update is allowed if any of these are true:

| Rule | Meaning |
| --- | --- |
| `telegram_allow_all` is true | Everyone is allowed. Not recommended. |
| User id is in `telegram_allowed_user_ids` | That Telegram account can use the bot. |
| Chat id is in `telegram_allowed_chat_ids` | That chat can use the bot. |

Unauthorized chats receive:

```text
This Telegram chat is not authorized for this agent.
```

## Getting IDs

Use a Telegram ID helper bot, or temporarily inspect backend logs after an unauthorized message. Monaw logs rejected update user and chat ids.

Do not leave `telegram_allow_all` enabled just to discover ids.

## Commands

| Command | Purpose |
| --- | --- |
| `/start` | Show bot status and current session. |
| `/help` | Show bot help. |
| `/status` | Show the active chat, selected model, and whether the model is a Telegram override or the desktop default. |
| `/whoami` | Show the Telegram user id, chat id, and topic/thread id for allowlist setup. |
| `/session` | Show the active chat name. |
| `/session rename <chat name>` | Rename the active chat. |
| `/resume` | List recent chat history. |
| `/resume 2` | Switch to a listed chat by number. |
| `/resume <chat name>` | Switch by chat title. |
| `/models` | List model options. |
| `/models 2` | Switch this Telegram chat to a listed model. |
| `/models <model name>` | Switch by model name. |
| `/modeldefault` | Clear this Telegram chat's model override and use the desktop default model. |
| `/effort` | List reasoning effort options. |
| `/effort 4` | Switch this Telegram chat to a listed reasoning effort. |
| `/effort <effort name>` | Switch by effort name, for example `/effort high`. |
| `/effort default` | Clear this Telegram chat's effort override and use the desktop default effort. |
| `/compact` | Compact this chat so future replies use the generated summary instead of earlier raw history. |
| `/new` | Start a fresh Telegram conversation session. |

## Conversation Behavior

Each Telegram chat maps to a Monaw conversation. Forum topic thread ids are treated separately where Telegram provides `message_thread_id`.

Conversation titles are created as:

```text
Telegram - <chat title or sender name or chat id>
```

`/resume` lists recent Monaw conversations and lets the Telegram chat switch to one.

`/session rename <chat name>` renames the active Monaw conversation. `/new` creates a new conversation and makes it the active session for that Telegram chat.

## Model Selection

By default, Telegram uses the desktop app's current model settings.

`/models` can set a per-conversation override. The override is stored for that Telegram conversation and does not change the desktop default model.

Use `/modeldefault` to clear the override and return that Telegram chat to the desktop app's current model settings.

`/effort` works the same way for reasoning effort. It can set a per-conversation effort override without changing the desktop default.

If a provider is invalid or missing, the bridge falls back to OpenAI defaults.

## Message Handling

Telegram text messages run through the same agent stream as desktop chat.

Photos and documents are downloaded into Monaw's runtime upload store and passed to the agent as chat attachments. Telegram voice messages, audio messages, and audio documents are transcribed before the agent receives them. The desktop chat input also has a microphone button that uses the same speech-to-text path and inserts the transcript into the text field while dictating.

Speech-to-text supports two engines:

| Engine | Behavior |
| --- | --- |
| Local | Powered by `faster-whisper` with the Whisper base model. Open Settings -> Model -> Speech to Text and download the local model before sending voice input. The same panel can offload the loaded model from memory or delete the downloaded files. |
| Cloud | Uses Gemini with `gemini-2.5-flash`. Configure a Google API key in Settings -> Connections before using this mode. |

If the local voice model is not installed yet, Telegram replies:

```text
Voice model is not installed/configured yet. Open Settings > Model > Speech to Text and download the model first.
```

If cloud speech-to-text is selected without a Google API key, Telegram replies with the missing key message instead of sending the audio to the agent.

Only one Telegram request per chat/thread runs at a time. If another message arrives while a request is running, the bot replies:

```text
A previous Telegram request is still running for this chat. Please wait for it to finish.
```

Agent responses are simplified to plain text for Telegram. Markdown tables, code fences, links, headings, and inline formatting are converted into safer plain text.

Long responses are split into safe Telegram-sized chunks.

## Permission Prompts

If a Telegram request hits a sensitive action approval, the bot sends inline buttons:

| Button | Behavior |
| --- | --- |
| Approve | Approves the pending action and resumes the waiting agent turn. |
| Reject | Rejects the pending action and resumes the waiting agent turn with denial. |

If a Telegram request hits an access grant for an unknown app or path, the bot sends:

| Button | Behavior |
| --- | --- |
| Once | Allows this one action only. |
| Session | Allows the target for the current session. |
| Always | Persists the target to the local allowlist. |
| Deny | Blocks the action. |

The desktop app can still approve or reject the same pending request. If either surface resolves it first, the other surface will show that the request is no longer pending.

## Attachments

Incoming Telegram photos and documents become chat attachments, matching the desktop chat upload flow. Audio inputs are handled as speech-to-text instead of file attachments so the selected chat model receives text.

When the agent creates response attachments, Telegram can send them back.

Limits:

| Type | Limit |
| --- | --- |
| Any attachment | 50 MB |
| Photo upload | 10 MB |
| Audio transcription input | 25 MB |

Images over the photo limit or images Telegram rejects by dimensions are sent as documents when possible.

If a file is too large, the bot sends the filename and local path instead.

## Scheduled Task Notifications

Scheduled tasks can send their final result to Telegram when:

| Requirement | Meaning |
| --- | --- |
| Telegram bot is running | A valid bot token is configured and service startup succeeded. |
| `notifyTelegram` is true | The task wants Telegram notification. |
| `telegramChatId` is set | Monaw knows where to send the result. |
| Run is not skipped | Skipped runs are not sent. |

The scheduling API can list known Telegram chats with:

```text
GET /api/scheduled-tasks/telegram-chats
```

## Runtime And Logs

Telegram starts during FastAPI startup only if a token is configured.

Runtime status is exposed through backend app state and Settings diagnostics. Startup failures are logged in:

```text
%USERPROFILE%\.monaw\runtime\backend.log
```

## Security Notes

Authorized Telegram users can ask the local Monaw runtime to act on your machine through your configured permission model.

Keep these rules:

| Rule | Reason |
| --- | --- |
| Keep the bot token private. | Anyone with the token can control the bot. |
| Keep allowlists narrow. | Telegram is a remote control surface. |
| Do not enable allow all. | It removes the user/chat boundary. |
| Keep approval gates enabled for risky actions. | Telegram and desktop can both resolve the same pending permission ticket. |
| Avoid sending secrets through Telegram. | Telegram messages are stored in conversation history. |

See [operations.md](operations.md) for source-specific restrictions, credential
rotation, backup, retention, deletion, and incident guidance.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| Bot does not respond. | Confirm token, network access, backend startup, and `%USERPROFILE%\.monaw\runtime\backend.log`. |
| Bot says chat is unauthorized. | Add the user id or chat id to the allowlist. |
| `/models` does not show expected model. | Check local model catalog and desktop model settings. |
| Request waits for approval. | Use the Telegram inline buttons or approve/reject the pending ticket in the desktop app. |
| Attachments do not send. | Check file existence and Telegram size limits. |
| Scheduled notifications do not arrive. | Confirm the scheduled task has `notifyTelegram` and `telegramChatId`, and the bot is running. |
