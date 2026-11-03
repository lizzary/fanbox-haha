"""Real browser against simulated X pages; navigation and scrolling trigger the APIs."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlsplit

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import async_playwright

from fanbox_lab import LabError, arguments
from x_lab import XExperiment
from test_x_data import timeline_payload, tweet, tweet_entry, user


def user_entry(handle, identifier):
    return {"entryId": "user-" + identifier,
            "content": {"itemContent": {"user_results": {"result": user(handle, identifier)}}}}


class XBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.pw = await async_playwright().start()
        self.addAsyncCleanup(self.pw.stop)
        for channel in ("chrome", "msedge", None):
            try:
                self.browser = await self.pw.chromium.launch(headless=True, **({"channel": channel} if channel else {}))
                self.channel = channel or "chromium"
                break
            except PlaywrightError:
                pass
        else:
            self.skipTest("No Chromium browser installed")
        self.addAsyncCleanup(self.browser.close)
        self.lab = XExperiment(arguments(["check", "--platform", "x", "--data-dir", self.temp.name, "--timeout", "3"]))
        self.expired = False
        self.block_second_page = False
        self.rate_limit = False

    async def context(self, **kwargs):
        context = await self.browser.new_context(**kwargs)
        self.addAsyncCleanup(context.close)
        await context.route("**/*", self.route)
        await self.lab.prepare_context(context)
        return context

    async def signed_in(self):
        context = await self.context()
        await context.add_cookies([{"name": "x_test_session", "value": "valid", "domain": ".x.com", "path": "/", "secure": True}])
        page = await context.new_page()
        await self.lab.verify(page)
        return context, page

    async def route(self, route):
        parsed = urlsplit(route.request.url)
        authenticated = "x_test_session=valid" in (await route.request.all_headers()).get("cookie", "") and not self.expired
        if parsed.hostname != "x.com":
            await route.abort()
            return
        if "/i/api/graphql/" in parsed.path:
            operation = parsed.path.rsplit("/", 1)[-1]
            variables = json.loads(parse_qs(parsed.query)["variables"][0])
            status = 200
            if not authenticated:
                payload, status = {"errors": [{"code": 89}]}, 401
            elif operation == "HomeTimeline":
                payload = {"data": {"home": {"home_timeline_urt": {"instructions": []}}}}
            elif operation == "UserByScreenName":
                handle = variables["screen_name"]
                payload = {"data": {"user": {"result": user(handle, "900" if handle == "selfuser" else "901")}}}
            elif operation == "Following":
                if variables.get("cursor"):
                    payload = timeline_payload([user_entry("other", "902"), user_entry("artist", "901")], cursor="0|end")
                    if self.rate_limit:
                        payload, status = {"errors": [{"code": 88}]}, 429
                else:
                    payload = timeline_payload([user_entry("other", "902")], cursor="users-next")
            elif operation == "UserOriginalsTimeline":
                if variables.get("cursor"):
                    payload = timeline_payload([tweet_entry(tweet("111")), tweet_entry(tweet("112"))], ended=True)
                else:
                    payload = timeline_payload([tweet_entry(tweet("111")), tweet_entry(tweet("997", "other"))], cursor="posts-next")
            elif operation == "UserRepliesTimeline":
                payload = timeline_payload([tweet_entry(tweet("113")), tweet_entry(tweet("998", "other"))], ended=True)
            elif operation == "UserRepostsTimeline":
                repost = tweet("114")
                repost["legacy"]["retweeted_status_result"] = {"result": tweet("115", "other", "Reposted test")}
                payload = timeline_payload([tweet_entry(repost)], ended=True)
            elif operation == "TweetDetail":
                value = tweet("112", text="Full selected post")
                value["legacy"]["extended_entities"] = {"media": [
                    {"id_str": "image1", "type": "photo", "media_url_https": "https://pbs.twimg.com/media/photo.jpg"},
                    {"id_str": "video1", "type": "video", "video_info": {"variants": [
                        {"content_type": "video/mp4", "bitrate": 100, "url": "https://video.twimg.com/movie.mp4"},
                    ]}},
                ]}
                payload = {"data": {"threaded_conversation_with_injections_v2": {
                    "instructions": [{"type": "TimelineAddEntries", "entries": [tweet_entry(tweet("111")), tweet_entry(value)]}],
                }}}
            else:
                raise AssertionError("Unexpected read operation: " + operation)
            await route.fulfill(status=status, content_type="application/json", body=json.dumps(payload))
            return
        if not authenticated and parsed.path != "/i/flow/login":
            # Keep the entire fixture local to routing, including the expired-session UI.
            await route.fulfill(content_type="text/html", body='<input autocomplete="username">')
            return
        if parsed.path == "/i/flow/login":
            await route.fulfill(content_type="text/html", body='<input autocomplete="username">')
            return
        operation, variables, next_cursor = None, {}, None
        if parsed.path == "/home":
            operation = "HomeTimeline"
        elif parsed.path == "/selfuser/following":
            operation, variables, next_cursor = "Following", {"userId": "900"}, "users-next"
        elif parsed.path == "/artist":
            operation, variables, next_cursor = "UserOriginalsTimeline", {"userId": "901"}, "posts-next"
        elif parsed.path == "/artist/with_replies":
            operation, variables = "UserRepliesTimeline", {"userId": "901"}
        elif parsed.path == "/artist/reposts":
            operation, variables = "UserRepostsTimeline", {"userId": "901"}
        elif parsed.path.startswith("/i/status/"):
            operation, variables = "TweetDetail", {"focalTweetId": parsed.path.rsplit("/", 1)[-1]}
        if operation is None:
            await route.abort()
            return
        config = json.dumps({"operation": operation, "variables": variables,
                             "next": None if self.block_second_page else next_cursor})
        body = ('<!doctype html><html><body style="height:6000px">'
                '<a data-testid="AppTabBar_Profile_Link" href="/selfuser">Profile</a>'
                '<button data-testid="SideNav_AccountSwitcher_Button">Account</button>'
                '<a role="tab" href="/artist">Posts</a><a role="tab" href="/artist/with_replies">Replies</a>'
                '<a role="tab" href="/artist/reposts">Reposts</a>'
                '<script>const config = ' + config + ';'
                'function api(op, vars) { return fetch("/i/api/graphql/native/" + op + "?variables=" + encodeURIComponent(JSON.stringify(vars))); }'
                'api(config.operation, config.variables);'
                'api("UserByScreenName", {screen_name: location.pathname.includes("artist") ? "artist" : "selfuser"});'
                'let loaded = false; addEventListener("scroll", () => { if (!loaded && config.next) {'
                'loaded = true; api(config.operation, {...config.variables, cursor: config.next}); }});'
                '</script></body></html>')
        await route.fulfill(content_type="text/html", body=body)

    async def test_native_following_and_post_pagination(self):
        _, page = await self.signed_in()
        authors = await self.lab.authors(page)
        self.assertEqual([item["creatorId"] for item in authors], ["other", "artist"])
        self.assertTrue(self.lab.report["author_list_complete"])
        self.lab.creator = "artist"
        posts = await self.lab.posts(page)
        self.assertEqual([item["id"] for item in posts], ["111", "112", "113", "115"])
        self.assertTrue(self.lab.report["all_posts_loaded"])
        self.assertEqual(self.lab.report["post_page_count"], 4)
        self.assertEqual(self.lab.report["post_timeline_pages"], {"posts": 2, "replies": 1, "reposts": 1})

    async def test_saved_x_session_and_two_selections_archive_media(self):
        context, _ = await self.signed_in()
        self.lab.channel = self.channel
        await self.lab.save_state(context)
        await context.close()
        fanbox_auth = Path(self.temp.name) / "auth" / "storage-state.json"
        fanbox_auth.parent.mkdir(parents=True)
        fanbox_auth.write_text('{"fanbox":"unchanged"}', encoding="utf-8")
        self.lab.args.command = "lab"
        original_context = self.browser.new_context

        async def routed_context(**kwargs):
            created = await original_context(**kwargs)
            await created.route("**/*", self.route)
            return created

        async def download(context, asset, destination, *_):
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(b"fixture media")
            return {"status": "downloaded", "bytes": 13, "sha256": "fixture"}

        with patch.object(self.lab, "launch", AsyncMock(return_value=self.browser)), \
                patch.object(self.browser, "new_context", side_effect=routed_context), \
                patch.object(self.lab, "login", AsyncMock()) as login, \
                patch("builtins.input", side_effect=["2", "2"]), \
                patch("fanbox_lab.download_asset", side_effect=download):
            await self.lab.run()
        login.assert_not_awaited()
        self.assertTrue(self.lab.report["storage_state_reused"])
        self.assertTrue(self.lab.report["archive_complete"])
        self.assertEqual(self.lab.report["post_id"], "112")
        self.assertEqual(self.lab.report["downloaded_assets"], 2)
        self.assertIn("<video controls", (self.lab.output / "index.html").read_text(encoding="utf-8"))
        self.assertEqual(json.loads(fanbox_auth.read_text())["fanbox"], "unchanged")
        self.assertEqual(self.lab.auth.parent.parent.name, "x")

    async def test_expired_session_is_distinct_from_page_failure(self):
        _, page = await self.signed_in()
        self.expired = True
        with self.assertRaises(LabError) as result:
            await self.lab.verify(page)
        self.assertEqual(result.exception.code, 3)

    async def test_temporary_login_limit_stops_instead_of_waiting_or_retrying(self):
        context = await self.context()
        page = await context.new_page()
        await page.route("https://x.com/i/jf/onboarding/web?mode=login", lambda route: route.fulfill(
            content_type="text/html; charset=utf-8", body="<meta charset='utf-8'><p>我们已临时限制你的登录。请稍后重试。</p>"))
        await page.goto("https://x.com/i/jf/onboarding/web?mode=login")
        self.assertIn("我们已临时限制你的登录", await page.locator("body").inner_text())
        with self.assertRaises(LabError) as result:
            await self.lab.viewer(page)
        self.assertEqual(result.exception.code, 5)
        self.assertEqual(self.lab.report["login_blocked"], "temporary_login_limit")

    async def test_rate_limit_preserves_partial_following_without_completing(self):
        _, page = await self.signed_in()
        self.rate_limit = True
        with self.assertRaisesRegex(LabError, "429"):
            await self.lab.authors(page)
        self.assertFalse(self.lab.report["author_list_complete"])
        self.assertEqual(len(json.loads((self.lab.output / "following.json").read_text())), 1)

    async def test_stalled_page_is_not_treated_as_exhausted(self):
        _, page = await self.signed_in()
        self.block_second_page = True
        with self.assertRaisesRegex(LabError, "尚不能确认"):
            await self.lab.authors(page)
        self.assertFalse(self.lab.report["author_list_complete"])

    async def test_page_limit_is_explicitly_partial(self):
        _, page = await self.signed_in()
        self.lab.args.max_pages = 1
        with self.assertRaisesRegex(LabError, "上限"):
            await self.lab.authors(page)
        self.assertFalse(self.lab.report["author_list_complete"])

    async def test_detail_does_not_save_another_tweet_in_the_conversation(self):
        _, page = await self.signed_in()
        with self.assertRaises(LabError) as result:
            await self.lab.fetch_post(page, "999")
        self.assertEqual(result.exception.code, 2)
        self.assertFalse((self.lab.output / "post.json").exists())
