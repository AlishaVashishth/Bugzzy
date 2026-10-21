"""Database engine and session setup (SQLAlchemy 2.0 async + psycopg 3).

Neon (our PostgreSQL host) can pause its compute when idle. ``pool_pre_ping``
and ``pool_recycle`` make sure a stale connection is detected and replaced.

The ``DATABASE_URL`` value is treated as a secret: it is never logged and
never included in an error message.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from urllib.parse import urlsplit

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.config import get_settings

# How many connections the app keeps open. Small is fine for FastAPI.
POOL_SIZE = 5
MAX_OVERFLOW = 5

# Recycle connections after 5 minutes so Neon's idle suspension does not
# leave us holding dead connections.
POOL_RECYCLE_SECONDS = 300


def normalize_database_url(url: str) -> str:
    """Make sure the URL uses the psycopg 3 driver.

    Neon gives us ``postgresql://...`` but SQLAlchemy needs an explicit
    driver: ``postgresql+psycopg://...``. psycopg 3 understands the Neon
    query parameters (sslmode, channel_binding) as-is.
    """

    if url.startswith("postgres://"):
        return "postgresql+psycopg://" + url[len("postgres://") :]
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url[len("postgresql://") :]
    return url


def database_host(url: str) -> str:
    """Return just the host name (lowercase) without printing the URL."""

    try:
        return (urlsplit(normalize_database_url(url)).hostname or "").lower()
    except ValueError:
        return ""


def database_name(url: str) -> str:
    """Return just the database name (lowercase) without printing the URL."""

    try:
        return (urlsplit(normalize_database_url(url)).path or "").lstrip("/").lower()
    except ValueError:
        return ""


def _comparison_host(url: str) -> str:
    """Host name with Neon's "-pooler" marker removed for comparison.

    Neon serves the same database through a pooled host
    (``...-pooler...``) and a direct host (``...``). Treating them as the
    same host makes the test-database safety check stricter.
    """

    return database_host(url).replace("-pooler.", ".", 1)


def same_database_target(first: str, second: str) -> bool:
    """True when two URLs point at the same database.

    Used by the integration tests to refuse to run against the main
    database. Compares the full URL and then the host + database name.
    Never includes either URL in an error message.
    """

    if normalize_database_url(first) == normalize_database_url(second):
        return True

    first_host, second_host = _comparison_host(first), _comparison_host(second)
    first_name, second_name = database_name(first), database_name(second)
    return bool(
        first_host
        and first_host == second_host
        and first_name
        and first_name == second_name
    )


def _engine_kwargs(url: str, use_null_pool: bool) -> dict:
    """Settings shared by the app engine and the test engine."""

    kwargs: dict = {
        "pool_pre_ping": True,
        "pool_recycle": POOL_RECYCLE_SECONDS,
    }

    if use_null_pool:
        # Tests create short-lived connections and should not keep a pool.
        kwargs["poolclass"] = NullPool
    else:
        kwargs.update(
            pool_size=POOL_SIZE,
            max_overflow=MAX_OVERFLOW,
            pool_timeout=30,
        )

    # Neon's pooled ("-pooler") endpoint does not support prepared
    # statements, so psycopg must not prepare any.
    if "-pooler" in database_host(url):
        kwargs["connect_args"] = {"prepare_threshold": None}

    return kwargs


_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None



def get_engine() -> AsyncEngine:
    """Create (once) and return the app-wide engine targeting DATABASE_URL."""

    global _engine, _sessionmaker

    if _engine is None:
        url = get_settings().database_url
        if not url:
            raise RuntimeError("DATABASE_URL is not configured.")

        _engine = create_async_engine(
            normalize_database_url(url),
            **_engine_kwargs(url, use_null_pool=False),
        )
        _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False)

    return _engine



def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    """Return the app-wide session maker (creating the engine if needed)."""

    get_engine()
    assert _sessionmaker is not None
    return _sessionmaker


def build_test_engine(url: str) -> AsyncEngine:
    """Build a standalone engine for database tests (NullPool, no secrets)."""

    return create_async_engine(
        normalize_database_url(url), **_engine_kwargs(url, use_null_pool=True)
    )


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency that yields one database session per request."""

    maker = get_sessionmaker()
    async with maker() as session:
        yield session


async def dispose_engine() -> None:
    """Close the connection pool (called when the app shuts down)."""

    global _engine, _sessionmaker

    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _sessionmaker = None
