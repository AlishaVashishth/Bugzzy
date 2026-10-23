"""Tests for CallerIndex integration in Context Builder."""

from unittest.mock import MagicMock, patch

import pytest

from pr_context_builder.builder.context_builder import build_context
from pr_context_builder.config import Settings
from pr_context_builder.index import CallerIndex
from pr_context_builder.models.context_package import LimitationType
from pr_context_builder.models.pr_metadata import (
    ChangedFile,
    DiffHunk,
    FileChangeStatus,
    PRState,
    PullRequest,
)

BUILDER_MOD = "pr_context_builder.builder.context_builder"


@pytest.fixture(autouse=True)
def mock_transformer_encoder():
    with patch(f"{BUILDER_MOD}.TransformerCodeEncoder") as mock_encoder:
        yield mock_encoder


def _create_test_pr() -> tuple[PullRequest, ChangedFile]:
    hunk = DiffHunk(
        old_start=1,
        old_lines=2,
        new_start=1,
        new_lines=2,
        header="def calculate_total():",
        lines=["-def calculate_total():", "+def calculate_total():"],
    )
    changed_file = ChangedFile(
        filename="src/service.py",
        status=FileChangeStatus.MODIFIED,
        patch="dummy",
        hunks=[hunk],
    )
    pr = PullRequest(
        owner="test-owner",
        repo="test-repo",
        number=10,
        title="Update calculate_total",
        state=PRState.OPEN,
        head_sha="head_sha_123",
        base_sha="base_sha_456",
        head_branch="feature",
        base_branch="main",
        changed_files=[changed_file],
    )
    return pr, changed_file


SERVICE_CONTENT = """def calculate_total():
    return 42
"""

CALLER_CONTENT = """from src.service import calculate_total

def run():
    return calculate_total()
"""

FALLBACK_CONTENT = """from src.service import calculate_total

def fallback_run():
    return calculate_total()
"""


def test_index_returns_files_contents_used() -> None:
    """When CallerIndex returns files, their contents are retrieved and used without tree scan."""
    pr, changed_file = _create_test_pr()
    mock_index = MagicMock(spec=CallerIndex)
    mock_index.find_caller_files.return_value = ["src/caller.py"]

    custom_settings = Settings(
        github_token="fake",
        dl_ranking_enabled=True,
        max_candidate_files=20,
    )

    with (
        patch(f"{BUILDER_MOD}.load_settings", return_value=custom_settings),
        patch(f"{BUILDER_MOD}.GitHubClient") as mock_client_cls,
    ):
        mock_client = mock_client_cls.return_value
        mock_client.get_pull_request.return_value = pr
        mock_client.get_pull_request_files.return_value = [changed_file]
        mock_client.get_pull_request_diff.return_value = "diff dummy"

        def fake_get_file_content(owner: str, repo: str, path: str, sha: str) -> str:
            if path == "src/service.py":
                return SERVICE_CONTENT
            if path == "src/caller.py":
                return CALLER_CONTENT
            raise FileNotFoundError(path)

        mock_client.get_file_content.side_effect = fake_get_file_content

        pkg = build_context("test-owner", "test-repo", 10, caller_index=mock_index)

        # Index called with symbol names
        mock_index.find_caller_files.assert_called_once_with(
            "test-owner", "test-repo", ["calculate_total"], limit=20
        )
        # Tree scan was NOT invoked
        mock_client.get_repository_tree.assert_not_called()

        # Caller file snippet & relationship present in package
        snippet_paths = [s.file_path for s in pkg.evidence_snippets]
        assert "src/caller.py" in snippet_paths
        assert any(
            rel.source_symbol == "src/caller.py" and rel.target_symbol == "calculate_total"
            for rel in pkg.inferred_relationships
        )
        assert not any(lim.limitation_type == LimitationType.CALLER_INDEX_EMPTY for lim in pkg.limitations)


def test_index_returns_empty_records_limitation_and_no_tree_scan() -> None:
    """When CallerIndex returns empty list, no tree scan occurs and CALLER_INDEX_EMPTY limitation is recorded."""
    pr, changed_file = _create_test_pr()
    mock_index = MagicMock(spec=CallerIndex)
    mock_index.find_caller_files.return_value = []

    custom_settings = Settings(
        github_token="fake",
        dl_ranking_enabled=True,
        max_candidate_files=20,
    )

    with (
        patch(f"{BUILDER_MOD}.load_settings", return_value=custom_settings),
        patch(f"{BUILDER_MOD}.GitHubClient") as mock_client_cls,
    ):
        mock_client = mock_client_cls.return_value
        mock_client.get_pull_request.return_value = pr
        mock_client.get_pull_request_files.return_value = [changed_file]
        mock_client.get_pull_request_diff.return_value = "diff dummy"
        mock_client.get_file_content.return_value = SERVICE_CONTENT

        pkg = build_context("test-owner", "test-repo", 10, caller_index=mock_index)

        mock_index.find_caller_files.assert_called_once()
        # Tree scan was NOT invoked
        mock_client.get_repository_tree.assert_not_called()

        # Limitation recorded
        assert any(lim.limitation_type == LimitationType.CALLER_INDEX_EMPTY for lim in pkg.limitations)


def test_index_raises_records_limitation_and_no_tree_scan() -> None:
    """When CallerIndex raises an exception, no tree scan occurs and CALLER_INDEX_EMPTY limitation is recorded."""
    pr, changed_file = _create_test_pr()
    mock_index = MagicMock(spec=CallerIndex)
    mock_index.find_caller_files.side_effect = RuntimeError("PostgreSQL connection timeout")

    custom_settings = Settings(
        github_token="fake",
        dl_ranking_enabled=True,
        max_candidate_files=20,
    )

    with (
        patch(f"{BUILDER_MOD}.load_settings", return_value=custom_settings),
        patch(f"{BUILDER_MOD}.GitHubClient") as mock_client_cls,
    ):
        mock_client = mock_client_cls.return_value
        mock_client.get_pull_request.return_value = pr
        mock_client.get_pull_request_files.return_value = [changed_file]
        mock_client.get_pull_request_diff.return_value = "diff dummy"
        mock_client.get_file_content.return_value = SERVICE_CONTENT

        pkg = build_context("test-owner", "test-repo", 10, caller_index=mock_index)

        mock_index.find_caller_files.assert_called_once()
        # Tree scan was NOT invoked
        mock_client.get_repository_tree.assert_not_called()

        # Limitation recorded
        assert any(lim.limitation_type == LimitationType.CALLER_INDEX_EMPTY for lim in pkg.limitations)


def test_index_unusable_paths_records_limitation_and_no_tree_scan() -> None:
    """When CallerIndex returns paths that are all skipped (e.g. changed files or non-Python), limitation is recorded."""
    pr, changed_file = _create_test_pr()
    mock_index = MagicMock(spec=CallerIndex)
    # src/service.py is in changed_files, docs/readme.md is non-python
    mock_index.find_caller_files.return_value = ["src/service.py", "docs/readme.md"]

    custom_settings = Settings(
        github_token="fake",
        dl_ranking_enabled=True,
        max_candidate_files=20,
    )

    with (
        patch(f"{BUILDER_MOD}.load_settings", return_value=custom_settings),
        patch(f"{BUILDER_MOD}.GitHubClient") as mock_client_cls,
    ):
        mock_client = mock_client_cls.return_value
        mock_client.get_pull_request.return_value = pr
        mock_client.get_pull_request_files.return_value = [changed_file]
        mock_client.get_pull_request_diff.return_value = "diff dummy"
        mock_client.get_file_content.return_value = SERVICE_CONTENT

        pkg = build_context("test-owner", "test-repo", 10, caller_index=mock_index)

        mock_index.find_caller_files.assert_called_once()
        mock_client.get_repository_tree.assert_not_called()
        assert any(lim.limitation_type == LimitationType.CALLER_INDEX_EMPTY for lim in pkg.limitations)


def test_no_index_old_behaviour() -> None:
    """When caller_index is None, behaviour is identical to the original tree discovery."""
    pr, changed_file = _create_test_pr()

    custom_settings = Settings(
        github_token="fake",
        dl_ranking_enabled=True,
        max_candidate_files=20,
    )

    with (
        patch(f"{BUILDER_MOD}.load_settings", return_value=custom_settings),
        patch(f"{BUILDER_MOD}.GitHubClient") as mock_client_cls,
    ):
        mock_client = mock_client_cls.return_value
        mock_client.get_pull_request.return_value = pr
        mock_client.get_pull_request_files.return_value = [changed_file]
        mock_client.get_pull_request_diff.return_value = "diff dummy"
        mock_client.get_repository_tree.return_value = (
            [{"path": "src/fallback.py", "type": "blob", "size": 100}],
            False,
        )

        def fake_get_file_content(owner: str, repo: str, path: str, sha: str) -> str:
            if path == "src/service.py":
                return SERVICE_CONTENT
            if path == "src/fallback.py":
                return FALLBACK_CONTENT
            raise FileNotFoundError(path)

        mock_client.get_file_content.side_effect = fake_get_file_content

        pkg = build_context("test-owner", "test-repo", 10, caller_index=None)

        # Standard tree scan invoked
        mock_client.get_repository_tree.assert_called_once()
        snippet_paths = [s.file_path for s in pkg.evidence_snippets]
        assert "src/fallback.py" in snippet_paths
