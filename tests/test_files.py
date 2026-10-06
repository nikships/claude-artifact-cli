"""Reading published bytes: FileReader, `read --path`, `pull`, typed publishes, status."""

import contextlib
import hashlib
import io
import json
import os
import tempfile
import unittest
import urllib.error
import urllib.parse
from pathlib import Path
from unittest import mock

from claude_artifact_cli import api, auth, cli

# Never reach PyPI or upgrade the install running the tests.
os.environ.setdefault("CLAUDE_ARTIFACT_NO_UPDATE_CHECK", "1")

DEPLOY = ("POST", "/api/frame/deploy/direct")
URL = "https://claude.ai/code/artifact/{}"
TOKEN = "SECRET-ASSET-TOKEN"


def boot_path(slug):
    return ("GET", f"/api/frame/{slug}?via=model_read")


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def src_path(ver, path):
    return f"/_f/{ver}/_src/{api.clean_path(path)}"


def manifest(files, typed=()):
    entries = [
        {"path": p, "sha256": sha(d), "size": len(d), "contentType": api.guess_content_type(p)}
        for p, d in files.items()
    ]
    return entries + [{"path": p, "src": {"type": "some-type"}} for p in typed]


def boot(ver="v1", files=None, typed=(), token=TOKEN, **extra):
    data = {"ver": ver, "title": "Live", "assetToken": token, "files": manifest(files or {}, typed)}
    data.update(extra)
    return data


class Seq:
    """A route answer that changes per call; the last one repeats."""

    def __init__(self, *answers):
        self.answers = list(answers)

    def next(self):
        return self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]


class Server:
    """Stands in for FrameClient._request: records calls, answers from routes."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def __call__(self, method, path, body=None):
        self.calls.append((method, path, body))
        if (method, path) not in self.routes:
            raise AssertionError(f"unexpected request {method} {path}")
        answer = self.routes[(method, path)]
        if isinstance(answer, Seq):
            answer = answer.next()
        if isinstance(answer, Exception):
            raise answer
        return answer

    def paths(self):
        return [(m, p) for m, p, _ in self.calls]

    def deployed(self):
        bodies = [b for m, p, b in self.calls if (m, p) == DEPLOY]
        assert len(bodies) == 1, f"expected one deploy, got {len(bodies)}"
        return bodies[0]


class Host:
    """Stands in for FileReader._get: (status, bytes) per URL path, 404 otherwise.

    Requests made with a token in `expired` get a 403, as the CDN does.
    """

    def __init__(self, routes=None, expired=()):
        self.routes = dict(routes or {})
        self.expired = set(expired)
        self.expired_status = 403
        self.calls = []
        self.tokens = []

    def __call__(self, reader, url_path):
        self.calls.append(url_path)
        self.tokens.append(reader._token)
        if reader._token in self.expired:
            return self.expired_status, b""
        answer = self.routes.get(url_path, (404, b""))
        return answer.next() if isinstance(answer, Seq) else answer


class FilesCase(unittest.TestCase):
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

    def host(self, routes=None, expired=()):
        host = Host(routes, expired)
        # A plain function, so the reader comes in as self, like the real method.
        patcher = mock.patch.object(api.FileReader, "_get", lambda reader, path: host(reader, path))
        patcher.start()
        self.addCleanup(patcher.stop)
        return host

    def live(self, slug="s1", ver="v1", files=None, typed=(), **extra):
        """A published artifact: boot + read routes and its source bytes."""
        files = files or {}
        server = self.serve(
            {
                boot_path(slug): boot(ver, files, typed, **extra),
                ("GET", f"/api/frame/read/{slug}"): {},
            }
        )
        host = self.host({src_path(ver, p): (200, d) for p, d in files.items()})
        return server, host

    def run_cli(self, *argv):
        """Runs the CLI; stdout comes back as bytes, since read --path writes raw bytes."""
        raw, err = io.BytesIO(), io.StringIO()
        out = io.TextIOWrapper(raw, encoding="utf-8")
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = cli.main([str(a) for a in argv])
            out.flush()
            return code, raw.getvalue(), err.getvalue()
        finally:
            out.detach()

    def write(self, rel, data):
        path = self.tmp / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data if isinstance(data, bytes) else data.encode())
        return path

    def state(self, rel="site"):
        return json.loads((self.tmp / rel / ".artifact.json").read_text())

    def assert_no_token(self, *texts):
        for text in texts:
            if isinstance(text, bytes):
                text = text.decode("utf-8", "replace")
            self.assertNotIn(TOKEN, text)


# -- pure helpers ------------------------------------------------------------


class CleanPathTest(unittest.TestCase):
    def test_accepts_relative_paths_and_percent_encodes(self):
        for path, want in (
            ("index.html", "index.html"),
            ("css/app.css", "css/app.css"),
            ("a b/c d.png", "a%20b/c%20d.png"),
            ("café/ü.txt", "caf%C3%A9/%C3%BC.txt"),
            ("a?b#c&d", "a%3Fb%23c%26d"),
            (".well-known/x", ".well-known/x"),
            ("a" * 512, "a" * 512),
        ):
            with self.subTest(path=path[:20]):
                self.assertEqual(api.clean_path(path), want)

    def test_rejects_unclean_paths(self):
        for path in (
            "",
            "/abs.html",
            "..",
            "../x",
            "a/../b",
            ".",
            "./a",
            "a/",
            "a//b",
            "a\\b",
            "a%20b",
            "a\x00b",
            "a\nb",
            "a\tb",
            "a\x7fb",
            "a" * 513,
            "é" * 257,  # 514 bytes
        ):
            with self.subTest(path=path[:20]):
                self.assertIsNone(api.clean_path(path))


COMMENTS = b'<script type="application/json" id="__frame_comments__">'


class StripServedPageTest(unittest.TestCase):
    def test_runtime_block_removed(self):
        served = (
            b"<head><!-- frame-runtime -->\n<script>rt()</script>\n<!-- /frame-runtime --></head>"
        )
        self.assertEqual(api.strip_served_page(served), b"<head></head>")

    def test_comments_trailer_removed(self):
        trailer = COMMENTS + b'{"mac":"m","payload":"p"}</script>'
        self.assertEqual(api.strip_served_page(b"<p>x</p>\n" + trailer + b"\n"), b"<p>x</p>")

    def test_runtime_and_trailer_together(self):
        served = (
            b"<!-- frame-runtime --><script>rt()</script><!-- /frame-runtime --><p>x</p>\n"
            + COMMENTS
            + b'{"mac":"m","payload":{}}</script>'
        )
        self.assertEqual(api.strip_served_page(served), b"<p>x</p>")

    def test_other_trailing_scripts_left_alone(self):
        for page in (
            b"<p>x</p>\n<script>go()</script>",
            b"<p>x</p>\n" + COMMENTS + b'{"payload":"no mac"}</script>',
            b"<p>x</p>\n" + COMMENTS + b'["mac"]</script>',
            b"<p>x</p>\n" + COMMENTS + b"not json</script>",
            b"<p>x</p>\n" + COMMENTS + b'{"mac":"m"}</script>\n<p>after</p>',
        ):
            with self.subTest(page=page[-30:]):
                self.assertEqual(api.strip_served_page(page), page)


# claude.ai/artifact/<short id> -> slug, as checked against the live site.
SHORT_IDS = (
    ("PmF6et3FSS2rysgpBRvFeC", "b85676da-92b6-42b4-ab03-3a93c37439ad"),
    ("9HeqVtGkS4xX5ub3veDy3S", "431c53b4-484a-4b38-bd5d-37722a55253d"),
)


class SlugFromTest(unittest.TestCase):
    def test_short_ids_and_their_urls_decode_to_uuid(self):
        for short, uuid in SHORT_IDS:
            for value in (
                short,
                f"https://claude.ai/artifact/{short}",
                f"  https://claude.ai/artifact/{short}?ref=x#top \n",
            ):
                with self.subTest(value=value):
                    self.assertEqual(api.slug_from(value), uuid)

    def test_uuid_and_code_url_kept_as_is(self):
        uuid = "b85676da-92b6-42b4-ab03-3a93c37439ad"
        for value in (uuid, URL.format(uuid), f"{URL.format(uuid)}/"):
            with self.subTest(value=value):
                self.assertEqual(api.slug_from(value), uuid)

    def test_garbage_raises(self):
        for value in ("", "not a slug", "a/b", "https://example.com/x y", "a" * 65):
            with self.subTest(value=value), self.assertRaises(api.ApiError):
                api.slug_from(value)


# -- FileReader --------------------------------------------------------------


class FileReaderTest(FilesCase):
    def test_boots_once_and_redacts_token(self):
        server, host = self.live(files={"a.css": b"a", "b.js": b"b"})
        reader = api.FrameClient("test-token").files("s1")
        self.assertEqual((reader.slug, reader.version), ("s1", "v1"))
        self.assertEqual(set(reader.files), {"a.css", "b.js"})
        self.assertEqual(reader.meta["assetToken"], api.REDACTED)
        self.assert_no_token(json.dumps(reader.meta))
        self.assertEqual((reader.fetch("a.css"), reader.fetch("b.js")), (b"a", b"b"))
        self.assertEqual(server.paths(), [boot_path("s1")])
        self.assertEqual(host.calls, [src_path("v1", "a.css"), src_path("v1", "b.js")])
        self.assertEqual(host.tokens, [TOKEN, TOKEN])

    def test_no_asset_token_raises(self):
        for token in (None, ""):
            with self.subTest(token=token):
                answer = boot(files={"a.css": b"a"})
                answer["assetToken"] = token
                self.serve({boot_path("s1"): answer})
                with self.assertRaises(api.ApiError) as ctx:
                    api.FrameClient("test-token").files("s1")
                self.assertIn("asset token", str(ctx.exception))

    def test_unknown_path_is_404_without_a_request(self):
        _, host = self.live(files={"a.css": b"a"})
        reader = api.FrameClient("test-token").files("s1")
        with self.assertRaises(api.ApiError) as ctx:
            reader.fetch("missing.css")
        self.assertEqual(ctx.exception.status, 404)
        self.assertIn("no file 'missing.css' in version v1", str(ctx.exception))
        self.assertEqual(host.calls, [])

    def test_unclean_manifest_path_never_requested(self):
        _, host = self.live(files={"../evil.txt": b"x"})
        reader = api.FrameClient("test-token").files("s1")
        with self.assertRaises(api.ApiError) as ctx:
            reader.fetch("../evil.txt")
        self.assertIn("not a clean relative path", str(ctx.exception))
        self.assertEqual(host.calls, [])

    def test_source_path_is_percent_encoded(self):
        _, host = self.live(files={"img/a b é.png": b"\x89PNG"})
        api.FrameClient("test-token").files("s1").fetch("img/a b é.png")
        self.assertEqual(host.calls, ["/_f/v1/_src/img/a%20b%20%C3%A9.png"])

    def test_falls_back_to_served_copy(self):
        self.serve({boot_path("s1"): boot(files={"css/app.css": b"p{}"})})
        host = self.host({"/_f/v1/css/app.css": (200, b"p{}")})
        data = api.FrameClient("test-token").files("s1").fetch("css/app.css")
        self.assertEqual(data, b"p{}")
        self.assertEqual(host.calls, ["/_f/v1/_src/css/app.css", "/_f/v1/css/app.css"])

    def test_served_index_is_the_root_and_stripped(self):
        source = b"<p>hi</p>"
        served = (
            b"<!-- frame-runtime --><script>rt()</script><!-- /frame-runtime -->"
            + source
            + b"\n"
            + COMMENTS
            + b'{"mac":"m","payload":"p"}</script>'
        )
        self.serve({boot_path("s1"): boot(files={"index.html": source})})
        host = self.host({"/_f/v1/": (200, served)})
        self.assertEqual(api.FrameClient("test-token").files("s1").fetch("index.html"), source)
        self.assertEqual(host.calls, ["/_f/v1/_src/index.html", "/_f/v1/"])

    def test_source_skipped_for_external_non_writer(self):
        for perm, want_src in (
            ({"role": "viewer"}, False),
            ({}, False),
            ({"role": "writer"}, True),
        ):
            with self.subTest(perm=perm):
                self.serve(
                    {boot_path("s1"): boot(files={"a.css": b"a"}, mode="external", perm=perm)}
                )
                host = self.host(
                    {src_path("v1", "a.css"): (200, b"a"), "/_f/v1/a.css": (200, b"a")}
                )
                api.FrameClient("test-token").files("s1").fetch("a.css")
                want = [src_path("v1", "a.css")] if want_src else ["/_f/v1/a.css"]
                self.assertEqual(host.calls, want)

    def test_expired_token_reboots_once_and_retries(self):
        for status in (401, 403):
            with self.subTest(status=status):
                server = self.serve(
                    {
                        boot_path("s1"): Seq(
                            boot(files={"a.css": b"a"}, token="OLD"),
                            boot(files={"a.css": b"a"}, token="NEW"),
                        )
                    }
                )
                host = self.host({src_path("v1", "a.css"): (200, b"a")}, expired={"OLD"})
                host.expired_status = status
                reader = api.FrameClient("test-token").files("s1")
                self.assertEqual(reader.fetch("a.css"), b"a")
                self.assertEqual(server.paths(), [boot_path("s1")] * 2)
                # source, then served copy with the old token; source again with the new one
                self.assertEqual(host.tokens, ["OLD", "OLD", "NEW"])
                self.assertEqual(
                    host.calls, [src_path("v1", "a.css"), "/_f/v1/a.css", src_path("v1", "a.css")]
                )
                self.assertEqual(reader.meta["assetToken"], api.REDACTED)

    def test_refused_source_then_missing_served_copy_still_reboots(self):
        server = self.serve({boot_path("s1"): boot(files={"a.css": b"a"})})
        host = self.host({src_path("v1", "a.css"): Seq((403, b""), (200, b"a"))})
        self.assertEqual(api.FrameClient("test-token").files("s1").fetch("a.css"), b"a")
        self.assertEqual(server.paths(), [boot_path("s1")] * 2)
        self.assertEqual(
            host.calls, [src_path("v1", "a.css"), "/_f/v1/a.css", src_path("v1", "a.css")]
        )

    def test_still_refused_after_reboot_raises(self):
        server = self.serve({boot_path("s1"): boot(files={"a.css": b"a"})})
        self.host(expired={TOKEN})
        with self.assertRaises(api.ApiError) as ctx:
            api.FrameClient("test-token").files("s1").fetch("a.css")
        self.assertEqual(ctx.exception.status, 403)
        self.assertEqual(len(server.calls), 2)  # one re-boot only
        self.assert_no_token(str(ctx.exception))

    def test_new_version_during_reboot_raises(self):
        self.serve(
            {
                boot_path("s1"): Seq(
                    boot("v1", {"a.css": b"a"}, token="OLD"), boot("v2", {"a.css": b"a"})
                )
            }
        )
        self.host(expired={"OLD"})
        with self.assertRaises(api.ApiError) as ctx:
            api.FrameClient("test-token").files("s1").fetch("a.css")
        self.assertIn("published while reading", str(ctx.exception))
        self.assertIn("v2", str(ctx.exception))
        self.assert_no_token(str(ctx.exception))

    def test_not_served_raises_404(self):
        self.serve({boot_path("s1"): boot(files={"a.css": b"a"})})
        self.host()
        with self.assertRaises(api.ApiError) as ctx:
            api.FrameClient("test-token").files("s1").fetch("a.css")
        self.assertEqual(ctx.exception.status, 404)
        self.assert_no_token(str(ctx.exception))

    def test_other_status_raises(self):
        self.serve({boot_path("s1"): boot(files={"a.css": b"a"})})
        self.host({"/_f/v1/a.css": (500, b"")})
        with self.assertRaises(api.ApiError) as ctx:
            api.FrameClient("test-token").files("s1").fetch("a.css")
        self.assertEqual(ctx.exception.status, 500)

    def test_sha256_mismatch_raises(self):
        self.serve({boot_path("s1"): boot(files={"a.css": b"a"})})
        self.host({src_path("v1", "a.css"): (200, b"tampered")})
        with self.assertRaises(api.ApiError) as ctx:
            api.FrameClient("test-token").files("s1").fetch("a.css")
        self.assertIn("sha256", str(ctx.exception))

    def test_entry_without_sha256_accepted_unverified(self):
        self.serve(
            {boot_path("s1"): {"ver": "v1", "assetToken": TOKEN, "files": [{"path": "a.css"}]}}
        )
        self.host({src_path("v1", "a.css"): (200, b"anything")})
        self.assertEqual(api.FrameClient("test-token").files("s1").fetch("a.css"), b"anything")

    def test_type_owned(self):
        self.live(files={"data.json": b"{}"}, typed=("index.html",))
        reader = api.FrameClient("test-token").files("s1")
        self.assertTrue(reader.type_owned("index.html"))
        self.assertFalse(reader.type_owned("data.json"))
        self.assertFalse(reader.type_owned("missing"))


class FakeResponse(io.BytesIO):
    status = 200


class ContentHostTransportTest(FilesCase):
    """The real _get, with the opener stubbed out."""

    def reader(self, token="tok/en+="):
        self.serve(
            {boot_path("s1"): boot(files={"a b.css": b"a", "index.html": b"<p>"}, token=token)}
        )
        return api.FrameClient("test-token").files("s1")

    def test_only_user_agent_sent_to_content_host(self):
        reader = self.reader()
        requests = []

        def open_(req, timeout):
            requests.append((req, timeout))
            return FakeResponse(b"a")

        with mock.patch.object(reader._opener, "open", side_effect=open_):
            self.assertEqual(reader.fetch("a b.css"), b"a")
        ((req, timeout),) = requests
        url = urllib.parse.urlsplit(req.full_url)
        self.assertEqual((url.scheme, url.netloc), ("https", "s1.frame.claudeusercontent.com"))
        self.assertEqual(url.path, "/_f/v1/_src/a%20b.css")
        self.assertEqual(url.query, "__frame_t=tok%2Fen%2B%3D")
        self.assertEqual(urllib.parse.parse_qs(url.query), {"__frame_t": ["tok/en+="]})
        self.assertEqual(
            req.header_items(), [("User-agent", f"claude-artifact-cli/{api.CLIENT_VERSION}")]
        )
        self.assertIsNone(req.data)
        self.assertNotIn("test-token", req.full_url)
        self.assertEqual(timeout, reader._client.timeout)

    def test_http_error_falls_back_to_served_copy(self):
        reader = self.reader()
        seen = []

        def open_(req, timeout):
            path = urllib.parse.urlsplit(req.full_url).path
            seen.append(path)
            if "/_src/" in path:
                raise urllib.error.HTTPError(req.full_url, 404, "nf", {}, io.BytesIO())
            return FakeResponse(b"<p>")

        with mock.patch.object(reader._opener, "open", side_effect=open_):
            self.assertEqual(reader.fetch("index.html"), b"<p>")
        self.assertEqual(seen, ["/_f/v1/_src/index.html", "/_f/v1/"])

    def test_network_error_is_redacted(self):
        reader = self.reader(token=TOKEN)
        err = urllib.error.URLError(f"refused https://x/?__frame_t={TOKEN}")
        with (
            mock.patch.object(reader._opener, "open", side_effect=err),
            self.assertRaises(api.ApiError) as ctx,
        ):
            reader.fetch("a b.css")
        self.assertIn("network error", str(ctx.exception))
        self.assert_no_token(str(ctx.exception))

    def test_redirects_not_followed(self):
        self.assertIsNone(api._NoRedirect().redirect_request(None, None, 302, "", {}, "x"))


# -- read --path ---------------------------------------------------------------


READ_FILES = {"index.html": b"<p>hi</p>", "img/logo.png": b"\x89PNG\x00\xff", "a b.txt": b"x"}


class DestinationTest(unittest.TestCase):
    def test_windows_unsafe_names(self):
        for part in ("C:", "C:evil", "a:stream", "CON", "nul.txt", "com1", "a.", "a ", "a?b", "x|y"):
            with self.subTest(part=part):
                self.assertTrue(cli._unsafe_on_windows(part))
        for part in ("index.html", "a b.txt", "console.js", "LPT", "comx.txt", ".x"):
            with self.subTest(part=part):
                self.assertFalse(cli._unsafe_on_windows(part))

    def test_windows_unsafe_path_refused_on_windows(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(cli, "_ON_WINDOWS", True):
            with self.assertRaises(SystemExit):
                cli._destination(Path(tmp), "C:evil/x.txt")
            self.assertEqual(cli._destination(Path(tmp), "a/b.txt"), Path(tmp) / "a" / "b.txt")

    def test_destination_must_resolve_inside_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "root"
            root.mkdir()
            with (
                mock.patch.object(cli.api, "clean_path", return_value="x"),
                self.assertRaises(SystemExit),
            ):
                cli._destination(root, "../outside.txt")


class ReadPathTest(FilesCase):
    def test_one_path_writes_raw_bytes_to_stdout(self):
        self.live(files=READ_FILES)
        code, out, err = self.run_cli("read", "s1", "--path", "img/logo.png")
        self.assertEqual((code, out, err), (0, b"\x89PNG\x00\xff", ""))

    def test_several_paths_need_out_dir(self):
        server = self.serve({})
        with self.assertRaises(SystemExit) as ctx:
            self.run_cli("read", "s1", "--path", "index.html", "--path", "a b.txt")
        self.assertIn("--out-dir", str(ctx.exception))
        self.assertEqual(server.calls, [])

    def test_out_dir_writes_nested_files_and_prints_paths(self):
        self.live(files=READ_FILES)
        out_dir = self.tmp / "out"
        code, out, _ = self.run_cli(
            "read", "s1", "--path", "img/logo.png", "--path", "a b.txt", "--out-dir", out_dir
        )
        self.assertEqual(code, 0)
        self.assertEqual((out_dir / "img/logo.png").read_bytes(), b"\x89PNG\x00\xff")
        self.assertEqual((out_dir / "a b.txt").read_bytes(), b"x")
        self.assertEqual(
            out.decode().splitlines(), [str(out_dir / "img/logo.png"), str(out_dir / "a b.txt")]
        )
        self.assertFalse((out_dir / "index.html").exists())

    def test_unsafe_manifest_path_not_written(self):
        self.live(files={"../evil.txt": b"x"})
        code, _, err = self.run_cli(
            "read", "s1", "--path", "../evil.txt", "--out-dir", self.tmp / "out"
        )
        self.assertEqual(code, 1)
        self.assertIn("not a clean relative path", err)
        self.assertFalse((self.tmp / "evil.txt").exists())

    def test_write_helper_refuses_unsafe_paths(self):
        for path in ("../evil.txt", "/abs.txt", "a//b"):
            with self.subTest(path=path), self.assertRaises(SystemExit):
                cli._write(self.tmp / "out", path, b"x")
        self.assertFalse((self.tmp / "evil.txt").exists())

    def test_missing_path_exits_1(self):
        self.live(files=READ_FILES)
        code, out, err = self.run_cli("read", "s1", "--path", "nope.css")
        self.assertEqual((code, out), (1, b""))
        self.assertIn("no file 'nope.css'", err)

    def test_refused_flag_combinations(self):
        out_file = self.tmp / "meta.json"
        for argv in (
            ["read", "s1", "--path", "index.html", "--json"],
            ["--json", "read", "s1", "--path", "index.html"],
            ["read", "s1", "--path", "index.html", "-o", out_file],
            ["read", "s1", "--out-dir", self.tmp / "out"],
        ):
            with self.subTest(argv=argv):
                server = self.serve({})
                with self.assertRaises(SystemExit):
                    self.run_cli(*argv)
                self.assertEqual(server.calls, [])
        self.assertFalse(out_file.exists())


# -- pull ----------------------------------------------------------------------


class PullTest(FilesCase):
    def linked(self, base, local, slug="s1", ver="v1", **state):
        """A directory pulled from `slug` at `ver` (base files), then edited to `local`."""
        for path, data in local.items():
            self.write(f"site/{path}", data)
        self.write(
            "site/.artifact.json",
            json.dumps(
                {
                    "slug": slug,
                    "version": ver,
                    "files": {p: sha(d) for p, d in base.items()},
                    **state,
                }
            ),
        )
        return self.tmp / "site"

    def test_fresh_pull_writes_files_and_state(self):
        files = {"index.html": b"<p>hi</p>", "css/app.css": b"p{}", "img/logo.png": b"\x89\x00"}
        self.live(files=files, ver="v4")
        site = self.tmp / "site"
        code, out, err = self.run_cli("pull", "s1", site)
        self.assertEqual((code, out.decode().strip()), (0, str(site)))
        for path, data in files.items():
            self.assertEqual((site / path).read_bytes(), data)
        self.assertEqual(
            self.state(),
            {
                "slug": "s1",
                "url": URL.format("s1"),
                "version": "v4",
                "title": "Live",
                "files": {p: sha(d) for p, d in files.items()},
            },
        )
        self.assertIn("version v4: wrote 3 file(s)", err)

    def test_default_dir_is_the_slug(self):
        uuid = "b85676da-92b6-42b4-ab03-3a93c37439ad"
        self.live(slug=uuid, files={"index.html": b"<p>"})
        cwd = os.getcwd()
        os.chdir(self.tmp)
        self.addCleanup(os.chdir, cwd)
        code, out, err = self.run_cli(
            "pull", "https://claude.ai/artifact/PmF6et3FSS2rysgpBRvFeC", "-q"
        )
        self.assertEqual((code, out.decode().strip(), err), (0, uuid, ""))
        self.assertEqual((self.tmp / uuid / "index.html").read_bytes(), b"<p>")
        self.assertEqual(self.state(uuid)["slug"], uuid)

    def test_type_owned_files_left_out(self):
        _, host = self.live(files={"data.json": b"{}"}, typed=("index.html", "type.js"))
        code, _, err = self.run_cli("pull", "s1", self.tmp / "site")
        self.assertEqual(code, 0)
        self.assertFalse((self.tmp / "site/index.html").exists())
        state = self.state()
        self.assertIs(state["typed"], True)
        self.assertEqual(state["files"], {"data.json": sha(b"{}")})
        self.assertEqual(host.calls, [src_path("v1", "data.json")])
        self.assertIn("left out 2 file(s) supplied by the artifact's type", err)

    def test_empty_artifact_still_creates_dir(self):
        self.live(files={}, typed=("index.html",))
        code, _, _ = self.run_cli("pull", "s1", self.tmp / "a/site", "-q")
        self.assertEqual(code, 0)
        self.assertEqual(os.listdir(self.tmp / "a/site"), [".artifact.json"])
        state = self.state("a/site")
        self.assertEqual((state["files"], state["typed"]), ({}, True))

    def test_unlinked_non_empty_dir_refused(self):
        for state in (None, {"slug": "other", "version": "v1"}):
            with self.subTest(state=state):
                self.write("site/notes.txt", "mine")
                if state:
                    self.write("site/.artifact.json", json.dumps(state))
                _, host = self.live(files={"index.html": b"<p>"})
                with self.assertRaises(SystemExit) as ctx:
                    self.run_cli("pull", "s1", self.tmp / "site")
                self.assertIn("--force", str(ctx.exception))
                self.assertEqual(host.calls, [])
                self.assertFalse((self.tmp / "site/index.html").exists())

    def test_target_that_is_a_file_refused(self):
        self.serve({})
        target = self.write("site", "x")
        with self.assertRaises(SystemExit):
            self.run_cli("pull", "s1", target)

    def test_force_into_unlinked_dir_takes_live_and_keeps_untracked(self):
        self.write("site/index.html", "<p>mine</p>")
        self.write("site/notes.txt", "mine")
        self.live(files={"index.html": b"<p>live</p>"})
        code, _, _ = self.run_cli("pull", "s1", self.tmp / "site", "--force", "-q")
        self.assertEqual(code, 0)
        self.assertEqual((self.tmp / "site/index.html").read_bytes(), b"<p>live</p>")
        self.assertEqual((self.tmp / "site/notes.txt").read_bytes(), b"mine")
        self.assertEqual(self.state()["slug"], "s1")

    def test_three_way_merge(self):
        site = self.linked(
            base={"a.txt": b"a0", "b.txt": b"b0", "c.txt": b"c0"},
            local={"a.txt": b"a0", "b.txt": b"b1", "c.txt": b"c0", "mine.txt": b"m"},
        )
        live = {"a.txt": b"a2", "b.txt": b"b0", "c.txt": b"c0", "new/n.txt": b"n"}
        self.live(ver="v2", files=live)
        code, _, err = self.run_cli("pull", "s1", site)
        self.assertEqual(code, 0)
        self.assertEqual((site / "a.txt").read_bytes(), b"a2")  # live-only change
        self.assertEqual((site / "b.txt").read_bytes(), b"b1")  # local-only change
        self.assertEqual((site / "new/n.txt").read_bytes(), b"n")
        self.assertEqual((site / "mine.txt").read_bytes(), b"m")  # untracked
        self.assertIn("wrote 2 file(s)", err)
        self.assertIn("kept 1 local edit(s): b.txt", err)
        state = self.state()
        self.assertEqual(state["version"], "v2")
        self.assertEqual(state["files"], {p: sha(d) for p, d in live.items()})

    def test_both_changed_is_a_conflict_and_writes_nothing(self):
        base = {"a.txt": b"a0", "b.txt": b"b0"}
        site = self.linked(base=base, local={"a.txt": b"a1", "b.txt": b"b0"})
        before = (site / ".artifact.json").read_text()
        self.live(ver="v2", files={"a.txt": b"a2", "b.txt": b"b2"})
        with self.assertRaises(SystemExit) as ctx:
            self.run_cli("pull", "s1", site)
        self.assertIn("  a.txt", str(ctx.exception))
        self.assertNotIn("b.txt", str(ctx.exception))
        self.assertEqual((site / "a.txt").read_bytes(), b"a1")
        self.assertEqual((site / "b.txt").read_bytes(), b"b0")  # live-only, still not written
        self.assertEqual((site / ".artifact.json").read_text(), before)

    def test_force_takes_live_on_conflict(self):
        site = self.linked(
            base={"a.txt": b"a0", "x.txt": b"x0"}, local={"a.txt": b"a1", "x.txt": b"x1"}
        )
        self.live(ver="v2", files={"a.txt": b"a2"})
        code, _, _ = self.run_cli("pull", "s1", site, "--force", "-q")
        self.assertEqual(code, 0)
        self.assertEqual((site / "a.txt").read_bytes(), b"a2")
        self.assertFalse((site / "x.txt").exists())  # deleted live, edited here: live wins

    def test_remote_deletion_removes_file_and_empty_dirs(self):
        files = {"index.html": b"i", "js/keep.js": b"k", "js/lib/x.js": b"x", "old/deep/y.js": b"y"}
        site = self.linked(base=files, local=files)
        self.live(ver="v2", files={"index.html": b"i", "js/keep.js": b"k"})
        code, _, err = self.run_cli("pull", "s1", site)
        self.assertEqual(code, 0)
        self.assertFalse((site / "js/lib").exists())
        self.assertFalse((site / "old").exists())
        self.assertTrue((site / "js/keep.js").exists())
        self.assertIn("removed 2 file(s) no longer published", err)
        self.assertEqual(set(self.state()["files"]), {"index.html", "js/keep.js"})

    def test_remote_deletion_of_locally_edited_file_conflicts(self):
        site = self.linked(base={"x.js": b"x0"}, local={"x.js": b"x1"})
        self.live(ver="v2", files={})
        with self.assertRaises(SystemExit) as ctx:
            self.run_cli("pull", "s1", site)
        self.assertIn("x.js", str(ctx.exception))
        self.assertEqual((site / "x.js").read_bytes(), b"x1")

    @unittest.skipUnless(hasattr(os, "symlink"), "needs symlinks")
    def test_symlink_on_write_path_refused(self):
        outside = self.tmp / "outside"
        outside.mkdir()
        for name in ("assets",):
            with self.subTest(name=name):
                site = self.tmp / f"site-{name}"
                site.mkdir()
                os.symlink(outside, site / name)
                self.live(files={f"{name}/a.txt": b"x"})
                with self.assertRaises(SystemExit) as ctx:
                    self.run_cli("pull", "s1", site)
                self.assertIn("symlink", str(ctx.exception))
                self.assertEqual(os.listdir(outside), [])

    def test_dot_paths_are_left_out(self):
        site = self.tmp / "site"
        self.live(
            files={
                "index.html": b"<p>",
                ".well-known/a.txt": b"x",
                ".artifact.json": b'{"slug": "evil"}',
            }
        )
        code, _, err = self.run_cli("pull", "s1", site)
        self.assertEqual(code, 0)
        self.assertIn("left out 2 dot-path file(s)", err)
        self.assertFalse((site / ".well-known").exists())
        state = json.loads((site / ".artifact.json").read_text())
        self.assertEqual(state["slug"], "s1")
        self.assertEqual(set(state["files"]), {"index.html"})

    @unittest.skipUnless(hasattr(os, "symlink"), "needs symlinks")
    def test_read_out_dir_refuses_symlinked_dot_dir(self):
        outside = self.tmp / "outside"
        outside.mkdir()
        out = self.tmp / "out"
        out.mkdir()
        os.symlink(outside, out / ".well-known")
        self.live(files={".well-known/a.txt": b"x"})
        with self.assertRaises(SystemExit) as ctx:
            self.run_cli("read", "s1", "--path", ".well-known/a.txt", "--out-dir", out)
        self.assertIn("symlink", str(ctx.exception))
        self.assertEqual(os.listdir(outside), [])

    def test_unsafe_manifest_path_not_written(self):
        self.live(files={"../evil.txt": b"x"})
        code, _, err = self.run_cli("pull", "s1", self.tmp / "site")
        self.assertEqual(code, 1)
        self.assertIn("not a clean relative path", err)
        self.assertFalse((self.tmp / "evil.txt").exists())


class NoAssetTokenOutputTest(FilesCase):
    def test_token_never_reaches_output_or_disk(self):
        self.live(files={"index.html": b"<p>hi</p>", "a.css": b"a"}, typed=("type.js",))
        outputs = []
        for argv in (
            ["pull", "s1", self.tmp / "site"],
            ["read", "s1", "--path", "a.css"],
            ["read", "s1", "--path", "index.html", "--path", "a.css", "--out-dir", self.tmp / "o"],
            ["read", "s1", "--path", "missing"],
            ["read", "s1", "--json"],
        ):
            outputs.extend(self.run_cli(*argv)[1:])
        self.assert_no_token(*outputs)
        for path in self.tmp.rglob("*"):
            if path.is_file():
                self.assert_no_token(path.read_bytes())


# -- publishing a typed directory --------------------------------------------


class TypedPublishTest(FilesCase):
    def make_site(self, **state):
        self.write("site/data.json", '{"a":1}')
        self.write("site/notes/new.txt", "n")
        self.write(
            "site/.artifact.json",
            json.dumps(
                {
                    "slug": "t1",
                    "url": URL.format("t1"),
                    "version": "v1",
                    "title": "Typed T",
                    "typed": True,
                    "files": {"data.json": sha(b"{}"), "old.csv": sha(b"a,b")},
                    **state,
                }
            ),
        )
        return self.tmp / "site"

    def test_patches_own_files_and_removes_local_deletions(self):
        site = self.make_site()
        server = self.serve({DEPLOY: {"slug": "t1", "version": "v2"}})
        code, out, _ = self.run_cli("publish", site, "-q")
        self.assertEqual((code, out.decode().strip()), (0, URL.format("t1")))
        self.assertEqual(server.paths(), [DEPLOY])
        body = server.deployed()
        self.assertEqual(
            (body["mode"], body["slug"], body["baseVersion"], body["title"]),
            ("patch", "t1", "v1", "Typed T"),
        )
        self.assertNotIn("favicon", body)
        self.assertEqual(
            body["manifest"],
            {
                "data.json": {"content": '{"a":1}', "contentType": "application/json"},
                "notes/new.txt": {"content": "n", "contentType": "text/plain"},
                "old.csv": None,
            },
        )
        self.assertEqual(
            self.state(),
            {
                "slug": "t1",
                "url": URL.format("t1"),
                "version": "v2",
                "title": "Typed T",
                "files": {"data.json": sha(b'{"a":1}'), "notes/new.txt": sha(b"n")},
                "typed": True,
            },
        )

    def test_explicit_title_and_remove(self):
        site = self.make_site()
        server = self.serve({DEPLOY: {"slug": "t1", "version": "v2"}})
        code, _, _ = self.run_cli("publish", site, "-q", "--title", "New", "--remove", "x.png")
        self.assertEqual(code, 0)
        body = server.deployed()
        self.assertEqual(body["title"], "New")
        self.assertEqual(
            sorted(p for p, v in body["manifest"].items() if v is None), ["old.csv", "x.png"]
        )
        self.assertEqual(self.state()["title"], "New")

    def test_refusals(self):
        for setup, argv in (
            (lambda: None, ["--mode", "replace"]),
            (lambda: self.write("site/index.html", "<p>"), []),
            (lambda: None, ["--slug", "other"]),  # no longer typed: needs an index.html
        ):
            with self.subTest(argv=argv):
                site = self.make_site()
                setup()
                server = self.serve({})
                with self.assertRaises(SystemExit):
                    self.run_cli("publish", site, "-q", *argv)
                self.assertEqual(server.calls, [])
                (site / "index.html").unlink(missing_ok=True)

    def test_local_index_message(self):
        site = self.make_site()
        self.write("site/index.html", "<p>")
        self.serve({})
        with self.assertRaises(SystemExit) as ctx:
            self.run_cli("publish", site)
        self.assertIn("belongs to the artifact's type", str(ctx.exception))


# -- status ----------------------------------------------------------------------


class StatusFilesTest(FilesCase):
    def make_dir(self, slug="d1", version="v3", **files):
        for path, data in files.items():
            self.write(f"site/{path}", data)
        self.write("site/.artifact.json", json.dumps({"slug": slug, "version": version}))
        return self.tmp / "site"

    def test_entries_without_sha256_are_fetched(self):
        boot_answer = {
            "ver": "v3",
            "assetToken": TOKEN,
            "files": [{"path": "index.html"}, {"path": "app.css"}, {"path": "remote.js"}],
        }
        server = self.serve({boot_path("d1"): boot_answer, ("GET", "/api/frame/read/d1"): {}})
        host = self.host(
            {src_path("v3", "index.html"): (200, b"<p>"), src_path("v3", "app.css"): (200, b"old")}
        )
        site = self.make_dir(**{"index.html": "<p>", "app.css": "new"})
        code, out, _ = self.run_cli("status", site, "--json")
        self.assertEqual(code, 0)
        self.assertEqual(
            json.loads(out)["files"],
            [
                {"state": "changed", "path": "app.css"},
                {"state": "same", "path": "index.html"},
                {"state": "remote-only", "path": "remote.js"},
            ],
        )
        self.assertEqual(
            sorted(host.calls), [src_path("v3", "app.css"), src_path("v3", "index.html")]
        )
        self.assertEqual(server.paths().count(boot_path("d1")), 2)  # read + one reader
        self.assert_no_token(out)

    def test_type_owned_files_excluded_and_in_sync_is_quiet(self):
        self.live(slug="d1", ver="v3", files={"data.json": b"{}"}, typed=("index.html",))
        site = self.make_dir(**{"data.json": "{}"})
        code, out, err = self.run_cli("status", site)
        self.assertEqual(code, 0)
        self.assertIn("same          data.json", out.decode())
        self.assertNotIn("index.html", out.decode())
        self.assertEqual(err, "")

    def test_linked_dir_out_of_sync_hints_pull_into_it(self):
        for version, files in (("v2", {"data.json": "{}"}), ("v3", {"data.json": "edited"})):
            with self.subTest(version=version):
                self.live(slug="d1", ver="v3", files={"data.json": b"{}"})
                site = self.make_dir(version=version, **files)
                code, _, err = self.run_cli("status", site)
                self.assertEqual(code, 0)
                self.assertIn(f"merge the live version in: claude-artifact pull d1 {site}", err)

    def test_unlinked_target_hints_pull_elsewhere(self):
        self.live(slug="x9", ver="v1", files={"index.html": b"<p>live</p>"})
        site = self.make_dir(**{"index.html": "<p>mine</p>"})
        page = self.write("page.html", "<p>mine</p>")
        for target in (site, page):
            with self.subTest(target=target.name):
                code, _, err = self.run_cli("status", target, "--slug", "x9")
                self.assertEqual(code, 0)
                self.assertIn("live copy: claude-artifact pull x9 <another-dir>", err)
                self.assertNotIn("merge the live version in", err)


if __name__ == "__main__":
    unittest.main()
