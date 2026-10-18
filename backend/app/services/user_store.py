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

# Per-user installation lists, keyed by the LOCAL user id. The list is the
# trusted set of installation ids GitHub returned for that user at login.
_installations_by_user_id: dict[int, list[dict[str, Any]]] = {}

# Which installation each user has chosen to "connect" in Bugzzy, keyed by
# local user id. A user must explicitly choose; we never auto-connect.
_connected_by_user_id: dict[int, int] = {}


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
    _installations_by_user_id.clear()
    _connected_by_user_id.clear()
    _next_id = 1


def set_user_installations(
    local_user_id: int, installations: list[dict[str, Any]]
) -> None:
    """Replace a user's stored installation list with a fresh one.

    This is called at login with the trusted list from GitHub. If the user
    had connected an installation that no longer appears in the new list,
    we clear the connection so they cannot keep using a stale id.

    Note: we deliberately do NOT auto-connect when there is exactly one
    installation. The user must always choose.
    """

    # Store a copy so later changes to the caller's list do not leak in.
    _installations_by_user_id[local_user_id] = [dict(item) for item in installations]

    connected = _connected_by_user_id.get(local_user_id)
    if connected is not None:
        still_present = any(
            item.get("installation_id") == connected
            for item in _installations_by_user_id[local_user_id]
        )
        if not still_present:
            del _connected_by_user_id[local_user_id]


def get_user_installations(local_user_id: int) -> list[dict[str, Any]]:
    """Return a copy of the user's trusted installation list."""

    return [dict(item) for item in _installations_by_user_id.get(local_user_id, [])]


def set_connected_installation(
    local_user_id: int, installation_id: int | None
) -> None:
    """Mark which installation is connected for a user (None clears it)."""

    if installation_id is None:
        _connected_by_user_id.pop(local_user_id, None)
    else:
        _connected_by_user_id[local_user_id] = installation_id


def get_connected_installation(local_user_id: int) -> int | None:
    """Return the user's connected installation id, or None."""

    return _connected_by_user_id.get(local_user_id)


def get_user_by_id(local_id: int) -> dict[str, Any] | None:
    """Find a user by their local Bugzzy id (not the GitHub id).

    Returns None when no such user exists, for example after a restart
    wiped the temporary store.
    """

    for user in _USERS_BY_GITHUB_ID.values():
        if user["id"] == local_id:
            return user
    return None
