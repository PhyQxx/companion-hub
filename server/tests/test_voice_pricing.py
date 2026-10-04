"""Quotes belong to the concrete, currently authorized TTS provider."""

import gc
from decimal import Decimal
from pathlib import Path
from weakref import ref

import pytest
from test_voice_config import BASE_CONFIG

from app.config.models import VoiceTtsProviderConfig
from app.config.store import ConfigStore
from app.harness.budget import BudgetDenied
from app.voice.factory import ConfigVoiceSource


def test_tts_quote_is_explicit_and_requires_currency() -> None:
    assert VoiceTtsProviderConfig(provider="edge_tts").request_cost_ceiling is None
    with pytest.raises(ValueError, match="voice_cost_ceiling_requires_currency"):
        VoiceTtsProviderConfig(provider="edge_tts", request_cost_ceiling=Decimal(".001"))
    quote = VoiceTtsProviderConfig(
        provider="edge_tts", cost_currency="CNY", request_cost_ceiling=Decimal("0")
    )
    assert quote.request_cost_ceiling == 0


@pytest.mark.parametrize("change", ["disable", "remove", "price", "voice", "secret", "url"])
async def test_old_provider_stops_after_its_authority_changes(change: str, tmp_path: Path) -> None:
    path = tmp_path / "voice.yaml"
    payload = (
        BASE_CONFIG
        + """
voice:
  tts:
    - provider: mimo
      base_url: https://tts.example/v1
      secret_value: synthetic-credential-a
      voice: synthetic-voice-a
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
    provider = chain.providers[0]
    binding = source.binding_for(provider)
    assert binding is not None and binding.quote is not None
    assert binding.quote.pricing.rate_per_unit == Decimal(".002")
    source.validate_provider(provider)
    changes = {
        "disable": payload.replace("provider: mimo", "provider: mimo\n      enabled: false"),
        "remove": BASE_CONFIG + "\nvoice: {tts: []}\n",
        "price": payload.replace("'0.002'", "'0.003'"),
        "voice": payload.replace("synthetic-voice-a", "synthetic-voice-b"),
        "secret": payload.replace("synthetic-credential-a", "synthetic-credential-b"),
        "url": payload.replace("https://tts.example/v1", "https://other.example/v1"),
    }
    path.write_text(changes[change])
    await store.reload(force=True)
    with pytest.raises(BudgetDenied, match="voice_provider_configuration_changed"):
        source.validate_provider(provider)
    _, replacement = await source.resolve()
    assert source.binding_for(provider) is binding
    if replacement is not None:
        new = source.binding_for(replacement.providers[0])
        assert new is not None
        source.validate_provider(replacement.providers[0])
        # Credential authority is never used as a persisted cost identifier.
        assert (new.endpoint == binding.endpoint) is (change == "secret")


@pytest.mark.parametrize("field", ["base_url", "secret_value"])
async def test_shared_senseaudio_connection_changes_revoke_old_provider(
    field: str, tmp_path: Path
) -> None:
    path = tmp_path / "voice.yaml"
    payload = (
        BASE_CONFIG
        + """
voice:
  senseaudio:
    base_url: https://shared.example
    secret_value: synthetic-shared-a
  tts:
    - provider: senseaudio
      voice: synthetic-voice
"""
    )
    path.write_text(payload)
    store = ConfigStore(path)
    await store.load()
    source = ConfigVoiceSource(store)
    _, chain = await source.resolve()
    assert chain is not None
    provider = chain.providers[0]
    old = source.binding_for(provider)
    assert old is not None
    source.validate_provider(provider)
    payload = payload.replace(
        "https://shared.example" if field == "base_url" else "synthetic-shared-a",
        "https://other.example" if field == "base_url" else "synthetic-shared-b",
    )
    path.write_text(payload)
    await store.reload(force=True)
    with pytest.raises(BudgetDenied, match="voice_provider_configuration_changed"):
        source.validate_provider(provider)
    _, new_chain = await source.resolve()
    assert new_chain is not None
    new = source.binding_for(new_chain.providers[0])
    assert new is not None
    assert (new.endpoint == old.endpoint) is (field == "secret_value")


async def test_quote_metadata_skips_unbuilt_entries_and_does_not_keep_old_providers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SYNTHETIC_ABSENT_TTS_KEY", raising=False)
    path = tmp_path / "voice.yaml"
    payload = (
        BASE_CONFIG
        + """
voice:
  tts:
    - provider: mimo
      base_url: https://tts.example
      secret_ref: env:SYNTHETIC_ABSENT_TTS_KEY
      cost_currency: CNY
      request_cost_ceiling: '0.009'
    - provider: edge_tts
      voice: synthetic-a
      cost_currency: CNY
      request_cost_ceiling: '0.002'
"""
    )
    path.write_text(payload)
    store = ConfigStore(path)
    await store.load()
    source = ConfigVoiceSource(store)
    _, chain = await source.resolve()
    assert chain is not None and len(chain.providers) == 1
    provider = chain.providers[0]
    binding = source.binding_for(provider)
    assert binding is not None and binding.quote is not None
    assert binding.quote.pricing.rate_per_unit == Decimal(".002")
    weak = ref(provider)
    path.write_text(payload.replace("synthetic-a", "synthetic-b"))
    await store.reload(force=True)
    await source.resolve()
    assert source.binding_for(provider) is binding
    del provider, chain
    gc.collect()
    assert weak() is None and len(source._pricing) == 1
