ALTER TABLE turn_checkpoints
ADD COLUMN finalized INTEGER NOT NULL DEFAULT 1 CHECK (finalized IN (0, 1));
