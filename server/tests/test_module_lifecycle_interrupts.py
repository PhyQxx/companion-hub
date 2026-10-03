"""Shutdown interruption preserves cancellation while closing every owned module."""

import asyncio

import pytest

from app.wiring.registry import ModuleRegistry, ModuleSpec


class Abort(BaseException):
    pass


@pytest.mark.parametrize("interruption", [asyncio.CancelledError("synthetic"), Abort("synthetic")])
async def test_shutdown_interrupt_closes_dependencies_and_is_not_repeated(
    interruption: BaseException,
) -> None:
    calls: list[str] = []

    async def clean() -> None:
        calls.append("base")

    async def interrupt() -> None:
        calls.append("interrupted")
        raise interruption

    registry = ModuleRegistry(
        [
            ModuleSpec("base", stop=clean),
            ModuleSpec("interrupted", requires=("base",), stop=interrupt),
        ]
    )
    await registry.start_all()
    with pytest.raises(type(interruption)) as caught:
        await registry.stop_all()
    assert caught.value is interruption
    assert calls == ["interrupted", "base"]
    assert set(registry.states.values()) == {"stopped"}
    assert registry.reason_codes["interrupted"] == "module_stop_failed"
    await registry.stop_all()
    assert calls == ["interrupted", "base"]


async def test_external_shutdown_cancellation_finishes_remaining_cleanup() -> None:
    started = asyncio.Event()
    calls: list[str] = []

    async def clean() -> None:
        calls.append("base")

    async def waiting() -> None:
        calls.append("waiting")
        started.set()
        await asyncio.Event().wait()

    registry = ModuleRegistry(
        [ModuleSpec("base", stop=clean), ModuleSpec("waiting", requires=("base",), stop=waiting)]
    )
    await registry.start_all()
    task = asyncio.create_task(registry.stop_all())
    try:
        await asyncio.wait_for(started.wait(), 3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert task.cancelled() and calls == ["waiting", "base"]
        assert set(registry.states.values()) == {"stopped"}
        await registry.stop_all()
        assert calls == ["waiting", "base"]
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_partial_start_cleanup_interruption_preserves_original_failure() -> None:
    original = RuntimeError("synthetic startup")
    calls: list[str] = []

    async def broken() -> None:
        raise original

    async def interrupt() -> None:
        calls.append("partial")
        raise asyncio.CancelledError("synthetic cleanup")

    async def clean() -> None:
        calls.append("base")

    registry = ModuleRegistry(
        [ModuleSpec("base", stop=clean), ModuleSpec("partial", start=broken, stop=interrupt)]
    )
    with pytest.raises(RuntimeError) as caught:
        await registry.start_all()
    assert caught.value is original and calls == ["partial", "base"]
    assert registry.reason_codes["partial"] == "module_start_cleanup_failed"
    assert registry.states["base"] == "stopped"


async def test_startup_rollback_cleanup_interrupt_preserves_original_failure() -> None:
    original = RuntimeError("synthetic startup")
    calls: list[str] = []

    async def broken() -> None:
        raise original

    async def interrupt() -> None:
        calls.append("interrupted")
        raise asyncio.CancelledError("synthetic cleanup")

    async def clean() -> None:
        calls.append("base")

    registry = ModuleRegistry(
        [
            ModuleSpec("base", stop=clean),
            ModuleSpec("interrupted", stop=interrupt),
            ModuleSpec("critical", start=broken),
        ]
    )
    with pytest.raises(RuntimeError) as caught:
        await registry.start_all()
    assert caught.value is original and calls == ["interrupted", "base"]
    assert registry.states["base"] == registry.states["interrupted"] == "stopped"


async def test_optional_partial_cleanup_interrupt_does_not_block_healthy_modules() -> None:
    calls: list[str] = []

    async def broken() -> None:
        raise RuntimeError("synthetic optional startup")

    async def interrupt() -> None:
        calls.append("optional.cleanup")
        raise asyncio.CancelledError("synthetic optional cleanup")

    async def healthy() -> None:
        calls.append("healthy.start")

    registry = ModuleRegistry(
        [
            ModuleSpec("optional", start=broken, stop=interrupt, critical=False),
            ModuleSpec("healthy", start=healthy),
        ]
    )
    await registry.start_all()
    assert calls == ["optional.cleanup", "healthy.start"]
    assert registry.states["optional"] == "degraded" and registry.states["healthy"] == "ready"
    assert registry.reason_codes["optional"] == "module_start_cleanup_failed"
    await registry.stop_all()


async def test_shutdown_interrupt_retains_other_failures_as_cause() -> None:
    original = asyncio.CancelledError("synthetic shutdown")
    failure = RuntimeError("synthetic stop failure")
    calls: list[str] = []

    async def clean() -> None:
        calls.append("base")

    async def broken() -> None:
        calls.append("broken")
        raise failure

    async def interrupt() -> None:
        calls.append("interrupted")
        raise original

    registry = ModuleRegistry(
        [
            ModuleSpec("base", stop=clean),
            ModuleSpec("broken", stop=broken),
            ModuleSpec("interrupted", stop=interrupt),
        ]
    )
    await registry.start_all()
    with pytest.raises(asyncio.CancelledError) as caught:
        await registry.stop_all()
    assert caught.value is original and calls == ["interrupted", "broken", "base"]
    assert isinstance(original.__cause__, BaseExceptionGroup)
    assert original.__cause__.exceptions == (failure,)
    assert set(registry.states.values()) == {"stopped"}


async def test_real_lifespan_cancellation_drains_before_closing_skill_client() -> None:
    from typing import cast

    from fastapi import FastAPI

    from app.chat import ChatService
    from app.skills.connections import SkillHttpClient
    from app.wiring.lifespan import LifespanDeps, build_lifespan

    calls: list[str] = []
    original = asyncio.CancelledError("synthetic conversation shutdown")

    class Conversation:
        async def recover_incomplete_turns(self) -> None:
            calls.append("recover")

        async def drain_background_work(self) -> None:
            calls.append("drain")
            raise original

    class Client:
        async def close(self) -> None:
            calls.append("close")

    app = FastAPI()
    deps = LifespanDeps(
        runtime_chat_service=cast(ChatService, Conversation()),
        skill_http_client=cast(SkillHttpClient, Client()),
    )
    with pytest.raises(asyncio.CancelledError) as caught:
        async with build_lifespan(deps)(app):
            assert calls == ["recover"]
    assert caught.value is original and calls == ["recover", "drain", "close"]
    assert set(app.state.module_registry.states.values()) == {"stopped"}
