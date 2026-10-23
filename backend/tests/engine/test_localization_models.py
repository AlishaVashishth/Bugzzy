"""Unit tests for Bug Localization Agent domain models and public interface contract.

Verifies model schema constraints, qualitative confidence semantics, Pydantic
serialization, and accurate propagation from ContextPackage without implementing
detection algorithms.
"""

import pytest

from pr_context_builder.localization import (
    BugLocalizationResult,
    BugSeverity,
    ConfidenceLevel,
    EvidenceReference,
    SuspectedBugLocation,
    localize_bugs,
)
from pr_context_builder.models.context_package import (
    ContextPackage,
    LimitationType,
    RetrievalLimitation,
)
from pr_context_builder.models.pr_metadata import ChangedFile, PullRequest
from pr_context_builder.models.repository import CodeSnippet, EvidenceOrigin


@pytest.fixture
def sample_context_package(
    sample_pull_request: PullRequest,
    sample_changed_file: ChangedFile,
    sample_code_snippet: CodeSnippet,
) -> ContextPackage:
    """Fixture providing a valid ContextPackage populated with realistic test data."""
    limitation = RetrievalLimitation(
        limitation_type=LimitationType.RATE_LIMIT_TRUNCATION,
        target_path=None,
        message="Candidate scan was truncated by GitHub API rate limits.",
    )
    return ContextPackage(
        pr=sample_pull_request,
        head_sha=sample_pull_request.head_sha,
        base_sha=sample_pull_request.base_sha,
        changed_files=[sample_changed_file],
        evidence_snippets=[sample_code_snippet],
        inferred_relationships=[],
        semantic_matches=[],
        test_snippets=[],
        limitations=[limitation],
        total_tokens_estimated=120,
    )


def test_model_instantiation_and_serialization() -> None:
    """Ensure models validate required fields and support clean JSON roundtrips."""
    evidence = EvidenceReference(
        file_path="src/calculator.py",
        start_line=10,
        end_line=15,
        commit_sha="head123456",
        origin=EvidenceOrigin.ENCLOSING_SCOPE,
        content_excerpt="def calculate_total(items): ...",
    )

    finding = SuspectedBugLocation(
        finding_id="BUG-001",
        file_path="src/calculator.py",
        start_line=12,
        end_line=12,
        commit_sha="head123456",
        is_in_changed_code=True,
        summary="Potential TypeError when iterating non-iterable items",
        detailed_explanation="calculate_total does not verify that items is an iterable.",
        potential_impact="Raises TypeError at runtime if passed None.",
        severity=BugSeverity.HIGH,
        severity_justification="Breaks core calculation workflow.",
        confidence=ConfidenceLevel.MODERATE,
        confidence_explanation="Inferred from absence of input validation in diff.",
        supporting_evidence=[evidence],
        related_test_references=["tests/test_calculator.py"],
        suggested_verification=["Add test passing items=None."],
    )

    result = BugLocalizationResult(
        pr_number=42,
        head_sha="head123456",
        base_sha="baseabcdef",
        findings=[finding],
        analysis_limitations=["Context truncated."],
        analyzed_snippets_count=1,
    )

    assert result.has_findings is True
    assert len(result.findings) == 1
    assert result.findings[0].severity == BugSeverity.HIGH
    assert result.findings[0].confidence == ConfidenceLevel.MODERATE
    assert result.findings[0].supporting_evidence[0].commit_sha == "head123456"

    # Test Pydantic JSON serialization & deserialization
    json_data = result.model_dump_json()
    reloaded = BugLocalizationResult.model_validate_json(json_data)
    assert reloaded.pr_number == 42
    assert reloaded.findings[0].finding_id == "BUG-001"
    assert reloaded.findings[0].severity == BugSeverity.HIGH


def test_empty_findings_is_valid() -> None:
    """Ensure returning zero findings is valid and explicitly supported."""
    result = BugLocalizationResult(
        pr_number=101,
        head_sha="head123",
        base_sha="base123",
        findings=[],
        analysis_limitations=[],
        analyzed_snippets_count=3,
    )
    assert result.has_findings is False
    assert result.findings == []


def test_localize_bugs_propagates_pr_metadata_and_shas(
    sample_context_package: ContextPackage,
) -> None:
    """Ensure localize_bugs accepts ContextPackage directly and propagates SHAs and metadata."""
    result = localize_bugs(sample_context_package)

    assert isinstance(result, BugLocalizationResult)
    assert result.pr_number == sample_context_package.pr.number
    assert result.head_sha == sample_context_package.head_sha
    assert result.base_sha == sample_context_package.base_sha
    assert result.analyzed_snippets_count == 1
    assert len(result.analysis_limitations) == 1
    assert result.analysis_limitations[0] == (
        "[rate_limit_truncation] Candidate scan was truncated by GitHub API rate limits."
    )
    assert result.has_findings is False


def test_localize_bugs_preserves_retrieval_limitation_details(
    sample_pull_request: PullRequest,
    sample_changed_file: ChangedFile,
    sample_code_snippet: CodeSnippet,
) -> None:
    """Ensure limitation_type, target_path, and original message are preserved in order."""
    lim1 = RetrievalLimitation(
        limitation_type=LimitationType.BUDGET_EXCEEDED,
        target_path="src/payment.py",
        message="Context budget was exceeded.",
    )
    lim2 = RetrievalLimitation(
        limitation_type=LimitationType.RATE_LIMIT_TRUNCATION,
        target_path=None,
        message="GitHub rate limit prevented complete retrieval.",
    )
    lim3 = RetrievalLimitation(
        limitation_type=LimitationType.BINARY_FILE_SKIPPED,
        target_path="assets/logo.png",
        message="Binary file omitted from text context.",
    )

    pkg = ContextPackage(
        pr=sample_pull_request,
        head_sha=sample_pull_request.head_sha,
        base_sha=sample_pull_request.base_sha,
        changed_files=[sample_changed_file],
        evidence_snippets=[sample_code_snippet],
        limitations=[lim1, lim2, lim3],
    )

    result = localize_bugs(pkg)

    assert len(result.analysis_limitations) == 3

    # 1. Limitation type is preserved (enum value)
    assert "[budget_exceeded]" in result.analysis_limitations[0]
    assert "[rate_limit_truncation]" in result.analysis_limitations[1]
    assert "[binary_file_skipped]" in result.analysis_limitations[2]

    # 2. Non-empty target path is preserved
    assert "(src/payment.py)" in result.analysis_limitations[0]
    assert "(assets/logo.png)" in result.analysis_limitations[2]

    # 3. Missing target path does not produce misleading empty path or parentheses
    assert "()" not in result.analysis_limitations[1]
    assert "None" not in result.analysis_limitations[1]
    assert result.analysis_limitations[1] == (
        "[rate_limit_truncation] GitHub rate limit prevented complete retrieval."
    )

    # 4. Original message is preserved
    assert "Context budget was exceeded." in result.analysis_limitations[0]
    assert "GitHub rate limit prevented complete retrieval." in result.analysis_limitations[1]
    assert "Binary file omitted from text context." in result.analysis_limitations[2]

    # 5. Multiple limitations retain their original order
    assert result.analysis_limitations[0] == (
        "[budget_exceeded] (src/payment.py) Context budget was exceeded."
    )
    assert result.analysis_limitations[1] == (
        "[rate_limit_truncation] GitHub rate limit prevented complete retrieval."
    )
    assert result.analysis_limitations[2] == (
        "[binary_file_skipped] (assets/logo.png) Binary file omitted from text context."
    )


def test_localize_bugs_invalid_input() -> None:
    """Ensure invalid inputs raise ValueError immediately."""
    with pytest.raises(ValueError, match="valid ContextPackage"):
        localize_bugs(None)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="valid ContextPackage"):
        localize_bugs({"not": "a context package"})  # type: ignore[arg-type]


def test_confidence_level_qualitative_semantics() -> None:
    """Ensure ConfidenceLevel represents qualitative categories, not numerical probabilities."""
    assert ConfidenceLevel.SPECULATIVE.value == "speculative"
    assert ConfidenceLevel.MODERATE.value == "moderate"
    assert ConfidenceLevel.STRONG.value == "strong"

    # Verify string comparisons work cleanly
    assert ConfidenceLevel.STRONG == "strong"
    assert ConfidenceLevel.MODERATE == "moderate"


def test_evidence_reference_provenance() -> None:
    """Ensure evidence references support both head_sha (active) and base_sha (deleted)."""
    # Active code evidence on head SHA
    active_ref = EvidenceReference(
        file_path="src/service.py",
        start_line=10,
        end_line=20,
        commit_sha="head_sha_val",
        origin=EvidenceOrigin.ENCLOSING_SCOPE,
        content_excerpt="def new_func(): pass",
    )
    assert active_ref.commit_sha == "head_sha_val"

    # Deleted code evidence on base SHA
    deleted_ref = EvidenceReference(
        file_path="src/deprecated.py",
        start_line=1,
        end_line=15,
        commit_sha="base_sha_val",
        origin=EvidenceOrigin.DIRECT_DIFF,
        content_excerpt="-def old_func(): pass",
    )
    assert deleted_ref.commit_sha == "base_sha_val"
