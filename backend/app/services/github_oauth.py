"""GitHub OAuth (user login) helpers.

This is the "sign in with GitHub" flow for a GitHub App:

1. We send the user to GitHub's authorize page (see build_authorize_url).
2. GitHub sends them back to our callback with a short-lived "code".
3. We trade that code for a user access token (exchange_code_for_token).
4. We use the token once to read the user's profile (fetch_github_user),
   then throw the token away.

Security notes:
* GitHub Apps must NOT send a "scope" parameter; permissions come from the
  App's own settings.
* The authorization code and the access token are treated as secrets: they
  are never included in any error message and are never logged.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

import httpx

from app.config import get_settings
from app.services.github_app import (
    GITHUB_API_BASE,
    GITHUB_API_VERSION,
    REQUEST_TIMEOUT,
    GitHubAPIError,
    GitHubAuthError,
    GitHubNetworkError,
)

# GitHub's OAuth endpoints (note: these live on github.com, not api.github.com).
GITHUB_AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
GITHUB_TOKEN_URL = "https://github.com/login/oauth/access_token"

# Temporary switch for PKCE. If GitHub ever rejects the PKCE parameters we
# can set this to False to fall back to state-only protection.
USE_PKCE = True


def build_authorize_url(state: str, code_challenge: str | None) -> str:
    """Build the URL we redirect the browser to so GitHub can log the user in.

    Includes the state (tamper protection) and, when enabled, the PKCE
    challenge. Deliberately omits "scope" because GitHub Apps ignore it.
    """

    settings = get_settings()

    params = {
        "client_id": settings.client_id,
        "redirect_uri": settings.redirect_uri,
        "state": state,
    }

    if USE_PKCE and code_challenge:
        params["code_challenge"] = code_challenge
        params["code_challenge_method"] = "S256"

    return f"{GITHUB_AUTHORIZE_URL}?{urlencode(params)}"


async def exchange_code_for_token(code: str, code_verifier: str | None) -> str:
    """Trade the temporary "code" for a GitHub user access token.

    Returns just the token string. Any failure raises a custom exception
    with a safe message (no token, code or secret included).
    """

    settings = get_settings()

    data = {
        "client_id": settings.client_id,
        "client_secret": settings.client_secret,
        "code": code,
        "redirect_uri": settings.redirect_uri,
    }
    if USE_PKCE and code_verifier:
        data["code_verifier"] = code_verifier

    # We ask GitHub for JSON. Without this header GitHub replies with a
    # URL-encoded form body, which is harder to read.
    headers = {"Accept": "application/json"}

    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            response = await client.post(
                GITHUB_TOKEN_URL, data=data, headers=headers
            )
    except httpx.TimeoutException as exc:
        raise GitHubNetworkError(
            "Timed out while contacting GitHub. Please try again."
        ) from exc
    except httpx.RequestError as exc:
        raise GitHubNetworkError(
            "Could not connect to GitHub. Please check your network."
        ) from exc

    # GitHub sometimes returns HTTP 200 with an "error" field in the body
    # when the exchange fails, so we must inspect the body, not just the code.
    try:
        payload = response.json()
    except ValueError as exc:
        raise GitHubAPIError(
            "GitHub returned an unreadable response during login."
        ) from exc

    if not isinstance(payload, dict):
        raise GitHubAPIError("GitHub returned an unexpected response during login.")

    if response.status_code >= 400 or payload.get("error"):
        # Build a safe message from GitHub's own error names only.
        error_name = str(payload.get("error") or response.status_code)
        error_desc = str(payload.get("error_description") or "").strip()
        message = f"GitHub rejected the login: {error_name}."
        if error_desc:
            message = f"{message} {error_desc}"
        raise GitHubAuthError(message)

    token = payload.get("access_token")
    if not token:
        # Never echo the payload; it could contain a token.
        raise GitHubAuthError(
            "GitHub did not return an access token during login."
        )

    return token


async def fetch_github_user(access_token: str) -> dict[str, Any]:
    """Read the logged-in user's profile, returning only safe fields."""

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": GITHUB_API_VERSION,
        "User-Agent": "bugzzy-app",
    }

    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            response = await client.get(f"{GITHUB_API_BASE}/user", headers=headers)
    except httpx.TimeoutException as exc:
        raise GitHubNetworkError(
            "Timed out while contacting GitHub. Please try again."
        ) from exc
    except httpx.RequestError as exc:
        raise GitHubNetworkError(
            "Could not connect to GitHub. Please check your network."
        ) from exc

    if response.status_code == 401:
        raise GitHubAuthError("GitHub rejected the user access token (401).")
    if response.status_code >= 400:
        raise GitHubAPIError(
            f"GitHub returned an unexpected status: {response.status_code}."
        )

    data = response.json()

    # Return only the fields the rest of the app needs.
    return {
        "id": data.get("id"),
        "login": data.get("login"),
        "name": data.get("name"),
        "avatar_url": data.get("avatar_url"),
    }
