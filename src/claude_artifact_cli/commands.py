"""Commands beyond publishing and reading files: comments, the artifact database,
assets, types, sharing, history and housekeeping."""

from __future__ import annotations

import base64
import json
import re
import secrets
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from . import api, auth


def _eprint(msg: str) -> None:
    print(msg, file=sys.stderr)


def _client(args) -> api.FrameClient:
    return api.FrameClient(auth.get_token(args.token), base=args.base)


def _print_json(data: Any) -> None:
    print(json.dumps(data, indent=2, ensure_ascii=False))


def _read_text(value: str) -> str:
    """A literal, or `-` for stdin."""
    return sys.stdin.read() if value == "-" else value


def _read_json(value: str, what: str) -> Any:
    """JSON given inline, `-` for stdin, or `@path` for a file's contents."""
    try:
        if value.startswith("@"):
            value = Path(value[1:]).read_text(encoding="utf-8")
        return json.loads(_read_text(value))
    except (OSError, ValueError) as exc:
        raise SystemExit(f"error: {what} is not valid JSON: {exc}") from None


def _one_line(text: str, width: int = 100) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= width else text[: width - 1] + "…"


# -- history and housekeeping ------------------------------------------------


def cmd_versions(args) -> int:
    slug = api.slug_from(args.slug)
    data = _client(args).versions(slug)
    if args.json:
        _print_json(data)
        return 0
    live = data.get("live")
    meta = data.get("versions") or {}
    for ver in data.get("history") or []:
        info = meta.get(ver) or {}
        mark = "*" if ver == live else " "
        when = (info.get("created_at") or "")[:19].replace("T", " ")
        note = f"  ({_one_line(info['note'], 60)})" if info.get("note") else ""
        print(f"{mark} {ver:<18} {when:<19}  {info.get('title') or ''}{note}")
    return 0


def cmd_rename(args) -> int:
    slug = api.slug_from(args.slug)
    data = _client(args).retitle(slug, args.title)
    if args.json:
        _print_json(data)
    else:
        print(data.get("title", args.title))
        _eprint(f"  version {data.get('version')}")
    return 0


def cmd_pin(args) -> int:
    slug = api.slug_from(args.slug)
    pinned = args.command == "pin"
    _client(args).pin(slug, pinned)
    _eprint(f"{'pinned' if pinned else 'unpinned'} {api.ARTIFACT_URL.format(slug=slug)}")
    return 0


def cmd_delete(args) -> int:
    slug = api.slug_from(args.slug)
    if not args.yes:
        raise SystemExit(
            f"error: deleting {api.ARTIFACT_URL.format(slug=slug)} removes it for everyone "
            "and can't be undone; pass --yes to confirm"
        )
    _client(args).delete(slug)
    _eprint(f"deleted {api.ARTIFACT_URL.format(slug=slug)}")
    return 0


# -- sharing -------------------------------------------------------------------


_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def _sharing_change(current: dict, args) -> dict | None:
    """The PATCH body for the requested change, or None if nothing changes."""
    read = current.get("read") or {}
    mode = read.get("mode") or "owner"
    users = list(read.get("users") or []) if mode in ("users", "org") else []
    viewers = [v.lower() for v in current.get("viewers") or []]
    commenters = [c.lower() for c in current.get("commenters") or []]

    if args.private:
        if mode == "owner":
            return None
        return {"read": {"mode": "owner", "users": []}, "viewers": [], "commenters": []}
    if args.org:
        if mode == "public":
            raise SystemExit("error: the artifact is public; sharing with the org would narrow it")
        if mode == "org":
            return None
        return {"read": {"mode": "org", "users": users}}

    add = [u.strip().lower() for u in args.add or []]
    remove = {u.strip().lower() for u in args.remove or []}
    for u in add + sorted(remove):
        if not _UUID_RE.match(u):
            raise SystemExit(f"error: {u!r} is not an account UUID")
    owner = (current.get("owner_account") or "").lower()
    add = [u for u in dict.fromkeys(add) if u != owner]
    if mode == "public" and (add or remove):
        raise SystemExit(
            "error: the artifact is public; anyone with the link can open it "
            "(use --private or --org to narrow it first)"
        )

    new_users = [u for u in dict.fromkeys(users + add) if u not in remove]
    if args.access == "view":
        if add and current.get("viewersEnabled") is not True:
            raise SystemExit("error: view-only sharing isn't available for this artifact")
        new_viewers = [v for v in dict.fromkeys(viewers + add) if v not in remove]
        new_commenters = [c for c in commenters if c not in add and c not in remove]
    else:
        new_viewers = [v for v in viewers if v not in add and v not in remove]
        new_commenters = commenters
        if current.get("commentersEnabled") is True:
            new_commenters = [c for c in dict.fromkeys(commenters + add) if c not in remove]
    new_mode = "org" if mode == "org" else ("users" if new_users else "owner")
    change: dict[str, Any] = {"read": {"mode": new_mode, "users": new_users}}
    if new_viewers != viewers:
        change["viewers"] = new_viewers
    if new_commenters != commenters:
        change["commenters"] = new_commenters
    if new_users == users and new_mode == mode and len(change) == 1:
        return None
    return change


def _print_sharing(data: dict, slug: str) -> None:
    read = data.get("read") or {}
    mode = read.get("mode") or "owner"
    label = {
        "owner": "private (owner only)",
        "users": "specific people",
        "org": "organization",
        "public": "anyone with the link",
    }.get(mode, mode)
    print(api.ARTIFACT_URL.format(slug=slug))
    print(f"  audience    {label}")
    if mode == "org" and data.get("orgReadLevel"):
        print(f"  org access  {data['orgReadLevel']}")
    for user in read.get("users") or []:
        print(f"  person      {user}")
    for key in ("viewers", "commenters", "editors"):
        for user in data.get(key) or []:
            print(f"  {key[:-1]:<10}  {user}")
    write = (data.get("write") or {}).get("mode")
    if write:
        print(f"  edit        {write}")


def cmd_share(args) -> int:
    slug = api.slug_from(args.slug)
    picked = sum(bool(x) for x in (args.private, args.org, args.add or args.remove))
    if picked > 1:
        raise SystemExit("error: pick one of --private, --org, or --add/--remove")
    client = _client(args)
    current = client.sharing(slug)
    if picked:
        if not isinstance(current.get("version"), str):
            raise SystemExit("error: the sharing settings came back without a version")
        change = _sharing_change(current, args)
        if change is None:
            _eprint("  nothing to change")
        else:
            current = client.set_sharing(slug, change, current["version"])
    if args.json:
        _print_json(current)
    else:
        _print_sharing(current, slug)
    return 0


# -- comments ------------------------------------------------------------------


def _is_claude(comment: dict) -> bool:
    return bool((comment.get("author") or {}).get("role"))


def awaiting_claude(thread: dict) -> bool:
    """A thread a person sent to Claude whose latest summons has no Claude reply."""
    if thread.get("resolved_at") or not thread.get("claude_activated_at"):
        return False
    waiting = False
    for comment in thread.get("comments") or []:
        if _is_claude(comment):
            waiting = False
        elif comment.get("to_claude_at"):
            waiting = True
    return waiting


def _author(comment: dict, names: dict) -> str:
    author = comment.get("author") or {}
    if author.get("role"):
        return "Claude"
    account = author.get("account") or "?"
    return names.get(account) or account


def _filter_threads(data: dict, args) -> list[dict]:
    threads = [t for t in data.get("threads") or [] if isinstance(t, dict)]
    if getattr(args, "thread", None):
        return [t for t in threads if t.get("id") == args.thread]
    if args.awaiting:
        return [t for t in threads if awaiting_claude(t)]
    if not args.all:
        return [t for t in threads if not t.get("resolved_at")]
    return threads


def cmd_comments_list(args) -> int:
    slug = api.slug_from(args.slug)
    data = _client(args).comments(slug)
    threads = _filter_threads(data, args)
    if not threads and getattr(args, "thread", None):
        raise api.ApiError(f"no comment thread {args.thread} on this artifact", 404)
    if args.json:
        _print_json({**data, "threads": threads})
        return 0
    if not threads:
        print("No comments." if not data.get("threads") else "No matching threads.")
        return 0
    names = data.get("names") or {}
    for thread in threads:
        state = []
        if thread.get("resolved_at"):
            state.append("resolved")
        if awaiting_claude(thread):
            state.append("awaiting Claude")
        elif thread.get("claude_activated_at"):
            state.append("sent to Claude")
        anchor = thread.get("anchor") or {}
        where = anchor.get("label") or anchor.get("path") or anchor.get("file") or ""
        print(f"{thread.get('id')}  {', '.join(state)}".rstrip())
        if where:
            print(f"  on {_one_line(where, 90)}")
        for comment in thread.get("comments") or []:
            when = (comment.get("created_at") or "")[:16].replace("T", " ")
            text = comment.get("text") or ""
            if not args.full:
                text = _one_line(text)
            print(f"  [{when}] {_author(comment, names)}: {text}")
        print()
    return 0


REPLY_MAX_BYTES = 4096


def answered(thread: dict) -> bool:
    """A Claude reply already stands after every request a person sent to Claude."""
    replied = False
    for comment in thread.get("comments") or []:
        if _is_claude(comment):
            replied = True
        elif comment.get("to_claude_at"):
            replied = False
    return replied


def cmd_comments_reply(args) -> int:
    slug = api.slug_from(args.slug)
    text = _read_text(args.text).strip()
    if not text:
        raise SystemExit("error: the reply is empty")
    size = len(text.encode("utf-8"))
    if size > REPLY_MAX_BYTES:
        raise SystemExit(
            f"error: the reply is {size} bytes; a comment holds at most {REPLY_MAX_BYTES} "
            "bytes of UTF-8"
        )
    client_id = args.id or str(uuid.uuid4())
    client = _client(args)
    thread = next(
        (t for t in client.comments(slug).get("threads") or [] if t.get("id") == args.thread),
        None,
    )
    if thread is None:
        raise api.ApiError(f"no comment thread {args.thread} on this artifact", 404)
    if not thread.get("claude_activated_at"):
        raise api.ApiError(
            "Claude can only reply on a thread a person sent to Claude (\"Send to Claude\" "
            "on the artifact's page); this one wasn't",
            403,
        )
    # An explicit --id is a retry of an earlier post, which the server dedupes.
    if not args.id and not args.allow_duplicate and answered(thread):
        raise api.ApiError(
            "a Claude reply already answers every request on this thread, so this looks "
            "like a duplicate; pass --allow-duplicate for a deliberate follow-up that adds "
            "something new, or --id with the earlier reply's id to retry that post"
        )
    # Marks the person's request answered, as the Artifact tool does.
    result = client.reply(
        slug, args.thread, text, client_id, answers_summon=awaiting_claude(thread)
    )
    if args.json:
        _print_json({**result, "id": client_id})
    else:
        print(result.get("comment_id", ""))
        if result.get("replayed"):
            _eprint("  already posted with this --id; nothing new was posted")
    return 0


def cmd_comments_resolve(args) -> int:
    slug = api.slug_from(args.slug)
    client = _client(args)
    for thread in args.thread:
        client.resolve(slug, thread)
        _eprint(f"resolved {thread}")
    return 0


# -- database ------------------------------------------------------------------


_SEGMENT = r"(?!\.\.?(?:/|$))[A-Za-z0-9_\-.~:@+]{1,200}"
_SEGMENT_RE = re.compile(f"^{_SEGMENT}$")
_COLLECTION_RE = re.compile(f"^{_SEGMENT}(?:/{_SEGMENT}){{0,14}}$")
_ME = "data/users/me"
_OPS = {"==": "eq", "!=": "ne", "<": "lt", "<=": "lte", ">": "gt", ">=": "gte"}
_ALL_OPS = {"eq", "ne", "in", "not-in", "lt", "lte", "gt", "gte", "array-contains"}


def _collection(value: str) -> str:
    value = value.strip("/")
    if not _COLLECTION_RE.match(value):
        raise SystemExit(
            f"error: {value!r} is not a collection path: 1-15 '/'-separated segments of "
            "letters, digits and _ - . ~ : @ +"
        )
    if len(value.split("/")) % 2 == 0:
        head, _, last = value.rpartition("/")
        raise SystemExit(
            f"error: {value!r} names a document, not a collection (collection paths have an "
            f"odd number of segments); use collection {head!r} with doc id {last!r}"
        )
    return value


def _doc_id(value: str) -> str:
    if not _SEGMENT_RE.match(value):
        raise SystemExit(f"error: {value!r} is not a document id (one path segment)")
    return value


class _Db:
    """Resolves `data/users/me` to the caller's own id once per command."""

    def __init__(
        self,
        client: api.FrameClient,
        slug: str,
        as_level: str | None = None,
        require_version: bool = False,
    ):
        self.client = client
        self.slug = slug
        self.as_level = as_level
        self.require_version = require_version
        self._me: str | None = None

    def collection(self, value: str) -> str:
        value = _collection(value)
        if value == _ME or value.startswith(_ME + "/"):
            if self._me is None:
                self._me = self.call("whoami", {})["id"]
            value = "data/users/" + self._me + value[len(_ME) :]
        return value

    def path(self, collection: str, doc: str) -> str:
        return f"{self.collection(collection)}/{_doc_id(doc)}"

    def call(self, verb: str, args: dict) -> dict:
        writes = verb in ("set", "update", "delete", "batch")
        try:
            return self.client.db(
                self.slug,
                verb,
                args,
                as_level=self.as_level if verb != "whoami" else None,
                require_version=self.require_version and writes,
            )
        except api.ApiError as exc:
            code = api.error_code(exc.body)
            if self.as_level and code in ("not-found", "not_found"):
                raise api.ApiError(
                    f"not found at --as-level {self.as_level}: either nothing is there, or the "
                    "page's access rules refuse this to such a viewer (a refused write reads as "
                    "not found)",
                    exc.status,
                    exc.body,
                ) from None
            if code == "not_declared":
                raise api.ApiError(
                    "this artifact's published version doesn't declare the db capability - "
                    "republish it with --capability db --capability user",
                    exc.status,
                    exc.body,
                ) from None
            if code == "version_mismatch":
                err = exc.body.get("error") or {}
                live = err.get("version")
                where = f" (it is at version {live})" if live is not None else ""
                if err.get("required"):
                    message = (
                        f"the document already exists{where} and --require-version needs "
                        "--if-version on a write to it (in a batch, if_version on that entry); "
                        "nothing was written. Read it and resend with the version you read"
                    )
                else:
                    message = (
                        f"the document changed since --if-version{where}; nothing was "
                        "written. Read it again and retry against what it holds now"
                    )
                raise api.ConflictError(message, exc.status, exc.body) from None
            raise


BATCH_MAX = 50

_DB_META = ("owned", "public", "owner_account_id", "owner_in_your_org", "outside_writer")


def _strip_meta(data: dict) -> dict:
    return {k: v for k, v in data.items() if k not in _DB_META}


def _write_args(path: str, data: Any, if_version: int | None, replace: bool) -> dict:
    args: dict[str, Any] = {"path": path}
    if data is not None:
        if not isinstance(data, dict):
            raise SystemExit("error: a document must be a JSON object")
        args["data"] = data
    if replace:
        args["replace"] = True
    if if_version is not None:
        args["ifVersion"] = if_version
    return args


def _doc_data(args) -> Any:
    if args.data is None:
        raise SystemExit("error: pass the document as --data JSON (or @file, or - for stdin)")
    return _read_json(args.data, "--data")


def _where_value(raw: str) -> Any:
    try:
        return json.loads(raw)
    except ValueError:
        return raw


_PERSON_RE = re.compile(r"^u_[A-Za-z0-9_-]{22}$")
_WINDOWS_UNSAFE = re.compile(
    r'[<>:"|?*]|[. ]$|^(CON|PRN|AUX|NUL|COM[0-9]|LPT[0-9])(\..*)?$', re.IGNORECASE
)


def _safe_dest(root: Path, parts: list[str]) -> Path:
    """root/parts..., refusing unsafe segments, symlinks and anything outside root.

    Segments can come from the server (a document id, the id `me` resolves to,
    a contract version), so each is checked before anything is written.
    """
    dest = root
    for part in parts:
        if not _SEGMENT_RE.match(part.removesuffix(".json").removesuffix(".d.ts") or "."):
            raise SystemExit(f"error: refusing to write unsafe path segment {part!r}")
        if sys.platform == "win32" and _WINDOWS_UNSAFE.search(part):
            raise SystemExit(f"error: {part!r} can't be a file name on Windows")
        dest = dest / part
        if dest.is_symlink():
            raise SystemExit(f"error: {dest} is a symlink; refusing to write through it")
    if not dest.resolve().is_relative_to(root.resolve()):
        raise SystemExit(f"error: refusing to write {dest} outside {root}")
    return dest


def _save_docs(out_dir: str, collection: str, docs: list[dict]) -> None:
    """Each document as <out_dir>/<collection>/<id>.json; prints the paths."""
    root = Path(out_dir)
    for doc in docs:
        doc_id = str(doc.get("id", ""))
        dest = _safe_dest(root, [*collection.split("/"), f"{doc_id}.json"])
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(
            json.dumps(doc.get("data"), indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        print(dest)


def _db_profiles(client: api.FrameClient, slug: str, args) -> int:
    ids = list(dict.fromkeys(args.id))
    if not 1 <= len(ids) <= 64:
        raise SystemExit("error: name 1-64 people at a time")
    for person in ids:
        if not _PERSON_RE.match(person):
            raise SystemExit(f"error: {person!r} is not a person id (u_ and 22 characters)")
    try:
        profiles = client.profiles(slug, ids)
    except api.ApiError as exc:
        if api.error_code(exc.body) == "not_declared" or (
            isinstance(exc.body, dict) and exc.body.get("error") == "not_declared"
        ):
            raise api.ApiError(
                "this artifact's published version doesn't declare the user capability - "
                "republish it with --capability user",
                exc.status,
                exc.body,
            ) from None
        raise
    if args.json:
        _print_json(profiles)
        return 0
    for person in ids:
        info = profiles.get(person) or {}
        name = info.get("name") or "(unknown)"
        print(f"{person}\t{name}" + ("\tguest" if info.get("guest") else ""))
    return 0


def cmd_db(args) -> int:
    slug = api.slug_from(args.slug)
    client = _client(args)
    verb = args.db_command

    if verb == "profiles":
        return _db_profiles(client, slug, args)

    db = _Db(
        client,
        slug,
        as_level=getattr(args, "as_level", None),
        require_version=getattr(args, "require_version", False),
    )

    if verb == "whoami":
        data = db.call("whoami", {})
        print(data.get("id", ""))
        return 0

    out_dir = getattr(args, "out_dir", None)
    if out_dir and args.json:
        raise SystemExit("error: --out-dir saves the documents; it doesn't combine with --json")

    if verb == "get":
        collection = db.collection(args.collection)
        data = db.call("get", {"path": f"{collection}/{_doc_id(args.doc)}"})
        if args.json:
            _print_json(_strip_meta(data))
            return 0
        if not data.get("exists"):
            _eprint("  no such document")
            return 1
        if out_dir:
            _save_docs(out_dir, collection, [{"id": args.doc, "data": data.get("data")}])
            _eprint(f"  version {data.get('version')}")
        else:
            _print_json(data.get("data"))
            _eprint(f"  version {data.get('version')}  updated {data.get('updatedAt')}")
        return 0

    if verb in ("list", "query"):
        query: dict[str, Any] = {"collection": db.collection(args.collection)}
        if args.limit is not None:
            query["limit"] = args.limit
        if args.cursor:
            query["cursor"] = args.cursor
        if verb == "query":
            where = []
            for field, op, value in args.where or []:
                op = _OPS.get(op, op)
                if op not in _ALL_OPS:
                    raise SystemExit(f"error: unknown operator {op!r}")
                where.append({"field": field, "op": op, "value": _where_value(value)})
            if where:
                query["where"] = where
            if args.order_by:
                field, _, direction = args.order_by.partition(":")
                order: dict[str, Any] = {"field": field}
                if direction:
                    if direction not in ("asc", "desc"):
                        raise SystemExit("error: --order-by takes FIELD or FIELD:asc|desc")
                    order["direction"] = direction
                query["orderBy"] = order
        docs: list = []
        cursor = None
        while True:
            data = db.call(verb, query)
            docs.extend(data.get("docs") or [])
            cursor = data.get("nextCursor") or None
            if not (args.all and cursor):
                break
            query["cursor"] = cursor
        if out_dir:
            _save_docs(out_dir, query["collection"], docs)
        elif args.json:
            _print_json({"docs": docs, **({"nextCursor": cursor} if cursor else {})})
        else:
            for doc in docs:
                body = json.dumps(doc.get("data"), ensure_ascii=False)
                print(f"{doc.get('id')}\tv{doc.get('version')}\t{body}")
        if cursor and not args.json:
            _eprint(f"  more: --cursor {cursor}  (or --all)")
        return 0

    if verb in ("set", "update", "delete", "edit"):
        path = db.path(args.collection, args.doc)
        if verb == "delete":
            call = ("delete", _write_args(path, None, args.if_version, False))
        elif verb == "edit":
            body = {
                "path": path,
                "field": args.field,
                "old_str": args.old,
                "new_str": args.new,
                "idem": base64.urlsafe_b64encode(secrets.token_bytes(16)).rstrip(b"=").decode(),
            }
            if args.replace_all:
                body["replace_all"] = True
            if args.if_version is not None:
                body["ifVersion"] = args.if_version
            call = ("str_replace", body)
        else:
            call = (verb, _write_args(path, _doc_data(args), args.if_version, verb == "set"))
        data = db.call(*call)
        if args.json:
            _print_json(_strip_meta(data))
        else:
            if data.get("version") is not None:
                print(data["version"])
            usage = data.get("usage") or {}
            if usage:
                _eprint(f"  {usage.get('documents')}/{usage.get('maxDocuments')} documents")
        return 0

    if verb == "batch":
        writes = _read_json(args.writes, "the batch")
        if not isinstance(writes, list) or not 1 <= len(writes) <= BATCH_MAX:
            raise SystemExit(f"error: a batch is a JSON list of 1-{BATCH_MAX} writes")
        ops = []
        for i, write in enumerate(writes):
            if not isinstance(write, dict) or write.get("op") not in ("set", "update", "delete"):
                raise SystemExit(f"error: write {i} needs op set, update or delete")
            path = db.path(str(write.get("collection", "")), str(write.get("doc_id", "")))
            data = write.get("data") if write["op"] != "delete" else None
            if write["op"] != "delete" and data is None:
                raise SystemExit(f"error: write {i} ({write['op']}) needs data")
            if any(op["path"] == path for op in ops):
                raise SystemExit(f"error: write {i} addresses {path} again; one write per document")
            ops.append(
                {"op": write["op"],
                 **_write_args(path, data, write.get("if_version"), write["op"] == "set")}
            )
        data = db.call("batch", {"ops": ops})
        if args.json:
            _print_json(_strip_meta(data))
        else:
            for op, result in zip(ops, data.get("results") or []):
                version = result.get("version")
                print(f"{op['op']:<7} {op['path']}" + (f"  v{version}" if version else ""))
        return 0

    raise SystemExit(f"error: unknown db command {verb}")


# -- assets --------------------------------------------------------------------


# The asset store's own type list, as Claude Code maps extensions.
ASSET_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".pdf": "application/pdf",
    ".woff2": "font/woff2",
    ".woff": "font/woff",
    ".ttf": "font/ttf",
    ".otf": "font/otf",
    ".csv": "text/csv",
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".json": "application/json",
    ".txt": "text/plain",
    ".ts": "text/plain",
    ".css": "text/css",
    ".js": "text/javascript",
    ".mjs": "text/javascript",
    ".cjs": "text/javascript",
}
ASSET_MAX = 20 * 1024 * 1024
SVG_MAX = 2 * 1024 * 1024


def _asset_row(asset: dict) -> str:
    size = asset.get("size_bytes")
    size_s = f"{size:>10,}" if isinstance(size, int) else " " * 10
    when = (asset.get("created_at") or "")[:10]
    return f"{asset.get('opaque_id')}  {size_s}  {asset.get('content_type', ''):<18} {when}"


def _all_assets(client: api.FrameClient, slug: str) -> tuple[list[dict], dict]:
    assets: list[dict] = []
    after = None
    while True:
        data = client.assets(slug, after=after)
        assets.extend(data.get("assets") or [])
        after = data.get("next") or None
        if not after:
            return assets, data.get("usage") or {}


def cmd_assets(args) -> int:
    slug = api.slug_from(args.slug)
    client = _client(args)
    action = args.assets_command

    if action == "list":
        assets, usage = _all_assets(client, slug)
        if args.json:
            _print_json({"assets": assets, "usage": usage})
            return 0
        if not assets:
            print("No assets.")
        for asset in assets:
            print(_asset_row(asset))
        if usage:
            _eprint(
                f"  {usage.get('files')}/{usage.get('max_files')} files, "
                f"{usage.get('bytes', 0):,}/{usage.get('max_bytes', 0):,} bytes"
            )
        return 0

    if action == "upload":
        results = []
        for name in args.file:
            path = Path(name)
            if not path.is_file():
                raise SystemExit(f"error: no such file: {path}")
            content_type = args.type or ASSET_TYPES.get(path.suffix.lower())
            if not content_type:
                raise SystemExit(
                    f"error: {path.name}: the asset store takes images, SVG, video, PDF, fonts, "
                    "CSS/JS, CSV, Markdown, JSON or text; pass --type to override"
                )
            data = path.read_bytes()
            limit = SVG_MAX if content_type == "image/svg+xml" else ASSET_MAX
            if not data or len(data) > limit:
                raise SystemExit(
                    f"error: {path.name} is empty" if not data
                    else f"error: {path.name} exceeds the {limit >> 20} MiB limit"
                )
            reply = client.upload_asset(slug, data, content_type)
            results.append({**reply, "file": str(path)})
            if not args.json:
                print(f"{reply['opaque_id']}  {reply.get('url', '')}  {path.name}")
        if args.json:
            _print_json(results)
        return 0

    if action == "get":
        assets, _ = _all_assets(client, slug)
        entry = next((a for a in assets if a.get("opaque_id") == args.id), None)
        if entry is None:
            raise api.ApiError(f"no asset {args.id} on this artifact", 404)
        data = client.files(slug).fetch_asset(args.id, entry.get("sha256"))
        if args.out:
            try:
                Path(args.out).write_bytes(data)
            except OSError as exc:
                raise SystemExit(f"error: can't write {args.out}: {exc}") from None
            print(args.out)
        else:
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()
        return 0

    if action == "delete":
        for asset_id in args.id:
            reply = client.delete_asset(slug, asset_id)
            _eprint(f"{'deleted' if reply.get('deleted') else 'not found'} {asset_id}")
        return 0

    if action == "copy":
        source = api.slug_from(args.source)
        if source == slug:
            raise SystemExit("error: --from names the destination itself")
        ids = list(dict.fromkeys(args.id))
        if not 1 <= len(ids) <= 10:
            raise SystemExit("error: copy 1-10 assets at a time")
        reply = client.copy_assets(slug, source, ids)
        copied = reply.get("assets") or []
        if args.json:
            _print_json(copied)
        else:
            for asset in copied:
                print(f"{asset.get('from_id')} -> {asset.get('opaque_id')}  {asset.get('url', '')}")
        return 0

    raise SystemExit(f"error: unknown assets command {action}")


# -- types ---------------------------------------------------------------------


def _resolve_type(client: api.FrameClient, value: str) -> str:
    """A type by URL, UUID, short id or (case-insensitive) title, e.g. `slides`."""
    if "/" in value or _UUID_RE.match(value.strip().lower()):
        return api.slug_from(value)
    wanted = value.strip().lower()
    for t in client.types():
        if (t.get("title") or "").strip().lower() == wanted:
            return t["slug"]
    try:
        return api.slug_from(value)
    except api.ApiError:
        raise SystemExit(
            f"error: no type is named {value!r}; `claude-artifact types list` shows them"
        ) from None


def cmd_types(args) -> int:
    client = _client(args)
    action = args.types_command

    if action == "list":
        types = client.types(getattr(args, "query", None))
        if args.json:
            _print_json(types)
            return 0
        for t in types:
            print(f"{t.get('slug'):<38} {(t.get('title') or ''):<16} "
                  f"{_one_line(t.get('description') or '', 70)}")
        return 0

    slug = _resolve_type(client, args.type)
    if action == "show":
        info = client.type_info(slug)
        if args.json:
            _print_json(info)
            return 0
        print(f"{info.get('title')}  {info.get('favicon') or ''}".strip())
        print(f"  url         {info.get('url') or api.ARTIFACT_URL.format(slug=slug)}")
        print(f"  release     {(info.get('release') or {}).get('version')}")
        if info.get("description"):
            print(f"  about       {_one_line(info['description'], 90)}")
        files = info.get("files") or []
        if files:
            print(f"  files       {len(files)}")
            for f in files:
                size = f.get("size")
                size_s = f"{size:>9,}" if isinstance(size, int) else " " * 9
                print(f"    {size_s}  {f.get('content_type', ''):<20} {f.get('path')}")
        return 0

    if action == "read":
        data = client.type_file(slug, args.path)
        if args.json:
            _print_json(data)
            return 0
        sys.stdout.write(data.get("text") or "")
        if data.get("truncated"):
            _eprint(f"  truncated: {data.get('chars')} of {data.get('size_bytes')} bytes shown")
        return 0

    if action == "instances":
        rows = client.type_instances(slug, limit=args.limit, rel=args.scope)
        if args.json:
            _print_json(rows)
            return 0
        for row in rows:
            when = (row.get("published_at") or row.get("created_at") or "")[:10]
            print(f"{row.get('slug'):<38} {when:<11} {row.get('rel', ''):<7} {row.get('title') or ''}")
        return 0

    raise SystemExit(f"error: unknown types command {action}")


def cmd_create(args) -> int:
    client = _client(args)
    type_slug = _resolve_type(client, args.type)
    key = args.idempotency_key or secrets.token_hex(16)
    data = client.create_from_type(type_slug, args.title, key)
    url = api.ARTIFACT_URL.format(slug=data["slug"])
    if args.json:
        _print_json({"url": url, **data})
    else:
        print(url)
        if not args.quiet:
            _eprint(f"  version {data.get('version')}; fill it with "
                    f"`claude-artifact pull {data['slug']} DIR`, edit, then `publish DIR`")
    return 0


# -- contract --------------------------------------------------------------------


def cmd_contract(args) -> int:
    client = _client(args)
    data = client.contract(args.version)
    version = data.get("version") or args.version
    if args.out_dir and args.defs is None:
        raise SystemExit("error: --out-dir goes with --defs")
    if args.defs is not None:
        if not re.fullmatch(r"[0-9]{1,4}\.[0-9]{1,5}\.[0-9]{1,6}", str(version)):
            raise api.ApiError(f"the contract came back with an unusable version {version!r}")
        if args.prompt or args.json:
            raise SystemExit("error: --defs prints TypeScript; it doesn't combine with --prompt or --json")
        known = list(data.get("capabilities") or [])
        names = args.defs or (["claude"] if data.get("claude") else []) + known
        unknown = [n for n in names if n != "claude" and n not in known]
        if unknown:
            raise SystemExit(
                f"error: contract {version} has no capability {', '.join(unknown)}; "
                f"it supports: {', '.join(known) or '(none)'}"
            )
        for name in dict.fromkeys(names):
            defs = client.contract_defs(version, name)
            if args.out_dir:
                dest = _safe_dest(Path(args.out_dir), [version, f"{name}.d.ts"])
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(defs, encoding="utf-8")
                print(dest)
            else:
                sys.stdout.write(f"// ---- {version}/{name}.d.ts\n{defs.rstrip()}\n\n")
        return 0
    if args.prompt:
        sys.stdout.write(client.contract_prompt(version))
    elif args.json:
        _print_json(data)
    else:
        _print_contract(data)
    return 0


def _print_contract(data: dict) -> None:
    print(f"contract {data.get('version')}")
    print(f"  capabilities  {', '.join(data.get('capabilities') or [])}")
    if data.get("core"):
        print(f"  always on     {', '.join(data['core'])}")


# -- watch -----------------------------------------------------------------------


def _comment_events(data: dict, seen: set) -> list[dict]:
    events = []
    names = data.get("names") or {}
    for thread in data.get("threads") or []:
        if not isinstance(thread, dict):
            continue
        for comment in thread.get("comments") or []:
            key = (thread.get("id"), comment.get("id"))
            if key in seen:
                continue
            seen.add(key)
            events.append({
                "event": "comment",
                "thread": thread.get("id"),
                "comment": comment.get("id"),
                "author": _author(comment, names),
                "text": comment.get("text"),
                "to_claude": bool(comment.get("to_claude_at")),
                "awaiting_claude": awaiting_claude(thread),
            })
        resolved = thread.get("resolved_at")
        if resolved and (thread.get("id"), "resolved") not in seen:
            seen.add((thread.get("id"), "resolved"))
            events.append({"event": "resolved", "thread": thread.get("id"), "at": resolved})
    return events


def cmd_watch(args) -> int:
    """Poll for new versions and comments until interrupted (or --count events)."""
    slug = api.slug_from(args.slug)
    client = _client(args)
    seen: set = set()
    version = client.current_version(slug)
    if not args.no_comments:
        _comment_events(client.comments(slug), seen)
    _eprint(f"watching {api.ARTIFACT_URL.format(slug=slug)} at version {version} "
            f"(every {args.interval}s; Ctrl-C stops)")
    emitted = 0
    while True:
        time.sleep(args.interval)
        events = []
        try:
            live = client.current_version(slug)
            # Fetch comments before committing the version, so a failed comments call
            # doesn't swallow the version event.
            comments = None if args.no_comments else client.comments(slug)
            if live != version:
                events.append({"event": "version", "version": live, "previous": version})
                version = live
            if comments is not None:
                events.extend(_comment_events(comments, seen))
        except api.ApiError as exc:
            if exc.status == 404:
                raise
            _eprint(f"  poll failed, retrying: {exc}")
            continue
        for event in events:
            if args.json:
                print(json.dumps(event, ensure_ascii=False), flush=True)
            elif event["event"] == "version":
                print(f"version {event['version']}", flush=True)
            elif event["event"] == "resolved":
                print(f"resolved {event['thread']}", flush=True)
            else:
                flag = "  [to Claude]" if event["to_claude"] else ""
                print(f"comment {event['thread']} {event['author']}: "
                      f"{_one_line(event['text'] or '')}{flag}", flush=True)
            emitted += 1
            if args.count and emitted >= args.count:
                return 0
