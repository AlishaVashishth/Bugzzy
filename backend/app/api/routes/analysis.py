"""Routes for pull request analysis and bug localization.

Orchestrates context building and AI bug localization for pull requests
belonging to user-selected repositories.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from app.api.deps import get_current_user, verify_csrf
from app.api.routes.repositories import _require_connected_installation
from app.services.github_app import (
    GitHubAPIError as AppGitHubAPIError,
    GitHubAuthError,
    GitHubNetworkError,
    InstallationUnavailableError,
    get_installation_token,
)
from app.services.github_repositories import list_installation_repositories
from app.services.store import Store, get_store
from pr_context_builder.builder.context_builder import build_context
from pr_context_builder.exceptions import (
    GitHubAPIError as EngineGitHubAPIError,
)
from pr_context_builder.index import PostgresCallerIndex
from pr_context_builder.localization.agent import localize_bugs

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/analysis", tags=["analysis"])


class PRAnalysisRequest(BaseModel):
    """Request payload for PR analysis."""

    owner: str
    repo: str
    pull_number: int = Field(gt=0)


def _get_database_url() -> str | None:
    """Read the database URL from app config if exposed, falling back to os.environ.

    Never reads TEST_DATABASE_URL, and never logs the URL.
    """
    try:
        from app.config import get_settings

        settings = get_settings()
        url = getattr(settings, "database_url", None)
        if url:
            return str(url).strip()
    except Exception:
        pass
    url = os.environ.get("DATABASE_URL")
    return url.strip() if url and url.strip() else None


@router.post("/pr")
async def analyze_pull_request(
    body: PRAnalysisRequest,
    user: dict[str, Any] = Depends(get_current_user),
    _csrf: None = Depends(verify_csrf),
    store: Store = Depends(get_store),
) -> dict[str, Any]:
    """Analyze a pull request for bugs.

    Builds code context from GitHub and runs deterministic and AI bug localization.
    Only repositories selected by the user for the connected installation are allowed.
    """
    # 2. Get the user's connected installation id
    installation_id = await _require_connected_installation(user, store)

    # 3. AUTHORIZATION: Verify repository is in selected repositories
    try:
        repositories = await list_installation_repositories(installation_id)
    except (
        AppGitHubAPIError,
        GitHubAuthError,
        GitHubNetworkError,
        InstallationUnavailableError,
    ) as exc:
        logger.error(
            "GitHub error listing repositories for installation %s: %s",
            installation_id,
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="GitHub returned an error while processing the request",
        )

    selected_repo_ids = set(await store.get_selected_repositories(user["id"], installation_id))
    target_full_name = f"{body.owner}/{body.repo}"

    matching_repo = next(
        (
            repo
            for repo in repositories
            if repo.get("full_name") == target_full_name
            or (repo.get("full_name") or "").lower() == target_full_name.lower()
        ),
        None,
    )

    if matching_repo is None or matching_repo.get("id") not in selected_repo_ids:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Repository not found",
        )

    # 4. token = await get_installation_token(installation_id)
    try:
        token = await get_installation_token(installation_id)
    except (
        AppGitHubAPIError,
        GitHubAuthError,
        GitHubNetworkError,
        InstallationUnavailableError,
    ) as exc:
        logger.error(
            "GitHub error obtaining installation token for %s: %s",
            installation_id,
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="GitHub returned an error while processing the request",
        )

    # 5. Run build_context and localize_bugs in threadpool (blocking operations)
    db_url = _get_database_url()
    caller_index = PostgresCallerIndex(db_url) if db_url else None

    try:
        if caller_index is not None:
            context = await run_in_threadpool(
                build_context,
                body.owner,
                body.repo,
                body.pull_number,
                token,
                caller_index=caller_index,
            )
        else:
            context = await run_in_threadpool(
                build_context,
                body.owner,
                body.repo,
                body.pull_number,
                token,
            )
        result = await run_in_threadpool(localize_bugs, context)
    except (AppGitHubAPIError, EngineGitHubAPIError) as exc:
        logger.error(
            "GitHub error during context building or bug localization: %s",
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="GitHub returned an error while processing the request",
        )

    # 6. Return result.model_dump(mode="json")
    return result.model_dump(mode="json")
