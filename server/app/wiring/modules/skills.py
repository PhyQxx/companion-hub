from __future__ import annotations

from dataclasses import dataclass

from app.config import ConfigStore, DatabaseConfigStore
from app.db import Database
from app.skills.connections import SkillConnectionStore, SkillHttpClient
from app.skills.credentials import SkillCredentialStore
from app.skills.generator import SkillDraftGenerator
from app.skills.runtime import SkillToolProvider
from app.skills.store import SkillStore


@dataclass(frozen=True, slots=True)
class SkillsModule:
    store: SkillStore | None
    connections: SkillConnectionStore | None
    credentials: SkillCredentialStore | None
    http_client: SkillHttpClient | None
    tools: SkillToolProvider | None
    generator: SkillDraftGenerator | None


def build_skills(
    database: Database | None,
    config: ConfigStore | DatabaseConfigStore | None,
) -> SkillsModule:
    store = SkillStore(database) if database is not None else None
    connections = SkillConnectionStore(database) if database is not None else None
    credentials = SkillCredentialStore(database) if database is not None else None
    client = (
        SkillHttpClient(connections, credentials=credentials) if connections is not None else None
    )
    return SkillsModule(
        store=store,
        connections=connections,
        credentials=credentials,
        http_client=client,
        tools=SkillToolProvider(store, connections=connections, http_client=client)
        if store is not None
        else None,
        generator=SkillDraftGenerator(config) if config is not None else None,
    )
