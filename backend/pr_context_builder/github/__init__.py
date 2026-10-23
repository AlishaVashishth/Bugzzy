"""GitHub API client and diff parsing subpackage."""

from pr_context_builder.github.client import GitHubClient, GitHubClientProtocol
from pr_context_builder.github.diff_parser import parse_patch_hunks

__all__ = ["GitHubClient", "GitHubClientProtocol", "parse_patch_hunks"]
