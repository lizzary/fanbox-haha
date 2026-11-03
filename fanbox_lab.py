"""Small, visible-browser experiment: manual login -> saved state -> Fanbox posts."""

from __future__ import annotations

import argparse
import asyncio
import html
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlencode, urlsplit

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import async_playwright

from fanbox_media import DownloadError, asset_filename, download_asset

ROOT = Path(__file__).resolve().parent
HOME = "https://www.fanbox.cc/"
API = "https://api.fanbox.cc/"
DEFAULT_CREATOR = "szcb911"

# Read only the current viewer, never the creator's profile or the CSRF token.
VIEWER_JS = """() => {
    const node = document.querySelector('meta[name="metadata"]');
    if (!node) return {known: false, logged_in: false};
    try {
        const meta = JSON.parse(node.content);
        const user = meta.context?.user;
        const loggedIn = meta.urlContext?.user?.isLoggedIn;
        return {
            known: typeof loggedIn === 'boolean',
            logged_in: loggedIn === true && !!user?.userId,
            user_id: user?.userId || null,
            name: user?.name || null
        };
    } catch { return {known: false, logged_in: false}; }
}"""

FETCH_JS = """async ({url, timeout}) => {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeout);
    try {
        const response = await fetch(url, {
            credentials: 'include', signal: controller.signal
        });
        const text = await response.text();
        let payload = null;
        try { payload = JSON.parse(text); } catch {}
        return {status: response.status,
                content_type: response.headers.get('content-type'), payload};
    } finally { clearTimeout(timer); }
}"""


class LabError(Exception):
    def __init__(self, message: str, code: int = 1):
        super().__init__(message)
        self.code = code


def fanbox_url(value: str):
    parsed = urlsplit(value)
    host = parsed.hostname or ""
    if (parsed.scheme != "https" or parsed.username or parsed.password
            or parsed.port not in (None, 443)
            or not (host == "fanbox.cc" or host.endswith(".fanbox.cc"))):
        raise LabError("请输入 https://...fanbox.cc/... 格式的官方链接。")
    return parsed


def creator_id(value: str) -> str:
    value = value.strip()
    if re.fullmatch(r"[A-Za-z0-9_-]+", value):
        return value
    parsed = fanbox_url(value)
    match = re.match(r"^/@([A-Za-z0-9_-]+)(?:/|$)", unquote(parsed.path))
    if parsed.hostname in ("www.fanbox.cc", "fanbox.cc") and match:
        return match[1]
    match = re.fullmatch(r"([A-Za-z0-9_-]+)\.fanbox\.cc", parsed.hostname or "")
    if match and match[1] not in ("www", "api", "downloads"):
        return match[1]
    raise LabError("无法识别作者；可输入 szcb911 或 https://www.fanbox.cc/@szcb911。")


def post_id(value: str) -> str:
    value = value.strip()
    if re.fullmatch(r"[0-9]+", value):
        return value
    parsed = fanbox_url(value)
    match = re.fullmatch(r"/(?:@[A-Za-z0-9_-]+/)?posts/([0-9]+)/?", unquote(parsed.path))
    if not match:
        raise LabError("请输入数字帖子 ID 或含 /posts/数字 的 Fanbox 帖子链接。")
    return match[1]


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


async def close_browser_resource(resource):
    """A user-closing-a-window error must not replace the actual experiment error."""
    original_error = sys.exc_info()[0] is not None
    try:
        await resource.close()
    except PlaywrightError as exc:
        already_closed = "has been closed" in str(exc) or "Target closed" in str(exc)
        if not original_error and not already_closed:
            raise


def list_items(payload: dict) -> list[dict]:
    body = payload.get("body")
    # Current live API uses `posts`; older responses used `items`.
    items = body.get("posts", body.get("items")) if isinstance(body, dict) else body
    if not isinstance(items, list) or not all(isinstance(p, dict) and p.get("id") for p in items):
        raise LabError("帖子列表结构发生变化：预期 body.posts 数组，请查看 list.raw.json。")
    return items


def api_url(value: str, endpoint: str | None = None) -> str:
    parsed = urlsplit(value)
    allowed = {"creator.listFollowing", "plan.listSupporting", "post.paginateCreator", "post.listCreator", "post.info"}
    if (parsed.scheme != "https" or parsed.hostname != "api.fanbox.cc"
            or parsed.port not in (None, 443) or parsed.username or parsed.password
            or parsed.path.lstrip("/") not in allowed
            or (endpoint and parsed.path != "/" + endpoint)):
        raise LabError("分页地址不是预期的 Fanbox 只读接口，已停止。")
    return value


def normalize_post(post: dict) -> dict:
    content = post.get("body")
    restricted = post.get("isRestricted") is True
    access = "restricted" if restricted else "available" if isinstance(content, dict) else "unavailable"
    content = content if isinstance(content, dict) and not restricted else {}
    paragraphs = []
    if isinstance(content.get("text"), str):
        paragraphs.append(content["text"])
    for block in content.get("blocks", []):
        if isinstance(block.get("text"), str):
            paragraphs.append(block["text"])

    assets, seen = [], set()
    for kind, fields in (("image", ("images", "imageMap")), ("file", ("files", "fileMap"))):
        for field in fields:
            entries = content.get(field) or []
            for item in entries.values() if isinstance(entries, dict) else entries:
                url = item.get("originalUrl") or item.get("url")
                if url and url not in seen:
                    seen.add(url)
                    assets.append({"kind": kind, "id": item.get("id"), "name": item.get("name"),
                                   "extension": item.get("extension"), "url": url,
                                   "width": item.get("width"), "height": item.get("height"),
                                   "size": item.get("size")})
    cover = post.get("coverImageUrl")
    if access == "available" and cover and cover not in seen:
        assets.append({"kind": "cover", "id": "cover", "url": cover})
    embeds = list((content.get("embedMap") or {}).values())
    embeds.extend((content.get("urlEmbedMap") or {}).values())
    if content.get("video"):
        embeds.append(content["video"])
    # Unknown legacy HTML is preserved but cannot be declared fully archived.
    if content.get("html"):
        embeds.append({"type": "legacy_html", "note": "原始 HTML 保留在 content.html 字段，需要单独核对资源。"})
    return {
        "id": str(post["id"]), "creator_id": post.get("creatorId"), "title": post.get("title"),
        "type": post.get("type"), "access": access, "fee_required": post.get("feeRequired"),
        "published_at": post.get("publishedDatetime"), "updated_at": post.get("updatedDatetime"),
        "text": "\n\n".join(paragraphs), "assets": assets, "external_embeds": embeds,
        # Preserve ordering, links, video providers, and unknown block types without guessing.
        "content": content,
    }


def write_preview(output: Path, post: dict) -> None:
    """A local, script-free reader; it never loads remote images or embedded scripts."""
    escape = lambda value: html.escape(str(value or ""), quote=True)
    parts, rendered = [], set()

    def render_asset(asset):
        key = asset["url"]
        if key in rendered:
            return
        rendered.add(key)
        label = escape(asset.get("name") or asset.get("id") or asset["kind"])
        local = asset.get("local_path")
        if asset.get("status") != "downloaded" or not local:
            parts.append(f"<p>资源未下载：{label}（{escape(asset.get('error'))}）</p>")
        elif asset["kind"] in ("image", "cover"):
            parts.append(f'<figure><img src="{quote(local)}" alt="{label}"></figure>')
        elif asset["kind"] == "video":
            parts.append(f'<figure><video controls preload="metadata" src="{quote(local)}"></video></figure>')
        else:
            parts.append(f'<p><a href="{quote(local)}">下载附件：{label}</a></p>')

    for asset in post["assets"]:
        if asset["kind"] == "cover":
            render_asset(asset)
    content = post["content"]
    if content.get("text"):
        parts.append("<p>" + escape(content["text"]).replace("\n", "<br>") + "</p>")
    for block in content.get("blocks", []):
        if "text" in block:
            parts.append("<p>" + escape(block["text"]).replace("\n", "<br>") + "</p>")
        for field, kind in (("imageId", "image"), ("fileId", "file")):
            for asset in post["assets"]:
                if field in block and asset["kind"] == kind and str(asset.get("id")) == str(block[field]):
                    render_asset(asset)
        for link in block.get("links", []):
            url = link.get("url", "")
            if urlsplit(url).scheme in ("https", "http"):
                parts.append(f'<p><a href="{escape(url)}">{escape(url)}</a></p>')
    for asset in post["assets"]:
        render_asset(asset)
    if post["external_embeds"]:
        parts.append("<h2>嵌入内容（尚未离线下载）</h2><pre>" + escape(
            json.dumps(post["external_embeds"], ensure_ascii=False, indent=2)) + "</pre>")
    document = ("<!doctype html><html lang=\"zh-CN\"><meta charset=\"utf-8\">"
                '<meta name="viewport" content="width=device-width, initial-scale=1">'
                f"<title>{escape(post['title'])}</title><style>"
                "body{max-width:860px;margin:40px auto;padding:0 24px;font:17px/1.8 system-ui;color:#222}"
                "img,video{max-width:100%;height:auto}figure{margin:24px 0}pre{white-space:pre-wrap;overflow-wrap:anywhere}"
                "p{overflow-wrap:anywhere}a{color:#1766a5}</style>"
                f"<h1>{escape(post['title'])}</h1><p>{escape(post['creator_id'])} · {escape(post['published_at'])}</p>"
                + "\n".join(parts) + "</html>")
    (output / "index.html").write_text(document, encoding="utf-8")


class Experiment:
    platform = "fanbox"
    platform_label = "Fanbox"
    home_url = HOME
    login_url = HOME
    default_creator = DEFAULT_CREATOR
    author_label = "作者（关注 + 赞助）"
    parse_creator = staticmethod(creator_id)
    parse_post = staticmethod(post_id)

    def __init__(self, args):
        self.args = args
        self.creator = self.parse_creator(args.creator or self.default_creator)
        self.data = args.data_dir.resolve()
        if self.platform != "fanbox":
            self.data = self.data / self.platform
        self.auth = self.data / "auth" / "storage-state.json"
        self.browser_file = self.data / "auth" / "browser.json"
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        self.output = self.data / "runs" / stamp
        self.report = {
            "started_at": datetime.now(timezone.utc).isoformat(), "command": args.command, "platform": self.platform,
            "creator_id": self.creator, "transport": args.transport,
            "anonymous": args.anonymous, "login_verified": False,
            "storage_state_reused": False, "login_opened": False,
            "requests": [], "result": "running",
        }
        self.channel = None

    def channels(self):
        if self.args.browser != "auto":
            return [self.args.browser]
        if self.browser_file.exists() and not self.args.anonymous:
            try:
                saved = json.loads(self.browser_file.read_text(encoding="utf-8")).get("channel")
            except (ValueError, AttributeError):
                saved = None
            if saved in ("chrome", "msedge", "chromium"):
                return [saved]
        return ["chrome", "msedge", "chromium"]

    async def launch(self, playwright, *, persistent=False):
        failures = []
        for channel in self.channels():
            options = {"headless": False if persistent else self.args.headless, "chromium_sandbox": True}
            if self.args.proxy:
                options["proxy"] = {"server": self.args.proxy}
            if channel != "chromium":
                options["channel"] = channel
            try:
                if persistent:
                    result = await playwright.chromium.launch_persistent_context(
                        self.data / "profiles" / channel, viewport={"width": 1280, "height": 900},
                        **options,
                    )
                else:
                    result = await playwright.chromium.launch(**options)
                self.channel = channel
                self.report["browser"] = channel
                print(f"[浏览器] {channel}，{'后台' if options['headless'] else '可见'}模式")
                return result
            except PlaywrightError as exc:
                failures.append(f"{channel}: {str(exc).splitlines()[0]}")
        raise LabError("浏览器启动失败。请关闭占用实验 profile 的实例，或安装 Chromium：\n"
                       "  python -m playwright install chromium\n" + "\n".join(failures))

    async def goto(self, page, url):
        try:
            response = await page.goto(url, wait_until="domcontentloaded", timeout=self.args.timeout * 1000)
        except PlaywrightError as exc:
            raise LabError("页面导航失败：请检查网络/代理，或在可见浏览器中确认页面能否打开。\n"
                           + str(exc).splitlines()[0]) from exc
        if response is not None and response.status >= 400:
            raise LabError(f"页面返回 HTTP {response.status}；请先在浏览器中检查页面。")

    async def viewer(self, page):
        if (urlsplit(page.url).hostname or "") not in ("fanbox.cc", "www.fanbox.cc"):
            return {"known": False, "logged_in": False}
        return await page.evaluate(VIEWER_JS)

    async def verify(self, page):
        # A fresh server-rendered page ensures we are not reading stale pre-login metadata.
        await self.goto(page, HOME)
        viewer = await self.viewer(page)
        if not viewer.get("known"):
            raise LabError("无法识别 Fanbox 登录状态：可能是验证页，或页面 metadata 结构已变化。")
        if not viewer["logged_in"]:
            raise LabError("Fanbox 确认当前未登录或登录已过期。请运行 login 重新登录。", 3)
        self.report["login_verified"] = True
        print("[登录] Fanbox 服务端页面确认已登录")
        return viewer

    async def save_state(self, context):
        # These files contain credentials. The entire data directory is gitignored.
        state = await context.storage_state(indexed_db=True)
        write_json(self.auth, state)
        write_json(self.browser_file, {"channel": self.channel})

    async def prepare_context(self, context):
        """Platform hook for observing native page requests before the first navigation."""

    async def navigate_creator(self, page):
        await self.goto(page, f"{HOME}@{self.creator}")

    def login_instructions(self):
        print("请在弹出的浏览器中点击登录，完成 Pixiv 登录和可能的验证。")
        print("回到 Fanbox 后会自动确认、保存登录状态并关闭窗口。Ctrl+C 可取消。")
        print("如果已登录但窗口未关闭，请在 Fanbox 页面按 Ctrl+R 刷新一次。")

    async def login(self, playwright):
        if self.args.login_mode == "manual":
            from manual_browser import login_with_browser
            return await login_with_browser(self, playwright)
        return await self.playwright_login(playwright)

    async def playwright_login(self, playwright):
        self.report.update({"login_opened": True, "login_mode": "playwright"})
        context = await self.launch(playwright, persistent=True)
        try:
            await self.prepare_context(context)
            page = context.pages[0] if context.pages else await context.new_page()
            # Keep an HTTP error/challenge visible so the user can handle it manually.
            try:
                await self.goto(page, self.login_url)
            except LabError as exc:
                print(f"[等待手动操作] {exc}")
            self.login_instructions()
            deadline = asyncio.get_running_loop().time() + self.args.login_timeout
            while asyncio.get_running_loop().time() < deadline:
                if not context.pages:
                    raise LabError("登录窗口已关闭，尚未确认登录成功。", 3)
                for candidate in list(context.pages):
                    try:
                        viewer = await self.viewer(candidate)
                    except PlaywrightError:
                        continue  # A login redirect may replace the document mid-read.
                    if viewer.get("logged_in"):
                        probe = await context.new_page()
                        await self.verify(probe)
                        await self.save_state(context)
                        self.report["login_captured"] = True
                        print(f"[保存] {self.auth}")
                        return
                await asyncio.sleep(1)
            raise LabError("等待登录超时；请重新运行 login。", 3)
        finally:
            await close_browser_resource(context)

    async def open_session(self, playwright):
        state = None
        if not self.args.anonymous:
            if not self.auth.exists():
                self.report["credential_check"] = "missing"
                raise LabError("尚未保存登录凭证。", 3)
            try:
                state = json.loads(self.auth.read_text(encoding="utf-8"))
                if not isinstance(state, dict) or not isinstance(state.get("cookies"), list) or not isinstance(state.get("origins", []), list):
                    raise ValueError("invalid storage state")
            except (ValueError, UnicodeError):
                self.report["credential_check"] = "invalid_file"
                raise LabError("保存的登录状态文件无效，需要重新登录。", 3) from None
        browser = await self.launch(playwright)
        try:
            try:
                context = await browser.new_context(storage_state=state, viewport={"width": 1280, "height": 900})
            except PlaywrightError:
                if state is not None:
                    self.report["credential_check"] = "invalid_file"
                    raise LabError("浏览器无法载入保存的登录状态，需要重新登录。", 3) from None
                raise
            await self.prepare_context(context)
            page = await context.new_page()
            if self.args.anonymous:
                print("[匿名实验] 不载入凭证。")
                await self.goto(page, self.home_url)
            else:
                try:
                    await self.verify(page)
                except LabError as exc:
                    self.report["credential_check"] = "expired" if exc.code == 3 else "check_failed"
                    raise
                self.report["credential_check"] = "valid"
                self.report["storage_state_reused"] = True
                await self.save_state(context)
            return browser, context, page
        except BaseException:
            await close_browser_resource(browser)
            raise

    async def ensure_session(self, playwright):
        print("[凭证] 正在后台检查已有登录状态…")
        try:
            return await self.open_session(playwright)
        except LabError as exc:
            # A connection failure is not evidence that the session expired.
            if exc.code != 3 or self.args.command == "check" or self.args.anonymous:
                raise
            print(f"[凭证] {exc} 正在打开登录窗口。")
        await self.login(playwright)
        self.report["login_verified"] = False
        print("[登录窗口已关闭] 正在后台复用刚保存的凭证…")
        return await self.open_session(playwright)

    async def api(self, page, endpoint: str, **params):
        if endpoint.startswith("https://"):
            url = api_url(endpoint)
            parsed = urlsplit(url)
            endpoint, params = parsed.path.lstrip("/"), parse_qs(parsed.query)
        else:
            url = api_url(API + endpoint + ("?" + urlencode(params) if params else ""))
        event = {"endpoint": endpoint, "params": params, "transport": self.args.transport}
        self.report["requests"].append(event)

        def observe(response):
            if response.url == url:
                event["http_status"] = response.status

        page.on("response", observe)
        try:
            if self.args.transport == "browser":
                result = await page.evaluate(FETCH_JS, {"url": url, "timeout": self.args.timeout * 1000})
            else:
                # Deliberate comparison: same cookie jar, but outside the browser network stack.
                response = await page.context.request.get(
                    url, headers={"Origin": HOME.rstrip("/"), "Referer": page.url,
                                  "User-Agent": await page.evaluate("navigator.userAgent")},
                    timeout=self.args.timeout * 1000, max_redirects=0,
                )
                try:
                    try:
                        payload = await response.json()
                    except (ValueError, PlaywrightError):
                        payload = None
                    result = {"status": response.status, "content_type": response.headers.get("content-type"),
                              "payload": payload}
                finally:
                    await response.dispose()
        except PlaywrightError as exc:
            event["error"] = str(exc).splitlines()[0]
            status = event.get("http_status", "未知")
            raise LabError(f"{endpoint} 请求失败（HTTP {status}）：可能是网络/CORS/验证或限流。\n"
                           "请在可见浏览器中确认访问情况后重试；诊断见 report.json。") from exc
        finally:
            page.remove_listener("response", observe)

        event.update({"http_status": result["status"], "content_type": result["content_type"]})
        status, payload = result["status"], result["payload"]
        print(f"[API/{self.args.transport}] {endpoint} -> HTTP {status}")
        if status != 200:
            hint = {401: "请重新登录。", 403: "可能是访问限制或站点验证，不能据此断言登录过期。",
                    404: "作者/帖子可能不存在或已经删除。", 429: "请求被限流，请稍后再试。"}.get(status, "请查看诊断报告。")
            raise LabError(f"{endpoint} 返回 HTTP {status}。{hint}")
        if not isinstance(payload, dict) or "body" not in payload:
            raise LabError(f"{endpoint} 未返回预期 JSON；可能是验证页面或接口结构变化。")
        return payload

    async def authors(self, page):
        merged = {}
        self.report["author_list_complete"] = False
        for source, endpoint, field, label in (
            ("following", "creator.listFollowing", "creators", "关注"),
            ("supporting", "plan.listSupporting", "plans", "赞助"),
        ):
            source_creators, seen_urls = {}, set()
            url = API + endpoint
            while url:
                api_url(url, endpoint)
                if url in seen_urls:
                    raise LabError(f"{label}列表分页重复，已停止，避免误报列表完整。")
                seen_urls.add(url)
                payload = await self.api(page, url)
                write_json(self.output / f"{source}-pages" / f"{len(seen_urls):04d}.json", payload)
                body = payload["body"]
                items = body.get(field, body.get("items")) if isinstance(body, dict) else body
                if not isinstance(items, list):
                    raise LabError(f"{label}列表结构发生变化：预期 body.{field}。")
                for item in items:
                    if not isinstance(item, dict) or not item.get("creatorId"):
                        raise LabError(f"{label}列表缺少 creatorId，已保存响应供检查。")
                    identifier = creator_id(item["creatorId"])
                    # A supporting item is a plan, not a creator profile. Do not mix
                    # its title/cover/description into the author's profile fields.
                    creator = dict(item) if source == "following" else {
                        "creatorId": identifier, "user": item.get("user"), "isSupported": True,
                    }
                    source_creators.setdefault(identifier, creator)
                    if identifier not in merged:
                        merged[identifier] = {**creator, "sources": []}
                    author = merged[identifier]
                    if source not in author["sources"]:
                        author["sources"].append(source)
                    author["isFollowed" if source == "following" else "isSupported"] = True
                    if not author.get("user") and creator.get("user"):
                        author["user"] = creator["user"]
                url = body.get("nextUrl") if isinstance(body, dict) else None
                if url:
                    await asyncio.sleep(0.25)
            write_json(self.output / f"{source}.json", list(source_creators.values()))
            self.report[f"{source}_count"] = len(source_creators)
        creators = list(merged.values())
        write_json(self.output / "authors.json", creators)
        self.report.update({"author_count": len(creators), "author_list_complete": True})
        print(f"[作者] 关注 {self.report['following_count']} 位，赞助 {self.report['supporting_count']} 位，去重后 {len(creators)} 位")
        for index, creator in enumerate(creators, 1):
            name = (creator.get("user") or {}).get("name", creator["creatorId"])
            labels = " + ".join({"following": "关注", "supporting": "赞助"}[source] for source in creator["sources"])
            print(f"{index:>3}. [{labels}] {name} (@{creator['creatorId']})")
        if not creators:
            print("[作者列表] 当前账号没有关注或正在赞助的作者。")
        return creators

    async def posts(self, page):
        index_payload = await self.api(page, "post.paginateCreator", creatorId=self.creator)
        write_json(self.output / "post-pages.json", index_payload)
        body = index_payload["body"]
        urls = body.get("pageUrls") if isinstance(body, dict) else body
        if not isinstance(urls, list) or not all(isinstance(url, str) for url in urls):
            raise LabError("帖子分页结构发生变化：预期 body.pageUrls。")
        if len(set(urls)) != len(urls):
            raise LabError("帖子分页地址重复，已停止，避免误报所有帖子已读完。")
        items, seen = [], set()
        self.report["all_posts_loaded"] = False
        for index, url in enumerate(urls, 1):
            api_url(url, "post.listCreator")
            if parse_qs(urlsplit(url).query).get("creatorId") != [self.creator]:
                raise LabError("分页地址的作者与所选作者不符，已停止。")
            if index > 1:
                await asyncio.sleep(0.25)
            payload = await self.api(page, url)
            write_json(self.output / "list-pages" / f"{index:04d}.json", payload)
            for item in list_items(payload):
                identifier = str(item["id"])
                if identifier not in seen:
                    items.append(item)
                    seen.add(identifier)
            print(f"[全部帖子] 已读取 {index}/{len(urls)} 页，累计 {len(items)} 篇")
        write_json(self.output / "list.raw.json", {"body": {"posts": items}})
        self.report["post_count"] = len(items)
        self.report["post_page_count"] = len(urls)
        self.report["all_posts_loaded"] = True
        for index, post in enumerate(items, 1):
            access = "受限" if post.get("isRestricted") else "列表标记可访问"
            date = str(post.get("publishedDatetime") or "")[:10]
            print(f"{index:>3}. {post['id']} | {date} | {access} | JPY {post.get('feeRequired', '?')} | {post.get('title', '')}")
        if not items:
            print("[列表] 当前响应中没有帖子。")
        return items

    async def choose(self, items, label, key):
        if not items:
            return None
        while True:
            # No network task is pending at a menu. Keep input on the main thread so
            # Ctrl+C does not leave an executor thread blocked on console input.
            value = input(f"请选择{label}序号（1–{len(items)}，q 退出）: ").strip()
            if value.lower() == "q":
                self.report["selection_cancelled"] = True
                return None
            if value.isdigit() and 1 <= int(value) <= len(items):
                return items[int(value) - 1]
            matched = next((item for item in items if str(item[key]) == value), None)
            if matched:
                return matched
            print("输入无效，请输入列表中的序号或 ID。")

    async def fetch_post(self, page, target):
        identifier = post_id(target)
        payload = await self.api(page, "post.info", postId=identifier)
        write_json(self.output / "post.raw.json", payload)
        body = payload["body"]
        # Current live API wraps the post in body.post; support the older flat body too.
        post = body.get("post", body) if isinstance(body, dict) else None
        if not isinstance(post, dict) or str(post.get("id")) != identifier:
            raise LabError("帖子响应为空或 ID 不符，请查看 post.raw.json。")
        normalized = normalize_post(post)
        referer = f"{HOME}@{creator_id(str(post.get('creatorId') or self.creator))}/posts/{identifier}"
        await self.archive_post(page, normalized, referer)

    async def archive_post(self, page, normalized, referer):
        normalized["archive_complete"] = False
        write_json(self.output / "post.json", normalized)
        write_json(self.output / "assets.json", normalized["assets"])
        (self.output / "text.txt").write_text(normalized["text"], encoding="utf-8")
        self.report.update({"post_id": normalized["id"], "post_access": normalized["access"],
                            "asset_count": len(normalized["assets"])})
        if normalized["access"] != "available":
            raise LabError("只取得帖子元数据，正文受限或不存在；这不算正文获取成功。请检查账号访问权限。", 2)
        print(f"[正文] {normalized['title']}；{len(normalized['text'])} 字，{len(normalized['assets'])} 个媒体/附件资源")
        failed = 0
        for index, asset in enumerate(normalized["assets"], 1):
            name = asset_filename(asset, index)
            destination = self.output / "media" / name
            asset["status"] = "downloading"
            write_json(self.output / "assets.json", normalized["assets"])
            print(f"[下载 {index}/{len(normalized['assets'])}] {name}")
            try:
                result = await download_asset(page.context, asset, destination, referer,
                                              self.args.timeout, self.args.download_timeout)
                asset.update(result)
                asset["local_path"] = destination.relative_to(self.output).as_posix()
                print(f"[已保存] {asset['bytes']:,} 字节")
            except (DownloadError, PlaywrightError, OSError, ValueError) as exc:
                asset.update({"status": "failed", "error": str(exc)})
                failed += 1
                print(f"[下载失败] {name}: {exc}")
            write_json(self.output / "assets.json", normalized["assets"])
            write_json(self.output / "post.json", normalized)
        normalized["archive_complete"] = failed == 0 and not normalized["external_embeds"]
        write_json(self.output / "post.json", normalized)
        write_json(self.output / "external-embeds.json", normalized["external_embeds"])
        write_preview(self.output, normalized)
        self.report.update({"archive_complete": normalized["archive_complete"], "failed_assets": failed,
                            "downloaded_assets": sum(a.get("status") == "downloaded" for a in normalized["assets"]),
                            "external_embed_count": len(normalized["external_embeds"])})
        if not normalized["archive_complete"]:
            raise LabError(f"帖子未完整下载：{failed} 个资源失败，{len(normalized['external_embeds'])} 个嵌入内容需单独处理。"
                           "已下载的内容已保留，详情见 assets.json 和 external-embeds.json。", 4)
        print(f"[整篇保存完成] 正文和全部 {len(normalized['assets'])} 个资源已保存。")
        print(f"[离线阅读] {self.output / 'index.html'}")

    async def run(self):
        async with async_playwright() as playwright:
            if self.args.command == "login":
                await self.login(playwright)
                return
            browser, context, page = await self.ensure_session(playwright)
            try:
                if self.args.command == "check":
                    return
                if self.args.command in ("lab", "authors", "following"):
                    creators = await self.authors(page)
                    if self.args.command != "lab":
                        return
                    chosen = await self.choose(creators, self.author_label, "creatorId")
                    if chosen is None:
                        return
                    self.creator = self.parse_creator(chosen["creatorId"])
                    self.report["creator_id"] = self.creator
                await self.navigate_creator(page)
                if self.args.command in ("list", "lab"):
                    items = await self.posts(page)
                    if self.args.command == "list":
                        return
                    while True:
                        chosen = await self.choose(items, "要完整下载的帖子", "id")
                        if chosen is None:
                            return
                        if chosen.get("isRestricted"):
                            print("当前账号无权读取这篇帖子的正文，请选择可访问的帖子。")
                            continue
                        await self.fetch_post(page, str(chosen["id"]))
                        break
                else:
                    await self.fetch_post(page, self.args.target)
            finally:
                try:
                    if not self.args.anonymous:
                        await self.save_state(context)
                finally:
                    try:
                        await close_browser_resource(context)
                    finally:
                        await close_browser_resource(browser)


def arguments(argv=None):
    parser = argparse.ArgumentParser(description="PostPock 实验：检查凭证 → 必要时登录 → 选作者 → 列出帖子 → 下载整篇")
    parser.add_argument("command", nargs="?", default="lab", choices=("lab", "login", "check", "authors", "following", "list", "fetch"))
    parser.add_argument("target", nargs="?", help="fetch 的帖子 ID 或官方 URL")
    parser.add_argument("--platform", choices=("fanbox", "x"), default="fanbox", help="平台，默认 fanbox")
    parser.add_argument("--login-mode", "--x-login-mode", dest="login_mode", choices=("manual", "playwright"), default="manual",
                        help="登录方式；两个平台均默认先用普通浏览器手动登录并关闭，再接管验证")
    parser.add_argument("--creator", help="作者 ID/用户名或主页 URL；Fanbox 默认 szcb911")
    parser.add_argument("--browser", choices=("auto", "chrome", "msedge", "chromium"), default="auto")
    parser.add_argument("--transport", choices=("browser", "request"), default="browser")
    parser.add_argument("--proxy", help="浏览器代理地址，例如 http://127.0.0.1:7890；不修改系统代理")
    parser.add_argument("--max-pages", type=int, default=0, help="X 每个时间线的分页上限；0 为不设上限，到上限会标记未读完")
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data")
    parser.add_argument("--timeout", type=int, default=30, help="单次请求超时秒数")
    parser.add_argument("--login-timeout", type=int, default=600, help="手动登录等待秒数")
    parser.add_argument("--download-timeout", type=int, default=600, help="每个资源下载的总超时秒数")
    visibility = parser.add_mutually_exclusive_group()
    visibility.add_argument("--headless", action="store_true", default=True, help="后台浏览器模式（默认）；登录仍显示窗口")
    visibility.add_argument("--show-browser", dest="headless", action="store_false", help="仅调试：显示验证和获取数据的浏览器")
    parser.add_argument("--anonymous", action="store_true", help="仅 list/fetch：全新匿名会话，不读写凭证")
    args = parser.parse_args(argv)
    if args.command == "fetch" and not args.target:
        parser.error("fetch 必须提供帖子 ID 或 URL")
    if args.target and args.command != "fetch":
        parser.error("只有 fetch 接受帖子 ID；作者请用 --creator")
    if args.anonymous and args.command not in ("list", "fetch"):
        parser.error("--anonymous 只适用于 list/fetch")
    if min(args.timeout, args.login_timeout, args.download_timeout) <= 0:
        parser.error("超时时间必须为正整数")
    if args.max_pages < 0:
        parser.error("--max-pages 不能为负数")
    if args.platform == "x" and (args.anonymous or args.transport != "browser"):
        parser.error("X 接入使用登录后的网页原生请求，请移除 --anonymous / --transport request")
    if args.platform == "x" and args.command == "list" and not args.creator:
        parser.error("X 的 list 命令需要 --creator 用户名或主页链接")
    if args.proxy:
        proxy = urlsplit(args.proxy)
        if proxy.scheme not in ("http", "https", "socks5") or not proxy.hostname or proxy.username or proxy.password:
            parser.error("--proxy 需要 http(s)://主机:端口 或 socks5://主机:端口，不在 URL 中嵌入账号密码")
    return args


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    args = arguments(argv)
    experiment = None
    code = 0
    try:
        if args.platform == "x":
            from x_lab import XExperiment
            experiment = XExperiment(args)
        else:
            experiment = Experiment(args)
        if args.target:
            experiment.parse_post(args.target)
        asyncio.run(experiment.run())
        experiment.report["result"] = "success"
    except KeyboardInterrupt:
        print("\n[取消] 实验已中止。", file=sys.stderr)
        code = 130
    except (LabError, PlaywrightError, OSError, ValueError, EOFError, KeyError, TypeError, AttributeError) as exc:
        print(f"[未通过] {exc}", file=sys.stderr)
        code = exc.code if isinstance(exc, LabError) else 1
        if experiment:
            experiment.report["error"] = str(exc)
    finally:
        if experiment:
            experiment.report["exit_code"] = code
            if code:
                experiment.report["result"] = {2: "metadata_only", 4: "partial_download", 5: "login_limited"}.get(code, "failed")
            write_json(experiment.output / "report.json", experiment.report)
            print(f"[结果目录] {experiment.output}")
    return code


if __name__ == "__main__":
    # Let the optional connector import the same classes when this file is the entry point.
    sys.modules.setdefault("fanbox_lab", sys.modules[__name__])
    raise SystemExit(main())
