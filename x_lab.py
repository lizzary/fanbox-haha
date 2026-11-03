"""X connector: observe native read requests while navigating and scrolling the site."""

from __future__ import annotations

import asyncio
import json
import re
from urllib.parse import parse_qs, urlsplit

from playwright.async_api import Error as PlaywrightError

from fanbox_lab import Experiment, LabError, write_json
from x_data import (X_HOSTS, detail_tweets, normalize_tweet, original_tweet, status_id,
                    timeline, tweet_author, user_record, username, validate_payload)

AUTH_OPERATIONS = {"HomeTimeline", "HomeLatestTimeline", "Viewer"}
READ_OPERATIONS = AUTH_OPERATIONS | {
    "Following", "UserByScreenName", "UserTweets", "UserTweetsAndReplies",
    "UserOriginalsTimeline", "UserRepliesTimeline", "UserRepostsTimeline",
    "TweetDetail", "TweetResultByRestId",
}


class NativeResponses:
    """Keep only the selected read-operation responses, never headers or auth tokens."""

    def __init__(self, context):
        self.queue = asyncio.Queue()
        self.operations = set()
        self.generation = 0
        self.authenticated = False
        self.auth_error = None
        self.profiles = {}
        self.tasks = set()
        context.on("response", self.observe)
        context.on("close", self.close)

    def close(self, *_):
        for task in tuple(self.tasks):
            task.cancel()

    def select(self, operations):
        self.generation += 1
        self.operations = set(operations)
        while not self.queue.empty():
            self.queue.get_nowait()

    def reset_auth(self):
        self.authenticated = False
        self.auth_error = None

    def observe(self, response):
        parsed = urlsplit(response.url)
        if parsed.hostname not in X_HOSTS or "/i/api/graphql/" not in parsed.path:
            return
        operation = parsed.path.rstrip("/").rsplit("/", 1)[-1]
        if operation not in READ_OPERATIONS:
            return
        if operation not in self.operations | AUTH_OPERATIONS | {"UserByScreenName"}:
            return
        task = asyncio.create_task(self.read(response, operation, self.generation))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def read(self, response, operation, generation):
        try:
            try:
                payload = await response.json()
            except (ValueError, PlaywrightError):
                payload = None
            query = parse_qs(urlsplit(response.url).query)
            try:
                if "variables" in query:
                    variables = json.loads(query["variables"][0])
                else:
                    request_body = response.request.post_data_json or {}
                    variables = request_body.get("variables", {})
            except (ValueError, PlaywrightError, AttributeError):
                variables = {}
            if not isinstance(variables, dict):
                variables = {}
            if operation in AUTH_OPERATIONS:
                try:
                    validate_payload(payload, response.status)
                    data = payload["data"]
                    if data.get("home") or data.get("viewer"):
                        self.authenticated, self.auth_error = True, None
                except LabError as exc:
                    self.auth_error = exc
            if operation == "UserByScreenName" and isinstance(payload, dict):
                result = ((payload.get("data") or {}).get("user") or {}).get("result")
                user = user_record(result)
                if user:
                    self.profiles[user["creatorId"].lower()] = user
            if operation in self.operations and generation == self.generation:
                await self.queue.put({"operation": operation, "status": response.status,
                                      "variables": variables, "payload": payload})
        except asyncio.CancelledError:
            raise
        except Exception:
            # Do not let an unhandled event task silently masquerade as a completed page.
            if operation in self.operations and generation == self.generation:
                await self.queue.put({"operation": operation, "status": response.status,
                                      "variables": {}, "payload": None})


class XExperiment(Experiment):
    platform = "x"
    platform_label = "X"
    home_url = "https://x.com/home"
    login_url = "https://x.com/i/flow/login"
    default_creator = ""
    author_label = "X 关注作者"
    parse_creator = staticmethod(username)
    parse_post = staticmethod(status_id)

    def __init__(self, args):
        super().__init__(args)
        self.report["transport"] = "native_page"
        self.captures = {}
        self.account = None

    async def prepare_context(self, context):
        if context not in self.captures:
            self.captures[context] = NativeResponses(context)
            context.on("close", lambda *_: self.captures.pop(context, None))

    def capture(self, page):
        capture = self.captures.get(page.context)
        if capture is None:
            raise LabError("X 浏览器上下文已关闭或尚未准备好。")
        return capture

    def login_instructions(self):
        print("请使用 X 的手机号/邮箱/用户名和密码登录，完成可能的验证码/二次验证后进入首页。")
        print("Google 第三方登录可能拒绝受控浏览器；若出现“不安全”提示，请关闭 Google 弹窗，改用 X 账号登录。")
        print("程序会确认登录、保存 X 专用凭证并关闭窗口；不会记录你输入的密码。")
        print("若窗口没有自动关闭，请打开 https://x.com/home；无法联网时可用 --proxy 配置代理。")

    async def viewer(self, page):
        parsed = urlsplit(page.url)
        if parsed.hostname == "accounts.google.com" and "rejected" in parsed.path:
            if not self.report.get("google_sso_rejected"):
                self.report["google_sso_rejected"] = True
                print("[Google 登录被拒绝] 请关闭 Google 弹窗，回到 X，用 X 账号和密码登录；程序仍在等待。")
            return {"known": False, "logged_in": False}
        if parsed.hostname not in X_HOSTS:
            return {"known": False, "logged_in": False}
        login_page = parsed.path.startswith(("/i/flow/login", "/i/jf/onboarding/", "/login", "/account/access"))
        if login_page:
            limited = page.get_by_text(re.compile(
                r"我们已临时限制你的登录|我們已暫時限制你的登入|temporarily limited your login|temporarily restricted your login|too many (?:login )?attempts",
                re.IGNORECASE,
            )).first
            if await limited.is_visible():
                self.report["login_blocked"] = "temporary_login_limit"
                raise LabError("X 提示已临时限制登录，本次实验已停止。请暂停重试，"
                               "稍后先在日常浏览器确认账号可正常登录；不自动反复提交。", 5)
        capture = self.capture(page)
        profile = page.locator('[data-testid="AppTabBar_Profile_Link"]').first
        account_button = page.locator('[data-testid="SideNav_AccountSwitcher_Button"]').first
        if await profile.is_visible() and await account_button.is_visible() and capture.authenticated:
            href = await profile.get_attribute("href")
            handle = username("https://x.com" + href if href and href.startswith("/") else href or "")
            if handle:
                profile_data = capture.profiles.get(handle.lower()) or {}
                return {"known": True, "logged_in": True, "username": handle,
                        "user_id": profile_data.get("userId")}
        login_form = page.locator('input[autocomplete="username"], input[name="password"], [data-testid="LoginForm_Login_Button"]').first
        if login_page or await login_form.is_visible():
            return {"known": True, "logged_in": False}
        if capture.auth_error and capture.auth_error.code == 3:
            return {"known": True, "logged_in": False}
        return {"known": False, "logged_in": False}

    async def verify(self, page):
        self.report["login_verified"] = False
        capture = self.capture(page)
        capture.reset_auth()
        await self.goto(page, self.home_url)
        deadline = asyncio.get_running_loop().time() + self.args.timeout
        while asyncio.get_running_loop().time() < deadline:
            viewer = await self.viewer(page)
            if viewer["logged_in"]:
                self.account = viewer
                self.report["login_verified"] = True
                print("[登录] X 的账号导航与本次服务端首页响应均确认已登录")
                return viewer
            if viewer["known"]:
                raise LabError("X 确认当前未登录或登录凭证已过期。", 3)
            if capture.auth_error:
                raise capture.auth_error
            await asyncio.sleep(0.25)
        raise LabError("无法确认 X 登录状态：页面可能尚未加载、正在验证或结构已变化。"
                       "可使用 --show-browser 查看，或用 --proxy 设置浏览器代理。")

    async def navigate_creator(self, page):
        # Each operation begins observing before it navigates, so no first page is missed.
        pass

    async def next_response(self, page, *, scroll=False, deadline=None):
        capture = self.capture(page)
        if deadline is None:
            deadline = asyncio.get_running_loop().time() + self.args.timeout
        while asyncio.get_running_loop().time() < deadline:
            try:
                return await asyncio.wait_for(capture.queue.get(), timeout=min(1, max(0.01, deadline - asyncio.get_running_loop().time())))
            except asyncio.TimeoutError:
                if page.is_closed():
                    raise LabError("X 浏览器页面已关闭。")
                if scroll:
                    await page.mouse.wheel(0, 2400)
        raise LabError("X 页面没有继续返回预期数据，尚不能确认列表已读完。"
                       "已保存收到的页面；请检查网络、限流或页面提示后重试。")

    def record_response(self, event):
        self.report["requests"].append({"operation": event["operation"], "http_status": event["status"],
                                        "transport": "native_page", "has_cursor": bool(event["variables"].get("cursor"))})

    async def pages(self, page, *, url, operations, kind, folder, handle):
        capture = self.capture(page)
        capture.select(operations)
        await self.goto(page, url)
        expected_cursor, selected_operation, count = None, None, 0
        seen_cursors = set()
        while True:
            # X itself constructs the next query, transaction headers, and feature flags.
            # We only scroll; no endpoint IDs or web-client bearer token are copied.
            deadline = asyncio.get_running_loop().time() + self.args.timeout
            while True:
                event = await self.next_response(page, scroll=count > 0, deadline=deadline)
                variables = event["variables"]
                if variables.get("cursor") != expected_cursor:
                    continue
                if selected_operation and event["operation"] != selected_operation:
                    continue
                profile = capture.profiles.get(handle.lower())
                if profile and variables.get("userId") and str(variables["userId"]) != profile["userId"]:
                    continue
                break
            count += 1
            self.record_response(event)
            write_json(self.output / folder / f"{count:04d}.json", event["payload"])
            validate_payload(event["payload"], event["status"])
            parsed = timeline(event["payload"], kind)
            selected_operation = event["operation"]
            yield parsed.items, count
            if parsed.ended or (not parsed.items and parsed.cursor == expected_cursor):
                return
            if parsed.cursor in seen_cursors:
                raise LabError("X 返回重复的分页游标，尚未确认列表完整，已停止。")
            seen_cursors.add(parsed.cursor)
            expected_cursor = parsed.cursor
            if self.args.max_pages and count >= self.args.max_pages:
                raise LabError("已达到 --max-pages 上限，列表尚未读完；已保存当前结果。")
            await page.mouse.wheel(0, 2400)

    async def authors(self, page):
        if not self.account:
            raise LabError("尚未确认 X 当前登录账号，无法读取其关注列表。", 3)
        self.report["author_list_complete"] = False
        authors, unavailable = {}, 0
        async for items, count in self.pages(
            page, url=f"https://x.com/{self.account['username']}/following",
            operations={"Following"}, kind="users", folder="following-pages", handle=self.account["username"],
        ):
            for item in items:
                author = user_record(item)
                if author:
                    authors.setdefault(author["userId"], author)
                else:
                    unavailable += 1
            write_json(self.output / "following.json", list(authors.values()))
            self.report.update({"following_page_count": count, "following_count": len(authors)})
            print(f"[X 关注列表] 已读取 {count} 页，累计 {len(authors)} 位作者")
        results = list(authors.values())
        write_json(self.output / "authors.json", results)
        self.report.update({"author_list_complete": True, "author_count": len(results), "unavailable_users": unavailable})
        for index, author in enumerate(results, 1):
            protected = " [受保护]" if author["protected"] else ""
            print(f"{index:>3}. {author['user']['name']} (@{author['creatorId']}){protected}")
        if not results:
            print("[X 关注列表] 未返回可选择的关注作者。")
        if unavailable:
            print(f"[X 关注列表] 另有 {unavailable} 个不可用账号条目，原始响应已保存。")
        return results

    async def posts(self, page):
        self.report.update({"all_posts_loaded": False, "post_list_scope": "X 网页当前可返回的作者时间线（含回复/转帖）"})
        posts, tab_paths, completed_pages = {}, set(), 0
        self.report["post_timeline_pages"] = {}
        timelines = (
            ("posts", f"/{self.creator}", {"UserOriginalsTimeline", "UserTweets"}),
            ("replies", f"/{self.creator}/with_replies", {"UserRepliesTimeline", "UserTweetsAndReplies"}),
            ("reposts", f"/{self.creator}/reposts", {"UserRepostsTimeline"}),
        )
        for tab, path, operations in timelines:
            if tab != "posts" and path not in tab_paths:
                continue
            count = 0
            async for items, count in self.pages(
                page, url="https://x.com" + path, operations=operations,
                kind="tweets", folder=f"list-pages/{tab}", handle=self.creator,
            ):
                if tab == "posts" and count == 1:
                    hrefs = await page.locator('[role="tab"][href]').evaluate_all("els => els.map(e => e.getAttribute('href'))")
                    tab_paths = {urlsplit(href).path for href in hrefs if href}
                for tweet in items:
                    author = tweet_author(tweet)
                    # Reply modules may include other users' posts for context.
                    if not author or author["creatorId"].lower() != self.creator.lower():
                        continue
                    original = original_tweet(tweet)
                    if not original.get("rest_id"):
                        continue
                    post = normalize_tweet(original)
                    reposted = original is not tweet
                    posts.setdefault(post["id"], {"id": post["id"], "creatorId": post["creator_id"],
                                                  "title": ("[转帖] " if reposted else "") + post["title"],
                                                  "publishedDatetime": post["published_at"],
                                                  "isRestricted": post["access"] != "available", "isRepost": reposted})
                write_json(self.output / "list.raw.json", {"posts": list(posts.values())})
                self.report["post_timeline_pages"][tab] = count
                self.report.update({"post_page_count": completed_pages + count, "post_count": len(posts)})
                print(f"[X 帖子/{tab}] 已读取 {count} 页，合计 {len(posts)} 篇")
            completed_pages += count
        self.report.update({"all_posts_loaded": True, "timeline_exhausted": True})
        results = sorted(posts.values(), key=lambda post: post["publishedDatetime"] or "", reverse=True)
        write_json(self.output / "list.raw.json", {"posts": results})
        for index, post in enumerate(results, 1):
            print(f"{index:>3}. {post['id']} | {str(post['publishedDatetime'] or '')[:10]} | {post['title']}")
        print("[X 帖子] 已读到网页返回的时间线末尾；这不保证包含账号全部历史或已删除的帖子。")
        return results

    async def fetch_post(self, page, target):
        identifier = status_id(target)
        capture = self.capture(page)
        capture.select({"TweetDetail", "TweetResultByRestId"})
        await self.goto(page, f"https://x.com/i/status/{identifier}")
        deadline = asyncio.get_running_loop().time() + self.args.timeout
        while True:
            event = await self.next_response(page, deadline=deadline)
            variables = event["variables"]
            requested = variables.get("focalTweetId") or variables.get("tweetId") or variables.get("rest_id")
            if requested is not None and str(requested) != identifier:
                continue
            self.record_response(event)
            write_json(self.output / "post.raw.json", event["payload"])
            validate_payload(event["payload"], event["status"])
            candidates = detail_tweets(event["payload"])
            tweet = next((item for item in candidates if str(item.get("rest_id")) == identifier), None)
            if tweet is None:
                raise LabError("X 没有返回目标帖子的完整内容，可能已删除、受保护或当前账号无权访问。", 2)
            normalized = normalize_tweet(tweet)
            if normalized["id"] != identifier:
                self.report["requested_post_id"] = identifier
            await self.archive_post(page, normalized, normalized["url"])
            return
