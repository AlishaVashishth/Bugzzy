"""Server-side sessions for logged-in Bugzzy users.

A "session" is how the backend remembers that a browser is logged in.
When a user signs in we create a random session id, hand it to the browser
in an HttpOnly cookie, and keep the real session data on the server.

Security detail: we never store the raw session id. Instead we store the
SHA-256 *hash* of it and look sessions up by that hash. That way, even if
the stored data were ever leaked, the leaked values could not be used as
cookies. The raw id is also never logged.
"""

from __future__ import annotations

import hashlib
import secrets
import time
from dataclasses import dataclass

from app.config import get_settings

# TEMPORARY in-memory store; same interface will be backed by PostgreSQL or Redis later.


def _now() -> float:
    """Current time in seconds.

    Kept in its own function so tests can replace ("monkeypatch") the clock
    instead of waiting for real time to pass.
    """

    return time.time()


@dataclass
class Session:
    """The server-side data for one login."""

    user_id: int          # local user id (NOT the GitHub id)
    csrf_token: str       # random token the frontend must echo back
    created_at: float      # when the session started (absolute lifetime)
    last_seen_at: float    # last time it was used (idle timeout)


def _hash_session_id(raw_session_id: str) -> str:
    """Turn a raw session id into the hash we store as the dictionary key."""

    return hashlib.sha256(raw_session_id.encode("utf-8")).hexdigest()


class SessionStore:
    """An in-memory collection of sessions keyed by hashed session id."""

    def __init__(self) -> None:
        # Keys are SHA-256 hashes of raw session ids, never the raw ids.
        self._sessions: dict[str, Session] = {}

    def create(self, user_id: int) -> tuple[str, str]:
        """Create a new session and return (raw_session_id, csrf_token).

        The raw id is returned to the caller (to put in the cookie) but is
        only ever stored here as a hash.
        """

        raw_session_id = secrets.token_urlsafe(32)
        session = Session(
            user_id=user_id,
            csrf_token=secrets.token_urlsafe(32),
            created_at=_now(),
            last_seen_at=_now(),
        )
        self._sessions[_hash_session_id(raw_session_id)] = session
        return raw_session_id, session.csrf_token

    def get(self, raw_session_id: str | None) -> Session | None:
        """Look up a session, or return None if missing or expired.

        Expired sessions (too old, or idle too long) are deleted and
        treated as if they never existed. A valid session has its
        "last_seen_at" refreshed.
        """

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

    def delete(self, raw_session_id: str | None) -> None:
        """Delete one session (used on logout)."""

        if not raw_session_id:
            return
        self._sessions.pop(_hash_session_id(raw_session_id), None)

    def delete_all_for_user(self, user_id: int) -> int:
        """Delete every session belonging to a user. Returns how many were removed.

        Handy for "log out everywhere" later, and for cleaning up when a
        user is deleted.
        """

        keys = [
            key
            for key, session in self._sessions.items()
            if session.user_id == user_id
        ]
        for key in keys:
            del self._sessions[key]
        return len(keys)

    def purge_expired(self) -> int:
        """Remove all expired sessions. Returns how many were removed."""

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

    def clear(self) -> None:
        """Remove all sessions. Used by tests."""

        self._sessions.clear()


# The single store used by the running app. Tests can replace it with a
# fresh instance via set_session_store().
_session_store = SessionStore()


def get_session_store() -> SessionStore:
    """Return the app-wide session store."""

    return _session_store


def set_session_store(store: SessionStore) -> None:
    """Replace the app-wide session store (used by tests)."""

    global _session_store
    _session_store = store
