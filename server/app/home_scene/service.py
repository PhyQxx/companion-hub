"""HOME-01 场景服务：条件判定 + 展开为待确认行动计划。

触发链路：Perception 管线事件（user_arrived_home 等）或用户手动运行 →
匹配启用场景 → 时段条件判定 → ActionPlanService.create_plan 展开为
计划（确认策略逐步裁决，A2 需用户确认后执行）。幂等键按事件去重：
scene:{id}:{event_id}——同一次感知事件绝不重复建计划。
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID

from app.cognition.action_plan import ActionInvocation, ActionPlanService
from app.cognition.action_registry import ActionRegistry
from app.home_scene.models import (
    MANUAL_TRIGGER,
    HomeSceneStep,
    HomeSceneTriggered,
    HomeSceneView,
    in_window,
)

from .store import HomeSceneStore


class HomeSceneService:
    def __init__(
        self,
        store: HomeSceneStore,
        plans: Callable[[], ActionPlanService] | ActionPlanService,
        registry: ActionRegistry,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store
        self._plans = plans
        self._registry = registry
        self._clock = clock or (lambda: datetime.now(UTC))
        self._event_locks: dict[UUID, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def create_scene(
        self,
        *,
        user_id: UUID,
        name: str,
        trigger: str,
        steps: list[HomeSceneStep],
        window_start: str | None = None,
        window_end: str | None = None,
    ) -> HomeSceneView:
        if not 1 <= len(steps) <= 10:
            raise ValueError("场景需要 1-10 个动作步骤")
        # 保存前逐动作编译校验：未知动作/参数漂移/A3 禁止在创建时即拒绝
        for step in steps:
            try:
                self._registry.compile(step.action_id, dict(step.arguments))
            except LookupError as error:
                raise ValueError(f"未知动作：{step.action_id}") from error
            except PermissionError as error:
                raise ValueError(f"动作被安全策略禁止：{step.action_id}") from error
        return await self._store.create_scene(
            user_id=user_id,
            name=name,
            trigger=trigger.strip(),
            steps=steps,
            window_start=window_start,
            window_end=window_end,
            now=self._clock(),
        )

    async def list_scenes(self, user_id: UUID) -> list[HomeSceneView]:
        return await self._store.list_scenes(user_id)

    async def get_scene(self, user_id: UUID, scene_id: UUID) -> HomeSceneView:
        return await self._store.get_scene_view(user_id, scene_id)

    async def delete_scene(self, user_id: UUID, scene_id: UUID) -> None:
        await self._store.delete_scene(user_id, scene_id)

    async def set_enabled(self, user_id: UUID, scene_id: UUID, *, enabled: bool) -> HomeSceneView:
        return await self._store.set_enabled(user_id, scene_id, enabled=enabled)

    async def handle_semantic_event(
        self, user_id: UUID, event_id: UUID, kind: str, *, cancel_arrivals: bool = True
    ) -> list[HomeSceneTriggered]:
        """Serialize arrival/leave handling per owner; no parallel expansion after cancellation."""
        async with self._event_locks[user_id]:
            return await self._handle_semantic_event(
                user_id, event_id, kind, cancel_arrivals=cancel_arrivals
            )

    async def _handle_semantic_event(
        self,
        user_id: UUID,
        event_id: UUID,
        kind: str,
        *,
        cancel_arrivals: bool,
    ) -> list[HomeSceneTriggered]:
        now = self._clock()
        if kind.strip() == "user_left_home" and cancel_arrivals:
            await self._cancel_arrival_plans(user_id)
        if kind.strip() == MANUAL_TRIGGER:
            # manual 场景只响应用户显式运行，感知事件永不触发
            return []
        scenes = await self._store.enabled_scenes_for_trigger(user_id, kind.strip())
        triggered: list[HomeSceneTriggered] = []
        for scene in scenes:
            if not in_window(now, scene.window_start, scene.window_end):
                continue
            plan = await self._expand(
                user_id,
                scene,
                idempotency_key=(
                    f"scene-arrival:{scene.id}:{event_id}"
                    if kind.strip() == "user_arrived_home"
                    else f"scene:{scene.id}:{event_id}"
                ),
            )
            if plan is not None:
                triggered.append(plan)
        return triggered

    async def on_departure(self, user_id: UUID, occurred_at: datetime) -> None:
        async with self._event_locks[user_id]:
            await self._cancel_arrival_plans(user_id, before=occurred_at)

    async def _cancel_arrival_plans(self, user_id: UUID, *, before: datetime | None = None) -> None:
        plans = self._plans() if callable(self._plans) else self._plans
        # Legacy keys remain supported; arrival prefixes survive scene deletion/restart.
        scenes = await self._store.list_scenes(user_id)
        arrival_ids = {str(scene.id) for scene in scenes if scene.trigger == "user_arrived_home"}
        cursor: UUID | None = None
        while True:
            page = await plans.list_plans(user_id=user_id, active_only=True, before_id=cursor)
            for plan in page:
                parts = plan.idempotency_key.split(":")
                arrival = parts[0] == "scene-arrival" or (
                    len(parts) == 3 and parts[0] == "scene" and parts[1] in arrival_ids
                )
                if arrival and (before is None or plan.created_at <= before):
                    try:
                        await plans.cancel_plan(
                            user_id=user_id, plan_id=plan.id, reason="user_left_home"
                        )
                    except ValueError:
                        # A concurrently completed plan no longer needs cancellation.
                        logging.getLogger(__name__).debug("arrival plan already terminal")
            if len(page) < 100:
                break
            cursor = page[-1].id

    async def run_manual(self, user_id: UUID, scene_id: UUID) -> HomeSceneTriggered | None:
        """用户显式运行场景；忽略时段条件（手动即明确意图），幂等键独立。"""
        scene = await self._store.get_scene_view(user_id, scene_id)
        if not scene.enabled:
            return None
        return await self._expand(
            user_id,
            scene,
            idempotency_key=f"scene:{scene.id}:manual:{self._clock().strftime('%Y%m%d%H%M%S')}",
        )

    async def _expand(
        self,
        user_id: UUID,
        scene: HomeSceneView,
        *,
        idempotency_key: str,
    ) -> HomeSceneTriggered | None:
        plans = self._plans() if callable(self._plans) else self._plans
        invocations = [
            ActionInvocation(action_id=step.action_id, arguments=step.arguments)
            for step in scene.steps
        ]
        plan = await plans.create_plan(
            user_id=user_id,
            title=f"场景：{scene.name}",
            invocations=invocations,
            idempotency_key=idempotency_key,
        )
        return HomeSceneTriggered(
            scene_id=scene.id,
            scene_name=scene.name,
            plan_id=plan.id,
            plan_status=plan.status,
            awaiting_confirmation=plan.status == "awaiting_confirmation",
        )

    async def active_scene_count(self, user_id: UUID) -> int:
        scenes = await self._store.list_scenes(user_id)
        return sum(1 for scene in scenes if scene.enabled and scene.trigger != MANUAL_TRIGGER)


__all__ = ["HomeSceneService"]
