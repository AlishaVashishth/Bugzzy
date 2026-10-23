"""GitHub REST API client implementation.

Provides authenticated, paginated, and error-resilient access to GitHub Pull Requests,
diff patches, and repository file contents at specific commit SHAs.
"""

import base64
import re
from datetime import datetime
from typing import Any, Protocol

import requests

from pr_context_builder.config import Settings
from pr_context_builder.exceptions import (
    AuthenticationError,
    GitHubAPIError,
    RateLimitExceededError,
    ResourceNotFoundError,
)
from pr_context_builder.github.diff_parser import parse_patch_hunks
from pr_context_builder.models.pr_metadata import (
    ChangedFile,
    FileChangeStatus,
    PRState,
    PullRequest,
)

TOKEN_REDACTION_PATTERNS = [
    re.compile(r"(ghp_[A-Za-z0-9_]{36,255})"),
    re.compile(r"(github_pat_[A-Za-z0-9_]{82})"),
    re.compile(r"(Bearer\s+)[A-Za-z0-9_\-\.]{20,}", re.IGNORECASE),
]


class GitHubClientProtocol(Protocol):
    """Protocol contract defining required GitHub API interactions."""

    def get_pull_request(self, owner: str, repo: str, pull_number: int) -> PullRequest:
        """Fetch pull request metadata."""
        ...

    def get_pull_request_files(self, owner: str, repo: str, pull_number: int) -> list[ChangedFile]:
        """Fetch list of changed files with diff patches."""
        ...

    def get_file_content(self, owner: str, repo: str, file_path: str, ref: str) -> str:
        """Fetch raw file content at a specific commit SHA or ref."""
        ...

    def get_pull_request_diff(self, owner: str, repo: str, pull_number: int) -> str:
        """Fetch unified diff for the full pull request."""
        ...

    def get_repository_tree(
        self, owner: str, repo: str, tree_sha: str, recursive: bool = True
    ) -> tuple[list[dict[str, Any]], bool]:
        """Fetch repository git tree at specific commit SHA or tree SHA."""
        ...


class GitHubClient:
    """Official GitHub REST API v3 client for PR context extraction."""

    def __init__(
        self,
        token: str | None = None,
        base_url: str = "https://api.github.com",
        timeout_seconds: int = 30,
        max_changed_files_limit: int = 100,
        session: requests.Session | None = None,
    ):
        self._token = token.strip() if token and token.strip() else None
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._max_changed_files_limit = max_changed_files_limit
        self._session = session or requests.Session()

    @classmethod
    def from_settings(cls, settings: Settings) -> "GitHubClient":
        """Instantiates client using application settings."""
        return cls(
            token=settings.github_token,
            base_url=settings.github_api_base_url,
            timeout_seconds=settings.http_timeout_seconds,
            max_changed_files_limit=settings.max_changed_files_limit,
        )

    def __repr__(self) -> str:
        masked_token = "***" if self._token else "None"
        return (
            f"GitHubClient("
            f"base_url={self._base_url!r}, "
            f"token={masked_token}, "
            f"timeout={self._timeout_seconds}s, "
            f"max_files_limit={self._max_changed_files_limit})"
        )

    def _sanitize_message(self, text: str) -> str:
        """Redacts all tokens and authorization credentials from output strings."""
        if not text:
            return ""
        sanitized = text
        if self._token:
            sanitized = sanitized.replace(self._token, "[REDACTED_SECRET]")
        for pattern in TOKEN_REDACTION_PATTERNS:
            sanitized = pattern.sub("[REDACTED_SECRET]", sanitized)
        return sanitized

    def _build_headers(self, accept: str = "application/vnd.github+json") -> dict[str, str]:
        headers = {
            "Accept": accept,
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "PR-Context-Builder-Agent/0.1.0",
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    def _request(
        self,
        method: str,
        endpoint: str,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
    ) -> requests.Response:
        """Executes an HTTP request with error mapping and credential sanitization."""
        url = f"{self._base_url}/{endpoint.lstrip('/')}"
        req_headers = headers if headers is not None else self._build_headers()

        try:
            response = self._session.request(
                method=method,
                url=url,
                headers=req_headers,
                params=params,
                timeout=self._timeout_seconds,
            )
        except requests.exceptions.Timeout as exc:
            raise GitHubAPIError(
                f"GitHub API request timed out after {self._timeout_seconds} seconds."
            ) from exc
        except requests.exceptions.ConnectionError as exc:
            clean_err = self._sanitize_message(str(exc))
            raise GitHubAPIError(f"Network failure connecting to GitHub API: {clean_err}") from exc
        except requests.exceptions.RequestException as exc:
            clean_err = self._sanitize_message(str(exc))
            raise GitHubAPIError(f"HTTP request to GitHub failed: {clean_err}") from exc

        self._check_status(response, endpoint)
        return response

    def _check_status(self, response: requests.Response, endpoint: str) -> None:
        """Inspects HTTP status code and headers, raising explicit domain exceptions."""
        if response.status_code in (200, 201, 204):
            return

        body = self._sanitize_message(response.text)
        status_code = response.status_code

        # Rate limit checks (403 with X-RateLimit headers or 429)
        remaining = response.headers.get("x-ratelimit-remaining")
        reset_header = response.headers.get("x-ratelimit-reset")
        reset_ts: int | None = None
        if reset_header and reset_header.isdigit():
            reset_ts = int(reset_header)

        is_rate_limited = (
            status_code == 429
            or (status_code == 403 and remaining == "0")
            or "rate limit" in body.lower()
        )

        if is_rate_limited:
            raise RateLimitExceededError(
                message=f"GitHub API rate limit exceeded on {endpoint}.",
                reset_timestamp=reset_ts,
                status_code=status_code,
            )

        if status_code == 401:
            raise AuthenticationError(
                "GitHub API authentication failed: Invalid or missing token.",
                status_code=401,
                response_body=body,
            )

        if status_code == 403:
            raise AuthenticationError(
                f"Access forbidden by GitHub API on {endpoint}: Insufficient permissions.",
                status_code=403,
                response_body=body,
            )

        if status_code == 404:
            raise ResourceNotFoundError(
                f"Resource not found on GitHub: {endpoint} (HTTP 404).",
                status_code=404,
                response_body=body,
            )

        raise GitHubAPIError(
            f"GitHub API returned error HTTP {status_code} on {endpoint}: {body}",
            status_code=status_code,
            response_body=body,
        )

    def get_pull_request(self, owner: str, repo: str, pull_number: int) -> PullRequest:
        """Fetches metadata for a GitHub Pull Request."""
        if not owner or not owner.strip():
            raise ValueError("Repository owner must be a non-empty string.")
        if not repo or not repo.strip():
            raise ValueError("Repository name must be a non-empty string.")
        if pull_number <= 0:
            raise ValueError("Pull request number must be a positive integer.")

        owner_clean = owner.strip()
        repo_clean = repo.strip()
        endpoint = f"repos/{owner_clean}/{repo_clean}/pulls/{pull_number}"

        response = self._request("GET", endpoint)

        try:
            data = response.json()
        except ValueError as exc:
            clean_body = self._sanitize_message(response.text[:200])
            raise GitHubAPIError(
                f"Malformed JSON response from GitHub API for {endpoint}: {clean_body}",
                status_code=response.status_code,
                response_body=self._sanitize_message(response.text),
            ) from exc

        if not isinstance(data, dict) or "base" not in data or "head" not in data:
            raise GitHubAPIError(
                f"Incomplete PR metadata returned for {endpoint}.",
                status_code=response.status_code,
            )

        # Parse state
        raw_state = data.get("state", "open").lower()
        if data.get("merged") is True:
            state = PRState.MERGED
        elif raw_state == "closed":
            state = PRState.CLOSED
        else:
            state = PRState.OPEN

        created_at = None
        if data.get("created_at"):
            try:
                created_at = datetime.fromisoformat(data["created_at"].replace("Z", "+00:00"))
            except ValueError:
                created_at = None

        updated_at = None
        if data.get("updated_at"):
            try:
                updated_at = datetime.fromisoformat(data["updated_at"].replace("Z", "+00:00"))
            except ValueError:
                updated_at = None

        author = None
        if isinstance(data.get("user"), dict):
            author = data["user"].get("login")

        return PullRequest(
            owner=owner_clean,
            repo=repo_clean,
            number=pull_number,
            title=data.get("title", ""),
            body=data.get("body"),
            state=state,
            base_sha=data["base"]["sha"],
            base_branch=data["base"].get("ref", ""),
            head_sha=data["head"]["sha"],
            head_branch=data["head"].get("ref", ""),
            author=author,
            created_at=created_at,
            updated_at=updated_at,
            changed_files=[],
        )

    def get_pull_request_files(self, owner: str, repo: str, pull_number: int) -> list[ChangedFile]:
        """Fetches all changed files in a pull request with pagination."""
        if not owner or not owner.strip():
            raise ValueError("Repository owner must be a non-empty string.")
        if not repo or not repo.strip():
            raise ValueError("Repository name must be a non-empty string.")
        if pull_number <= 0:
            raise ValueError("Pull request number must be a positive integer.")

        owner_clean = owner.strip()
        repo_clean = repo.strip()
        endpoint = f"repos/{owner_clean}/{repo_clean}/pulls/{pull_number}/files"

        all_files: list[ChangedFile] = []
        page = 1
        per_page = 100

        while True:
            response = self._request(
                "GET",
                endpoint,
                params={"per_page": per_page, "page": page},
            )

            try:
                page_data = response.json()
            except ValueError as exc:
                clean_body = self._sanitize_message(response.text[:200])
                raise GitHubAPIError(
                    f"Malformed JSON response from GitHub API on page {page}: {clean_body}",
                    status_code=response.status_code,
                    response_body=self._sanitize_message(response.text),
                ) from exc

            if not isinstance(page_data, list):
                got_type = type(page_data).__name__
                raise GitHubAPIError(
                    f"Expected list of changed files from {endpoint}, got {got_type}."
                )

            if not page_data:
                break

            for item in page_data:
                filename = item.get("filename", "")
                raw_status = item.get("status", "modified").lower()
                try:
                    status = FileChangeStatus(raw_status)
                except ValueError:
                    status = FileChangeStatus.MODIFIED

                patch = item.get("patch")
                hunks = parse_patch_hunks(patch) if patch else []
                additions = int(item.get("additions", 0))
                deletions = int(item.get("deletions", 0))
                changes = int(item.get("changes", additions + deletions))

                # Binary files or diffs exceeding GitHub patch limit have patch=None
                is_binary = patch is None and (changes > 0 or raw_status != "removed")

                changed_file = ChangedFile(
                    filename=filename,
                    status=status,
                    additions=additions,
                    deletions=deletions,
                    changes=changes,
                    patch=patch,
                    hunks=hunks,
                    is_binary=is_binary,
                    previous_filename=item.get("previous_filename"),
                    blob_sha=item.get("sha"),
                )
                all_files.append(changed_file)

                if len(all_files) >= self._max_changed_files_limit:
                    return all_files

            if len(page_data) < per_page:
                break

            page += 1

        return all_files

    def get_file_content(self, owner: str, repo: str, file_path: str, ref: str) -> str:
        """Fetches raw file content at a specific commit SHA or branch ref."""
        if not owner or not owner.strip():
            raise ValueError("Repository owner must be a non-empty string.")
        if not repo or not repo.strip():
            raise ValueError("Repository name must be a non-empty string.")
        if not file_path or not file_path.strip():
            raise ValueError("File path must be a non-empty string.")
        if not ref or not ref.strip():
            raise ValueError("Git ref/commit SHA must be a non-empty string.")

        owner_clean = owner.strip()
        repo_clean = repo.strip()
        path_clean = file_path.strip().lstrip("/")
        endpoint = f"repos/{owner_clean}/{repo_clean}/contents/{path_clean}"

        # Request with raw media type to get plain content directly
        headers = self._build_headers(accept="application/vnd.github.raw+json")
        response = self._request("GET", endpoint, headers=headers, params={"ref": ref.strip()})

        # Check if content returned is JSON object with base64 encoded content
        content_type = response.headers.get("content-type", "")
        if "application/json" in content_type:
            try:
                data = response.json()
                if (
                    isinstance(data, dict)
                    and data.get("encoding") == "base64"
                    and "content" in data
                ):
                    raw_bytes = base64.b64decode(data["content"])
                    return raw_bytes.decode("utf-8", errors="replace")
            except (ValueError, UnicodeDecodeError):
                pass

        return response.text

    def get_pull_request_diff(self, owner: str, repo: str, pull_number: int) -> str:
        """Fetches unified diff text for the entire pull request."""
        if not owner or not owner.strip():
            raise ValueError("Repository owner must be a non-empty string.")
        if not repo or not repo.strip():
            raise ValueError("Repository name must be a non-empty string.")
        if pull_number <= 0:
            raise ValueError("Pull request number must be a positive integer.")

        owner_clean = owner.strip()
        repo_clean = repo.strip()
        endpoint = f"repos/{owner_clean}/{repo_clean}/pulls/{pull_number}"

        headers = self._build_headers(accept="application/vnd.github.v3.diff")
        response = self._request("GET", endpoint, headers=headers)
        return response.text

    def get_repository_tree(
        self, owner: str, repo: str, tree_sha: str, recursive: bool = True
    ) -> tuple[list[dict[str, Any]], bool]:
        """Fetches Git tree for repository at an exact commit/tree SHA.

        Uses endpoint: GET /repos/{owner}/{repo}/git/trees/{tree_sha}?recursive=1
        Returns tuple of (tree entries list, truncated bool flag).
        """
        if not owner or not owner.strip():
            raise ValueError("Repository owner must be a non-empty string.")
        if not repo or not repo.strip():
            raise ValueError("Repository name must be a non-empty string.")
        if not tree_sha or not tree_sha.strip():
            raise ValueError("Git tree/commit SHA must be a non-empty string.")

        owner_clean = owner.strip()
        repo_clean = repo.strip()
        sha_clean = tree_sha.strip()
        endpoint = f"repos/{owner_clean}/{repo_clean}/git/trees/{sha_clean}"

        params = {"recursive": "1"} if recursive else None
        response = self._request("GET", endpoint, params=params)

        try:
            data = response.json()
        except ValueError as exc:
            clean_body = self._sanitize_message(response.text[:200])
            raise GitHubAPIError(
                f"Malformed JSON response from GitHub API tree endpoint: {clean_body}",
                status_code=response.status_code,
                response_body=self._sanitize_message(response.text),
            ) from exc

        if not isinstance(data, dict):
            got_type = type(data).__name__
            raise GitHubAPIError(
                f"Expected JSON object from Git Trees API {endpoint}, got {got_type}."
            )

        tree_entries = data.get("tree")
        if not isinstance(tree_entries, list):
            raise GitHubAPIError(
                f"Expected 'tree' field to be a list in Git Trees API response from {endpoint}."
            )

        is_truncated = bool(data.get("truncated", False))
        return tree_entries, is_truncated
