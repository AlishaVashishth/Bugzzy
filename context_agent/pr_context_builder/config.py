"""Configuration management for Context Builder Agent.

Loads settings from environment variables and optional .env file,
keeping credentials secure and never logging raw secrets.
"""

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from pr_context_builder.exceptions import ConfigurationError


@dataclass(frozen=True)
class Settings:
    """Immutable application settings populated from environment variables."""

    github_token: str | None
    github_api_base_url: str = "https://api.github.com"
    max_context_tokens_budget: int = 16000
    max_changed_files_limit: int = 100
    http_timeout_seconds: int = 30
    log_level: str = "INFO"
    dl_ranking_enabled: bool = False
    code_encoder_model: str = "microsoft/codebert-base"
    dl_device: str = "cpu"
    dl_batch_size: int = 16
    groq_api_key: str | None = None
    groq_model: str = "llama-3.3-70b-versatile"
    groq_timeout_seconds: float = 30.0
    groq_enabled: bool = False
    groq_max_tokens: int = 2048

    def __repr__(self) -> str:
        """Mask secrets when inspecting settings."""
        masked_token = "***" if self.github_token else "None"
        masked_groq = "***" if self.groq_api_key else "None"
        return (
            f"Settings("
            f"github_token={masked_token}, "
            f"github_api_base_url={self.github_api_base_url!r}, "
            f"max_context_tokens_budget={self.max_context_tokens_budget}, "
            f"max_changed_files_limit={self.max_changed_files_limit}, "
            f"http_timeout_seconds={self.http_timeout_seconds}, "
            f"log_level={self.log_level!r}, "
            f"dl_ranking_enabled={self.dl_ranking_enabled}, "
            f"code_encoder_model={self.code_encoder_model!r}, "
            f"dl_device={self.dl_device!r}, "
            f"dl_batch_size={self.dl_batch_size}, "
            f"groq_api_key={masked_groq}, "
            f"groq_model={self.groq_model!r}, "
            f"groq_timeout_seconds={self.groq_timeout_seconds}, "
            f"groq_enabled={self.groq_enabled}, "
            f"groq_max_tokens={self.groq_max_tokens})"
        )


def load_settings(env_file_path: str | Path | None = None) -> Settings:
    """Loads and validates configuration from environment and optional .env file.

    Args:
        env_file_path: Optional explicit path to a .env file.

    Returns:
        Validated Settings instance.

    Raises:
        ConfigurationError: If any numeric values are invalid or non-positive.
    """
    if env_file_path is not None:
        load_dotenv(dotenv_path=env_file_path)
    else:
        load_dotenv()  # Fallbacks to default .env if present

    token = os.getenv("GITHUB_TOKEN")
    base_url = os.getenv("GITHUB_API_BASE_URL", "https://api.github.com").strip()

    try:
        max_budget = int(os.getenv("MAX_CONTEXT_TOKENS_BUDGET", "16000"))
        if max_budget <= 0:
            raise ValueError("MAX_CONTEXT_TOKENS_BUDGET must be positive.")
    except ValueError as exc:
        raise ConfigurationError(f"Invalid MAX_CONTEXT_TOKENS_BUDGET: {exc}") from exc

    try:
        max_files = int(os.getenv("MAX_CHANGED_FILES_LIMIT", "100"))
        if max_files <= 0:
            raise ValueError("MAX_CHANGED_FILES_LIMIT must be positive.")
    except ValueError as exc:
        raise ConfigurationError(f"Invalid MAX_CHANGED_FILES_LIMIT: {exc}") from exc

    try:
        timeout = int(os.getenv("HTTP_TIMEOUT_SECONDS", "30"))
        if timeout <= 0:
            raise ValueError("HTTP_TIMEOUT_SECONDS must be positive.")
    except ValueError as exc:
        raise ConfigurationError(f"Invalid HTTP_TIMEOUT_SECONDS: {exc}") from exc

    log_level = os.getenv("LOG_LEVEL", "INFO").upper().strip()

    dl_ranking_enabled = os.getenv("ENABLE_DL_RANKING", "false").lower() in (
        "true",
        "1",
        "yes",
    )
    code_encoder_model = os.getenv("CODE_ENCODER_MODEL", "microsoft/codebert-base").strip()
    dl_device = os.getenv("DL_DEVICE", "cpu").lower().strip()

    try:
        dl_batch_size = int(os.getenv("DL_BATCH_SIZE", "16"))
        if dl_batch_size <= 0:
            raise ValueError("DL_BATCH_SIZE must be positive.")
    except ValueError as exc:
        raise ConfigurationError(f"Invalid DL_BATCH_SIZE: {exc}") from exc

    groq_api_key = os.getenv("GROQ_API_KEY")
    groq_model = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile").strip()

    groq_enabled_env = os.getenv("ENABLE_GROQ", "").lower().strip()
    groq_enabled = groq_enabled_env in ("true", "1", "yes")

    try:
        groq_timeout = float(os.getenv("GROQ_TIMEOUT_SECONDS", "30.0"))
        if groq_timeout <= 0:
            raise ValueError("GROQ_TIMEOUT_SECONDS must be positive.")
    except ValueError as exc:
        raise ConfigurationError(f"Invalid GROQ_TIMEOUT_SECONDS: {exc}") from exc

    try:
        groq_max_tokens = int(os.getenv("GROQ_MAX_TOKENS", "2048"))
        if groq_max_tokens <= 0:
            raise ValueError("GROQ_MAX_TOKENS must be positive.")
    except ValueError as exc:
        raise ConfigurationError(f"Invalid GROQ_MAX_TOKENS: {exc}") from exc

    return Settings(
        github_token=token if token else None,
        github_api_base_url=base_url,
        max_context_tokens_budget=max_budget,
        max_changed_files_limit=max_files,
        http_timeout_seconds=timeout,
        log_level=log_level,
        dl_ranking_enabled=dl_ranking_enabled,
        code_encoder_model=code_encoder_model,
        dl_device=dl_device,
        dl_batch_size=dl_batch_size,
        groq_api_key=groq_api_key if groq_api_key else None,
        groq_model=groq_model,
        groq_timeout_seconds=groq_timeout,
        groq_enabled=groq_enabled,
        groq_max_tokens=groq_max_tokens,
    )
