import hashlib
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import async_playwright

from fanbox_media import DownloadError, asset_filename, download_asset, validate_media_url


PAYLOAD = bytes(range(256)) * 9000  # > 2 MiB: exercises several browser-to-Python chunks.


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        if "media_session=valid" not in self.headers.get("Cookie", "") or not self.headers.get("Referer"):
            self.send_error(403)
            return
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/attachment")
            self.end_headers()
            return
        if self.path == "/denied":
            self.send_error(403)
            return
        payload = b"<html>login</html>" if self.path == "/html" else PAYLOAD
        self.send_response(200)
        self.send_header("Content-Type", "text/html" if self.path == "/html" else "application/octet-stream")
        self.send_header("Content-Length", str(len(payload)))
        if self.path == "/attachment":
            self.send_header("Content-Disposition", 'attachment; filename="original.zip"')
        self.end_headers()
        self.wfile.write(payload)


class MediaNameTests(unittest.TestCase):
    def test_filename_cannot_escape_media_directory(self):
        for value in ("../../escape", "CON", "a:b\\c/?.zip", "a" * 300):
            name = asset_filename({"name": value, "extension": "zip", "url": "https://downloads.fanbox.cc/file"}, 1)
            self.assertNotIn("/", name)
            self.assertNotIn("\\", name)
            self.assertTrue(name.startswith("001-"))
            self.assertLess(len(name), 110)

    def test_untrusted_redirect_host_is_rejected(self):
        for url in ("https://fanbox.cc.example.com/file", "http://downloads.fanbox.cc/file", "file:///c:/secret"):
            with self.assertRaises(DownloadError):
                validate_media_url(url)


class MediaBrowserTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.pw = await async_playwright().start()
        self.addAsyncCleanup(self.pw.stop)
        for channel in ("chrome", "msedge", None):
            try:
                self.browser = await self.pw.chromium.launch(headless=True, **({"channel": channel} if channel else {}))
                break
            except PlaywrightError:
                continue
        else:
            self.skipTest("No Chromium browser installed")
        self.addAsyncCleanup(self.browser.close)
        self.context = await self.browser.new_context()
        await self.context.add_cookies([{"name": "media_session", "value": "valid", "url": self.base}])
        self.destination = Path(self.temp.name) / "asset.zip"

        def local_only(url):
            if not url.startswith(self.base + "/"):
                raise DownloadError("Unexpected test URL")
            return url

        patcher = patch("fanbox_media.validate_media_url", side_effect=local_only)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_streams_redirected_attachment_with_cookie_and_referer(self):
        result = await download_asset(self.context, {"kind": "file", "url": self.base + "/redirect"},
                                      self.destination, self.base + "/post")
        self.assertEqual(result["bytes"], len(PAYLOAD))
        self.assertEqual(result["sha256"], hashlib.sha256(PAYLOAD).hexdigest())
        self.assertEqual(self.destination.read_bytes(), PAYLOAD)
        self.assertFalse(self.destination.with_suffix(".zip.part").exists())

    async def test_bad_response_leaves_no_complete_or_partial_file(self):
        for path in ("/denied", "/html"):
            with self.subTest(path=path), self.assertRaises(DownloadError):
                await download_asset(self.context, {"kind": "file", "url": self.base + path},
                                     self.destination, self.base + "/post")
            self.assertFalse(self.destination.exists())
            self.assertFalse(self.destination.with_suffix(".zip.part").exists())

    async def test_size_mismatch_is_not_a_successful_download(self):
        with self.assertRaisesRegex(DownloadError, "长度不符"):
            await download_asset(self.context, {"kind": "file", "url": self.base + "/attachment", "size": 3},
                                 self.destination, self.base + "/post")
        self.assertFalse(self.destination.exists())
        self.assertFalse(self.destination.with_suffix(".zip.part").exists())
