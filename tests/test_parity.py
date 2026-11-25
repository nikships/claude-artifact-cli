"""Behavior that mirrors Claude Code 2.1.332: publish retries and chunking, content
types, paged listing, type search, contract definitions, the database's access
levels and people, and comment-reply guards."""

import io
import json
import os
import urllib.error
import urllib.parse
from unittest import mock

from claude_artifact_cli import api, commands

# Never reach PyPI or upgrade the install running the tests.
os.environ.setdefault("CLAUDE_ARTIFACT_NO_UPDATE_CHECK", "1")

from test_commands import COMMENTS, DB, SLUG, Case, claude, human, thread


def page(html="<title>T</title>hi"):
    return api.Asset("index.html", html.encode(), "text/html")


class Deploys:
    """Stands in for FrameClient._request on /deploy/direct; answers in order."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    def __call__(self, method, path, body=None, **kw):
        assert method == "POST" and path.startswith("/api/frame/deploy/direct?"), path
        query = dict(urllib.parse.parse_qsl(path.split("?", 1)[1]))
        self.calls.append((query, json.loads(json.dumps(body))))
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, Exception):
            raise answer
        return answer


class DeployCase(Case):
    def deploys(self, *answers):
        fake = Deploys(*answers)
        patcher = mock.patch.object(api.FrameClient, "_request", side_effect=fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        sleep = mock.patch.object(api.time, "sleep")
        self.sleep = sleep.start()
        self.addCleanup(sleep.stop)
        return fake


class RetryTest(DeployCase):
    def test_every_send_carries_one_attempt_id(self):
        fake = self.deploys({"slug": "s1", "version": "v1"})
        api.FrameClient("t").publish(page(), meta={"title": "T"})
        (query, _), = fake.calls
        self.assertRegex(query["attempt_id"], r"^[0-9a-f-]{36}$")
        self.assertNotIn("resend", query)

    def test_no_answer_resends_once_marked_as_a_resend(self):
        fake = self.deploys(
            api.NetworkError("network error: reset"),
            {"slug": "s1", "version": "v2", "replayed": True},
        )
        result = api.FrameClient("t").publish(page(), slug="s1", base_version="v1")
        self.assertEqual(result["version"], "v2")
        (first, _), (second, body) = fake.calls
        self.assertEqual(first["attempt_id"], second["attempt_id"])
        self.assertNotIn("resend", first)
        self.assertEqual(second["resend"], "1")
        self.assertEqual(body["slug"], "s1")

    def test_a_new_artifact_is_not_resent_blind(self):
        fake = self.deploys(api.NetworkError("network error: reset"))
        with self.assertRaises(api.NetworkError) as ctx:
            api.FrameClient("t").publish(page(), meta={"title": "T"})
        self.assertEqual(len(fake.calls), 1)
        self.assertIn("claude-artifact list", str(ctx.exception))

    def test_publish_settling_waits_and_resends(self):
        settling = api.ApiError("HTTP 409", 409, {"error": "x", "reason": "publish_settling"}, 3)
        fake = self.deploys(
            api.NetworkError("reset"), settling, {"slug": "s1", "version": "v2", "replayed": True}
        )
        api.FrameClient("t").publish(page(), slug="s1", base_version="v1")
        self.assertEqual(len(fake.calls), 3)
        self.sleep.assert_called_once_with(3)
        self.assertEqual(fake.calls[2][0]["resend"], "1")

    def test_settling_is_not_a_version_conflict(self):
        err = urllib.error.HTTPError(
            "u", 409, "c", {"Retry-After": "1"},
            io.BytesIO(b'{"error":"still storing","reason":"publish_settling"}'),
        )
        self.addCleanup(err.close)
        with mock.patch.object(api.urllib.request, "urlopen", side_effect=err), \
                self.assertRaises(api.ApiError) as ctx:
            api.FrameClient("t")._request("POST", "/api/frame/deploy/direct?attempt_id=a", {})
        self.assertNotIsInstance(ctx.exception, api.ConflictError)
        self.assertEqual(ctx.exception.retry_after, 1.0)

    def test_503_retries_with_backoff_then_gives_up(self):
        busy = api.ApiError("HTTP 503", 503, {"error": "busy"})
        fake = self.deploys(busy)
        with self.assertRaises(api.ApiError):
            api.FrameClient("t").publish(page(), slug="s1", base_version="v1")
        self.assertEqual(len(fake.calls), 3)
        self.assertEqual([c.args[0] for c in self.sleep.call_args_list], [5.0, 10.0])
        self.assertEqual(fake.calls[1][0].get("resend"), "1")

    def test_a_stranded_503_is_not_retried(self):
        fake = self.deploys(api.ApiError("HTTP 503", 503, {"slug": "s1", "live": "v1"}))
        with self.assertRaises(api.ApiError):
            api.FrameClient("t").publish(page(), slug="s1", base_version="v1")
        self.assertEqual(len(fake.calls), 1)

    def test_a_plan_cap_is_reported_not_retried(self):
        fake = self.deploys(api.ApiError("HTTP 429", 429, {"error": "daily_pushes"}, 5))
        with self.assertRaises(api.ApiError) as ctx:
            api.FrameClient("t").publish(page(), slug="s1", base_version="v1")
        self.assertEqual(len(fake.calls), 1)
        self.assertIn("resets at UTC midnight", str(ctx.exception))

    def test_a_short_429_is_retried_once(self):
        fake = self.deploys(api.ApiError("HTTP 429", 429, {"error": "slow down"}, 2),
                            {"slug": "s1", "version": "v2"})
        api.FrameClient("t").publish(page(), slug="s1", base_version="v1")
        self.assertEqual(len(fake.calls), 2)
        self.sleep.assert_called_once_with(2)


class ChunkTest(DeployCase):
    def test_more_than_255_entries_go_as_chained_patches(self):
        fake = self.deploys(
            {"slug": "new", "version": "v1"},
            {"slug": "new", "version": "v2"},
        )
        extra = [api.Asset(f"f/{i}.txt", b"x", "text/plain") for i in range(300)]
        result = api.FrameClient("t").publish(
            page(), extra=extra, meta={"title": "T", "thumbnail": "QQ=="}
        )
        self.assertEqual(result["version"], "v2")
        (_, first), (_, second) = fake.calls
        self.assertEqual(len(first["manifest"]), 255)
        self.assertIn("index.html", first["manifest"])
        self.assertEqual(first["mode"], "replace")
        self.assertEqual(first["thumbnail"], "QQ==")
        self.assertEqual(len(second["manifest"]), 46)
        self.assertEqual((second["mode"], second["slug"], second["baseVersion"]),
                         ("patch", "new", "v1"))
        self.assertNotIn("thumbnail", second)

    def test_a_failed_later_part_says_what_is_live(self):
        self.deploys({"slug": "new1", "version": "v2"}, api.ApiError("HTTP 500", 500))
        extra = [api.Asset(f"f/{i}.txt", b"x", "text/plain") for i in range(300)]
        with self.assertRaises(api.ApiError) as ctx:
            api.FrameClient("t").publish(page(), extra=extra, meta={"title": "T"})
        message = str(ctx.exception)
        self.assertIn("only 1 of 2", message)
        self.assertIn("v2", message)
        self.assertIn("--slug new1", message)
        self.assertIn("may have landed", message)


class TransportTest(DeployCase):
    def test_a_truncated_body_is_no_answer(self):
        class Truncated:
            status = 200
            headers = None

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                raise api.http.client.IncompleteRead(b"{")

        with mock.patch.object(api.urllib.request, "urlopen", return_value=Truncated()), \
                self.assertRaises(api.NetworkError):
            api.FrameClient("t")._request("GET", "/api/frame/x")

    def test_an_unreadable_error_body_keeps_its_status(self):
        err = urllib.error.HTTPError("u", 503, "c", {}, None)
        with mock.patch.object(err, "read", side_effect=ConnectionResetError("reset")), \
                mock.patch.object(api.urllib.request, "urlopen", side_effect=err), \
                self.assertRaises(api.ApiError) as ctx:
            api.FrameClient("t")._request("GET", "/api/frame/x")
        self.assertNotIsInstance(ctx.exception, api.NetworkError)
        self.assertEqual(ctx.exception.status, 503)

    def test_non_utf8_text_is_an_error_naming_the_file(self):
        with self.assertRaises(api.ApiError) as ctx:
            api.Asset("notes.log", b"\xff\xfe", "text/plain").wire()
        self.assertIn("notes.log", str(ctx.exception))


class ContentTypeTest(DeployCase):
    def test_extension_map_comes_first(self):
        for name, expected in (
            ("a.md", "text/markdown"),
            ("m.webmanifest", "application/manifest+json"),
            ("x.MJS", "text/javascript"),
            ("i.avif", "image/avif"),
            ("f.woff2", "font/woff2"),
            ("d.geojson", "application/json"),
            ("dir.v2/README", "application/octet-stream"),
        ):
            self.assertEqual(api.guess_content_type(name), expected, name)

    def test_content_type_override_and_unserved_warning(self):
        fake = self.deploys({"slug": "s1", "version": "v1"})
        site = self.tmp / "p.html"
        site.write_text("<title>P</title>")
        self.write("notes.log", "x")
        self.write("blob.bin", "y")
        code, _, err = self.run_cli(
            "publish", site, "--root", self.tmp, "--file", "notes.log", "--file", "blob.bin",
            "--content-type", "notes.log=text/plain",
        )
        self.assertEqual(code, 0, err)
        manifest = fake.calls[0][1]["manifest"]
        self.assertEqual(manifest["notes.log"]["contentType"], "text/plain")
        self.assertIn("blob.bin goes as application/octet-stream", err)
        self.assertNotIn("notes.log goes", err)

    def test_content_type_must_name_a_file(self):
        site = self.write("p.html", "<title>P</title>")
        code, _, err = self.run_cli("publish", site, "--content-type", "nope.txt=text/plain")
        self.assertNotEqual(code, 0)
        self.assertIn("names no file", err)


class EditAndPinTest(DeployCase):
    def test_edit_publishes_and_saves_the_change(self):
        fake = self.deploys({"slug": "s1", "version": "v2"})
        site = self.write("p.html", "<title>P</title><p>teh cat</p>")
        with mock.patch.object(api.FrameClient, "pin") as pin:
            code, _, err = self.run_cli(
                "publish", site, "--slug", "s1", "--base-version", "v1",
                "--edit", "teh", "the", "--edit", "cat", "dog", "--pin",
            )
        self.assertEqual(code, 0, err)
        sent = fake.calls[0][1]["manifest"]["index.html"]["content"]
        self.assertEqual(sent, "<title>P</title><p>the dog</p>")
        self.assertEqual(site.read_text(), sent)
        pin.assert_called_once_with("s1", True)

    def test_an_edit_that_is_not_unique_publishes_nothing(self):
        fake = self.deploys({"slug": "s1", "version": "v2"})
        site = self.write("p.html", "<p>a a</p>")
        code, _, err = self.run_cli("publish", site, "--slug", "s1", "--edit", "a", "b")
        self.assertNotEqual(code, 0)
        self.assertIn("occurs more than once", err)
        # Overlapping matches count too: "aa" sits twice in "aaa".
        self.write("p.html", "aaa")
        code, _, err = self.run_cli("publish", site, "--slug", "s1", "--edit", "aa", "X")
        self.assertIn("occurs more than once", err)
        code, _, err = self.run_cli("publish", site, "--slug", "s1", "--edit", "zz", "X")
        self.assertIn("is not in", err)
        self.assertEqual(fake.calls, [])
        self.assertEqual(site.read_text(), "aaa")


class ListingTest(Case):
    def test_types_query_and_instance_scope(self):
        server = self.serve({
            ("GET", "/api/frame/types?q=deck+slides"): {"types": [
                {"slug": "t1", "title": "Slides", "description": "Decks"}]},
            ("GET", f"/api/frame/types/{SLUG}/instances?limit=50&rel=mine"): {"instances": [
                {"slug": "a1", "title": "Q3", "rel": "mine"}]},
        })
        code, out, _ = self.run_cli("types", "list", "--query", "deck slides")
        self.assertEqual(code, 0)
        self.assertIn("Slides", out)
        code, out, _ = self.run_cli("types", "instances", SLUG, "--scope", "mine")
        self.assertEqual(code, 0)
        self.assertIn("Q3", out)
        self.assertEqual(len(server.calls), 2)

    def test_contract_defs(self):
        self.serve({
            ("GET", "/api/frame/contract/latest"): {
                "version": "0.2.99", "capabilities": ["db", "user"], "claude": True},
            ("GET", "/api/frame/contract/0.2.99/claude.d.ts"): "declare const claude: {};\n",
            ("GET", "/api/frame/contract/0.2.99/db.d.ts"): "interface Db {}\n",
            ("GET", "/api/frame/contract/0.2.99/user.d.ts"): "interface User {}\n",
        })
        code, out, _ = self.run_cli("contract", "--defs", "db")
        self.assertEqual(code, 0)
        self.assertIn("0.2.99/db.d.ts", out)
        self.assertIn("interface Db", out)
        self.assertNotIn("User", out)
        code, out, _ = self.run_cli("contract", "--defs", "--out-dir", self.tmp)
        self.assertEqual(code, 0)
        self.assertEqual(
            sorted(p.name for p in (self.tmp / "0.2.99").iterdir()),
            ["claude.d.ts", "db.d.ts", "user.d.ts"],
        )
        code, _, err = self.run_cli("contract", "--defs", "nope")
        self.assertNotEqual(code, 0)
        self.assertIn("no capability nope", err)

    def test_contract_defs_refuse_an_unsafe_version(self):
        self.serve({("GET", "/api/frame/contract/latest"): {
            "version": "../../etc", "capabilities": ["db"]}})
        code, _, err = self.run_cli("contract", "--defs", "--out-dir", self.tmp)
        self.assertEqual(code, 1)
        self.assertIn("unusable version", err)
        self.assertEqual(list(self.tmp.iterdir()), [])


class DbParityTest(Case):
    def test_as_level_and_require_version_go_on_the_wire(self):
        server = self.serve({DB: {"version": 3}})
        code, _, err = self.run_cli(
            "db", "set", SLUG, "tasks", "t1", "--data", "{}", "--as-level", "interact",
            "--require-version",
        )
        self.assertEqual(code, 0, err)
        body = server.bodies(*DB)[0]
        self.assertEqual((body["asLevel"], body["requireVersion"]), ("interact", True))

    def test_reads_never_send_require_version(self):
        server = self.serve({DB: {"exists": True, "data": {}, "version": 1}})
        self.run_cli("db", "get", SLUG, "tasks", "t1", "--as-level", "view")
        body = server.bodies(*DB)[0]
        self.assertEqual(body["asLevel"], "view")
        self.assertNotIn("requireVersion", body)

    def test_required_version_refusal_is_explained(self):
        err = api.ConflictError("HTTP 409", 409, {"error": {
            "code": "version_mismatch", "required": True, "version": 2}})
        self.serve({DB: err})
        code, _, out = self.run_cli(
            "db", "update", SLUG, "tasks", "t1", "--data", "{}", "--require-version")
        self.assertEqual(code, 3)
        self.assertIn("already exists (it is at version 2)", out)

    def test_refused_at_a_lower_level_reads_as_not_found(self):
        self.serve({DB: api.ApiError("HTTP 404", 404, {"error": {
            "asLevel": "view", "code": "not-found", "message": "not found"}})})
        code, _, out = self.run_cli(
            "db", "set", SLUG, "tasks", "t1", "--data", "{}", "--as-level", "view")
        self.assertEqual(code, 1)
        self.assertIn("--as-level view", out)

    def test_out_dir_saves_documents(self):
        self.serve({DB: {"docs": [{"id": "t1", "data": {"a": 1}, "version": 1},
                                  {"id": "t2", "data": {"b": 2}, "version": 4}]}})
        code, out, _ = self.run_cli("db", "list", SLUG, "boards/b1/cols", "--out-dir", self.tmp)
        self.assertEqual(code, 0)
        saved = self.tmp / "boards" / "b1" / "cols" / "t2.json"
        self.assertIn(str(saved), out)
        self.assertEqual(json.loads(saved.read_text()), {"b": 2})

    def test_out_dir_refuses_an_unsafe_id_from_the_server(self):
        self.serve({DB: {"docs": [{"id": "..", "data": {}, "version": 1}]}})
        code, _, err = self.run_cli("db", "list", SLUG, "tasks", "--out-dir", self.tmp)
        self.assertNotEqual(code, 0)
        self.assertIn("unsafe", err)
        self.serve({DB: lambda body: {"id": "../x"} if body["verb"] == "whoami" else {
            "docs": [{"id": "d", "data": {}, "version": 1}]}})
        code, _, err = self.run_cli(
            "db", "list", SLUG, "data/users/me", "--out-dir", self.tmp)
        self.assertNotEqual(code, 0)
        self.assertIn("unsafe", err)
        self.assertEqual(list(self.tmp.iterdir()), [])

    def test_get_json_succeeds_for_a_missing_document(self):
        self.serve({DB: {"exists": False}})
        code, out, _ = self.run_cli("db", "get", SLUG, "tasks", "t9", "--json")
        self.assertEqual(code, 0)
        self.assertFalse(json.loads(out)["exists"])

    def test_profiles(self):
        person = "u_WKugDwcLxJ9M5m0d9tP_Qw"
        server = self.serve({("POST", f"/api/frame/user/profiles/{SLUG}"): {
            "profiles": {person: {"name": "Ada", "guest": True}}}})
        code, out, _ = self.run_cli("db", "profiles", SLUG, person)
        self.assertEqual(code, 0)
        self.assertEqual(out.strip().split("\t"), [person, "Ada", "guest"])
        self.assertEqual(server.bodies("POST", f"/api/frame/user/profiles/{SLUG}"),
                         [{"ids": [person]}])
        code, _, err = self.run_cli("db", "profiles", SLUG, "bob")
        self.assertNotEqual(code, 0)
        self.assertIn("not a person id", err)


class ReplyGuardTest(Case):
    def test_a_duplicate_reply_is_refused(self):
        answered = thread("t1", [human("c1", "fix?", True), claude("c2", "done")], activated=True)
        server = self.serve({COMMENTS: {"threads": [answered]}})
        code, _, err = self.run_cli("comments", "reply", SLUG, "t1", "again")
        self.assertEqual(code, 1)
        self.assertIn("--allow-duplicate", err)
        self.assertEqual(len(server.calls), 1)

    def test_allow_duplicate_and_retry_ids_post(self):
        answered = thread("t1", [human("c1", "fix?", True), claude("c2", "done")], activated=True)
        post = ("POST", f"/api/frame/comments/{SLUG}/t1")
        server = self.serve({COMMENTS: {"threads": [answered]}, post: {"comment_id": "c3"}})
        self.assertEqual(self.run_cli("comments", "reply", SLUG, "t1", "more",
                                      "--allow-duplicate")[0], 0)
        self.assertEqual(self.run_cli("comments", "reply", SLUG, "t1", "more",
                                      "--id", "4b7c9b0e-3a43-4b0c-9a65-3c1e6ff0d9a1")[0], 0)
        self.assertEqual(len(server.bodies(*post)), 2)

    def test_reply_size_cap(self):
        code, _, err = self.run_cli("comments", "reply", SLUG, "t1", "é" * 2049)
        self.assertNotEqual(code, 0)
        self.assertIn("4098 bytes", err)

    def test_list_one_thread(self):
        self.serve({COMMENTS: {"threads": [thread("t1", [human("c1", "one")]),
                                           thread("t2", [human("c2", "two")])]}})
        code, out, _ = self.run_cli("comments", "list", SLUG, "--thread", "t2")
        self.assertEqual(code, 0)
        self.assertIn("two", out)
        self.assertNotIn("one", out)
        code, _, _ = self.run_cli("comments", "list", SLUG, "--thread", "t9")
        self.assertEqual(code, 1)
        code, _, _ = self.run_cli("comments", "list", SLUG, "--thread", "t9", "--json")
        self.assertEqual(code, 1)

    def test_answered(self):
        self.assertFalse(commands.answered(thread("t", [human("c", "x", True)])))
        self.assertTrue(commands.answered(
            thread("t", [human("c", "x", True), claude("d", "y")])))
        self.assertFalse(commands.answered(
            thread("t", [human("c", "x", True), claude("d", "y"), human("e", "z", True)])))
