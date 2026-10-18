"""GitHub App authentication helpers.

A GitHub App proves who it is in two steps:

1. It signs a short-lived JWT (JSON Web Token) with its private key.
2. It sends that JWT to GitHub, and GitHub replies with app details
   (or, in later steps, an installation token).

This module covers those first two steps. Nothing here ever prints or
stores the private key or the JWT in a message, so failures stay safe.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import jwt

from app.config import get_settings

# GitHub's REST API base URL and the API version header GitHub currently
# asks clients to send.
GITHUB_API_BASE = "https://api.github.com"
GITHUB_API_VERSION = "2022-11-28"

# How long to wait for GitHub before giving up (in seconds).
REQUEST_TIMEOUT = 15.0

# Only reuse a cached installation token while it still has this much life
# left. Tokens last about an hour, so 5 minutes of headroom is plenty.
TOKEN_REFRESH_MARGIN_SECONDS = 5 * 60


class GitHubAuthError(Exception):
    """Raised when our credentials are wrong (bad key, bad JWT, HTTP 401)."""


class GitHubAPIError(Exception):
    """Raised when GitHub answers with an unexpected non-2xx status."""


class GitHubNetworkError(Exception):
    """Raised when we cannot reach GitHub (timeout or connection failure)."""


class InstallationUnavailableError(Exception):
    """Raised when an installation is gone, suspended, or no longer shared.

    GitHub answers 404 or 403 when we ask for a token for such an
    installation. Callers usually respond by asking the user to reconnect.
    """


def load_private_key() -> str:
    """Read the app's private key (PEM format) from disk.

    The path comes from settings, which already verified the file exists.
    We return the text but never log it.
    """

    settings = get_settings()
    key_path: Path = settings.private_key_path
    try:
        return key_path.read_text(encoding="utf-8")
    except OSError as exc:
        # Deliberately do not echo the file contents.
        raise GitHubAuthError(
            f"Could not read the private key file at {key_path}."
        ) from exc


def create_app_jwt() -> str:
    """Create the RS256 JWT GitHub expects from an App.

    Timing rules from GitHub:
      - "iat" (issued at) may be at most 60 seconds in the past.
      - "exp" (expiry) may be at most 10 minutes in the future.

    So we set iat = now - 60 and exp = now + 540 (9 minutes), staying
    comfortably inside both limits.
    """

    settings = get_settings()
    private_key = load_private_key()

    now = int(time.time())
    payload = {
        "iat": now - 60,   # not older than GitHub's 60-second allowance
        "exp": now + 540,  # expires in 9 minutes (under the 10-minute cap)
        # The App ID identifies the issuer. PyJWT requires "iss" to be a
        # string, so we convert the numeric App ID.
        "iss": str(settings.app_id),
    }

    try:
        # PyJWT signs the payload with our RSA key using the RS256 algorithm.
        return jwt.encode(payload, private_key, algorithm="RS256")
    except Exception as exc:  # noqa: BLE001 - convert any key/JWT failure
        # The exception text could theoretically contain key material, so we
        # raise a clean, generic message instead of passing it through.
        raise GitHubAuthError(
            "Failed to create the app JWT. Check that the private key is a "
            "valid RSA key for this GitHub App."
        ) from exc


async def get_app_info() -> dict[str, Any]:
    """Call GitHub's "GET /app" endpoint and return the parsed JSON.

    The request is authenticated with the App JWT as a Bearer token.
    """

    token = create_app_jwt()

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": GITHUB_API_VERSION,
        "User-Agent": "bugzzy-app",
    }

    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            response = await client.get(f"{GITHUB_API_BASE}/app", headers=headers)
    except httpx.TimeoutException as exc:
        raise GitHubNetworkError(
            "Timed out while contacting GitHub. Please try again."
        ) from exc
    except httpx.RequestError as exc:
        raise GitHubNetworkError(
            "Could not connect to GitHub. Please check your network."
        ) from exc

    # 401 means our JWT or key is not accepted by GitHub.
    if response.status_code == 401:
        raise GitHubAuthError(
            "GitHub rejected our credentials (401). "
            "Check the App ID and private key."
        )

    # Any other non-2xx status is a general API problem.
    if response.status_code >= 400:
        raise GitHubAPIError(
            f"GitHub returned an unexpected status: {response.status_code}."
        )

    return response.json()


# The app's URL slug never changes, so we remember it after the first
# successful lookup instead of calling GitHub on every request.
_app_slug_cache: str | None = None


async def get_app_slug() -> str:
    """Return the GitHub App's slug (used to build the install URL).

    The slug comes from get_app_info(). It is cached in memory after the
    first success. If the lookup fails, the caller decides how to map the
    exception (e.g. 502/503); we never invent a slug.
    """

    global _app_slug_cache

    if _app_slug_cache is not None:
        return _app_slug_cache

    data = await get_app_info()
    slug = data.get("slug")
    if not slug:
        raise GitHubAPIError("GitHub did not return an app slug.")

    _app_slug_cache = slug
    return slug


def reset_app_slug_cache() -> None:
    """Forget the cached slug. Used by tests."""

    global _app_slug_cache
    _app_slug_cache = None


# ---------------------------------------------------------------------------
# Installation access tokens
# ---------------------------------------------------------------------------
# An installation token lets us act on an installation's repositories. It
# lasts about an hour and must NEVER leave the server: it is never returned
# in a response and never logged.
#
# Cache shape: {installation_id: (token, expires_at_epoch_seconds)}.
_token_cache: dict[int, tuple[str, float]] = {}

# One lock per installation id so that, when several requests need a token
# at once, only one of them actually calls GitHub. The guard lock protects
# creating the per-installation locks themselves.
_token_locks: dict[int, asyncio.Lock] = {}
_token_locks_guard = asyncio.Lock()


def _parse_iso_to_epoch(value: str) -> float:
    """Turn GitHub's ISO 8601 "expires_at" into epoch seconds.

    If it cannot be parsed we fall back to "about an hour from now" so we
    still cache something sane.
    """

    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except (ValueError, AttributeError):
        return time.time() + 3600


async def _get_token_lock(installation_id: int) -> asyncio.Lock:
    """Return (creating if needed) the lock for one installation id."""

    lock = _token_locks.get(installation_id)
    if lock is None:
        async with _token_locks_guard:
            lock = _token_locks.get(installation_id)
            if lock is None:
                lock = asyncio.Lock()
                _token_locks[installation_id] = lock
    return lock


def _cached_token_is_fresh(installation_id: int) -> str | None:
    """Return the cached token if it still has enough time left."""

    cached = _token_cache.get(installation_id)
    if cached is None:
        return None
    token, expires_at = cached
    if expires_at - time.time() > TOKEN_REFRESH_MARGIN_SECONDS:
        return token
    return None


async def get_installation_token(installation_id: int) -> str:
    """Return a valid installation token, fetching one when needed.

    The token is cached in memory per installation and reused while it has
    more than TOKEN_REFRESH_MARGIN_SECONDS left. A per-installation lock
    makes sure concurrent requests do not fetch several tokens at once.
    """

    fresh = _cached_token_is_fresh(installation_id)
    if fresh is not None:
        return fresh

    lock = await _get_token_lock(installation_id)
    async with lock:
        # Check again: another request may have filled the cache while we
        # were waiting for the lock.
        fresh = _cached_token_is_fresh(installation_id)
        if fresh is not None:
            return fresh

        app_jwt = create_app_jwt()

        headers = {
            "Authorization": f"Bearer {app_jwt}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": GITHUB_API_VERSION,
            "User-Agent": "bugzzy-app",
        }
        url = f"{GITHUB_API_BASE}/app/installations/{installation_id}/access_tokens"

        try:
            async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
                response = await client.post(url, headers=headers)
        except httpx.TimeoutException as exc:
            raise GitHubNetworkError(
                "Timed out while contacting GitHub. Please try again."
            ) from exc
        except httpx.RequestError as exc:
            raise GitHubNetworkError(
                "Could not connect to GitHub. Please check your network."
            ) from exc

        # 404/403 means the installation was removed, suspended, or the app
        # no longer has access to it.
        if response.status_code in (403, 404):
            raise InstallationUnavailableError(
                "This GitHub installation is no longer available."
            )
        if response.status_code == 401:
            raise GitHubAuthError(
                "GitHub rejected our app credentials (401). "
                "Check the App ID and private key."
            )
        if response.status_code >= 400:
            raise GitHubAPIError(
                f"GitHub returned an unexpected status: {response.status_code}."
            )

        data = response.json()
        token = data.get("token")
        if not token:
            # Never echo the body: it would contain the token.
            raise GitHubAPIError("GitHub did not return an installation token.")

        expires_at = _parse_iso_to_epoch(str(data.get("expires_at") or ""))
        _token_cache[installation_id] = (token, expires_at)
        return token


def invalidate_installation_token(installation_id: int) -> None:
    """Forget the cached token for one installation (e.g. after a 401)."""

    _token_cache.pop(installation_id, None)


def reset_token_cache() -> None:
    """Clear all cached tokens and locks. Used by tests."""

    _token_cache.clear()
    _token_locks.clear()


