"""Application configuration.

This module is the single place where we read environment variables.
Keeping it separate means the rest of the code never has to know where
the values come from (a .env file when developing locally, real
environment variables in production).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv

# BASE_DIR is the "backend" folder. We compute it from this file's own
# location instead of using the current working directory, so the app
# finds .env correctly no matter where it is started from.
BASE_DIR = Path(__file__).resolve().parent.parent

# The .env file that lives next to the "app" and "secrets" folders.
ENV_PATH = BASE_DIR / ".env"

# Load the .env file into the process environment. load_dotenv does not
# overwrite variables that are already set, which is what we want: real
# environment variables should win over the local file.
load_dotenv(ENV_PATH)

# Names of the variables we need. We keep them in one list so the error
# message can name the missing variable precisely.
_REQUIRED_VARS = (
    "GITHUB_APP_ID",
    "GITHUB_CLIENT_ID",
    "GITHUB_CLIENT_SECRET",
    "GITHUB_PRIVATE_KEY_PATH",
    "GITHUB_WEBHOOK_SECRET",
)

# Optional variables with safe defaults. These are read if present but the
# app still starts without them, which keeps local setup simple.
DEFAULT_REDIRECT_URI = "http://localhost:8000/api/v1/auth/github/callback"
DEFAULT_FRONTEND_URL = "http://localhost:5173"
DEFAULT_APP_ENV = "development"

# --- Step 3 session defaults ---
DEFAULT_SESSION_COOKIE_NAME = "bugzzy_session"
# Absolute lifetime: a session can live at most 7 days, no matter how active.
DEFAULT_SESSION_MAX_AGE_SECONDS = 604800
# Idle timeout: a session that has not been used for 24 hours is expired.
DEFAULT_SESSION_IDLE_TIMEOUT_SECONDS = 86400


@dataclass(frozen=True)
class Settings:
    """Immutable snapshot of the configuration the app needs.

    "frozen=True" means the values cannot be changed after creation, which
    helps prevent accidental edits at runtime.
    """

    app_id: int
    client_id: str
    client_secret: str
    private_key_path: Path
    webhook_secret: str

    # --- Step 2 additions (all optional, with safe defaults) ---
    redirect_uri: str
    frontend_url: str
    app_env: str

    # --- Step 3 additions (all optional, with safe defaults) ---
    session_cookie_name: str
    session_max_age_seconds: int
    session_idle_timeout_seconds: int

    @property
    def frontend_origin(self) -> str:
        """Just the scheme + host of the frontend URL, e.g. http://localhost:5173.

        Browsers send this exact string in the "Origin" header, so we use it
        to compare against.
        """

        parts = urlsplit(self.frontend_url)
        return f"{parts.scheme}://{parts.netloc}"

    @property
    def backend_origin(self) -> str:
        """Just the scheme + host of this backend, e.g. http://localhost:8000.

        Derived from the OAuth redirect_uri, which points at this server.
        """

        parts = urlsplit(self.redirect_uri)
        return f"{parts.scheme}://{parts.netloc}"

    @property
    def is_production(self) -> bool:
        """True only when APP_ENV is exactly "production".

        We use this to decide whether cookies must be sent over HTTPS
        (the "secure" flag). Locally, over plain HTTP, a secure cookie
        would never be stored by the browser, so we keep it False.
        """

        return self.app_env == "production"

    @property
    def cookie_secure(self) -> bool:
        """Whether auth cookies should carry the Secure flag."""

        return self.is_production


def _require(name: str) -> str:
    """Return an environment variable or fail fast naming the variable.

    The error message deliberately includes only the variable *name*, never
    its value, so secrets cannot leak into logs or tracebacks.
    """

    value = os.getenv(name)
    if value is None or value.strip() == "":
        raise RuntimeError(
            f"Missing required environment variable: {name}. "
            f"Add it to {ENV_PATH} or set it in the environment."
        )
    return value.strip()


def _optional(name: str, default: str) -> str:
    """Return an environment variable, or the default when unset/blank."""

    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return value.strip()


def _optional_int(name: str, default: int) -> int:
    """Return an integer environment variable, or the default.

    A non-numeric value is a configuration mistake, so we fail fast naming
    the variable (never its value).
    """

    raw = _optional(name, str(default))
    try:
        return int(raw)
    except ValueError as exc:
        raise RuntimeError(
            f"Invalid environment variable: {name} must be a whole number."
        ) from exc


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Build and cache the Settings object.

    lru_cache keeps a single instance around, so we only read the
    environment and validate the files once per process.
    """

    # Read every required variable first so a missing one is reported.
    raw_values = {name: _require(name) for name in _REQUIRED_VARS}

    # The App ID must be a number; GitHub uses numeric App IDs.
    try:
        app_id = int(raw_values["GITHUB_APP_ID"])
    except ValueError as exc:
        raise RuntimeError(
            "Invalid environment variable: GITHUB_APP_ID must be a number."
        ) from exc

    # Resolve the private key path relative to the backend folder, then make
    # it absolute. This way "./secrets/key.pem" works regardless of the
    # current working directory.
    key_path = Path(raw_values["GITHUB_PRIVATE_KEY_PATH"]).expanduser()
    if not key_path.is_absolute():
        key_path = (BASE_DIR / key_path).resolve()
    else:
        key_path = key_path.resolve()

    # Fail fast if the key file is not there, naming the path (not its
    # contents) so the problem is obvious.
    if not key_path.is_file():
        raise RuntimeError(
            f"Private key file not found at {key_path}. "
            "Check GITHUB_PRIVATE_KEY_PATH."
        )

    return Settings(
        app_id=app_id,
        client_id=raw_values["GITHUB_CLIENT_ID"],
        client_secret=raw_values["GITHUB_CLIENT_SECRET"],
        private_key_path=key_path,
        webhook_secret=raw_values["GITHUB_WEBHOOK_SECRET"],
        # Optional values fall back to sensible local-development defaults.
        redirect_uri=_optional("GITHUB_REDIRECT_URI", DEFAULT_REDIRECT_URI),
        frontend_url=_optional("FRONTEND_URL", DEFAULT_FRONTEND_URL),
        app_env=_optional("APP_ENV", DEFAULT_APP_ENV),
        session_cookie_name=_optional(
            "SESSION_COOKIE_NAME", DEFAULT_SESSION_COOKIE_NAME
        ),
        session_max_age_seconds=_optional_int(
            "SESSION_MAX_AGE_SECONDS", DEFAULT_SESSION_MAX_AGE_SECONDS
        ),
        session_idle_timeout_seconds=_optional_int(
            "SESSION_IDLE_TIMEOUT_SECONDS", DEFAULT_SESSION_IDLE_TIMEOUT_SECONDS
        ),
    )
