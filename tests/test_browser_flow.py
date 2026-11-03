"""Real Chromium, mocked Fanbox responses: no real account or network is used."""

import html
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlsplit

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import async_playwright

from fanbox_lab import Experiment, HOME, LabError, arguments


class BrowserFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.pw = await async_playwright().start()
        self.addAsyncCleanup(self.pw.stop)
        self.browser = None
        for channel in ("chrome", "msedge", None):
            try:
                options = {"channel": channel} if channel else {}
                self.browser = await self.pw.chromium.launch(headless=True, **options)
                self.channel = channel
                break
            except PlaywrightError:
                pass
        if self.browser is None:
            self.skipTest("Install Chrome/Edge or run python -m playwright install chromium")
        self.addAsyncCleanup(self.browser.close)
        args = arguments(["check", "--headless", "--data-dir", self.temp.name])
        self.lab = Experiment(args)
        self.expired = False
        self.api_status = 200
        self.fail_second_page = False
        self.fail_supporting = False

    async def route(self, route):
        url = route.request.url
        authenticated = "lab_session=valid" in (await route.request.all_headers()).get("cookie", "")
        authenticated = authenticated and not self.expired
        if url.startswith("https://www.fanbox.cc/"):
            metadata = {"context": {"user": {"userId": "42" if authenticated else None}},
                        "urlContext": {"user": {"isLoggedIn": authenticated}}}
            body = '<meta name="metadata" content="' + html.escape(json.dumps(metadata), quote=True) + '">'
            headers = {"content-type": "text/html"}
            if url.endswith("/test-login"):
                headers["set-cookie"] = "lab_session=valid; Domain=.fanbox.cc; Path=/; Secure; HttpOnly; SameSite=Lax"
            await route.fulfill(status=200, headers=headers, body=body)
        elif url.startswith("https://api.fanbox.cc/"):
            query = parse_qs(urlsplit(url).query)
            identifier = query.get("postId", ["123"])[0]
            post = {"id": identifier, "creatorId": "szcb911", "title": "Local fixture", "type": "text",
                    "feeRequired": 300, "isRestricted": not authenticated,
                    "body": {"text": "Full paid content"} if authenticated else None}
            status = self.api_status
            if "/creator.listFollowing" in url:
                if query.get("page") == ["2"]:
                    body = {"creators": [{"creatorId": "szcb911", "user": {"name": "Selected creator"}}]}
                else:
                    body = {"creators": [{"creatorId": "first", "user": {"name": "First creator"}}],
                            "nextUrl": "https://api.fanbox.cc/creator.listFollowing?page=2"}
            elif "/plan.listSupporting" in url:
                if self.fail_supporting:
                    status = 503
                if query.get("page") == ["2"]:
                    body = {"plans": [{"id": "p3", "creatorId": "supported-only", "user": {"name": "Supported only"}}]}
                else:
                    body = {"plans": [
                        {"id": "p1", "creatorId": "szcb911", "title": "Plan one", "user": {"name": "Selected creator"}},
                        {"id": "p2", "creatorId": "szcb911", "title": "Plan two", "user": {"name": "Selected creator"}},
                    ], "nextUrl": "https://api.fanbox.cc/plan.listSupporting?page=2"}
            elif "/post.paginateCreator?" in url:
                body = {"pageUrls": [f"https://api.fanbox.cc/post.listCreator?creatorId=szcb911&page={n}" for n in (1, 2)]}
            elif "/post.listCreator?" in url:
                body = {"posts": [post]}
                if query.get("page") == ["2"]:
                    body["posts"].append(dict(post, id="124"))
                    if self.fail_second_page:
                        status = 503
            else:
                body = {"post": post}
            await route.fulfill(status=status, headers={
                "content-type": "application/json", "access-control-allow-origin": HOME.rstrip("/"),
                "access-control-allow-credentials": "true",
            }, body=json.dumps({"body": body}))
        else:
            await route.abort()  # No request is allowed to reach the internet.

    async def context(self, *, storage_state=None):
        context = await self.browser.new_context(storage_state=storage_state, service_workers="block")
        self.addAsyncCleanup(context.close)
        await context.route("**/*", self.route)
        return context

    async def test_export_restart_verify_and_fetch_with_cookie(self):
        first = await self.context()
        page = await first.new_page()
        await page.goto(HOME + "test-login")
        await self.lab.verify(page)
        self.lab.channel = self.channel or "chromium"
        await self.lab.save_state(first)
        await first.close()
        await self.browser.close()

        # Restart the browser process: the only bridge is storage-state.json.
        options = {"channel": self.channel} if self.channel else {}
        self.browser = await self.pw.chromium.launch(headless=True, **options)
        self.addAsyncCleanup(self.browser.close)
        second = await self.context(storage_state=str(self.lab.auth))
        page = await second.new_page()
        viewer = await self.lab.verify(page)
        self.assertEqual(viewer["user_id"], "42")
        posts = await self.lab.posts(page)
        self.assertEqual(len(posts), 2)
        self.assertTrue(self.lab.report["all_posts_loaded"])
        await self.lab.fetch_post(page, "123")
        result = json.loads((self.lab.output / "post.json").read_text(encoding="utf-8"))
        self.assertEqual(result["access"], "available")
        self.assertEqual(result["text"], "Full paid content")
        self.assertTrue(result["archive_complete"])
        self.assertTrue((self.lab.output / "index.html").is_file())
        self.assertNotIn("lab_session", json.dumps(self.lab.report))

    async def test_anonymous_http_200_does_not_mean_body_available(self):
        context = await self.context()
        page = await context.new_page()
        await page.goto(HOME)
        with self.assertRaises(LabError) as error:
            await self.lab.fetch_post(page, "123")
        self.assertEqual(error.exception.code, 2)
        self.assertEqual(self.lab.report["requests"][0]["http_status"], 200)
        self.assertEqual(self.lab.report["post_access"], "restricted")
        self.assertTrue((self.lab.output / "post.raw.json").is_file())

    async def test_cookie_presence_does_not_mean_session_valid(self):
        context = await self.context()
        page = await context.new_page()
        await page.goto(HOME + "test-login")
        self.expired = True
        with self.assertRaises(LabError) as error:
            await self.lab.verify(page)
        self.assertEqual(error.exception.code, 3)
        self.assertFalse(self.lab.report["login_verified"])

    async def test_http_403_is_recorded_as_failure(self):
        context = await self.context()
        page = await context.new_page()
        await page.goto(HOME)
        self.api_status = 403
        with self.assertRaisesRegex(LabError, "HTTP 403"):
            await self.lab.posts(page)
        self.assertEqual(self.lab.report["requests"][0]["http_status"], 403)

    async def test_pagination_failure_is_not_reported_as_all_posts(self):
        context = await self.context()
        page = await context.new_page()
        await page.goto(HOME)
        self.fail_second_page = True
        with self.assertRaisesRegex(LabError, "HTTP 503"):
            await self.lab.posts(page)
        self.assertFalse(self.lab.report["all_posts_loaded"])
        self.assertTrue((self.lab.output / "list-pages" / "0001.json").is_file())

    async def test_valid_credentials_skip_login_and_run_both_selections(self):
        first = await self.context()
        page = await first.new_page()
        await page.goto(HOME + "test-login")
        self.lab.channel = self.channel or "chromium"
        await self.lab.save_state(first)
        await first.close()
        self.lab.args.command = "lab"
        original_new_context = self.browser.new_context

        async def routed_context(**kwargs):
            context = await original_new_context(**kwargs)
            await context.route("**/*", self.route)
            return context

        with patch.object(self.lab, "launch", AsyncMock(return_value=self.browser)), \
                patch.object(self.browser, "new_context", side_effect=routed_context), \
                patch.object(self.lab, "login", AsyncMock()) as login, \
                patch("builtins.input", side_effect=["invalid", "2", "2"]):
            await self.lab.run()
        login.assert_not_awaited()
        self.assertEqual(self.lab.report["following_count"], 2)
        self.assertEqual(self.lab.report["supporting_count"], 2)
        self.assertEqual(self.lab.report["author_count"], 3)
        self.assertEqual(self.lab.report["creator_id"], "szcb911")
        self.assertEqual(self.lab.report["post_id"], "124")
        self.assertTrue(self.lab.report["archive_complete"])

    async def test_authors_merge_both_lists_and_deduplicate_plans(self):
        context = await self.context()
        page = await context.new_page()
        await page.goto(HOME)
        authors = await self.lab.authors(page)
        self.assertEqual([author["creatorId"] for author in authors], ["first", "szcb911", "supported-only"])
        self.assertEqual(authors[0]["sources"], ["following"])
        self.assertEqual(authors[1]["sources"], ["following", "supporting"])
        self.assertEqual(authors[2]["sources"], ["supporting"])
        self.assertTrue(authors[1]["isSupported"])
        self.assertNotIn("title", authors[1])
        with patch("builtins.input", return_value="3"):
            self.assertEqual((await self.lab.choose(authors, "作者", "creatorId"))["creatorId"], "supported-only")
        self.assertEqual(len(json.loads((self.lab.output / "supporting.json").read_text(encoding="utf-8"))), 2)
        self.assertTrue(self.lab.report["author_list_complete"])

    async def test_supporting_failure_does_not_silently_use_following_only(self):
        context = await self.context()
        page = await context.new_page()
        await page.goto(HOME)
        self.fail_supporting = True
        with self.assertRaisesRegex(LabError, "HTTP 503"):
            await self.lab.authors(page)
        self.assertFalse(self.lab.report["author_list_complete"])
        self.assertFalse((self.lab.output / "authors.json").exists())

    async def test_expired_state_triggers_login_then_rechecks(self):
        self.lab.args.command = "lab"
        reopened = (object(), object(), object())
        with patch.object(self.lab, "open_session", AsyncMock(side_effect=[LabError("expired", 3), reopened])) as check, \
                patch.object(self.lab, "login", AsyncMock()) as login:
            self.assertEqual(await self.lab.ensure_session(self.pw), reopened)
        self.assertEqual(check.await_count, 2)
        login.assert_awaited_once()

    async def test_network_failure_does_not_claim_session_expired(self):
        self.lab.args.command = "lab"
        with patch.object(self.lab, "open_session", AsyncMock(side_effect=LabError("HTTP 503"))), \
                patch.object(self.lab, "login", AsyncMock()) as login:
            with self.assertRaises(LabError):
                await self.lab.ensure_session(self.pw)
        login.assert_not_awaited()

    async def test_failed_attachment_keeps_successes_and_marks_archive_partial(self):
        context = await self.context()
        page = await context.new_page()
        await page.goto(HOME)
        post = {"id": "123", "creatorId": "szcb911", "body": {"files": [
            {"id": "one", "name": "one", "url": "https://downloads.fanbox.cc/one.zip"},
            {"id": "two", "name": "two", "url": "https://downloads.fanbox.cc/two.zip"},
        ]}}

        async def download(context, asset, destination, *_):
            if asset["id"] == "two":
                raise OSError("simulated disk/network failure")
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(b"zip")
            return {"status": "downloaded", "bytes": 3, "sha256": "test"}

        with patch.object(self.lab, "api", AsyncMock(return_value={"body": {"post": post}})), \
                patch("fanbox_lab.download_asset", side_effect=download):
            with self.assertRaises(LabError) as error:
                await self.lab.fetch_post(page, "123")
        self.assertEqual(error.exception.code, 4)
        self.assertFalse(self.lab.report["archive_complete"])
        self.assertEqual(self.lab.report["downloaded_assets"], 1)
        assets = json.loads((self.lab.output / "assets.json").read_text(encoding="utf-8"))
        self.assertEqual(assets[1]["status"], "failed")
        self.assertEqual((self.lab.output / assets[0]["local_path"]).read_bytes(), b"zip")

    async def test_external_video_is_preserved_without_claiming_full_download(self):
        context = await self.context()
        page = await context.new_page()
        await page.goto(HOME)
        post = {"id": "123", "body": {"video": {"serviceProvider": "youtube", "videoId": "test"}}}
        with patch.object(self.lab, "api", AsyncMock(return_value={"body": {"post": post}})):
            with self.assertRaises(LabError) as error:
                await self.lab.fetch_post(page, "123")
        self.assertEqual(error.exception.code, 4)
        self.assertFalse(self.lab.report["archive_complete"])
        embeds = json.loads((self.lab.output / "external-embeds.json").read_text(encoding="utf-8"))
        self.assertEqual(embeds[0]["videoId"], "test")


if __name__ == "__main__":
    unittest.main()
