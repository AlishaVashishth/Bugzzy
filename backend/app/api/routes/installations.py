"""Routes for discovering and connecting GitHub App installations.

A "GitHub App installation" is the app being installed on a user account or
organization. A user can have several. These endpoints let the frontend:

* list the installations GitHub returned for the current user at login;
* get the URL to install the app on another account;
* pick ("connect") one installation to use in Bugzzy;
* disconnect it again.

SECURITY: we only ever accept an installation id that appears in the list
GitHub returned for THIS user. A raw id from a URL or request body is never
trusted on its own.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse

from app.api.deps import get_current_user, verify_csrf
from app.services import github_app
from app.services.github_app import (
    GitHubAPIError,
    GitHubAuthError,
    GitHubNetworkError,
)
from app.services.store import (
    InstallationNotAvailableError,
    Store,
    get_store,
)

# All routes here live under /api/v1/installations.
router = APIRouter(prefix="/api/v1/installations", tags=["installations"])

# The base URL GitHub uses to install an app. The slug is filled in from the
# app's own details.
INSTALL_BASE_URL = "https://github.com/apps"


@router.get("")
async def list_installations(
    user: dict = Depends(get_current_user),
    store: Store = Depends(get_store),
) -> JSONResponse:
    """List the current user's installations and which one is connected.

    NOTE: the list is a snapshot taken when the user last logged in. It is
    NOT refreshed on every request. After installing the app on a new
    account, the frontend should send the user to /api/v1/auth/github/login
    to refresh it (that login re-reads the installations from GitHub).
    """

    return JSONResponse(
        content={
            "installations": await store.get_user_installations(user["id"]),
            "connected_installation_id": await store.get_connected_installation(
                user["id"]
            ),
        },
        headers={"Cache-Control": "no-store"},
    )


@router.get("/install-url")
async def install_url(user: dict = Depends(get_current_user)) -> JSONResponse:
    """Return the URL a user visits to install the app on an account."""

    try:
        slug = await github_app.get_app_slug()
    except GitHubAuthError as exc:
        return JSONResponse(status_code=401, content={"detail": str(exc)})
    except GitHubAPIError:
        # Map any API problem to a safe, generic message.
        return JSONResponse(
            status_code=502,
            content={"detail": "Could not get the app install URL right now."},
        )
    except GitHubNetworkError:
        return JSONResponse(
            status_code=503,
            content={"detail": "Could not reach GitHub to get the install URL."},
        )

    return JSONResponse(content={"url": f"{INSTALL_BASE_URL}/{slug}/installations/new"})


@router.post("/{installation_id}/connect")
async def connect_installation(
    installation_id: int,
    user: dict = Depends(get_current_user),
    _csrf: None = Depends(verify_csrf),
    store: Store = Depends(get_store),
) -> JSONResponse:
    """Connect one of the user's installations.

    The id must be in the user's own list; anything else is a 404 with the
    same message, so we never reveal whether an id exists for someone else.
    """

    installations = await store.get_user_installations(user["id"])
    chosen = None
    for item in installations:
        if item.get("installation_id") == installation_id:
            chosen = item
            break

    if chosen is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Installation not found",
        )

    try:
        await store.set_connected_installation(user["id"], installation_id)
    except InstallationNotAvailableError:
        # The store re-checks the list. Treat a mismatch the same way so we
        # never reveal anything about other users' installations.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Installation not found",
        )
    return JSONResponse(
        content={
            "connected_installation_id": installation_id,
            "installation": chosen,
        }
    )


@router.post("/disconnect")
async def disconnect_installation(
    user: dict = Depends(get_current_user),
    _csrf: None = Depends(verify_csrf),
    store: Store = Depends(get_store),
) -> JSONResponse:
    """Clear the user's connected installation.

    Succeeds even if nothing was connected, so the frontend can call it
    freely.
    """

    await store.set_connected_installation(user["id"], None)
    return JSONResponse(content={"connected_installation_id": None})
