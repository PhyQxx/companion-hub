"""Public cognition exports; implementations load only when requested."""

from typing import TYPE_CHECKING, Any

from app._exports import resolve_export

if TYPE_CHECKING:
    from .action import ActionEngine
    from .action_plan import (
        ActionInvocation,
        ActionPlanService,
        ActionPlanStatus,
        ActionPlanView,
        ActionRunResult,
        ActionStepStatus,
        ActionStepView,
        ActionVerificationStatus,
        PlanExecutionEvent,
    )
    from .action_registry import (
        ActionDefinition,
        ActionRegistry,
        ActionRisk,
        CompiledAction,
        ConfirmationPolicy,
        VerificationPolicy,
        build_builtin_action_registry,
        render_action_catalog,
    )
    from .action_runner import ToolActionRunner
    from .attention import ATTENTION_POLICY_VERSION, AttentionEngine
    from .commitments import CommitmentTracker
    from .cycle import CognitiveCycle
    from .deliberation import COGNITIVE_POLICY_VERSION, RouterDeliberator, RuleBasedDeliberator
    from .goal_tracker import GoalTracker
    from .models import (
        ActionLevel,
        ActionOutcome,
        ActionPlan,
        ActionResult,
        AttentionResult,
        ClaimedGoalReminder,
        CognitiveDecision,
        CognitiveDecisionView,
        DecisionKind,
        FeedbackKind,
        GoalKind,
        GoalStatus,
        GoalView,
        ReflectionCandidate,
        SemanticEvent,
        Urgency,
        WorldState,
    )
    from .propose import PlanCompletionReporter, ProposeActionTool
    from .reflection import ReflectionEngine
    from .store import CognitiveStore
    from .structured import StructuredDeliberator
    from .world import WorldStateBuilder

_EXPORTS = {
    "ATTENTION_POLICY_VERSION": ("app.cognition.attention", "ATTENTION_POLICY_VERSION"),
    "COGNITIVE_POLICY_VERSION": ("app.cognition.ports", "COGNITIVE_POLICY_VERSION"),
    "ActionDefinition": ("app.cognition.action_registry", "ActionDefinition"),
    "ActionEngine": ("app.cognition.action", "ActionEngine"),
    "ActionInvocation": ("app.cognition.action_plan", "ActionInvocation"),
    "ActionLevel": ("app.cognition.models", "ActionLevel"),
    "ActionOutcome": ("app.cognition.models", "ActionOutcome"),
    "ActionPlan": ("app.cognition.models", "ActionPlan"),
    "ActionPlanService": ("app.cognition.action_plan", "ActionPlanService"),
    "ActionPlanStatus": ("app.cognition.action_plan", "ActionPlanStatus"),
    "ActionPlanView": ("app.cognition.action_plan", "ActionPlanView"),
    "ActionRegistry": ("app.cognition.action_registry", "ActionRegistry"),
    "ActionResult": ("app.cognition.models", "ActionResult"),
    "ActionRisk": ("app.cognition.action_registry", "ActionRisk"),
    "ActionRunResult": ("app.cognition.action_plan", "ActionRunResult"),
    "ActionStepStatus": ("app.cognition.action_plan", "ActionStepStatus"),
    "ActionStepView": ("app.cognition.action_plan", "ActionStepView"),
    "ActionVerificationStatus": ("app.cognition.action_plan", "ActionVerificationStatus"),
    "AttentionEngine": ("app.cognition.attention", "AttentionEngine"),
    "AttentionResult": ("app.cognition.models", "AttentionResult"),
    "ClaimedGoalReminder": ("app.cognition.models", "ClaimedGoalReminder"),
    "CognitiveCycle": ("app.cognition.cycle", "CognitiveCycle"),
    "CognitiveDecision": ("app.cognition.models", "CognitiveDecision"),
    "CognitiveDecisionView": ("app.cognition.models", "CognitiveDecisionView"),
    "CognitiveStore": ("app.cognition.store", "CognitiveStore"),
    "CommitmentTracker": ("app.cognition.commitments", "CommitmentTracker"),
    "CompiledAction": ("app.cognition.action_registry", "CompiledAction"),
    "ConfirmationPolicy": ("app.cognition.action_registry", "ConfirmationPolicy"),
    "DecisionKind": ("app.cognition.models", "DecisionKind"),
    "FeedbackKind": ("app.cognition.models", "FeedbackKind"),
    "GoalKind": ("app.cognition.models", "GoalKind"),
    "GoalStatus": ("app.cognition.models", "GoalStatus"),
    "GoalTracker": ("app.cognition.goal_tracker", "GoalTracker"),
    "GoalView": ("app.cognition.models", "GoalView"),
    "PlanCompletionReporter": ("app.cognition.propose", "PlanCompletionReporter"),
    "PlanExecutionEvent": ("app.cognition.action_plan", "PlanExecutionEvent"),
    "ProposeActionTool": ("app.cognition.propose", "ProposeActionTool"),
    "ReflectionCandidate": ("app.cognition.models", "ReflectionCandidate"),
    "ReflectionEngine": ("app.cognition.reflection", "ReflectionEngine"),
    "RouterDeliberator": ("app.cognition.deliberation", "RouterDeliberator"),
    "RuleBasedDeliberator": ("app.cognition.rule", "RuleBasedDeliberator"),
    "SemanticEvent": ("app.cognition.models", "SemanticEvent"),
    "StructuredDeliberator": ("app.cognition.structured", "StructuredDeliberator"),
    "ToolActionRunner": ("app.cognition.action_runner", "ToolActionRunner"),
    "Urgency": ("app.cognition.models", "Urgency"),
    "VerificationPolicy": ("app.cognition.action_registry", "VerificationPolicy"),
    "WorldState": ("app.cognition.models", "WorldState"),
    "WorldStateBuilder": ("app.cognition.world", "WorldStateBuilder"),
    "build_builtin_action_registry": (
        "app.cognition.action_registry",
        "build_builtin_action_registry",
    ),
    "render_action_catalog": ("app.cognition.action_registry", "render_action_catalog"),
}

__all__ = [
    "ATTENTION_POLICY_VERSION",
    "COGNITIVE_POLICY_VERSION",
    "ActionDefinition",
    "ActionEngine",
    "ActionInvocation",
    "ActionLevel",
    "ActionOutcome",
    "ActionPlan",
    "ActionPlanService",
    "ActionPlanStatus",
    "ActionPlanView",
    "ActionRegistry",
    "ActionResult",
    "ActionRisk",
    "ActionRunResult",
    "ActionStepStatus",
    "ActionStepView",
    "ActionVerificationStatus",
    "AttentionEngine",
    "AttentionResult",
    "ClaimedGoalReminder",
    "CognitiveCycle",
    "CognitiveDecision",
    "CognitiveDecisionView",
    "CognitiveStore",
    "CommitmentTracker",
    "CompiledAction",
    "ConfirmationPolicy",
    "DecisionKind",
    "FeedbackKind",
    "GoalKind",
    "GoalStatus",
    "GoalTracker",
    "GoalView",
    "PlanCompletionReporter",
    "PlanExecutionEvent",
    "ProposeActionTool",
    "ReflectionCandidate",
    "ReflectionEngine",
    "RouterDeliberator",
    "RuleBasedDeliberator",
    "SemanticEvent",
    "StructuredDeliberator",
    "ToolActionRunner",
    "Urgency",
    "VerificationPolicy",
    "WorldState",
    "WorldStateBuilder",
    "build_builtin_action_registry",
    "render_action_catalog",
]


def __getattr__(name: str) -> Any:
    return resolve_export(__name__, globals(), _EXPORTS, name)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
