-- Wolfpaw v2 — step 25 (skills auto-emission).
--
-- The Skill Distiller is a new agent slot; surface it on the
-- ``agent_kind`` enum so its ``token_usage`` + LangSmith traces
-- attribute to a meaningful name rather than impersonating one of the
-- existing agents.

ALTER TYPE agent_kind ADD VALUE IF NOT EXISTS 'skill_distiller';
