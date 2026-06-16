-- Common read paths for startup/sidebar history, scheduled tasks, tool context, and memory views.
CREATE INDEX IF NOT EXISTS idx_conversations_updated
ON conversations(updated_at DESC, id);

CREATE INDEX IF NOT EXISTS idx_tool_calls_conversation
ON tool_calls(conversation_id, id);

CREATE INDEX IF NOT EXISTS idx_tool_outcomes_conversation
ON tool_outcomes(conversation_id, id DESC);

CREATE INDEX IF NOT EXISTS idx_plan_steps_conversation_status
ON plan_steps(conversation_id, status, id);

CREATE INDEX IF NOT EXISTS idx_scheduled_tasks_created
ON scheduled_tasks(created_at DESC, id);

CREATE INDEX IF NOT EXISTS idx_scheduled_task_runs_status
ON scheduled_task_runs(status, task_id);

CREATE INDEX IF NOT EXISTS idx_memory_candidates_status_updated
ON memory_candidates(status, updated_at DESC, id);

CREATE INDEX IF NOT EXISTS idx_memory_checkpoints_status_updated
ON memory_checkpoints(status, updated_at DESC, id);

CREATE INDEX IF NOT EXISTS idx_memory_episodes_updated
ON memory_episodes(updated_at DESC, id);
