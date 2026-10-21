"""Routes for viewing and selecting installation repositories.

Everything here works with the user's CONNECTED installation only. We never
accept an installation id or repository id from the browser on trust:
installation ids come from the user's stored (GitHub-provided) list, and
repository ids are checked against a fresh GitHub response before saving.

Installation tokens are used server-side by the service layer and are never
returned to the browser.
"""

from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from app.api.deps import get_current_user, verify_csrf
from app.db.engine import get_sessionmaker
from app.db.models import CodeSymbol, RepoFile, Repository, SymbolEdge
from app.db.postgres_store import PostgresStore
from app.indexer.repo_indexer import index_single_repository, index_user_installation
from app.services.github_app import (
    GitHubAPIError,
    GitHubAuthError,
    GitHubNetworkError,
    InstallationUnavailableError,
)
from app.services.github_repositories import list_installation_repositories
from app.services.store import Store, get_store

# All routes here live under /api/v1/repositories.
router = APIRouter(prefix="/api/v1/repositories", tags=["repositories"])

# Maximum number of repositories a user may select in one request.
MAX_SELECTED_REPOSITORIES = 200


class RepositorySelectRequest(BaseModel):
    """Body for POST /select.

    Pydantic validates this for us: the list must contain integers only and
    contain at most MAX_SELECTED_REPOSITORIES entries (violations -> 422).
    """

    repository_ids: list[int] = Field(
        default_factory=list, max_length=MAX_SELECTED_REPOSITORIES
    )


async def _require_connected_installation(user: dict, store: Store) -> int:
    """Return the user's connected installation id, or raise 409.

    We also double-check the id is still in the user's stored installation
    list. If it is not (for example the user was removed from the
    installation), we clear the connection and report 409.
    """

    installation_id = await store.get_connected_installation(user["id"])
    if installation_id is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="No GitHub installation connected",
        )

    known_ids = {
        item.get("installation_id")
        for item in await store.get_user_installations(user["id"])
    }
    if installation_id not in known_ids:
        await store.set_connected_installation(user["id"], None)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="No GitHub installation connected",
        )

    return installation_id


async def _clear_connection(
    user: dict, installation_id: int, store: Store
) -> None:
    """Forget the connection and any selections for one installation."""

    await store.clear_selected_repositories(user["id"], installation_id)
    await store.set_connected_installation(user["id"], None)


async def _load_repositories(
    user: dict, installation_id: int, force_refresh: bool, store: Store
) -> list[dict]:
    """Call the service and map its exceptions to clean HTTP errors."""

    try:
        return await list_installation_repositories(
            installation_id, force_refresh=force_refresh
        )
    except InstallationUnavailableError:
        await _clear_connection(user, installation_id, store)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Installation is no longer available. Please reconnect.",
        )
    except GitHubAuthError:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not authenticate with GitHub.",
        )
    except GitHubNetworkError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Could not reach GitHub. Please try again.",
        )
    except GitHubAPIError:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="GitHub returned an error while listing repositories.",
        )


@router.get("")
async def list_repositories(
    refresh: bool = Query(default=False),
    user: dict = Depends(get_current_user),
    store: Store = Depends(get_store),
) -> JSONResponse:
    """List the connected installation's repositories with selection flags.

    Add ?refresh=true to bypass the 60 second server-side cache and read a
    fresh list from GitHub.
    """

    installation_id = await _require_connected_installation(user, store)
    repositories = await _load_repositories(
        user, installation_id, refresh, store
    )

    selected = set(
        await store.get_selected_repositories(user["id"], installation_id)
    )
    items = [
        {**repo, "selected": repo.get("id") in selected} for repo in repositories
    ]

    return JSONResponse(
        content={
            "installation_id": installation_id,
            "repositories": items,
            "total": len(items),
        },
        headers={"Cache-Control": "no-store"},
    )


@router.post("/select")
async def select_repositories(
    body: RepositorySelectRequest,
    background_tasks: BackgroundTasks,
    user: dict = Depends(get_current_user),
    _csrf: None = Depends(verify_csrf),
    store: Store = Depends(get_store),
) -> JSONResponse:
    """Save the repositories the user picked for the connected installation.

    We always fetch a fresh list first and keep only ids that appear in it.
    If any requested id is not accessible, nothing is saved and we return
    422. An empty list is allowed and clears the selection.
    """

    installation_id = await _require_connected_installation(user, store)
    repositories = await _load_repositories(
        user, installation_id, True, store
    )

    accessible_ids = {repo.get("id") for repo in repositories}

    # Reject the whole request if even one id is not accessible.
    for requested_id in body.repository_ids:
        if requested_id not in accessible_ids:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="One or more repositories are not accessible",
            )

    # Remove duplicates while keeping the order the user sent.
    unique_ids: list[int] = []
    for requested_id in body.repository_ids:
        if requested_id not in unique_ids:
            unique_ids.append(requested_id)

    await store.set_selected_repositories(
        user["id"], installation_id, unique_ids
    )

    if unique_ids and isinstance(store, PostgresStore):
        background_tasks.add_task(
            index_user_installation, installation_id, user["id"]
        )

    return JSONResponse(content={"selected_repository_ids": unique_ids})


@router.post("/{repo_id}/index")
async def trigger_reindex(
    repo_id: int,
    background_tasks: BackgroundTasks,
    user: dict = Depends(get_current_user),
    _csrf: None = Depends(verify_csrf),
    store: Store = Depends(get_store),
) -> JSONResponse:
    """Trigger a reindex of a repository.

    Allowed only if the user has the repo in selected_repositories.
    """
    installation_id = await store.get_connected_installation(user["id"])
    if installation_id is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="No GitHub installation connected",
        )

    selected_repo_ids = await store.get_selected_repositories(
        user["id"], installation_id
    )

    maker = get_sessionmaker()
    async with maker() as session:
        repo = (
            await session.execute(
                select(Repository).where(
                    (Repository.id == repo_id) | (Repository.github_repo_id == repo_id)
                )
            )
        ).scalar_one_or_none()

    if not repo:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Repository not found",
        )

    if (
        repo.github_repo_id not in selected_repo_ids
        and repo.id not in selected_repo_ids
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Repository is not selected for this user",
        )

    background_tasks.add_task(index_single_repository, repo.id)
    return JSONResponse(
        content={"detail": "Reindexing scheduled", "repo_id": repo.id}
    )


@router.get("/{repo_id}/index")
async def get_repository_index_counts(
    repo_id: int,
    user: dict = Depends(get_current_user),
    _store: Store = Depends(get_store),
) -> JSONResponse:
    """Return index counts for a repository (files, symbols, edges, unresolved_edges, last_indexed_sha)."""
    maker = get_sessionmaker()
    async with maker() as session:
        repo = (
            await session.execute(
                select(Repository).where(
                    (Repository.id == repo_id) | (Repository.github_repo_id == repo_id)
                )
            )
        ).scalar_one_or_none()

        if not repo:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Repository not found",
            )

        target_repo_id = repo.id

        files_count = (
            await session.execute(
                select(func.count(RepoFile.id)).where(
                    RepoFile.repo_id == target_repo_id,
                    RepoFile.deleted_at.is_(None),
                )
            )
        ).scalar_one()

        symbols_count = (
            await session.execute(
                select(func.count(CodeSymbol.id)).where(
                    CodeSymbol.repo_id == target_repo_id,
                    CodeSymbol.deleted_at.is_(None),
                )
            )
        ).scalar_one()

        edges_count = (
            await session.execute(
                select(func.count(SymbolEdge.id)).where(
                    SymbolEdge.repo_id == target_repo_id
                )
            )
        ).scalar_one()

        unresolved_count = (
            await session.execute(
                select(func.count(SymbolEdge.id)).where(
                    SymbolEdge.repo_id == target_repo_id,
                    SymbolEdge.target_symbol_id.is_(None),
                )
            )
        ).scalar_one()

    return JSONResponse(
        content={
            "repo_id": target_repo_id,
            "github_repo_id": repo.github_repo_id,
            "owner": repo.owner,
            "name": repo.name,
            "files": files_count,
            "symbols": symbols_count,
            "edges": edges_count,
            "unresolved_edges": unresolved_count,
            "last_indexed_sha": repo.last_indexed_sha,
        }
    )
