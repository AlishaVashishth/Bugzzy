"""A tiny in-memory store for logged-in users.

TEMPORARY, will be replaced by the PostgreSQL implementation from the teammate.

Everything lives in a dictionary, so it disappears when the server
restarts. It exists only so Step 2 can prove the login flow works end to
end; it is not meant for production.
"""

from __future__ import annotations

from typing import Any

# Keyed by the GitHub user id, so the same person logging in twice maps to
# exactly one record (an "upsert").
_USERS_BY_GITHUB_ID: dict[int, dict[str, Any]] = {}

# Simple counter used to hand out local ids, starting at 1.
_next_id = 1


def upsert_user(github_user: dict[str, Any]) -> dict[str, Any]:
    """Insert or update a user and return the stored record.

    If we have seen this GitHub user before, their record is refreshed
    (for example a new display name) instead of creating a duplicate.
    """

    global _next_id

    github_user_id = github_user.get("id")

    existing = _USERS_BY_GITHUB_ID.get(github_user_id)
    if existing is not None:
        # Refresh the mutable profile fields but keep the local id.
        existing["username"] = github_user.get("login")
        existing["name"] = github_user.get("name")
        existing["avatar_url"] = github_user.get("avatar_url")
        return existing

    user = {
        "id": _next_id,
        "github_user_id": github_user_id,
        "username": github_user.get("login"),
        "name": github_user.get("name"),
        "avatar_url": github_user.get("avatar_url"),
    }
    _USERS_BY_GITHUB_ID[github_user_id] = user
    _next_id += 1
    return user


def reset_store() -> None:
    """Clear all users. Used by tests to start from a clean slate."""

    global _next_id
    _USERS_BY_GITHUB_ID.clear()
    _next_id = 1


def get_user_by_id(local_id: int) -> dict[str, Any] | None:
    """Find a user by their local Bugzzy id (not the GitHub id).

    Returns None when no such user exists, for example after a restart
    wiped the temporary store.
    """

    for user in _USERS_BY_GITHUB_ID.values():
        if user["id"] == local_id:
            return user
    return None
