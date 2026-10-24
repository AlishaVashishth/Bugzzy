"""Automated unit tests for Context Builder pipeline orchestration.

Verifies end-to-end context assembly, tree discovery, candidate filtering,
deleted-file provenance, deep learning ranking integration, and token budgeting.
All tests run with mocked GitHub and model dependencies (no live network/weights).
"""

from unittest.mock import MagicMock, patch

import pytest

from pr_context_builder.builder.context_builder import build_context
from pr_context_builder.config import Settings
from pr_context_builder.exceptions import GitHubAPIError
from pr_context_builder.models.context_package import (
    ContextPackage,
    LimitationType,
    SemanticRelevanceEstimate,
)
from pr_context_builder.models.pr_metadata import (
    ChangedFile,
    DiffHunk,
    FileChangeStatus,
    PRState,
    PullRequest,
)
from pr_context_builder.models.repository import CodeSnippet, EvidenceOrigin
from pr_context_builder.semantic.encoder import ModelDependencyMissingError

BUILDER_MOD = "pr_context_builder.builder.context_builder"


def _dummy_pr() -> PullRequest:
    return PullRequest(
        number=42,
        title="Fix auth token validation",
        body="Resolves token expiration edge case.",
        state=PRState.OPEN,
        html_url="https://github.com/example/repo/pull/42",
        head_sha="head_sha_123456",
        head_branch="feature/fix-auth",
        base_sha="base_sha_abcdef",
        base_branch="main",
        owner="example",
        repo="repo",
        author="alice",
    )


def _dummy_changed_file() -> ChangedFile:
    hunk = DiffHunk(
        old_start=10,
        old_lines=2,
        new_start=10,
        new_lines=3,
        lines=[" def validate(token):", "-    return False", "+    return True"],
    )
    return ChangedFile(
        filename="src/auth.py",
        status=FileChangeStatus.MODIFIED,
        additions=1,
        deletions=1,
        changes=2,
        patch="@@ -10,2 +10,3 @@\n def validate(token):\n-    return False\n+    return True",
        hunks=[hunk],
    )


def _dummy_source_code() -> str:
    lines = [f"# Setup line {i}" for i in range(1, 10)]
    lines.append("def validate(token):")
    lines.append("    return True")
    lines.extend([f"# Trailing line {i}" for i in range(12, 20)])
    return "\n".join(lines) + "\n"


def test_build_context_invalid_inputs() -> None:
    """Ensure invalid public arguments raise ValueError immediately."""
    with pytest.raises(ValueError, match="Repository owner"):
        build_context("", "repo", 42)
    with pytest.raises(ValueError, match="Repository name"):
        build_context("owner", "", 42)
    with pytest.raises(ValueError, match="Pull request number"):
        build_context("owner", "repo", 0)
    with pytest.raises(ValueError, match="Pull request number"):
        build_context("owner", "repo", -1)


def test_build_context_github_api_failure() -> None:
    """Ensure authoritative API errors propagate without being hidden by generic RuntimeError."""
    with patch(f"{BUILDER_MOD}.GitHubClient") as mock_client_cls:
        mock_client = mock_client_cls.return_value
        mock_client.get_pull_request.side_effect = GitHubAPIError("Not Found", status_code=404)

        with pytest.raises(GitHubAPIError, match="Not Found"):
            build_context("owner", "repo", 42)


def test_build_context_success_minimal() -> None:
    """Ensure successful PR assembly with authoritative SHAs and recalculated tokens."""
    pr = _dummy_pr()
    changed_file = _dummy_changed_file()
    source_content = "import os\n\n\ndef validate(token):\n    return True\n"
    diff_text = "diff --git a/src/auth.py b/src/auth.py\n@@ -10,2 +10,3 @@"

    with patch(f"{BUILDER_MOD}.GitHubClient") as mock_client_cls:
        mock_client = mock_client_cls.return_value
        mock_client.get_pull_request.return_value = pr
        mock_client.get_pull_request_files.return_value = [changed_file]
        mock_client.get_pull_request_diff.return_value = diff_text
        mock_client.get_file_content.return_value = source_content

        pkg: ContextPackage = build_context("example", "repo", 42)

        assert pkg.head_sha == "head_sha_123456"
        assert pkg.base_sha == "base_sha_abcdef"
        assert len(pkg.changed_files) == 1
        assert pkg.changed_files[0].filename == "src/auth.py"
        assert len(pkg.evidence_snippets) >= 1
        assert pkg.total_tokens_estimated > 0
        assert pkg.semantic_matches == []


def test_build_context_tree_discovery_and_truncation() -> None:
    """Ensure Git Trees discovery excludes vendors, handles truncation, and skips huge files."""
    pr = _dummy_pr()
    changed_file = _dummy_changed_file()

    tree_entries = [
        {"path": "src/auth.py", "type": "blob", "size": 100},
        {"path": ".venv/lib/site.py", "type": "blob", "size": 100},
        {"path": "src/huge.py", "type": "blob", "size": 500_000},
        {"path": "tests/test_auth.py", "type": "blob", "size": 300},
        {"path": "src/user.py", "type": "blob", "size": 200},
        {"path": "docs/readme.txt", "type": "blob", "size": 50},
    ]

    custom_settings = Settings(
        github_token="fake_token",
        dl_ranking_enabled=True,
        max_context_tokens_budget=16000,
    )

    with (
        patch(f"{BUILDER_MOD}.load_settings", return_value=custom_settings),
        patch(f"{BUILDER_MOD}.GitHubClient") as mock_client_cls,
    ):
        mock_client = mock_client_cls.return_value
        mock_client.get_pull_request.return_value = pr
        mock_client.get_pull_request_files.return_value = [changed_file]
        mock_client.get_pull_request_diff.return_value = "diff --git"
        mock_client.get_repository_tree.return_value = (tree_entries, True)
        mock_client.get_file_content.side_effect = lambda o, r, path, sha: (
            f"# Content of {path}\ndef foo(): pass\n"
        )

        pkg = build_context("example", "repo", 42)

        limit_types = [lim.limitation_type for lim in pkg.limitations]
        assert LimitationType.RATE_LIMIT_TRUNCATION in limit_types

        oversized_limits = [
            lim
            for lim in pkg.limitations
            if lim.limitation_type == LimitationType.BUDGET_EXCEEDED
            and lim.target_path == "src/huge.py"
        ]
        assert len(oversized_limits) == 1

        download_calls = [call[0][2] for call in mock_client.get_file_content.call_args_list]
        assert "tests/test_auth.py" in download_calls


def test_build_context_candidate_count_limit() -> None:
    """Ensure candidate count limit caps tree downloads before fetching."""
    pr = _dummy_pr()
    changed_file = _dummy_changed_file()

    tree_entries = [{"path": f"src/m_{i}.py", "type": "blob", "size": 100} for i in range(30)]

    custom_settings = Settings(
        github_token="fake_token",
        dl_ranking_enabled=True,
    )

    with (
        patch(f"{BUILDER_MOD}.load_settings", return_value=custom_settings),
        patch(f"{BUILDER_MOD}.GitHubClient") as mock_client_cls,
    ):
        mock_client = mock_client_cls.return_value
        mock_client.get_pull_request.return_value = pr
        mock_client.get_pull_request_files.return_value = [changed_file]
        mock_client.get_pull_request_diff.return_value = "diff"
        mock_client.get_repository_tree.return_value = (tree_entries, False)
        mock_client.get_file_content.return_value = "def helper(): pass\n"

        pkg = build_context("example", "repo", 42)

        candidate_downloads = [
            call for call in mock_client.get_file_content.call_args_list if "src/m_" in call[0][2]
        ]
        assert len(candidate_downloads) == 20

        assert any(
            lim.limitation_type == LimitationType.BUDGET_EXCEEDED
            and "Candidate repository files limited" in lim.message
            for lim in pkg.limitations
        )


def test_build_context_deleted_file_base_sha_retrieval_and_provenance() -> None:
    """Ensure deleted files retrieve base content from base_sha and carry accurate commit SHA."""
    pr = _dummy_pr()
    hunk = DiffHunk(
        old_start=1,
        old_lines=3,
        new_start=0,
        new_lines=0,
        lines=["-def deprecated_fn():", "-    pass", "-"],
    )
    deleted_file = ChangedFile(
        filename="src/legacy.py",
        status=FileChangeStatus.REMOVED,
        additions=0,
        deletions=3,
        changes=3,
        patch="@@ -1,3 +0,0 @@\n-def deprecated_fn():\n-    pass\n-",
        hunks=[hunk],
    )

    base_content = "def deprecated_fn():\n    pass\n"

    with patch(f"{BUILDER_MOD}.GitHubClient") as mock_client_cls:
        mock_client = mock_client_cls.return_value
        mock_client.get_pull_request.return_value = pr
        mock_client.get_pull_request_files.return_value = [deleted_file]
        mock_client.get_pull_request_diff.return_value = "diff"
        mock_client.get_file_content.return_value = base_content

        pkg = build_context("example", "repo", 42)

        mock_client.get_file_content.assert_called_with(
            "example", "repo", "src/legacy.py", pr.base_sha
        )

        del_snippets = [s for s in pkg.evidence_snippets if s.file_path == "src/legacy.py"]
        assert len(del_snippets) >= 1
        assert all(s.commit_sha == pr.base_sha for s in del_snippets)


def test_build_context_deleted_file_source_unavailable() -> None:
    """Ensure unavailable base content retains direct diff and records limitation."""
    pr = _dummy_pr()
    hunk = DiffHunk(
        old_start=1,
        old_lines=2,
        new_start=0,
        new_lines=0,
        lines=["-removed_line1", "-removed_line2"],
    )
    deleted_file = ChangedFile(
        filename="src/deleted_missing.py",
        status=FileChangeStatus.REMOVED,
        hunks=[hunk],
        patch="diff patch",
    )

    with patch(f"{BUILDER_MOD}.GitHubClient") as mock_client_cls:
        mock_client = mock_client_cls.return_value
        mock_client.get_pull_request.return_value = pr
        mock_client.get_pull_request_files.return_value = [deleted_file]
        mock_client.get_pull_request_diff.return_value = "diff"
        mock_client.get_file_content.side_effect = GitHubAPIError("Not found", status_code=404)

        pkg = build_context("example", "repo", 42)

        retained = [s for s in pkg.evidence_snippets if s.file_path == "src/deleted_missing.py"]
        assert len(retained) == 1
        assert retained[0].origin == EvidenceOrigin.DIRECT_DIFF
        assert retained[0].commit_sha == pr.base_sha

        assert any(
            lim.limitation_type == LimitationType.FILE_NOT_FOUND
            and lim.target_path == "src/deleted_missing.py"
            for lim in pkg.limitations
        )


def test_build_context_dl_ranking_enabled_and_invoked() -> None:
    """Ensure DL ranking is invoked on eligible candidate snippets with diff query."""
    pr = _dummy_pr()
    changed_file = _dummy_changed_file()

    tree_entries = [{"path": "src/related.py", "type": "blob", "size": 100}]
    custom_settings = Settings(
        github_token="fake_token",
        dl_ranking_enabled=True,
    )

    with (
        patch(f"{BUILDER_MOD}.load_settings", return_value=custom_settings),
        patch(f"{BUILDER_MOD}.GitHubClient") as mock_client_cls,
        patch(f"{BUILDER_MOD}.TransformerCodeEncoder") as mock_encoder_cls,
        patch(f"{BUILDER_MOD}.SemanticRanker") as mock_ranker_cls,
    ):
        mock_client = mock_client_cls.return_value
        mock_client.get_pull_request.return_value = pr
        mock_client.get_pull_request_files.return_value = [changed_file]
        mock_client.get_pull_request_diff.return_value = "diff text with validate"
        mock_client.get_repository_tree.return_value = (tree_entries, False)

        def _mock_content(o, r, p, s):
            if "auth.py" in p:
                return _dummy_source_code()
            return "import src.auth\n\ndef caller():\n    return src.auth.validate('token')\n"

        mock_client.get_file_content.side_effect = _mock_content

        def _mock_rank(query, candidates, **kwargs):
            return [
                SemanticRelevanceEstimate(
                    file_path=cand.file_path,
                    start_line=cand.start_line,
                    end_line=cand.end_line,
                    similarity_score=0.8521,
                    model_name="microsoft/codebert-base",
                    is_model_estimate=True,
                )
                for cand in candidates
            ]

        mock_ranker = mock_ranker_cls.return_value
        mock_ranker.rank_snippets.side_effect = _mock_rank

        pkg = build_context("example", "repo", 42)

        assert mock_encoder_cls.called
        assert mock_ranker_cls.called

        call_kwargs = mock_ranker.rank_snippets.call_args[1]
        assert call_kwargs["query"] == "diff text with validate"
        assert len(call_kwargs["candidates"]) >= 1

        assert len(pkg.semantic_matches) == 1
        assert pkg.semantic_matches[0].similarity_score == 0.8521
        assert pkg.semantic_matches[0].is_model_estimate is True


def test_build_context_dl_dependency_missing_fallback() -> None:
    """Ensure missing dependencies produce honest MODEL_UNAVAILABLE limitation."""
    pr = _dummy_pr()
    changed_file = _dummy_changed_file()
    custom_settings = Settings(github_token="fake_token", dl_ranking_enabled=True)

    with (
        patch(f"{BUILDER_MOD}.load_settings", return_value=custom_settings),
        patch(f"{BUILDER_MOD}.GitHubClient") as mock_client_cls,
        patch(f"{BUILDER_MOD}.TransformerCodeEncoder") as mock_encoder_cls,
    ):
        mock_client = mock_client_cls.return_value
        mock_client.get_pull_request.return_value = pr
        mock_client.get_pull_request_files.return_value = [changed_file]
        mock_client.get_pull_request_diff.return_value = "diff text"
        mock_client.get_repository_tree.return_value = (
            [{"path": "src/rel.py", "type": "blob", "size": 100}],
            False,
        )

        def _mock_content_missing(o, r, p, s):
            if "auth.py" in p:
                return _dummy_source_code()
            return "import src.auth\n\ndef rel(): src.auth.validate('t')\n"

        mock_client.get_file_content.side_effect = _mock_content_missing
        mock_encoder_cls.side_effect = ModelDependencyMissingError("PyTorch is not installed.")

        pkg = build_context("example", "repo", 42)

        assert any(
            lim.limitation_type == LimitationType.MODEL_UNAVAILABLE
            and "PyTorch is not installed" in lim.message
            for lim in pkg.limitations
        )
        assert pkg.semantic_matches == []
        assert len(pkg.evidence_snippets) >= 1


def test_build_context_budget_enforcement_and_sync() -> None:
    """Ensure budget limits snippets, recalculates tokens, and syncs semantic matches."""
    pr = _dummy_pr()
    changed_file = _dummy_changed_file()

    custom_settings = Settings(
        github_token="fake_token",
        dl_ranking_enabled=False,
        max_context_tokens_budget=30,
    )

    with (
        patch(f"{BUILDER_MOD}.load_settings", return_value=custom_settings),
        patch(f"{BUILDER_MOD}.GitHubClient") as mock_client_cls,
        patch(f"{BUILDER_MOD}.RepositoryCodeLocator") as mock_locator_cls,
    ):
        mock_client = mock_client_cls.return_value
        mock_client.get_pull_request.return_value = pr
        mock_client.get_pull_request_files.return_value = [changed_file]
        mock_client.get_pull_request_diff.return_value = "diff"

        snip1 = CodeSnippet(
            file_path="src/auth.py",
            start_line=1,
            end_line=10,
            content="def direct_diff(): pass\n",
            commit_sha="head1",
            origin=EvidenceOrigin.DIRECT_DIFF,
        )
        snip2 = CodeSnippet(
            file_path="src/other.py",
            start_line=1,
            end_line=20,
            content="def related_code(): " + ("word " * 100),
            commit_sha="head1",
            origin=EvidenceOrigin.REPOSITORY_REFERENCE,
        )
        mock_locator = mock_locator_cls.return_value
        mock_locator.locate_pr_code.return_value = MagicMock(
            snippets=[snip1, snip2],
            relationships=[],
            limitations=[],
        )

        pkg = build_context("example", "repo", 42)

        assert len(pkg.evidence_snippets) == 1
        assert pkg.evidence_snippets[0].file_path == "src/auth.py"

        assert any(lim.limitation_type == LimitationType.BUDGET_EXCEEDED for lim in pkg.limitations)
        assert pkg.total_tokens_estimated < 100


def test_build_context_budget_fallback_preserves_first_diff() -> None:
    """Ensure safe fallback preserves primary direct-diff snippet even if budget is tiny."""
    pr = _dummy_pr()
    changed_file = _dummy_changed_file()

    custom_settings = Settings(
        github_token="fake_token",
        dl_ranking_enabled=False,
        max_context_tokens_budget=1,
    )

    with (
        patch(f"{BUILDER_MOD}.load_settings", return_value=custom_settings),
        patch(f"{BUILDER_MOD}.GitHubClient") as mock_client_cls,
    ):
        mock_client = mock_client_cls.return_value
        mock_client.get_pull_request.return_value = pr
        mock_client.get_pull_request_files.return_value = [changed_file]
        mock_client.get_pull_request_diff.return_value = "diff"
        mock_client.get_file_content.return_value = _dummy_source_code()

        pkg = build_context("example", "repo", 42)

        assert len(pkg.evidence_snippets) >= 1
        assert any(
            s.origin in (EvidenceOrigin.DIRECT_DIFF, EvidenceOrigin.ENCLOSING_SCOPE)
            for s in pkg.evidence_snippets
        )
        assert any(lim.limitation_type == LimitationType.BUDGET_EXCEEDED for lim in pkg.limitations)


def test_build_context_test_snippets_segregated() -> None:
    """Ensure test snippets are segregated into test_snippets field in ContextPackage."""
    pr = _dummy_pr()
    changed_file = _dummy_changed_file()

    with (
        patch(f"{BUILDER_MOD}.GitHubClient") as mock_client_cls,
        patch(f"{BUILDER_MOD}.RepositoryCodeLocator") as mock_locator_cls,
    ):
        mock_client = mock_client_cls.return_value
        mock_client.get_pull_request.return_value = pr
        mock_client.get_pull_request_files.return_value = [changed_file]
        mock_client.get_pull_request_diff.return_value = "diff"

        snip_code = CodeSnippet(
            file_path="src/auth.py",
            start_line=1,
            end_line=5,
            content="def auth(): pass\n",
            commit_sha="head1",
            origin=EvidenceOrigin.DIRECT_DIFF,
        )
        snip_test = CodeSnippet(
            file_path="tests/test_auth.py",
            start_line=1,
            end_line=10,
            content="def test_auth(): pass\n",
            commit_sha="head1",
            origin=EvidenceOrigin.TEST_FILE,
        )

        mock_locator = mock_locator_cls.return_value
        mock_locator.locate_pr_code.return_value = MagicMock(
            snippets=[snip_code, snip_test],
            relationships=[],
            limitations=[],
        )

        pkg = build_context("example", "repo", 42)

        assert len(pkg.evidence_snippets) == 1
        assert pkg.evidence_snippets[0].file_path == "src/auth.py"
        assert len(pkg.test_snippets) == 1
        assert pkg.test_snippets[0].file_path == "tests/test_auth.py"


def test_build_context_missing_diff_and_binary_file() -> None:
    """Ensure missing PR diff and binary file produce honest limitations without crashing."""
    pr = _dummy_pr()
    binary_file = ChangedFile(
        filename="assets/logo.png",
        status=FileChangeStatus.MODIFIED,
        is_binary=True,
        patch=None,
    )

    custom_settings = Settings(github_token="fake_token", dl_ranking_enabled=True)

    with (
        patch(f"{BUILDER_MOD}.load_settings", return_value=custom_settings),
        patch(f"{BUILDER_MOD}.GitHubClient") as mock_client_cls,
    ):
        mock_client = mock_client_cls.return_value
        mock_client.get_pull_request.return_value = pr
        mock_client.get_pull_request_files.return_value = [binary_file]
        mock_client.get_pull_request_diff.side_effect = GitHubAPIError(
            "Diff too large", status_code=400
        )
        mock_client.get_repository_tree.return_value = ([], False)

        pkg = build_context("example", "repo", 42)

        limit_types = [lim.limitation_type for lim in pkg.limitations]
        assert LimitationType.API_ERROR in limit_types
        assert LimitationType.BINARY_FILE_SKIPPED in limit_types
        assert LimitationType.INFERENCE_ERROR in limit_types


def test_dl_relevance_affects_candidate_selection_order() -> None:
    """Ensure DL ranking relevance score sorts candidates before budget truncation."""
    pr = _dummy_pr()
    changed_file = _dummy_changed_file()

    custom_settings = Settings(
        github_token="fake_token",
        dl_ranking_enabled=True,
        max_context_tokens_budget=40,  # Tight budget: allows diff + 1 candidate only
    )

    with (
        patch(f"{BUILDER_MOD}.load_settings", return_value=custom_settings),
        patch(f"{BUILDER_MOD}.GitHubClient") as mock_client_cls,
        patch(f"{BUILDER_MOD}.RepositoryCodeLocator") as mock_locator_cls,
        patch(f"{BUILDER_MOD}.TransformerCodeEncoder"),
        patch(f"{BUILDER_MOD}.SemanticRanker") as mock_ranker_cls,
    ):
        mock_client = mock_client_cls.return_value
        mock_client.get_pull_request.return_value = pr
        mock_client.get_pull_request_files.return_value = [changed_file]
        mock_client.get_pull_request_diff.return_value = "diff --git a/src/auth.py"
        mock_client.get_repository_tree.return_value = ([], False)

        diff_snip = CodeSnippet(
            file_path="src/auth.py",
            start_line=10,
            end_line=12,
            content="def validate(token):\n    return True\n",
            commit_sha="head1",
            origin=EvidenceOrigin.DIRECT_DIFF,
        )
        # Low relevance candidate appears FIRST in discovery order
        cand_low = CodeSnippet(
            file_path="src/low_rel.py",
            start_line=1,
            end_line=5,
            content="def low_relevance():\n    pass\n",
            commit_sha="head1",
            origin=EvidenceOrigin.REPOSITORY_REFERENCE,
        )
        # High relevance candidate appears SECOND in discovery order
        cand_high = CodeSnippet(
            file_path="src/high_rel.py",
            start_line=1,
            end_line=5,
            content="def high_relevance():\n    pass\n",
            commit_sha="head1",
            origin=EvidenceOrigin.REPOSITORY_REFERENCE,
        )

        mock_locator = mock_locator_cls.return_value
        mock_locator.locate_pr_code.return_value = MagicMock(
            snippets=[diff_snip, cand_low, cand_high],
            relationships=[],
            limitations=[],
        )

        def _mock_rank(query, candidates, **kwargs):
            # Assign scores: low gets 0.15, high gets 0.95
            for c in candidates:
                if c.file_path == "src/low_rel.py":
                    c.relevance_score = 0.15
                    c.origin = EvidenceOrigin.SEMANTIC_RANKING
                elif c.file_path == "src/high_rel.py":
                    c.relevance_score = 0.95
                    c.origin = EvidenceOrigin.SEMANTIC_RANKING
            return [
                SemanticRelevanceEstimate(
                    file_path="src/high_rel.py",
                    start_line=1,
                    end_line=5,
                    similarity_score=0.95,
                    model_name="codebert",
                    is_model_estimate=True,
                ),
                SemanticRelevanceEstimate(
                    file_path="src/low_rel.py",
                    start_line=1,
                    end_line=5,
                    similarity_score=0.15,
                    model_name="codebert",
                    is_model_estimate=True,
                ),
            ]

        mock_ranker = mock_ranker_cls.return_value
        mock_ranker.rank_snippets.side_effect = _mock_rank

        pkg = build_context("example", "repo", 42)

        retained_paths = [s.file_path for s in pkg.evidence_snippets]
        # Direct diff is preserved first
        assert retained_paths[0] == "src/auth.py"
        # High relevance candidate was chosen ahead of low relevance candidate
        assert "src/high_rel.py" in retained_paths
        assert "src/low_rel.py" not in retained_paths

        # Semantic matches are synchronized: only high_rel is present
        assert len(pkg.semantic_matches) == 1
        assert pkg.semantic_matches[0].file_path == "src/high_rel.py"


def test_oversized_single_primary_diff_limitation_message() -> None:
    """Ensure oversized primary diff without other snippets reports honest limitation."""
    pr = _dummy_pr()
    changed_file = _dummy_changed_file()

    custom_settings = Settings(
        github_token="fake_token",
        dl_ranking_enabled=False,
        max_context_tokens_budget=1,  # Ultra-tiny budget
    )

    with (
        patch(f"{BUILDER_MOD}.load_settings", return_value=custom_settings),
        patch(f"{BUILDER_MOD}.GitHubClient") as mock_client_cls,
    ):
        mock_client = mock_client_cls.return_value
        mock_client.get_pull_request.return_value = pr
        mock_client.get_pull_request_files.return_value = [changed_file]
        mock_client.get_pull_request_diff.return_value = "diff"
        mock_client.get_file_content.return_value = _dummy_source_code()

        pkg = build_context("example", "repo", 42)

        # Primary diff is preserved
        assert len(pkg.evidence_snippets) == 1
        assert pkg.evidence_snippets[0].file_path == "src/auth.py"
        # Total tokens reflect the retained diff
        assert pkg.total_tokens_estimated > 1

        # Limitation explicitly reports that budget was exceeded
        budget_limits = [
            lim for lim in pkg.limitations if lim.limitation_type == LimitationType.BUDGET_EXCEEDED
        ]
        assert len(budget_limits) == 1
        msg = budget_limits[0].message
        assert "exceeded" in msg
        assert "Primary direct diff was preserved as essential evidence despite exceeding" in msg
        # Must not claim that 0 snippets were omitted to fit budget
        assert "0 lower-priority" not in msg
        assert "omitted to fit budget" not in msg


def test_oversized_primary_diff_with_other_snippets_limitation_message() -> None:
    """Ensure oversized primary diff with other omitted snippets reports both accurately."""
    pr = _dummy_pr()
    changed_file = _dummy_changed_file()

    custom_settings = Settings(
        github_token="fake_token",
        dl_ranking_enabled=False,
        max_context_tokens_budget=5,  # Tiny budget
    )

    with (
        patch(f"{BUILDER_MOD}.load_settings", return_value=custom_settings),
        patch(f"{BUILDER_MOD}.GitHubClient") as mock_client_cls,
        patch(f"{BUILDER_MOD}.RepositoryCodeLocator") as mock_locator_cls,
    ):
        mock_client = mock_client_cls.return_value
        mock_client.get_pull_request.return_value = pr
        mock_client.get_pull_request_files.return_value = [changed_file]
        mock_client.get_pull_request_diff.return_value = "diff"

        diff_snip = CodeSnippet(
            file_path="src/auth.py",
            start_line=1,
            end_line=10,
            content="def big_diff(): " + ("code " * 50),
            commit_sha="head1",
            origin=EvidenceOrigin.DIRECT_DIFF,
        )
        cand1 = CodeSnippet(
            file_path="src/extra1.py",
            start_line=1,
            end_line=5,
            content="def extra1(): pass\n",
            commit_sha="head1",
            origin=EvidenceOrigin.REPOSITORY_REFERENCE,
        )
        cand2 = CodeSnippet(
            file_path="src/extra2.py",
            start_line=1,
            end_line=5,
            content="def extra2(): pass\n",
            commit_sha="head1",
            origin=EvidenceOrigin.REPOSITORY_REFERENCE,
        )

        mock_locator = mock_locator_cls.return_value
        mock_locator.locate_pr_code.return_value = MagicMock(
            snippets=[diff_snip, cand1, cand2],
            relationships=[],
            limitations=[],
        )

        pkg = build_context("example", "repo", 42)

        # Primary diff is preserved, cand1 and cand2 are omitted
        assert len(pkg.evidence_snippets) == 1
        assert pkg.evidence_snippets[0].file_path == "src/auth.py"

        budget_limits = [
            lim for lim in pkg.limitations if lim.limitation_type == LimitationType.BUDGET_EXCEEDED
        ]
        assert len(budget_limits) == 1
        msg = budget_limits[0].message
        assert "exceeded" in msg
        assert "Primary direct diff was preserved as essential evidence despite exceeding" in msg
        assert "2 lower-priority snippet(s) were omitted" in msg


def test_context_builder_budget_prioritizes_call_hierarchy_over_other_candidates() -> None:
    """Ensure CALL_HIERARCHY snippets are prioritized directly behind direct diffs."""
    pr = _dummy_pr()
    changed_file = _dummy_changed_file()

    custom_settings = Settings(
        github_token="fake_token",
        dl_ranking_enabled=False,
        max_context_tokens_budget=35,  # Allows diff + call hierarchy in budget
    )

    with (
        patch(f"{BUILDER_MOD}.load_settings", return_value=custom_settings),
        patch(f"{BUILDER_MOD}.GitHubClient") as mock_client_cls,
        patch(f"{BUILDER_MOD}.RepositoryCodeLocator") as mock_locator_cls,
    ):
        mock_client = mock_client_cls.return_value
        mock_client.get_pull_request.return_value = pr
        mock_client.get_pull_request_files.return_value = [changed_file]
        mock_client.get_pull_request_diff.return_value = "diff"

        diff_snip = CodeSnippet(
            file_path="src/auth.py",
            start_line=10,
            end_line=12,
            content="def validate(): return True\n",
            commit_sha="head1",
            origin=EvidenceOrigin.ENCLOSING_SCOPE,
        )
        call_hier_snip = CodeSnippet(
            file_path="src/auth.py",
            start_line=1,
            end_line=3,
            content="def helper(): return 1\n",
            commit_sha="head1",
            origin=EvidenceOrigin.CALL_HIERARCHY,
        )
        other_snip = CodeSnippet(
            file_path="src/other.py",
            start_line=1,
            end_line=5,
            content="def distant_ref(): " + ("extra_tokens " * 30),
            commit_sha="head1",
            origin=EvidenceOrigin.REPOSITORY_REFERENCE,
        )

        mock_locator = mock_locator_cls.return_value
        mock_locator.locate_pr_code.return_value = MagicMock(
            snippets=[diff_snip, call_hier_snip, other_snip],
            relationships=[],
            limitations=[],
        )

        pkg = build_context("example", "repo", 42)

        retained = [s.file_path for s in pkg.evidence_snippets]
        assert "src/auth.py" in retained
        # CALL_HIERARCHY was retained
        assert any(s.origin == EvidenceOrigin.CALL_HIERARCHY for s in pkg.evidence_snippets)
        # Low priority REPOSITORY_REFERENCE was omitted
        assert not any(
            s.origin == EvidenceOrigin.REPOSITORY_REFERENCE for s in pkg.evidence_snippets
        )
