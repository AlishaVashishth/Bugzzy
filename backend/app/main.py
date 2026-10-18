"""Bugzzy FastAPI application entry point.

Run it locally with:  uvicorn app.main:app --port 8000
"""

from __future__ import annotations

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.deps import get_current_user
from app.api.routes.auth import router as auth_router
from app.config import get_settings
from app.services.github_app import (
    GitHubAPIError,
    GitHubAuthError,
    GitHubNetworkError,
    get_app_info,
)

# Create the FastAPI application with a human-friendly title.
app = FastAPI(title="Bugzzy")

# Read settings once at startup so CORS can be configured below.
_settings = get_settings()

# CORS controls which websites may call this API from a browser. We allow
# exactly our frontend origin (never "*"), because "*" cannot be combined
# with cookies. allow_credentials=True lets the session cookie be sent.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[_settings.frontend_url],
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type", "X-CSRF-Token"],
)

# Register the authentication routes (/api/v1/auth/...).
app.include_router(auth_router)


@app.get("/api/v1/health")
async def health() -> dict[str, str]:
    """Simple health check so we can confirm the server is running."""

    return {"status": "ok"}


# NOTE: Temporary developer endpoint. It used to be public; it now requires
# a logged-in session (get_current_user) so app details are not exposed.
# It will be removed once real features replace it.
@app.get("/api/v1/github/app-check")
async def github_app_check(_user: dict = Depends(get_current_user)) -> JSONResponse:
    """Return a safe subset of the GitHub App's own details.

    Requires a valid session. We intentionally expose only a few safe
    fields, never the JWT or any secret, and we turn our custom exceptions
    into clean HTTP responses.
    """

    try:
        data = await get_app_info()
    except GitHubAuthError as exc:
        # 401 Unauthorized: our credentials were rejected.
        return JSONResponse(status_code=401, content={"detail": str(exc)})
    except GitHubAPIError as exc:
        # 502 Bad Gateway: GitHub is reachable but answered with an error.
        return JSONResponse(status_code=502, content={"detail": str(exc)})
    except GitHubNetworkError as exc:
        # 503 Service Unavailable: we could not reach GitHub at all.
        return JSONResponse(status_code=503, content={"detail": str(exc)})

    # Pull out only the fields we want to expose publicly.
    owner = data.get("owner") or {}
    return JSONResponse(
        content={
            "id": data.get("id"),
            "slug": data.get("slug"),
            "name": data.get("name"),
            "owner_login": owner.get("login"),
            "permissions": data.get("permissions"),
            "events": data.get("events"),
        }
    )
