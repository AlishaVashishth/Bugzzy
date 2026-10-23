"""Data models representing repository files, symbols, and code evidence."""

from enum import StrEnum

from pydantic import BaseModel, Field


class SymbolType(StrEnum):
    """Categorization of code constructs."""

    FUNCTION = "function"
    METHOD = "method"
    CLASS = "class"
    MODULE = "module"
    IMPORT = "import"
    VARIABLE = "variable"
    TEST_CASE = "test_case"
    UNKNOWN = "unknown"


class EvidenceOrigin(StrEnum):
    """Explicit source tracking for retrieved code evidence."""

    DIRECT_DIFF = "direct_diff"
    ENCLOSING_SCOPE = "enclosing_scope"
    CALL_HIERARCHY = "call_hierarchy"
    DIRECT_DEPENDENCY = "direct_dependency"
    TEST_FILE = "test_file"
    REPOSITORY_REFERENCE = "repository_reference"
    SEMANTIC_RANKING = "semantic_ranking"


class SymbolReference(BaseModel):
    """Reference to an identified code construct."""

    name: str = Field(..., description="Name of the symbol")
    symbol_type: SymbolType = Field(..., description="Type of symbol")
    file_path: str = Field(..., description="Path where symbol is defined or used")
    start_line: int = Field(..., ge=1, description="1-indexed starting line")
    end_line: int = Field(..., ge=1, description="1-indexed ending line")
    parent_scope: str | None = Field(default=None, description="Enclosing class/module name")


class CodeSnippet(BaseModel):
    """Verifiable source code snippet retrieved from the repository."""

    file_path: str = Field(..., description="Relative repository path")
    start_line: int = Field(..., ge=1, description="1-indexed starting line number")
    end_line: int = Field(..., ge=1, description="1-indexed ending line number")
    content: str = Field(..., description="Exact code content retrieved")
    commit_sha: str = Field(..., description="Commit SHA from which this code was retrieved")
    origin: EvidenceOrigin = Field(..., description="Reason/origin why this code was retrieved")
    language: str = Field(default="python", description="Language of the snippet")
    symbols: list[SymbolReference] = Field(
        default_factory=list, description="Symbols defined or referenced within this snippet"
    )
    relevance_score: float | None = Field(
        default=None, ge=-1.0, le=1.0, description="Model-estimated relevance score if ranked"
    )
    is_model_estimate: bool = Field(
        default=False, description="Flag indicating if inclusion/ranking is model-estimated"
    )
