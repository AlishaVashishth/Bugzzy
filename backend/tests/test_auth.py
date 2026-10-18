"""Tests for the GitHub OAuth login flow.

No real network calls are made to GitHub. The two functions that talk to
GitHub (exchange_code_for_token and fetch_github_user) are replaced with
fakes for every test, so no code or token ever leaves this machine.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from app.api.routes.auth import OAUTH_COOKIE_NAME
from app.config import get_settings
from app.main import app
from app.security.sessions import get_session_store
from app.services import github_oauth
from app.services.github_app import GitHubAuthError, GitHubNetworkError
from app.services.user_store import reset_store

# A fake token with a distinctive value so we can prove it never leaks.
FAKE_TOKEN = "gho_FAKE_TOKEN_SHOULD_NOT_LEAK"


@pytest.fixture()
def client() -> TestClient:
    """A fresh client so each test starts with no cookies."""

    # Start every test with empty stores.
    reset_store()
    get_session_store().clear()
    return TestClient(app)


@pytest.fixture()
def fake_github(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """Replace the real GitHub calls with controllable fakes."""

    async def fake_exchange(code: str, code_verifier: str | None) -> str:
        return FAKE_TOKEN

    async def fake_fetch(access_token: str) -> dict[str, object]:
        return {
            "id": 4242,
            "login": "octocat",
            "name": "The Octocat",
            "avatar_url": "https://example.invalid/avatar.png",
        }

    monkeypatch.setattr(github_oauth, "exchange_code_for_token", fake_exchange)
    monkeypatch.setattr(github_oauth, "fetch_github_user", fake_fetch)
    return {"token": FAKE_TOKEN}


def _start_login(client: TestClient) -> str:
    """Hit /github/login and return the state from the redirect URL."""

    resp = client.get("/api/v1/auth/github/login", follow_redirects=False)
    assert resp.status_code == 307
    location = resp.headers["location"]
    query = parse_qs(urlparse(location).query)
    return query["state"][0]


# --------------------------------------------------------------------------
# /github/login
# --------------------------------------------------------------------------

def test_login_redirects_to_github_with_expected_params(client: TestClient) -> None:
    resp = client.get("/api/v1/auth/github/login", follow_redirects=False)

    assert resp.status_code == 307
    location = resp.headers["location"]
    assert location.startswith("https://github.com/login/oauth/authorize")

    query = parse_qs(urlparse(location).query)
    assert query["client_id"][0]  # present and non-empty
    assert query["redirect_uri"][0] == (
        "http://localhost:8000/api/v1/auth/github/callback"
    )
    assert query["state"][0]
    assert query["code_challenge"][0]
    assert query["code_challenge_method"][0] == "S256"
    # GitHub Apps must not send a scope parameter.
    assert "scope" not in query


def test_login_sets_httponly_state_cookie(client: TestClient) -> None:
    resp = client.get("/api/v1/auth/github/login", follow_redirects=False)

    set_cookie = resp.headers["set-cookie"]
    assert OAUTH_COOKIE_NAME in set_cookie
    assert "HttpOnly" in set_cookie
    assert "samesite=lax" in set_cookie.lower()
    # Local development is not production, so the cookie must not be Secure.
    assert "secure" not in set_cookie.lower()


# --------------------------------------------------------------------------
# /github/callback - error cases
# --------------------------------------------------------------------------

def test_callback_wrong_state_returns_400(client: TestClient) -> None:
    _start_login(client)  # sets the cookie in the client's jar

    resp = client.get(
        "/api/v1/auth/github/callback",
        params={"code": "abc", "state": "not-the-right-state"},
        follow_redirects=False,
    )

    assert resp.status_code == 400
    assert "detail" in resp.json()


def test_callback_missing_cookie_returns_400(client: TestClient) -> None:
    # No login call, so there is no state cookie at all.
    resp = client.get(
        "/api/v1/auth/github/callback",
        params={"code": "abc", "state": "whatever"},
        follow_redirects=False,
    )

    assert resp.status_code == 400


def test_callback_missing_code_returns_400(client: TestClient) -> None:
    state = _start_login(client)

    resp = client.get(
        "/api/v1/auth/github/callback",
        params={"state": state},
        follow_redirects=False,
    )

    assert resp.status_code == 400


def test_callback_error_param_returns_400(client: TestClient) -> None:
    resp = client.get(
        "/api/v1/auth/github/callback",
        params={"error": "access_denied"},
        follow_redirects=False,
    )

    assert resp.status_code == 400
    assert "access_denied" in resp.json()["detail"]


# --------------------------------------------------------------------------
# /github/callback - happy path (Step 3: session cookie + redirect)
# --------------------------------------------------------------------------

def test_callback_happy_path_redirects_and_sets_session(
    client: TestClient,
    fake_github: dict[str, object],
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = get_settings()
    state = _start_login(client)

    with caplog.at_level("DEBUG"):
        resp = client.get(
            "/api/v1/auth/github/callback",
            params={"code": "the-code", "state": state},
            follow_redirects=False,
        )

    # A 302 redirect to the frontend, not a JSON body.
    assert resp.status_code == 302
    assert resp.headers["location"] == settings.frontend_url

    set_cookie = resp.headers.get("set-cookie", "")
    # The real session cookie is set with the expected attributes.
    assert f"{settings.session_cookie_name}=" in set_cookie
    assert "HttpOnly" in set_cookie
    assert "samesite=lax" in set_cookie.lower()
    assert f"Max-Age={settings.session_max_age_seconds}" in set_cookie
    assert "secure" not in set_cookie.lower()  # not production
    # The temporary OAuth cookie is cleared.
    assert OAUTH_COOKIE_NAME in set_cookie

    # The raw session id must not leak into the URL or the body.
    raw_session_id = client.cookies.get(settings.session_cookie_name)
    assert raw_session_id
    assert raw_session_id not in resp.headers["location"]
    assert raw_session_id not in resp.text
    # The GitHub token must not leak either.
    assert FAKE_TOKEN not in resp.text
    assert FAKE_TOKEN not in caplog.text


def test_same_user_twice_creates_one_user_record(
    client: TestClient, fake_github: dict[str, object]
) -> None:
    state1 = _start_login(client)
    client.get(
        "/api/v1/auth/github/callback",
        params={"code": "c1", "state": state1},
        follow_redirects=False,
    )

    state2 = _start_login(client)
    client.get(
        "/api/v1/auth/github/callback",
        params={"code": "c2", "state": state2},
        follow_redirects=False,
    )

    # Same GitHub user -> a single user record still.
    from app.services.user_store import _USERS_BY_GITHUB_ID

    assert len(_USERS_BY_GITHUB_ID) == 1


# --------------------------------------------------------------------------
# /github/callback - exception mapping
# --------------------------------------------------------------------------

def test_callback_auth_error_returns_401(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def boom(code: str, code_verifier: str | None) -> str:
        raise GitHubAuthError("GitHub rejected the login.")

    monkeypatch.setattr(github_oauth, "exchange_code_for_token", boom)

    state = _start_login(client)
    resp = client.get(
        "/api/v1/auth/github/callback",
        params={"code": "abc", "state": state},
        follow_redirects=False,
    )

    assert resp.status_code == 401


def test_callback_network_error_returns_503(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def boom(code: str, code_verifier: str | None) -> str:
        raise GitHubNetworkError("Could not connect to GitHub.")

    monkeypatch.setattr(github_oauth, "exchange_code_for_token", boom)

    state = _start_login(client)
    resp = client.get(
        "/api/v1/auth/github/callback",
        params={"code": "abc", "state": state},
        follow_redirects=False,
    )

    assert resp.status_code == 503
