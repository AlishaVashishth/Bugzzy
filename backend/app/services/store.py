"""Storage for users, sessions, installations and selected repositories.

The app needs to remember things between requests. In production that data
lives in PostgreSQL. During unit tests (and local development without a
database) an in-memory version is used instead, so nothing needs a database.

Both versions implement the same ``Store`` interface, which is why the rest
of the code never has to care which one is active.

Two important security ideas are kept here:

* The raw session id is never stored. Only its SHA-256 hash is kept.
* A GitHub user access token is never passed to this module or stored.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from app.config import get_settings

logger = logging.getLogger(__name__)

# GitHub only uses these two account types in the installations API. The
# database has a CHECK constraint for them, so anything else is skipped
# (with a safe log line) instead of breaking the whole login.
VALID_ACCOUNT_TYPES = {"User", "Organization"}

# The database only allows these two repository-selection values.
VALID_REPOSITORY_SELECTIONS = {"all", "selected"}


@dataclass
class Session:
    """The server-side data for one login.

    Timestamps are kept as plain floats (seconds since the epoch) so the
    existing session tests can move them into the past to simulate expiry.
    """

    user_id: int          # local user id (NOT the GitHub id)
    csrf_token: str       # random token the frontend must echo back
    created_at: float     # when the session started (absolute lifetime)
    last_seen_at: float   # last time it was used (idle timeout)


class InstallationNotAvailableError(Exception):
    """Raised when a user tries to connect an installation they do not have."""


def _now() -> float:
    """Current time in seconds (kept in a function so tests can replace it)."""

    return time.time()


def _hash_session_id(raw_session_id: str) -> str:
    """Turn a raw session id into the hash we store as the dictionary key.

    The in-memory store uses a hex string for readability. The PostgreSQL
    store stores the raw 32-byte digest instead.
    """

    return hashlib.sha256(raw_session_id.encode("utf-8")).hexdigest()


def clean_installations(installations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep only installations the database can store safely.

    GitHub is external input. If it ever sends an unexpected account type or
    repository-selection value, we skip that one installation instead of
    failing the entire login. The log line never contains tokens.
    """

    cleaned: list[dict[str, Any]] = []

    for item in installations:
        if item.get("account_type") not in VALID_ACCOUNT_TYPES:
            logger.warning(
                "Skipping a GitHub installation with an unsupported account type."
            )
            continue

        if item.get("repository_selection") not in VALID_REPOSITORY_SELECTIONS:
            logger.warning(
                "Skipping a GitHub installation with an unsupported "
                "repository selection."
            )
            continue

        # These fields are required by the database.
        if (
            item.get("installation_id") is None
            or item.get("account_id") is None
            or not item.get("account_login")
        ):
            logger.warning(
                "Skipping a GitHub installation with missing required fields."
            )
            continue

        cleaned.append(dict(item))

    return cleaned


class Store(ABC):
    """Everything the app needs to save or load.

    Every method is async because the PostgreSQL version talks to a
    database. The in-memory version is async too, so callers do not need to
    know which one they are using.
    """

    # --- Users -----------------------------------------------------------
    @abstractmethod
    async def upsert_user(self, github_user: dict[str, Any]) -> dict[str, Any]:
        """Insert or update a user by GitHub id, then return the user."""

    @abstractmethod
    async def get_user_by_id(self, local_user_id: int) -> dict[str, Any] | None:
        """Find a user by their local Bugzzy id."""

    # --- GitHub App installations ---------------------------------------
    @abstractmethod
    async def set_user_installations(
        self, local_user_id: int, installations: list[dict[str, Any]]
    ) -> None:
        """Replace the user's installation list with a fresh one."""

    @abstractmethod
    async def get_user_installations(
        self, local_user_id: int
    ) -> list[dict[str, Any]]:
        """Return the user's trusted installation list."""

    @abstractmethod
    async def set_connected_installation(
        self, local_user_id: int, installation_id: int | None
    ) -> None:
        """Set (or clear, with None) the user's one connected installation."""

    @abstractmethod
    async def get_connected_installation(self, local_user_id: int) -> int | None:
        """Return the user's connected installation id, or None."""

    # --- Selected repositories ------------------------------------------
    @abstractmethod
    async def set_selected_repositories(
        self, local_user_id: int, installation_id: int, repo_ids: list[int]
    ) -> None:
        """Replace the selected repository ids for one installation."""

    @abstractmethod
    async def get_selected_repositories(
        self, local_user_id: int, installation_id: int
    ) -> list[int]:
        """Return the selected repository ids for one installation."""

    @abstractmethod
    async def clear_selected_repositories(
        self, local_user_id: int, installation_id: int
    ) -> None:
        """Forget the selected repository ids for one installation."""

    # --- Sessions --------------------------------------------------------
    @abstractmethod
    async def create_session(self, user_id: int) -> tuple[str, str]:
        """Create a session and return (raw_session_id, csrf_token)."""

    @abstractmethod
    async def get_session(self, raw_session_id: str | None) -> Session | None:
        """Look up a valid session, or return None."""

    @abstractmethod
    async def delete_session(self, raw_session_id: str | None) -> None:
        """Delete one session (used on logout)."""

    @abstractmethod
    async def delete_all_sessions_for_user(self, user_id: int) -> int:
        """Delete every session belonging to a user."""

    @abstractmethod
    async def purge_expired_sessions(self) -> int:
        """Delete expired sessions and return how many were removed."""


class InMemoryStore(Store):
    """A simple dictionary-based store, used by tests and local dev.

    Everything disappears when the server restarts. This is intentional:
    it is a fallback for when no DATABASE_URL is configured.
    """

    def __init__(self) -> None:
        # Keyed by GitHub user id so the same person maps to one record.
        self._users_by_github_id: dict[int, dict[str, Any]] = {}
        self._next_id = 1

        # Keyed by the LOCAL user id.
        self._installations_by_user_id: dict[int, list[dict[str, Any]]] = {}
        self._connected_by_user_id: dict[int, int] = {}

        # Keyed by (local user id, installation id).
        self._selected_repos: dict[tuple[int, int], list[int]] = {}

        # Keys are SHA-256 hex digests of raw session ids, never raw ids.
        # Tests look at this dictionary directly to simulate expiry.
        self._sessions: dict[str, Session] = {}

    # --- Users -----------------------------------------------------------
    async def upsert_user(self, github_user: dict[str, Any]) -> dict[str, Any]:
        github_user_id = github_user.get("id")

        existing = self._users_by_github_id.get(github_user_id)
        if existing is not None:
            # Refresh the profile fields but keep the local id.
            existing["username"] = github_user.get("login")
            existing["name"] = github_user.get("name")
            existing["avatar_url"] = github_user.get("avatar_url")
            return existing

        user = {
            "id": self._next_id,
            "github_user_id": github_user_id,
            "username": github_user.get("login"),
            "name": github_user.get("name"),
            "avatar_url": github_user.get("avatar_url"),
        }
        self._users_by_github_id[github_user_id] = user
        self._next_id += 1
        return user

    async def get_user_by_id(self, local_user_id: int) -> dict[str, Any] | None:
        for user in self._users_by_github_id.values():
            if user["id"] == local_user_id:
                return user
        return None

    # --- GitHub App installations ---------------------------------------
    async def set_user_installations(
        self, local_user_id: int, installations: list[dict[str, Any]]
    ) -> None:
        cleaned = clean_installations(installations)

        # Store copies so later changes to the caller's list do not leak in.
        self._installations_by_user_id[local_user_id] = [
            dict(item) for item in cleaned
        ]

        connected = self._connected_by_user_id.get(local_user_id)
        if connected is not None:
            still_present = any(
                item.get("installation_id") == connected
                for item in self._installations_by_user_id[local_user_id]
            )
            if not still_present:
                # The connection is gone, so its selections are meaningless.
                del self._connected_by_user_id[local_user_id]
                self._selected_repos.pop((local_user_id, connected), None)

    async def get_user_installations(
        self, local_user_id: int
    ) -> list[dict[str, Any]]:
        return [dict(item) for item in self._installations_by_user_id.get(local_user_id, [])]

    async def set_connected_installation(
        self, local_user_id: int, installation_id: int | None
    ) -> None:
        previous = self._connected_by_user_id.get(local_user_id)

        if installation_id is None:
            if previous is not None:
                self._selected_repos.pop((local_user_id, previous), None)
            self._connected_by_user_id.pop(local_user_id, None)
            return

        # Only an installation GitHub returned for this user may be connected.
        known = {
            item.get("installation_id")
            for item in self._installations_by_user_id.get(local_user_id, [])
        }
        if installation_id not in known:
            raise InstallationNotAvailableError(
                "That installation is not in this user's list."
            )

        if previous is not None and previous != installation_id:
            self._selected_repos.pop((local_user_id, previous), None)

        self._connected_by_user_id[local_user_id] = installation_id

    async def get_connected_installation(self, local_user_id: int) -> int | None:
        return self._connected_by_user_id.get(local_user_id)

    # --- Selected repositories ------------------------------------------
    async def set_selected_repositories(
        self, local_user_id: int, installation_id: int, repo_ids: list[int]
    ) -> None:
        # dict.fromkeys removes duplicates while keeping the original order.
        self._selected_repos[(local_user_id, installation_id)] = list(
            dict.fromkeys(repo_ids)
        )

    async def get_selected_repositories(
        self, local_user_id: int, installation_id: int
    ) -> list[int]:
        return list(self._selected_repos.get((local_user_id, installation_id), []))

    async def clear_selected_repositories(
        self, local_user_id: int, installation_id: int
    ) -> None:
        self._selected_repos.pop((local_user_id, installation_id), None)

    # --- Sessions --------------------------------------------------------
    async def create_session(self, user_id: int) -> tuple[str, str]:
        raw_session_id = secrets.token_urlsafe(32)
        session = Session(
            user_id=user_id,
            csrf_token=secrets.token_urlsafe(32),
            created_at=_now(),
            last_seen_at=_now(),
        )
        self._sessions[_hash_session_id(raw_session_id)] = session
        return raw_session_id, session.csrf_token

    async def get_session(self, raw_session_id: str | None) -> Session | None:
        if not raw_session_id:
            return None

        key = _hash_session_id(raw_session_id)
        session = self._sessions.get(key)
        if session is None:
            return None

        settings = get_settings()
        now = _now()

        # Absolute lifetime: cannot be older than the maximum age.
        if now - session.created_at > settings.session_max_age_seconds:
            del self._sessions[key]
            return None

        # Idle timeout: must have been used recently.
        if now - session.last_seen_at > settings.session_idle_timeout_seconds:
            del self._sessions[key]
            return None

        # Still valid: remember that it was just used.
        session.last_seen_at = now
        return session

    async def delete_session(self, raw_session_id: str | None) -> None:
        if not raw_session_id:
            return
        self._sessions.pop(_hash_session_id(raw_session_id), None)

    async def delete_all_sessions_for_user(self, user_id: int) -> int:
        keys = [
            key
            for key, session in self._sessions.items()
            if session.user_id == user_id
        ]
        for key in keys:
            del self._sessions[key]
        return len(keys)

    async def purge_expired_sessions(self) -> int:
        settings = get_settings()
        now = _now()
        keys = [
            key
            for key, session in self._sessions.items()
            if now - session.created_at > settings.session_max_age_seconds
            or now - session.last_seen_at > settings.session_idle_timeout_seconds
        ]
        for key in keys:
            del self._sessions[key]
        return len(keys)

    # --- Test helpers ----------------------------------------------------
    def clear(self) -> None:
        """Remove all sessions (kept for older tests)."""

        self._sessions.clear()

    def reset_all(self) -> None:
        """Clear every in-memory record. Used by tests."""

        self._users_by_github_id.clear()
        self._installations_by_user_id.clear()
        self._connected_by_user_id.clear()
        self._selected_repos.clear()
        self._sessions.clear()
        self._next_id = 1


# The store the running app uses. It is created on first use so that the
# choice can depend on settings (and so tests can replace it).
_active_store: Store | None = None


def build_store() -> Store:
    """Create the right store for the current configuration."""

    settings = get_settings()

    if not settings.database_url:
        # Development fallback: no database configured, so keep everything
        # in memory. This warning is safe (it contains no URL or secret).
        logger.warning("DATABASE_URL not set, using in-memory stores")
        return InMemoryStore()

    # Imported here so unit tests never need to load the database modules.
    from app.db.engine import get_sessionmaker
    from app.db.postgres_store import PostgresStore

    return PostgresStore(get_sessionmaker())


def get_store() -> Store:
    """Return the active store, creating it once if necessary."""

    global _active_store
    if _active_store is None:
        _active_store = build_store()
    return _active_store


def set_store(store: Store) -> None:
    """Replace the active store. Used by tests."""

    global _active_store
    _active_store = store
