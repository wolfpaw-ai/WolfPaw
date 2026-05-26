"""Background workers — long-running jobs invoked outside the request path.

Step 22 lands the first job (:mod:`wolfpaw.workers.jobs.compact_thread`)
as fire-and-forget ``asyncio.create_task`` triggered from
``conv.append``. Step 23 adds the arq runtime that takes ownership of
these jobs so they survive process restarts and have backpressure.
Step 26 adds the first scheduled job — the weekly Sleep Cycle that
re-scores old plans, consolidates near-duplicate skills, and
garbage-collects orphan threads.
"""
