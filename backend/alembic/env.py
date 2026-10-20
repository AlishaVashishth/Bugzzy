"""Alembic environment configuration.

Reads DATABASE_URL from the environment (loaded from backend/.env)
and exposes ``target_metadata`` for autogenerate support.
"""
import os
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from dotenv import load_dotenv
from sqlalchemy import engine_from_config, pool

# ---------------------------------------------------------------------------
# 1. Make sure `app` package is importable when running from backend/
# ---------------------------------------------------------------------------
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# ---------------------------------------------------------------------------
# 2. Load environment variables from backend/.env
# ---------------------------------------------------------------------------
_env_path = Path(__file__).resolve().parents[1] / ".env"
load_dotenv(_env_path)

# ---------------------------------------------------------------------------
# 3. Alembic Config object (provides access to alembic.ini values)
# ---------------------------------------------------------------------------
config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# ---------------------------------------------------------------------------
# 4. Resolve the database URL
# ---------------------------------------------------------------------------
_raw_url = os.environ.get("DATABASE_URL")
if not _raw_url:
    raise RuntimeError(
        "DATABASE_URL is not set. "
        "Add it to backend/.env or export it before running Alembic."
    )

# Neon (and Heroku) sometimes use postgres:// which SQLAlchemy 2.x rejects.
# We also need the psycopg (v3) driver suffix.
if _raw_url.startswith("postgres://"):
    _raw_url = _raw_url.replace("postgres://", "postgresql+psycopg://", 1)
elif _raw_url.startswith("postgresql://"):
    _raw_url = _raw_url.replace("postgresql://", "postgresql+psycopg://", 1)

config.set_main_option("sqlalchemy.url", _raw_url)

# ---------------------------------------------------------------------------
# 5. Model metadata for autogenerate
# ---------------------------------------------------------------------------
from app.db.models import Base  # noqa: E402

target_metadata = Base.metadata

# ---------------------------------------------------------------------------
# 6. Migration runners
# ---------------------------------------------------------------------------

def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (emit SQL to stdout)."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode (connect to the database)."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
