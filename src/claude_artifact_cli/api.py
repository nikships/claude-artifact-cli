"""Minimal client for the Claude Artifacts ("frame") API.

Protocol reverse-engineered from the Claude Code CLI bundle (v2.1.273, checked
against v2.1.287).

    POST {base}/api/frame/deploy/direct     publish
    POST {base}/api/frame/deploy/prepare    content-hash preflight (large publishes)
    POST {base}/api/frame/upload            stage blobs the preflight asked for
    GET  {base}/api/frame/{slug}?via=model_read   metadata + file manifest
    GET  {base}/api/frame/read/{slug}       ownership / sharing status
    GET  {base}/api/frame/frames?limit=N    list artifacts

base defaults to https://api.anthropic.com. Auth is the claude.ai OAuth access
token as a bearer, with the anthropic-beta: oauth-2025-04-20 opt-in.
"""

from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

DEFAULT_BASE = "https://api.anthropic.com"
ARTIFACT_URL = "https://claude.ai/code/artifact/{slug}"
OAUTH_BETA = "oauth-2025-04-20"
CLIENT_VERSION = "2.1.287"

SLUG_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# Content types the server takes as a raw UTF-8 string; everything else is
# base64-encoded on the wire. Mirrors the CLI's own set exactly.
TEXT_TYPES = frozenset(
    {
        "text/html",
        "text/css",
        "text/plain",
        "text/markdown",
        "text/csv",
        "text/javascript",
        "application/javascript",
        "application/json",
        "application/manifest+json",
        "application/xml",
        "text/xml",
        "image/svg+xml",
    }
)

# Inline bodies above this get split into prepare -> upload -> deploy.
INLINE_BUDGET = 15 * 1024 * 1024
UPLOAD_BATCH = 15 * 1024 * 1024


class ApiError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, body: Any = None):
        super().__init__(message)
        self.status = status
        self.body = body


class ConflictError(ApiError):
    """A publish was refused because a newer version than baseVersion is live."""

    @property
    def live(self) -> str | None:
        return self.body.get("live") if isinstance(self.body, dict) else None


# Responses carry short-lived credentials: the boot's assetToken and
# subscriptionToken, and __frame_t on every thumbnail URL. Strip them before
# anything reaches stdout or disk.
_FRAME_T_RE = re.compile(r"""(__frame_t=)[^&#\s'"]*""", re.IGNORECASE)
REDACTED = "[redacted]"


def redact(node: Any) -> Any:
    """Copy of a response with every token-like value replaced."""
    if isinstance(node, dict):
        return {
            key: REDACTED
            if key.lower().endswith("token") and isinstance(value, str)
            else redact(value)
            for key, value in node.items()
        }
    if isinstance(node, list):
        return [redact(item) for item in node]
    if isinstance(node, str) and "__frame_t" in node.lower():
        return _FRAME_T_RE.sub(lambda m: m.group(1) + REDACTED, node)
    return node


@dataclass
class Asset:
    """One file in the publish."""

    path: str
    data: bytes
    content_type: str

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.data).hexdigest()

    def wire(self) -> str:
        if self.content_type in TEXT_TYPES:
            return self.data.decode("utf-8")
        return base64.b64encode(self.data).decode("ascii")

    def wire_bytes(self) -> int:
        """Roughly what this costs JSON-encoded, matching the CLI's accounting."""
        return len(json.dumps(self.wire())) - 2


def guess_content_type(path: str) -> str:
    guessed, _ = mimetypes.guess_type(path)
    if guessed == "application/x-javascript":
        return "text/javascript"
    return guessed or "application/octet-stream"


def slug_from(value: str) -> str:
    """Accept a bare slug or a full claude.ai artifact URL."""
    value = value.strip()
    match = re.search(r"/code/artifact/([A-Za-z0-9_-]+)", value)
    if match:
        return match.group(1)
    if SLUG_RE.match(value):
        return value
    raise ApiError(f"not a valid artifact slug or URL: {value!r}")


class FrameClient:
    def __init__(self, token: str, base: str = DEFAULT_BASE, timeout: int = 120):
        self.token = token
        self.base = base.rstrip("/")
        self.timeout = timeout

    # -- transport ---------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "anthropic-beta": OAUTH_BETA,
            "Content-Type": "application/json",
            "Accept": "application/json, application/vnd.ant.frame-refusal+json",
            "X-Frame-CP": "go",
            "X-Frame-Surface": "code",
            "X-Frame-Platform": "cli",
            "X-Frame-Client-Version": CLIENT_VERSION,
            "User-Agent": f"claude-artifact-cli/{CLIENT_VERSION}",
        }

    def _request(self, method: str, path: str, body: dict | None = None) -> Any:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            f"{self.base}{path}", data=data, headers=self._headers(), method=method
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
                status = resp.status
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            status = exc.code
        except urllib.error.URLError as exc:
            raise ApiError(f"network error talking to {self.base}: {exc.reason}") from exc

        try:
            parsed = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            parsed = raw.decode("utf-8", "replace")

        if status == 401:
            raise ApiError(
                "401 unauthorized - the token was rejected. Note this route needs a "
                "claude.ai OAuth token (from `claude /login`), not an ANTHROPIC_API_KEY.",
                status,
                parsed,
            )
        if status == 409:
            raise ConflictError(_describe_conflict(parsed), status, parsed)
        if status >= 400:
            raise ApiError(_describe(status, parsed), status, parsed)
        return parsed

    # -- operations --------------------------------------------------------

    def list_frames(self, limit: int = 200) -> list[dict]:
        data = self._request("GET", f"/api/frame/frames?limit={int(limit)}")
        if isinstance(data, list):
            return redact(data)
        for key in ("frames", "results", "data"):
            if isinstance(data, dict) and isinstance(data.get(key), list):
                return redact(data[key])
        raise ApiError("unexpected listing response shape", body=data)

    def read(self, slug: str) -> dict:
        """Artifact metadata + published file manifest.

        Note: the published *bytes* are served from a separate sandboxed host
        behind a short-lived asset token, so they are not fetched here. This
        returns paths, sizes, sha256 and content types, with tokens redacted.
        """
        quoted = urllib.parse.quote(slug)
        boot = redact(self._request("GET", f"/api/frame/{quoted}?via=model_read"))
        try:
            boot["_access"] = redact(self._request("GET", f"/api/frame/read/{quoted}"))
        except ApiError:
            pass
        return boot

    def current_version(self, slug: str) -> str | None:
        boot = self._request("GET", f"/api/frame/{urllib.parse.quote(slug)}?via=model_read")
        return boot.get("ver") if isinstance(boot, dict) else None

    def publish(
        self,
        page: Asset,
        extra: list[Asset] | None = None,
        removals: list[str] | None = None,
        slug: str | None = None,
        meta: dict | None = None,
        mode: str | None = None,
        base_version=None,
        force: bool = False,
        on_progress=lambda msg: None,
    ) -> dict:
        # "patch" overlays the manifest onto baseVersion, so files left out are
        # kept; "replace" makes the manifest the whole artifact. Updates default
        # to patch, as the Artifact tool does, so publishing just the page never
        # drops its CSS, JS or images. Explicit null removals are patch-only.
        # This is settled on the caller's slug, before a large publish's
        # preflight reserves one for a brand-new artifact.
        if mode is None:
            mode = "patch" if slug else "replace"
        if removals and not slug:
            raise ApiError("removing a file needs --slug/--url (there is nothing to patch)")
        if removals and mode != "patch":
            raise ApiError(
                'removing a file needs --mode patch (in "replace" mode, just omit the file)'
            )
        if mode == "patch" and not slug:
            raise ApiError("--mode patch needs --slug/--url (there is nothing to patch)")
        if mode == "patch" and base_version is None:
            # The server needs a version to overlay onto. Without one the caller
            # knows, the live version stands in, which also means a concurrent
            # publish made since the caller last looked is not detected.
            on_progress("no base version known - patching onto the live version")
            base_version = self.current_version(slug)
            if base_version is None:
                raise ApiError("couldn't determine the current version to patch onto")

        assets = [page] + list(extra or [])
        manifest: dict[str, Any] = {}

        total = sum(a.wire_bytes() for a in assets)
        staged: list[Asset] = []

        if total > INLINE_BUDGET:
            on_progress(
                f"body is {total // 1024 // 1024} MB - using prepare/upload flow"
            )
            slug, staged = self._stage(assets, slug, on_progress)

        staged_paths = {a.path for a in staged}
        for asset in assets:
            if asset.path in staged_paths:
                manifest[asset.path] = {
                    "sha256": asset.sha256,
                    "contentType": asset.content_type,
                }
            else:
                manifest[asset.path] = {
                    "content": asset.wire(),
                    "contentType": asset.content_type,
                }
        for path in removals or []:
            manifest[path] = None

        body: dict[str, Any] = dict(meta or {})
        body["manifest"] = manifest
        body["mode"] = mode
        if slug:
            body["slug"] = slug
        if base_version is not None:
            body["baseVersion"] = base_version
        if force:
            body["force"] = True

        on_progress("POST /api/frame/deploy/direct")
        result = self._request("POST", "/api/frame/deploy/direct", body)
        if not isinstance(result, dict) or "slug" not in result:
            raise ApiError("deploy returned an incomplete response", body=result)
        return result

    # -- large-publish path ------------------------------------------------

    def _stage(self, assets: list[Asset], slug: str | None, on_progress):
        """prepare -> upload. Returns (slug, assets staged by hash)."""
        by_sha = {a.sha256: a for a in assets}
        prep_body: dict[str, Any] = {"shas": list(by_sha)}
        if slug:
            prep_body["slug"] = slug

        on_progress("POST /api/frame/deploy/prepare")
        prep = self._request("POST", "/api/frame/deploy/prepare", prep_body)
        if not isinstance(prep, dict) or not isinstance(prep.get("slug"), str):
            raise ApiError("malformed preflight answer", body=prep)

        reserved = prep["slug"]
        if slug and reserved != slug:
            raise ApiError("preflight answered for a different artifact - not publishing")

        missing = [s for s in prep.get("missing") or [] if s in by_sha]
        if not missing:
            return reserved, []

        batch: list[Asset] = []
        size = 0
        staged: list[Asset] = []
        for sha in missing:
            asset = by_sha[sha]
            cost = asset.wire_bytes()
            if batch and size + cost > UPLOAD_BATCH:
                self._upload(reserved, batch, on_progress)
                staged.extend(batch)
                batch, size = [], 0
            batch.append(asset)
            size += cost
        if batch:
            self._upload(reserved, batch, on_progress)
            staged.extend(batch)
        return reserved, staged

    def _upload(self, slug: str, batch: list[Asset], on_progress) -> None:
        on_progress(f"POST /api/frame/upload ({len(batch)} file(s))")
        self._request(
            "POST",
            "/api/frame/upload",
            {
                "slug": slug,
                "files": [
                    {"path": a.path, "content": a.wire(), "contentType": a.content_type}
                    for a in batch
                ],
            },
        )


def _describe_conflict(body: Any) -> str:
    live = body.get("live") if isinstance(body, dict) else None
    where = f" (live version {live})" if live else ""
    return (
        f"HTTP 409: a newer version{where} was published since the base version "
        "this publish was made against. Compare with `claude-artifact status`, merge, "
        "then publish again with --base-version set to the live version, or pass "
        "--force to discard it."
    )


def _describe(status: int, body: Any) -> str:
    detail = ""
    if isinstance(body, dict):
        detail = body.get("message") or body.get("error") or json.dumps(body)[:400]
    elif isinstance(body, str):
        detail = body[:400]
    hint = {
        403: " (the account may not have Artifacts enabled)",
        404: " (no such artifact, or it isn't yours to update)",
        413: " (payload too large)",
        429: " (rate limited or daily publish cap reached)",
    }.get(status, "")
    return f"HTTP {status}{hint}: {detail}".rstrip(": ")
