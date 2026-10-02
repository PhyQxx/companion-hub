"""Explicit mapping registration for Alembic and schema compatibility checks."""

from sqlalchemy import MetaData

from .base import Base


def registered_metadata() -> MetaData:
    # Import all mappings before returning metadata. Add domain mapping imports
    # here as they move out of models.py; never rely on incidental service imports.
    from . import costs, models  # noqa: F401

    return Base.metadata
