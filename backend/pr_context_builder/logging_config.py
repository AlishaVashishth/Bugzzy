"""Structured logging configuration with automatic token redacting."""

import logging
import re
import sys

# Pattern to capture GitHub tokens (ghp_, github_pat_, etc.)
TOKEN_PATTERNS = [
    re.compile(r"(ghp_[A-Za-z0-9_]{36,255})"),
    re.compile(r"(github_pat_[A-Za-z0-9_]{82})"),
    re.compile(r"(Bearer\s+)[A-Za-z0-9_\-\.]{20,}", re.IGNORECASE),
]


class RedactingFormatter(logging.Formatter):
    """Logging formatter that redacts GitHub tokens and secrets from log records."""

    def format(self, record: logging.LogRecord) -> str:
        original = super().format(record)
        sanitized = original
        for pattern in TOKEN_PATTERNS:
            sanitized = pattern.sub("[REDACTED_SECRET]", sanitized)
        return sanitized


def configure_logger(name: str = "pr_context_builder", level: str = "INFO") -> logging.Logger:
    """Configures and returns a thread-safe logger with secret redaction.

    Args:
        name: Logger name hierarchy.
        level: Log level string (DEBUG, INFO, WARNING, ERROR).

    Returns:
        Configured logging.Logger instance.
    """
    logger = logging.getLogger(name)
    numeric_level = getattr(logging, level.upper(), logging.INFO)
    logger.setLevel(numeric_level)

    # Avoid adding duplicate handlers if already configured
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        formatter = RedactingFormatter(
            fmt="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        handler.setFormatter(formatter)
        logger.addHandler(handler)

    logger.propagate = False
    return logger
