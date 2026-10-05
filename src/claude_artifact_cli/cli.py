"""Command-line front end."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from . import api, auth

TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)


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


# -- commands --------------------------------------------------------------


def cmd_publish(args) -> int:
    page_path = Path(args.file)
    if not page_path.is_file():
        raise SystemExit(f"error: no such file: {page_path}")

    raw = page_path.read_bytes()
    content_type = api.guess_content_type(page_path.name)
    if content_type not in ("text/html", "text/markdown"):
        content_type = "text/html"

    page = api.Asset(path="index.html", data=raw, content_type=content_type)
    extra = _load_assets(args.file_spec or [], args.root)

    slug = api.slug_from(args.url or args.slug) if (args.url or args.slug) else None

    meta = {}
    meta["title"] = args.title or _title_from_html(
        raw.decode("utf-8", "replace"), page_path.stem
    )
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
        removals=args.remove or [],
        slug=slug,
        meta=meta,
        mode=args.mode,
        base_version=args.base_version,
        force=args.force,
        on_progress=progress,
    )

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(api.ARTIFACT_URL.format(slug=result["slug"]))
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
    data = _client(args).read(slug)

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

    if args.out:
        Path(args.out).write_text(json.dumps(data, indent=2), encoding="utf-8")
        _eprint(f"wrote metadata to {args.out}")
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
    p.add_argument("file", help="the HTML page to publish")
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
    p.add_argument("--mode", choices=["replace", "patch"], help="default: replace")
    p.add_argument("--base-version", help="expected current version (default: look it up)")
    p.add_argument("--force", action="store_true", help="overwrite a newer version")
    p.add_argument("-q", "--quiet", action="store_true")
    p.set_defaults(func=cmd_publish)

    p = sub.add_parser("list", parents=[common], help="list your artifacts")
    p.add_argument("--limit", type=int, default=200)
    p.add_argument("--scope", choices=["mine", "shared", "all"], default="mine")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("read", parents=[common], help="read a published artifact back")
    p.add_argument("slug", help="slug or full artifact URL")
    p.add_argument("-o", "--out", help="write the metadata JSON to this file")
    p.set_defaults(func=cmd_read)

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
    except api.ApiError as exc:
        _eprint(f"error: {exc}")
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
