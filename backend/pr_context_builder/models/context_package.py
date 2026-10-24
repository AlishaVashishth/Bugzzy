"""Data models for structured context packages returned to downstream agents."""

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, Field

from pr_context_builder.models.pr_metadata import ChangedFile, PullRequest
from pr_context_builder.models.repository import CodeSnippet


class LimitationType(StrEnum):
    """Specific categories of retrieval limitations or degradation."""

    BINARY_FILE_SKIPPED = "binary_file_skipped"
    RATE_LIMIT_TRUNCATION = "rate_limit_truncation"
    BUDGET_EXCEEDED = "budget_exceeded"
    FILE_NOT_FOUND = "file_not_found"
    UNRESOLVED_DEPENDENCY = "unresolved_dependency"
    UNSUPPORTED_LANGUAGE = "unsupported_language"
    SYNTAX_PARSE_ERROR = "syntax_parse_error"
    API_ERROR = "api_error"
    MODEL_UNAVAILABLE = "model_unavailable"
    INFERENCE_ERROR = "inference_error"
    CALLER_INDEX_EMPTY = "caller_index_empty"


class RetrievalLimitation(BaseModel):
    """Explicit declaration of incomplete or unavailable retrieval context."""

    limitation_type: LimitationType = Field(..., description="Category of the retrieval constraint")
    target_path: str | None = Field(default=None, description="Affected file path or resource")
    message: str = Field(..., description="Detailed explanation of what could not be retrieved")
    suggested_mitigation: str | None = Field(
        default=None, description="Actionable suggestion for downstream agent"
    )


class InferredRelationship(BaseModel):
    """Represents an inferred code relationship clearly separated from raw evidence."""

    source_symbol: str = Field(..., description="Source identifier/function/file")
    target_symbol: str = Field(..., description="Target identifier/function/file")
    relationship_type: str = Field(..., description="e.g. calls, imports, tests, subclasses")
    confidence: float = Field(
        default=1.0, ge=0.0, le=1.0, description="Confidence score of heuristic inference"
    )
    heuristic_used: str = Field(..., description="Name of the deterministic rule or heuristic")


class SemanticRelevanceEstimate(BaseModel):
    """Model-generated relevance estimate between PR diff context and repository snippet.

    Explicitly labeled as a learned estimate to preserve distinction from ground-truth evidence.
    """

    file_path: str = Field(..., description="Target file path of ranked snippet")
    start_line: int = Field(..., ge=1, description="1-indexed starting line")
    end_line: int = Field(..., ge=1, description="1-indexed ending line")
    similarity_score: float = Field(
        ..., ge=-1.0, le=1.0, description="Cosine similarity score produced by code encoder"
    )
    model_name: str = Field(..., description="Name of pretrained model used for encoding")
    is_model_estimate: bool = Field(
        default=True, description="Strictly true: learned heuristic, not deterministic proof"
    )


class ContextPackage(BaseModel):
    """Complete, self-contained structured context package for downstream AI agents."""

    pr: PullRequest = Field(..., description="Pull request metadata")
    head_sha: str = Field(..., description="Head commit SHA evaluated")
    base_sha: str = Field(..., description="Base commit SHA evaluated")
    changed_files: list[ChangedFile] = Field(
        default_factory=list, description="Verified list of files changed in PR"
    )
    evidence_snippets: list[CodeSnippet] = Field(
        default_factory=list,
        description="Direct verified source code snippets with commit SHA and line numbers",
    )
    inferred_relationships: list[InferredRelationship] = Field(
        default_factory=list,
        description="Inferred dependencies separated from direct evidence",
    )
    semantic_matches: list[SemanticRelevanceEstimate] = Field(
        default_factory=list,
        description="Learned semantic relevance rankings (model-generated estimates)",
    )
    test_snippets: list[CodeSnippet] = Field(
        default_factory=list, description="Direct code snippets of tests covering changed code"
    )
    limitations: list[RetrievalLimitation] = Field(
        default_factory=list,
        description="Explicit limitations, skipped items, or API limits encountered",
    )
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        description="Timestamp when context package was built",
    )
    total_tokens_estimated: int = Field(
        default=0, ge=0, description="Approximate token count of context payload"
    )
