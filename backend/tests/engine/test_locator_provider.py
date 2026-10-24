"""Tests for candidate_provider support in RepositoryCodeLocator."""

from unittest.mock import MagicMock

from pr_context_builder.github.client import GitHubClientProtocol
from pr_context_builder.models.pr_metadata import (
    ChangedFile,
    DiffHunk,
    FileChangeStatus,
    PullRequest,
)
from pr_context_builder.models.repository import EvidenceOrigin
from pr_context_builder.retrieval.locator import RepositoryCodeLocator

SOURCE_CODE = """def zeta():
    pass

def alpha():
    pass
"""


def _create_test_pr() -> PullRequest:
    # Hunk 1 touches zeta (lines 1-2)
    hunk1 = DiffHunk(
        old_start=1,
        old_lines=2,
        new_start=1,
        new_lines=2,
        header="def zeta():",
        lines=["-def zeta():", "+def zeta():"],
    )
    # Hunk 2 touches alpha (lines 4-5)
    hunk2 = DiffHunk(
        old_start=4,
        old_lines=2,
        new_start=4,
        new_lines=2,
        header="def alpha():",
        lines=["-def alpha():", "+def alpha():"],
    )
    # Hunk 3 touches zeta again (lines 1-2) to verify deduplication
    hunk3 = DiffHunk(
        old_start=2,
        old_lines=1,
        new_start=2,
        new_lines=1,
        header="pass",
        lines=["-    pass", "+    pass"],
    )
    changed_file = ChangedFile(
        filename="src/service.py",
        status=FileChangeStatus.MODIFIED,
        patch="dummy",
        hunks=[hunk1, hunk2, hunk3],
    )
    return PullRequest(
        owner="test-owner",
        repo="test-repo",
        number=42,
        title="Test PR",
        base_sha="base_sha",
        base_branch="main",
        head_sha="head_sha",
        head_branch="feature",
        changed_files=[changed_file],
    )


def test_candidate_provider_called_with_sorted_unique_names_and_used() -> None:
    """Ensure candidate_provider is invoked with sorted unique symbol names and results are used."""
    mock_client = MagicMock(spec=GitHubClientProtocol)
    mock_client.get_file_content.return_value = SOURCE_CODE
    locator = RepositoryCodeLocator(github_client=mock_client)
    pr = _create_test_pr()

    provider = MagicMock()
    provider.return_value = {
        "src/consumer.py": "from src.service import alpha\n\ndef run():\n    alpha()\n"
    }

    result = locator.locate_pr_code(pr, candidate_provider=provider)

    # Provider should be called with sorted unique symbol names: ['alpha', 'zeta']
    provider.assert_called_once_with(["alpha", "zeta"])

    # Provider's returned files should be used for related-code discovery
    file_paths = [s.file_path for s in result.snippets]
    assert "src/consumer.py" in file_paths

    related_snippet = next(s for s in result.snippets if s.file_path == "src/consumer.py")
    assert related_snippet.origin == EvidenceOrigin.DIRECT_DEPENDENCY
    assert any(
        rel.source_symbol == "src/consumer.py" and rel.target_symbol == "alpha"
        for rel in result.relationships
    )


def test_no_provider_preserves_old_behaviour() -> None:
    """Ensure locate_pr_code behaves identically to old implementation when candidate_provider is None."""
    mock_client = MagicMock(spec=GitHubClientProtocol)
    mock_client.get_file_content.return_value = SOURCE_CODE
    locator = RepositoryCodeLocator(github_client=mock_client)
    pr = _create_test_pr()

    # Case 1: candidate_file_contents passed explicitly without provider
    prefetched_candidates = {
        "src/consumer.py": "from src.service import alpha\n\ndef run():\n    alpha()\n"
    }
    result_with_candidates = locator.locate_pr_code(
        pr,
        candidate_file_contents=prefetched_candidates,
        candidate_provider=None,
    )
    assert any(s.file_path == "src/consumer.py" for s in result_with_candidates.snippets)
    assert any(
        rel.source_symbol == "src/consumer.py" and rel.target_symbol == "alpha"
        for rel in result_with_candidates.relationships
    )

    # Case 2: default call with no candidates and no provider
    result_default = locator.locate_pr_code(pr)
    assert len(result_default.relationships) == 0
    assert not any(s.file_path == "src/consumer.py" for s in result_default.snippets)
