from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from uuid import UUID
from weakref import WeakValueDictionary

from app.cognition.models import SemanticEvent
from app.cognition.ports import CognitiveCyclePort
from app.harness.budget import BudgetDenied
from app.harness.time import utc
from app.privacy.service import PolicyService
from app.schemas import PrivacyLevel

from .models import PerceptionDisposition, PerceptionResult
from .ports import EventAdmissionPort, EventAuditRepository, EventPolicy

ValidateEvent = Callable[[], bool | Awaitable[bool]]
HandleResult = Callable[[SemanticEvent, PerceptionResult], Awaitable[None]]
EventObserver = Callable[[SemanticEvent], Awaitable[None]]
logger = logging.getLogger(__name__)


class PerceptionPipeline:
    """Stable, expiring semantic-event ingress before cognitive deliberation."""

    def __init__(
        self,
        cycle: CognitiveCyclePort,
        store: EventAuditRepository,
        policy: EventPolicy,
        *,
        admission: EventAdmissionPort,
    ) -> None:
        self._cycle = cycle
        self._store = store
        self._policy = policy
        self._admission = admission
        self._tasks: dict[tuple[UUID, str], asyncio.Task[None]] = {}
        # Holders and waiters keep their lock alive. Idle source keys must not
        # accumulate permanently in a long-running observation process.
        self._locks: WeakValueDictionary[tuple[UUID, str], asyncio.Lock] = WeakValueDictionary()
        self._recent: dict[tuple[UUID, str], tuple[UUID, datetime]] = {}
        self._event_observer: EventObserver | None = None
        self._departure_observer: EventObserver | None = None

    def set_event_observer(self, observer: EventObserver | None) -> None:
        """注册语义事件观察者（如任务调度器的事件触发）；异常不外溢到主管线。"""
        self._event_observer = observer

    def set_departure_observer(self, observer: EventObserver) -> None:
        """Cancellation-only hook: DND must not keep an arrival plan running."""
        self._departure_observer = observer

    async def stop(self) -> None:
        tasks = tuple(self._tasks.values())
        self._tasks.clear()
        for task in tasks:
            task.cancel()
        for task in tasks:
            with suppress(asyncio.CancelledError):
                await task

    def submit(
        self,
        event: SemanticEvent,
        *,
        stable_for_seconds: float = 0,
        validate: ValidateEvent | None = None,
        handler: HandleResult | None = None,
    ) -> None:
        event = event.model_copy(deep=True)
        dedupe_key = self._dedupe_key(event)
        key = (event.user_id, dedupe_key)
        previous = self._tasks.pop(key, None)
        if previous is not None:
            previous.cancel()
        task = asyncio.create_task(
            self._wait_and_process(
                key,
                event,
                stable_for_seconds=stable_for_seconds,
                validate=validate,
                handler=handler,
            ),
            name=f"perception-{event.kind}-{event.event_id}",
        )
        self._tasks[key] = task

    async def process(self, event: SemanticEvent) -> PerceptionResult:
        event = event.model_copy(deep=True)
        key = (event.user_id, self._dedupe_key(event))
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            if event.privacy_level == PrivacyLevel.L3:
                result = await self._process_private(event)
            else:
                await self._admission.verify(event)
                if await self._store.get(event.event_id) is not None:
                    result = await self._process_locked(event, key=key)
                else:
                    result = await self._admission.execute(
                        event, lambda: self._process_locked(event, key=key)
                    )
        # Observers own their derived jobs/runs. Do not let background work inherit
        # the short-lived inline claim after its decision and audit have committed.
        if (
            self._departure_observer is not None
            and event.privacy_level != PrivacyLevel.L3
            and event.kind == "user_left_home"
            and result.disposition
            in {
                PerceptionDisposition.PROCESSED,
                PerceptionDisposition.MERGED,
                PerceptionDisposition.SUPPRESSED,
            }
            and result.reason_code != "event_already_processed"
            and (event.expires_at is None or utc(event.expires_at) > datetime.now(UTC))
        ):
            try:
                await self._departure_observer(event)
            except Exception:
                logger.exception("departure observer failed: %s", event.event_id)
        if (
            self._event_observer is not None
            and event.privacy_level != PrivacyLevel.L3
            and result.disposition == PerceptionDisposition.PROCESSED
            and (event.expires_at is None or utc(event.expires_at) > datetime.now(UTC))
        ):
            try:
                await self._event_observer(event)
            except Exception:
                logger.exception("event observer failed: %s", event.event_id)
        return result

    async def _process_private(self, event: SemanticEvent) -> PerceptionResult:
        # Ephemeral ingress must not rely on an adapter to remember to discard
        # writes, or seed ordinary dedupe with an unpersisted private event ID.
        now = datetime.now(UTC)
        if event.expires_at is not None and utc(event.expires_at) <= now:
            return PerceptionResult(
                event_id=event.event_id,
                disposition=PerceptionDisposition.EXPIRED,
                reason_code="event_expired",
            )
        rejection = await self._policy.reject_reason(event, now=now)
        if rejection is not None:
            return PerceptionResult(
                event_id=event.event_id,
                disposition=PerceptionDisposition.SUPPRESSED,
                reason_code=rejection,
                decision=await self._cycle.suppress(event, rejection),
            )
        return PerceptionResult(
            event_id=event.event_id,
            disposition=PerceptionDisposition.PROCESSED,
            decision=await self._cycle.evaluate(event),
        )

    async def _process_locked(
        self,
        event: SemanticEvent,
        *,
        key: tuple[UUID, str],
    ) -> PerceptionResult:
        now = datetime.now(UTC)
        dedupe_key = key[1]
        cutoff = now - timedelta(seconds=self._policy.settings.dedupe_window_seconds)
        self._recent = {
            recent_key: value for recent_key, value in self._recent.items() if value[1] >= cutoff
        }
        existing = await self._store.get(event.event_id)
        if existing is not None:
            if existing.user_id != event.user_id:
                raise BudgetDenied("event_source_owner_mismatch")
            if (
                existing.kind != event.kind
                or existing.source_kind != event.source_kind
                or existing.dedupe_key != dedupe_key
                or existing.privacy_level != str(event.privacy_level)
                or existing.evidence_ids != tuple(event.evidence_ids)
                or utc(existing.occurred_at) != utc(event.occurred_at)
                or (utc(existing.expires_at) if existing.expires_at is not None else None)
                != (utc(event.expires_at) if event.expires_at is not None else None)
            ):
                raise BudgetDenied("event_source_changed")
            return PerceptionResult(
                event_id=event.event_id,
                disposition=PerceptionDisposition.MERGED,
                reason_code="event_already_processed",
                merged_into_event_id=event.event_id,
            )
        memory_duplicate = self._recent.get(key)
        if memory_duplicate is not None:
            await self._store.record(
                event,
                dedupe_key=dedupe_key,
                disposition=PerceptionDisposition.MERGED,
                reason_code="cross_source_duplicate",
                merged_into_event_id=memory_duplicate[0],
                now=now,
            )
            return PerceptionResult(
                event_id=event.event_id,
                disposition=PerceptionDisposition.MERGED,
                reason_code="cross_source_duplicate",
                merged_into_event_id=memory_duplicate[0],
            )
        if event.expires_at is not None and utc(event.expires_at) <= now:
            await self._store.record(
                event,
                dedupe_key=dedupe_key,
                disposition=PerceptionDisposition.EXPIRED,
                reason_code="event_expired",
                now=now,
            )
            return PerceptionResult(
                event_id=event.event_id,
                disposition=PerceptionDisposition.EXPIRED,
                reason_code="event_expired",
            )
        duplicate = await self._store.recent_duplicate(
            event,
            dedupe_key=dedupe_key,
            now=now,
            window_seconds=self._policy.settings.dedupe_window_seconds,
        )
        if duplicate is not None:
            self._recent[key] = (duplicate.event_id, now)
            await self._store.record(
                event,
                dedupe_key=dedupe_key,
                disposition=PerceptionDisposition.MERGED,
                reason_code="cross_source_duplicate",
                merged_into_event_id=duplicate.event_id,
                now=now,
            )
            return PerceptionResult(
                event_id=event.event_id,
                disposition=PerceptionDisposition.MERGED,
                reason_code="cross_source_duplicate",
                merged_into_event_id=duplicate.event_id,
            )
        policy_decision = PolicyService.rejection(
            await self._policy.reject_reason(event, now=now), phase="delivery"
        )
        rejection = policy_decision.reason_code
        if rejection is not None:
            decision = await self._cycle.suppress(event, rejection)
            self._recent[key] = (event.event_id, now)
            await self._store.record(
                event,
                dedupe_key=dedupe_key,
                disposition=PerceptionDisposition.SUPPRESSED,
                reason_code=rejection,
                decision_id=decision.id,
                now=now,
            )
            return PerceptionResult(
                event_id=event.event_id,
                disposition=PerceptionDisposition.SUPPRESSED,
                reason_code=rejection,
                decision=decision,
            )
        decision = await self._cycle.evaluate(event)
        self._recent[key] = (event.event_id, now)
        await self._store.record(
            event,
            dedupe_key=dedupe_key,
            disposition=PerceptionDisposition.PROCESSED,
            reason_code=(decision.reason_codes[-1] if decision.reason_codes else None),
            decision_id=decision.id,
            now=now,
        )
        return PerceptionResult(
            event_id=event.event_id,
            disposition=PerceptionDisposition.PROCESSED,
            decision=decision,
        )

    async def _wait_and_process(
        self,
        key: tuple[UUID, str],
        event: SemanticEvent,
        *,
        stable_for_seconds: float,
        validate: ValidateEvent | None,
        handler: HandleResult | None,
    ) -> None:
        try:
            if stable_for_seconds > 0:
                await asyncio.sleep(stable_for_seconds)
            if validate is not None:
                valid = validate()
                if isinstance(valid, Awaitable):
                    valid = await valid
                if not valid:
                    now = datetime.now(UTC)
                    if event.privacy_level != PrivacyLevel.L3:
                        await self._store.record(
                            event,
                            dedupe_key=key[1],
                            disposition=PerceptionDisposition.UNSTABLE,
                            reason_code="stability_check_failed",
                            now=now,
                        )
                    result = PerceptionResult(
                        event_id=event.event_id,
                        disposition=PerceptionDisposition.UNSTABLE,
                        reason_code="stability_check_failed",
                    )
                else:
                    result = await self.process(event)
            else:
                result = await self.process(event)
            if handler is not None:
                await handler(event, result)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("perception event failed: %s", event.event_id)
        finally:
            if self._tasks.get(key) is asyncio.current_task():
                self._tasks.pop(key, None)

    @staticmethod
    def _dedupe_key(event: SemanticEvent) -> str:
        return event.dedupe_key or f"{event.kind}:{event.user_id}"
