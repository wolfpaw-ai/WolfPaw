-- Wolfpaw v1 — step 8 (sandbox).
--
-- Tasks lifecycle isn't built until step 15, but the sandbox + compute
-- metering ship in step 8 so dev work and the executor (step 13) can
-- exercise code execution before tasks exist. Relax the NOT NULL on
-- task_id so ad-hoc compute (NULL task_id) is representable; once tasks
-- are wired in, the executor always populates task_id and these columns
-- become effectively non-null in production traffic.

ALTER TABLE sandboxes
    ALTER COLUMN task_id DROP NOT NULL;

ALTER TABLE compute_usage
    ALTER COLUMN task_id DROP NOT NULL;
