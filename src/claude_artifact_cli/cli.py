"""Command-line front end."""

from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from . import __version__, api, auth, commands, update

TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)

# Written into a published directory so the next publish of it updates the same
# artifact and can detect a version someone else published in between.
STATE_FILE = ".artifact.json"

EXIT_CONFLICT = 3


def _eprint(msg: str) -> None:
    print(msg, file=sys.stderr)


def _client(args) -> api.FrameClient:
    return api.FrameClient(auth.get_token(args.token), base=args.base)


def _title_from_html(text: str, fallback: str) -> str:
    match = TITLE_RE.search(text)
    if match:
        title = re.sub(r"\s+", " ", match.group(1)).strip()
        if title:
            return title[:200]
    return fallback


def _load_assets(specs: list[str], root: str | None) -> list[api.Asset]:
    """Each spec is 'published/path=source/path' or just 'path' for both."""
    base = Path(root) if root else Path.cwd()
    assets = []
    for spec in specs:
        published, _, source = spec.partition("=")
        src = Path(source or published)
        if not src.is_absolute():
            src = base / src
        if not src.is_file():
            raise SystemExit(f"error: no such file: {src}")
        assets.append(
            api.Asset(
                path=published.lstrip("/"),
                data=src.read_bytes(),
                content_type=api.guess_content_type(published),
            )
        )
    return assets


def _content_types(specs: list[str] | None) -> dict[str, str]:
    """--content-type PUB=TYPE flags: published path -> content type."""
    types = {}
    for spec in specs or []:
        path, sep, value = spec.partition("=")
        value = value.split(";")[0].strip().lower()
        if not sep or not path or not re.fullmatch(r"[a-z0-9.+-]+/[a-z0-9.+-]+", value):
            raise SystemExit(f"error: --content-type takes PUB=TYPE, e.g. data.bin=text/plain; got {spec!r}")
        types[path.lstrip("/")] = value
    return types


def _apply_content_types(assets: list[api.Asset], types: dict[str, str]) -> None:
    unknown = sorted(set(types) - {a.path for a in assets})
    if unknown:
        raise SystemExit(f"error: --content-type names no file being published: {', '.join(unknown)}")
    for asset in assets:
        if asset.path in types:
            asset.content_type = types[asset.path]


def _warn_unserved(assets: list[api.Asset]) -> None:
    """Claude Code refuses to publish types artifacts don't serve; say so here."""
    for asset in assets:
        if asset.content_type not in api.SERVED_TYPES:
            _eprint(
                f"warning: {asset.path} goes as {asset.content_type}, which artifacts don't "
                "serve; a page may not be able to load it. A source, config or log file goes "
                "as text/plain (--content-type PATH=text/plain, or a .txt name)."
            )


def _apply_edits(page: api.Asset, edits: list[list[str]], where: Path) -> api.Asset:
    """--edit OLD NEW pairs, applied in order; each OLD must occur exactly once."""
    try:
        text = page.data.decode("utf-8")
    except UnicodeDecodeError:
        raise SystemExit(f"error: --edit needs a UTF-8 page; {where} isn't") from None
    for i, (old, new) in enumerate(edits, 1):
        if not old:
            raise SystemExit(f"error: --edit {i}: the text to replace is empty")
        first = text.find(old)
        # A second match may overlap the first ("aa" in "aaa"), which count() misses.
        count = 0 if first < 0 else 1 + (text.find(old, first + 1) >= 0)
        if count != 1:
            shown = old if len(old) <= 60 else old[:57] + "..."
            raise SystemExit(
                f"error: --edit {i}: {shown!r} "
                f"{'is not in' if count == 0 else 'occurs more than once in'} {where} "
                "(after the edits before it); it must occur exactly once. Nothing was published"
            )
        text = text.replace(old, new, 1)
    return api.Asset(path=page.path, data=text.encode("utf-8"), content_type=page.content_type)


def _dir_files(root: Path) -> dict[str, Path]:
    """Published path -> file for everything under root, skipping dotfiles.

    Symlinks are refused rather than followed, so a link can't publish a file
    from outside the directory.
    """
    files = {}
    for dirpath, dirnames, filenames in os.walk(root):
        here = Path(dirpath)
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        for name in [d for d in dirnames if (here / d).is_symlink()] + [
            f for f in filenames if not f.startswith(".")
        ]:
            path = here / name
            if path.is_symlink():
                raise SystemExit(f"error: {path} is a symlink; copy the file in instead")
            files[path.relative_to(root).as_posix()] = path
    return dict(sorted(files.items()))


def _load_state(root: Path) -> dict:
    try:
        state = json.loads((root / STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return state if isinstance(state, dict) else {}


def _save_state(root: Path, state: dict) -> None:
    (root / STATE_FILE).write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def _target_slug(args) -> str | None:
    value = args.url or args.slug
    return api.slug_from(value) if value else None


def _is_dot_path(path: str) -> bool:
    """Directories never hold these: publish skips them, so pull and status do too.

    An unclean path (such as one with "..") is not a dot path; it is refused.
    """
    return api.clean_path(path) is not None and any(
        part.startswith(".") for part in path.split("/")
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


_ON_WINDOWS = os.name == "nt"
_WINDOWS_RESERVED = re.compile(r"^(CON|PRN|AUX|NUL|COM[0-9]|LPT[0-9])(\..*)?$", re.IGNORECASE)


def _unsafe_on_windows(part: str) -> bool:
    """Names Windows would resolve somewhere else: drives, streams, devices, aliases."""
    return (
        ":" in part
        or bool(re.search(r'[<>"|?*]', part))
        or bool(_WINDOWS_RESERVED.match(part))
        or part.endswith((".", " "))
    )


def _destination(root: Path, published: str) -> Path:
    """Where a published path lands under root, refusing anything that escapes it.

    Manifest paths come from the server and, for a shared artifact, from other
    people, so they're checked before anything is written.
    """
    parts = published.split("/")
    if api.clean_path(published) is None or (
        _ON_WINDOWS and any(_unsafe_on_windows(p) for p in parts)
    ):
        raise SystemExit(f"error: refusing to write unsafe path {published!r}")
    dest = root
    for part in parts:
        dest = dest / part
        if dest.is_symlink():
            raise SystemExit(f"error: {dest} is a symlink; refusing to write through it")
    if not dest.resolve().is_relative_to(root.resolve()):
        raise SystemExit(f"error: refusing to write {published!r} outside {root}")
    return dest


def _write(root: Path, published: str, data: bytes) -> Path:
    dest = _destination(root, published)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    return dest


def _capabilities(args) -> dict | None:
    """--capability NAME[=JSON] flags as a declaration; {} for --clear-capabilities."""
    if args.clear_capabilities:
        if args.capability:
            raise SystemExit("error: --clear-capabilities and --capability don't combine")
        return {}
    if not args.capability:
        return None
    caps: dict = {}
    for spec in args.capability:
        name, sep, raw = spec.partition("=")
        name = name.strip()
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", name):
            raise SystemExit(f"error: {name!r} is not a capability name")
        try:
            caps[name] = json.loads(raw) if sep else {}
        except ValueError as exc:
            raise SystemExit(f"error: --capability {name}: invalid JSON: {exc}") from None
    return caps


def _contract_fields(client: api.FrameClient, slug: str | None, args) -> dict:
    """The deploy fields for --contract and --capability.

    A declaration needs a contract: the one passed, else the artifact's stored
    one, else the latest. Names are checked against that contract's roster.
    """
    caps = _capabilities(args)
    contract = args.contract
    if caps is None and contract is None:
        return {}
    if contract is None and slug:
        with contextlib.suppress(api.ApiError):
            stored = client.access(slug).get("contract")
            if isinstance(stored, str) and stored != "0.0.0":
                contract = stored
    roster = client.contract(contract or "latest")
    contract = roster.get("version") or contract
    known = set(roster.get("capabilities") or [])
    unknown = sorted(set(caps or {}) - known)
    if unknown:
        raise SystemExit(
            f"error: contract {contract} has no capability {', '.join(unknown)}; "
            f"it supports: {', '.join(sorted(known)) or '(none)'}"
        )
    fields: dict = {"contract": contract}
    if caps is not None:
        fields["capabilities"] = caps
    return fields


_DOWNLOAD_LINK = re.compile(rb"<a\b[^>]*\sdownload\b", re.IGNORECASE)


def _warn_download_links(client: api.FrameClient, slug: str | None, page, meta: dict) -> None:
    """Warn when a page offers files through <a download> without the downloads capability.

    claude.ai blocks a plain download link inside an artifact ("File downloads are
    not available for this artifact"); a page saves files through the downloads
    capability instead.
    """
    if page is None or not _DOWNLOAD_LINK.search(page.data):
        return
    caps = meta.get("capabilities")
    if caps is None and slug:
        with contextlib.suppress(api.ApiError):
            caps = client.access(slug).get("capabilities")
    if isinstance(caps, dict) and "downloads" in caps:
        return
    _eprint(
        "warning: the page has <a download> links, which claude.ai blocks inside an "
        "artifact (\"File downloads are not available for this artifact\"). Publish with "
        "--capability downloads and save the file from the click handler with "
        "(await claude.use(\"downloads\")).save({filename, data})."
    )


def _copies(client: api.FrameClient, specs: list[list[str]]) -> list[api.Copy]:
    """--copy SOURCE PUB[=SRC]: files the server copies from another artifact."""
    copies = []
    readers: dict[str, api.FileReader] = {}
    for source, spec in specs:
        published, _, src = spec.partition("=")
        published, src = published.lstrip("/"), (src or published).lstrip("/")
        if api.clean_path(published) is None:
            raise SystemExit(f"error: {published!r} is not a clean relative path")
        slug = api.slug_from(source)
        reader = readers.get(slug) or readers.setdefault(slug, client.files(slug))
        entry = reader.files.get(src)
        if entry is None:
            raise SystemExit(f"error: {source} has no file {src!r} in version {reader.version}")
        digest = entry.get("sha256")
        if not isinstance(digest, str):
            digest = hashlib.sha256(reader.fetch(src)).hexdigest()
        content_type = entry.get("contentType") or api.guess_content_type(src)
        copies.append(api.Copy(published, slug, src, reader.version, digest, content_type))
    return copies


# -- commands --------------------------------------------------------------


def cmd_publish(args) -> int:
    target = Path(args.file)
    slug = _target_slug(args)
    base_version = args.base_version
    mode = args.mode
    state_root = None
    state: dict = {}
    typed = False

    if target.is_dir():
        if args.file_spec or args.root:
            raise SystemExit(
                "error: --file and --root don't apply when publishing a directory; "
                "put the files in it"
            )
        files = _dir_files(target)
        state = _load_state(target)
        state_slug = state.get("slug")
        if slug is None and isinstance(state_slug, str):
            slug = state_slug
        same_artifact = slug is not None and slug == state_slug
        if base_version is None and same_artifact:
            base_version = state.get("version")
        typed = same_artifact and state.get("typed") is True
        known = state.get("files") if same_artifact else None
        if not isinstance(known, dict):
            known = None
        if typed and mode == "replace":
            raise SystemExit(
                "error: an artifact made from a type can only be patched; its type's "
                "files aren't in the directory"
            )
        if typed and "index.html" in files:
            raise SystemExit(
                f"error: {target}/index.html belongs to the artifact's type and can't be "
                "published; remove it"
            )
        if not typed and "index.html" not in files:
            raise SystemExit(f"error: no index.html in {target}")
        if mode == "patch" or (mode is None and (typed or known is not None)):
            # The directory manages the files .artifact.json lists: patch those,
            # and remove the ones deleted locally since the last pull or
            # publish. Files it never had (dotfiles, an Artifact type's fixed
            # files) are left alone.
            mode = "patch"
            removals = sorted((set(known or {}) - set(files)) | set(args.remove or []))
        else:
            # A first publish of the directory: it is the whole artifact.
            if mode is None and not args.remove:
                mode = "replace"
            removals = list(args.remove or [])
        page_file = files.pop("index.html", None)
        raw = page_file.read_bytes() if page_file else b""
        page = (
            api.Asset(path="index.html", data=raw, content_type="text/html") if page_file else None
        )
        extra = [
            api.Asset(path=pub, data=src.read_bytes(), content_type=api.guess_content_type(pub))
            for pub, src in files.items()
        ]
        fallback_title = target.resolve().name
        state_root = target
    elif target.is_file():
        raw = target.read_bytes()
        content_type = api.guess_content_type(target.name)
        if content_type not in ("text/html", "text/markdown"):
            content_type = "text/html"
        page = api.Asset(path="index.html", data=raw, content_type=content_type)
        page_file = target
        extra = _load_assets(args.file_spec or [], args.root)
        removals = list(args.remove or [])
        fallback_title = target.stem
    else:
        raise SystemExit(f"error: no such file or directory: {target}")

    _apply_content_types(extra, _content_types(args.content_type))
    _warn_unserved(extra)
    if args.edit:
        if page is None or page_file is None:
            raise SystemExit("error: --edit changes the page, and this publish has none")
        page = _apply_edits(page, args.edit, page_file)
        raw = page.data

    meta = {}
    if page is None and isinstance(state.get("title"), str):
        fallback_title = state["title"]
    meta["title"] = args.title or _title_from_html(raw.decode("utf-8", "replace"), fallback_title)
    if args.favicon:
        meta["favicon"] = args.favicon
    elif not slug:
        meta["favicon"] = "📄"  # required on a first publish
    for key, value in (
        ("description", args.description),
        ("icon", args.icon),
        ("label", args.label),
        ("note", args.note),
    ):
        if value:
            meta[key] = value
    for key, path in (("thumbnail", args.thumbnail), ("thumbnail_dark", args.thumbnail_dark)):
        if path:
            if not Path(path).is_file():
                raise SystemExit(f"error: no such file: {path}")
            meta[key] = base64.b64encode(Path(path).read_bytes()).decode("ascii")

    progress = (lambda m: None) if args.quiet else (lambda m: _eprint(f"  {m}"))

    client = _client(args)
    meta.update(_contract_fields(client, slug, args))
    _warn_download_links(client, slug, page, meta)
    copies = _copies(client, args.copy or [])
    result = client.publish(
        page=page,
        extra=extra,
        removals=removals,
        slug=slug,
        meta=meta,
        mode=mode,
        base_version=base_version,
        force=args.force,
        on_progress=progress,
        copies=copies,
    )
    url = api.ARTIFACT_URL.format(slug=result["slug"])

    if args.edit:
        # Published with the edits; the file keeps them so it stays the source.
        try:
            page_file.write_bytes(page.data)
        except OSError as exc:
            _eprint(f"warning: published, but saving the edits to {page_file} failed: {exc}")
    if args.pin:
        try:
            client.pin(result["slug"], True)
        except api.ApiError as exc:
            _eprint(f"warning: published, but pinning failed: {exc}")

    if state_root is not None:
        new_state = {
            "slug": result["slug"],
            "url": url,
            "version": result.get("version"),
            "title": meta["title"],
            "files": {a.path: a.sha256 for a in ([page] if page else []) + extra},
        }
        if typed:
            new_state["typed"] = True
        _save_state(state_root, new_state)

    if args.json:
        print(json.dumps({"url": url, **result}, indent=2))
    else:
        print(url)
        if result.get("version") is not None and not args.quiet:
            _eprint(f"  version {result['version']}")
    return 0


def _wanted(row: dict, args) -> bool:
    """The `list` filters. rel=favorited serves browser sessions only, so pins are here."""
    if row.get("softDeleted") or row.get("soft_deleted"):
        return False
    if args.pinned and not row.get("starred"):
        return False
    if args.type:
        wanted = args.type.strip().lower()
        kind = (row.get("type_title") or "").lower()
        return kind == wanted or (wanted == "page" and not kind)
    return True


def _list_rows(client: api.FrameClient, args) -> tuple[list[dict], str | None, bool]:
    """(rows, next page token, cut short by --limit) for `list`.

    From the paged gallery, filtered page by page so --limit counts printed rows;
    the unpaged listing if the gallery isn't served.
    """
    filtering = bool(args.pinned or args.type)
    rows: list[dict] = []
    token = args.page_token
    seen: set[str] = set()
    try:
        while len(rows) < args.limit:
            size = 200 if filtering else min(200, args.limit - len(rows))
            page, token = client.list_artifacts(args.scope, size, token)
            rows.extend(f for f in page if _wanted(f, args))
            if not token or len(rows) >= args.limit:
                break
            if token in seen:
                raise api.ApiError("the listing repeated a page token; stopping")
            seen.add(token)
    except api.ApiError as exc:
        if exc.status not in (400, 403, 404) or args.page_token or rows:
            raise
        rows, token = client.list_frames(limit=args.limit), None
        if args.scope != "all":
            rows = [f for f in rows if (f.get("rel") or "mine") == args.scope]
        rows = [f for f in rows if _wanted(f, args)]
    if len(rows) > args.limit:
        # The rest of this page isn't reachable through the token: say so instead.
        return rows[: args.limit], None, True
    return rows, token, False


def cmd_list(args) -> int:
    frames, token, cut = _list_rows(_client(args), args)

    if args.json:
        print(json.dumps(frames, indent=2))
    else:
        if not frames:
            print("No artifacts.")
        for f in frames:
            slug = f.get("slug", "?")
            title = (f.get("title") or "(untitled)").strip()
            updated = (f.get("updatedAt") or "")[:10]
            kind = f.get("type_title") or ""
            print(f"{slug:<38} {updated:<11} {kind:<10} {title}".rstrip())
    if token:
        _eprint(f"  more: --page-token {token}")
    elif cut:
        _eprint(f"  more: raise --limit (now {args.limit})")
    return 0


def cmd_read(args) -> int:
    slug = api.slug_from(args.slug)
    if args.path:
        return _read_files(args, slug)
    if args.out_dir:
        raise SystemExit("error: --out-dir goes with --path")
    data = _client(args).read(slug)

    if args.out:
        Path(args.out).write_text(json.dumps(data, indent=2), encoding="utf-8")
        _eprint(f"wrote metadata to {args.out}")

    if args.json:
        print(json.dumps(data, indent=2))
        return 0

    print(f"{data.get('title') or '(untitled)'}  {data.get('favicon') or ''}".strip())
    print(f"  url        {api.ARTIFACT_URL.format(slug=slug)}")
    print(f"  version    {data.get('ver')}")
    print(f"  updated    {data.get('updated_at')}")
    access = data.get("_access") or {}
    role = (data.get("perm") or {}).get("role")
    print(f"  role       {role or '?'}"
          f"{'  (public)' if access.get('public') else ''}")
    if access.get("contract") and access.get("contract") != "0.0.0":
        print(f"  contract   {access['contract']}")
    caps = access.get("capabilities")
    if isinstance(caps, dict) and caps:
        print(f"  caps       {', '.join(sorted(caps))}")

    files = data.get("files") or []
    if files:
        print(f"  files      {len(files)}")
        for f in sorted(files, key=lambda x: x.get("path", "")):
            size = f.get("size")
            size_s = f"{size:>9,}" if isinstance(size, int) else " " * 9
            print(f"    {size_s}  {f.get('contentType',''):<26} {f.get('path','')}")
    return 0


def _read_files(args, slug: str) -> int:
    if args.json or args.out:
        raise SystemExit("error: --path writes file bytes; it doesn't combine with --json or -o")
    if len(args.path) > 1 and not args.out_dir:
        raise SystemExit("error: more than one --path needs --out-dir")
    reader = _client(args).files(slug)
    for path in args.path:
        data = reader.fetch(path)
        if args.out_dir:
            print(_write(Path(args.out_dir), path, data))
        else:
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()
    return 0


@dataclass
class _Plan:
    write: list[str] = field(default_factory=list)
    delete: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)


def _merge_plan(
    local: dict[str, Path], known: dict, remote: dict[str, str], force: bool
) -> _Plan:
    """Three-way merge by file: base hashes from .artifact.json, local, live.

    The live version wins where only it changed, a local edit is kept where only
    it changed, and a file changed on both sides is a conflict (or, with force,
    takes the live version).
    """
    plan = _Plan()
    for path in sorted(set(remote) | set(known)):
        base = known.get(path)
        theirs = remote.get(path)
        mine = _sha256(local[path]) if path in local else None
        if mine == theirs:
            continue
        if mine == base:
            # Unchanged here: follow the live version, including deletions.
            (plan.write if theirs else plan.delete).append(path)
        elif theirs == base:
            plan.kept.append(path)
        elif force:
            (plan.write if theirs else plan.delete).append(path)
        else:
            plan.conflicts.append(path)
    return plan


def cmd_pull(args) -> int:
    slug = api.slug_from(args.slug)
    root = Path(args.dir or slug)
    if root.exists() and not root.is_dir():
        raise SystemExit(f"error: {root} exists and is not a directory")
    reader = _client(args).files(slug)
    typed = any(reader.type_owned(p) for p in reader.files)
    dotted = sorted(p for p in reader.files if not reader.type_owned(p) and _is_dot_path(p))
    own = sorted(p for p in reader.files if not reader.type_owned(p) and not _is_dot_path(p))

    state = _load_state(root) if root.is_dir() else {}
    refreshing = state.get("slug") == slug
    known = state.get("files") if refreshing and isinstance(state.get("files"), dict) else {}
    local = _dir_files(root) if root.is_dir() else {}
    if local and not refreshing and not args.force:
        raise SystemExit(
            f"error: {root} is not empty and was not pulled from or published to this "
            "artifact; pick another directory or pass --force"
        )

    fetched = {path: reader.fetch(path) for path in own}
    remote_sha = {path: hashlib.sha256(data).hexdigest() for path, data in fetched.items()}
    plan = _merge_plan(local, known, remote_sha, force=args.force)
    if plan.conflicts:
        listing = "\n".join(f"  {p}" for p in plan.conflicts)
        raise SystemExit(
            f"error: changed both locally and in the live version:\n{listing}\n"
            "nothing was written. Copy your versions of these files elsewhere, pull again "
            "with --force (it takes the live version of just these files), reapply your "
            "changes, then publish"
        )

    root.mkdir(parents=True, exist_ok=True)
    for path in plan.write:
        _write(root, path, fetched[path])
    for path in plan.delete:
        local[path].unlink()
        parent = local[path].parent
        while parent != root and not any(parent.iterdir()):
            parent.rmdir()
            parent = parent.parent

    state = {
        "slug": slug,
        "url": api.ARTIFACT_URL.format(slug=slug),
        "version": reader.version,
        "title": reader.meta.get("title"),
        "files": remote_sha,
    }
    skipped = len(reader.files) - len(own) - len(dotted)
    if typed:
        state["typed"] = True
    _save_state(root, state)

    print(root)
    if not args.quiet:
        _eprint(f"  version {reader.version}: wrote {len(plan.write)} file(s)")
        if plan.delete:
            _eprint(f"  removed {len(plan.delete)} file(s) no longer published")
        if plan.kept:
            _eprint(f"  kept {len(plan.kept)} local edit(s): {', '.join(plan.kept)}")
        if skipped:
            _eprint(f"  left out {skipped} file(s) supplied by the artifact's type")
        if dotted:
            _eprint(f"  left out {len(dotted)} dot-path file(s), which directories don't hold")
    return 0


def _compare(
    local: dict[str, Path], remote: dict[str, str | None], fetch_sha
) -> list[tuple[str, str]]:
    """(state, path) for every path on either side, sorted by path.

    A manifest entry without a sha256 (older artifacts) is hashed by fetching it.
    """
    rows = []
    for path in sorted(set(local) | set(remote)):
        if path not in remote:
            rows.append(("local-only", path))
        elif path not in local:
            rows.append(("remote-only", path))
        else:
            remote_sha = remote[path] or fetch_sha(path)
            rows.append(("same" if _sha256(local[path]) == remote_sha else "changed", path))
    return rows


def cmd_status(args) -> int:
    target = Path(args.path)
    slug = _target_slug(args)
    state: dict = {}

    if target.is_dir():
        state = _load_state(target)
        local = _dir_files(target)
    elif target.is_file():
        # A single page stands in for index.html; other remote files have no
        # local counterpart to compare against.
        local = {"index.html": target}
    else:
        raise SystemExit(f"error: no such file or directory: {target}")

    if slug is None:
        slug = state.get("slug") if isinstance(state.get("slug"), str) else None
    if slug is None:
        raise SystemExit(
            f"error: no artifact to compare with - pass --slug/--url, or publish {target} first"
        )

    client = _client(args)
    data = client.read(slug)
    live = data.get("ver")
    base = state.get("version") if state.get("slug") == slug else None
    # Files an Artifact type supplies, and dot paths, are never in a directory.
    remote = {
        f["path"]: f.get("sha256")
        for f in data.get("files") or []
        if isinstance(f, dict)
        and isinstance(f.get("path"), str)
        and not isinstance(f.get("src"), dict)
        and not _is_dot_path(f["path"])
    }
    reader = None

    def fetch_sha(path: str) -> str:
        nonlocal reader
        if reader is None:
            reader = client.files(slug)
        return hashlib.sha256(reader.fetch(path)).hexdigest()

    rows = _compare(local, remote, fetch_sha)
    if target.is_file():
        rows = [r for r in rows if r[0] != "remote-only"]
    behind = base is not None and base != live
    in_sync = not behind and all(r[0] == "same" for r in rows)

    if args.json:
        print(
            json.dumps(
                {
                    "slug": slug,
                    "url": api.ARTIFACT_URL.format(slug=slug),
                    "live": live,
                    "base": base,
                    "behind": behind,
                    "files": [{"state": s, "path": p} for s, p in rows],
                },
                indent=2,
            )
        )
        return 0

    print(api.ARTIFACT_URL.format(slug=slug))
    print(f"  live version  {live}")
    if base is not None:
        note = "  (up to date)" if base == live else "  (someone published since - merge first)"
        print(f"  base version  {base}{note}")
    for state_name, path in rows:
        print(f"  {state_name:<12}  {path}")
    if not in_sync:
        if target.is_dir() and state.get("slug") == slug:
            _eprint(f"  merge the live version in: claude-artifact pull {slug} {target}")
        else:
            _eprint(f"  live copy: claude-artifact pull {slug} <another-dir>")
    return 0


def cmd_whoami(args) -> int:
    """Confirm the token works, without printing it."""
    token = auth.get_token(args.token)
    _eprint(f"token found ({len(token)} chars, ends …{token[-6:]})")
    frames = api.FrameClient(token, base=args.base).list_frames(limit=1)
    _eprint(f"authenticated against {args.base} - listing returned {len(frames)} row(s)")
    return 0


# -- parser ----------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="claude-artifact",
        description="Publish Claude Artifacts from the command line, "
        "using the Claude Code login already on this machine.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--token", help="bearer token (default: read from Keychain)")
    parser.add_argument("--base", default=api.DEFAULT_BASE, help=argparse.SUPPRESS)
    parser.add_argument("--json", action="store_true", help="raw JSON output")
    sub = parser.add_subparsers(dest="command", required=True)

    # Accept the global flags after the subcommand too. SUPPRESS keeps the
    # subparser from clobbering a value given before it.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--token", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    common.add_argument(
        "--json", action="store_true", default=argparse.SUPPRESS, help="raw JSON output"
    )

    p = sub.add_parser("publish", parents=[common], help="publish or update an artifact")
    p.add_argument(
        "file",
        help="the HTML page to publish, or a directory with an index.html "
        f"(its slug and version are kept in {STATE_FILE})",
    )
    p.add_argument("--slug", help="update this existing artifact")
    p.add_argument("--url", help="update the artifact at this claude.ai URL")
    p.add_argument("--title", help="defaults to the page's <title>")
    p.add_argument("--favicon", help="emoji for the browser tab")
    p.add_argument("--description", help="one-sentence subtitle")
    p.add_argument("--icon", help="one generic word, e.g. chart")
    p.add_argument("--label", help="short name for this publish")
    p.add_argument(
        "--file",
        dest="file_spec",
        action="append",
        metavar="PUB[=SRC]",
        help="supporting file; repeatable",
    )
    p.add_argument("--root", help="base directory for --file sources")
    p.add_argument(
        "--remove", action="append", metavar="PATH", help="delete a published file"
    )
    p.add_argument(
        "--mode",
        choices=["replace", "patch"],
        help="patch keeps published files you leave out; replace drops them "
        "(default: patch for a page update, replace for a new artifact or a directory)",
    )
    p.add_argument(
        "--base-version",
        help="version this publish was made against; a newer live version is refused",
    )
    p.add_argument("--force", action="store_true", help="overwrite a newer version")
    p.add_argument(
        "--capability",
        action="append",
        metavar="NAME[=JSON]",
        help="declare a runtime capability, e.g. db, user, assets, 'downloads=true'; "
        "repeatable; replaces the stored declaration (see `claude-artifact contract --prompt`)",
    )
    p.add_argument(
        "--clear-capabilities", action="store_true", help="drop every declared capability"
    )
    p.add_argument(
        "--contract",
        metavar="VERSION",
        help="runtime contract to pin, or 'latest' to upgrade (default: keep the stored one)",
    )
    p.add_argument(
        "--copy",
        nargs=2,
        action="append",
        metavar=("SOURCE", "PUB[=SRC]"),
        help="have the server copy a published file from another artifact; repeatable",
    )
    p.add_argument(
        "--content-type",
        action="append",
        metavar="PUB=TYPE",
        help="content type for a supporting file the extension doesn't give; repeatable",
    )
    p.add_argument(
        "--edit",
        nargs=2,
        action="append",
        metavar=("OLD", "NEW"),
        help="replace OLD (which must occur exactly once) with NEW in the page before "
        "publishing, and save the page with the change; repeatable, applied in order",
    )
    p.add_argument("--pin", action="store_true", help="pin the artifact once it is published")
    p.add_argument("--note", help="note recorded with this version")
    p.add_argument("--thumbnail", metavar="IMAGE", help="gallery thumbnail image")
    p.add_argument("--thumbnail-dark", metavar="IMAGE", help="dark-mode gallery thumbnail")
    p.add_argument("-q", "--quiet", action="store_true")
    p.set_defaults(func=cmd_publish)

    p = sub.add_parser("list", parents=[common], help="list your artifacts")
    p.add_argument("--limit", type=int, default=200, help="most rows to print (default 200)")
    p.add_argument("--scope", choices=["mine", "shared", "all"], default="mine")
    p.add_argument("--pinned", action="store_true", help="only pinned artifacts")
    p.add_argument(
        "--type",
        metavar="TITLE",
        help="only artifacts made from this type (Docs, Slides, ...), or 'page' for plain pages",
    )
    p.add_argument("--page-token", help="continue a listing that printed `more: --page-token`")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("read", parents=[common], help="metadata, manifest, or a file's bytes")
    p.add_argument("slug", help="slug or full artifact URL")
    p.add_argument("-o", "--out", help="write the metadata JSON to this file")
    p.add_argument(
        "--path",
        action="append",
        metavar="PATH",
        help="print this published file's bytes instead of metadata; repeatable with --out-dir",
    )
    p.add_argument("--out-dir", metavar="DIR", help="save --path files under DIR")
    p.set_defaults(func=cmd_read)

    p = sub.add_parser(
        "pull", parents=[common], help="download an artifact's files into a directory"
    )
    p.add_argument("slug", help="slug or full artifact URL")
    p.add_argument("dir", nargs="?", help="target directory (default: the slug)")
    p.add_argument(
        "--force", action="store_true", help="overwrite local edits or a non-empty directory"
    )
    p.add_argument("-q", "--quiet", action="store_true")
    p.set_defaults(func=cmd_pull)

    p = sub.add_parser(
        "status", parents=[common], help="compare local files with the published version"
    )
    p.add_argument(
        "path", nargs="?", default=".", help="a published directory or page (default: .)"
    )
    p.add_argument("--slug", help=f"artifact to compare with (default: from {STATE_FILE})")
    p.add_argument("--url", help="artifact URL to compare with")
    p.set_defaults(func=cmd_status)

    _add_more(sub, common)

    p = sub.add_parser("update", help="upgrade this CLI to the latest release now")
    p.set_defaults(func=update.cmd_update)

    p = sub.add_parser("whoami", parents=[common], help="check that auth works")
    p.set_defaults(func=cmd_whoami)

    return parser


def _add_more(sub, common) -> None:
    """Parsers for comments, db, assets, types, sharing and housekeeping."""

    def slug_arg(p):
        p.add_argument("slug", help="slug or full artifact URL")

    # comments
    p = sub.add_parser("comments", help="read, reply to and resolve comment threads")
    csub = p.add_subparsers(dest="comments_command", required=True)
    c = csub.add_parser("list", parents=[common], help="comment threads (open ones by default)")
    slug_arg(c)
    which = c.add_mutually_exclusive_group()
    which.add_argument("--all", action="store_true", help="include resolved threads")
    which.add_argument(
        "--awaiting", action="store_true", help="only threads waiting on a reply from Claude"
    )
    which.add_argument("--thread", metavar="ID", help="only this thread")
    c.add_argument("--full", action="store_true", help="don't shorten comment text")
    c.set_defaults(func=commands.cmd_comments_list)
    c = csub.add_parser(
        "reply", parents=[common], help="reply as Claude on a thread sent to Claude"
    )
    slug_arg(c)
    c.add_argument("thread", help="thread id (from `comments list`)")
    c.add_argument("text", help="reply text, or - for stdin")
    c.add_argument(
        "--id", help="client id (UUID) making a retry idempotent (default: a fresh one)"
    )
    c.add_argument(
        "--allow-duplicate",
        action="store_true",
        help="post even though a Claude reply already answers every request on the thread",
    )
    c.set_defaults(func=commands.cmd_comments_reply)
    c = csub.add_parser("resolve", parents=[common], help="resolve threads sent to Claude")
    slug_arg(c)
    c.add_argument("thread", nargs="+", help="thread id(s)")
    c.set_defaults(func=commands.cmd_comments_resolve)

    # db
    p = sub.add_parser("db", help="read and write an artifact's database")
    dsub = p.add_subparsers(dest="db_command", required=True)

    def as_level(d):
        d.add_argument(
            "--as-level",
            choices=["view", "interact", "admin"],
            help="act with only this access level, to check what the page's rules allow "
            "such a viewer (view: shared view-only; interact: any signed-in viewer; admin: "
            "an editor)",
        )

    def require_version(d):
        d.add_argument(
            "--require-version",
            action="store_true",
            help="refuse a write to an existing document that carries no --if-version",
        )

    def db_parser(name, help_text, doc=True):
        d = dsub.add_parser(name, parents=[common], help=help_text)
        slug_arg(d)
        d.add_argument(
            "collection", help="collection path (odd segments, e.g. tasks or data/users/me)"
        )
        if doc:
            d.add_argument("doc", help="document id")
        as_level(d)
        d.set_defaults(func=commands.cmd_db)
        return d

    d = db_parser("get", "one document")
    d.add_argument("--out-dir", metavar="DIR", help="save it as DIR/<collection>/<doc>.json")
    for name, help_text in (("list", "documents in a collection"), ("query", "filtered documents")):
        d = db_parser(name, help_text, doc=False)
        d.add_argument(
            "--out-dir", metavar="DIR", help="save each as DIR/<collection>/<id>.json"
        )
        d.add_argument("--limit", type=int)
        d.add_argument("--cursor", help="nextCursor from an earlier page")
        d.add_argument("--all", action="store_true", help="follow every page")
        if name == "query":
            d.add_argument(
                "--where",
                nargs=3,
                action="append",
                metavar=("FIELD", "OP", "VALUE"),
                help="OP: == != < <= > >= in not-in array-contains; VALUE is JSON or a string",
            )
            d.add_argument("--order-by", metavar="FIELD[:asc|desc]")
    for name, help_text in (
        ("set", "create or replace a document"),
        ("update", "merge fields into a document ({\"__delete__\": true} drops one)"),
    ):
        d = db_parser(name, help_text)
        d.add_argument("--data", help="JSON object, - for stdin, or @FILE")
        d.add_argument("--if-version", type=int, help="refuse unless the document is at this version")
        require_version(d)
    d = db_parser("delete", "delete a document")
    d.add_argument("--if-version", type=int)
    require_version(d)
    d = db_parser("edit", "replace text inside one string field")
    d.add_argument("--field", required=True)
    d.add_argument("--old", required=True, help="text to find")
    d.add_argument("--new", required=True, help="replacement text")
    d.add_argument("--replace-all", action="store_true")
    d.add_argument("--if-version", type=int)
    d = dsub.add_parser("batch", parents=[common], help="several writes in one request")
    slug_arg(d)
    d.add_argument(
        "writes",
        help="JSON list of {op, collection, doc_id, data?, if_version?}; - for stdin, or @FILE",
    )
    as_level(d)
    require_version(d)
    d.set_defaults(func=commands.cmd_db)
    d = dsub.add_parser("whoami", parents=[common], help="your id for data/users/<id>")
    slug_arg(d)
    d.set_defaults(func=commands.cmd_db)
    d = dsub.add_parser(
        "profiles", parents=[common], help="display names for person ids (u_...) in the data"
    )
    slug_arg(d)
    d.add_argument("id", nargs="+", help="person ids, 1-64")
    d.set_defaults(func=commands.cmd_db)

    # assets
    p = sub.add_parser("assets", help="an artifact's uploaded asset store")
    asub = p.add_subparsers(dest="assets_command", required=True)
    a = asub.add_parser("list", parents=[common], help="uploaded assets and usage")
    slug_arg(a)
    a = asub.add_parser("upload", parents=[common], help="upload files; prints id and url")
    slug_arg(a)
    a.add_argument("file", nargs="+")
    a.add_argument("--type", help="content type (default: from the extension)")
    a = asub.add_parser("get", parents=[common], help="an asset's bytes")
    slug_arg(a)
    a.add_argument("id", help="asset id (32 hex characters)")
    a.add_argument("-o", "--out", help="write to this file instead of stdout")
    a = asub.add_parser("delete", parents=[common], help="delete assets for good")
    slug_arg(a)
    a.add_argument("id", nargs="+")
    a = asub.add_parser("copy", parents=[common], help="copy assets from another artifact")
    slug_arg(a)
    a.add_argument("--from", dest="source", required=True, help="artifact to copy from")
    a.add_argument("id", nargs="+", help="asset ids on the source (1-10)")
    for a in asub.choices.values():
        a.set_defaults(func=commands.cmd_assets)

    # types
    p = sub.add_parser("types", help="Artifact types (Slides, Docs, Dashboard, ...)")
    tsub = p.add_subparsers(dest="types_command", required=True)
    t = tsub.add_parser("list", parents=[common], help="types this account can create from")
    t.add_argument("--query", "-q", help="only the types that best match this text")
    t = tsub.add_parser("show", parents=[common], help="a type's metadata and files")
    t.add_argument("type", help="type title (e.g. slides), slug or URL")
    t = tsub.add_parser("read", parents=[common], help="a text file the type ships")
    t.add_argument("type", help="type title, slug or URL")
    t.add_argument("path", help="e.g. SKILL.md")
    t = tsub.add_parser("instances", parents=[common], help="artifacts made from a type")
    t.add_argument("type", help="type title, slug or URL")
    t.add_argument("--limit", type=int, default=50)
    t.add_argument("--scope", choices=["mine", "shared", "all"], default="all")
    for t in tsub.choices.values():
        t.set_defaults(func=commands.cmd_types)

    p = sub.add_parser("create", parents=[common], help="start a new artifact from a type")
    p.add_argument("type", help="type title (e.g. slides), slug or URL")
    p.add_argument("--title")
    p.add_argument("--idempotency-key", help="reuse to make a retried create safe")
    p.add_argument("-q", "--quiet", action="store_true")
    p.set_defaults(func=commands.cmd_create)

    p = sub.add_parser("contract", parents=[common], help="runtime contract and capabilities")
    p.add_argument("version", nargs="?", default="latest")
    p.add_argument(
        "--prompt", action="store_true", help="print the capability guide for page authors"
    )
    p.add_argument(
        "--defs",
        nargs="*",
        metavar="NAME",
        help="print the TypeScript definitions (.d.ts) for these capabilities, or for "
        "claude.use and every capability when no name is given",
    )
    p.add_argument("--out-dir", metavar="DIR", help="save --defs as DIR/<version>/<name>.d.ts")
    p.set_defaults(func=commands.cmd_contract)

    # sharing and housekeeping
    p = sub.add_parser("share", parents=[common], help="show or change who can open an artifact")
    slug_arg(p)
    p.add_argument("--private", action="store_true", help="only the owner")
    p.add_argument("--org", action="store_true", help="everyone in the organization")
    p.add_argument("--add", action="append", metavar="ACCOUNT_UUID", help="share with a person")
    p.add_argument("--remove", action="append", metavar="ACCOUNT_UUID", help="unshare a person")
    p.add_argument(
        "--access", choices=["view", "comment"], default="comment", help="for --add (default comment)"
    )
    p.set_defaults(func=commands.cmd_share)

    p = sub.add_parser("versions", parents=[common], help="version history")
    slug_arg(p)
    p.set_defaults(func=commands.cmd_versions)

    p = sub.add_parser("rename", parents=[common], help="change the title")
    slug_arg(p)
    p.add_argument("title")
    p.set_defaults(func=commands.cmd_rename)

    for name, help_text in (("pin", "pin to your sidebar"), ("unpin", "unpin")):
        p = sub.add_parser(name, parents=[common], help=help_text)
        slug_arg(p)
        p.set_defaults(func=commands.cmd_pin)

    p = sub.add_parser("delete", parents=[common], help="delete an artifact for everyone")
    slug_arg(p)
    p.add_argument("--yes", action="store_true", help="confirm; there is no undo")
    p.set_defaults(func=commands.cmd_delete)

    p = sub.add_parser("watch", parents=[common], help="print new versions and comments as they land")
    slug_arg(p)
    p.add_argument("--interval", type=float, default=30, help="seconds between polls (default 30)")
    p.add_argument("--count", type=int, help="exit after this many events")
    p.add_argument("--no-comments", action="store_true", help="only watch versions")
    p.set_defaults(func=commands.cmd_watch)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # Updating is best effort: no problem with it may stop or change a command.
    check = None
    if args.command != "update":
        with contextlib.suppress(Exception):
            check = update.start_check()
    try:
        return _run(args)
    finally:
        with contextlib.suppress(Exception):
            update.finish_check(check, quiet=getattr(args, "quiet", False))


def _run(args) -> int:
    try:
        return args.func(args)
    except auth.AuthError as exc:
        _eprint(f"auth error: {exc}")
        return 2
    except api.ConflictError as exc:
        _eprint(f"error: {exc}")
        return EXIT_CONFLICT
    except api.ApiError as exc:
        _eprint(f"error: {exc}")
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
