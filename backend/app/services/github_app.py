"""GitHub App authentication helpers.

A GitHub App proves who it is in two steps:

1. It signs a short-lived JWT (JSON Web Token) with its private key.
2. It sends that JWT to GitHub, and GitHub replies with app details
   (or, in later steps, an installation token).

This module covers those first two steps. Nothing here ever prints or
stores the private key or the JWT in a message, so failures stay safe.
"""

from __future__ import annotations

import time
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


class GitHubAuthError(Exception):
    """Raised when our credentials are wrong (bad key, bad JWT, HTTP 401)."""


class GitHubAPIError(Exception):
    """Raised when GitHub answers with an unexpected non-2xx status."""


class GitHubNetworkError(Exception):
    """Raised when we cannot reach GitHub (timeout or connection failure)."""


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

