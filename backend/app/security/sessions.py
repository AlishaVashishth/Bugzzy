"""Session helpers.

The real session logic now lives in ``app.services.store``, because sessions
are stored in the same place as users (PostgreSQL, or in memory for tests).
This module keeps the small helpers that older code and tests import.

Security reminder: we never store or log the raw session id. Only its
SHA-256 hash is stored, and the raw value lives in the browser cookie.
"""

from __future__ import annotations

from app.services.store import InMemoryStore, Session, get_store, set_store

# Re-export the dataclass so existing imports keep working.
__all__ = ["Session", "get_session_store", "set_session_store"]


def get_session_store() -> InMemoryStore:
    """Return the active in-memory store.

    Some tests inspect session objects directly (for example to move their
    timestamps into the past). That only makes sense for the in-memory
    store, so we refuse to pretend with the PostgreSQL one.
    """

    store = get_store()
    if not isinstance(store, InMemoryStore):
        raise RuntimeError("The in-memory session store is not active.")
    return store


def set_session_store(store: InMemoryStore) -> None:
    """Replace the active store. Kept for older tests."""

    set_store(store)
