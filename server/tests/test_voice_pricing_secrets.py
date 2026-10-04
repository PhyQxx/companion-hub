"""Cost identifiers must not be derived from connection credentials or prompts."""

from pathlib import Path

import pytest
from test_voice_config import BASE_CONFIG

from app.config.store import ConfigStore
from app.harness.budget import BudgetDenied
from app.voice.factory import ConfigVoiceSource


@pytest.mark.parametrize(
    "url",
    [
        "https://user:synthetic-a@tts.example/v1",
        "https://tts.example/v1?api_key=synthetic-a",
        "https://tts.example/v1/synthetic-a",
    ],
)
async def test_credential_url_changes_are_private_authority_only(url: str, tmp_path: Path) -> None:
    path = tmp_path / "voice.yaml"
    payload = (
        BASE_CONFIG
        + f"""
voice:
  tts:
    - provider: mimo
      base_url: '{url}'
      secret_value: synthetic-key
      cost_currency: CNY
      request_cost_ceiling: '0.002'
"""
    )
    path.write_text(payload)
    store = ConfigStore(path)
    await store.load()
    source = ConfigVoiceSource(store)
    _, chain = await source.resolve()
    assert chain is not None
    old = chain.providers[0]
    binding = source.binding_for(old)
    assert binding is not None
    path.write_text(payload.replace("synthetic-a", "synthetic-b"))
    await store.reload(force=True)
    with pytest.raises(BudgetDenied, match="voice_provider_configuration_changed"):
        source.validate_provider(old)
    _, replacement = await source.resolve()
    assert replacement is not None
    new = source.binding_for(replacement.providers[0])
    assert new is not None and new.endpoint == binding.endpoint
    assert new.authority_fingerprint != binding.authority_fingerprint


@pytest.mark.parametrize("field", ["initial_prompt", "hotwords"])
async def test_asr_prompt_is_excluded_from_cost_identifier(field: str, tmp_path: Path) -> None:
    path = tmp_path / "voice.yaml"
    payload = (
        BASE_CONFIG
        + f"""
voice:
  asr:
    provider: faster_whisper
    model: small
    runs_local: true
    {field}: synthetic private phrase a
    cost_currency: CNY
    request_cost_ceiling: '0.003'
"""
    )
    path.write_text(payload)
    store = ConfigStore(path)
    await store.load()
    source = ConfigVoiceSource(store)
    old, _ = await source.resolve()
    assert old is not None
    binding = source.binding_for(old)
    assert binding is not None
    path.write_text(payload.replace("phrase a", "phrase b"))
    await store.reload(force=True)
    with pytest.raises(BudgetDenied, match="voice_provider_configuration_changed"):
        source.validate_provider(old)
    replacement, _ = await source.resolve()
    assert replacement is not None
    new = source.binding_for(replacement)
    assert new is not None and new.endpoint == binding.endpoint


@pytest.mark.parametrize("field", ["base_url", "secret_value"])
async def test_shared_senseaudio_credentials_do_not_change_public_identifier(
    field: str, tmp_path: Path
) -> None:
    path = tmp_path / "voice.yaml"
    payload = (
        BASE_CONFIG
        + """
voice:
  senseaudio:
    base_url: https://user:synthetic-a@tts.example/v1
    secret_value: synthetic-a
  tts:
    - provider: senseaudio
      cost_currency: CNY
      request_cost_ceiling: '0.002'
"""
    )
    path.write_text(payload)
    store = ConfigStore(path)
    await store.load()
    source = ConfigVoiceSource(store)
    _, chain = await source.resolve()
    assert chain is not None
    old = chain.providers[0]
    binding = source.binding_for(old)
    assert binding is not None
    path.write_text(
        payload.replace(
            "user:synthetic-a" if field == "base_url" else "secret_value: synthetic-a",
            "user:synthetic-b" if field == "base_url" else "secret_value: synthetic-b",
        )
    )
    await store.reload(force=True)
    with pytest.raises(BudgetDenied, match="voice_provider_configuration_changed"):
        source.validate_provider(old)
    _, replacement = await source.resolve()
    assert replacement is not None
    new = source.binding_for(replacement.providers[0])
    assert new is not None and new.endpoint == binding.endpoint
    assert new.authority_fingerprint != binding.authority_fingerprint
