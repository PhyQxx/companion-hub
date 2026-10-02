"""Explicit mapping registration for Alembic and schema compatibility checks."""

from sqlalchemy import MetaData

from .base import Base


def registered_metadata() -> MetaData:
    # Keep registration explicit; services and compatibility exports are optional.
    from . import (  # noqa: F401
        actions,
        appearance,
        assets,
        calendar,
        cognition,
        configuration,
        conversation,
        costs,
        delivery,
        devices,
        events,
        home,
        identity,
        jobs,
        mail,
        meetings,
        memory,
        runs,
        skills,
        tasks,
        workflows,
    )

    return Base.metadata
