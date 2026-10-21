"""Unit test: configuration must not change the test store.

Even when DATABASE_URL is present, unit tests keep using the in-memory
store. This test never connects to a database.
"""

from __future__ import annotations

import asyncio

import pytest

from app.config import get_settings
from app.db.engine import same_database_target
from app.services.store import InMemoryStore, get_store


def test_unit_tests_use_memory_even_when_database_url_is_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A fake URL is enough: we never connect, we only prove which store is
    # chosen. It is not a real database address.
    monkeypatch.setenv("DATABASE_URL", "postgresql://example.invalid/example")

    # Settings are cached, so clear the cache to re-read the environment.
    get_settings.cache_clear()
    try:
        # The setting was read from the environment ...
        assert get_settings().database_url is not None
        # ... but unit tests still use the in-memory store.
        assert isinstance(get_store(), InMemoryStore)
    finally:
        # Put the cache back the way the rest of the suite expects.
        get_settings.cache_clear()


def test_production_requires_a_database_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Production must fail fast instead of silently using memory."""

    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    get_settings.cache_clear()
    try:
        with pytest.raises(RuntimeError) as excinfo:
            get_settings()
        # The error names the variable but never shows a value.
        assert "DATABASE_URL" in str(excinfo.value)
    finally:
        get_settings.cache_clear()


def test_same_database_target_safety_check() -> None:
    """The test-database guard compares hosts and database names."""

    first = "postgresql://user:pass@example.invalid:5432/db"
    same = "postgres://user:pass@example.invalid:5432/db"
    other_name = "postgresql://user:pass@example.invalid:5432/other"
    other_host = "postgresql://user:pass@other.invalid:5432/db"
    pooled = "postgresql://user:pass@example-pooler.invalid:5432/db"

    assert same_database_target(first, same) is True
    assert same_database_target(first, other_name) is False
    assert same_database_target(first, other_host) is False
    assert same_database_target(first, pooled) is True


def test_in_memory_store_skips_unsupported_account_types() -> None:
    """GitHub sending an unexpected account type must not break login."""

    store = InMemoryStore()
    asyncio.run(
        store.set_user_installations(
            1,
            [
                {
                    "installation_id": 1,
                    "account_login": "ok",
                    "account_id": 1,
                    "account_type": "User",
                    "repository_selection": "all",
                },
                {
                    "installation_id": 2,
                    "account_login": "skip",
                    "account_id": 2,
                    "account_type": "Enterprise",
                    "repository_selection": "all",
                },
            ],
        )
    )

    rows = asyncio.run(store.get_user_installations(1))
    assert [row["installation_id"] for row in rows] == [1]
