"""One owned voice root, from recognition dispatch through final output."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import timedelta
from typing import TypeVar

from app.config import DatabaseConfigStore
from app.harness.budget import (
    BudgetDenied,
    budget_scope,
    current_budget,
    current_tool_budget,
    tool_budget_scope,
)
from app.harness.joined_read import join_on_cancel
from app.harness.operations import OperationPolicy
from app.harness.source_cleanup import close_after_source
from app.harness.voice_sources import VoiceSourceClaim
from app.ids import uuid7
from app.voice.contracts import SpeechRecognizer
from app.voice.delivery_ports import RecognitionRequest, VoiceTurnContext

from .operation import operate_with_run
from .parent_budget import current_parent_budget
from .speech_delivery import SqlSpeechDelivery, _Delivery, recover_expired_speech_deliveries

T = TypeVar("T")


class SqlVoiceTurnDelivery:
    def __init__(self, speech: SqlSpeechDelivery) -> None:
        self.speech = speech

    def validate_ephemeral(self) -> None:
        config = self.speech.config.current.config.run_budget
        if (
            current_budget() is not None
            or current_tool_budget() is not None
            or config.max_daily_cost is not None
            or config.max_monthly_cost is not None
        ):
            # A durable parent may retain a monetary cap absent from the
            # current global config. L3 cannot create its unit reservations or
            # safely replace that quota with an unmetered legacy invocation.
            raise BudgetDenied("ephemeral_operation_run_forbidden")

    async def start(self, source: VoiceSourceClaim) -> VoiceTurnContext:
        if source.privacy_level.value == "L3":
            raise BudgetDenied("ephemeral_operation_run_forbidden")
        parent = current_parent_budget(self.speech.database, source.user_id)
        await self.speech.source_guard.validate(source)
        await join_on_cancel(
            recover_expired_speech_deliveries(self.speech.database), name="voice-root-recover"
        )
        snapshot = (
            await self.speech.config.refresh()
            if isinstance(self.speech.config, DatabaseConfigStore)
            else self.speech.config.current
        )
        context = _VoiceTurn(
            self.speech,
            source,
            uuid7(),
            snapshot.version,
            snapshot.config.run_budget,
            parent,
            entry="voice.utterance",
            criterion="voice_reply_sent",
        )
        context.deadline = context.created_at + timedelta(
            seconds=context.config.interactive_deadline_seconds
        )
        try:
            await join_on_cancel(context.create(), name="voice-root-create")
            await context.validate()
            return context
        except BaseException as error:
            await context.finish(
                "cancelled" if isinstance(error, asyncio.CancelledError) else "failed",
                error.reason_code if isinstance(error, BudgetDenied) else "voice_root_start_failed",
            )
            raise


class _VoiceTurn(_Delivery):
    @contextmanager
    def bind(self) -> Iterator[None]:
        with (
            budget_scope(self.budget),
            tool_budget_scope(self.budget.tool_budget if self.budget else None),
        ):
            yield

    @property
    def requests(self) -> set[_RecognitionRequest]:
        if not hasattr(self, "_requests"):
            self._requests: set[_RecognitionRequest] = set()
        return self._requests

    @property
    def operations(self) -> set[asyncio.Task[object]]:
        if not hasattr(self, "_operations"):
            self._operations: set[asyncio.Task[object]] = set()
        return self._operations

    async def finish(self, status: str, reason: str) -> None:
        async def close() -> None:
            for request in tuple(self.requests):
                await request.aclose()
            owner = asyncio.current_task()
            others = [task for task in self.operations if task is not owner]
            for task in others:
                task.cancel()
            if others:
                await asyncio.gather(*others, return_exceptions=True)
            await super(_VoiceTurn, self).finish(status, reason)

        await join_on_cancel(close(), name="voice-root-close")

    async def _operate(
        self,
        provider: SpeechRecognizer,
        entry: str,
        invoke: Callable[[Callable[[], Awaitable[None]]], Awaitable[T]],
    ) -> T:
        self.provider = provider
        binding = self.port.pricing.binding_for(provider)
        quote = binding.quote if binding else None
        if quote is not None:
            self.currency = quote.pricing.currency
        await self.validate()
        policy = OperationPolicy(
            binding.config_version
            if binding and binding.config_version is not None
            else self.config_version,
            tuple(self.config.model_dump().items()),
            quote,
        )
        task = asyncio.current_task()
        assert task is not None
        self.operations.add(task)
        started = succeeded = False
        tool = self.budget.tool_budget if self.budget else None
        permit = None

        async def settle() -> None:
            if tool is not None and permit is not None:
                await join_on_cancel(
                    tool.settle_tool(
                        permit.call_id,
                        reported_ok=True if succeeded else None if started else False,
                    ),
                    name="voice-recognition-settle",
                )

        async def dispatch(mark: Callable[[], Awaitable[None]]) -> T:
            async def begin() -> None:
                nonlocal started
                await mark()
                started = True

            return await invoke(begin)

        try:
            with self.bind():
                async with close_after_source(settle):
                    if tool is not None:
                        permit = await tool.reserve_tool(
                            tool_name=entry, user_id=self.source.user_id
                        )
                    result = await operate_with_run(
                        self.port.database,
                        policy,
                        user_id=self.source.user_id,
                        privacy_level=self.source.privacy_level,
                        entry=entry,
                        invoke=dispatch,
                        evidence=lambda _: {"voice_turn_id": str(self.run_id)},
                        source_guard=self.validate,
                        cost_endpoint=binding.endpoint
                        if binding
                        else f"voice.asr.{type(provider).__name__}",
                        budget_source=lambda: self.port.config.current.config.run_budget,
                        cooperative=True,
                        trace_parent_id=self.run_id,
                        missing_quote_reason="voice_cost_estimate_unavailable",
                    )
                    succeeded = True
                    return result
        finally:
            self.operations.discard(task)

    async def transcribe(
        self, provider: SpeechRecognizer, pcm: bytes, *, sample_rate: int, language: str | None
    ) -> str:
        async def invoke(mark: Callable[[], Awaitable[None]]) -> str:
            await mark()
            call = provider.transcribe(pcm, sample_rate=sample_rate, language=language)
            if provider.runs_local:
                # Thread-backed local decoding cannot be physically cancelled.
                return await join_on_cancel(call, name="voice-local-transcribe")
            return await call

        return await self._operate(provider, "voice.asr", invoke)

    def recognition_request(
        self,
        provider: SpeechRecognizer,
        feed: Callable[[bytes], Awaitable[str | None]],
        finalize: Callable[[], Awaitable[str | None]],
    ) -> RecognitionRequest:
        request = _RecognitionRequest(self, provider, feed, finalize)
        self.requests.add(request)
        return request


@dataclass(slots=True)
class _Command:
    pcm: bytes | None
    result: asyncio.Future[str | None]


class _RecognitionRequest:
    def __init__(
        self,
        root: _VoiceTurn,
        provider: SpeechRecognizer,
        feed: Callable[[bytes], Awaitable[str | None]],
        finalize: Callable[[], Awaitable[str | None]],
    ) -> None:
        self.root, self.provider = root, provider
        self.decode_feed, self.decode_finalize = feed, finalize
        self.queue: asyncio.Queue[_Command] = asyncio.Queue(maxsize=1)
        self.task: asyncio.Task[str | None] | None = None
        self.closed = False
        self.finishing = False
        self.provider_error: BaseException | None = None
        self.command_lock = asyncio.Lock()

    async def _produce(self) -> str | None:
        async def invoke(mark: Callable[[], Awaitable[None]]) -> None:
            while True:
                command = await self.queue.get()
                try:
                    await mark()
                    result = (
                        await self.decode_feed(command.pcm)
                        if command.pcm is not None
                        else await self.decode_finalize()
                    )
                    if not command.result.done():
                        command.result.set_result(result)
                    if command.pcm is None:
                        self.finishing = True
                        return
                except BaseException as error:
                    self.finishing = True
                    self.provider_error = error
                    if not command.result.done():
                        command.result.set_exception(error)
                    raise

        try:
            await self.root._operate(self.provider, "voice.asr_stream", invoke)
        finally:
            self.root.requests.discard(self)
            while not self.queue.empty():
                self.queue.get_nowait().result.cancel()
        return None

    async def _command(self, pcm: bytes | None) -> str | None:
        async with self.command_lock:
            return await self._locked_command(pcm)

    async def _locked_command(self, pcm: bytes | None) -> str | None:
        if self.closed:
            raise BudgetDenied("voice_recognition_closed")
        if self.task is not None and self.task.done():
            await self.task
            raise BudgetDenied("voice_recognition_closed")
        if self.task is None:
            with self.root.bind():
                self.task = asyncio.create_task(self._produce(), name="voice-recognition-request")
            self.task.add_done_callback(
                lambda task: task.exception() if not task.cancelled() else None
            )
        future: asyncio.Future[str | None] = asyncio.get_running_loop().create_future()
        try:
            await self.queue.put(_Command(pcm, future))
            await asyncio.wait({future, self.task}, return_when=asyncio.FIRST_COMPLETED)
            if self.task.done():
                await self.task
            result = await future
            if pcm is None:
                await self.task  # Include provider/SQL/tool terminal cleanup.
                self.closed = True
            return result
        except asyncio.CancelledError:
            await self.aclose()
            raise
        except Exception:
            # A command result can precede the producer's SQL terminal write.
            # Join that write before ordinary ASR fallback is allowed.
            try:
                await join_on_cancel(self.task, name="voice-recognition-terminal")
            except Exception as terminal:
                if self.provider_error is not None and terminal is not self.provider_error:
                    raise BudgetDenied("voice_request_settlement_failed") from terminal
                raise
            raise
        finally:
            if not future.done():
                future.cancel()
            elif not future.cancelled():
                future.exception()

    async def feed(self, pcm: bytes) -> str | None:
        return await self._command(pcm)

    async def finalize(self) -> str | None:
        return await self._command(None)

    async def aclose(self) -> None:
        was_closed = self.closed
        self.closed = True

        async def close() -> None:
            if self.task is not None:
                if not was_closed and not self.finishing and not self.task.done():
                    self.task.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await self.task
            self.root.requests.discard(self)

        await join_on_cancel(close(), name="voice-recognition-close")
