"""Minimal client for the Claude Artifacts ("frame") API.

Protocol reverse-engineered from the Claude Code CLI bundle (v2.1.273, checked
against v2.1.287).

    POST {base}/api/frame/deploy/direct     publish
    POST {base}/api/frame/deploy/prepare    content-hash preflight (large publishes)
    POST {base}/api/frame/upload            stage blobs the preflight asked for
    GET  {base}/api/frame/{slug}?via=model_read   metadata + file manifest
    GET  {base}/api/frame/read/{slug}       ownership / sharing status
    GET  {base}/api/frame/frames?limit=N    list artifacts

    GET  https://{slug}.frame.claudeusercontent.com/_f/{ver}/_src/{path}?__frame_t={assetToken}
         published source bytes; /_f/{ver}/{path} is the served copy

base defaults to https://api.anthropic.com. Auth is the claude.ai OAuth access
token as a bearer, with the anthropic-beta: oauth-2025-04-20 opt-in. The content
host takes only the boot's short-lived assetToken, never the OAuth token.
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
CONTENT_HOST = "https://{slug}.frame.claudeusercontent.com"
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

# The served copy of the page carries the frame runtime, and can carry a
# trailing comments script; the stored source has neither.
_RUNTIME_RE = re.compile(rb"<!-- frame-runtime -->.*?<!-- /frame-runtime -->", re.DOTALL)
_COMMENTS_TAG = b'<script type="application/json" id="__frame_comments__">'


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

        Paths, sizes, sha256 and content types, with tokens redacted. The
        bytes themselves come from files().
        """
        boot = redact(self._boot(slug))
        quoted = urllib.parse.quote(slug)
        try:
            boot["_access"] = redact(self._request("GET", f"/api/frame/read/{quoted}"))
        except ApiError:
            pass
        return boot

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
        "this publish was made against. For a directory, `claude-artifact pull SLUG DIR` "
        "merges it in; for a page, compare with `claude-artifact status` and merge by "
        "hand. Then publish again (a page with --base-version set to the live version), "
        "or pass --force to discard it."
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
