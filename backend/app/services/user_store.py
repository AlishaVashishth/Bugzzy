"""Small compatibility helpers for the user store.

The real storage now lives in ``app.services.store`` (PostgreSQL in
production, in memory for tests and local development). Routes receive the
store through ``Depends(get_store)`` instead of importing functions here.

This module only keeps the helpers that tests still use.
"""

from __future__ import annotations

from app.services.store import InMemoryStore, get_store


def reset_store() -> None:
    """Clear every record in the active in-memory store.

    Unit tests call this to start from a clean slate. It is a no-op for the
    PostgreSQL store, which must never be wiped by tests.
    """

    store = get_store()
    if isinstance(store, InMemoryStore):
        store.reset_all()


def count_users() -> int:
    """Return how many users are in the in-memory store (tests only)."""

    store = get_store()
    if not isinstance(store, InMemoryStore):
        raise RuntimeError("The in-memory store is not active.")
    return len(store._users_by_github_id)
