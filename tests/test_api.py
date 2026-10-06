import base64
import io
import json
import os
import unittest
import urllib.error
from unittest import mock

from claude_artifact_cli import api

# Never reach PyPI or upgrade the install running the tests.
os.environ.setdefault("CLAUDE_ARTIFACT_NO_UPDATE_CHECK", "1")

DEPLOY = ("POST", "/api/frame/deploy/direct")


def boot_path(slug):
    return ("GET", f"/api/frame/{slug}?via=model_read")


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
        if isinstance(answer, Exception):
            raise answer
        return answer

    def paths(self):
        return [(m, p) for m, p, _ in self.calls]

    def body(self, method="POST", path="/api/frame/deploy/direct"):
        bodies = [b for m, p, b in self.calls if (m, p) == (method, path)]
        assert len(bodies) == 1, f"expected one {method} {path}, got {len(bodies)}"
        return bodies[0]


def page(html="<title>T</title>hi"):
    return api.Asset("index.html", html.encode(), "text/html")


class ApiCase(unittest.TestCase):
    def serve(self, routes):
        server = Server(routes)
        patcher = mock.patch.object(api.FrameClient, "_request", side_effect=server)
        patcher.start()
        self.addCleanup(patcher.stop)
        return server

    def client(self):
        return api.FrameClient("test-token")


class PublishModeTest(ApiCase):
    def test_new_artifact_defaults_to_replace(self):
        server = self.serve({DEPLOY: {"slug": "new1", "version": "v1"}})
        result = self.client().publish(page(), meta={"title": "T"})
        self.assertEqual(result["slug"], "new1")
        self.assertEqual(server.paths(), [DEPLOY])
        body = server.body()
        self.assertEqual(body["mode"], "replace")
        self.assertEqual(body["title"], "T")
        self.assertNotIn("slug", body)
        self.assertNotIn("baseVersion", body)
        self.assertEqual(body["manifest"]["index.html"]["content"], "<title>T</title>hi")

    def test_update_defaults_to_patch_onto_live_version(self):
        server = self.serve({boot_path("s1"): {"ver": "v7"}, DEPLOY: {"slug": "s1"}})
        self.client().publish(page(), slug="s1")
        self.assertEqual(server.paths(), [boot_path("s1"), DEPLOY])
        body = server.body()
        self.assertEqual((body["mode"], body["slug"], body["baseVersion"]), ("patch", "s1", "v7"))
        self.assertEqual(list(body["manifest"]), ["index.html"])

    def test_explicit_base_version_skips_lookup(self):
        for mode in ("patch", "replace"):
            with self.subTest(mode=mode):
                server = self.serve({DEPLOY: {"slug": "s1"}})
                self.client().publish(page(), slug="s1", mode=mode, base_version="v3")
                self.assertEqual(server.paths(), [DEPLOY])
                self.assertEqual(server.body()["mode"], mode)
                self.assertEqual(server.body()["baseVersion"], "v3")

    def test_replace_update_without_base_sends_none(self):
        server = self.serve({DEPLOY: {"slug": "s1"}})
        self.client().publish(page(), slug="s1", mode="replace", force=True)
        body = server.body()
        self.assertEqual(server.paths(), [DEPLOY])
        self.assertEqual(body["mode"], "replace")
        self.assertNotIn("baseVersion", body)
        self.assertIs(body["force"], True)

    def test_removals_are_null_entries_in_patch(self):
        server = self.serve({DEPLOY: {"slug": "s1"}})
        css = api.Asset("style.css", b"p{}", "text/css")
        self.client().publish(
            page(), extra=[css], removals=["old.js"], slug="s1", base_version="v1"
        )
        manifest = server.body()["manifest"]
        self.assertEqual(set(manifest), {"index.html", "style.css", "old.js"})
        self.assertIsNone(manifest["old.js"])
        self.assertEqual(server.body()["mode"], "patch")

    def test_invalid_combinations_are_refused_before_any_request(self):
        cases = [
            {"removals": ["a.css"]},
            {"removals": ["a.css"], "slug": "s1", "mode": "replace"},
            {"mode": "patch"},
        ]
        for kwargs in cases:
            with self.subTest(**kwargs):
                server = self.serve({})
                with self.assertRaises(api.ApiError):
                    self.client().publish(page(), **kwargs)
                self.assertEqual(server.calls, [])

    def test_patch_without_known_live_version_errors(self):
        server = self.serve({boot_path("s1"): {"title": "no ver"}})
        with self.assertRaises(api.ApiError):
            self.client().publish(page(), slug="s1")
        self.assertNotIn(DEPLOY, server.paths())

    def test_typed_publish_without_page(self):
        server = self.serve({DEPLOY: {"slug": "t1", "version": "v2"}})
        data = api.Asset("data.json", b"{}", "application/json")
        self.client().publish(
            None, extra=[data], removals=["old.csv"], slug="t1", base_version="v1"
        )
        body = server.body()
        self.assertEqual(body["mode"], "patch")
        self.assertEqual(
            body["manifest"],
            {"data.json": {"content": "{}", "contentType": "application/json"}, "old.csv": None},
        )


class ConflictTest(unittest.TestCase):
    def http_error(self, status, body):
        raw = io.BytesIO(json.dumps(body).encode())
        err = urllib.error.HTTPError("https://x", status, "err", {}, raw)
        self.addCleanup(err.close)
        return err

    def test_409_raises_conflict_error_with_live(self):
        err = self.http_error(409, {"live": "v9"})
        with (
            mock.patch("urllib.request.urlopen", side_effect=err),
            self.assertRaises(api.ConflictError) as ctx,
        ):
            api.FrameClient("test-token").publish(page(), slug="s1", base_version="v1")
        self.assertIsInstance(ctx.exception, api.ApiError)
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(ctx.exception.live, "v9")
        self.assertIn("v9", str(ctx.exception))

    def test_409_message_points_at_pull(self):
        err = self.http_error(409, {"live": "v9"})
        with (
            mock.patch("urllib.request.urlopen", side_effect=err),
            self.assertRaises(api.ConflictError) as ctx,
        ):
            api.FrameClient("test-token").publish(page(), slug="s1", base_version="v1")
        self.assertIn("claude-artifact pull SLUG DIR", str(ctx.exception))
        self.assertIn("--force", str(ctx.exception))

    def test_other_errors_are_plain_api_errors(self):
        err = self.http_error(500, {"message": "boom"})
        with (
            mock.patch("urllib.request.urlopen", side_effect=err),
            self.assertRaises(api.ApiError) as ctx,
        ):
            api.FrameClient("test-token").list_frames()
        self.assertNotIsInstance(ctx.exception, api.ConflictError)
        self.assertEqual(ctx.exception.status, 500)

    def test_live_is_none_without_dict_body(self):
        self.assertIsNone(api.ConflictError("x", 409, "not json").live)


class RedactTest(ApiCase):
    def test_token_keys_redacted_case_insensitively(self):
        data = {
            "assetToken": "SECRET1",
            "nested": [{"SubscriptionTOKEN": "SECRET2", "title": "keep"}],
            "maxToken": 5,
            "tokens": "not a token key",
        }
        self.assertEqual(
            api.redact(data),
            {
                "assetToken": "[redacted]",
                "nested": [{"SubscriptionTOKEN": "[redacted]", "title": "keep"}],
                "maxToken": 5,
                "tokens": "not a token key",
            },
        )
        self.assertEqual(data["assetToken"], "SECRET1")  # input left alone

    def test_frame_t_param_redacted_other_params_kept(self):
        url = "https://h/thumb.png?w=1&__frame_t=SECRET&h=2#frag"
        self.assertEqual(
            api.redact({"thumb": url}),
            {"thumb": "https://h/thumb.png?w=1&__frame_t=[redacted]&h=2#frag"},
        )
        self.assertEqual(api.redact("a?__FRAME_T=x"), "a?__FRAME_T=[redacted]")

    def test_list_frames_redacts(self):
        frames = [{"slug": "a", "thumbnailUrl": "https://h/t?__frame_t=SECRET"}]
        for shape in (frames, {"frames": frames}):
            with self.subTest(shape=type(shape).__name__):
                self.serve({("GET", "/api/frame/frames?limit=200"): shape})
                out = self.client().list_frames()
                self.assertEqual(out[0]["slug"], "a")
                self.assertNotIn("SECRET", json.dumps(out))

    def test_read_redacts_boot(self):
        self.serve(
            {
                boot_path("s1"): {"ver": "v1", "assetToken": "SECRET", "files": []},
                ("GET", "/api/frame/read/s1"): {"public": False},
            }
        )
        data = self.client().read("s1")
        self.assertEqual(data["ver"], "v1")
        self.assertEqual(data["_access"], {"public": False})
        self.assertNotIn("SECRET", json.dumps(data))

    def test_read_redacts_access_response(self):
        self.serve(
            {
                boot_path("s1"): {"ver": "v1"},
                ("GET", "/api/frame/read/s1"): {"viewerToken": "SECRET"},
            }
        )
        self.assertNotIn("SECRET", json.dumps(self.client().read("s1")))


class WireTest(ApiCase):
    def test_text_types_raw_others_base64(self):
        png = bytes(range(10))
        self.assertEqual(api.Asset("a.svg", b"<svg/>", "image/svg+xml").wire(), "<svg/>")
        self.assertEqual(api.Asset("a.json", b"{}", "application/json").wire(), "{}")
        self.assertEqual(
            api.Asset("a.png", png, "image/png").wire(), base64.b64encode(png).decode()
        )
        self.assertEqual(api.guess_content_type("x.unknownext"), "application/octet-stream")

    def test_large_publish_stages_missing_blobs(self):
        big = api.Asset("big.png", b"\x00" * 64, "image/png")
        server = self.serve(
            {
                ("POST", "/api/frame/deploy/prepare"): {
                    "slug": "s1",
                    "missing": [big.sha256, page().sha256],
                },
                ("POST", "/api/frame/upload"): {},
                DEPLOY: {"slug": "s1"},
            }
        )
        with mock.patch.object(api, "INLINE_BUDGET", 10):
            self.client().publish(page(), extra=[big], slug="s1", base_version="v1")
        prep = server.body("POST", "/api/frame/deploy/prepare")
        self.assertEqual(prep["slug"], "s1")
        self.assertEqual(prep["shas"], [big.sha256])  # the page is never staged
        upload = server.body("POST", "/api/frame/upload")
        self.assertEqual([f["path"] for f in upload["files"]], ["big.png"])
        manifest = server.body()["manifest"]
        self.assertEqual(manifest["big.png"], {"sha256": big.sha256, "contentType": "image/png"})
        self.assertIn("content", manifest["index.html"])

    def test_large_new_publish_still_defaults_to_replace(self):
        server = self.serve(
            {
                ("POST", "/api/frame/deploy/prepare"): {"slug": "fresh", "missing": []},
                boot_path("fresh"): api.ApiError("HTTP 404", 404),
                DEPLOY: {"slug": "fresh"},
            }
        )
        with mock.patch.object(api, "INLINE_BUDGET", 10):
            self.client().publish(page(), extra=[api.Asset("big.png", b"\x00" * 64, "image/png")])
        self.assertEqual(server.body()["mode"], "replace")
        self.assertNotIn(boot_path("fresh"), server.paths())


    def test_large_typed_publish_stages_only_extra(self):
        big = api.Asset("big.png", b"\x00" * 64, "image/png")
        server = self.serve(
            {
                ("POST", "/api/frame/deploy/prepare"): {"slug": "t1", "missing": [big.sha256]},
                ("POST", "/api/frame/upload"): {},
                DEPLOY: {"slug": "t1"},
            }
        )
        with mock.patch.object(api, "INLINE_BUDGET", 10):
            self.client().publish(None, extra=[big], slug="t1", base_version="v1")
        self.assertEqual(server.body("POST", "/api/frame/deploy/prepare")["shas"], [big.sha256])
        manifest = server.body()["manifest"]
        self.assertEqual(manifest, {"big.png": {"sha256": big.sha256, "contentType": "image/png"}})

    def test_large_page_alone_is_sent_inline(self):
        server = self.serve({DEPLOY: {"slug": "s1"}})
        with mock.patch.object(api, "INLINE_BUDGET", 10):
            self.client().publish(page("x" * 64), slug="s1", base_version="v1")
        self.assertEqual(server.paths(), [DEPLOY])
        self.assertEqual(server.body()["manifest"]["index.html"]["content"], "x" * 64)


if __name__ == "__main__":
    unittest.main()
