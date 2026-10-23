"""Automated unit tests for GitHubClient with mocked HTTP interactions.

All tests run completely offline without internet access, GitHub credentials,
or real repositories.
"""

from unittest.mock import MagicMock

import pytest
import requests

from pr_context_builder.exceptions import (
    AuthenticationError,
    GitHubAPIError,
    RateLimitExceededError,
    ResourceNotFoundError,
)
from pr_context_builder.github.client import GitHubClient
from pr_context_builder.models.pr_metadata import FileChangeStatus, PRState


def _mock_response(
    status_code: int = 200,
    json_data: dict | list | None = None,
    text: str = "",
    headers: dict[str, str] | None = None,
) -> requests.Response:
    """Helper creating a simulated requests.Response object."""
    resp = requests.Response()
    resp.status_code = status_code
    resp._content = (
        (text.encode("utf-8"))
        if json_data is None
        else (requests.compat.json.dumps(json_data).encode("utf-8"))
    )
    resp.headers = requests.structures.CaseInsensitiveDict(headers or {})
    return resp


@pytest.fixture
def mock_session() -> MagicMock:
    return MagicMock(spec=requests.Session)


@pytest.fixture
def client(mock_session: MagicMock) -> GitHubClient:
    return GitHubClient(
        token="ghp_test_secret_token_1234567890abcdef",
        base_url="https://api.github.com",
        timeout_seconds=10,
        session=mock_session,
    )


def test_get_pull_request_success(client: GitHubClient, mock_session: MagicMock) -> None:
    """Ensure successful PR metadata extraction and head SHA consistency."""
    mock_payload = {
        "number": 101,
        "title": "Refactor auth middleware",
        "body": "Improves security and error handling.",
        "state": "open",
        "merged": False,
        "base": {"sha": "base1234567890", "ref": "main"},
        "head": {"sha": "headabcdef1234", "ref": "feature/auth"},
        "user": {"login": "monalisa"},
        "created_at": "2024-01-15T10:30:00Z",
        "updated_at": "2024-01-15T11:00:00Z",
    }
    mock_session.request.return_value = _mock_response(status_code=200, json_data=mock_payload)

    pr = client.get_pull_request("octocat", "spoon-knife", 101)

    assert pr.owner == "octocat"
    assert pr.repo == "spoon-knife"
    assert pr.number == 101
    assert pr.title == "Refactor auth middleware"
    assert pr.state == PRState.OPEN
    assert pr.base_sha == "base1234567890"
    assert pr.head_sha == "headabcdef1234"
    assert pr.base_branch == "main"
    assert pr.head_branch == "feature/auth"
    assert pr.author == "monalisa"
    assert pr.created_at is not None

    # Verify authorization header was set
    called_headers = mock_session.request.call_args[1]["headers"]
    assert "Authorization" in called_headers
    assert "Bearer ghp_test_secret_token_1234567890abcdef" in called_headers["Authorization"]


def test_get_pull_request_merged_state(client: GitHubClient, mock_session: MagicMock) -> None:
    """Ensure merged PRs are classified with PRState.MERGED."""
    mock_payload = {
        "number": 102,
        "title": "Merged PR",
        "state": "closed",
        "merged": True,
        "base": {"sha": "basesha", "ref": "main"},
        "head": {"sha": "headsha", "ref": "patch-1"},
    }
    mock_session.request.return_value = _mock_response(status_code=200, json_data=mock_payload)

    pr = client.get_pull_request("octocat", "spoon-knife", 102)
    assert pr.state == PRState.MERGED


def test_get_pull_request_files_pagination(mock_session: MagicMock) -> None:
    """Ensure changed files pagination fetches across pages and parses hunks."""
    client = GitHubClient(
        token="ghp_test_token",
        timeout_seconds=10,
        max_changed_files_limit=200,
        session=mock_session,
    )

    # Page 1 returns 100 files
    page_1 = [
        {
            "filename": f"file_{i}.py",
            "status": "modified",
            "additions": 2,
            "deletions": 1,
            "changes": 3,
            "patch": f"@@ -1,3 +1,4 @@\n-old\n+new_{i}",
            "sha": f"blob_{i}",
        }
        for i in range(100)
    ]
    # Page 2 returns 2 files
    page_2 = [
        {
            "filename": "final_file.py",
            "status": "added",
            "additions": 10,
            "deletions": 0,
            "changes": 10,
            "patch": "@@ -0,0 +1,10 @@\n+line1",
            "sha": "blob_final",
        },
        {
            "filename": "old_file.py",
            "status": "removed",
            "additions": 0,
            "deletions": 5,
            "changes": 5,
            "patch": "@@ -1,5 +0,0 @@\n-line1",
            "sha": "blob_del",
        },
    ]

    mock_session.request.side_effect = [
        _mock_response(status_code=200, json_data=page_1),
        _mock_response(status_code=200, json_data=page_2),
    ]

    files = client.get_pull_request_files("octocat", "spoon-knife", 101)

    assert len(files) == 102
    assert mock_session.request.call_count == 2
    assert files[0].filename == "file_0.py"
    assert len(files[0].hunks) == 1
    assert files[100].filename == "final_file.py"
    assert files[100].status == FileChangeStatus.ADDED
    assert files[101].status == FileChangeStatus.REMOVED


def test_get_pull_request_files_respects_max_limit(mock_session: MagicMock) -> None:
    """Ensure changed files fetching strictly respects max_changed_files_limit."""
    client = GitHubClient(
        token="ghp_test_token",
        timeout_seconds=10,
        max_changed_files_limit=50,
        session=mock_session,
    )
    # Page 1 returns 100 files, but limit is 50
    page_1 = [
        {
            "filename": f"file_{i}.py",
            "status": "modified",
            "additions": 1,
            "deletions": 1,
            "changes": 2,
            "patch": "@@ -1 +1 @@\n-a\n+b",
            "sha": f"sha_{i}",
        }
        for i in range(100)
    ]
    mock_session.request.return_value = _mock_response(status_code=200, json_data=page_1)

    files = client.get_pull_request_files("octocat", "spoon-knife", 101)
    assert len(files) == 50


def test_changed_files_missing_patch_and_renamed(
    client: GitHubClient, mock_session: MagicMock
) -> None:
    """Ensure missing patch data (large diff or binary file) and renames are recorded accurately."""
    mock_files = [
        {
            "filename": "assets/logo.png",
            "status": "modified",
            "additions": 0,
            "deletions": 0,
            "changes": 1,
            "patch": None,  # Binary / omitted patch
            "sha": "blob_png",
        },
        {
            "filename": "src/new_name.py",
            "status": "renamed",
            "previous_filename": "src/old_name.py",
            "additions": 1,
            "deletions": 1,
            "changes": 2,
            "patch": "@@ -1,2 +1,2 @@\n-a\n+b",
            "sha": "blob_renamed",
        },
    ]
    mock_session.request.return_value = _mock_response(status_code=200, json_data=mock_files)

    files = client.get_pull_request_files("octocat", "spoon-knife", 101)

    assert len(files) == 2
    assert files[0].filename == "assets/logo.png"
    assert files[0].patch is None
    assert files[0].is_binary is True
    assert files[0].hunks == []

    assert files[1].status == FileChangeStatus.RENAMED
    assert files[1].previous_filename == "src/old_name.py"
    assert len(files[1].hunks) == 1


def test_authentication_error_401(client: GitHubClient, mock_session: MagicMock) -> None:
    """Ensure 401 response raises AuthenticationError."""
    mock_session.request.return_value = _mock_response(
        status_code=401, text='{"message": "Bad credentials"}'
    )

    with pytest.raises(AuthenticationError) as exc_info:
        client.get_pull_request("octocat", "spoon-knife", 1)

    assert exc_info.value.status_code == 401
    assert "Invalid or missing token" in str(exc_info.value)


def test_resource_not_found_404(client: GitHubClient, mock_session: MagicMock) -> None:
    """Ensure 404 response raises ResourceNotFoundError."""
    mock_session.request.return_value = _mock_response(
        status_code=404, text='{"message": "Not Found"}'
    )

    with pytest.raises(ResourceNotFoundError) as exc_info:
        client.get_pull_request("octocat", "spoon-knife", 99999)

    assert exc_info.value.status_code == 404


def test_rate_limit_exceeded_error(client: GitHubClient, mock_session: MagicMock) -> None:
    """Ensure 403 rate-limit header triggers RateLimitExceededError with reset timestamp."""
    headers = {
        "x-ratelimit-remaining": "0",
        "x-ratelimit-reset": "1735689600",
    }
    mock_session.request.return_value = _mock_response(
        status_code=403,
        text='{"message": "API rate limit exceeded"}',
        headers=headers,
    )

    with pytest.raises(RateLimitExceededError) as exc_info:
        client.get_pull_request("octocat", "spoon-knife", 1)

    assert exc_info.value.status_code == 403
    assert exc_info.value.reset_timestamp == 1735689600


def test_network_timeout_and_connection_errors(
    client: GitHubClient, mock_session: MagicMock
) -> None:
    """Ensure timeouts and network failures raise GitHubAPIError."""
    mock_session.request.side_effect = requests.exceptions.Timeout("Read timed out")

    with pytest.raises(GitHubAPIError) as exc_info:
        client.get_pull_request("octocat", "spoon-knife", 1)

    assert "timed out" in str(exc_info.value).lower()

    mock_session.request.side_effect = requests.exceptions.ConnectionError("DNS failure")

    with pytest.raises(GitHubAPIError) as exc_info:
        client.get_pull_request("octocat", "spoon-knife", 1)

    assert "network failure" in str(exc_info.value).lower()


def test_malformed_json_response(client: GitHubClient, mock_session: MagicMock) -> None:
    """Ensure malformed or non-JSON response raises GitHubAPIError."""
    mock_session.request.return_value = _mock_response(
        status_code=200, text="<html>502 Bad Gateway</html>"
    )

    with pytest.raises(GitHubAPIError) as exc_info:
        client.get_pull_request("octocat", "spoon-knife", 1)

    assert "malformed json" in str(exc_info.value).lower()


def test_token_redaction_in_client(mock_session: MagicMock) -> None:
    """Ensure secret token is never exposed in client repr or sanitized text."""
    secret = "ghp_super_secret_token_abcdef1234567890"
    cli = GitHubClient(token=secret, session=mock_session)

    # Repr masking
    assert secret not in repr(cli)
    assert "***" in repr(cli)

    # Sanitization helper
    leak_attempt = f"Error occurred with token {secret}"
    sanitized = cli._sanitize_message(leak_attempt)
    assert secret not in sanitized
    assert "[REDACTED_SECRET]" in sanitized


def test_get_file_content_raw_and_base64(client: GitHubClient, mock_session: MagicMock) -> None:
    """Ensure file content retrieval supports both raw text and base64 JSON responses."""
    # 1. Raw response
    mock_session.request.return_value = _mock_response(
        status_code=200,
        text="print('Hello from raw')",
        headers={"content-type": "text/plain; charset=utf-8"},
    )
    content_raw = client.get_file_content("octocat", "spoon-knife", "main.py", "headsha123")
    assert content_raw == "print('Hello from raw')"

    # 2. Base64 JSON response
    mock_session.request.return_value = _mock_response(
        status_code=200,
        json_data={
            "encoding": "base64",
            "content": "cHJpbnQoJ2hlbGxvIGZyb20gYjY0Jyk=",  # print('hello from b64')
        },
        headers={"content-type": "application/json; charset=utf-8"},
    )
    content_b64 = client.get_file_content("octocat", "spoon-knife", "main.py", "headsha123")
    assert content_b64 == "print('hello from b64')"


def test_get_pull_request_diff(client: GitHubClient, mock_session: MagicMock) -> None:
    """Ensure raw diff endpoint returns unified diff string with diff media header."""
    sample_diff = "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-old\n+new"
    mock_session.request.return_value = _mock_response(status_code=200, text=sample_diff)

    diff = client.get_pull_request_diff("octocat", "spoon-knife", 101)
    assert diff == sample_diff

    # Verify Accept header was set to diff format
    called_headers = mock_session.request.call_args[1]["headers"]
    assert called_headers["Accept"] == "application/vnd.github.v3.diff"


def test_invalid_client_inputs(client: GitHubClient) -> None:
    """Ensure invalid repository identities or PR numbers raise ValueError."""
    with pytest.raises(ValueError):
        client.get_pull_request("", "repo", 1)

    with pytest.raises(ValueError):
        client.get_pull_request("owner", "", 1)

    with pytest.raises(ValueError):
        client.get_pull_request("owner", "repo", 0)

    with pytest.raises(ValueError):
        client.get_pull_request("owner", "repo", -5)


def test_get_repository_tree_success(client: GitHubClient, mock_session: MagicMock) -> None:
    """Ensure get_repository_tree fetches entries recursively and returns truncation flag."""
    mock_payload = {
        "sha": "tree_sha_123",
        "url": "https://api.github.com/repos/octocat/spoon-knife/git/trees/tree_sha_123",
        "tree": [
            {"path": "src/app.py", "mode": "100644", "type": "blob", "size": 150},
            {"path": "tests/test_app.py", "mode": "100644", "type": "blob", "size": 250},
            {"path": "src", "mode": "040000", "type": "tree"},
        ],
        "truncated": False,
    }
    mock_session.request.return_value = _mock_response(status_code=200, json_data=mock_payload)

    entries, truncated = client.get_repository_tree("octocat", "spoon-knife", "tree_sha_123")
    assert len(entries) == 3
    assert truncated is False
    assert entries[0]["path"] == "src/app.py"

    call_args = mock_session.request.call_args
    assert call_args[1]["params"] == {"recursive": "1"}


def test_get_repository_tree_truncated(client: GitHubClient, mock_session: MagicMock) -> None:
    """Ensure truncated flag is correctly detected and reported."""
    mock_payload = {
        "sha": "tree_sha_123",
        "tree": [{"path": "a.py", "type": "blob"}],
        "truncated": True,
    }
    mock_session.request.return_value = _mock_response(status_code=200, json_data=mock_payload)

    entries, truncated = client.get_repository_tree("octocat", "spoon-knife", "tree_sha_123")
    assert len(entries) == 1
    assert truncated is True


def test_get_repository_tree_api_error(client: GitHubClient, mock_session: MagicMock) -> None:
    """Ensure API errors on tree retrieval map to domain exceptions."""
    mock_session.request.return_value = _mock_response(
        status_code=404, json_data={"message": "Not Found"}
    )
    with pytest.raises(ResourceNotFoundError):
        client.get_repository_tree("octocat", "spoon-knife", "bad_sha")


def test_get_repository_tree_malformed(client: GitHubClient, mock_session: MagicMock) -> None:
    """Ensure malformed tree payload raises GitHubAPIError."""
    # 1. Non-dict root
    mock_session.request.return_value = _mock_response(
        status_code=200, json_data=["not", "a", "dict"]
    )
    with pytest.raises(GitHubAPIError):
        client.get_repository_tree("octocat", "spoon-knife", "sha1")

    # 2. 'tree' is not a list
    mock_session.request.return_value = _mock_response(
        status_code=200, json_data={"tree": "not-a-list", "truncated": False}
    )
    with pytest.raises(GitHubAPIError):
        client.get_repository_tree("octocat", "spoon-knife", "sha1")


def test_get_repository_tree_invalid_inputs(client: GitHubClient) -> None:
    """Ensure invalid parameters for tree retrieval raise ValueError."""
    with pytest.raises(ValueError):
        client.get_repository_tree("", "repo", "sha")
    with pytest.raises(ValueError):
        client.get_repository_tree("owner", "", "sha")
    with pytest.raises(ValueError):
        client.get_repository_tree("owner", "repo", "")
