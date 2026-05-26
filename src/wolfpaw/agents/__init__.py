"""Agent implementations.

Step 10: Quick Agent (Haiku + non-sandbox tools).
Step 11: Triage Agent + Router (composes triage → downstream handlers).
Step 12: Planning Agent (Sonnet/Opus + procedural + skills retrieval).
Step 13: Executor (runs a Plan, dispatches step kinds, parallel groups).
Step 14: Post-Evaluator (scores completed executions, updates procedural memory).
Step 24 (v2): Plan Pre-Evaluator (sanity-check between Planner and Executor).
Step 25 (v2): Skill Distiller (auto-emit skills from high-scoring plans).
Step 28 (v2): Tool Creator (propose, get user-approval, run user-tools).
"""

from wolfpaw.agents.executor import (
    ExecutorAgent,
    get_executor_agent,
    reset_executor_agent,
)
from wolfpaw.agents.tool_creator import (
    ToolCreatorAgent,
    ToolCreationOutcome,
    get_tool_creator_agent,
    reset_tool_creator_agent,
)
from wolfpaw.agents.plan_pre_evaluator import (
    PlanPreEvaluatorAgent,
    get_pre_evaluator_agent,
    plan_with_pre_evaluation,
    reset_pre_evaluator_agent,
)
from wolfpaw.agents.planner import (
    PlannerAgent,
    PlanContext,
    get_planner_agent,
    reset_planner_agent,
)
from wolfpaw.agents.post_evaluator import (
    PostEvaluatorAgent,
    get_post_evaluator_agent,
    reset_post_evaluator_agent,
)
from wolfpaw.agents.quick import QuickAgent
from wolfpaw.agents.router import Router, get_router, reset_router
from wolfpaw.agents.skill_distiller import (
    SkillDistillerAgent,
    get_skill_distiller_agent,
    maybe_distill_skill,
    reset_skill_distiller_agent,
)
from wolfpaw.agents.triage import (
    Route,
    Complexity,
    TriageAgent,
    TriageVerdict,
)

__all__ = [
    "Complexity",
    "ExecutorAgent",
    "PlanContext",
    "PlanPreEvaluatorAgent",
    "PlannerAgent",
    "PostEvaluatorAgent",
    "QuickAgent",
    "Route",
    "Router",
    "SkillDistillerAgent",
    "ToolCreationOutcome",
    "ToolCreatorAgent",
    "TriageAgent",
    "TriageVerdict",
    "get_executor_agent",
    "get_planner_agent",
    "get_post_evaluator_agent",
    "get_pre_evaluator_agent",
    "get_router",
    "get_skill_distiller_agent",
    "get_tool_creator_agent",
    "maybe_distill_skill",
    "plan_with_pre_evaluation",
    "reset_executor_agent",
    "reset_planner_agent",
    "reset_post_evaluator_agent",
    "reset_pre_evaluator_agent",
    "reset_router",
    "reset_skill_distiller_agent",
    "reset_tool_creator_agent",
]
