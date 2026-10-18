"""Helpers that protect the OAuth login flow.

Two ideas keep the login safe:

* "state": a random value we generate before sending the user to GitHub.
  GitHub sends it back, and if it does not match the value we stored in
  the user's cookie, somebody may be tampering with the request.

* PKCE (Proof Key for Code Exchange): we generate a random "verifier",
  hash it to make a "challenge", send the challenge to GitHub, and later
  send the verifier to prove the same client is finishing the login.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets


def create_state() -> str:
    """Create a random, hard-to-guess state value."""

    # token_urlsafe(32) gives ~43 characters of random text.
    return secrets.token_urlsafe(32)


def create_code_verifier() -> str:
    """Create a random PKCE code verifier.

    GitHub accepts a verifier of 43-128 characters; 64 random bytes
    encoded with token_urlsafe comfortably fits in that range.
    """

    return secrets.token_urlsafe(64)


def code_challenge_s256(verifier: str) -> str:
    """Turn a verifier into its S256 challenge.

    The challenge is the SHA-256 hash of the verifier, encoded with
    base64url and with the trailing "=" padding removed, exactly as the
    PKCE specification requires.
    """

    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii")
    return challenge.rstrip("=")


def verify_state(expected: str | None, received: str | None) -> bool:
    """Safely compare two state values.

    Returns False when either value is missing. Uses a constant-time
    comparison so attackers cannot learn the state one character at a
    time from how long the check takes.
    """

    if not expected or not received:
        return False
    return hmac.compare_digest(expected, received)
