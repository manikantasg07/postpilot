"""Read and write secrets without ever putting them in files.

Lookup order for get_secret("OPENAI_API_KEY"):
  1. Environment variable OPENAI_API_KEY  -> how GitHub Actions passes secrets
  2. OS keyring (Windows Credential Manager) -> how your PC stores them
"""
from __future__ import annotations

import os

import keyring
from keyring.errors import KeyringError, PasswordDeleteError

SERVICE = "postcadence"


class SecretsUnavailable(RuntimeError):
    """There is no keyring on this machine to store secrets in."""

# Every secret the app is allowed to use. Typos fail loudly instead of silently.
KNOWN_SECRETS = {
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "GEMINI_API_KEY",
    "LINKEDIN_CLIENT_ID",
    "LINKEDIN_CLIENT_SECRET",
    "LINKEDIN_ACCESS_TOKEN",
    "GITHUB_TOKEN",
    # Punctuality. Kept apart from GITHUB_TOKEN on purpose: that one is an
    # OAuth token for this machine, while this one is a fine-grained token
    # that goes to a third party and may do nothing but start the workflow.
    "GITHUB_DISPATCH_TOKEN",
    "CRONJOB_API_KEY",
}


def _check(name: str) -> None:
    if name not in KNOWN_SECRETS:
        raise KeyError(f"Unknown secret {name!r}. Known: {sorted(KNOWN_SECRETS)}")


def get_secret(name: str) -> str | None:
    _check(name)
    value = os.environ.get(name)
    if value:
        return value
    try:
        return keyring.get_password(SERVICE, name)
    except KeyringError:
        return None


def set_secret(name: str, value: str) -> None:
    """Store one secret, or say plainly why it could not be stored.

    A machine with no keyring backend - headless Linux, WSL, a container -
    raises here. Letting that escape meant a 500 right after a successful
    sign-in or a billed test call, with the value lost and nothing explaining
    why. SecretsUnavailable is something callers can show.
    """
    _check(name)
    value = value.strip()
    if not value:
        raise ValueError("Secret value is empty")
    try:
        keyring.set_password(SERVICE, name, value)
    except KeyringError as exc:
        raise SecretsUnavailable(
            f"This machine has no place to store {name} securely ({exc}). "
            "Install a keyring backend, or set it as an environment variable."
        ) from exc


def delete_secret(name: str) -> bool:
    """Forget one secret. Missing, or nowhere to look, both mean "not there"."""
    _check(name)
    try:
        keyring.delete_password(SERVICE, name)
        return True
    except (PasswordDeleteError, KeyringError):
        return False


def mask(value: str | None) -> str:
    """'sk-abc123xyz789' -> 'sk-a…z789'. Safe to print."""
    if not value:
        return "(not set)"
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}…{value[-4:]}"
