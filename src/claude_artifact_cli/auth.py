"""Locate the claude.ai OAuth access token this machine already holds.

Claude Code stores its login in the macOS login Keychain under the generic
password service "Claude Code-credentials" (and, on other platforms or as a
fallback, in ~/.claude/.credentials.json). The stored value is a JSON blob
holding the OAuth access token, refresh token and expiry.

The exact key path has moved between Claude Code releases, so rather than
hardcode one we walk the JSON and take the most plausible access token,
preferring anything nested under a claude.ai-looking key.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

KEYCHAIN_SERVICE = "Claude Code-credentials"
CREDENTIALS_FILE = Path.home() / ".claude" / ".credentials.json"

# Checked in order; first one set wins.
ENV_VARS = ("CLAUDE_ARTIFACT_TOKEN", "CLAUDE_CODE_ARTIFACTS_API_TOKEN")

_TOKEN_KEYS = ("accesstoken", "access_token")
_PREFERRED_PARENTS = ("claudeaioauth", "claude_ai_oauth", "oauth")
_EXPIRY_KEYS = ("expiresat", "expires_at")


class AuthError(RuntimeError):
    pass


def _walk(node: Any, parent: str = "") -> list[tuple[str, str, Any]]:
    """Yield (parent_key, token, containing_dict) for every token-looking leaf."""
    found: list[tuple[str, str, Any]] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key.lower().replace("-", "_").replace("_", "") in (
                k.replace("_", "") for k in _TOKEN_KEYS
            ) and isinstance(value, str) and value:
                found.append((parent, value, node))
            else:
                found.extend(_walk(value, key))
    elif isinstance(node, list):
        for item in node:
            found.extend(_walk(item, parent))
    return found


def _pick(blob: str) -> tuple[str, int | None]:
    try:
        data = json.loads(blob)
    except json.JSONDecodeError as exc:
        raise AuthError(f"stored credentials are not valid JSON: {exc}") from exc

    candidates = _walk(data)
    if not candidates:
        raise AuthError(
            "no access token found in the stored credentials. "
            "Run `claude /login`, or pass --token explicitly."
        )

    def rank(item: tuple[str, str, Any]) -> int:
        parent = item[0].lower().replace("_", "")
        return 0 if parent in [p.replace("_", "") for p in _PREFERRED_PARENTS] else 1

    candidates.sort(key=rank)
    _, token, container = candidates[0]

    expires_at = None
    for key, value in container.items():
        if key.lower().replace("_", "") in [e.replace("_", "") for e in _EXPIRY_KEYS]:
            if isinstance(value, (int, float)):
                expires_at = int(value)
            break
    return token, expires_at


def _from_keychain() -> str | None:
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _from_file() -> str | None:
    if not CREDENTIALS_FILE.is_file():
        return None
    return CREDENTIALS_FILE.read_text(encoding="utf-8")


def get_token(explicit: str | None = None) -> str:
    """Return a bearer token, or raise AuthError explaining what to do."""
    if explicit:
        return explicit
    for var in ENV_VARS:
        value = os.environ.get(var)
        if value:
            return value

    blob = _from_keychain() or _from_file()
    if blob is None:
        raise AuthError(
            "couldn't find a Claude Code login on this machine.\n"
            f"  Looked in: macOS Keychain service {KEYCHAIN_SERVICE!r}, {CREDENTIALS_FILE}\n"
            "  Fix: run `claude /login`, or pass --token / set "
            f"{ENV_VARS[0]}."
        )

    token, expires_at = _pick(blob)
    if expires_at and expires_at / 1000 < time.time():
        raise AuthError(
            "the stored Claude Code access token has expired. "
            "Start Claude Code once (it refreshes on launch), or run `claude /login`."
        )
    return token
