-- Memory v2 keeps global and workspace memory in markdown files only. The review
-- queue, episodes, checkpoints, and audit trail of the old design are retired.
DROP TABLE IF EXISTS memory_candidates;
DROP TABLE IF EXISTS memory_episodes;
DROP TABLE IF EXISTS memory_checkpoints;
DROP TABLE IF EXISTS memory_audit;
DROP TABLE IF EXISTS memory_audit_counters;
