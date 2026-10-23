"""Tests for the GitHub webhook receiver.

Nothing here may touch the real database, GitHub or the network. An autouse
fixture replaces every outside call and fails loudly if a test forgets to.
"""

import asyncio
import hashlib
import hmac
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from app.api.routes import webhooks
from app.main import app

SECRET = "test-secret"
URL = "/api/v1/webhooks/github"
client = TestClient(app)


def sign(secret, body):
    return "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def _no_database():
    raise AssertionError("tests must not touch the real database")


@pytest.fixture(autouse=True)
def isolate(monkeypatch):
    monkeypatch.setattr(webhooks, "get_settings", lambda: SimpleNamespace(webhook_secret=SECRET))
    monkeypatch.setattr(webhooks, "index_single_repository", AsyncMock())
    monkeypatch.setattr(webhooks, "index_user_installation", AsyncMock())
    monkeypatch.setattr(webhooks, "get_sessionmaker", _no_database)
    monkeypatch.setattr(webhooks, "run_engine_review", lambda *a, **k: [])
    monkeypatch.setattr(webhooks, "upsert_review_comment", AsyncMock(return_value="skipped"))
    monkeypatch.delenv("BUGZZY_POST_COMMENTS", raising=False)
    webhooks._recent_deliveries.clear()


def post(event, payload, *, secret=SECRET, delivery=None):
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    headers = {
        "X-GitHub-Event": event,
        "X-Hub-Signature-256": sign(secret, body),
        "Content-Type": "application/json",
    }
    if delivery:
        headers["X-GitHub-Delivery"] = delivery
    return client.post(URL, content=body, headers=headers)


class _FakeResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _FakeSession:
    def __init__(self, value):
        self._value = value

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, statement):
        return _FakeResult(self._value)


def _sessionmaker_returning(value):
    return lambda: (lambda: _FakeSession(value))


def test_ping_event():
    response = post("ping", {"zen": "Keep it simple"})
    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_invalid_signature_rejected():
    response = post("ping", {"zen": "x"}, secret="wrong-secret")
    assert response.status_code == 401


def test_missing_signature_rejected():
    response = client.post(URL, content=b"{}", headers={"X-GitHub-Event": "ping"})
    assert response.status_code == 401


def test_unconfigured_secret_rejected(monkeypatch):
    monkeypatch.setattr(webhooks, "get_settings", lambda: SimpleNamespace(webhook_secret=""))
    response = post("ping", {"zen": "x"})
    assert response.status_code == 503


def test_push_default_branch_schedules_indexing(monkeypatch):
    mock_index = AsyncMock()
    monkeypatch.setattr(webhooks, "_index_by_github_id", mock_index)
    payload = {"ref": "refs/heads/main", "repository": {"id": 123, "default_branch": "main"}}
    response = post("push", payload)
    assert response.status_code == 200
    assert response.json() == {"queued": "repository indexing"}
    mock_index.assert_awaited_once_with(123)


def test_push_other_branch_ignored(monkeypatch):
    mock_index = AsyncMock()
    monkeypatch.setattr(webhooks, "_index_by_github_id", mock_index)
    payload = {"ref": "refs/heads/feature", "repository": {"id": 456, "default_branch": "main"}}
    response = post("push", payload)
    assert response.status_code == 200
    assert response.json() == {"ignored": "non-default branch"}
    mock_index.assert_not_awaited()


def test_installation_event_schedules_indexing(monkeypatch):
    mock_safe = AsyncMock()
    monkeypatch.setattr(webhooks, "_safe_index_user_installation", mock_safe)
    response = post("installation", {"action": "created", "installation": {"id": 789}})
    assert response.status_code == 200
    assert response.json() == {"queued": "installation indexing"}
    mock_safe.assert_awaited_once_with(789)


def test_installation_deleted_is_ignored(monkeypatch):
    mock_safe = AsyncMock()
    monkeypatch.setattr(webhooks, "_safe_index_user_installation", mock_safe)
    response = post("installation", {"action": "deleted", "installation": {"id": 789}})
    assert response.status_code == 200
    mock_safe.assert_not_awaited()


@pytest.mark.parametrize("action", ["opened", "synchronize", "reopened"])
def test_pull_request_review_actions_schedule_review(monkeypatch, action):
    mock_review = AsyncMock()
    monkeypatch.setattr(webhooks, "_review_pull_request", mock_review)
    payload = {
        "action": action,
        "repository": {"id": 123, "full_name": "owner/name"},
        "pull_request": {"number": 5},
    }
    response = post("pull_request", payload)
    assert response.status_code == 202
    assert response.json() == {"queued": "pull request review"}
    mock_review.assert_awaited_once_with(123, 5, action)


def test_pull_request_closed_is_ignored(monkeypatch):
    mock_review = AsyncMock()
    monkeypatch.setattr(webhooks, "_review_pull_request", mock_review)
    payload = {
        "action": "closed",
        "repository": {"id": 123},
        "pull_request": {"number": 5},
    }
    response = post("pull_request", payload)
    assert response.status_code == 200
    assert response.json() == {"ignored": "action closed"}
    mock_review.assert_not_awaited()


def test_pull_request_missing_repository_id_is_ignored(monkeypatch):
    mock_review = AsyncMock()
    monkeypatch.setattr(webhooks, "_review_pull_request", mock_review)
    payload = {"action": "opened", "pull_request": {"number": 5}}
    response = post("pull_request", payload)
    assert response.status_code == 200
    assert response.json() == {"ignored": "missing repository id or pull request number"}
    mock_review.assert_not_awaited()


def test_duplicate_delivery_ignored():
    first = post("ping", {"zen": "a"}, delivery="delivery-1")
    second = post("ping", {"zen": "a"}, delivery="delivery-1")
    assert first.json() == {"ok": True}
    assert second.json() == {"ignored": "duplicate"}


def test_index_by_github_id_uses_internal_id(monkeypatch):
    mock_index = AsyncMock()
    monkeypatch.setattr(webhooks, "get_sessionmaker", _sessionmaker_returning(42))
    monkeypatch.setattr(webhooks, "index_single_repository", mock_index)
    asyncio.run(webhooks._index_by_github_id(1424454140))
    mock_index.assert_awaited_once_with(42)


def test_index_by_github_id_unknown_repository(monkeypatch):
    mock_index = AsyncMock()
    monkeypatch.setattr(webhooks, "get_sessionmaker", _sessionmaker_returning(None))
    monkeypatch.setattr(webhooks, "index_single_repository", mock_index)
    asyncio.run(webhooks._index_by_github_id(999))
    mock_index.assert_not_awaited()


def test_safe_index_user_installation_swallows_errors(monkeypatch):
    monkeypatch.setattr(
        webhooks, "index_user_installation", AsyncMock(side_effect=RuntimeError("boom"))
    )
    asyncio.run(webhooks._safe_index_user_installation(1))

class _FakeRepoResult:
    def __init__(self, row):
        self._row = row

    def one_or_none(self):
        return self._row


class _FakeRepoSession:
    def __init__(self, row):
        self._row = row

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, statement):
        return _FakeRepoResult(self._row)


def _repo_sessionmaker(row):
    return lambda: (lambda: _FakeRepoSession(row))


def test_review_pull_request_fetches_changed_files(monkeypatch):
    row = SimpleNamespace(id=7, owner="Amvi3007", name="Testing", installation_id=111)
    monkeypatch.setattr(webhooks, "load_symbols_by_path", AsyncMock(return_value={}))
    mock_token = AsyncMock(return_value="tok")
    mock_fetch = AsyncMock(return_value=[{"path": "a.py", "status": "modified"}])
    monkeypatch.setattr(webhooks, "get_sessionmaker", _repo_sessionmaker(row))
    monkeypatch.setattr(webhooks, "get_installation_token", mock_token)
    monkeypatch.setattr(webhooks, "fetch_pr_files", mock_fetch)
    asyncio.run(webhooks._review_pull_request(123, 5, "opened"))
    mock_token.assert_awaited_once_with(111)
    mock_fetch.assert_awaited_once_with("tok", "Amvi3007", "Testing", 5)


def test_review_pull_request_unknown_repository(monkeypatch):
    mock_token = AsyncMock()
    mock_fetch = AsyncMock()
    monkeypatch.setattr(webhooks, "get_sessionmaker", _repo_sessionmaker(None))
    monkeypatch.setattr(webhooks, "get_installation_token", mock_token)
    monkeypatch.setattr(webhooks, "fetch_pr_files", mock_fetch)
    asyncio.run(webhooks._review_pull_request(999, 5, "opened"))
    mock_token.assert_not_awaited()
    mock_fetch.assert_not_awaited()


def test_review_pull_request_without_installation(monkeypatch):
    row = SimpleNamespace(id=7, owner="o", name="n", installation_id=None)
    mock_token = AsyncMock()
    mock_fetch = AsyncMock()
    monkeypatch.setattr(webhooks, "get_sessionmaker", _repo_sessionmaker(row))
    monkeypatch.setattr(webhooks, "get_installation_token", mock_token)
    monkeypatch.setattr(webhooks, "fetch_pr_files", mock_fetch)
    asyncio.run(webhooks._review_pull_request(123, 5, "opened"))
    mock_token.assert_not_awaited()
    mock_fetch.assert_not_awaited()


def test_review_pull_request_swallows_errors(monkeypatch):
    row = SimpleNamespace(id=7, owner="o", name="n", installation_id=1)
    monkeypatch.setattr(webhooks, "get_sessionmaker", _repo_sessionmaker(row))
    monkeypatch.setattr(webhooks, "get_installation_token", AsyncMock(return_value="tok"))
    monkeypatch.setattr(
        webhooks, "fetch_pr_files", AsyncMock(side_effect=RuntimeError("boom"))
    )
    asyncio.run(webhooks._review_pull_request(123, 5, "opened"))


def test_review_pull_request_looks_up_symbols_for_changed_python_files(monkeypatch):
    row = SimpleNamespace(id=7, owner="o", name="n", installation_id=1)
    files = [
        {"path": "billing.py", "status": "modified", "patch": "@@ -7,1 +7,1 @@\n-a\n+b"},
        {"path": "README.md", "status": "modified", "patch": "@@ -1 +1 @@\n-a\n+b"},
        {"path": "new.py", "status": "added", "patch": "@@ -0,0 +1 @@\n+x"},
        {"path": "new_name.py", "previous_path": "old_name.py", "status": "renamed", "patch": None},
    ]
    mock_load = AsyncMock(return_value={})
    monkeypatch.setattr(webhooks, "get_sessionmaker", _repo_sessionmaker(row))
    monkeypatch.setattr(webhooks, "get_installation_token", AsyncMock(return_value="tok"))
    monkeypatch.setattr(webhooks, "fetch_pr_files", AsyncMock(return_value=files))
    monkeypatch.setattr(webhooks, "load_symbols_by_path", mock_load)
    asyncio.run(webhooks._review_pull_request(123, 5, "opened"))
    assert mock_load.await_args.args[1] == 7
    assert mock_load.await_args.args[2] == ["billing.py", "old_name.py"]


def _engine_ready_monkeypatch(monkeypatch, engine):
    row = SimpleNamespace(id=7, owner="Amvi3007", name="Testing", installation_id=111)
    monkeypatch.setattr(webhooks, "get_sessionmaker", _repo_sessionmaker(row))
    monkeypatch.setattr(webhooks, "get_installation_token", AsyncMock(return_value="tok"))
    monkeypatch.setattr(webhooks, "fetch_pr_files", AsyncMock(return_value=[]))
    monkeypatch.setattr(webhooks, "load_symbols_by_path", AsyncMock(return_value={}))
    monkeypatch.setattr(webhooks, "run_engine_review", engine)


def test_review_pull_request_runs_the_engine_with_the_token(monkeypatch):
    from unittest.mock import MagicMock

    finding = {
        "finding_id": "BUG-001",
        "file_path": "billing.py",
        "start_line": 7,
        "end_line": 7,
        "summary": "Incompatible call",
        "severity": "high",
        "confidence": "strong",
    }
    engine = MagicMock(return_value=[finding])
    _engine_ready_monkeypatch(monkeypatch, engine)
    asyncio.run(webhooks._review_pull_request(123, 5, "opened"))
    engine.assert_called_once_with("Amvi3007", "Testing", 5, "tok")


def test_review_pull_request_survives_an_engine_failure(monkeypatch):
    from unittest.mock import MagicMock

    engine = MagicMock(side_effect=RuntimeError("engine exploded"))
    _engine_ready_monkeypatch(monkeypatch, engine)
    asyncio.run(webhooks._review_pull_request(123, 5, "opened"))
    engine.assert_called_once()


_COMMENT_FINDING = {
    "finding_id": "BUG-001",
    "file_path": "billing.py",
    "start_line": 7,
    "end_line": 7,
    "summary": "Incompatible call",
    "severity": "high",
    "confidence": "strong",
}


def test_review_pull_request_posts_comment_when_enabled(monkeypatch):
    from unittest.mock import MagicMock

    _engine_ready_monkeypatch(monkeypatch, MagicMock(return_value=[_COMMENT_FINDING]))
    upsert = AsyncMock(return_value="created")
    monkeypatch.setattr(webhooks, "upsert_review_comment", upsert)
    monkeypatch.setenv("BUGZZY_POST_COMMENTS", "1")
    asyncio.run(webhooks._review_pull_request(123, 5, "opened"))
    upsert.assert_awaited_once_with("tok", "Amvi3007", "Testing", 5, [_COMMENT_FINDING])


def test_review_pull_request_does_not_post_by_default(monkeypatch):
    from unittest.mock import MagicMock

    _engine_ready_monkeypatch(monkeypatch, MagicMock(return_value=[_COMMENT_FINDING]))
    upsert = AsyncMock(return_value="created")
    monkeypatch.setattr(webhooks, "upsert_review_comment", upsert)
    asyncio.run(webhooks._review_pull_request(123, 5, "opened"))
    upsert.assert_not_awaited()


def test_review_pull_request_survives_a_comment_failure(monkeypatch):
    from unittest.mock import MagicMock

    _engine_ready_monkeypatch(monkeypatch, MagicMock(return_value=[_COMMENT_FINDING]))
    upsert = AsyncMock(side_effect=RuntimeError("403 forbidden"))
    monkeypatch.setattr(webhooks, "upsert_review_comment", upsert)
    monkeypatch.setenv("BUGZZY_POST_COMMENTS", "1")
    asyncio.run(webhooks._review_pull_request(123, 5, "opened"))
    upsert.assert_awaited_once()