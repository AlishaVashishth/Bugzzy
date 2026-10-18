"""Tests for installation discovery and linking.

Every GitHub call is monkeypatched; no real network traffic happens. For
the pagination and app_id filtering we replace the httpx client with a
small fake so the real fetch_user_installations code still runs.
"""

from __future__ import annotations

import asyncio
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import app
from app.security.sessions import get_session_store
from app.services import github_app, github_oauth
from app.services.github_app import (
    GitHubAPIError,
    GitHubAuthError,
    GitHubNetworkError,
)
from app.services.user_store import reset_store

FAKE_TOKEN = "gho_FAKE_TOKEN_SHOULD_NOT_LEAK"

APP_ID = get_settings().app_id
OTHER_APP_ID = APP_ID + 1

GITHUB_USER = {
    "id": 4242,
    "login": "octocat",
    "name": "The Octocat",
    "avatar_url": "https://example.invalid/avatar.png",
}

SECOND_USER = {
    "id": 5151,
    "login": "second-user",
    "name": "Second User",
    "avatar_url": "https://example.invalid/second.png",
}

CONNECTED_COOKIE = get_settings().session_cookie_name


@pytest.fixture(autouse=True)
def reset_caches() -> None:
    """Clear stores and the cached app slug before every test."""

    reset_store()
    get_session_store().clear()
    github_app.reset_app_slug_cache()


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _norm_installation(installation_id: int, login: str = "octocat") -> dict:
    """The shape stored in the user store (already filtered)."""

    return {
        "installation_id": installation_id,
        "account_login": login,
        "account_id": 999,
        "account_type": "User",
        "target_type": "User",
        "repository_selection": "all",
    }


def _raw_installation(
    installation_id: int, app_id: int = APP_ID, login: str = "octocat"
) -> dict:
    """The raw shape GitHub returns from /user/installations."""

    return {
        "id": installation_id,
        "app_id": app_id,
        "app_slug": "bugzzy",
        "target_id": 1,
        "target_type": "User",
        "repository_selection": "all",
        "account": {
            "login": login,
            "id": 999,
            "type": "User",
            "avatar_url": "https://example.invalid/a.png",
        },
    }


def _patch_login_github(monkeypatch: pytest.MonkeyPatch, user: dict | None = None) -> None:
    """Replace token exchange + user fetch so login needs no network."""

    async def fake_exchange(code: str, code_verifier: str | None) -> str:
        return FAKE_TOKEN

    async def fake_user(access_token: str) -> dict:
        return dict(user or GITHUB_USER)

    monkeypatch.setattr(github_oauth, "exchange_code_for_token", fake_exchange)
    monkeypatch.setattr(github_oauth, "fetch_github_user", fake_user)


def _patch_installations(
    monkeypatch: pytest.MonkeyPatch, installations: list[dict]
) -> None:
    """Make fetch_user_installations return a fixed (already filtered) list."""

    async def fake(access_token: str) -> list[dict]:
        return [dict(item) for item in installations]

    monkeypatch.setattr(github_oauth, "fetch_user_installations", fake)


def _complete_login(client: TestClient) -> None:
    login = client.get("/api/v1/auth/github/login", follow_redirects=False)
    state = parse_qs(urlparse(login.headers["location"]).query)["state"][0]
    callback = client.get(
        "/api/v1/auth/github/callback",
        params={"code": "the-code", "state": state},
        follow_redirects=False,
    )
    assert callback.status_code == 302


def _csrf(client: TestClient) -> str:
    return client.get("/api/v1/auth/me").json()["csrf_token"]


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict:
        return self._payload


def _install_http_mock(monkeypatch: pytest.MonkeyPatch, handler) -> list[dict]:
    """Replace httpx.AsyncClient used by github_oauth with a fake.

    handler(url, params) -> (status_code, payload). Returns a list that
    records the params of every request so tests can count pages.
    """

    calls: list[dict] = []

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, headers=None, params=None):
            params = dict(params or {})
            calls.append(params)
            status_code, payload = handler(url, params)
            return _FakeResponse(status_code, payload)

        async def post(self, *args, **kwargs):
            raise AssertionError("no POST expected in this test")

    monkeypatch.setattr(github_oauth.httpx, "AsyncClient", FakeAsyncClient)
    return calls


# --------------------------------------------------------------------------
# Auth guards
# --------------------------------------------------------------------------

def test_endpoints_without_session_return_401(client: TestClient) -> None:
    assert client.get("/api/v1/installations").status_code == 401
    assert client.get("/api/v1/installations/install-url").status_code == 401
    assert client.post("/api/v1/installations/123/connect").status_code == 401
    assert client.post("/api/v1/installations/disconnect").status_code == 401


def test_post_endpoints_without_csrf_return_403(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_login_github(monkeypatch)
    _patch_installations(monkeypatch, [_norm_installation(555)])
    _complete_login(client)

    # No X-CSRF-Token header -> blocked before the handler runs.
    assert client.post("/api/v1/installations/555/connect").status_code == 403
    assert client.post("/api/v1/installations/disconnect").status_code == 403


# --------------------------------------------------------------------------
# Listing + filtering
# --------------------------------------------------------------------------

def test_login_filters_out_other_apps(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_login_github(monkeypatch)

    def handler(url: str, params: dict):
        return (
            200,
            {
                "total_count": 2,
                "installations": [
                    _raw_installation(111, app_id=APP_ID),
                    _raw_installation(222, app_id=OTHER_APP_ID),
                ],
            },
        )

    _install_http_mock(monkeypatch, handler)
    _complete_login(client)

    resp = client.get("/api/v1/installations")
    assert resp.status_code == 200
    body = resp.json()
    ids = [item["installation_id"] for item in body["installations"]]
    # Only our app's installation is kept.
    assert ids == [111]
    assert body["connected_installation_id"] is None
    assert resp.headers["cache-control"] == "no-store"


def test_pagination_combines_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    pages = {
        1: {
            "total_count": 3,
            "installations": [
                _raw_installation(1, app_id=APP_ID),
                _raw_installation(2, app_id=OTHER_APP_ID),
            ],
        },
        2: {
            "total_count": 3,
            "installations": [_raw_installation(3, app_id=APP_ID)],
        },
    }

    def handler(url: str, params: dict):
        return (200, pages[int(params["page"])])

    calls = _install_http_mock(monkeypatch, handler)

    result = asyncio.run(github_oauth.fetch_user_installations(FAKE_TOKEN))

    ids = sorted(item["installation_id"] for item in result)
    assert ids == [1, 3]  # app_id 2 filtered out
    assert [c["page"] for c in calls] == [1, 2]


def test_pagination_stops_at_page_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(url: str, params: dict):
        page = int(params["page"])
        items = [
            _raw_installation(page * 1000 + i, app_id=APP_ID) for i in range(100)
        ]
        return (200, {"total_count": 1_000_000, "installations": items})

    calls = _install_http_mock(monkeypatch, handler)

    result = asyncio.run(github_oauth.fetch_user_installations(FAKE_TOKEN))

    # 10 pages x 100 items, and no more requests than the cap.
    assert len(result) == 1000
    assert len(calls) == github_oauth.MAX_INSTALLATION_PAGES == 10


# --------------------------------------------------------------------------
# Connect / disconnect
# --------------------------------------------------------------------------

def test_connect_flow(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_login_github(monkeypatch)
    _patch_installations(
        monkeypatch,
        [
            _norm_installation(111, login="octocat"),
            _norm_installation(222, login="octocat-org"),
        ],
    )
    _complete_login(client)
    csrf = _csrf(client)

    # Nothing connected yet (one installation is NOT auto-connected).
    assert client.get("/api/v1/installations").json()["connected_installation_id"] is None

    # Connect a valid id.
    resp = client.post(
        "/api/v1/installations/111/connect",
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 200
    assert resp.json()["connected_installation_id"] == 111

    # It now shows as connected.
    assert client.get("/api/v1/installations").json()["connected_installation_id"] == 111

    # An id not in the list is rejected with 404.
    bad = client.post(
        "/api/v1/installations/999/connect",
        headers={"X-CSRF-Token": csrf},
    )
    assert bad.status_code == 404
    assert bad.json()["detail"] == "Installation not found"

    # A non-integer id is a validation error.
    malformed = client.post(
        "/api/v1/installations/abc/connect",
        headers={"X-CSRF-Token": csrf},
    )
    assert malformed.status_code == 422


def test_disconnect_clears_connection(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_login_github(monkeypatch)
    _patch_installations(monkeypatch, [_norm_installation(111)])
    _complete_login(client)
    csrf = _csrf(client)

    # Even with exactly one installation, nothing is auto-connected.
    assert client.get("/api/v1/installations").json()["connected_installation_id"] is None

    client.post("/api/v1/installations/111/connect", headers={"X-CSRF-Token": csrf})
    assert client.get("/api/v1/installations").json()["connected_installation_id"] == 111

    resp = client.post("/api/v1/installations/disconnect", headers={"X-CSRF-Token": csrf})
    assert resp.status_code == 200
    assert resp.json()["connected_installation_id"] is None
    assert client.get("/api/v1/installations").json()["connected_installation_id"] is None

    # Disconnecting again still succeeds.
    again = client.post("/api/v1/installations/disconnect", headers={"X-CSRF-Token": csrf})
    assert again.status_code == 200


def test_other_user_cannot_connect(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # User 1 logs in and has installation 111.
    _patch_login_github(monkeypatch, user=GITHUB_USER)
    _patch_installations(monkeypatch, [_norm_installation(111)])
    _complete_login(client)

    # User 2 is a completely different client with no installations.
    other = TestClient(app)
    _patch_login_github(monkeypatch, user=SECOND_USER)
    _patch_installations(monkeypatch, [])
    _complete_login(other)
    other_csrf = _csrf(other)

    assert other.get("/api/v1/installations").json()["installations"] == []

    resp = other.post(
        "/api/v1/installations/111/connect",
        headers={"X-CSRF-Token": other_csrf},
    )
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Installation not found"


# --------------------------------------------------------------------------
# Login resilience + refresh semantics
# --------------------------------------------------------------------------

def test_login_succeeds_when_installations_fail(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _patch_login_github(monkeypatch)

    async def boom(access_token: str) -> list[dict]:
        raise GitHubNetworkError("Could not connect to GitHub.")

    monkeypatch.setattr(github_oauth, "fetch_user_installations", boom)

    # The callback still succeeds.
    _complete_login(client)
    assert client.get("/api/v1/installations").json()["installations"] == []

    # The warning is safe: no token appears anywhere.
    assert FAKE_TOKEN not in caplog.text


def test_relogin_clears_connection_that_disappeared(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_login_github(monkeypatch)
    _patch_installations(monkeypatch, [_norm_installation(111)])
    _complete_login(client)
    csrf = _csrf(client)
    client.post("/api/v1/installations/111/connect", headers={"X-CSRF-Token": csrf})
    assert client.get("/api/v1/installations").json()["connected_installation_id"] == 111

    # Re-login with a list that no longer includes 111.
    _patch_installations(monkeypatch, [_norm_installation(222)])
    _complete_login(client)

    body = client.get("/api/v1/installations").json()
    assert body["connected_installation_id"] is None
    assert [i["installation_id"] for i in body["installations"]] == [222]


# --------------------------------------------------------------------------
# Post-install redirect handling
# --------------------------------------------------------------------------

def test_post_install_callback_redirects_and_skips_exchange(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    exchanged = {"called": False}

    async def spy_exchange(code: str, code_verifier: str | None) -> str:
        exchanged["called"] = True
        return FAKE_TOKEN

    monkeypatch.setattr(github_oauth, "exchange_code_for_token", spy_exchange)

    resp = client.get(
        "/api/v1/auth/github/callback",
        params={
            "code": "post-install-code",
            "setup_action": "install",
            "installation_id": "12345",
        },
        follow_redirects=False,
    )

    assert resp.status_code == 302
    assert resp.headers["location"] == "/api/v1/auth/github/login"
    # The untrusted code was never exchanged.
    assert exchanged["called"] is False


# --------------------------------------------------------------------------
# Install URL
# --------------------------------------------------------------------------

def test_install_url_uses_app_slug(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_login_github(monkeypatch)
    _patch_installations(monkeypatch, [])

    async def fake_app_info() -> dict:
        return {"slug": "bugzzy-pr-debugger-assistant"}

    monkeypatch.setattr(github_app, "get_app_info", fake_app_info)

    _complete_login(client)
    resp = client.get("/api/v1/installations/install-url")

    assert resp.status_code == 200
    assert resp.json()["url"] == (
        "https://github.com/apps/bugzzy-pr-debugger-assistant/installations/new"
    )


def test_install_url_handles_github_failure(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_login_github(monkeypatch)
    _patch_installations(monkeypatch, [])
    _complete_login(client)

    async def failing_app_info() -> dict:
        raise GitHubAPIError("boom")

    monkeypatch.setattr(github_app, "get_app_info", failing_app_info)
    github_app.reset_app_slug_cache()

    resp = client.get("/api/v1/installations/install-url")
    assert resp.status_code == 502
    assert "boom" not in resp.text  # safe, generic message

    async def failing_network() -> dict:
        raise GitHubNetworkError("timeout")

    monkeypatch.setattr(github_app, "get_app_info", failing_network)
    github_app.reset_app_slug_cache()
    assert client.get("/api/v1/installations/install-url").status_code == 503


def test_no_token_in_responses_or_logs(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _patch_login_github(monkeypatch)
    _patch_installations(monkeypatch, [_norm_installation(111)])

    with caplog.at_level("DEBUG"):
        _complete_login(client)
        csrf = _csrf(client)
        client.post("/api/v1/installations/111/connect", headers={"X-CSRF-Token": csrf})
        listing = client.get("/api/v1/installations")

    assert FAKE_TOKEN not in listing.text
    assert FAKE_TOKEN not in caplog.text
