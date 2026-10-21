"""Shared pytest setup.

Unit tests must never touch a real database, even when DATABASE_URL happens
to be set in .env. This fixture replaces the active store with a fresh
in-memory one before every test, so the database code is never used.

The database integration tests in ``test_db_stores.py`` override this with
their own store, and they only run when TEST_DATABASE_URL is set.
"""

from __future__ import annotations

import asyncio
import sys
import pytest

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from app.services.store import InMemoryStore, set_store


@pytest.fixture(autouse=True)
def force_in_memory_store():
    """Give every test a clean in-memory store."""

    set_store(InMemoryStore())
    yield
    # Do not let a database-backed store leak into the next test.
    set_store(InMemoryStore())
