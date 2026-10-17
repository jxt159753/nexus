CREATE TABLE node_identity (node_id TEXT PRIMARY KEY);
CREATE TABLE execution_receipts (
    attempt_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    command_digest TEXT NOT NULL,
    identity_json TEXT NOT NULL,
    boot_id TEXT NOT NULL,
    phase TEXT NOT NULL CHECK (phase IN ('received', 'started', 'completed', 'cancelled', 'unknown')),
    job_name TEXT,
    execution_stopped INTEGER NOT NULL DEFAULT 1 CHECK (execution_stopped IN (0, 1)),
    result_json TEXT CHECK (result_json IS NULL OR length(CAST(result_json AS BLOB)) <= 65536),
    acknowledged INTEGER NOT NULL DEFAULT 0 CHECK (acknowledged IN (0, 1)),
    CHECK ((phase = 'completed') = (result_json IS NOT NULL))
);
