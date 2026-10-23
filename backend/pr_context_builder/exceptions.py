"""Custom exceptions hierarchy for the Context Builder Agent.

Ensures predictable error handling, explicit failure modes, and clear distinction
between API failures, retrieval issues, and configuration defects.
"""


class ContextBuilderError(Exception):
    """Base exception for all Context Builder Agent errors."""


class ConfigurationError(ContextBuilderError):
    """Raised when environment variables or project settings are invalid."""


class GitHubAPIError(ContextBuilderError):
    """Base exception for GitHub REST API communication failures."""

    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        response_body: str | None = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body


class AuthenticationError(GitHubAPIError):
    """Raised when GitHub authentication credentials are invalid or missing."""


class RateLimitExceededError(GitHubAPIError):
    """Raised when GitHub API rate limits are encountered."""

    def __init__(
        self,
        message: str = "GitHub API rate limit exceeded.",
        reset_timestamp: int | None = None,
        status_code: int = 403,
    ):
        super().__init__(message, status_code=status_code)
        self.reset_timestamp = reset_timestamp


class ResourceNotFoundError(GitHubAPIError):
    """Raised when a pull request, repository, or commit SHA is not found (404)."""


class RetrievalError(ContextBuilderError):
    """Raised when an error occurs during repository or file retrieval."""


class DiffParseError(RetrievalError):
    """Raised when git patch or diff cannot be reliably parsed."""


class BinaryFileError(RetrievalError):
    """Raised when an operation encounters an unhandled binary file."""


class UnsupportedLanguageError(RetrievalError):
    """Raised when deterministic AST analysis is requested on an unsupported language."""


class SyntaxParseError(RetrievalError):
    """Raised when deterministic parsing of source code encounters a syntax error."""


class BudgetExceededError(ContextBuilderError):
    """Raised when the context budget limit prevents complete evidence inclusion."""


class DeepLearningModelError(ContextBuilderError):
    """Base exception for deep learning encoder and ranking failures."""


class ModelDependencyMissingError(DeepLearningModelError):
    """Raised when PyTorch or Transformers is requested but not installed."""


class ModelNotFoundError(DeepLearningModelError):
    """Raised when the specified pretrained model weights or tokenizer cannot be loaded."""


class ModelInferenceError(DeepLearningModelError):
    """Raised when an error occurs during tensor forward pass or embedding computation."""
