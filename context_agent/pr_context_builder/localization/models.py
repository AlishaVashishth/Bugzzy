"""Domain data models for Bug Localization Agent findings and evidence grounding."""

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, Field

from pr_context_builder.models.repository import EvidenceOrigin


class BugSeverity(StrEnum):
    """Estimated severity of a suspected bug."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class ConfidenceLevel(StrEnum):
    """Qualitative assessment of confidence based on evidence strength.

    Strictly qualitative: NOT a calibrated mathematical probability.
    Must not be conflated with similarity or ranking scores.
    """

    SPECULATIVE = "speculative"
    MODERATE = "moderate"
    STRONG = "strong"


class EvidenceReference(BaseModel):
    """Direct reference to a verified evidence item in the ContextPackage."""

    file_path: str = Field(..., description="Target file path of evidence")
    start_line: int = Field(..., ge=1, description="1-indexed starting line")
    end_line: int = Field(..., ge=1, description="1-indexed ending line")
    commit_sha: str = Field(..., description="Commit SHA associated with evidence")
    origin: EvidenceOrigin = Field(..., description="Origin classification from ContextPackage")
    content_excerpt: str = Field(..., description="Short text excerpt illustrating the evidence")


class SuspectedBugLocation(BaseModel):
    """A localized code defect candidate strictly grounded in supplied evidence."""

    finding_id: str = Field(..., description="Unique identifier (e.g., 'BUG-001')")
    file_path: str = Field(..., description="File path containing suspected defect")
    start_line: int = Field(..., ge=1, description="1-indexed start line of suspected defect")
    end_line: int = Field(..., ge=1, description="1-indexed end line of suspected defect")
    commit_sha: str = Field(..., description="Commit SHA where defect is present")
    is_in_changed_code: bool = Field(
        ..., description="True if inside PR diff; False if in surrounding/calling code"
    )
    summary: str = Field(..., description="Concise description of the suspected defect")
    detailed_explanation: str = Field(
        ..., description="Detailed explanation linking evidence to the suspected defect"
    )
    potential_impact: str = Field(
        ...,
        description="Anticipated runtime consequence (e.g., TypeError, KeyError, unhandled None)",
    )
    severity: BugSeverity = Field(..., description="Severity classification")
    severity_justification: str = Field(..., description="Explicit rationale for severity level")
    confidence: ConfidenceLevel = Field(
        ..., description="Qualitative confidence rating (not a calibrated probability)"
    )
    confidence_explanation: str = Field(
        ..., description="Rationale for confidence level; explicit notation of missing proof"
    )
    supporting_evidence: list[EvidenceReference] = Field(
        default_factory=list,
        description="Ground-truth snippets or diff hunks supporting this finding",
    )
    related_test_references: list[str] = Field(
        default_factory=list, description="Paths/symbols in test_snippets relevant to this finding"
    )
    suggested_verification: list[str] = Field(
        default_factory=list,
        description="Suggested unit tests or dynamic assertions to confirm or refute the finding",
    )


class AISuggestedBugLocation(BaseModel):
    """An AI-suggested bug candidate, strictly separated from deterministic findings."""

    suggestion_id: str = Field(..., description="Unique suggestion identifier (e.g., 'AI-001')")
    file_path: str = Field(..., description="Target file path verified against supplied context")
    start_line: int = Field(..., ge=1, description="1-indexed starting line")
    end_line: int = Field(..., ge=1, description="1-indexed ending line")
    commit_sha: str = Field(..., description="Commit SHA verified against supplied context")
    is_in_changed_code: bool = Field(
        ..., description="True if inside PR diff; False if in calling/surrounding code"
    )
    summary: str = Field(..., description="Concise summary of the potential issue")
    detailed_explanation: str = Field(
        ..., description="AI explanation of why this code may be affected"
    )
    potential_impact: str = Field(..., description="Anticipated runtime consequence")
    confidence_explanation: str = Field(..., description="Qualitative rationale and uncertainties")
    supporting_evidence: list[EvidenceReference] = Field(
        default_factory=list, description="Verified snippets supporting this suggestion"
    )
    related_test_references: list[str] = Field(
        default_factory=list, description="Related test files/symbols"
    )
    suggested_verification: list[str] = Field(
        default_factory=list, description="Suggested assertions or tests to verify"
    )
    model_name: str = Field(..., description="Name of the model that generated this suggestion")


class BugLocalizationResult(BaseModel):
    """Complete, structured localization outcome returned to downstream consumers."""

    pr_number: int = Field(..., ge=1, description="Pull Request number analyzed")
    head_sha: str = Field(..., description="Head commit SHA evaluated")
    base_sha: str = Field(..., description="Base commit SHA evaluated")
    findings: list[SuspectedBugLocation] = Field(
        default_factory=list,
        description=(
            "List of suspected bug locations. An empty list denotes no actionable defects found."
        ),
    )
    ai_suggestions: list[AISuggestedBugLocation] = Field(
        default_factory=list,
        description="AI-suggested defect candidates clearly separated from deterministic findings",
    )
    analysis_limitations: list[str] = Field(
        default_factory=list,
        description=(
            "Explicit caveats regarding truncated context, missing dependencies, or unparsed files"
        ),
    )
    analyzed_snippets_count: int = Field(
        default=0, ge=0, description="Count of evidence and test snippets analyzed"
    )
    dl_model_applied: str | None = Field(
        default=None, description="Pretrained DL model used during analysis, if any"
    )
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        description="Timestamp when localization analysis was completed",
    )

    @property
    def has_findings(self) -> bool:
        """Convenience property indicating whether actionable findings exist."""
        return len(self.findings) > 0

    @property
    def has_suggestions(self) -> bool:
        """Convenience property indicating whether AI suggestions exist."""
        return len(self.ai_suggestions) > 0
