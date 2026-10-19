"""Shared pytest fixtures for Context Builder Agent."""

import pytest

from pr_context_builder.models.pr_metadata import (
    ChangedFile,
    DiffHunk,
    FileChangeStatus,
    PullRequest,
)
from pr_context_builder.models.repository import CodeSnippet, EvidenceOrigin


@pytest.fixture
def sample_diff_hunk() -> DiffHunk:
    return DiffHunk(
        old_start=10,
        old_lines=5,
        new_start=10,
        new_lines=8,
        header="def calculate_total():",
        lines=[
            " def calculate_total(items):",
            "-    total = 0",
            "+    total = 0.0",
            "+    if not items:",
            "+        return total",
            "     for item in items:",
        ],
    )


@pytest.fixture
def sample_changed_file(sample_diff_hunk: DiffHunk) -> ChangedFile:
    return ChangedFile(
        filename="src/calculator.py",
        status=FileChangeStatus.MODIFIED,
        additions=3,
        deletions=1,
        changes=4,
        patch="@@ -10,5 +10,8 @@ def calculate_total():\n-total = 0\n+total = 0.0",
        hunks=[sample_diff_hunk],
        is_binary=False,
    )


@pytest.fixture
def sample_pull_request(sample_changed_file: ChangedFile) -> PullRequest:
    return PullRequest(
        owner="octocat",
        repo="Hello-World",
        number=42,
        title="Fix precision in calculate_total",
        body="Addresses floating point precision when calculating total sums.",
        base_sha="6dcb09b5b57875f334f61aebed695e2e4193db5e",
        base_branch="main",
        head_sha="7fd1a60b01f91b314f59955a4e4d4e80d8dee11d",
        head_branch="fix-precision",
        changed_files=[sample_changed_file],
    )


@pytest.fixture
def sample_code_snippet() -> CodeSnippet:
    return CodeSnippet(
        file_path="src/calculator.py",
        start_line=10,
        end_line=18,
        content="def calculate_total(items):\n    total = 0.0\n    return total\n",
        commit_sha="7fd1a60b01f91b314f59955a4e4d4e80d8dee11d",
        origin=EvidenceOrigin.DIRECT_DIFF,
        language="python",
    )
