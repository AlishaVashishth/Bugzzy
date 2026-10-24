"""Context Builder Pipeline Orchestration.

Orchestrates GitHub API interactions, repository code location, deep learning
semantic ranking, and token budgeting to produce a complete ContextPackage for
downstream debugging agents.
"""

from __future__ import annotations

import logging
from typing import Any

from pr_context_builder.config import Settings, load_settings
from pr_context_builder.exceptions import GitHubAPIError
from pr_context_builder.github.client import GitHubClient
from pr_context_builder.index import CallerIndex
from pr_context_builder.models.context_package import (
    ContextPackage,
    LimitationType,
    RetrievalLimitation,
    SemanticRelevanceEstimate,
)
from pr_context_builder.models.pr_metadata import ChangedFile, PullRequest
from pr_context_builder.models.repository import CodeSnippet, EvidenceOrigin
from pr_context_builder.retrieval.locator import LocatedCodeResult, RepositoryCodeLocator
from pr_context_builder.semantic.encoder import (
    ModelDependencyMissingError,
    TransformerCodeEncoder,
)
from pr_context_builder.semantic.ranker import SemanticRanker

logger = logging.getLogger(__name__)

# Excluded directory prefixes and substrings for repository candidate discovery
EXCLUDED_PATH_PARTS = (
    "/.git/",
    "/.github/",
    "/.venv/",
    "/venv/",
    "/.tox/",
    "/node_modules/",
    "/site-packages/",
    "/__pycache__/",
    "/dist/",
    "/build/",
    "/.pytest_cache/",
    "/.ruff_cache/",
    "/.mypy_cache/",
)


def _estimate_tokens(text: str) -> int:
    """Estimates LLM subword token count using a character/word heuristic approximation.

    Downstream language models typically encode code with BPE/Byte-level tokenizers
    at approximately 3 to 4 characters per token. Whitespace-splitting alone
    substantially underestimates code token counts. This heuristic provides a
    conservative approximation without adding external tokenizer dependencies.
    """
    if not text:
        return 0
    # Heuristic: max of whitespace word count and char length divided by 4
    return max(len(text.split()), len(text) // 4)


def _is_suitable_candidate_path(path: str, changed_paths: set[str]) -> bool:
    """Determines whether a repository file path is an eligible candidate for discovery."""
    if not path or not path.endswith(".py"):
        return False
    if path in changed_paths:
        return False

    normalized = f"/{path.replace('\\', '/').lstrip('/')}"
    return all(excluded not in normalized for excluded in EXCLUDED_PATH_PARTS)


def _discover_candidate_files(
    client: GitHubClient,
    owner: str,
    repo: str,
    head_sha: str,
    changed_files: list[ChangedFile],
    max_candidate_files: int = 20,
    max_file_size_bytes: int = 100_000,
) -> tuple[dict[str, str] | None, list[RetrievalLimitation]]:
    """Discovers candidate repository files via the Git Trees API.

    Applies pre-download filtering (path suitability, exclusion lists, file size,
    and count limits) before fetching file contents.

    Returns:
        Tuple of (mapping of {file_path: content} or None, list of limitations encountered).
    """
    limitations: list[RetrievalLimitation] = []
    changed_paths = {f.filename for f in changed_files}

    try:
        tree_entries, is_truncated = client.get_repository_tree(
            owner, repo, head_sha, recursive=True
        )
    except GitHubAPIError as exc:
        limitations.append(
            RetrievalLimitation(
                limitation_type=LimitationType.API_ERROR,
                target_path=None,
                message=f"Failed to retrieve repository tree at commit {head_sha}: {exc}",
                suggested_mitigation="Verify repository read permissions and tree SHA.",
            )
        )
        return None, limitations

    if is_truncated:
        limitations.append(
            RetrievalLimitation(
                limitation_type=LimitationType.RATE_LIMIT_TRUNCATION,
                target_path=None,
                message=(
                    "GitHub repository tree response was truncated by the API; "
                    "candidate repository scan may be incomplete."
                ),
                suggested_mitigation=(
                    "Review critical dependencies manually if relevant files were omitted."
                ),
            )
        )

    # Filter and rank candidate paths
    suitable_entries: list[dict[str, Any]] = []
    for entry in tree_entries:
        if entry.get("type") != "blob":
            continue
        path = entry.get("path")
        if not path or not _is_suitable_candidate_path(path, changed_paths):
            continue

        # Check pre-download file size if reported by GitHub API
        size = entry.get("size")
        if size is not None and size > max_file_size_bytes:
            limitations.append(
                RetrievalLimitation(
                    limitation_type=LimitationType.BUDGET_EXCEEDED,
                    target_path=path,
                    message=(
                        f"File {path} ({size} bytes) exceeds configured size limit "
                        f"({max_file_size_bytes} bytes); skipped before download."
                    ),
                )
            )
            continue

        suitable_entries.append(entry)

    # Prioritize test files and files in similar directories
    changed_dirs = {f.filename.rsplit("/", 1)[0] for f in changed_files if "/" in f.filename}

    def _priority_score(entry: dict[str, Any]) -> int:
        p = entry.get("path", "")
        # Highest priority: test files
        if "test" in p.lower():
            return 0
        # Medium priority: files in matching directory
        if any(p.startswith(d + "/") for d in changed_dirs):
            return 1
        # Lower priority: general source files
        return 2

    suitable_entries.sort(key=_priority_score)

    # Apply candidate limit before fetching file content
    if len(suitable_entries) > max_candidate_files:
        limitations.append(
            RetrievalLimitation(
                limitation_type=LimitationType.BUDGET_EXCEEDED,
                target_path=None,
                message=(
                    f"Candidate repository files limited to {max_candidate_files} files "
                    f"(found {len(suitable_entries)} eligible files)."
                ),
                suggested_mitigation="Increase candidate limit if broader search is needed.",
            )
        )
        suitable_entries = suitable_entries[:max_candidate_files]

    candidate_contents: dict[str, str] = {}
    for entry in suitable_entries:
        path = entry["path"]
        try:
            content = client.get_file_content(owner, repo, path, head_sha)
            candidate_contents[path] = content
        except GitHubAPIError as exc:
            limitations.append(
                RetrievalLimitation(
                    limitation_type=LimitationType.FILE_NOT_FOUND,
                    target_path=path,
                    message=f"Failed to retrieve candidate file {path} at {head_sha}: {exc}",
                )
            )

    return candidate_contents, limitations


def build_context(
    owner: str,
    repo: str,
    pull_number: int,
    github_token: str | None = None,
    caller_index: CallerIndex | None = None,
) -> ContextPackage:
    """Orchestrates retrieval and packaging of PR context for downstream AI agents.

    Parameters:
        owner: GitHub repository owner or organization name.
        repo: GitHub repository name.
        pull_number: 1-indexed Pull Request number.
        github_token: Optional personal access token or installation access token.
            Defaults to the token configured in the environment if omitted.

    Returns:
        Fully populated ContextPackage containing verified snippets, relationships,
        semantic relevance rankings, and explicit retrieval limitations.

    Raises:
        ValueError: If input arguments are missing or invalid.
        GitHubAPIError: If authoritative PR metadata or file listing fails.
    """
    if not owner or not isinstance(owner, str) or not owner.strip():
        raise ValueError("Repository owner must be a non-empty string.")
    if not repo or not isinstance(repo, str) or not repo.strip():
        raise ValueError("Repository name must be a non-empty string.")
    if not isinstance(pull_number, int) or pull_number <= 0:
        raise ValueError("Pull request number must be a positive integer.")

    settings: Settings = load_settings()
    active_token = (
        github_token.strip() if github_token and github_token.strip() else None
    ) or settings.github_token

    client = GitHubClient(
        token=active_token,
        base_url=settings.github_api_base_url,
        timeout_seconds=settings.http_timeout_seconds,
        max_changed_files_limit=settings.max_changed_files_limit,
    )

    limitations: list[RetrievalLimitation] = []

    # 1. Fetch PR metadata
    pr_meta: PullRequest = client.get_pull_request(owner, repo, pull_number)

    # 2. Fetch changed files with diff patches
    changed_files: list[ChangedFile] = client.get_pull_request_files(owner, repo, pull_number)
    pr: PullRequest = pr_meta.model_copy(update={"changed_files": changed_files})

    # 3. Fetch unified diff (used as focal query for semantic ranking)
    try:
        diff_text: str = client.get_pull_request_diff(owner, repo, pull_number)
    except GitHubAPIError as exc:
        diff_text = ""
        limitations.append(
            RetrievalLimitation(
                limitation_type=LimitationType.API_ERROR,
                target_path=None,
                message=f"Failed to retrieve unified diff for PR #{pull_number}: {exc}",
            )
        )

    # 4. Discover candidate repository files
    candidate_contents: dict[str, str] | None = None

    if caller_index is None:
        candidate_provider = None
        if settings.dl_ranking_enabled:
            candidate_contents, tree_limits = _discover_candidate_files(
                client=client,
                owner=owner,
                repo=repo,
                head_sha=pr.head_sha,
                changed_files=changed_files,
                max_candidate_files=settings.max_candidate_files,
                max_file_size_bytes=100_000,
            )
            limitations.extend(tree_limits)
    else:
        def candidate_provider(names: list[str]) -> dict[str, str]:
            def _handle_empty(warn_msg: str) -> dict[str, str]:
                logger.warning(warn_msg)
                limitations.append(
                    RetrievalLimitation(
                        limitation_type=LimitationType.CALLER_INDEX_EMPTY,
                        message=f"Caller index returned no usable files for repository {owner}/{repo}.",
                    )
                )
                return {}

            try:
                paths = caller_index.find_caller_files(
                    owner, repo, names, limit=settings.max_candidate_files
                )
            except Exception as exc:
                return _handle_empty(
                    f"Caller index lookup failed for repository {owner}/{repo}: {type(exc).__name__}"
                )

            if not paths:
                return _handle_empty(
                    f"Caller index returned no candidate paths for repository {owner}/{repo}."
                )

            changed_paths = {f.filename for f in changed_files}
            files: dict[str, str] = {}
            for path in paths:
                if not _is_suitable_candidate_path(path, changed_paths):
                    continue
                try:
                    content = client.get_file_content(owner, repo, path, pr.head_sha)
                    files[path] = content
                except Exception:
                    continue

            if not files:
                return _handle_empty(
                    f"Caller index returned paths for repository {owner}/{repo} but none were usable."
                )

            return files

    # 5. Locate enclosing symbols and related code
    locator = RepositoryCodeLocator(
        github_client=client,
        max_candidate_files=settings.max_candidate_files,
    )
    locate_result: LocatedCodeResult = locator.locate_pr_code(
        pr,
        candidate_file_contents=candidate_contents,
        candidate_provider=candidate_provider,
    )
    limitations.extend(locate_result.limitations)

    # 6. Deep Learning semantic ranking
    semantic_matches: list[SemanticRelevanceEstimate] = []
    if settings.dl_ranking_enabled:
        if not diff_text.strip():
            limitations.append(
                RetrievalLimitation(
                    limitation_type=LimitationType.INFERENCE_ERROR,
                    target_path=None,
                    message=(
                        "Semantic ranking skipped because PR diff text is empty or unavailable."
                    ),
                )
            )
        else:
            # Select eligible candidate snippets (repository references and direct dependencies)
            candidate_snippets = [
                snip
                for snip in locate_result.snippets
                if snip.origin
                in (
                    EvidenceOrigin.REPOSITORY_REFERENCE,
                    EvidenceOrigin.DIRECT_DEPENDENCY,
                )
            ]
            if candidate_snippets:
                try:
                    encoder = TransformerCodeEncoder(
                        model_name=settings.code_encoder_model,
                        device=settings.dl_device,
                        batch_size=settings.dl_batch_size,
                    )
                    ranker = SemanticRanker(encoder)
                    semantic_matches = ranker.rank_snippets(
                        query=diff_text,
                        candidates=candidate_snippets,
                        top_k=None,
                        min_threshold=-1.0,
                    )
                except ModelDependencyMissingError as exc:
                    limitations.append(
                        RetrievalLimitation(
                            limitation_type=LimitationType.MODEL_UNAVAILABLE,
                            target_path=None,
                            message=f"Deep learning dependencies unavailable: {exc}",
                            suggested_mitigation=(
                                "Install requirements-dl.txt to enable semantic code ranking."
                            ),
                        )
                    )
                except Exception as exc:
                    limitations.append(
                        RetrievalLimitation(
                            limitation_type=LimitationType.INFERENCE_ERROR,
                            target_path=None,
                            message=f"Semantic ranking inference failed: {exc}",
                        )
                    )

    # 7. Context token budgeting and truncation
    # Prioritize direct diff and enclosing scope snippets, followed by direct call hierarchy,
    # over broad repository references.
    direct_snippets: list[CodeSnippet] = []
    call_hierarchy_snippets: list[CodeSnippet] = []
    other_snippets: list[CodeSnippet] = []
    for snip in locate_result.snippets:
        if snip.origin in (EvidenceOrigin.DIRECT_DIFF, EvidenceOrigin.ENCLOSING_SCOPE):
            direct_snippets.append(snip)
        elif snip.origin == EvidenceOrigin.CALL_HIERARCHY:
            call_hierarchy_snippets.append(snip)
        else:
            other_snippets.append(snip)

    # Sort candidate snippets by descending relevance score before budget truncation.
    # Snippets without a score are assigned -inf so they do not outrank scored snippets.
    # Python's stable sort preserves original discovery order for ties and unscored items.
    other_snippets.sort(
        key=lambda s: s.relevance_score if s.relevance_score is not None else -float("inf"),
        reverse=True,
    )

    # Combine in priority order: direct diff first, then direct call hierarchy,
    # then relevance-ranked candidate code
    ordered_snippets = direct_snippets + call_hierarchy_snippets + other_snippets

    # Account for metadata tokens in the budget
    meta_text = f"{pr.title}\n{pr.body or ''}\n" + "\n".join(
        f"{f.filename} {f.status}" for f in pr.changed_files
    )
    meta_tokens = _estimate_tokens(meta_text)
    effective_budget = max(0, settings.max_context_tokens_budget - meta_tokens)

    allowed_snippets: list[CodeSnippet] = []
    tokens_so_far = 0

    for snip in ordered_snippets:
        snip_tokens = _estimate_tokens(snip.content)
        if tokens_so_far + snip_tokens <= effective_budget:
            allowed_snippets.append(snip)
            tokens_so_far += snip_tokens
        else:
            # Safe fallback: if budget is exhausted on the very first snippet, preserve at
            # least one essential direct-diff snippet so the package is never stripped of diffs.
            if not allowed_snippets and snip.origin in (
                EvidenceOrigin.DIRECT_DIFF,
                EvidenceOrigin.ENCLOSING_SCOPE,
            ):
                allowed_snippets.append(snip)
                tokens_so_far += snip_tokens
            break

    # Recalculate final estimated token count (retained snippets + base metadata)
    recalculated_tokens = meta_tokens + sum(_estimate_tokens(s.content) for s in allowed_snippets)
    omitted_count = len(ordered_snippets) - len(allowed_snippets)

    if recalculated_tokens > settings.max_context_tokens_budget:
        if omitted_count > 0:
            msg = (
                f"Context token budget ({settings.max_context_tokens_budget}) exceeded "
                f"(approx. {recalculated_tokens} tokens estimated). Primary direct diff "
                f"was preserved as essential evidence despite exceeding the budget, and "
                f"{omitted_count} lower-priority snippet(s) were omitted."
            )
        else:
            msg = (
                f"Context token budget ({settings.max_context_tokens_budget}) exceeded "
                f"(approx. {recalculated_tokens} tokens estimated). Primary direct diff "
                f"was preserved as essential evidence despite exceeding the configured budget."
            )
        limitations.append(
            RetrievalLimitation(
                limitation_type=LimitationType.BUDGET_EXCEEDED,
                target_path=None,
                message=msg,
                suggested_mitigation=(
                    "Increase max_context_tokens_budget if more surrounding context is needed."
                ),
            )
        )
    elif omitted_count > 0:
        limitations.append(
            RetrievalLimitation(
                limitation_type=LimitationType.BUDGET_EXCEEDED,
                target_path=None,
                message=(
                    f"Context token budget ({settings.max_context_tokens_budget}) reached "
                    f"(approx. {recalculated_tokens} tokens estimated); "
                    f"{omitted_count} lower-priority snippet(s) omitted to fit budget."
                ),
                suggested_mitigation=(
                    "Increase max_context_tokens_budget if more surrounding context is needed."
                ),
            )
        )

    # Synchronize semantic matches with retained snippets to prevent stale references
    retained_keys = {(s.file_path, s.start_line, s.end_line) for s in allowed_snippets}
    synchronized_semantic_matches = [
        sm for sm in semantic_matches if (sm.file_path, sm.start_line, sm.end_line) in retained_keys
    ]

    # Segregate test snippets from general evidence snippets
    test_snippets = [s for s in allowed_snippets if s.origin == EvidenceOrigin.TEST_FILE]
    evidence_snippets = [s for s in allowed_snippets if s.origin != EvidenceOrigin.TEST_FILE]

    return ContextPackage(
        pr=pr,
        head_sha=pr.head_sha,
        base_sha=pr.base_sha,
        changed_files=pr.changed_files,
        evidence_snippets=evidence_snippets,
        inferred_relationships=locate_result.relationships,
        semantic_matches=synchronized_semantic_matches,
        test_snippets=test_snippets,
        limitations=limitations,
        total_tokens_estimated=recalculated_tokens,
    )


__all__ = ["build_context"]
