"""Tests for Bugzzy sessions, /auth/me, logout, CSRF, expiry and CORS.

No real network calls are made: the GitHub functions are monkeypatched.
"""

from __future__ import annotations

import hashlib
import time
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import app
from app.security.sessions import get_session_store
from app.services import github_oauth
from app.services.user_store import reset_store

# A fake GitHub token with a distinctive value so leaks are easy to spot.
FAKE_TOKEN = "gho_FAKE_TOKEN_SHOULD_NOT_LEAK"

GITHUB_USER = {
    "id": 4242,
    "login": "octocat",
    "name": "The Octocat",
    "avatar_url": "https://example.invalid/avatar.png",
}


@pytest.fixture()
def client() -> TestClient:
    """A fresh client and empty stores for each test."""

    reset_store()
    get_session_store().clear()
    return TestClient(app)


@pytest.fixture()
def fake_github(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the real GitHub calls so no network traffic happens."""

    async def fake_exchange(code: str, code_verifier: str | None) -> str:
        return FAKE_TOKEN

    async def fake_fetch(access_token: str) -> dict[str, object]:
        return dict(GITHUB_USER)

    monkeypatch.setattr(github_oauth, "exchange_code_for_token", fake_exchange)
    monkeypatch.setattr(github_oauth, "fetch_github_user", fake_fetch)


def _complete_login(client: TestClient) -> None:
    """Run login -> callback so the client gets a real session cookie."""

    login = client.get("/api/v1/auth/github/login", follow_redirects=False)
    state = parse_qs(urlparse(login.headers["location"]).query)["state"][0]
    callback = client.get(
        "/api/v1/auth/github/callback",
        params={"code": "the-code", "state": state},
        follow_redirects=False,
    )
    assert callback.status_code == 302


def _session_key(raw_session_id: str) -> str:
    """Reproduce the store's hashing so tests can find the session."""

    return hashlib.sha256(raw_session_id.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# /auth/me basics
# --------------------------------------------------------------------------

def test_me_without_cookie_returns_401(client: TestClient) -> None:
    resp = client.get("/api/v1/auth/me")
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Not authenticated"


def test_me_with_forged_cookie_returns_401(client: TestClient) -> None:
    settings = get_settings()
    client.cookies.set(settings.session_cookie_name, "totally-made-up-session")
    resp = client.get("/api/v1/auth/me")
    assert resp.status_code == 401


def test_me_with_random_cookie_returns_401(client: TestClient) -> None:
    settings = get_settings()
    # A long random-looking value that was never issued by us.
    client.cookies.set(settings.session_cookie_name, "a" * 43)
    resp = client.get("/api/v1/auth/me")
    assert resp.status_code == 401


def test_full_flow_me_returns_user_and_csrf(
    client: TestClient, fake_github: None
) -> None:
    _complete_login(client)

    resp = client.get("/api/v1/auth/me")
    assert resp.status_code == 200
    body = resp.json()
    assert body["user"]["username"] == "octocat"
    assert body["user"]["github_user_id"] == 4242
    assert body["csrf_token"]
    # The response must not be cached by browsers or proxies.
    assert resp.headers["cache-control"] == "no-store"


def test_me_survives_repeated_calls(client: TestClient, fake_github: None) -> None:
    _complete_login(client)

    first = client.get("/api/v1/auth/me")
    second = client.get("/api/v1/auth/me")

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["user"] == second.json()["user"]
    # Same session -> same CSRF token both times.
    assert first.json()["csrf_token"] == second.json()["csrf_token"]


# --------------------------------------------------------------------------
# Logout + CSRF
# --------------------------------------------------------------------------

def test_logout_without_csrf_token_returns_403(
    client: TestClient, fake_github: None
) -> None:
    _complete_login(client)

    resp = client.post("/api/v1/auth/logout")
    assert resp.status_code == 403
    assert resp.json()["detail"] == "CSRF check failed"


def test_logout_with_wrong_csrf_token_returns_403(
    client: TestClient, fake_github: None
) -> None:
    _complete_login(client)

    resp = client.post(
        "/api/v1/auth/logout", headers={"X-CSRF-Token": "wrong-token"}
    )
    assert resp.status_code == 403


def test_logout_with_wrong_origin_returns_403(
    client: TestClient, fake_github: None
) -> None:
    _complete_login(client)
    csrf = client.get("/api/v1/auth/me").json()["csrf_token"]

    resp = client.post(
        "/api/v1/auth/logout",
        headers={"X-CSRF-Token": csrf, "Origin": "http://evil.example"},
    )
    assert resp.status_code == 403
    assert resp.json()["detail"] == "CSRF check failed"


def test_logout_happy_path_clears_session(
    client: TestClient, fake_github: None
) -> None:
    settings = get_settings()
    _complete_login(client)

    raw_session_id = client.cookies.get(settings.session_cookie_name)
    assert raw_session_id

    csrf = client.get("/api/v1/auth/me").json()["csrf_token"]
    resp = client.post(
        "/api/v1/auth/logout",
        headers={"X-CSRF-Token": csrf, "Origin": settings.frontend_origin},
    )

    assert resp.status_code == 200
    assert resp.json() == {"message": "Logged out"}

    # The cookie is cleared in the response.
    set_cookie = resp.headers.get("set-cookie", "")
    assert f"{settings.session_cookie_name}=" in set_cookie
    assert "Max-Age=0" in set_cookie or "expires=" in set_cookie.lower()

    # And the session is gone for real.
    assert client.get("/api/v1/auth/me").status_code == 401

    # The old cookie value no longer works even if a browser still sent it.
    stale = TestClient(app)
    stale.cookies.set(settings.session_cookie_name, raw_session_id)
    assert stale.get("/api/v1/auth/me").status_code == 401


def test_logout_with_no_session_returns_401(client: TestClient) -> None:
    resp = client.post("/api/v1/auth/logout")
    assert resp.status_code == 401


# --------------------------------------------------------------------------
# Expiry (no sleeping: timestamps are manipulated directly)
# --------------------------------------------------------------------------

def test_session_expired_by_absolute_lifetime(
    client: TestClient, fake_github: None
) -> None:
    settings = get_settings()
    _complete_login(client)
    raw = client.cookies.get(settings.session_cookie_name)

    store = get_session_store()
    session = store._sessions[_session_key(raw)]
    session.created_at = time.time() - settings.session_max_age_seconds - 10

    assert client.get("/api/v1/auth/me").status_code == 401


def test_session_expired_by_idle_timeout(
    client: TestClient, fake_github: None
) -> None:
    settings = get_settings()
    _complete_login(client)
    raw = client.cookies.get(settings.session_cookie_name)

    store = get_session_store()
    session = store._sessions[_session_key(raw)]
    session.last_seen_at = time.time() - settings.session_idle_timeout_seconds - 10

    assert client.get("/api/v1/auth/me").status_code == 401


# --------------------------------------------------------------------------
# Second login rotates the session
# --------------------------------------------------------------------------

def test_second_login_creates_new_session_and_old_one_dies(
    client: TestClient, fake_github: None
) -> None:
    settings = get_settings()
    _complete_login(client)
    first_raw = client.cookies.get(settings.session_cookie_name)

    _complete_login(client)
    second_raw = client.cookies.get(settings.session_cookie_name)

    assert first_raw != second_raw

    # The old session cookie must no longer work if it is passed in.
    old = TestClient(app)
    old.cookies.set(settings.session_cookie_name, first_raw)
    assert old.get("/api/v1/auth/me").status_code == 401

    # The new one still works.
    assert client.get("/api/v1/auth/me").status_code == 200


# --------------------------------------------------------------------------
# Store integrity
# --------------------------------------------------------------------------

def test_store_keys_are_hashes_not_raw_ids(
    client: TestClient, fake_github: None
) -> None:
    settings = get_settings()
    _complete_login(client)
    raw = client.cookies.get(settings.session_cookie_name)

    store = get_session_store()
    assert raw not in store._sessions
    assert _session_key(raw) in store._sessions


# --------------------------------------------------------------------------
# Protected app-check endpoint
# --------------------------------------------------------------------------

def test_app_check_without_session_returns_401(client: TestClient) -> None:
    resp = client.get("/api/v1/github/app-check")
    assert resp.status_code == 401


def test_app_check_with_session_returns_data(
    client: TestClient, fake_github: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_app_info() -> dict[str, object]:
        return {
            "id": 1,
            "slug": "bugzzy",
            "name": "Bugzzy",
            "owner": {"login": "alisha"},
            "permissions": {"contents": "read"},
            "events": [],
        }

    # main.py imports get_app_info directly, so patch it there.
    monkeypatch.setattr("app.main.get_app_info", fake_app_info)

    _complete_login(client)
    resp = client.get("/api/v1/github/app-check")

    assert resp.status_code == 200
    assert resp.json()["name"] == "Bugzzy"
    assert resp.json()["owner_login"] == "alisha"


# --------------------------------------------------------------------------
# CORS
# --------------------------------------------------------------------------

def test_cors_preflight_allows_frontend_origin(client: TestClient) -> None:
    settings = get_settings()

    resp = client.options(
        "/api/v1/auth/logout",
        headers={
            "Origin": settings.frontend_origin,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "x-csrf-token",
        },
    )

    assert resp.status_code == 200
    assert resp.headers["access-control-allow-origin"] == settings.frontend_origin
    assert resp.headers["access-control-allow-credentials"] == "true"


def test_cors_preflight_rejects_other_origin(client: TestClient) -> None:
    resp = client.options(
        "/api/v1/auth/logout",
        headers={
            "Origin": "http://evil.example",
            "Access-Control-Request-Method": "POST",
        },
    )

    # Starlette rejects a disallowed preflight with 400 and never echoes the
    # foreign origin back as allowed.
    assert resp.status_code == 400
    assert resp.headers.get("access-control-allow-origin") != "http://evil.example"
