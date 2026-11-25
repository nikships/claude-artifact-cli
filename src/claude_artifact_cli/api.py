"""Minimal client for the Claude Artifacts ("frame") API.

Protocol reverse-engineered from the Claude Code CLI bundle (v2.1.273, checked
against v2.1.332).

    POST {base}/api/frame/deploy/direct     publish
    POST {base}/api/frame/deploy/prepare    content-hash preflight (large publishes)
    POST {base}/api/frame/upload            stage blobs the preflight asked for
    GET  {base}/api/frame/{slug}?via=model_read   metadata + file manifest
    GET  {base}/api/frame/read/{slug}       ownership / sharing status
    GET  {base}/api/frame/artifacts?rel=R&limit=N[&page_token=T]   list artifacts
    GET  {base}/api/frame/frames?limit=N    list artifacts (older, unpaged)

    GET  https://{slug}.frame.claudeusercontent.com/_f/{ver}/_src/{path}?__frame_t={assetToken}
         published source bytes; /_f/{ver}/{path} is the served copy

base defaults to https://api.anthropic.com. Auth is the claude.ai OAuth access
token as a bearer, with the anthropic-beta: oauth-2025-04-20 opt-in. The content
host takes only the boot's short-lived assetToken, never the OAuth token.
"""

from __future__ import annotations

import base64
import hashlib
import http.client
import json
import mimetypes
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

DEFAULT_BASE = "https://api.anthropic.com"
ARTIFACT_URL = "https://claude.ai/code/artifact/{slug}"
CONTENT_HOST = "https://{slug}.frame.claudeusercontent.com"
OAUTH_BETA = "oauth-2025-04-20"
CLIENT_VERSION = "2.1.332"

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

# Content type by extension, as Claude Code assigns it; a file named with one
# of these needs no explicit type. Python's mimetypes varies by platform and
# version (.md, .webmanifest, .avif, .woff2), so this map is consulted first.
EXTENSION_TYPES = {
    ".html": "text/html",
    ".css": "text/css",
    ".js": "text/javascript",
    ".mjs": "text/javascript",
    ".json": "application/json",
    ".map": "application/json",
    ".geojson": "application/json",
    ".webmanifest": "application/manifest+json",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".csv": "text/csv",
    ".xml": "application/xml",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".avif": "image/avif",
    ".ico": "image/vnd.microsoft.icon",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
    ".otf": "font/otf",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".ogg": "audio/ogg",
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".pdf": "application/pdf",
    ".wasm": "application/wasm",
}

# Content types artifacts serve. Claude Code refuses to publish anything else.
SERVED_TYPES = TEXT_TYPES | frozenset(
    {
        "application/wasm",
        "image/png",
        "image/jpeg",
        "image/gif",
        "image/webp",
        "image/avif",
        "image/x-icon",
        "image/vnd.microsoft.icon",
        "font/woff",
        "font/woff2",
        "font/ttf",
        "font/otf",
        "application/font-woff",
        "application/font-woff2",
        "audio/mpeg",
        "audio/ogg",
        "audio/wav",
        "audio/webm",
        "video/mp4",
        "video/webm",
        "video/ogg",
        "application/pdf",
    }
)

# Inline bodies above this get split into prepare -> upload -> deploy.
INLINE_BUDGET = 15 * 1024 * 1024
UPLOAD_BATCH = 15 * 1024 * 1024

# The server refuses a manifest of more than 256 entries in one request; a
# bigger publish goes as several, each later one a patch onto the one before.
# 255 matches Claude Code's own per-call limit.
MANIFEST_MAX = 255

# Publish retries, as Claude Code paces them.
_SETTLE_RESENDS = 2  # 409 publish_settling: the first attempt is still being stored
_SETTLE_WAIT_MAX = 20.0
_BUSY_ATTEMPTS = 3  # 503: total attempts
_BUSY_WAIT_MAX = 20.0
_RATE_WAIT_MAX = 30.0  # 429 that is not a plan cap: one retry if Retry-After allows
_PLAN_CAPS = {
    "daily_new": "daily new-artifact limit for your plan reached - resets at UTC midnight",
    "daily_pushes": "daily publish limit for your plan reached - resets at UTC midnight",
    "rate_limited": "publish rate limit for your plan reached",
    "contract_rollout": "this runtime contract isn't available to your plan yet",
}

# The served copy of the page carries the frame runtime, and can carry a
# trailing comments script; the stored source has neither.
_RUNTIME_RE = re.compile(rb"<!-- frame-runtime -->.*?<!-- /frame-runtime -->", re.DOTALL)
_COMMENTS_TAG = b'<script type="application/json" id="__frame_comments__">'


class ApiError(RuntimeError):
    def __init__(
        self,
        message: str,
        status: int | None = None,
        body: Any = None,
        retry_after: float | None = None,
    ):
        super().__init__(message)
        self.status = status
        self.body = body
        self.retry_after = retry_after


class NetworkError(ApiError):
    """The request got no HTTP answer: it may or may not have reached the server."""


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
            try:
                return self.data.decode("utf-8")
            except UnicodeDecodeError:
                raise ApiError(
                    f"{self.path} goes as {self.content_type}, which must be UTF-8, and it "
                    "isn't; convert it, or give it a binary type"
                ) from None
        return base64.b64encode(self.data).decode("ascii")

    def wire_bytes(self) -> int:
        """Roughly what this costs JSON-encoded, matching the CLI's accounting."""
        return len(json.dumps(self.wire())) - 2


@dataclass
class Copy:
    """A file the server copies from another artifact's published version."""

    path: str
    from_slug: str
    from_path: str
    from_ver: str
    sha256: str
    content_type: str


def guess_content_type(path: str) -> str:
    dot = path.rfind(".")
    if dot > path.rfind("/"):
        known = EXTENSION_TYPES.get(path[dot:].lower())
        if known:
            return known
    guessed, _ = mimetypes.guess_type(path)
    if guessed == "application/x-javascript":
        return "text/javascript"
    return guessed or "application/octet-stream"


def clean_path(path: str) -> str | None:
    """A published path as a URL path, or None if it isn't a clean relative path."""
    if not path or len(path.encode("utf-8")) > 512:
        return None
    parts = path.split("/")
    for part in parts:
        if part in ("", ".", "..") or re.search(r"[\\%\x00-\x1f\x7f]", part):
            return None
    return "/".join(urllib.parse.quote(part, safe="") for part in parts)


def strip_served_page(data: bytes) -> bytes:
    """Recover a page's source from its served copy."""
    data = _RUNTIME_RE.sub(b"", data, count=1)
    trimmed = data.rstrip(b" \n")
    start = trimmed.rfind(_COMMENTS_TAG)
    if start == -1 or not trimmed.endswith(b"</script>"):
        return data
    try:
        payload = json.loads(trimmed[start + len(_COMMENTS_TAG) : -len(b"</script>")])
    except ValueError:
        return data
    if not isinstance(payload, dict) or "mac" not in payload:
        return data
    head = trimmed[:start]
    return head[:-1] if head.endswith(b"\n") else head


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class FileReader:
    """Published bytes of one artifact version.

    Boots once to get the version, manifest and assetToken. The token stays on
    this object: it is never printed, logged, written or put in an error.
    """

    def __init__(self, client: FrameClient, slug: str):
        self._client = client
        self.slug = slug
        self._opener = urllib.request.build_opener(_NoRedirect)
        self._boot()

    def _boot(self) -> None:
        boot = self._client._boot(self.slug)
        token = boot.get("assetToken")
        if not isinstance(token, str) or not token:
            raise ApiError(
                "this login can't read the artifact's files (the server issued no asset "
                "token - a public artifact from another organization, for example)"
            )
        self._token = token
        self.version = boot.get("ver")
        self.files = {
            f["path"]: f
            for f in boot.get("files") or []
            if isinstance(f, dict) and isinstance(f.get("path"), str)
        }
        self.meta = redact(boot)
        role = (boot.get("perm") or {}).get("role")
        # Mirrors Claude Code: the stored source is offered unless the artifact
        # is shared in from outside and the reader can't write to it.
        self._source = boot.get("mode") != "external" or role == "writer"

    def type_owned(self, path: str) -> bool:
        """True for files an Artifact type supplies; they can't be republished."""
        return isinstance(self.files.get(path, {}).get("src"), dict)

    def fetch(self, path: str) -> bytes:
        entry = self.files.get(path)
        if entry is None:
            raise ApiError(f"no file {path!r} in version {self.version}", 404)
        quoted = clean_path(path)
        if quoted is None:
            raise ApiError(f"{path!r} is not a clean relative path")
        status, data, denied = self._fetch(path, quoted)
        if status != 200 and denied:
            # Asset tokens are short-lived; boot again once for a fresh one.
            version = self.version
            self._boot()
            if self.version != version:
                raise ApiError(
                    f"version {self.version} was published while reading {version} - "
                    "run the command again"
                )
            status, data, _ = self._fetch(path, quoted)
        if status == 404:
            raise ApiError(f"no file {path!r} is served for version {self.version}", 404)
        if status != 200:
            raise ApiError(f"reading {path!r} failed with HTTP {status}", status)
        expected = entry.get("sha256")
        if isinstance(expected, str) and hashlib.sha256(data).hexdigest() != expected:
            raise ApiError(f"{path!r} doesn't match its published sha256")
        return data

    def fetch_asset(self, asset_id: str, sha256: str | None = None) -> bytes:
        """An uploaded asset's bytes, checked against `sha256` when given."""
        if not re.fullmatch(r"[0-9a-f]{32}", asset_id):
            raise ApiError(f"{asset_id!r} is not an asset id (32 hex characters)")
        status, data = self._get(f"/_f/{self.version}/_blob/{asset_id}")
        if status in (401, 403):
            self._boot()
            status, data = self._get(f"/_f/{self.version}/_blob/{asset_id}")
        if status == 404:
            raise ApiError(f"no asset {asset_id} on this artifact", 404)
        if status != 200:
            raise ApiError(f"reading asset {asset_id} failed with HTTP {status}", status)
        if sha256 and hashlib.sha256(data).hexdigest() != sha256:
            raise ApiError(f"asset {asset_id} doesn't match its sha256")
        return data

    def _fetch(self, path: str, quoted: str) -> tuple[int, bytes, bool]:
        """(status, bytes, whether either request was refused for its token)."""
        denied = False
        if self._source:
            status, data = self._get(f"/_f/{self.version}/_src/{quoted}")
            if status == 200:
                return status, data, False
            denied = status in (401, 403)
        served = "" if path == "index.html" else quoted
        status, data = self._get(f"/_f/{self.version}/{served}")
        if status == 200 and path == "index.html":
            data = strip_served_page(data)
        return status, data, denied or status in (401, 403)

    def _get(self, url_path: str) -> tuple[int, bytes]:
        url = (
            CONTENT_HOST.format(slug=self.slug)
            + url_path
            + "?__frame_t="
            + urllib.parse.quote(self._token, safe="")
        )
        # Only a User-Agent: the OAuth token never goes to the content host, and
        # its CDN refuses urllib's default agent.
        req = urllib.request.Request(
            url, headers={"User-Agent": f"claude-artifact-cli/{CLIENT_VERSION}"}
        )
        try:
            with self._opener.open(req, timeout=self._client.timeout) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            exc.close()
            return exc.code, b""
        except urllib.error.URLError as exc:
            raise ApiError(
                f"network error reading {self.slug}.frame.claudeusercontent.com: "
                f"{redact(str(exc.reason))}"
            ) from None


_BASE58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_SHORT_ID_RE = re.compile(f"^[{_BASE58}]{{16,22}}$")


def _uuid_from_short_id(value: str) -> str | None:
    """claude.ai/artifact/<id> links carry the slug's UUID in base58."""
    if not _SHORT_ID_RE.match(value):
        return None
    n = 0
    for char in value:
        n = n * 58 + _BASE58.index(char)
    if n >= 1 << 128:
        return None
    h = f"{n:032x}"
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"


def slug_from(value: str) -> str:
    """Accept a slug, a short id, or a claude.ai artifact URL of either form."""
    value = value.strip()
    match = re.search(r"claude\.ai/(?:code/)?artifact/([A-Za-z0-9_-]+)", value)
    candidate = match.group(1) if match else value
    short = _uuid_from_short_id(candidate)
    if short:
        return short
    if SLUG_RE.match(candidate):
        return candidate
    raise ApiError(f"not a valid artifact slug or URL: {value!r}")


def _q(value: str) -> str:
    return urllib.parse.quote(value, safe="")


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

    def _request(
        self,
        method: str,
        path: str,
        body: dict | None = None,
        *,
        raw: bytes | None = None,
        content_type: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        """One API call. `raw` sends bytes as-is under `content_type` instead of JSON."""
        data = raw if raw is not None else (
            json.dumps(body).encode("utf-8") if body is not None else None
        )
        sent = self._headers()
        if content_type:
            sent["Content-Type"] = content_type
        sent.update(headers or {})
        req = urllib.request.Request(f"{self.base}{path}", data=data, headers=sent, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw_reply = resp.read()
                status = resp.status
                reply_headers = getattr(resp, "headers", None)
        except urllib.error.HTTPError as exc:
            status = exc.code
            reply_headers = getattr(exc, "headers", None)
            try:
                raw_reply = exc.read()
            except (OSError, http.client.HTTPException):
                # The status is known; only the error body was lost.
                raw_reply = b""
        except urllib.error.URLError as exc:
            raise NetworkError(f"network error talking to {self.base}: {exc.reason}") from exc
        except (OSError, http.client.HTTPException) as exc:
            # A reset, timeout or truncated body mid-response: the request may have landed.
            raise NetworkError(f"network error talking to {self.base}: {exc!r}") from exc
        retry_after = _retry_after(reply_headers)

        try:
            parsed = json.loads(raw_reply) if raw_reply else None
        except json.JSONDecodeError:
            parsed = raw_reply.decode("utf-8", "replace")

        if status == 401:
            raise ApiError(
                "401 unauthorized - the token was rejected. Note this route needs a "
                "claude.ai OAuth token (from `claude /login`), not an ANTHROPIC_API_KEY.",
                status,
                parsed,
            )
        if status == 409 and "publish_settling" not in _codes(parsed):
            message = (
                _describe_conflict(parsed)
                if path.startswith("/api/frame/deploy/")
                else _describe(status, parsed)
            )
            raise ConflictError(message, status, parsed, retry_after)
        if status >= 400:
            raise ApiError(_describe(status, parsed), status, parsed, retry_after)
        return parsed

    # -- operations --------------------------------------------------------

    def list_artifacts(
        self, rel: str = "mine", limit: int = 200, page_token: str | None = None
    ) -> tuple[list[dict], str | None]:
        """One page of the gallery: (rows, next page token or None).

        `rel` is mine, shared or all (favorited and template answer 403 to the
        OAuth token: they serve browser sessions only). Rows carry
        `slug` (the API's `id`) and `updatedAt` alongside the API's own fields.
        """
        query = {"rel": rel, "kind": "page,document", "sort": "last_viewed", "limit": str(limit)}
        if page_token:
            query["page_token"] = page_token
        data = self._request("GET", "/api/frame/artifacts?" + urllib.parse.urlencode(query))
        artifacts = (data.get("artifacts") or []) if isinstance(data, dict) else None
        if not isinstance(artifacts, list):
            raise ApiError("unexpected listing response shape", body=data)
        rows = []
        for row in artifacts:
            if not isinstance(row, dict) or row.get("kind") not in ("page", "document"):
                continue
            rows.append(
                {"slug": row.get("id"), "updatedAt": row.get("updated_at") or row.get("created_at"), **row}
            )
        token = data.get("next_page_token")
        return redact(rows), token if isinstance(token, str) and token else None

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

        Paths, sizes, sha256 and content types, with tokens redacted. The
        bytes themselves come from files().
        """
        boot = redact(self._boot(slug))
        try:
            boot["_access"] = self.access(slug)
        except ApiError:
            pass
        return boot

    def access(self, slug: str) -> dict:
        """Ownership, sharing, and the stored contract and capabilities."""
        return redact(self._request("GET", f"/api/frame/read/{_q(slug)}"))

    def _boot(self, slug: str) -> dict:
        """The raw boot response. It holds live tokens: never return it to callers."""
        boot = self._request("GET", f"/api/frame/{urllib.parse.quote(slug)}?via=model_read")
        if not isinstance(boot, dict):
            raise ApiError("unexpected boot response shape")
        return boot

    def current_version(self, slug: str) -> str | None:
        return self._boot(slug).get("ver")

    def files(self, slug: str) -> FileReader:
        """A reader for the published bytes of the artifact's live version."""
        return FileReader(self, slug)

    def versions(self, slug: str) -> dict:
        """Version history: live, history (newest first) and per-version metadata."""
        return redact(self._request("GET", f"/api/frame/versions/{_q(slug)}"))

    def retitle(self, slug: str, title: str) -> dict:
        """Rename. The server records the new title as a new version."""
        return redact(self._request("POST", f"/api/frame/retitle/{_q(slug)}", {"title": title}))

    def pin(self, slug: str, pinned: bool = True) -> None:
        self._request("POST" if pinned else "DELETE", f"/api/frame/favorite/{_q(slug)}")

    def delete(self, slug: str) -> None:
        """Delete the artifact for everyone. There is no undo."""
        self._request("DELETE", f"/api/frame/{_q(slug)}")

    # -- sharing -----------------------------------------------------------

    def sharing(self, slug: str) -> dict:
        return redact(self._request("GET", f"/api/frame/perm/{_q(slug)}"))

    def set_sharing(self, slug: str, change: dict, version: str) -> dict:
        """PATCH the sharing settings read at `version`; a change made since is refused."""
        return redact(
            self._request(
                "PATCH", f"/api/frame/perm/{_q(slug)}", change, headers={"If-Match": version}
            )
        )

    # -- comments ----------------------------------------------------------

    def comments(self, slug: str) -> dict:
        """Every comment thread, with `names` mapping account ids to display names."""
        return redact(self._request("GET", f"/api/frame/comments/{_q(slug)}?names=1"))

    def reply(
        self, slug: str, thread: str, text: str, client_id: str, answers_summon: bool = False
    ) -> dict:
        """Reply as Claude on a thread a person sent to Claude.

        `client_id` makes the post idempotent: resending it answers `replayed`
        instead of posting twice.
        """
        body: dict[str, Any] = {"id": client_id, "text": text}
        if answers_summon:
            body["answers_summon"] = True
        data = self._request(
            "POST", f"/api/frame/comments/{_q(slug)}/{_q(thread)}", body
        )
        if not isinstance(data, dict) or "comment_id" not in data:
            raise ApiError("comment reply returned an unexpected response", body=data)
        return redact({k: v for k, v in data.items() if k != "payload"})

    def resolve(self, slug: str, thread: str) -> None:
        """Resolve a thread a person sent to Claude."""
        self._request(
            "POST",
            f"/api/frame/comments/{_q(slug)}/{_q(thread)}/session-resolve",
            {"resolved": True},
        )

    # -- database ----------------------------------------------------------

    def db(
        self,
        slug: str,
        verb: str,
        args: dict,
        as_level: str | None = None,
        require_version: bool = False,
    ) -> dict:
        """One artifact-database call: get, list, query, set, update, delete,
        str_replace, batch or whoami.

        `as_level` (view, interact, admin) acts with only that access level, to
        check what the page's rules allow such a viewer. `require_version` has
        the server refuse a write to an existing document that carries no
        ifVersion.
        """
        body: dict[str, Any] = {"slug": slug, "verb": verb, "args": args}
        if as_level:
            body["asLevel"] = as_level
        if require_version:
            body["requireVersion"] = True
        data = self._request("POST", "/api/frame/db/agent", body)
        if not isinstance(data, dict):
            raise ApiError("db returned an unexpected response", body=data)
        return redact(data)

    def profiles(self, slug: str, ids: list[str]) -> dict:
        """{id: {name?, guest?}} for the people (u_… ids) the artifact knows."""
        data = self._request("POST", f"/api/frame/user/profiles/{_q(slug)}", {"ids": ids})
        profiles = data.get("profiles") if isinstance(data, dict) else None
        if not isinstance(profiles, dict):
            raise ApiError("profiles returned an unexpected response", body=data)
        return redact(profiles)

    # -- assets ------------------------------------------------------------

    def assets(self, slug: str, after: str | None = None, limit: int = 50) -> dict:
        body: dict[str, Any] = {"limit": limit}
        if after:
            body["after"] = after
        return redact(self._request("POST", f"/api/frame/blob/{_q(slug)}/agent-list", body))

    def upload_asset(self, slug: str, data: bytes, content_type: str) -> dict:
        reply = self._request(
            "POST",
            f"/api/frame/blob/{_q(slug)}/agent-upload",
            raw=data,
            content_type=content_type,
        )
        if not isinstance(reply, dict) or "opaque_id" not in reply:
            raise ApiError("asset upload returned an unexpected response", body=reply)
        return redact(reply)

    def delete_asset(self, slug: str, asset_id: str) -> dict:
        return redact(
            self._request(
                "POST", f"/api/frame/blob/{_q(slug)}/{_q(asset_id)}/agent-delete", {}
            )
        )

    def copy_assets(self, slug: str, from_slug: str, ids: list[str]) -> dict:
        return redact(
            self._request(
                "POST",
                f"/api/frame/blob/{_q(slug)}/agent-copy",
                {"from": from_slug, "ids": ids},
            )
        )

    # -- types and contracts -----------------------------------------------

    def types(self, query: str | None = None) -> list[dict]:
        """Types this account can create from; `query` narrows to the best matches."""
        path = "/api/frame/types"
        if query:
            path += "?" + urllib.parse.urlencode({"q": query})
        data = self._request("GET", path)
        if isinstance(data, dict) and isinstance(data.get("types"), list):
            return redact(data["types"])
        raise ApiError("unexpected types response shape", body=data)

    def type_info(self, slug: str) -> dict:
        return redact(self._request("GET", f"/api/frame/types/{_q(slug)}"))

    def type_file(self, slug: str, path: str) -> dict:
        """A text file the type ships, e.g. its SKILL.md: {text, truncated, …}."""
        quoted = clean_path(path)
        if quoted is None:
            raise ApiError(f"{path!r} is not a clean relative path")
        return redact(self._request("GET", f"/api/frame/types/{_q(slug)}/files/{quoted}"))

    def type_instances(self, slug: str, limit: int = 50, rel: str = "all") -> list[dict]:
        """Artifacts made from a type; `rel` is mine, shared or all."""
        path = f"/api/frame/types/{_q(slug)}/instances?limit={int(limit)}"
        if rel != "all":
            path += f"&rel={_q(rel)}"
        data = self._request("GET", path)
        if isinstance(data, dict) and isinstance(data.get("instances"), list):
            return redact(data["instances"])
        raise ApiError("unexpected instances response shape", body=data)

    def create_from_type(self, type_slug: str, title: str | None, key: str) -> dict:
        """A new artifact made from a type. `key` makes a retried create idempotent."""
        body: dict[str, Any] = {"target_type_version": None}
        if title:
            body["title"] = title
        data = self._request(
            "POST",
            f"/api/frame/types/{_q(type_slug)}/create?idempotency_key={_q(key)}",
            body,
        )
        if not isinstance(data, dict) or not isinstance(data.get("slug"), str):
            raise ApiError("create returned an incomplete response", body=data)
        return redact(data)

    def contract(self, version: str = "latest") -> dict:
        return self._request("GET", f"/api/frame/contract/{_q(version)}")

    def contract_prompt(self, version: str) -> str:
        data = self._request("GET", f"/api/frame/contract/{_q(version)}/prompt")
        return data if isinstance(data, str) else json.dumps(data, indent=2)

    def contract_defs(self, version: str, name: str) -> str:
        """TypeScript definitions for one capability (or `claude`, for claude.use)."""
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", name):
            raise ApiError(f"{name!r} is not a capability name")
        data = self._request("GET", f"/api/frame/contract/{_q(version)}/{name}.d.ts")
        if not isinstance(data, str) or not data.strip():
            raise ApiError(f"contract {version} returned no definitions for {name}")
        return data

    def publish(
        self,
        page: Asset | None,
        extra: list[Asset] | None = None,
        removals: list[str] | None = None,
        slug: str | None = None,
        meta: dict | None = None,
        mode: str | None = None,
        base_version=None,
        force: bool = False,
        on_progress=lambda msg: None,
        copies: list[Copy] | None = None,
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

        # page is None only for a typed artifact, whose page is the type's.
        extra = list(extra or [])
        assets = ([page] if page else []) + extra
        manifest: dict[str, Any] = {}

        total = sum(a.wire_bytes() for a in assets)
        staged: list[Asset] = []

        # The server takes the page only as inline content, so only the
        # supporting files can be staged by hash.
        if total > INLINE_BUDGET and extra:
            on_progress(
                f"body is {total // 1024 // 1024} MB - using prepare/upload flow"
            )
            slug, staged = self._stage(extra, slug, on_progress)
        if copies:
            slug = self._copy(copies, slug, on_progress)

        staged_paths = {a.path for a in staged}
        for asset in assets:
            if asset.path in staged_paths:
                key, value = "sha256", asset.sha256
            else:
                key, value = "content", asset.wire()
            manifest[asset.path] = {key: value, "contentType": asset.content_type}
        for copy in copies or []:
            manifest[copy.path] = {"sha256": copy.sha256, "contentType": copy.content_type}
        for path in removals or []:
            manifest[path] = None

        # The page goes first, so it is always in the first request.
        entries = list(manifest.items())
        parts = [
            dict(entries[i : i + MANIFEST_MAX]) for i in range(0, len(entries), MANIFEST_MAX)
        ] or [{}]
        if len(parts) > 1:
            on_progress(
                f"{len(entries)} manifest entries - publishing in {len(parts)} requests "
                f"of at most {MANIFEST_MAX}"
            )
        result: dict = {}
        for index, part in enumerate(parts):
            body: dict[str, Any] = dict(meta or {})
            if index > 0:
                # Later parts patch onto the version the previous part made.
                body.pop("thumbnail", None)
                body.pop("thumbnail_dark", None)
                slug, mode, base_version, force = result["slug"], "patch", result.get("version"), False
            body["manifest"] = part
            body["mode"] = mode
            if slug:
                body["slug"] = slug
            if base_version is not None:
                body["baseVersion"] = base_version
            if force:
                body["force"] = True
            try:
                result = self._deploy(body, on_progress)
            except ApiError as exc:
                if index == 0:
                    raise
                raise type(exc)(
                    f"{exc} - only {index} of {len(parts)} requests went through for "
                    f"{ARTIFACT_URL.format(slug=result['slug'])}: version "
                    f"{result.get('version')} has part of the files, and the failed request "
                    "may have landed too. Check with `claude-artifact read "
                    f"{result['slug']}`, then publish again with --slug {result['slug']} "
                    "to send the rest",
                    exc.status,
                    exc.body,
                    exc.retry_after,
                ) from None
        return result

    def _deploy(self, body: dict, on_progress: Callable[[str], None]) -> dict:
        """POST /deploy/direct, retried the way Claude Code retries it.

        Every send carries one attempt id. A send that gets no answer is sent
        again once, marked as a re-send, so a publish that already landed is
        answered (`replayed`) rather than repeated; the server takes a re-send
        only for a named artifact. A 409 publish_settling (that earlier send
        is still being stored), a 503, and a 429 that isn't a plan cap are
        retried after the server's Retry-After.
        """
        attempt = str(uuid.uuid4())
        resend = False
        resent = settled = rate_retried = False
        settle_count = 0
        busy_attempts = 1
        while True:
            query = {"attempt_id": attempt}
            if resend:
                query["resend"] = "1"
            path = "/api/frame/deploy/direct?" + urllib.parse.urlencode(query)
            on_progress("POST /api/frame/deploy/direct" + (" (re-send)" if resend else ""))
            try:
                result = self._request("POST", path, body)
            except NetworkError as exc:
                if resent or not body.get("slug"):
                    where = (
                        f"`claude-artifact read {body['slug']}` shows whether it did"
                        if body.get("slug")
                        else "`claude-artifact list` shows it if it did"
                    )
                    raise NetworkError(
                        f"{exc} - the publish may or may not have landed; {where}"
                    ) from None
                resent = resend = True
                on_progress("no answer - re-sending once, marked as a re-send")
                continue
            except ApiError as exc:
                codes = _codes(exc.body)
                if exc.status == 409 and "publish_settling" in codes:
                    if settle_count >= _SETTLE_RESENDS:
                        raise ApiError(
                            "HTTP 409: an earlier send of this publish reached claude.ai and "
                            "is still being stored; check with `claude-artifact read` in a "
                            "moment before publishing again",
                            exc.status,
                            exc.body,
                        ) from None
                    settle_count += 1
                    settled = True
                    wait = min(exc.retry_after if exc.retry_after is not None else 2.0,
                               _SETTLE_WAIT_MAX)
                    on_progress(f"an earlier send is still being stored - re-sending in {wait:g}s")
                    time.sleep(wait)
                    continue
                if (
                    exc.status == 503
                    and busy_attempts < _BUSY_ATTEMPTS
                    and not _stranded(exc.body)
                ):
                    busy_attempts += 1
                    floor = 5.0 * (busy_attempts - 1)
                    wait = min(max(exc.retry_after or 0.0, floor), _BUSY_WAIT_MAX)
                    # The refused send may still have landed: re-send where allowed.
                    resend = resend or bool(body.get("slug"))
                    on_progress(f"HTTP 503 - retrying in {wait:g}s "
                                f"(attempt {busy_attempts} of {_BUSY_ATTEMPTS})")
                    time.sleep(wait)
                    continue
                if exc.status != 429:
                    raise
                cap = next((c for c in codes if c in _PLAN_CAPS), None)
                if cap:
                    detail = exc.body.get("message") if isinstance(exc.body, dict) else None
                    raise ApiError(
                        f"HTTP 429: {_PLAN_CAPS[cap]}" + (f" ({detail})" if detail else ""),
                        exc.status,
                        exc.body,
                    ) from None
                wait = exc.retry_after
                if not rate_retried and wait is not None and wait <= _RATE_WAIT_MAX:
                    rate_retried = True
                    on_progress(f"HTTP 429 - retrying in {wait:g}s")
                    time.sleep(wait)
                    continue
                raise
            if not isinstance(result, dict) or "slug" not in result:
                raise ApiError("deploy returned an incomplete response", body=result)
            if result.get("replayed") and (resent or settled):
                on_progress("the earlier send had landed; the server answered it")
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

        answered = prep.get("missing", [])
        if not isinstance(answered, list):
            raise ApiError("malformed preflight answer", body=prep)
        missing = [s for s in answered if s in by_sha]

        batch: list[Asset] = []
        size = 0
        for sha in missing:
            asset = by_sha[sha]
            cost = asset.wire_bytes()
            if batch and size + cost > UPLOAD_BATCH:
                self._upload(reserved, batch, on_progress)
                batch, size = [], 0
            batch.append(asset)
            size += cost
        if batch:
            self._upload(reserved, batch, on_progress)
        # Every hash the preflight didn't ask for is already stored, typically
        # the unchanged files of a republish. Reference those by hash too:
        # sending them inline puts a large republish over the deploy body cap
        # (HTTP 413) even though nothing new needs to travel.
        return reserved, list(assets)

    def _copy(self, copies: list[Copy], slug: str | None, on_progress) -> str:
        """Have the server copy files from other artifacts into this one."""
        if not slug:
            on_progress("POST /api/frame/deploy/prepare")
            prep = self._request(
                "POST", "/api/frame/deploy/prepare", {"shas": [c.sha256 for c in copies]}
            )
            if not isinstance(prep, dict) or not isinstance(prep.get("slug"), str):
                raise ApiError("malformed preflight answer", body=prep)
            slug = prep["slug"]
        on_progress(f"POST /api/frame/upload ({len(copies)} copied file(s))")
        self._request(
            "POST",
            "/api/frame/upload",
            {
                "slug": slug,
                "files": [
                    {
                        "path": c.path,
                        "from": {"slug": c.from_slug, "path": c.from_path, "ver": c.from_ver},
                    }
                    for c in copies
                ],
            },
        )
        return slug

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
        "this publish was made against. For a directory, `claude-artifact pull SLUG DIR` "
        "merges it in; for a page, compare with `claude-artifact status` and merge by "
        "hand. Then publish again (a page with --base-version set to the live version), "
        "or pass --force to discard it."
    )


def _retry_after(headers: Any) -> float | None:
    """Retry-After in seconds (delta form only), or None."""
    try:
        value = headers.get("Retry-After") if headers is not None else None
        return max(0.0, float(value)) if value is not None else None
    except (TypeError, ValueError):
        return None


def _codes(body: Any) -> set[str]:
    """Every machine-readable code an error body carries: reason, error, error.code."""
    found: set[str] = set()
    if isinstance(body, dict):
        for key in ("reason", "error", "code"):
            value = body.get(key)
            if isinstance(value, str) and re.fullmatch(r"[a-z][a-z0-9_.-]{0,63}", value):
                found.add(value)
        err = body.get("error")
        if isinstance(err, dict) and isinstance(err.get("code"), str):
            found.add(err["code"])
    return found


def _stranded(body: Any) -> bool:
    """A 503 naming the artifact: the publish was cut off partway; don't resend blindly."""
    return isinstance(body, dict) and isinstance(body.get("slug"), str)


def error_code(body: Any) -> str | None:
    """The machine-readable code of an error body, e.g. `not_declared`."""
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict) and isinstance(err.get("code"), str):
            return err["code"]
        if isinstance(body.get("code"), str):
            return body["code"]
    return None


def _describe(status: int, body: Any) -> str:
    detail = ""
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            # {"error": {"code", "message", "reason"?}}: db, assets.
            parts = [err.get("code"), err.get("message"), err.get("reason")]
            detail = " - ".join(str(p) for p in parts if p)
        else:
            detail = body.get("message") or err or json.dumps(body)[:400]
            if body.get("code") and isinstance(detail, str):
                detail = f"{body['code']} - {detail}"
    elif isinstance(body, str):
        detail = body.strip()[:400]
    # Generic hints only when the server said nothing more specific.
    hint = "" if detail else {
        403: " (the account may not have Artifacts enabled)",
        404: " (no such artifact, or it isn't yours to update)",
        413: " (payload too large)",
        429: " (rate limited or daily publish cap reached)",
    }.get(status, "")
    return f"HTTP {status}{hint}: {detail}".rstrip(": ")
