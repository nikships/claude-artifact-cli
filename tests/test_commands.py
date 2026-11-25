"""Comments, the artifact database, assets, types, sharing and housekeeping."""

import base64
import contextlib
import hashlib
import io
import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from claude_artifact_cli import api, auth, cli, commands

# Never reach PyPI or upgrade the install running the tests.
os.environ.setdefault("CLAUDE_ARTIFACT_NO_UPDATE_CHECK", "1")

SLUG = "950a6035-8949-4101-99d6-132d4fa83da0"
OTHER = "746aadc3-288e-41e0-9fe3-4ea86134803c"
DEPLOY = ("POST", "/api/frame/deploy/direct")
DB = ("POST", "/api/frame/db/agent")
COMMENTS = ("GET", f"/api/frame/comments/{SLUG}?names=1")


def _strip_attempt(path):
    """Deploys carry ?attempt_id=…; the fakes route on the bare path."""
    return path.split("?")[0] if path.startswith("/api/frame/deploy/direct?") else path


class Server:
    """Stands in for FrameClient._request, keyword arguments included."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def __call__(self, method, path, body=None, **kw):
        path = _strip_attempt(path)
        self.calls.append((method, path, body, kw))
        if (method, path) not in self.routes:
            raise AssertionError(f"unexpected request {method} {path}")
        answer = self.routes[(method, path)]
        if callable(answer) and not isinstance(answer, Exception):
            answer = answer(body)
        if isinstance(answer, Exception):
            raise answer
        return answer

    def bodies(self, method, path):
        return [b for m, p, b, _ in self.calls if (m, p) == (method, path)]

    def kwargs(self, method, path):
        return [k for m, p, _, k in self.calls if (m, p) == (method, path)]


class Case(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        patcher = mock.patch.object(auth, "get_token", return_value="test-token")
        patcher.start()
        self.addCleanup(patcher.stop)

    def serve(self, routes):
        server = Server(routes)
        patcher = mock.patch.object(api.FrameClient, "_request", side_effect=server)
        patcher.start()
        self.addCleanup(patcher.stop)
        return server

    def run_cli(self, *argv, stdin=None):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.ExitStack() as stack:
            stack.enter_context(contextlib.redirect_stdout(out))
            stack.enter_context(contextlib.redirect_stderr(err))
            if stdin is not None:
                stack.enter_context(mock.patch("sys.stdin", io.StringIO(stdin)))
            try:
                code = cli.main([str(a) for a in argv])
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else 1
                err.write(str(exc.code))
        return code, out.getvalue(), err.getvalue()

    def write(self, rel, data):
        path = self.tmp / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data if isinstance(data, bytes) else data.encode())
        return path


def thread(tid, comments, activated=False, resolved=None):
    t = {"id": tid, "comments": comments, "anchor": {"label": "a heading"}}
    if activated:
        t["claude_activated_at"] = "2026-10-13T00:00:00Z"
    if resolved:
        t["resolved_at"] = resolved
    return t


def human(cid, text, to_claude=False):
    c = {"id": cid, "author": {"account": "u_me"}, "text": text, "created_at": "2026-10-13T15:00Z"}
    if to_claude:
        c["to_claude_at"] = "2026-10-13T15:00:00Z"
    return c


def claude(cid, text):
    return {"id": cid, "author": {"account": "u_me", "role": "assistant"}, "text": text}


# -- transport and errors ----------------------------------------------------


class ErrorTextTest(unittest.TestCase):
    def test_nested_error_body(self):
        body = {"error": {"code": "not_declared", "message": "no capability"}}
        self.assertEqual(api._describe(404, body), "HTTP 404: not_declared - no capability")
        self.assertEqual(api.error_code(body), "not_declared")

    def test_generic_hint_only_without_detail(self):
        self.assertIn("isn't yours", api._describe(404, None))
        self.assertEqual(api._describe(403, "Claude is not activated on this thread"),
                         "HTTP 403: Claude is not activated on this thread")

    def test_non_deploy_409_is_not_a_publish_conflict(self):
        client = api.FrameClient("t")
        err = api.urllib.error.HTTPError("u", 409, "c", {}, io.BytesIO(
            b'{"error":{"code":"version_mismatch","message":"changed"}}'))
        with mock.patch.object(api.urllib.request, "urlopen", side_effect=err), \
                self.assertRaises(api.ConflictError) as ctx:
            client._request("POST", "/api/frame/db/agent", {})
        self.assertNotIn("pull", str(ctx.exception))
        self.assertIn("version_mismatch", str(ctx.exception))

    def test_raw_body_and_headers_are_sent(self):
        client = api.FrameClient("t")
        sent = {}

        class Reply(io.BytesIO):
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_open(req, timeout):
            sent["data"], sent["headers"] = req.data, dict(req.header_items())
            return Reply(b'{"opaque_id":"' + b"a" * 32 + b'"}')

        with mock.patch.object(api.urllib.request, "urlopen", side_effect=fake_open):
            client.upload_asset(SLUG, b"\x89PNG", "image/png")
            client._request("PATCH", "/x", {"a": 1}, headers={"If-Match": "3"})
        self.assertEqual(sent["headers"]["If-match"], "3")
        self.assertEqual(sent["data"], b'{"a": 1}')


# -- comments ------------------------------------------------------------------


class CommentsTest(Case):
    def test_awaiting_claude(self):
        self.assertTrue(commands.awaiting_claude(
            thread("t", [human("c1", "hi", to_claude=True)], activated=True)))
        self.assertFalse(commands.awaiting_claude(
            thread("t", [human("c1", "hi", True), claude("c2", "ok")], activated=True)))
        self.assertTrue(commands.awaiting_claude(thread(
            "t", [human("c1", "hi", True), claude("c2", "ok"), human("c3", "again", True)],
            activated=True)))
        self.assertFalse(commands.awaiting_claude(
            thread("t", [human("c1", "hi", True)], activated=True, resolved="2026")))
        self.assertFalse(commands.awaiting_claude(thread("t", [human("c1", "hi")])))

    def test_list_hides_resolved_and_names_authors(self):
        self.serve({COMMENTS: {"threads": [
            thread("open1", [human("c1", "first\nline")]),
            thread("done1", [human("c2", "old")], resolved="2026-10-13"),
        ], "names": {"u_me": "Pat"}}})
        code, out, _ = self.run_cli("comments", "list", SLUG)
        self.assertEqual(code, 0)
        self.assertIn("open1", out)
        self.assertIn("Pat: first line", out)
        self.assertNotIn("done1", out)
        _, out, _ = self.run_cli("comments", "list", SLUG, "--all")
        self.assertIn("done1", out)

    def test_awaiting_filter_json(self):
        self.serve({COMMENTS: {"threads": [
            thread("wait", [human("c1", "?", True)], activated=True),
            thread("plain", [human("c2", "x")]),
        ]}})
        _, out, _ = self.run_cli("comments", "list", SLUG, "--awaiting", "--json")
        self.assertEqual([t["id"] for t in json.loads(out)["threads"]], ["wait"])

    def test_reply_answers_the_summons_with_client_id(self):
        path = ("POST", f"/api/frame/comments/{SLUG}/wait")
        server = self.serve({
            COMMENTS: {"threads": [thread("wait", [human("c1", "?", True)], activated=True)]},
            path: {"thread_id": "wait", "comment_id": "id-1", "payload": {"big": 1}},
        })
        code, out, _ = self.run_cli("comments", "reply", SLUG, "wait", "-", "--id", "id-1",
                                    stdin="Done.\n")
        self.assertEqual((code, out.strip()), (0, "id-1"))
        self.assertEqual(server.bodies(*path),
                         [{"id": "id-1", "text": "Done.", "answers_summon": True}])

    def test_reply_text_starting_with_at_is_literal(self):
        path = ("POST", f"/api/frame/comments/{SLUG}/wait")
        server = self.serve({
            COMMENTS: {"threads": [thread("wait", [human("c1", "?", True)], activated=True)]},
            path: {"thread_id": "wait", "comment_id": "x"},
        })
        code, _, _ = self.run_cli("comments", "reply", SLUG, "wait", "@Pat done", "--id", "x")
        self.assertEqual(code, 0)
        self.assertEqual(server.bodies(*path)[0]["text"], "@Pat done")

    def test_reply_refused_on_thread_not_sent_to_claude(self):
        server = self.serve({COMMENTS: {"threads": [thread("plain", [human("c", "x")])]}})
        code, _, err = self.run_cli("comments", "reply", SLUG, "plain", "hi")
        self.assertEqual(code, 1)
        self.assertIn("Send to Claude", err)
        self.assertEqual(len(server.calls), 1)

    def test_reply_replayed_says_so(self):
        path = ("POST", f"/api/frame/comments/{SLUG}/wait")
        self.serve({
            COMMENTS: {"threads": [thread("wait", [claude("c", "x")], activated=True)]},
            path: {"thread_id": "wait", "comment_id": "c", "replayed": True},
        })
        code, _, err = self.run_cli("comments", "reply", SLUG, "wait", "again", "--id", "c")
        self.assertEqual(code, 0)
        self.assertIn("already posted", err)

    def test_resolve_uses_session_route(self):
        path = ("POST", f"/api/frame/comments/{SLUG}/t1/session-resolve")
        server = self.serve({path: {"thread_id": "t1"}})
        code, _, _ = self.run_cli("comments", "resolve", SLUG, "t1")
        self.assertEqual(code, 0)
        self.assertEqual(server.bodies(*path), [{"resolved": True}])


# -- database ------------------------------------------------------------------


class DbTest(Case):
    def db_server(self, replies):
        """replies: verb -> answer (or callable taking args)."""
        def answer(body):
            self.assertEqual(body["slug"], SLUG)
            reply = replies[body["verb"]]
            return reply(body["args"]) if callable(reply) else reply
        return self.serve({DB: answer})

    def sent(self, server):
        return [(b["verb"], b["args"]) for b in server.bodies(*DB)]

    def test_get_prints_data(self):
        server = self.db_server({"get": {"exists": True, "data": {"a": 1}, "version": 2}})
        code, out, err = self.run_cli("db", "get", SLUG, "tasks", "t1")
        self.assertEqual((code, json.loads(out)), (0, {"a": 1}))
        self.assertIn("version 2", err)
        self.assertEqual(self.sent(server), [("get", {"path": "tasks/t1"})])

    def test_missing_document_exits_1(self):
        self.db_server({"get": {"exists": False}})
        code, _, err = self.run_cli("db", "get", SLUG, "tasks", "nope")
        self.assertEqual(code, 1)
        self.assertIn("no such document", err)

    def test_me_resolves_once(self):
        server = self.db_server({"whoami": {"id": "u_abc"}, "set": {"version": 1}})
        code, out, _ = self.run_cli("db", "set", SLUG, "data/users/me/d1/cards", "c1",
                                    "--data", '{"x": 1}')
        self.assertEqual((code, out.strip()), (0, "1"))
        self.assertEqual(self.sent(server), [
            ("whoami", {}),
            ("set", {"path": "data/users/u_abc/d1/cards/c1", "data": {"x": 1},
                     "replace": True}),
        ])

    def test_document_path_as_collection_is_refused(self):
        server = self.db_server({})
        code, _, err = self.run_cli("db", "list", SLUG, "tasks/t1")
        self.assertEqual(code, 1)
        self.assertIn("collection 'tasks' with doc id 't1'", err)
        self.assertEqual(server.calls, [])

    def test_query_maps_operators_and_pages(self):
        pages = iter([
            {"docs": [{"id": "a", "data": {}, "version": 1}], "nextCursor": "c2"},
            {"docs": [{"id": "b", "data": {}, "version": 1}], "nextCursor": ""},
        ])
        server = self.db_server({"query": lambda args: next(pages)})
        code, out, _ = self.run_cli("db", "query", SLUG, "tasks", "--where", "n", ">=", "2",
                                    "--where", "tag", "in", '["x","y"]', "--where", "s", "==", "hi",
                                    "--order-by", "n:desc", "--all", "--json")
        self.assertEqual(code, 0)
        self.assertEqual([d["id"] for d in json.loads(out)["docs"]], ["a", "b"])
        first, second = self.sent(server)
        self.assertEqual(first[1]["where"], [
            {"field": "n", "op": "gte", "value": 2},
            {"field": "tag", "op": "in", "value": ["x", "y"]},
            {"field": "s", "op": "eq", "value": "hi"},
        ])
        self.assertEqual(first[1]["orderBy"], {"field": "n", "direction": "desc"})
        self.assertEqual(second[1]["cursor"], "c2")

    def test_update_if_version_mismatch_exits_3(self):
        err = api.ConflictError("HTTP 409", 409, {"error": {"code": "version_mismatch",
                                                            "version": 4}})
        server = self.db_server({"update": lambda args: (_ for _ in ()).throw(err)})
        code, _, out = self.run_cli("db", "update", SLUG, "tasks", "t1", "--data", '{"n":1}',
                                    "--if-version", "2")
        self.assertEqual(code, 3)
        self.assertIn("version 4", out)
        self.assertEqual(self.sent(server)[0][1],
                         {"path": "tasks/t1", "data": {"n": 1}, "ifVersion": 2})

    def test_not_declared_suggests_capability(self):
        err = api.ApiError("x", 404, {"error": {"code": "not_declared"}})
        self.db_server({"list": lambda args: (_ for _ in ()).throw(err)})
        code, _, out = self.run_cli("db", "list", SLUG, "tasks")
        self.assertEqual(code, 1)
        self.assertIn("--capability db", out)

    def test_edit_sends_str_replace_with_idem(self):
        server = self.db_server({"str_replace": {"version": 3}})
        code, _, _ = self.run_cli("db", "edit", SLUG, "tasks", "t1", "--field", "title",
                                  "--old", "a", "--new", "b", "--replace-all")
        self.assertEqual(code, 0)
        verb, args = self.sent(server)[0]
        self.assertEqual(verb, "str_replace")
        self.assertRegex(args.pop("idem"), r"^[A-Za-z0-9_-]{22}$")
        self.assertEqual(args, {"path": "tasks/t1", "field": "title", "old_str": "a",
                                "new_str": "b", "replace_all": True})

    def test_batch(self):
        server = self.db_server({"batch": {"results": [{"version": 1}, {}]}})
        writes = [
            {"op": "set", "collection": "tasks", "doc_id": "a", "data": {"x": 1}},
            {"op": "delete", "collection": "tasks", "doc_id": "b", "if_version": 2},
        ]
        code, out, _ = self.run_cli("db", "batch", SLUG, json.dumps(writes))
        self.assertEqual(code, 0)
        self.assertEqual(self.sent(server), [("batch", {"ops": [
            {"op": "set", "path": "tasks/a", "data": {"x": 1}, "replace": True},
            {"op": "delete", "path": "tasks/b", "ifVersion": 2},
        ]})])
        self.assertIn("set     tasks/a  v1", out)

    def test_batch_rejects_bad_writes(self):
        self.db_server({})
        code, _, err = self.run_cli("db", "batch", SLUG, '[{"op":"merge"}]')
        self.assertEqual(code, 1)
        self.assertIn("op set, update or delete", err)
        twice = [{"op": "delete", "collection": "t", "doc_id": "a"}] * 2
        _, _, err = self.run_cli("db", "batch", SLUG, json.dumps(twice))
        self.assertIn("one write per document", err)
        _, _, err = self.run_cli("db", "batch", SLUG, json.dumps(twice * 26))
        self.assertIn("1-50 writes", err)

    def test_data_from_file(self):
        server = self.db_server({"set": {"version": 1}})
        doc = self.write("doc.json", '{"k": "v"}')
        code, _, _ = self.run_cli("db", "set", SLUG, "t", "a", "--data", f"@{doc}")
        self.assertEqual(code, 0)
        self.assertEqual(self.sent(server)[0][1]["data"], {"k": "v"})


# -- assets --------------------------------------------------------------------


class AssetsTest(Case):
    LIST = ("POST", f"/api/frame/blob/{SLUG}/agent-list")

    def test_upload_maps_extension_and_sends_raw(self):
        up = ("POST", f"/api/frame/blob/{SLUG}/agent-upload")
        server = self.serve({up: {"opaque_id": "a" * 32, "url": "/_blob/" + "a" * 32}})
        png = self.write("x.png", b"\x89PNG")
        code, out, _ = self.run_cli("assets", "upload", SLUG, png)
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("a" * 32))
        self.assertEqual(server.kwargs(*up), [{"raw": b"\x89PNG", "content_type": "image/png"}])

    def test_upload_refuses_unknown_type_and_oversize_svg(self):
        self.serve({})
        code, _, err = self.run_cli("assets", "upload", SLUG, self.write("x.exe", b"MZ"))
        self.assertEqual(code, 1)
        self.assertIn("--type", err)
        code, _, err = self.run_cli(
            "assets", "upload", SLUG, self.write("big.svg", b"<" * (2 * 1024 * 1024 + 1)))
        self.assertIn("2 MiB", err)

    def test_list_follows_pages(self):
        pages = iter([
            {"assets": [{"opaque_id": "a" * 32}], "next": "n2", "usage": {}},
            {"assets": [{"opaque_id": "b" * 32}], "usage": {"files": 2}},
        ])
        server = self.serve({self.LIST: lambda body: next(pages)})
        _, out, _ = self.run_cli("assets", "list", SLUG, "--json")
        self.assertEqual([a["opaque_id"] for a in json.loads(out)["assets"]], ["a" * 32, "b" * 32])
        self.assertEqual(server.bodies(*self.LIST), [{"limit": 50}, {"limit": 50, "after": "n2"}])

    def test_get_checks_sha256(self):
        data = b"hello"
        self.serve({self.LIST: {"assets": [
            {"opaque_id": "a" * 32, "sha256": hashlib.sha256(data).hexdigest()}]}})
        reader = mock.MagicMock()
        reader.fetch_asset.return_value = data
        with mock.patch.object(api.FrameClient, "files", return_value=reader):
            _, _, _ = self.run_cli("assets", "get", SLUG, "a" * 32, "-o", self.tmp / "o")
        self.assertEqual((self.tmp / "o").read_bytes(), data)
        reader.fetch_asset.assert_called_once_with("a" * 32, hashlib.sha256(data).hexdigest())

    def test_fetch_asset_rejects_bad_bytes(self):
        reader = api.FileReader.__new__(api.FileReader)
        reader.version = "v1"
        with mock.patch.object(api.FileReader, "_get", return_value=(200, b"x")) as get:
            self.assertEqual(reader.fetch_asset("a" * 32), b"x")
            get.assert_called_with(f"/_f/v1/_blob/{'a' * 32}")
            with self.assertRaises(api.ApiError):
                reader.fetch_asset("a" * 32, "0" * 64)
            with self.assertRaises(api.ApiError):
                reader.fetch_asset("../x")

    def test_copy_and_delete(self):
        copy = ("POST", f"/api/frame/blob/{SLUG}/agent-copy")
        delete = ("POST", f"/api/frame/blob/{SLUG}/{'a' * 32}/agent-delete")
        server = self.serve({
            copy: {"assets": [{"from_id": "b" * 32, "opaque_id": "c" * 32}]},
            delete: {"deleted": True},
        })
        _, out, _ = self.run_cli("assets", "copy", SLUG, "--from", OTHER, "b" * 32)
        self.assertEqual(server.bodies(*copy), [{"from": OTHER, "ids": ["b" * 32]}])
        self.assertIn("c" * 32, out)
        _, _, err = self.run_cli("assets", "delete", SLUG, "a" * 32)
        self.assertIn("deleted", err)


# -- types ---------------------------------------------------------------------


TYPES = ("GET", "/api/frame/types")
SLIDES = "3e9dc3d6-8a10-4143-ac0b-5af4f2b03a61"


class TypesTest(Case):
    def test_type_by_title(self):
        server = self.serve({
            TYPES: {"types": [{"slug": SLIDES, "title": "Slides"}]},
            ("GET", f"/api/frame/types/{SLIDES}/files/SKILL.md"): {"text": "# Slides\n"},
        })
        code, out, _ = self.run_cli("types", "read", "slides", "SKILL.md")
        self.assertEqual((code, out), (0, "# Slides\n"))
        self.assertEqual(len(server.calls), 2)

    def test_unknown_type(self):
        self.serve({TYPES: {"types": []}})
        code, _, err = self.run_cli("types", "show", "nope!")
        self.assertEqual(code, 1)
        self.assertIn("types list", err)

    def test_create_sends_idempotency_key(self):
        path = ("POST", f"/api/frame/types/{SLIDES}/create?idempotency_key=k1")
        server = self.serve({path: {"slug": SLUG, "version": "v1"}})
        code, out, _ = self.run_cli("create", SLIDES, "--title", "Deck", "--idempotency-key",
                                    "k1", "-q")
        self.assertEqual((code, out.strip()), (0, api.ARTIFACT_URL.format(slug=SLUG)))
        self.assertEqual(server.bodies(*path), [{"target_type_version": None, "title": "Deck"}])


# -- publish: capabilities, contract, copies -------------------------------------


class PublishExtrasTest(Case):
    def page(self):
        return self.write("page.html", "<title>P</title>")

    def test_capabilities_use_the_stored_contract(self):
        server = self.serve({
            ("GET", f"/api/frame/read/{SLUG}"): {"contract": "0.2.70"},
            ("GET", "/api/frame/contract/0.2.70"): {"version": "0.2.70",
                                                    "capabilities": ["db", "user", "downloads"]},
            DEPLOY: {"slug": SLUG},
        })
        code, _, _ = self.run_cli("publish", self.page(), "--slug", SLUG, "--base-version", "v1",
                                  "--capability", "db", "--capability", "user",
                                  "--capability", "downloads=true", "-q")
        self.assertEqual(code, 0)
        body = server.bodies(*DEPLOY)[0]
        self.assertEqual(body["contract"], "0.2.70")
        self.assertEqual(body["capabilities"], {"db": {}, "user": {}, "downloads": True})

    def test_download_link_without_downloads_capability_warns(self):
        page = self.write("dl.html", '<title>P</title><a href="deck.pptx" download>Deck</a>')
        self.serve({
            ("GET", f"/api/frame/read/{SLUG}"): {"contract": "0.2.70", "capabilities": {}},
            DEPLOY: {"slug": SLUG},
        })
        code, out, err = self.run_cli("publish", page, "--slug", SLUG, "--base-version", "v1",
                                      "-q")
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), api.ARTIFACT_URL.format(slug=SLUG))
        self.assertIn("--capability downloads", err)

    def test_download_link_with_stored_downloads_capability_is_quiet(self):
        page = self.write("dl.html", '<title>P</title><a download href="deck.pptx">Deck</a>')
        self.serve({
            ("GET", f"/api/frame/read/{SLUG}"): {"contract": "0.2.98",
                                                 "capabilities": {"downloads": {}}},
            DEPLOY: {"slug": SLUG},
        })
        code, _, err = self.run_cli("publish", page, "--slug", SLUG, "--base-version", "v1")
        self.assertEqual(code, 0)
        self.assertNotIn("warning", err)

    def test_download_link_declared_on_this_publish_is_quiet(self):
        page = self.write("dl.html", '<title>P</title><A HREF="x.csv" DOWNLOAD>CSV</A>')
        self.serve({
            ("GET", "/api/frame/contract/latest"): {"version": "0.2.98",
                                                    "capabilities": ["downloads"]},
            DEPLOY: {"slug": SLUG},
        })
        code, _, err = self.run_cli("publish", page, "--capability", "downloads", "-q")
        self.assertEqual(code, 0)
        self.assertNotIn("warning", err)

    def test_new_artifact_with_download_link_warns(self):
        page = self.write("dl.html", '<title>P</title><a class="b" download="r.pdf">PDF</a>')
        self.serve({DEPLOY: {"slug": SLUG}})
        code, _, err = self.run_cli("publish", page, "-q")
        self.assertEqual(code, 0)
        self.assertIn("File downloads are not available", err)

    def test_page_without_download_link_never_reads_access(self):
        self.serve({DEPLOY: {"slug": SLUG}})  # an access read would be an unexpected request
        code, _, err = self.run_cli("publish", self.page(), "--slug", SLUG, "--base-version", "v1",
                                    "-q")
        self.assertEqual(code, 0)
        self.assertNotIn("warning", err)

    def test_new_artifact_uses_latest_and_refuses_unknown(self):
        server = self.serve({
            ("GET", "/api/frame/contract/latest"): {"version": "0.2.76", "capabilities": ["db"]},
            DEPLOY: {"slug": SLUG},
        })
        code, _, err = self.run_cli("publish", self.page(), "--capability", "dbx")
        self.assertEqual(code, 1)
        self.assertIn("no capability dbx", err)
        code, _, _ = self.run_cli("publish", self.page(), "--capability", "db", "-q")
        self.assertEqual(server.bodies(*DEPLOY)[0]["contract"], "0.2.76")

    def test_clear_capabilities_and_extras(self):
        server = self.serve({
            ("GET", f"/api/frame/read/{SLUG}"): {"contract": "0.0.0"},
            ("GET", "/api/frame/contract/latest"): {"version": "0.2.76", "capabilities": []},
            DEPLOY: {"slug": SLUG},
        })
        thumb = self.write("t.png", b"\x89PNG")
        code, _, _ = self.run_cli("publish", self.page(), "--slug", SLUG, "--base-version", "v1",
                                  "--clear-capabilities", "--note", "n1", "--thumbnail", thumb,
                                  "-q")
        self.assertEqual(code, 0)
        body = server.bodies(*DEPLOY)[0]
        self.assertEqual((body["capabilities"], body["contract"], body["note"]),
                         ({}, "0.2.76", "n1"))
        self.assertEqual(base64.b64decode(body["thumbnail"]), b"\x89PNG")

    def test_copy_from_another_artifact(self):
        reader = mock.MagicMock(version="v9", files={
            "img/a.png": {"path": "img/a.png", "sha256": "f" * 64, "contentType": "image/png"}})
        server = self.serve({
            ("POST", "/api/frame/deploy/prepare"): {"slug": SLUG, "missing": []},
            ("POST", "/api/frame/upload"): {},
            DEPLOY: {"slug": SLUG},
        })
        with mock.patch.object(api.FrameClient, "files", return_value=reader):
            code, _, _ = self.run_cli("publish", self.page(), "--copy",
                                      api.ARTIFACT_URL.format(slug=OTHER), "pics/a.png=img/a.png",
                                      "-q")
        self.assertEqual(code, 0)
        self.assertEqual(server.bodies("POST", "/api/frame/upload"), [{"slug": SLUG, "files": [
            {"path": "pics/a.png", "from": {"slug": OTHER, "path": "img/a.png", "ver": "v9"}}]}])
        body = server.bodies(*DEPLOY)[0]
        self.assertEqual(body["slug"], SLUG)
        self.assertEqual(body["manifest"]["pics/a.png"],
                         {"sha256": "f" * 64, "contentType": "image/png"})

    def test_copy_of_missing_file(self):
        reader = mock.MagicMock(version="v9", files={})
        self.serve({})
        with mock.patch.object(api.FrameClient, "files", return_value=reader):
            code, _, err = self.run_cli("publish", self.page(), "--copy", OTHER, "x.css")
        self.assertEqual(code, 1)
        self.assertIn("no file 'x.css'", err)


# -- sharing, housekeeping, watch ------------------------------------------------


PERM = ("GET", f"/api/frame/perm/{SLUG}")
PATCH = ("PATCH", f"/api/frame/perm/{SLUG}")
ALICE = "11111111-2222-4333-8444-555555555555"


class ShareTest(Case):
    def perm(self, **extra):
        data = {"read": {"mode": "owner"}, "version": "3", "owner_account": "owner-uuid",
                "commentersEnabled": True, "viewersEnabled": True}
        data.update(extra)
        return data

    def test_show(self):
        self.serve({PERM: self.perm(read={"mode": "org"}, orgReadLevel="interact")})
        _, out, _ = self.run_cli("share", SLUG)
        self.assertIn("organization", out)

    def test_org_sends_if_match(self):
        server = self.serve({PERM: self.perm(), PATCH: self.perm(read={"mode": "org"})})
        code, _, _ = self.run_cli("share", SLUG, "--org")
        self.assertEqual(code, 0)
        self.assertEqual(server.bodies(*PATCH), [{"read": {"mode": "org", "users": []}}])
        self.assertEqual(server.kwargs(*PATCH), [{"headers": {"If-Match": "3"}}])

    def test_private_when_already_private_changes_nothing(self):
        server = self.serve({PERM: self.perm()})
        code, _, err = self.run_cli("share", SLUG, "--private")
        self.assertEqual(code, 0)
        self.assertIn("nothing to change", err)
        self.assertEqual(server.bodies(*PATCH), [])

    def test_add_and_remove_people(self):
        server = self.serve({PERM: self.perm(), PATCH: self.perm()})
        self.run_cli("share", SLUG, "--add", ALICE.upper(), "--access", "view")
        self.assertEqual(server.bodies(*PATCH)[0], {
            "read": {"mode": "users", "users": [ALICE]}, "viewers": [ALICE]})
        server = self.serve({
            PERM: self.perm(read={"mode": "users", "users": [ALICE]}, commenters=[ALICE]),
            PATCH: self.perm()})
        self.run_cli("share", SLUG, "--remove", ALICE)
        self.assertEqual(server.bodies(*PATCH)[0], {
            "read": {"mode": "owner", "users": []}, "commenters": []})

    def test_remove_on_public_artifact_refuses(self):
        server = self.serve({PERM: self.perm(read={"mode": "public"}), PATCH: self.perm()})
        code, _, err = self.run_cli("share", SLUG, "--remove", ALICE)
        self.assertEqual(code, 1)
        self.assertIn("public", err)
        self.assertEqual(server.bodies(*PATCH), [])

    def test_bad_uuid_and_conflicting_flags(self):
        self.serve({PERM: self.perm()})
        _, _, err = self.run_cli("share", SLUG, "--add", "alice@example.com")
        self.assertIn("account UUID", err)
        _, _, err = self.run_cli("share", SLUG, "--org", "--private")
        self.assertIn("pick one", err)


class HousekeepingTest(Case):
    def test_delete_needs_yes(self):
        server = self.serve({("DELETE", f"/api/frame/{SLUG}"): None})
        code, _, err = self.run_cli("delete", SLUG)
        self.assertEqual(code, 1)
        self.assertIn("--yes", err)
        self.assertEqual(server.calls, [])
        code, _, _ = self.run_cli("delete", SLUG, "--yes")
        self.assertEqual((code, len(server.calls)), (0, 1))

    def test_pin_unpin_rename(self):
        fav = f"/api/frame/favorite/{SLUG}"
        server = self.serve({("POST", fav): None, ("DELETE", fav): None,
                             ("POST", f"/api/frame/retitle/{SLUG}"): {"title": "New", "version": "v2"}})
        self.run_cli("pin", SLUG)
        self.run_cli("unpin", SLUG)
        _, out, _ = self.run_cli("rename", SLUG, "New")
        self.assertEqual(out.strip(), "New")
        self.assertEqual([c[0] for c in server.calls], ["POST", "DELETE", "POST"])

    def test_versions_marks_live_and_redacts(self):
        self.serve({("GET", f"/api/frame/versions/{SLUG}"): {
            "live": "v2", "history": ["v2", "v1"],
            "versions": {"v2": {"title": "T", "note": "fix", "thumb_img_url": "x?__frame_t=SECRET"}}}})
        _, out, _ = self.run_cli("versions", SLUG)
        self.assertTrue(out.startswith("* v2"))
        self.assertIn("(fix)", out)
        _, out, _ = self.run_cli("versions", SLUG, "--json")
        self.assertNotIn("SECRET", out)

    def test_list_pinned(self):
        path = "/api/frame/artifacts?rel=mine&kind=page%2Cdocument&sort=last_viewed&limit=200"
        self.serve({("GET", path): {"artifacts": [
            {"kind": "page", "id": "a", "title": "A", "rel": "mine", "starred": True},
            {"kind": "page", "id": "b", "title": "B", "rel": "mine"}]}})
        _, out, _ = self.run_cli("list", "--pinned")
        self.assertIn("A", out)
        self.assertNotIn(" B", out)


class WatchTest(unittest.TestCase):
    def test_comment_events_are_new_only(self):
        seen = set()
        data = {"threads": [thread("t", [human("c1", "hi", True)], activated=True)]}
        self.assertEqual(len(commands._comment_events(data, seen)), 1)
        self.assertEqual(commands._comment_events(data, seen), [])
        data["threads"][0]["comments"].append(claude("c2", "ok"))
        data["threads"][0]["resolved_at"] = "2026"
        events = commands._comment_events(data, seen)
        self.assertEqual([e["event"] for e in events], ["comment", "resolved"])
        self.assertEqual(events[0]["author"], "Claude")

    def test_watch_emits_version_then_stops_at_count(self):
        versions = iter(["v1", "v2"])
        client = mock.MagicMock()
        client.current_version.side_effect = lambda slug: next(versions)
        client.comments.return_value = {"threads": []}
        args = mock.MagicMock(slug=SLUG, interval=0, count=1, no_comments=False, json=True)
        out = io.StringIO()
        with mock.patch.object(commands, "_client", return_value=client), \
                mock.patch.object(commands.time, "sleep"), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(commands.cmd_watch(args), 0)
        self.assertEqual(json.loads(out.getvalue()),
                         {"event": "version", "version": "v2", "previous": "v1"})

    def test_failed_comments_poll_keeps_version_event(self):
        versions = iter(["v1", "v2", "v2"])
        client = mock.MagicMock()
        client.current_version.side_effect = lambda slug: next(versions)
        client.comments.side_effect = [
            {"threads": []}, api.ApiError("boom", 503), {"threads": []}]
        args = mock.MagicMock(slug=SLUG, interval=0, count=1, no_comments=False, json=True)
        out = io.StringIO()
        with mock.patch.object(commands, "_client", return_value=client), \
                mock.patch.object(commands.time, "sleep"), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(commands.cmd_watch(args), 0)
        self.assertEqual(json.loads(out.getvalue()),
                         {"event": "version", "version": "v2", "previous": "v1"})


class ParserTest(unittest.TestCase):
    def test_every_new_command_has_help(self):
        for argv in (["comments", "list", "-h"], ["db", "query", "-h"], ["assets", "upload", "-h"],
                     ["types", "show", "-h"], ["create", "-h"], ["contract", "-h"],
                     ["share", "-h"], ["versions", "-h"], ["watch", "-h"], ["delete", "-h"]):
            with contextlib.redirect_stdout(io.StringIO()) as out, self.assertRaises(SystemExit):
                cli.build_parser().parse_args(argv)
            self.assertTrue(re.search(r"usage:", out.getvalue()))


if __name__ == "__main__":
    unittest.main()
