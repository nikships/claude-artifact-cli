"""Command-line front end."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from . import api, auth

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


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _destination(root: Path, published: str) -> Path:
    """Where a published path lands under root, refusing anything that escapes it."""
    if api.clean_path(published) is None:
        raise SystemExit(f"error: refusing to write unsafe path {published!r}")
    dest = root
    for part in published.split("/"):
        dest = dest / part
        if dest.is_symlink():
            raise SystemExit(f"error: {dest} is a symlink; refusing to write through it")
    return dest


def _write(root: Path, published: str, data: bytes) -> Path:
    dest = _destination(root, published)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    return dest


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
        if typed:
            # An Artifact type's own files stay fixed and aren't in the
            # directory, so patch only this artifact's files, and turn files
            # deleted locally since the last pull or publish into removals.
            if mode is None:
                mode = "patch"
            known = state.get("files") if isinstance(state.get("files"), dict) else {}
            removals = sorted((set(known) - set(files)) | set(args.remove or []))
        elif mode is None and not args.remove:
            # The directory is the whole artifact, so files deleted locally go too.
            mode = "replace"
        index = files.pop("index.html", None)
        raw = index.read_bytes() if index else b""
        page = api.Asset(path="index.html", data=raw, content_type="text/html") if index else None
        extra = [
            api.Asset(path=pub, data=src.read_bytes(), content_type=api.guess_content_type(pub))
            for pub, src in files.items()
        ]
        fallback_title = target.resolve().name
        state_root = target
        if not typed:
            removals = list(args.remove or [])
    elif target.is_file():
        raw = target.read_bytes()
        content_type = api.guess_content_type(target.name)
        if content_type not in ("text/html", "text/markdown"):
            content_type = "text/html"
        page = api.Asset(path="index.html", data=raw, content_type=content_type)
        extra = _load_assets(args.file_spec or [], args.root)
        removals = list(args.remove or [])
        fallback_title = target.stem
    else:
        raise SystemExit(f"error: no such file or directory: {target}")

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
    ):
        if value:
            meta[key] = value

    progress = (lambda m: None) if args.quiet else (lambda m: _eprint(f"  {m}"))

    client = _client(args)
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
    )
    url = api.ARTIFACT_URL.format(slug=result["slug"])

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


def cmd_list(args) -> int:
    frames = _client(args).list_frames(limit=args.limit)

    if args.scope != "all":
        want = "mine" if args.scope == "mine" else "shared"
        frames = [f for f in frames if (f.get("rel") or "mine") == want]
    frames = [f for f in frames if not f.get("softDeleted")]

    if args.json:
        print(json.dumps(frames, indent=2))
        return 0

    if not frames:
        print("No artifacts.")
        return 0

    for f in frames:
        slug = f.get("slug", "?")
        title = (f.get("title") or "(untitled)").strip()
        updated = (f.get("updatedAt") or "")[:10]
        print(f"{slug:<38} {updated:<11} {title}")
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
    own = sorted(p for p in reader.files if not reader.type_owned(p))

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
    skipped = len(reader.files) - len(own)
    if skipped:
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
    # Files an Artifact type supplies aren't the artifact's own and never pulled.
    remote = {
        f["path"]: f.get("sha256")
        for f in data.get("files") or []
        if isinstance(f.get("path"), str) and not isinstance(f.get("src"), dict)
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
    p.add_argument("-q", "--quiet", action="store_true")
    p.set_defaults(func=cmd_publish)

    p = sub.add_parser("list", parents=[common], help="list your artifacts")
    p.add_argument("--limit", type=int, default=200)
    p.add_argument("--scope", choices=["mine", "shared", "all"], default="mine")
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

    p = sub.add_parser("whoami", parents=[common], help="check that auth works")
    p.set_defaults(func=cmd_whoami)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
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
