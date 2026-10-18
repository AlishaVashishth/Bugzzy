"""Authentication routes: "Sign in with GitHub" for a GitHub App.

Flow summary:

    GET  /api/v1/auth/github/login     -> send the browser to GitHub
    GET  /api/v1/auth/github/callback  -> GitHub sends the user back here
    GET  /api/v1/auth/me               -> who am I? (needs a session)
    POST /api/v1/auth/logout           -> end the session (needs CSRF token)

The temporary "state" and PKCE "code_verifier" values live in one
short-lived cookie during login. On success we replace it with a real
Bugzzy session cookie that holds a random session id; the matching session
data is kept server-side and never given to the browser.
"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse

from app.api.deps import (
    get_current_session,
    get_current_user,
    get_raw_session_id,
    verify_csrf,
)
from app.config import get_settings
from app.security.sessions import get_session_store
from app.security.state import (
    code_challenge_s256,
    create_code_verifier,
    create_state,
    verify_state,
)
from app.services import github_oauth
from app.services.github_app import (
    GitHubAPIError,
    GitHubAuthError,
    GitHubNetworkError,
)
from app.services.user_store import set_user_installations, upsert_user

# A logger for this module. We only ever log safe, fixed messages: never a
# token, a code, or a session id.
logger = logging.getLogger(__name__)

# All routes here live under /api/v1/auth.
router = APIRouter(prefix="/api/v1/auth", tags=["auth"])

# Name of the short-lived cookie that carries the state + PKCE verifier.
OAUTH_COOKIE_NAME = "bugzzy_oauth"

# How long the cookie (and therefore the login attempt) stays valid.
OAUTH_COOKIE_MAX_AGE = 10 * 60  # 10 minutes in seconds


# --------------------------------------------------------------------------
# Cookie helpers
# --------------------------------------------------------------------------

def _set_oauth_cookie(response: Response, state: str, verifier: str) -> None:
    """Attach the temporary OAuth cookie to a response.

    HttpOnly stops JavaScript from reading it (XSS protection).
    SameSite=Lax means it is still sent on GitHub's redirect back to us.
    Secure is only enabled in production, because local development uses
    plain http and a Secure cookie would be dropped by the browser.
    """

    settings = get_settings()
    payload = json.dumps({"state": state, "code_verifier": verifier})
    response.set_cookie(
        key=OAUTH_COOKIE_NAME,
        value=payload,
        max_age=OAUTH_COOKIE_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
        path="/",
    )


def _clear_oauth_cookie(response: Response) -> None:
    """Remove the temporary OAuth cookie once it has served its purpose."""

    settings = get_settings()
    response.delete_cookie(
        key=OAUTH_COOKIE_NAME,
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
        path="/",
    )


def _set_session_cookie(response: Response, raw_session_id: str) -> None:
    """Attach the real session cookie to a response.

    The cookie value is the random session id. It is HttpOnly so page
    JavaScript cannot read it (the CSRF token handles that instead), and
    its lifetime matches the session's absolute maximum age.
    """

    settings = get_settings()
    response.set_cookie(
        key=settings.session_cookie_name,
        value=raw_session_id,
        max_age=settings.session_max_age_seconds,
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
        path="/",
    )


def _clear_session_cookie(response: Response) -> None:
    """Clear the session cookie using the same attributes it was set with."""

    settings = get_settings()
    response.delete_cookie(
        key=settings.session_cookie_name,
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
        path="/",
    )


# --------------------------------------------------------------------------
# Login
# --------------------------------------------------------------------------

@router.get("/github/login")
async def github_login() -> RedirectResponse:
    """Begin login: set the state cookie and redirect to GitHub.

    A 307 redirect keeps the HTTP method and is what the browser needs to
    leave our site and land on GitHub's authorize page.
    """

    state = create_state()
    verifier = create_code_verifier()
    challenge = code_challenge_s256(verifier)

    authorize_url = github_oauth.build_authorize_url(state, challenge)

    # Redirect the user's browser to GitHub. 307 = temporary redirect.
    response = RedirectResponse(url=authorize_url, status_code=307)
    _set_oauth_cookie(response, state, verifier)
    return response


@router.get("/github/callback")
async def github_callback(
    request: Request,
    code: str | None = Query(default=None),
    state: str | None = Query(default=None),
    error: str | None = Query(default=None),
    setup_action: str | None = Query(default=None),
    installation_id: str | None = Query(default=None),
) -> Response:
    """Finish login: validate, exchange the code, then start a session.

    This endpoint handles two cases:

    * Normal login: we sent the user to GitHub ourselves, so a "state" value
      and our oauth cookie are present. We verify them strictly.
    * Post-install redirect: the App is set to request user authorization
      during installation, so GitHub can send the user here right after
      they install. That request has a code and setup_action but no state
      and no cookie. We do NOT trust its installation_id; instead we send
      the user through a clean login (which refreshes their installation
      list). The code is never exchanged without a passing state check.

    installation_id is accepted only so FastAPI does not reject the query
    string. Its value is ignored for trust purposes and never stored.
    """

    # We keep a reference to this purely to make the "ignored" intent clear.
    _ = installation_id

    # Read the temporary OAuth cookie. It must be gone once this request
    # finishes, whatever the outcome.
    cookie_value = request.cookies.get(OAUTH_COOKIE_NAME)

    stored_state = None
    code_verifier = None
    if cookie_value:
        try:
            cookie_data = json.loads(cookie_value)
            stored_state = cookie_data.get("state")
            code_verifier = cookie_data.get("code_verifier")
        except (ValueError, AttributeError):
            # A corrupt cookie is treated the same as a missing one.
            stored_state = None

    def bad_request(message: str) -> JSONResponse:
        """Build a 400 response that also clears the temporary cookie."""

        resp = JSONResponse(status_code=400, content={"detail": message})
        _clear_oauth_cookie(resp)
        return resp

    # 1. GitHub can tell us the user cancelled or something went wrong.
    if error:
        return bad_request(f"GitHub login failed: {error}.")

    # 2. The code must be present. (Its value is never logged.)
    if not code:
        return bad_request("Missing 'code' from GitHub.")

    # 2b. Post-install redirect. There is no state and no cookie, so this is
    #     not a login we started. Send the user to a clean login instead of
    #     exchanging this code. This keeps the strict state check below.
    if setup_action and not state and not cookie_value:
        resp = RedirectResponse(
            url="/api/v1/auth/github/login", status_code=302
        )
        _clear_oauth_cookie(resp)
        return resp

    # 3. The state must be present in the query string.
    if not state:
        return bad_request("Missing 'state' from GitHub.")

    # 4. The cookie must exist and its state must match what GitHub returned.
    if not cookie_value:
        return bad_request("Login session expired or missing. Please try again.")
    if not verify_state(stored_state, state):
        return bad_request("State check failed. Please try again.")

    # 5. Trade the code for a user access token (used once, then discarded).
    try:
        access_token = await github_oauth.exchange_code_for_token(
            code, code_verifier
        )
        github_user = await github_oauth.fetch_github_user(access_token)
    except GitHubAuthError as exc:
        resp = JSONResponse(status_code=401, content={"detail": str(exc)})
        _clear_oauth_cookie(resp)
        return resp
    except GitHubNetworkError as exc:
        resp = JSONResponse(status_code=503, content={"detail": str(exc)})
        _clear_oauth_cookie(resp)
        return resp
    except GitHubAPIError as exc:
        resp = JSONResponse(status_code=502, content={"detail": str(exc)})
        _clear_oauth_cookie(resp)
        return resp

    # 6. While we still have the token, list this user's installations. This
    #    is the ONLY trusted source of installation ids. If it fails, login
    #    must still succeed, just with an empty list.
    try:
        installations = await github_oauth.fetch_user_installations(access_token)
    except (GitHubAuthError, GitHubAPIError, GitHubNetworkError):
        logger.warning(
            "Could not fetch GitHub installations during login; "
            "continuing with an empty list."
        )
        installations = []
    # The token is intentionally not stored anywhere. It lives only in the
    # local variable above and is dropped as soon as this request ends.

    # 7. Save the user locally and replace their installation list.
    user = upsert_user(github_user)
    set_user_installations(user["id"], installations)

    # 8. If this browser already has a session, remove it first. Every login
    #    gets a brand new session id; we never reuse an old one.
    store = get_session_store()
    existing_raw_id = get_raw_session_id(request)
    if existing_raw_id:
        store.delete(existing_raw_id)

    # 9. Create the new session and send the browser to the frontend.
    raw_session_id, _csrf_token = store.create(user["id"])

    response = RedirectResponse(url=get_settings().frontend_url, status_code=302)
    _set_session_cookie(response, raw_session_id)
    _clear_oauth_cookie(response)

    # Never log the raw session id or the CSRF token.
    return response


# --------------------------------------------------------------------------
# Who am I?
# --------------------------------------------------------------------------

@router.get("/me")
async def me(
    session=Depends(get_current_session),
    user: dict = Depends(get_current_user),
) -> JSONResponse:
    """Return the logged-in user and their CSRF token.

    The frontend reads the CSRF token here and sends it back in the
    X-CSRF-Token header on state-changing requests.
    """

    return JSONResponse(
        content={
            "user": {
                "id": user["id"],
                "github_user_id": user["github_user_id"],
                "username": user["username"],
                "name": user["name"],
                "avatar_url": user["avatar_url"],
            },
            "csrf_token": session.csrf_token,
        },
        headers={"Cache-Control": "no-store"},
    )


# --------------------------------------------------------------------------
# Logout
# --------------------------------------------------------------------------

@router.post("/logout")
async def logout(
    request: Request,
    _user: dict = Depends(get_current_user),
    _csrf: None = Depends(verify_csrf),
) -> JSONResponse:
    """End the current session.

    Deleting the session server-side is what really logs the user out; we
    also clear the cookie so the browser stops sending it.
    """

    # Remove the session from the store.
    get_session_store().delete(get_raw_session_id(request))

    response = JSONResponse(content={"message": "Logged out"})
    _clear_session_cookie(response)
    return response
