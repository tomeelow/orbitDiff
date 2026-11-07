"""Alembic environment: migrations run against ORBITDIFF_DATABASE_URL."""

from alembic import context
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool

from orbitdiff.config import settings
from orbitdiff.db import Base

# PostGIS owns these; they are never ours to migrate.
POSTGIS_TABLES = {"spatial_ref_sys", "topology", "layer"}


def include_object(obj, name, type_, reflected, compare_to) -> bool:  # noqa: ANN001
    return not (type_ == "table" and name in POSTGIS_TABLES)


url = context.config.attributes.get("url") or settings().database_url
if context.is_offline_mode():
    context.configure(url=url, target_metadata=Base.metadata, include_object=include_object)
    with context.begin_transaction():
        context.run_migrations()
else:
    with create_engine(url, poolclass=NullPool).connect() as connection:
        context.configure(
            connection=connection, target_metadata=Base.metadata, include_object=include_object
        )
        with context.begin_transaction():
            context.run_migrations()
