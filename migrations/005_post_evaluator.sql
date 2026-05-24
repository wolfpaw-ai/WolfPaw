-- Wolfpaw v1 — step 14 (Post-Evaluator).
--
-- task_events.task_id is NOT NULL with an FK to tasks(id), but the
-- tasks lifecycle doesn't land until step 15. The Post-Evaluator emits
-- task_events for plan-scored verdicts starting now, so we relax the
-- NOT NULL the same way step 8 did for sandboxes / compute_usage.
--
-- Once the Executor passes a real task_id through (step 15+), every
-- Post-Evaluator emission will populate it. Until then, plan_id is
-- carried in the row's `content` jsonb so we can still join back to
-- the originating plan.

ALTER TABLE task_events
    ALTER COLUMN task_id DROP NOT NULL;
