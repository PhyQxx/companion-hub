from __future__ import annotations

from .models import AttentionResult, SemanticEvent, WorldState

ATTENTION_POLICY_VERSION = "attention-v1"


class AttentionEngine:
    def __init__(self, *, threshold: float = 0.55) -> None:
        self.threshold = threshold

    def evaluate(self, event: SemanticEvent, state: WorldState) -> AttentionResult:
        if event.passive:
            return AttentionResult(
                score=1,
                threshold=self.threshold,
                reason_codes=["passive_request"],
                should_deliberate=True,
            )
        critical = event.kind in {"water_leak", "safety.alarm", "water_leak_detected"}
        if state.dnd and not critical:
            return AttentionResult(
                score=0,
                threshold=self.threshold,
                reason_codes=["dnd"],
                should_deliberate=False,
            )
        base = {
            "water_leak": 1.0,
            "water_leak_detected": 1.0,
            "user_arrived_home": 0.7,
            "light_on_too_long": 0.62,
            "temperature_high": 0.6,
            "device_offline": 0.45,
        }.get(event.kind, 0.4)
        reasons = ["event_salience"]
        # 事件源可自带显著性（如屏幕感知的视觉判定 notable）：只升不降，与表值取大
        source_salience = event.attributes.get("salience")
        if (
            isinstance(source_salience, (int, float))
            and 0.0 <= float(source_salience) <= 1.0
            and float(source_salience) > base
        ):
            base = float(source_salience)
            reasons.append("source_salience")
        score = base * event.confidence
        if event.expires_at is not None:
            reasons.append("time_sensitive")
            score += 0.05
        if state.active_goals:
            reasons.append("active_goal_context")
            score += 0.05
        if state.same_trigger_recent_count:
            reasons.append("duplicate_penalty")
            score -= min(0.15 * state.same_trigger_recent_count, 0.45)
        if state.ignored_same_trigger_count:
            reasons.append("feedback_penalty")
            score -= min(0.1 * state.ignored_same_trigger_count, 0.3)
        score = max(0.0, min(score, 1.0))
        return AttentionResult(
            score=score,
            threshold=self.threshold,
            reason_codes=reasons,
            should_deliberate=critical or score >= self.threshold,
        )
