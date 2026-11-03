"""Stream files through Chromium's network stack without buffering large ZIPs in RAM."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import re
from contextlib import suppress
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit


class DownloadError(Exception):
    pass


def validate_media_url(url: str) -> str:
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    if (parsed.scheme != "https" or parsed.username or parsed.password
            or parsed.port not in (None, 443)
            or not any(host == domain or host.endswith("." + domain)
                       for domain in ("fanbox.cc", "pximg.net", "pixiv.net", "twimg.com"))):
        raise DownloadError("资源地址不是受支持平台的 HTTPS 媒体地址，未自动下载。")
    return url


def asset_filename(asset: dict, index: int) -> str:
    fallback = unquote(urlsplit(asset["url"]).path.rsplit("/", 1)[-1])
    name = str(asset.get("name") or fallback or asset.get("id") or "resource")
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .") or "resource"
    extension = re.sub(r"[^a-zA-Z0-9]", "", str(asset.get("extension") or ""))[:12]
    if extension and not name.lower().endswith("." + extension.lower()):
        name += "." + extension
    suffix = Path(name).suffix[:16]
    stem = name[:-len(suffix)] if suffix else name
    # Prefix prevents Windows reserved names and collisions between equal original names.
    return f"{index:03d}-{stem[:80]}{suffix}"


async def download_asset(context, asset: dict, destination: Path, referer: str,
                         timeout: int = 30, download_timeout: int = 600) -> dict:
    validate_media_url(asset["url"])
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_suffix(destination.suffix + ".part")
    try:
        return await asyncio.wait_for(
            _stream_asset(context, asset, destination, partial, referer, timeout),
            timeout=download_timeout,
        )
    except asyncio.TimeoutError as exc:
        raise DownloadError(f"资源下载超时（最长 {download_timeout} 秒）。") from exc
    finally:
        # This exact path is created by this download; completed files are never removed.
        partial.unlink(missing_ok=True)


async def _stream_asset(context, asset, destination, partial, referer, timeout):
    page = await context.new_page()
    session = None
    navigation = None
    stream = None
    paused_id = None
    events = asyncio.Queue()
    try:
        session = await context.new_cdp_session(page)
        session.on("Fetch.requestPaused", events.put_nowait)
        await session.send("Fetch.enable", {
            "patterns": [{"urlPattern": "*", "requestStage": "Response"}],
        })
        navigation = asyncio.create_task(page.goto(
            # The intercepted response stays uncommitted while its body is streamed.
            # Header/read deadlines and the outer download deadline bound this work.
            asset["url"], referer=referer, wait_until="commit", timeout=0,
        ))
        for _ in range(10):
            try:
                event = await asyncio.wait_for(events.get(), timeout=timeout)
            except asyncio.TimeoutError:
                if navigation.done():
                    await navigation  # Surface a DNS/connection failure instead of hiding it.
                raise DownloadError("等待资源响应超时。")
            paused_id = event["requestId"]
            validate_media_url(event["request"]["url"])
            status = event.get("responseStatusCode", 0)
            headers = {h["name"].lower(): h["value"] for h in event.get("responseHeaders", [])}
            if 300 <= status < 400 and headers.get("location"):
                validate_media_url(urljoin(event["request"]["url"], headers["location"]))
                await session.send("Fetch.continueRequest", {"requestId": paused_id})
                paused_id = None
                continue
            if status != 200:
                raise DownloadError(f"资源返回 HTTP {status}，未保存为完整文件。")
            content_type = headers.get("content-type", "").split(";", 1)[0].lower()
            if content_type in ("text/html", "application/xhtml+xml") and "attachment" not in headers.get("content-disposition", "").lower():
                raise DownloadError("资源实际返回 HTML 页面，可能是登录/验证页面。")
            if asset["kind"] in ("image", "cover") and not content_type.startswith("image/"):
                raise DownloadError(f"图片返回了非图片类型：{content_type or '未知'}。")
            if asset["kind"] == "video" and content_type not in ("video/mp4", "application/octet-stream"):
                raise DownloadError(f"视频返回了非 MP4 类型：{content_type or '未知'}。")

            stream = (await session.send("Fetch.takeResponseBodyAsStream", {"requestId": paused_id}))["stream"]
            digest, size = hashlib.sha256(), 0
            with partial.open("wb") as output:
                while True:
                    chunk = await asyncio.wait_for(session.send("IO.read", {"handle": stream, "size": 1024 * 1024}), timeout=timeout)
                    data = base64.b64decode(chunk["data"]) if chunk.get("base64Encoded") else chunk["data"].encode("utf-8")
                    output.write(data)
                    digest.update(data)
                    size += len(data)
                    if chunk.get("eof"):
                        break
            expected = asset.get("size")
            if expected is None and not headers.get("content-encoding"):
                expected = headers.get("content-length")
            if expected is not None and int(expected) != size:
                raise DownloadError(f"资源长度不符：预期 {expected} 字节，实际 {size} 字节。")
            if asset["kind"] in ("image", "cover") and not size:
                raise DownloadError("图片响应为空。")
            partial.replace(destination)
            return {"status": "downloaded", "bytes": size, "sha256": digest.hexdigest(),
                    "content_type": content_type, "http_status": status}
        raise DownloadError("资源重定向次数过多。")
    finally:
        if session is not None:
            if stream:
                with suppress(Exception):
                    await session.send("IO.close", {"handle": stream})
            if paused_id:
                with suppress(Exception):
                    await session.send("Fetch.failRequest", {"requestId": paused_id, "errorReason": "Aborted"})
            with suppress(Exception):
                await session.detach()
        if navigation is not None:
            navigation.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await navigation
        await page.close()
