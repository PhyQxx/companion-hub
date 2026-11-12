"""嵌入版本回填（方案 A2）：旧版本嵌入分批重算，收敛后换入目标 provider。

启动门控由 ``attach_embedding_provider`` 承担：探测到新 provider 且库内存在
版本落后行时不静默切换（旧实现会瞬间让整库向量通道失效且无任何报警），
而是交给 :class:`EmbeddingBackfillService` 按批重算，收敛后回调换入。
中断重启后重扫剩余行续跑，已更新行不会被重复计算；回填期间检索/入库照常
工作（新旧版本行按既有 version 隔离语义各自参与或不参与向量通道）。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections.abc import Callable

from .embeddings import EmbeddingProvider
from .store import MemoryStore

logger = logging.getLogger(__name__)


class EmbeddingBackfillService:
    """有界回填循环：每 tick 处理一批落后行，收敛（stale=0）后触发换入回调。

    停机安全：stop() 取消任务；已完成的批次以行级原子更新落库，重启后
    由 run_once 重扫剩余行继续，不重复计算。
    """

    def __init__(
        self,
        store: MemoryStore,
        provider: EmbeddingProvider,
        *,
        interval_seconds: float = 10.0,
        batch_size: int = 100,
        on_complete: Callable[[], None],
    ) -> None:
        self._store = store
        self._provider = provider
        self._interval_seconds = interval_seconds
        self._batch_size = batch_size
        self._on_complete = on_complete
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="aria-memory-reembed")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def run_once(self) -> bool:
        """执行一个 tick；返回是否已收敛并换入（True 后循环退出）。"""
        remaining = await self._store.count_embedding_mismatch(version=self._provider.version)
        self._store.embedding_stale_rows = remaining
        if remaining == 0:
            self._store.pending_embedding_version = None
            self._on_complete()
            logger.info("memory.embedding backfill complete target=%s", self._provider.version)
            return True
        updated = await self._store.reembed_stale_batch(
            provider=self._provider, limit=self._batch_size
        )
        logger.info(
            "memory.embedding backfill batch updated=%d remaining_before=%d target=%s",
            updated,
            remaining,
            self._provider.version,
        )
        return False

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self._interval_seconds)
            try:
                if await self.run_once():
                    return
            except asyncio.CancelledError:
                raise
            except Exception:
                # 回填失败不阻断服务；下个 tick 重试，剩余行重新统计。
                logger.exception("memory.embedding backfill tick failed")


async def attach_embedding_provider(
    store: MemoryStore,
    provider: EmbeddingProvider,
    *,
    backfill_enabled: bool | None = None,
    interval_seconds: float = 10.0,
    batch_size: int = 100,
) -> EmbeddingBackfillService | None:
    """换版门控：无落后行直接换入；有落后行推迟切换并返回回填服务。

    返回 None 表示无需回填（已直接换入），或回填被
    ``ARIA_MEMORY_EMBED_BACKFILL=0`` 关闭、切换保持推迟（维持旧 provider）。
    """
    if backfill_enabled is None:
        backfill_enabled = os.getenv("ARIA_MEMORY_EMBED_BACKFILL", "1") == "1"
    stale = await store.count_embedding_mismatch(version=provider.version)
    store.embedding_stale_rows = stale
    if stale == 0:
        store.set_embedding_provider(provider)
        logger.info(
            "semantic embedding enabled model=%s dimension=%d",
            provider.model_name,
            provider.dimension,
        )
        return None
    store.pending_embedding_version = provider.version
    if not backfill_enabled:
        logger.warning(
            "memory.embedding swap deferred reason=backfill_disabled stale_rows=%d target=%s",
            stale,
            provider.version,
        )
        return None
    logger.warning(
        "memory.embedding swap deferred reason=stale_rows stale_rows=%d target=%s",
        stale,
        provider.version,
    )

    def complete() -> None:
        store.set_embedding_provider(provider)

    service = EmbeddingBackfillService(
        store,
        provider,
        interval_seconds=interval_seconds,
        batch_size=batch_size,
        on_complete=complete,
    )
    await service.start()
    return service
