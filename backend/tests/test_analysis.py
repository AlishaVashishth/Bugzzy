"""Unit and integration tests for POST /api/v1/analysis/pr."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import MagicMock
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.security.sessions import get_session_store
from app.services import github_app, github_oauth, github_repositories, user_store
from app.services.store import get_store

FAKE_INSTALLATION_TOKEN = "ghs_FAKE_INSTALLATION_TOKEN_MUST_NOT_LEAK"

GITHUB_USER = {
    "id": 4242,
    "login": "octocat",
    "name": "The Octocat",
    "avatar_url": "https://example.invalid/avatar.png",
}


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start every test with empty stores and caches, and clean DB env."""
    user_store.reset_store()
    get_session_store().clear()
    github_app.reset_token_cache()
    github_app.reset_app_slug_cache()
    github_repositories.reset_repository_cache()
    monkeypatch.delenv("DATABASE_URL", raising=False)


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


def _patch_login_github(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_exchange(code: str, code_verifier: str | None) -> str:
        return "gho_FAKE_LOGIN_TOKEN"

    async def fake_user(access_token: str) -> dict:
        return dict(GITHUB_USER)

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


def test_pr_analysis_no_session_cookie(client: TestClient) -> None:
    """a) no session cookie -> 401."""
    resp = client.post(
        "/api/v1/analysis/pr",
        json={"owner": "octocat", "repo": "repo-1001", "pull_number": 42},
    )
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Not authenticated"


def test_pr_analysis_repo_not_selected(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, gh: FakeGitHubHTTP
) -> None:
    """b) logged in, repo NOT selected -> 404."""
    installation_id = 12345
    csrf = _login_and_connect(client, monkeypatch, installation_id)
    asyncio.run(get_store().set_selected_repositories(_user_id(client), installation_id, []))

    resp = client.post(
        "/api/v1/analysis/pr",
        json={"owner": "octocat", "repo": "repo-1001", "pull_number": 42},
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Repository not found"


def test_pr_analysis_repo_selected(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, gh: FakeGitHubHTTP
) -> None:
    """c) logged in, repo selected -> 200."""
    installation_id = 12345
    csrf = _login_and_connect(client, monkeypatch, installation_id)
    asyncio.run(get_store().set_selected_repositories(_user_id(client), installation_id, [1001]))

    mock_context = MagicMock()
    mock_result = MagicMock()
    expected_payload = {
        "verified_findings": [],
        "ai_suggestions": [],
        "limitations": [],
    }
    mock_result.model_dump.return_value = expected_payload

    called_args: dict = {}

    def fake_build_context(owner: str, repo: str, pull_number: int, github_token: str | None, caller_index=None):
        called_args["build_context"] = (owner, repo, pull_number, github_token)
        return mock_context

    def fake_localize_bugs(context):
        called_args["localize_bugs"] = context
        return mock_result

    monkeypatch.setattr("app.api.routes.analysis.build_context", fake_build_context)
    monkeypatch.setattr("app.api.routes.analysis.localize_bugs", fake_localize_bugs)

    resp = client.post(
        "/api/v1/analysis/pr",
        json={"owner": "octocat", "repo": "repo-1001", "pull_number": 42},
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 200
    assert resp.json() == expected_payload
    assert called_args["build_context"] == (
        "octocat",
        "repo-1001",
        42,
        FAKE_INSTALLATION_TOKEN,
    )
    assert called_args["localize_bugs"] == mock_context


def test_pr_analysis_without_csrf_token(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, gh: FakeGitHubHTTP
) -> None:
    """d) POST without CSRF token -> 403."""
    installation_id = 12345
    _login_and_connect(client, monkeypatch, installation_id)
    asyncio.run(get_store().set_selected_repositories(_user_id(client), installation_id, [1001]))

    resp = client.post(
        "/api/v1/analysis/pr",
        json={"owner": "octocat", "repo": "repo-1001", "pull_number": 42},
        # Omitting X-CSRF-Token header
    )
    assert resp.status_code == 403
    assert resp.json()["detail"] == "CSRF check failed"


def test_pr_analysis_invalid_pull_number(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, gh: FakeGitHubHTTP
) -> None:
    """Validation: pull_number must be gt=0 -> 422."""
    installation_id = 12345
    csrf = _login_and_connect(client, monkeypatch, installation_id)

    resp = client.post(
        "/api/v1/analysis/pr",
        json={"owner": "octocat", "repo": "repo-1001", "pull_number": 0},
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 422


def test_pr_analysis_github_error_returns_502(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, gh: FakeGitHubHTTP
) -> None:
    """Errors: GitHubAPIError -> 502 with generic message."""
    installation_id = 12345
    csrf = _login_and_connect(client, monkeypatch, installation_id)
    asyncio.run(get_store().set_selected_repositories(_user_id(client), installation_id, [1001]))

    from pr_context_builder.exceptions import GitHubAPIError as EngineGitHubAPIError

    def fake_build_context(owner: str, repo: str, pull_number: int, github_token: str | None, caller_index=None):
        raise EngineGitHubAPIError("Authoritative PR fetch failed", status_code=500)

    monkeypatch.setattr("app.api.routes.analysis.build_context", fake_build_context)

    resp = client.post(
        "/api/v1/analysis/pr",
        json={"owner": "octocat", "repo": "repo-1001", "pull_number": 42},
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 502
    assert resp.json()["detail"] == "GitHub returned an error while processing the request"


def test_pr_analysis_passes_caller_index_when_database_url_set(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, gh: FakeGitHubHTTP
) -> None:
    """When DATABASE_URL is set, PostgresCallerIndex is created and passed to build_context."""
    installation_id = 12345
    csrf = _login_and_connect(client, monkeypatch, installation_id)
    asyncio.run(get_store().set_selected_repositories(_user_id(client), installation_id, [1001]))

    fake_db_url = "postgresql://u:p@localhost:5432/x"
    monkeypatch.setattr("app.api.routes.analysis._get_database_url", lambda: fake_db_url)

    from pr_context_builder.index import PostgresCallerIndex

    mock_context = MagicMock()
    mock_result = MagicMock()
    mock_result.model_dump.return_value = {"status": "ok"}

    captured: dict[str, Any] = {}

    def fake_build_context(
        owner: str,
        repo: str,
        pull_number: int,
        github_token: str | None,
        caller_index: Any = None,
    ):
        captured["owner"] = owner
        captured["repo"] = repo
        captured["pull_number"] = pull_number
        captured["github_token"] = github_token
        captured["caller_index"] = caller_index
        return mock_context

    monkeypatch.setattr("app.api.routes.analysis.build_context", fake_build_context)
    monkeypatch.setattr("app.api.routes.analysis.localize_bugs", lambda ctx: mock_result)

    resp = client.post(
        "/api/v1/analysis/pr",
        json={"owner": "octocat", "repo": "repo-1001", "pull_number": 42},
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 200
    assert captured["owner"] == "octocat"
    assert captured["repo"] == "repo-1001"
    assert captured["pull_number"] == 42
    assert captured["github_token"] == FAKE_INSTALLATION_TOKEN

    caller_idx = captured["caller_index"]
    assert isinstance(caller_idx, PostgresCallerIndex)


def test_pr_analysis_passes_none_when_database_url_not_set(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, gh: FakeGitHubHTTP
) -> None:
    """When DATABASE_URL is not set, caller_index is None and build_context is called as today."""
    installation_id = 12345
    csrf = _login_and_connect(client, monkeypatch, installation_id)
    asyncio.run(get_store().set_selected_repositories(_user_id(client), installation_id, [1001]))

    monkeypatch.setattr("app.api.routes.analysis._get_database_url", lambda: None)

    mock_context = MagicMock()
    mock_result = MagicMock()
    mock_result.model_dump.return_value = {"status": "ok"}

    captured: dict[str, Any] = {}

    def fake_build_context(
        owner: str,
        repo: str,
        pull_number: int,
        github_token: str | None,
        caller_index: Any = None,
    ):
        captured["owner"] = owner
        captured["repo"] = repo
        captured["pull_number"] = pull_number
        captured["github_token"] = github_token
        captured["caller_index"] = caller_index
        return mock_context

    monkeypatch.setattr("app.api.routes.analysis.build_context", fake_build_context)
    monkeypatch.setattr("app.api.routes.analysis.localize_bugs", lambda ctx: mock_result)

    resp = client.post(
        "/api/v1/analysis/pr",
        json={"owner": "octocat", "repo": "repo-1001", "pull_number": 42},
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 200
    assert captured["owner"] == "octocat"
    assert captured["repo"] == "repo-1001"
    assert captured["pull_number"] == 42
    assert captured["github_token"] == FAKE_INSTALLATION_TOKEN
    assert captured["caller_index"] is None
