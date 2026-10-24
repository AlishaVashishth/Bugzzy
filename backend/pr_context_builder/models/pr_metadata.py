"""Data models representing Pull Request metadata, diffs, and changed files."""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class PRState(StrEnum):
    """Lifecycle state of a Pull Request."""

    OPEN = "open"
    CLOSED = "closed"
    MERGED = "merged"


class FileChangeStatus(StrEnum):
    """Status indicating how a file was changed in the PR."""

    ADDED = "added"
    MODIFIED = "modified"
    REMOVED = "removed"
    RENAMED = "renamed"
    COPIED = "copied"
    CHANGED = "changed"
    UNCHANGED = "unchanged"


class DiffHunk(BaseModel):
    """Represents a unified diff hunk with accurate line coordinates."""

    old_start: int = Field(..., description="Starting line in the base version")
    old_lines: int = Field(..., description="Number of lines in the base version")
    new_start: int = Field(..., description="Starting line in the head version")
    new_lines: int = Field(..., description="Number of lines in the head version")
    header: str = Field(default="", description="Optional hunk header / symbol context")
    lines: list[str] = Field(default_factory=list, description="Raw lines including +, -, or space")

    def get_modified_head_lines(self) -> set[int]:
        """Returns the set of line numbers in the head version that are added or modified (+)."""
        if not self.lines:
            if self.new_lines <= 0:
                return set()
            return set(range(self.new_start, self.new_start + self.new_lines))

        has_markers = any(line.startswith(("+", "-", " ")) for line in self.lines)
        if not has_markers:
            if self.new_lines <= 0:
                return set()
            return set(range(self.new_start, self.new_start + self.new_lines))

        has_context_lines = any(line.startswith(" ") for line in self.lines)
        head_line_count = len([line for line in self.lines if not line.startswith(("-", "\\"))])
        if not has_context_lines and head_line_count < self.new_lines:
            # Synthetic / truncated hunk mock without full context line enumeration
            return set(range(self.new_start, self.new_start + self.new_lines))

        modified: set[int] = set()
        curr = self.new_start
        for line in self.lines:
            if line.startswith("+"):
                modified.add(curr)
                curr += 1
            elif line.startswith("-") or line.startswith("\\"):
                continue
            else:
                curr += 1
        return modified


class ChangedFile(BaseModel):
    """Detailed information on a single file modified in a PR."""

    filename: str = Field(..., description="Target file path in repository")
    status: FileChangeStatus = Field(
        ..., description="Change type (modified, added, removed, etc.)"
    )
    additions: int = Field(default=0, ge=0)
    deletions: int = Field(default=0, ge=0)
    changes: int = Field(default=0, ge=0)
    patch: str | None = Field(default=None, description="Raw unified diff patch if text file")
    hunks: list[DiffHunk] = Field(default_factory=list, description="Parsed diff hunks")
    is_binary: bool = Field(default=False, description="Flag indicating if the file is binary")
    previous_filename: str | None = Field(default=None, description="Original filename if renamed")
    blob_sha: str | None = Field(default=None, description="Git object SHA of file content")

    def get_modified_head_lines(self) -> set[int]:
        """Returns the set of all line numbers in the head version that are added or modified."""
        lines: set[int] = set()
        for hunk in self.hunks:
            lines.update(hunk.get_modified_head_lines())
        return lines


class PullRequest(BaseModel):
    """Metadata and contents of a GitHub Pull Request."""

    owner: str = Field(..., description="Repository owner or organization")
    repo: str = Field(..., description="Repository name")
    number: int = Field(..., ge=1, description="PR sequence number")
    title: str = Field(..., description="PR title")
    body: str | None = Field(default=None, description="PR description body")
    state: PRState = Field(default=PRState.OPEN)
    base_sha: str = Field(..., description="Base commit SHA (target of PR)")
    base_branch: str = Field(..., description="Target branch name")
    head_sha: str = Field(..., description="Head commit SHA (source of PR)")
    head_branch: str = Field(..., description="Source branch name")
    author: str | None = Field(default=None, description="PR creator username")
    created_at: datetime | None = Field(default=None)
    updated_at: datetime | None = Field(default=None)
    changed_files: list[ChangedFile] = Field(
        default_factory=list, description="List of changed files in this PR"
    )
