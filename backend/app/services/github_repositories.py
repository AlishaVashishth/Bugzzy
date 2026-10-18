"""List the repositories an installation can access.

We first get an installation access token (from github_app), then ask
GitHub for the repositories that token can see. Results are cached briefly
(60 seconds) because the list rarely changes and this saves GitHub calls.

Security: installation tokens are server-side only. They are never
returned to the browser, never logged, and never put in an error message.
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from app.services.github_app import (
    GITHUB_API_BASE,
    GITHUB_API_VERSION,
    REQUEST_TIMEOUT,
    GitHubAPIError,
    GitHubAuthError,
    GitHubNetworkError,
    get_installation_token,
    invalidate_installation_token,
)

# Safety cap for repository pagination. GitHub returns pages of up to 100
# items; we stop after this many pages even if GitHub claims there are more.
MAX_REPOSITORY_PAGES = 10

# GitHub returns at most this many repositories per page.
REPOSITORIES_PER_PAGE = 100

# How long a fetched repository list stays fresh in memory (seconds).
RESULT_CACHE_SECONDS = 60

# Cache shape: {installation_id: (expires_at_epoch, [repository, ...])}.
_result_cache: dict[int, tuple[float, list[dict[str, Any]]]] = {}


def _keep_only_safe_fields(repo: dict[str, Any]) -> dict[str, Any]:
    """Pick just the repository fields the app needs.

    We drop everything else so we never accidentally pass through extra
    data from GitHub.
    """

    owner = repo.get("owner") or {}
    return {
        "id": repo.get("id"),
        "name": repo.get("name"),
        "full_name": repo.get("full_name"),
        "owner_login": owner.get("login"),
        "private": repo.get("private"),
        "archived": repo.get("archived"),
        "default_branch": repo.get("default_branch"),
        "html_url": repo.get("html_url"),
        "language": repo.get("language"),
        "updated_at": repo.get("updated_at"),
    }


def _is_rate_limited(response: httpx.Response) -> bool:
    """True when GitHub is refusing us because we hit a rate limit."""

    if response.status_code == 429:
        return True
    if response.status_code == 403:
        remaining = response.headers.get("x-ratelimit-remaining")
        retry_after = response.headers.get("retry-after")
        return remaining == "0" or retry_after is not None
    return False


async def _fetch_all_repositories(token: str) -> list[dict[str, Any]]:
    """Fetch and combine every page of repositories for one token.

    Raises GitHubAuthError on a 401 so the caller can refresh the token.
    """

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": GITHUB_API_VERSION,
        "User-Agent": "bugzzy-app",
    }

    results: list[dict[str, Any]] = []
    raw_seen = 0
    page = 1

    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            while page <= MAX_REPOSITORY_PAGES:
                response = await client.get(
                    f"{GITHUB_API_BASE}/installation/repositories",
                    headers=headers,
                    params={"per_page": REPOSITORIES_PER_PAGE, "page": page},
                )

                if response.status_code == 401:
                    # Token was revoked early. Let the caller retry once.
                    raise GitHubAuthError(
                        "GitHub rejected the installation token (401)."
                    )
                if _is_rate_limited(response):
                    raise GitHubAPIError(
                        "GitHub rate limit reached. Please try again shortly."
                    )
                if response.status_code >= 400:
                    raise GitHubAPIError(
                        "GitHub returned an unexpected status while listing "
                        f"repositories: {response.status_code}."
                    )

                payload = response.json()
                if not isinstance(payload, dict):
                    raise GitHubAPIError(
                        "GitHub returned an unexpected repositories response."
                    )

                page_items = payload.get("repositories") or []
                raw_seen += len(page_items)
                for repo in page_items:
                    results.append(_keep_only_safe_fields(repo))

                if not page_items:
                    break

                total_count = payload.get("total_count")
                if isinstance(total_count, int) and raw_seen >= total_count:
                    break

                page += 1
    except httpx.TimeoutException as exc:
        raise GitHubNetworkError(
            "Timed out while contacting GitHub. Please try again."
        ) from exc
    except httpx.RequestError as exc:
        raise GitHubNetworkError(
            "Could not connect to GitHub. Please check your network."
        ) from exc

    return results


async def list_installation_repositories(
    installation_id: int, force_refresh: bool = False
) -> list[dict[str, Any]]:
    """Return the repositories for an installation.

    Uses a short-lived (60 second) in-memory cache unless force_refresh is
    True. If the installation token is rejected (401), it is discarded and
    one fresh attempt is made.
    """

    if not force_refresh:
        cached = _result_cache.get(installation_id)
        if cached is not None and cached[0] - time.time() > 0:
            return [dict(repo) for repo in cached[1]]

    token = await get_installation_token(installation_id)

    try:
        repositories = await _fetch_all_repositories(token)
    except GitHubAuthError:
        # The token was revoked before its expiry. Throw it away and try
        # exactly once more with a brand new token.
        invalidate_installation_token(installation_id)
        token = await get_installation_token(installation_id)
        repositories = await _fetch_all_repositories(token)

    _result_cache[installation_id] = (
        time.time() + RESULT_CACHE_SECONDS,
        [dict(repo) for repo in repositories],
    )
    return repositories


def reset_repository_cache() -> None:
    """Clear the short-lived repository cache. Used by tests."""

    _result_cache.clear()
