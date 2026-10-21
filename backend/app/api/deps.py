"""Reusable FastAPI dependencies for authentication and CSRF protection.

A "dependency" is a small function FastAPI runs before your route. If it
raises an HTTPException, the route never runs. We use:

* get_current_session -> the current session (or 401)
* get_current_user    -> the current user (or 401)
* verify_csrf         -> protects POST requests from cross-site forgery (403)
"""

from __future__ import annotations

import hmac

from fastapi import Depends, HTTPException, Request, status

from app.config import get_settings
from app.security.sessions import Session
from app.services.store import Store, get_store

# The same 401 body is used everywhere, so we never hint at whether a
# session, a user, or a cookie was the problem.
_NOT_AUTHENTICATED = "Not authenticated"

# A generic 403 body for CSRF problems.
_CSRF_FAILED = "CSRF check failed"


def get_raw_session_id(request: Request) -> str | None:
    """Read the raw session id from the request's cookies.

    The raw value stays in memory only for this request; it is never logged
    and never echoed back to the client.
    """

    settings = get_settings()
    return request.cookies.get(settings.session_cookie_name)


async def _load_session_or_401(
    request: Request, store: Store
) -> tuple[str, Session]:
    """Look up the current session, or raise 401.

    Returns the raw id (so callers can delete the session) and the Session.
    """

    raw_session_id = get_raw_session_id(request)
    session = await store.get_session(raw_session_id)
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=_NOT_AUTHENTICATED
        )
    return raw_session_id or "", session


async def get_current_session(
    request: Request, store: Store = Depends(get_store)
) -> Session:
    """Return the session for this request, or raise 401."""

    _, session = await _load_session_or_401(request, store)
    return session


async def get_current_user(
    request: Request, store: Store = Depends(get_store)
) -> dict:
    """Return the logged-in user, or raise 401.

    If the session is valid but its user no longer exists, we delete the
    stale session and treat the request as unauthenticated.
    """

    raw_session_id, session = await _load_session_or_401(request, store)

    user = await store.get_user_by_id(session.user_id)
    if user is None:
        # Remove the dangling session so it cannot linger.
        await store.delete_session(raw_session_id)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=_NOT_AUTHENTICATED
        )

    return user


async def verify_csrf(
    request: Request,
    session: Session = Depends(get_current_session),
) -> None:
    """Protect state-changing requests (POST, PUT, DELETE).

    Two checks must pass:

    1. If the request carries an "Origin" header, it must be exactly our
       frontend's origin. This blocks requests started by other websites.
    2. The "X-CSRF-Token" header must match the token stored in the session.
       Our own JavaScript can read that token from /auth/me, but a
       malicious site cannot, so it cannot forge this header.

    Any failure is returned as 403 "CSRF check failed".
    """

    settings = get_settings()

    # 1. Origin check. Browsers send Origin on cross-site requests and on
    #    non-GET requests; when it is absent we rely on the token check.
    #
    # In development only, we also accept this backend's own origin. That
    # lets you test POSTs from the browser console (e.g. on the API docs
    # page) without setting up the frontend. This is for development
    # testing only: in production the frontend origin is the only one
    # accepted.
    allowed_origins = {settings.frontend_origin}
    if not settings.is_production:
        allowed_origins.add(settings.backend_origin)

    origin = request.headers.get("origin")
    if origin is not None and origin not in allowed_origins:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=_CSRF_FAILED
        )

    # 2. Token check. hmac.compare_digest avoids leaking information through
    #    how long the comparison takes.
    token = request.headers.get("x-csrf-token")
    if not token or not hmac.compare_digest(token, session.csrf_token):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=_CSRF_FAILED
        )
