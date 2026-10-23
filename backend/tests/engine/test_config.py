"""Unit tests for configuration management and token protection."""

import io
import logging

import pytest

from pr_context_builder.config import Settings, load_settings
from pr_context_builder.exceptions import ConfigurationError
from pr_context_builder.logging_config import RedactingFormatter, configure_logger


def test_default_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure defaults are populated when environment variables are unset."""
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_API_BASE_URL", raising=False)
    monkeypatch.delenv("MAX_CONTEXT_TOKENS_BUDGET", raising=False)
    monkeypatch.delenv("MAX_CHANGED_FILES_LIMIT", raising=False)
    monkeypatch.delenv("HTTP_TIMEOUT_SECONDS", raising=False)

    settings = load_settings()
    assert settings.github_token is None
    assert settings.github_api_base_url == "https://api.github.com"
    assert settings.max_context_tokens_budget == 16000
    assert settings.max_changed_files_limit == 100
    assert settings.http_timeout_seconds == 30
    assert settings.dl_ranking_enabled is False
    assert settings.code_encoder_model == "microsoft/codebert-base"
    assert settings.dl_device == "cpu"
    assert settings.dl_batch_size == 16


def test_token_masking_in_repr() -> None:
    """Ensure secrets are never exposed when stringifying or inspecting Settings."""
    secret = "ghp_super_secret_personal_access_token_12345"
    settings = Settings(github_token=secret)
    repr_str = repr(settings)
    assert secret not in repr_str
    assert "***" in repr_str


def test_invalid_numeric_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure invalid or negative configuration values raise ConfigurationError."""
    monkeypatch.setenv("MAX_CONTEXT_TOKENS_BUDGET", "-5")
    with pytest.raises(ConfigurationError):
        load_settings()

    monkeypatch.setenv("MAX_CONTEXT_TOKENS_BUDGET", "not_a_number")
    with pytest.raises(ConfigurationError):
        load_settings()

    monkeypatch.delenv("MAX_CONTEXT_TOKENS_BUDGET", raising=False)
    monkeypatch.setenv("DL_BATCH_SIZE", "0")
    with pytest.raises(ConfigurationError):
        load_settings()


def test_logger_redacts_tokens() -> None:
    """Ensure logger redacts sensitive GitHub tokens from log outputs."""
    logger = configure_logger("test_redactor", level="DEBUG")
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(RedactingFormatter(fmt="%(message)s"))
    logger.handlers = [handler]

    secret_token = "ghp_" + "a" * 36
    logger.info(f"Connecting using token {secret_token} now")

    output = stream.getvalue()
    assert secret_token not in output
    assert "[REDACTED_SECRET]" in output
