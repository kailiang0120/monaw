CREATE TABLE snapshot_ref_gc (
    snapshot_ref TEXT NOT NULL PRIMARY KEY,
    state TEXT NOT NULL CHECK (state IN ('pending', 'reclaimed')),
    created_at TEXT NOT NULL,
    reclaimed_at TEXT
);

CREATE INDEX idx_snapshot_ref_gc_state_created
ON snapshot_ref_gc(state, created_at);

CREATE TRIGGER trg_run_operations_snapshot_gc_insert
BEFORE INSERT ON run_operations
WHEN NEW.snapshot_ref IS NOT NULL
  AND EXISTS (
      SELECT 1 FROM snapshot_ref_gc WHERE snapshot_ref = NEW.snapshot_ref
  )
BEGIN
    SELECT RAISE(ABORT, 'snapshot reference is being reclaimed');
END;

CREATE TRIGGER trg_run_operations_snapshot_gc_update
BEFORE UPDATE OF snapshot_ref ON run_operations
WHEN NEW.snapshot_ref IS NOT NULL
  AND EXISTS (
      SELECT 1 FROM snapshot_ref_gc WHERE snapshot_ref = NEW.snapshot_ref
  )
BEGIN
    SELECT RAISE(ABORT, 'snapshot reference is being reclaimed');
END;
