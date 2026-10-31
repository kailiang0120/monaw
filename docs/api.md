# API Route Reference

This file is generated from FastAPI route definitions. Update it with:

```powershell
uv run --project backend python scripts/generate-api-docs.py
```

The backend API is a privileged local control plane. All `/api` routes
require an Electron-minted bearer session. `/health` is the only
unauthenticated runtime endpoint.

| Method | Path | Required scope | Response model |
| --- | --- | --- | --- |
| `GET` | `/api/access-grants/pending` | `approval:resolve` | `list` |
| `POST` | `/api/access-grants/{ticket_id}/resolve` | `approval:resolve` | `AccessGrantTicketOut` |
| `GET` | `/api/approvals/history` | `approval:resolve` | `list` |
| `GET` | `/api/approvals/pending` | `approval:resolve` | `list` |
| `POST` | `/api/approvals/{ticket_id}/approve` | `approval:resolve` | `ApprovalTicketOut` |
| `POST` | `/api/approvals/{ticket_id}/reject` | `approval:resolve` | `ApprovalTicketOut` |
| `POST` | `/api/chat` | `agent:run` | `stream/file/none` |
| `GET` | `/api/chat/jobs` | `agent:run` | `list` |
| `POST` | `/api/chat/jobs` | `agent:run` | `ChatJobCreateResponse` |
| `POST` | `/api/chat/jobs/{job_id}/cancel` | `agent:run` | `OkResponse` |
| `GET` | `/api/chat/jobs/{job_id}/stream` | `agent:run` | `stream/file/none` |
| `POST` | `/api/chat/steer` | `agent:run` | `ChatSteerResponse` |
| `GET` | `/api/conversations` | `agent:run` | `list` |
| `POST` | `/api/conversations` | `agent:run` | `ConversationOut` |
| `POST` | `/api/conversations/workspace-validation` | `agent:run` | `ConversationWorkspace` |
| `DELETE` | `/api/conversations/{conv_id}` | `agent:run` | `OkResponse` |
| `PATCH` | `/api/conversations/{conv_id}` | `agent:run` | `ConversationOut` |
| `GET` | `/api/conversations/{conv_id}/context-usage` | `agent:run` | `ContextUsagePayload` |
| `GET` | `/api/conversations/{conv_id}/messages` | `agent:run` | `MessagesResponse` |
| `GET` | `/api/diagnostics/browser-use` | `diagnostics:read` | `BrowserUseDiagnosticsOut` |
| `POST` | `/api/diagnostics/browser-use/extension/install` | `diagnostics:control` | `BrowserExtensionInstallOut` |
| `POST` | `/api/diagnostics/browser-use/reset` | `diagnostics:control` | `BrowserUseDiagnosticsOut` |
| `GET` | `/api/diagnostics/mcp` | `diagnostics:read` | `list` |
| `POST` | `/api/diagnostics/mcp/{name}/reconnect` | `diagnostics:control` | `MCPServerDiagnosticsOut` |
| `GET` | `/api/diagnostics/summary` | `diagnostics:read` | `DiagnosticsSummaryPayload` |
| `GET` | `/api/diagnostics/support-report` | `diagnostics:read` | `stream/file/none` |
| `GET` | `/api/events` | `agent:run` | `stream/file/none` |
| `GET` | `/api/files/{attachment_id}` | `files:read` | `stream/file/none` |
| `GET` | `/api/files/{attachment_id}/preview` | `files:read` | `stream/file/none` |
| `GET` | `/api/memories` | `agent:run` | `list` |
| `POST` | `/api/memories` | `agent:run` | `MemoryOut` |
| `GET` | `/api/memories/index/health` | `agent:run` | `stream/file/none` |
| `POST` | `/api/memories/index/rebuild` | `agent:run` | `stream/file/none` |
| `GET` | `/api/memories/inspect` | `agent:run` | `stream/file/none` |
| `GET` | `/api/memories/search` | `agent:run` | `list` |
| `GET` | `/api/memories/workspaces` | `agent:run` | `list` |
| `DELETE` | `/api/memories/{memory_id}` | `agent:run` | `OkResponse` |
| `PATCH` | `/api/memories/{memory_id}` | `agent:run` | `MemoryOut` |
| `GET` | `/api/messages/{message_id}/tool-calls` | `agent:run` | `list` |
| `GET` | `/api/observability/errors` | `diagnostics:read` | `list` |
| `GET` | `/api/observability/logs/backend` | `diagnostics:read` | `ObservabilityBackendLogOut` |
| `GET` | `/api/observability/runs` | `diagnostics:read` | `list` |
| `GET` | `/api/observability/runs/{run_id}` | `diagnostics:read` | `ObservabilityRunDetailOut` |
| `POST` | `/api/observability/runs/{run_id}/export-debug-bundle` | `diagnostics:control` | `ObservabilityDebugBundleOut` |
| `POST` | `/api/observability/runs/{run_id}/replay` | `diagnostics:control` | `ObservabilityReplayResultOut` |
| `GET` | `/api/observability/summary` | `diagnostics:read` | `ObservabilitySummaryOut` |
| `DELETE` | `/api/observability/support-mode` | `diagnostics:control` | `ObservabilitySupportModeOut` |
| `GET` | `/api/observability/support-mode` | `diagnostics:read` | `ObservabilitySupportModeOut` |
| `POST` | `/api/observability/support-mode` | `diagnostics:control` | `ObservabilitySupportModeOut` |
| `POST` | `/api/openai-account/login` | `settings:write` | `dict` |
| `POST` | `/api/openai-account/logout` | `settings:write` | `dict` |
| `GET` | `/api/openai-account/status` | `settings:read` | `dict` |
| `POST` | `/api/privacy/delete-data` | `agent:run` | `DataDeletionResult` |
| `POST` | `/api/sandbox/docker/resolve-image` | `settings:write` | `SandboxResolveImageResponse` |
| `GET` | `/api/sandbox/status` | `settings:read` | `SandboxStatusPayload` |
| `GET` | `/api/scheduled-tasks` | `agent:run` | `list` |
| `POST` | `/api/scheduled-tasks` | `agent:run` | `ScheduledTaskOut` |
| `POST` | `/api/scheduled-tasks/preview` | `agent:run` | `SchedulePreviewResponse` |
| `GET` | `/api/scheduled-tasks/telegram-chats` | `agent:run` | `TelegramChatsResponse` |
| `DELETE` | `/api/scheduled-tasks/{task_id}` | `agent:run` | `stream/file/none` |
| `PATCH` | `/api/scheduled-tasks/{task_id}` | `agent:run` | `ScheduledTaskOut` |
| `GET` | `/api/scheduled-tasks/{task_id}/operations/{operation_key}` | `agent:run` | `RunNowResponse` |
| `POST` | `/api/scheduled-tasks/{task_id}/run` | `agent:run` | `RunNowResponse` |
| `GET` | `/api/scheduled-tasks/{task_id}/runs` | `agent:run` | `list` |
| `GET` | `/api/settings` | `settings:read` | `AgentSettingsPayload` |
| `PUT` | `/api/settings` | `settings:write` | `AgentSettingsPayload` |
| `GET` | `/api/settings/allowlisted-apps` | `settings:read` | `list` |
| `POST` | `/api/settings/allowlisted-apps` | `settings:write` | `AppEntryOut` |
| `DELETE` | `/api/settings/allowlisted-apps/{alias}` | `settings:write` | `OkResponse` |
| `GET` | `/api/settings/controller-policy` | `settings:read` | `ControllerPolicyOut` |
| `PUT` | `/api/settings/controller-policy` | `settings:write` | `OkResponse` |
| `GET` | `/api/settings/controller-policy-markdown` | `settings:read` | `ControllerPolicyMarkdownPayload` |
| `POST` | `/api/settings/mcp-credentials/commit` | `mcp:credentials` | `stream/file/none` |
| `POST` | `/api/settings/mcp-credentials/hydrate` | `mcp:credentials` | `stream/file/none` |
| `GET` | `/api/settings/mcp-credentials/inventory` | `mcp:credentials` | `stream/file/none` |
| `POST` | `/api/settings/mcp-credentials/prepare` | `mcp:credentials` | `stream/file/none` |
| `POST` | `/api/settings/mcp-credentials/revoke` | `mcp:credentials` | `stream/file/none` |
| `GET` | `/api/settings/model-options` | `settings:read` | `ModelOptionsPayload` |
| `GET` | `/api/settings/speech-to-text` | `settings:read` | `SpeechToTextStatusPayload` |
| `GET` | `/api/settings/storage` | `settings:read` | `StorageUsage` |
| `POST` | `/api/settings/storage/{category}/clean` | `settings:write` | `StorageCleanResult` |
| `DELETE` | `/api/settings/workspace-instructions` | `settings:write` | `WorkspaceInstructionsPayload` |
| `GET` | `/api/settings/workspace-instructions` | `settings:read` | `WorkspaceInstructionsPayload` |
| `PUT` | `/api/settings/workspace-instructions` | `settings:write` | `WorkspaceInstructionsPayload` |
| `POST` | `/api/speech-to-text/transcribe` | `agent:run` | `SpeechToTextTranscriptionResponse` |
| `POST` | `/api/uploads` | `agent:run` | `AttachmentUploadResponse` |
| `GET` | `/health` | `unauthenticated` | `stream/file/none` |
