import contextlib
import hashlib
import io
import json
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from claude_artifact_cli import api, auth, cli

# Never reach PyPI or upgrade the install running the tests.
os.environ.setdefault("CLAUDE_ARTIFACT_NO_UPDATE_CHECK", "1")

DEPLOY = ("POST", "/api/frame/deploy/direct")


def _strip_attempt(path):
    """Deploys carry ?attempt_id=…; the fakes route on the bare path."""
    return path.split("?")[0] if path.startswith("/api/frame/deploy/direct?") else path
URL = "https://claude.ai/code/artifact/{}"


def boot_path(slug):
    return ("GET", f"/api/frame/{slug}?via=model_read")


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class Server:
    """Stands in for FrameClient._request: records calls, answers from routes."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def __call__(self, method, path, body=None):
        path = _strip_attempt(path)
        self.calls.append((method, path, body))
        if (method, path) not in self.routes:
            raise AssertionError(f"unexpected request {method} {path}")
        answer = self.routes[(method, path)]
        if isinstance(answer, Exception):
            raise answer
        return answer

    def paths(self):
        return [(m, p) for m, p, _ in self.calls]

    def deployed(self):
        bodies = [b for m, p, b in self.calls if (m, p) == DEPLOY]
        assert len(bodies) == 1, f"expected one deploy, got {len(bodies)}"
        return bodies[0]


class CliCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        patcher = mock.patch.object(auth, "get_token", return_value="test-token")
        self.get_token = patcher.start()
        self.addCleanup(patcher.stop)

    def serve(self, routes):
        server = Server(routes)
        patcher = mock.patch.object(api.FrameClient, "_request", side_effect=server)
        patcher.start()
        self.addCleanup(patcher.stop)
        return server

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main([str(a) for a in argv])
        return code, out.getvalue(), err.getvalue()

    def write(self, rel, data):
        path = self.tmp / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data if isinstance(data, bytes) else data.encode())
        return path


class PublishFileTest(CliCase):
    def test_new_page_publishes_replace_with_title_and_favicon(self):
        server = self.serve({DEPLOY: {"slug": "new1", "version": "v1"}})
        page = self.write("page.html", "<title> My  Page </title>")
        code, out, _ = self.run_cli("publish", page, "-q")
        self.assertEqual((code, out.strip()), (0, URL.format("new1")))
        body = server.deployed()
        self.assertEqual(body["mode"], "replace")
        self.assertEqual(body["title"], "My Page")
        self.assertIn("favicon", body)
        self.assertNotIn("baseVersion", body)

    def test_update_defaults_to_patch_with_live_base(self):
        server = self.serve({boot_path("s1"): {"ver": "v4"}, DEPLOY: {"slug": "s1"}})
        page = self.write("page.html", "<p>x</p>")
        code, _, _ = self.run_cli("publish", page, "--url", URL.format("s1"), "-q")
        self.assertEqual(code, 0)
        self.assertEqual(server.paths(), [boot_path("s1"), DEPLOY])
        body = server.deployed()
        self.assertEqual((body["mode"], body["slug"], body["baseVersion"]), ("patch", "s1", "v4"))
        self.assertNotIn("favicon", body)

    def test_supporting_files_and_removals(self):
        server = self.serve({DEPLOY: {"slug": "s1"}})
        page = self.write("page.html", "<p>x</p>")
        self.write("src/style.css", "p{}")
        code, _, _ = self.run_cli(
            "publish", page, "--slug", "s1", "--base-version", "v2",
            "--root", self.tmp, "--file", "css/app.css=src/style.css", "--remove", "old.js",
        )
        self.assertEqual(code, 0)
        body = server.deployed()
        self.assertEqual(body["baseVersion"], "v2")
        self.assertEqual(
            body["manifest"]["css/app.css"], {"content": "p{}", "contentType": "text/css"}
        )
        self.assertIsNone(body["manifest"]["old.js"])
        self.assertEqual(set(body["manifest"]), {"index.html", "css/app.css", "old.js"})

    def test_patch_without_slug_exits_1(self):
        server = self.serve({})
        page = self.write("page.html", "<p>x</p>")
        code, _, err = self.run_cli("publish", page, "--mode", "patch")
        self.assertEqual(code, 1)
        self.assertIn("--mode patch needs --slug", err)
        self.assertEqual(server.calls, [])


class ExitCodeTest(CliCase):
    def test_conflict_exits_3(self):
        self.serve({DEPLOY: api.ConflictError("HTTP 409: live v9", 409, {"live": "v9"})})
        page = self.write("page.html", "<p>x</p>")
        code, _, err = self.run_cli("publish", page, "--slug", "s1", "--base-version", "v1")
        self.assertEqual(code, 3)
        self.assertIn("v9", err)

    def test_real_409_exits_3_and_suggests_pull(self):
        raw = io.BytesIO(json.dumps({"live": "v9"}).encode())
        err_409 = urllib.error.HTTPError("https://x", 409, "conflict", {}, raw)
        self.addCleanup(err_409.close)
        page = self.write("page.html", "<p>x</p>")
        with mock.patch("urllib.request.urlopen", side_effect=err_409):
            code, _, err = self.run_cli("publish", page, "--slug", "s1", "--base-version", "v1")
        self.assertEqual(code, 3)
        self.assertIn("live version v9", err)
        self.assertIn("claude-artifact pull", err)

    def test_api_error_exits_1(self):
        self.serve({("GET", "/api/frame/artifacts?rel=mine&kind=page%2Cdocument&sort=last_viewed&limit=200"): api.ApiError("HTTP 500", 500)})
        self.assertEqual(self.run_cli("list")[0], 1)

    def test_auth_error_exits_2(self):
        self.get_token.side_effect = auth.AuthError("no login")
        code, _, err = self.run_cli("list")
        self.assertEqual(code, 2)
        self.assertIn("auth error: no login", err)


class PublishDirTest(CliCase):
    def make_site(self):
        site = self.tmp / "site"
        self.write("site/index.html", "<title>Site</title>")
        self.write("site/css/app.css", "p{}")
        self.write("site/img/logo.png", b"\x89PNG\x00")
        self.write("site/.hidden", "x")
        self.write("site/.git/config", "x")
        return site

    def test_first_publish_sends_all_files_and_saves_state(self):
        server = self.serve({DEPLOY: {"slug": "d1", "version": "v1"}})
        site = self.make_site()
        code, _, _ = self.run_cli("publish", site, "-q")
        self.assertEqual(code, 0)
        body = server.deployed()
        self.assertEqual(body["mode"], "replace")
        self.assertNotIn("slug", body)
        manifest = body["manifest"]
        self.assertEqual(set(manifest), {"index.html", "css/app.css", "img/logo.png"})
        self.assertEqual(manifest["img/logo.png"]["contentType"], "image/png")
        self.assertEqual(manifest["css/app.css"]["content"], "p{}")
        state = json.loads((site / ".artifact.json").read_text())
        self.assertEqual(
            state,
            {
                "slug": "d1",
                "url": URL.format("d1"),
                "version": "v1",
                "title": "Site",
                "files": {
                    "index.html": sha(b"<title>Site</title>"),
                    "css/app.css": sha(b"p{}"),
                    "img/logo.png": sha(b"\x89PNG\x00"),
                },
            },
        )

    def test_republish_reuses_slug_and_base_version(self):
        site = self.make_site()
        self.serve({DEPLOY: {"slug": "d1", "version": "v1"}})
        self.run_cli("publish", site, "-q")
        server = self.serve({DEPLOY: {"slug": "d1", "version": "v2"}})
        code, _, _ = self.run_cli("publish", site, "-q")
        self.assertEqual(code, 0)
        self.assertEqual(server.paths(), [DEPLOY])
        body = server.deployed()
        # Once .artifact.json lists the directory's files, a republish patches.
        self.assertEqual((body["slug"], body["baseVersion"], body["mode"]), ("d1", "v1", "patch"))
        self.assertNotIn(".artifact.json", body["manifest"])
        self.assertEqual(json.loads((site / ".artifact.json").read_text())["version"], "v2")

    def test_republish_removes_only_files_deleted_locally(self):
        site = self.make_site()
        self.serve({DEPLOY: {"slug": "d1", "version": "v1"}})
        self.run_cli("publish", site, "-q")
        (site / "img" / "logo.png").unlink()
        server = self.serve({DEPLOY: {"slug": "d1", "version": "v2"}})
        self.run_cli("publish", site, "-q")
        manifest = server.deployed()["manifest"]
        self.assertIsNone(manifest["img/logo.png"])
        self.assertEqual(
            {k for k, v in manifest.items() if v is not None}, {"index.html", "css/app.css"}
        )
        self.assertNotIn("img/logo.png", json.loads((site / ".artifact.json").read_text())["files"])

    def test_explicit_replace_still_replaces(self):
        site = self.make_site()
        self.serve({DEPLOY: {"slug": "d1", "version": "v1"}})
        self.run_cli("publish", site, "-q")
        server = self.serve({DEPLOY: {"slug": "d1", "version": "v2"}})
        self.run_cli("publish", site, "--mode", "replace", "-q")
        self.assertEqual(server.deployed()["mode"], "replace")

    def test_explicit_slug_or_base_version_overrides_state(self):
        site = self.make_site()
        self.write("site/.artifact.json", json.dumps({"slug": "d1", "version": "v1"}))
        for extra, want in (
            (["--slug", "other"], ("other", None)),
            (["--base-version", "v5"], ("d1", "v5")),
        ):
            with self.subTest(extra=extra):
                server = self.serve({DEPLOY: {"slug": want[0], "version": "v9"}})
                self.assertEqual(self.run_cli("publish", site, "-q", *extra)[0], 0)
                body = server.deployed()
                self.assertEqual((body["slug"], body.get("baseVersion")), want)
                self.assertEqual(body["mode"], "replace")
                self.write("site/.artifact.json", json.dumps({"slug": "d1", "version": "v1"}))

    def test_conflict_leaves_state_alone(self):
        site = self.make_site()
        state = json.dumps({"slug": "d1", "version": "v1"})
        self.write("site/.artifact.json", state)
        self.serve({DEPLOY: api.ConflictError("409", 409, {"live": "v2"})})
        self.assertEqual(self.run_cli("publish", site, "-q")[0], 3)
        self.assertEqual((site / ".artifact.json").read_text(), state)

    def test_refused_directories(self):
        self.serve({})
        site = self.make_site()
        self.write("bare/style.css", "p{}")
        for argv in (
            ["publish", self.tmp / "bare"],
            ["publish", site, "--file", "x.css"],
            ["publish", site, "--root", self.tmp],
        ):
            with self.subTest(argv=argv[1:]), self.assertRaises(SystemExit):
                self.run_cli(*argv)

    @unittest.skipUnless(hasattr(os, "symlink"), "needs symlinks")
    def test_symlinks_refused(self):
        self.serve({})
        secret = self.write("outside.txt", "secret")
        for name in ("link.txt", "linkdir"):
            with self.subTest(name=name):
                site = self.make_site()
                link = site / name
                os.symlink(secret if name == "link.txt" else self.tmp, link)
                with self.assertRaises(SystemExit) as ctx:
                    self.run_cli("publish", site)
                self.assertIn("symlink", str(ctx.exception))
                link.unlink()


SECRET_BOOT = {
    "ver": "v3",
    "title": "T",
    "assetToken": "SECRET-A",
    "files": [{"path": "index.html", "thumb": "https://h/t?__frame_t=SECRET-B&w=1"}],
}


class NoTokenOutputTest(CliCase):
    def test_read_json_and_out_file_are_redacted(self):
        self.serve({boot_path("s1"): SECRET_BOOT, ("GET", "/api/frame/read/s1"): {}})
        out_file = self.tmp / "meta.json"
        code, out, _ = self.run_cli("read", "s1", "--json", "-o", out_file)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["ver"], "v3")
        for text in (out, out_file.read_text()):
            self.assertNotIn("SECRET", text)
            self.assertIn("__frame_t=[redacted]&w=1", text)

    def test_list_json_is_redacted(self):
        rows = [
            {"kind": "page", "id": "a", "rel": "mine", "previewToken": "SECRET",
             "thumbnail_url": "x?__frame_t=SECRET"}
        ]
        self.serve({("GET", "/api/frame/artifacts?rel=mine&kind=page%2Cdocument&sort=last_viewed&limit=200"): {"artifacts": rows}})
        code, out, _ = self.run_cli("--json", "list")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)[0]["slug"], "a")
        self.assertNotIn("SECRET", out)

    def test_list_follows_pages_and_reports_the_next_token(self):
        first = "/api/frame/artifacts?rel=mine&kind=page%2Cdocument&sort=last_viewed&limit=3"
        second = first[: -1] + "2&page_token=T1"
        self.serve({
            ("GET", first): {"artifacts": [{"kind": "page", "id": "a", "title": "A"}],
                            "next_page_token": "T1"},
            ("GET", second): {"artifacts": [
                {"kind": "page", "id": "b", "title": "B", "type_title": "Docs"},
                {"kind": "page", "id": "c", "title": "C"}], "next_page_token": "T2"},
        })
        code, out, err = self.run_cli("list", "--limit", "3")
        self.assertEqual(code, 0)
        self.assertEqual([line.split()[0] for line in out.splitlines()], ["a", "b", "c"])
        self.assertIn("Docs", out)
        self.assertIn("--page-token T2", err)

    def test_list_limit_counts_rows_after_filters(self):
        base = "/api/frame/artifacts?rel=mine&kind=page%2Cdocument&sort=last_viewed&limit=200"
        self.serve({
            ("GET", base): {"artifacts": [
                {"kind": "page", "id": "a", "title": "A"},
                {"kind": "page", "id": "b", "title": "B", "starred": True}],
                "next_page_token": "T1"},
            ("GET", base + "&page_token=T1"): {"artifacts": [
                {"kind": "page", "id": "c", "title": "C", "starred": True},
                {"kind": "page", "id": "d", "title": "D", "starred": True}],
                "next_page_token": "T1"},
        })
        code, out, err = self.run_cli("list", "--pinned", "--limit", "2")
        self.assertEqual(code, 0)
        self.assertEqual([line.split()[0] for line in out.splitlines()], ["b", "c"])
        self.assertIn("raise --limit", err)
        # A token the server already gave stops the listing instead of looping.
        code, _, err = self.run_cli("list", "--pinned", "--limit", "5")
        self.assertEqual(code, 1)
        self.assertIn("repeated a page token", err)

    def test_list_falls_back_to_the_unpaged_listing(self):
        self.serve({
            ("GET", "/api/frame/artifacts?rel=mine&kind=page%2Cdocument&sort=last_viewed&limit=200"): api.ApiError("HTTP 404", 404),
            ("GET", "/api/frame/frames?limit=200"): {"frames": [
                {"slug": "a", "rel": "mine", "title": "A"},
                {"slug": "b", "rel": "shared", "title": "B"}]},
        })
        code, out, _ = self.run_cli("list")
        self.assertEqual(code, 0)
        self.assertIn("A", out)
        self.assertNotIn(" B", out)


class StatusTest(CliCase):
    def remote(self, ver="v3"):
        files = [
            {"path": "index.html", "sha256": sha(b"<p>same</p>")},
            {"path": "app.css", "sha256": sha(b"old")},
            {"path": "gone.js", "sha256": sha(b"js")},
        ]
        return {boot_path("d1"): {"ver": ver, "files": files}, ("GET", "/api/frame/read/d1"): {}}

    def make_dir(self, version):
        self.write("site/index.html", "<p>same</p>")
        self.write("site/app.css", "new")
        self.write("site/new.png", b"\x00")
        self.write("site/.artifact.json", json.dumps({"slug": "d1", "version": version}))
        return self.tmp / "site"

    def test_directory_json(self):
        self.serve(self.remote("v3"))
        code, out, _ = self.run_cli("status", self.make_dir("v2"), "--json")
        self.assertEqual(code, 0)
        self.assertEqual(
            json.loads(out),
            {
                "slug": "d1",
                "url": URL.format("d1"),
                "live": "v3",
                "base": "v2",
                "behind": True,
                "files": [
                    {"state": "changed", "path": "app.css"},
                    {"state": "remote-only", "path": "gone.js"},
                    {"state": "same", "path": "index.html"},
                    {"state": "local-only", "path": "new.png"},
                ],
            },
        )

    def test_directory_text_up_to_date(self):
        self.serve(self.remote("v3"))
        code, out, _ = self.run_cli("status", self.make_dir("v3"))
        self.assertEqual(code, 0)
        self.assertIn("base version  v3  (up to date)", out)
        self.assertIn("changed       app.css", out)

    def test_single_file_compares_only_index(self):
        self.serve(self.remote("v3"))
        page = self.write("page.html", "<p>edited</p>")
        code, out, _ = self.run_cli("status", page, "--slug", "d1", "--json")
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual(data["files"], [{"state": "changed", "path": "index.html"}])
        self.assertEqual((data["base"], data["behind"]), (None, False))

    def test_other_slug_has_no_base(self):
        server = self.serve({boot_path("x9"): {"ver": "v1"}, ("GET", "/api/frame/read/x9"): {}})
        code, out, _ = self.run_cli("status", self.make_dir("v2"), "--slug", "x9", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(server.paths()[0], boot_path("x9"))
        self.assertEqual((json.loads(out)["base"], json.loads(out)["behind"]), (None, False))

    def test_no_slug_anywhere_exits(self):
        self.serve({})
        page = self.write("page.html", "<p>x</p>")
        with self.assertRaises(SystemExit):
            self.run_cli("status", page)


if __name__ == "__main__":
    unittest.main()
