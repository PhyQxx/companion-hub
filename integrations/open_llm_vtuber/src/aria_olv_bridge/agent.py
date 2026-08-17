from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

try:
    # Open-LLM-VTuber's documented source launch imports the package through `src`.
    from src.open_llm_vtuber.agent.agents.agent_interface import AgentInterface
    from src.open_llm_vtuber.agent.input_types import BatchInput, TextSource
    from src.open_llm_vtuber.agent.output_types import Actions, DisplayText, SentenceOutput
    from src.open_llm_vtuber.agent.transformers import (
        actions_extractor,
        display_processor,
        sentence_divider,
        tts_filter,
    )
except ModuleNotFoundError:
    # Keep compatibility with environments that install open_llm_vtuber as a package.
    from open_llm_vtuber.agent.agents.agent_interface import AgentInterface
    from open_llm_vtuber.agent.input_types import BatchInput, TextSource
    from open_llm_vtuber.agent.output_types import Actions, DisplayText, SentenceOutput
    from open_llm_vtuber.agent.transformers import (
        actions_extractor,
        display_processor,
        sentence_divider,
        tts_filter,
    )

from .client import AriaBridgeClient, AriaBridgeConfig


class AriaAgent(AgentInterface):
    """Open-LLM-VTuber Agent whose conversation engine is Aria."""

    manages_history = True

    def __init__(
        self,
        live2d_model=None,
        tts_preprocessor_config=None,
        *,
        base_url: str = "http://127.0.0.1:8000",
        password_env: str = "ARIA_CHAT_PASSWORD",
        privacy_level: str = "L1",
        conversation_title: str = "Open-LLM-VTuber",
        mapping_path: str = ".aria/open_llm_vtuber_conversations.json",
        faster_first_response: bool = True,
        segment_method: str = "pysbd",
        structured_reply: bool = True,
    ) -> None:
        super().__init__()
        self._client = AriaBridgeClient(
            AriaBridgeConfig(
                base_url=base_url,
                password_env=password_env,
                privacy_level=privacy_level,
                conversation_title=conversation_title,
                mapping_path=Path(mapping_path),
            )
        )
        self._live2d_model = live2d_model
        self._structured_reply = structured_reply
        if not structured_reply:
            self.chat = tts_filter(tts_preprocessor_config)(
                display_processor()(
                    actions_extractor(live2d_model)(
                        sentence_divider(
                            faster_first_response=faster_first_response,
                            segment_method=segment_method,
                            valid_tags=["think"],
                        )(self.chat)
                    )
                )
            )

    def set_memory_from_history(self, conf_uid: str, history_uid: str) -> None:
        self._client.select_history(conf_uid, history_uid)

    def handle_interrupt(self, heard_response: str) -> None:
        del heard_response
        self._client.request_interrupt()

    async def close(self) -> None:
        await self._client.close()

    async def chat(self, input_data: BatchInput) -> AsyncIterator[str | SentenceOutput]:
        prompt = self._to_text_prompt(input_data)
        if not self._structured_reply:
            async for delta in self._client.stream_message(prompt):
                yield delta
            return

        chunks: list[str] = []
        async for delta in self._client.stream_message(prompt):
            chunks.append(delta)
        text = "".join(chunks)
        reply = self._client.last_agent_reply or {}
        display_text = str(reply.get("text") or text)
        tts_text = str(reply.get("tts_text") or display_text)
        expressions = self._expression_values(reply.get("expressions"))
        pictures: list[str] = []
        sounds: list[str] = []
        actions = reply.get("actions", [])
        if isinstance(actions, list):
            for action in actions:
                if not isinstance(action, dict) or not isinstance(action.get("value"), str):
                    continue
                if action.get("type") == "picture":
                    pictures.append(action["value"])
                elif action.get("type") == "sound":
                    sounds.append(action["value"])
                elif action.get("type") == "expression":
                    expressions.extend(self._expression_values([action["value"]]))
        yield SentenceOutput(
            display_text=DisplayText(text=display_text),
            tts_text=tts_text,
            actions=Actions(
                expressions=expressions or None,
                pictures=pictures or None,
                sounds=sounds or None,
            ),
        )

    def _expression_values(self, values) -> list[str | int]:
        if not isinstance(values, list):
            return []
        emotion_map = getattr(self._live2d_model, "emo_map", {}) or {}
        return [emotion_map.get(value, value) for value in values if isinstance(value, str)]

    @staticmethod
    def _to_text_prompt(input_data: BatchInput) -> str:
        parts: list[str] = []
        for text_data in input_data.texts:
            if text_data.source == TextSource.CLIPBOARD:
                parts.append(f"[Clipboard content: {text_data.content}]")
            else:
                parts.append(text_data.content)
        if input_data.images:
            parts.append("[Image input is not enabled in the Aria text bridge yet.]")
        if input_data.files:
            names = ", ".join(item.name for item in input_data.files)
            parts.append(f"[Attached files are not enabled in the Aria text bridge yet: {names}]")
        return "\n".join(part for part in parts if part.strip())
