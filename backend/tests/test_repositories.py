"""Tests for installation tokens and repository listing/selection.

No real network traffic happens. Github calls go through a small fake httpx
client, and no real installation token is ever created: the fake returns a
distinctive fake token so tests can prove it never leaks.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api import deps
from app.config import get_settings
from app.main import app
from app.security.sessions import get_session_store
from app.services import github_app, github_oauth, github_repositories, user_store
from app.services.github_app import (
    GitHubAPIError,
    GitHubAuthError,
    InstallationUnavailableError,
)
from app.services.store import get_store

FAKE_INSTALLATION_TOKEN = "ghs_FAKE_INSTALLATION_TOKEN_MUST_NOT_LEAK"

ALLOWED_REPO_FIELDS = {
    "id",
    "name",
    "full_name",
    "owner_login",
    "private",
    "archived",
    "default_branch",
    "html_url",
    "language",
    "updated_at",
}

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


# --------------------------------------------------------------------------
# Fixtures + helpers
# --------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_state() -> None:
    """Start every test with empty stores and caches."""

    user_store.reset_store()
    get_session_store().clear()
    github_app.reset_token_cache()
    github_app.reset_app_slug_cache()
    github_repositories.reset_repository_cache()


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict | None, headers: dict):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.headers = headers

    def json(self) -> dict:
        return self._payload


class FakeGitHubHTTP:
    """A controllable stand-in for httpx.AsyncClient."""

    def __init__(self) -> None:
        self.handler = None
        self.calls: list[dict] = []
        self.delay = 0.0

    async def call(self, method, url, params, headers, json_body, data):
        self.calls.append(
            {"method": method, "url": url, "params": dict(params or {})}
        )
        if self.delay:
            await asyncio.sleep(self.delay)
        status_code, payload, extra_headers = self.handler(
            method, url, params or {}, headers or {}, json_body, data
        )
        return _FakeResponse(status_code, payload, extra_headers)

    def count(self, method: str, contains: str) -> int:
        return sum(
            1
            for call in self.calls
            if call["method"] == method and contains in call["url"]
        )


def _iso(seconds: int) -> str:
    return (
        (datetime.now(timezone.utc) + timedelta(seconds=seconds))
        .isoformat()
        .replace("+00:00", "Z")
    )


def _raw_repo(repo_id: int, name: str | None = None) -> dict:
    return {
        "id": repo_id,
        "name": name or f"repo-{repo_id}",
        "full_name": f"octocat/repo-{repo_id}",
        "owner": {"login": "octocat", "type": "User"},
        "private": False,
        "archived": False,
        "default_branch": "main",
        "html_url": f"https://github.com/octocat/repo-{repo_id}",
        "language": "Python",
        "updated_at": "2026-01-01T00:00:00Z",
        # Extra fields that must be dropped.
        "permissions": {"admin": True},
        "fork": False,
        "size": 123,
    }


def _default_handler(token_expires_in: int = 3600):
    def handler(method, url, params, headers, json_body, data):
        if method == "POST" and url.endswith("/access_tokens"):
            return (
                201,
                {"token": FAKE_INSTALLATION_TOKEN, "expires_at": _iso(token_expires_in)},
                {},
            )
        if method == "GET" and "/installation/repositories" in url:
            page = int(params.get("page", 1))
            if page == 1:
                return (200, {"total_count": 1, "repositories": [_raw_repo(1001)]}, {})
            return (200, {"total_count": 1, "repositories": []}, {})
        raise AssertionError(f"unexpected request: {method} {url}")

    return handler


@pytest.fixture()
def gh(monkeypatch: pytest.MonkeyPatch) -> FakeGitHubHTTP:
    controller = FakeGitHubHTTP()
    controller.handler = _default_handler()

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, headers=None, params=None):
            return await controller.call("GET", url, params, headers, None, None)

        async def post(self, url, headers=None, json=None, data=None):
            return await controller.call("POST", url, None, headers, json, data)

    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)
    return controller


def _norm_installation(installation_id: int) -> dict:
    return {
        "installation_id": installation_id,
        "account_login": "octocat",
        "account_id": 999,
        "account_type": "User",
        "target_type": "User",
        "repository_selection": "all",
    }


def _patch_login_github(
    monkeypatch: pytest.MonkeyPatch, user: dict | None = None
) -> None:
    async def fake_exchange(code: str, code_verifier: str | None) -> str:
        return "gho_FAKE_LOGIN_TOKEN"

    async def fake_user(access_token: str) -> dict:
        return dict(user or GITHUB_USER)

    monkeypatch.setattr(github_oauth, "exchange_code_for_token", fake_exchange)
    monkeypatch.setattr(github_oauth, "fetch_github_user", fake_user)


def _patch_installations(
    monkeypatch: pytest.MonkeyPatch, installations: list[dict]
) -> None:
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


def _user_id(client: TestClient) -> int:
    return client.get("/api/v1/auth/me").json()["user"]["id"]


def _login_and_connect(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, installation_id: int
) -> str:
    _patch_login_github(monkeypatch)
    _patch_installations(monkeypatch, [_norm_installation(installation_id)])
    _complete_login(client)
    csrf = _csrf(client)
    resp = client.post(
        f"/api/v1/installations/{installation_id}/connect",
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 200
    return csrf


# --------------------------------------------------------------------------
# Installation token cache (service level)
# --------------------------------------------------------------------------

def test_token_cache_reuses_valid_token(gh: FakeGitHubHTTP) -> None:
    async def run():
        first = await github_app.get_installation_token(1)
        second = await github_app.get_installation_token(1)
        return first, second

    first, second = asyncio.run(run())

    assert first == second == FAKE_INSTALLATION_TOKEN
    assert gh.count("POST", "/access_tokens") == 1


def test_token_with_little_time_left_is_refreshed(gh: FakeGitHubHTTP) -> None:
    gh.handler = _default_handler(token_expires_in=60)  # less than 5 minutes

    async def run():
        await github_app.get_installation_token(1)
        await github_app.get_installation_token(1)

    asyncio.run(run())
    assert gh.count("POST", "/access_tokens") == 2


def test_concurrent_calls_fetch_one_token(gh: FakeGitHubHTTP) -> None:
    gh.delay = 0.01  # keep the call in-flight so coroutines overlap

    async def run():
        return await asyncio.gather(
            *[github_app.get_installation_token(1) for _ in range(5)]
        )

    tokens = asyncio.run(run())

    assert all(token == FAKE_INSTALLATION_TOKEN for token in tokens)
    assert gh.count("POST", "/access_tokens") == 1


def test_token_404_and_403_raise_installation_unavailable(
    gh: FakeGitHubHTTP,
) -> None:
    gh.handler = lambda *args: (404, {"message": "Not Found"}, {})

    async def run404():
        with pytest.raises(InstallationUnavailableError):
            await github_app.get_installation_token(1)

    asyncio.run(run404())

    gh.handler = lambda *args: (403, {"message": "Forbidden"}, {})

    async def run403():
        with pytest.raises(InstallationUnavailableError):
            await github_app.get_installation_token(2)

    asyncio.run(run403())


# --------------------------------------------------------------------------
# Repository service
# --------------------------------------------------------------------------

def test_repository_401_invalidates_and_retries_once(
    gh: FakeGitHubHTTP,
) -> None:
    attempts = {"n": 0}

    def handler(method, url, params, headers, json_body, data):
        if method == "POST" and url.endswith("/access_tokens"):
            return (201, {"token": FAKE_INSTALLATION_TOKEN, "expires_at": _iso(3600)}, {})
        attempts["n"] += 1
        if attempts["n"] == 1:
            return (401, {}, {})
        return (200, {"total_count": 1, "repositories": [_raw_repo(1001)]}, {})

    gh.handler = handler
    repos = asyncio.run(github_repositories.list_installation_repositories(5, True))

    assert [repo["id"] for repo in repos] == [1001]
    assert attempts["n"] == 2
    assert gh.count("POST", "/access_tokens") == 2  # original + refreshed


def test_repository_second_401_fails_safely(gh: FakeGitHubHTTP) -> None:
    calls = {"n": 0}

    def handler(method, url, params, headers, json_body, data):
        if method == "POST" and url.endswith("/access_tokens"):
            return (201, {"token": FAKE_INSTALLATION_TOKEN, "expires_at": _iso(3600)}, {})
        calls["n"] += 1
        return (401, {}, {})

    gh.handler = handler

    async def run():
        with pytest.raises(GitHubAuthError):
            await github_repositories.list_installation_repositories(5, True)

    asyncio.run(run())
    assert calls["n"] == 2  # retried exactly once


def test_repository_pagination_combines_pages(gh: FakeGitHubHTTP) -> None:
    pages = {
        1: {"total_count": 3, "repositories": [_raw_repo(1), _raw_repo(2)]},
        2: {"total_count": 3, "repositories": [_raw_repo(3)]},
    }

    def handler(method, url, params, headers, json_body, data):
        if method == "POST" and url.endswith("/access_tokens"):
            return (201, {"token": FAKE_INSTALLATION_TOKEN, "expires_at": _iso(3600)}, {})
        return (200, pages[int(params["page"])], {})

    gh.handler = handler
    repos = asyncio.run(github_repositories.list_installation_repositories(9, True))

    assert [repo["id"] for repo in repos] == [1, 2, 3]
    pages_requested = [
        c["params"]["page"]
        for c in gh.calls
        if "/installation/repositories" in c["url"]
    ]
    assert pages_requested == [1, 2]


def test_repository_pagination_stops_at_cap(gh: FakeGitHubHTTP) -> None:
    def handler(method, url, params, headers, json_body, data):
        if method == "POST" and url.endswith("/access_tokens"):
            return (201, {"token": FAKE_INSTALLATION_TOKEN, "expires_at": _iso(3600)}, {})
        page = int(params["page"])
        items = [_raw_repo(page * 1000 + i) for i in range(100)]
        return (200, {"total_count": 1_000_000, "repositories": items}, {})

    gh.handler = handler
    repos = asyncio.run(github_repositories.list_installation_repositories(9, True))

    assert len(repos) == 1000
    assert gh.count("GET", "/installation/repositories") == github_repositories.MAX_REPOSITORY_PAGES == 10


def test_repository_returns_only_allowed_fields(gh: FakeGitHubHTTP) -> None:
    repos = asyncio.run(github_repositories.list_installation_repositories(3, True))
    assert set(repos[0].keys()) == ALLOWED_REPO_FIELDS
    assert repos[0]["owner_login"] == "octocat"


def test_repository_result_cache_and_force_refresh(gh: FakeGitHubHTTP) -> None:
    first = asyncio.run(github_repositories.list_installation_repositories(7))
    second = asyncio.run(github_repositories.list_installation_repositories(7))
    assert [r["id"] for r in first] == [r["id"] for r in second] == [1001]
    # Second call came from cache: only one repository fetch so far.
    assert gh.count("GET", "/installation/repositories") == 1

    asyncio.run(github_repositories.list_installation_repositories(7, force_refresh=True))
    assert gh.count("GET", "/installation/repositories") == 2


def test_token_never_appears_in_logs(
    gh: FakeGitHubHTTP, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level("DEBUG"):
        asyncio.run(github_repositories.list_installation_repositories(4, True))
    assert FAKE_INSTALLATION_TOKEN not in caplog.text


# --------------------------------------------------------------------------
# GET /api/v1/repositories
# --------------------------------------------------------------------------

def test_repositories_requires_session(client: TestClient) -> None:
    assert client.get("/api/v1/repositories").status_code == 401


def test_repositories_409_without_connection(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, gh: FakeGitHubHTTP
) -> None:
    _patch_login_github(monkeypatch)
    _patch_installations(monkeypatch, [_norm_installation(111)])
    _complete_login(client)  # logged in but nothing connected

    resp = client.get("/api/v1/repositories")
    assert resp.status_code == 409
    assert resp.json()["detail"] == "No GitHub installation connected"


def test_repositories_success_and_selection(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, gh: FakeGitHubHTTP
) -> None:
    csrf = _login_and_connect(client, monkeypatch, 111)

    def handler(method, url, params, headers, json_body, data):
        if method == "POST" and url.endswith("/access_tokens"):
            return (201, {"token": FAKE_INSTALLATION_TOKEN, "expires_at": _iso(3600)}, {})
        return (
            200,
            {"total_count": 2, "repositories": [_raw_repo(1001), _raw_repo(1002)]},
            {},
        )

    gh.handler = handler

    resp = client.get("/api/v1/repositories")
    assert resp.status_code == 200
    body = resp.json()
    assert body["installation_id"] == 111
    assert body["total"] == 2
    assert [r["selected"] for r in body["repositories"]] == [False, False]
    assert resp.headers["cache-control"] == "no-store"
    assert FAKE_INSTALLATION_TOKEN not in resp.text

    # Select one repository.
    selected = client.post(
        "/api/v1/repositories/select",
        json={"repository_ids": [1001]},
        headers={"X-CSRF-Token": csrf},
    )
    assert selected.status_code == 200
    assert selected.json()["selected_repository_ids"] == [1001]

    after = client.get("/api/v1/repositories").json()
    flags = {r["id"]: r["selected"] for r in after["repositories"]}
    assert flags == {1001: True, 1002: False}


def test_repositories_installation_unavailable_clears(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, gh: FakeGitHubHTTP
) -> None:
    _login_and_connect(client, monkeypatch, 111)
    user_id = _user_id(client)
    asyncio.run(get_store().set_selected_repositories(user_id, 111, [1001]))

    # A 404 from the token endpoint means the installation is gone.
    gh.handler = lambda *args: (404, {"message": "Not Found"}, {})

    resp = client.get("/api/v1/repositories")
    assert resp.status_code == 409
    assert resp.json()["detail"] == "Installation is no longer available. Please reconnect."

    # Connection and selections are cleared.
    assert asyncio.run(get_store().get_connected_installation(user_id)) is None
    assert (
        asyncio.run(get_store().get_selected_repositories(user_id, 111)) == []
    )


def test_repositories_rate_limit_returns_502(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, gh: FakeGitHubHTTP
) -> None:
    _login_and_connect(client, monkeypatch, 111)

    def handler(method, url, params, headers, json_body, data):
        if method == "POST" and url.endswith("/access_tokens"):
            return (201, {"token": FAKE_INSTALLATION_TOKEN, "expires_at": _iso(3600)}, {})
        return (403, {}, {"x-ratelimit-remaining": "0"})

    gh.handler = handler
    resp = client.get("/api/v1/repositories")
    assert resp.status_code == 502
    assert FAKE_INSTALLATION_TOKEN not in resp.text


def test_rate_limit_maps_to_api_error(gh: FakeGitHubHTTP) -> None:
    def handler(method, url, params, headers, json_body, data):
        if method == "POST" and url.endswith("/access_tokens"):
            return (201, {"token": FAKE_INSTALLATION_TOKEN, "expires_at": _iso(3600)}, {})
        return (429, {}, {"retry-after": "30"})

    gh.handler = handler

    async def run():
        with pytest.raises(GitHubAPIError) as excinfo:
            await github_repositories.list_installation_repositories(6, True)
        return str(excinfo.value)

    message = asyncio.run(run())
    assert "rate limit" in message.lower()
    assert FAKE_INSTALLATION_TOKEN not in message


def test_repositories_network_error_returns_503(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, gh: FakeGitHubHTTP
) -> None:
    _login_and_connect(client, monkeypatch, 111)

    def handler(method, url, params, headers, json_body, data):
        if method == "POST" and url.endswith("/access_tokens"):
            return (201, {"token": FAKE_INSTALLATION_TOKEN, "expires_at": _iso(3600)}, {})
        raise httpx.ConnectError("boom")

    gh.handler = handler
    resp = client.get("/api/v1/repositories")
    assert resp.status_code == 503


# --------------------------------------------------------------------------
# POST /api/v1/repositories/select
# --------------------------------------------------------------------------

def test_select_without_csrf_returns_403(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, gh: FakeGitHubHTTP
) -> None:
    _login_and_connect(client, monkeypatch, 111)
    resp = client.post("/api/v1/repositories/select", json={"repository_ids": [1001]})
    assert resp.status_code == 403


def test_select_unavailable_id_returns_422_and_stores_nothing(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, gh: FakeGitHubHTTP
) -> None:
    csrf = _login_and_connect(client, monkeypatch, 111)
    user_id = _user_id(client)

    resp = client.post(
        "/api/v1/repositories/select",
        json={"repository_ids": [9999]},
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 422
    assert resp.json()["detail"] == "One or more repositories are not accessible"
    assert (
        asyncio.run(get_store().get_selected_repositories(user_id, 111)) == []
    )


def test_select_non_integer_ids_returns_422(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, gh: FakeGitHubHTTP
) -> None:
    csrf = _login_and_connect(client, monkeypatch, 111)
    resp = client.post(
        "/api/v1/repositories/select",
        json={"repository_ids": ["abc"]},
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 422


def test_select_empty_list_clears(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, gh: FakeGitHubHTTP
) -> None:
    csrf = _login_and_connect(client, monkeypatch, 111)

    client.post(
        "/api/v1/repositories/select",
        json={"repository_ids": [1001]},
        headers={"X-CSRF-Token": csrf},
    )
    cleared = client.post(
        "/api/v1/repositories/select",
        json={"repository_ids": []},
        headers={"X-CSRF-Token": csrf},
    )
    assert cleared.status_code == 200
    assert cleared.json()["selected_repository_ids"] == []

    after = client.get("/api/v1/repositories").json()
    assert all(r["selected"] is False for r in after["repositories"])


def test_selections_isolated_between_users(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, gh: FakeGitHubHTTP
) -> None:
    # User 1 selects a repository.
    csrf1 = _login_and_connect(client, monkeypatch, 111)
    user1 = _user_id(client)
    client.post(
        "/api/v1/repositories/select",
        json={"repository_ids": [1001]},
        headers={"X-CSRF-Token": csrf1},
    )

    # User 2 is a separate client/account with their own installation.
    other = TestClient(app)
    _patch_login_github(monkeypatch, user=SECOND_USER)
    _patch_installations(monkeypatch, [_norm_installation(222)])
    _complete_login(other)
    user2 = _user_id(other)
    csrf2 = _csrf(other)
    other.post(
        "/api/v1/installations/222/connect",
        headers={"X-CSRF-Token": csrf2},
    )

    assert (
        asyncio.run(get_store().get_selected_repositories(user1, 111)) == [1001]
    )
    assert asyncio.run(get_store().get_selected_repositories(user2, 222)) == []

    body = other.get("/api/v1/repositories").json()
    assert all(r["selected"] is False for r in body["repositories"])


def test_disconnect_clears_selections(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, gh: FakeGitHubHTTP
) -> None:
    csrf = _login_and_connect(client, monkeypatch, 111)
    user_id = _user_id(client)
    client.post(
        "/api/v1/repositories/select",
        json={"repository_ids": [1001]},
        headers={"X-CSRF-Token": csrf},
    )
    assert (
        asyncio.run(get_store().get_selected_repositories(user_id, 111))
        == [1001]
    )

    client.post("/api/v1/installations/disconnect", headers={"X-CSRF-Token": csrf})
    assert (
        asyncio.run(get_store().get_selected_repositories(user_id, 111)) == []
    )


def test_changing_installation_clears_selections(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, gh: FakeGitHubHTTP
) -> None:
    _patch_login_github(monkeypatch)
    _patch_installations(
        monkeypatch, [_norm_installation(111), _norm_installation(222)]
    )
    _complete_login(client)
    csrf = _csrf(client)
    user_id = _user_id(client)

    client.post("/api/v1/installations/111/connect", headers={"X-CSRF-Token": csrf})
    client.post(
        "/api/v1/repositories/select",
        json={"repository_ids": [1001]},
        headers={"X-CSRF-Token": csrf},
    )
    assert (
        asyncio.run(get_store().get_selected_repositories(user_id, 111))
        == [1001]
    )

    # Switch to the other installation: selections for 111 are cleared.
    client.post("/api/v1/installations/222/connect", headers={"X-CSRF-Token": csrf})
    assert (
        asyncio.run(get_store().get_selected_repositories(user_id, 111)) == []
    )
    assert asyncio.run(get_store().get_connected_installation(user_id)) == 222


# --------------------------------------------------------------------------
# CSRF origin handling
# --------------------------------------------------------------------------

def test_csrf_accepts_backend_origin_in_development(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, gh: FakeGitHubHTTP
) -> None:
    _login_and_connect(client, monkeypatch, 111)
    csrf = _csrf(client)
    backend_origin = get_settings().backend_origin

    resp = client.post(
        "/api/v1/auth/logout",
        headers={"X-CSRF-Token": csrf, "Origin": backend_origin},
    )
    assert resp.status_code == 200


def test_csrf_rejects_random_origin(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, gh: FakeGitHubHTTP
) -> None:
    _login_and_connect(client, monkeypatch, 111)
    csrf = _csrf(client)

    resp = client.post(
        "/api/v1/auth/logout",
        headers={"X-CSRF-Token": csrf, "Origin": "http://evil.example"},
    )
    assert resp.status_code == 403


def test_csrf_rejects_backend_origin_in_production(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, gh: FakeGitHubHTTP
) -> None:
    _login_and_connect(client, monkeypatch, 111)
    csrf = _csrf(client)

    production = replace(get_settings(), app_env="production")
    monkeypatch.setattr(deps, "get_settings", lambda: production)

    resp = client.post(
        "/api/v1/auth/logout",
        headers={
            "X-CSRF-Token": csrf,
            "Origin": production.backend_origin,
        },
    )
    assert resp.status_code == 403
