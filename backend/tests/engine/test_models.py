"""Unit tests for domain models, validation, and serialization."""

from pr_context_builder.models.context_package import (
    ContextPackage,
    InferredRelationship,
    LimitationType,
    RetrievalLimitation,
    SemanticRelevanceEstimate,
)
from pr_context_builder.models.pr_metadata import ChangedFile, PullRequest
from pr_context_builder.models.repository import CodeSnippet


def test_context_package_creation(
    sample_pull_request: PullRequest,
    sample_changed_file: ChangedFile,
    sample_code_snippet: CodeSnippet,
) -> None:
    """Ensure ContextPackage records evidence, commit SHAs, and limitations explicitly."""
    limitation = RetrievalLimitation(
        limitation_type=LimitationType.BINARY_FILE_SKIPPED,
        target_path="assets/logo.png",
        message="Binary file omitted from text context.",
        suggested_mitigation="Inspect raw blob if metadata is needed.",
    )

    relationship = InferredRelationship(
        source_symbol="src.calculator.calculate_total",
        target_symbol="tests.test_calculator.test_calculate_total",
        relationship_type="tested_by",
        confidence=0.95,
        heuristic_used="naming_convention_and_import",
    )

    semantic_match = SemanticRelevanceEstimate(
        file_path="src/calculator.py",
        start_line=10,
        end_line=18,
        similarity_score=0.9123,
        model_name="microsoft/codebert-base",
        is_model_estimate=True,
    )

    package = ContextPackage(
        pr=sample_pull_request,
        head_sha=sample_pull_request.head_sha,
        base_sha=sample_pull_request.base_sha,
        changed_files=[sample_changed_file],
        evidence_snippets=[sample_code_snippet],
        inferred_relationships=[relationship],
        semantic_matches=[semantic_match],
        test_snippets=[],
        limitations=[limitation],
    )

    assert package.head_sha == "7fd1a60b01f91b314f59955a4e4d4e80d8dee11d"
    assert len(package.evidence_snippets) == 1
    assert package.evidence_snippets[0].commit_sha == package.head_sha
    assert len(package.inferred_relationships) == 1
    assert len(package.semantic_matches) == 1
    assert package.semantic_matches[0].is_model_estimate is True
    assert package.semantic_matches[0].similarity_score == 0.9123
    assert len(package.limitations) == 1
    assert package.limitations[0].limitation_type == LimitationType.BINARY_FILE_SKIPPED

    # Ensure model serializes to clean JSON dictionary
    data = package.model_dump()
    assert data["pr"]["number"] == 42
    assert data["evidence_snippets"][0]["start_line"] == 10
    assert data["semantic_matches"][0]["model_name"] == "microsoft/codebert-base"
