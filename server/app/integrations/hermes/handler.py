"""deleg.hermes_task：把确认后的自然语言任务委派给 NAS 上的 hermes agent。

安全语义（docs/06 Hermes 集成 §7）：
- 提交边界失败（无 run_id）→ hermes_submit_unknown，不自动重试；
- 审批挂起 → 通知用户（注入的 notify 通道），超时由 manager stop；
- 协作式取消 → DelegCancelled，结果不汇报；
- 汇报 summary 只含 instruction 标题、状态与裁剪后的输出文本。
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID

from app.integrations.hermes.manager import HermesManager, HermesManagerError
from app.jobs.delegated import DelegCancelled, DelegHandlerError, DelegRunContext

logger = logging.getLogger("app.integrations.hermes")

DELEG_KIND_HERMES = "deleg.hermes_task"
SUMMARY_OUTPUT_CHARS = 1_500

NotifyApproval = Callable[[str, UUID, str], Awaitable[None]]
"""(run_id, user_id, instruction_preview) → 主动通知用户审批挂起。"""


class HermesTaskHandler:
    def __init__(
        self,
        manager: HermesManager,
        *,
        notify: NotifyApproval | None = None,
    ) -> None:
        self._manager = manager
        self._notify = notify

    def set_notify(self, notify: NotifyApproval) -> None:
        """main 装配后期注入主动通知通道（ProactiveDeliveryService 适配器）。"""
        self._notify = notify

    async def __call__(
        self, payload: dict[str, Any], run_context: DelegRunContext
    ) -> dict[str, Any]:
        user_id_raw = payload.get("user_id")
        instruction = str(payload.get("instruction") or "").strip()
        timeout_seconds = payload.get("timeout_seconds")
        if not isinstance(user_id_raw, str):
            raise DelegCancelled("hermes_payload_invalid")
        try:
            user_id = UUID(user_id_raw)
        except ValueError as error:
            raise DelegCancelled("hermes_payload_invalid") from error
        if not instruction:
            raise DelegCancelled("hermes_payload_invalid")

        config = await self._manager.aconfig()
        if not config.enabled:
            raise DelegHandlerError("hermes_disabled")
        if len(instruction) > config.instruction_max_chars:
            raise DelegHandlerError("hermes_instruction_too_long")

        try:
            run_id = await self._manager.submit_task(
                user_id,
                instruction,
                timeout_seconds=int(timeout_seconds) if timeout_seconds else None,
            )
        except HermesManagerError as error:
            raise DelegHandlerError(error.reason_code) from error

        async def notify_approval() -> None:
            if self._notify is None:
                return
            with contextlib.suppress(Exception):
                await self._notify(run_id, user_id, instruction[:200])

        outcome = await self._manager.watch_task(
            user_id,
            run_id,
            on_approval_pending=notify_approval,
            is_cancel_requested=run_context.is_cancel_requested,
        )
        if outcome.status == "cancelled":
            raise DelegCancelled("hermes_task")
        return _summarize(instruction, run_id, outcome)


def _summarize(instruction: str, run_id: str, outcome: Any) -> dict[str, Any]:
    status_text = {
        "completed": "已完成",
        "failed": "执行失败",
        "stopped": "已停止（超时）",
        "approval_timeout": "未完成：等待审批超时，已停止",
    }.get(outcome.status, outcome.status)
    output = outcome.output_text.strip()
    body = output[:SUMMARY_OUTPUT_CHARS]
    if len(output) > SUMMARY_OUTPUT_CHARS:
        body += f"…（结果共 {len(output)} 字符，已截断）"
    summary = f"【hermes 任务·{status_text}】{instruction[:120]}\n{body}".strip()
    return {
        "summary": summary,
        "run_id": run_id,
        "status": outcome.status,
        "waited_for_approval": outcome.waited_for_approval,
        "output_chars": len(outcome.output_text),
        "elapsed_seconds": round(outcome.finished_at - outcome.submitted_at, 1),
    }
