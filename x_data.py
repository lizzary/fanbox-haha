"""Parse X's read-only web responses without hard-coding GraphQL query IDs."""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

from fanbox_lab import LabError

X_HOSTS = {"x.com", "www.x.com", "twitter.com", "www.twitter.com", "mobile.twitter.com"}


def x_url(value: str):
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or parsed.hostname not in X_HOSTS
            or parsed.port not in (None, 443) or parsed.username or parsed.password):
        raise LabError("请输入 https://x.com/... 或 https://twitter.com/... 的官方链接。")
    return parsed


def username(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    if "://" in value:
        value = x_url(value).path.strip("/").split("/")[0]
    value = value.removeprefix("@")
    if not re.fullmatch(r"[A-Za-z0-9_]{1,15}", value) or value.lower() in {
        "home", "i", "explore", "settings", "messages", "notifications", "search", "login", "signup",
    }:
        raise LabError("请输入 X 用户名（@handle）或作者主页链接。")
    return value


def status_id(value: str) -> str:
    value = value.strip()
    if re.fullmatch(r"[0-9]+", value):
        return value
    parsed = x_url(value)
    match = re.fullmatch(r"/(?:[A-Za-z0-9_]+|i/web)/status/([0-9]+)(?:/(?:photo|video)/[0-9]+)?/?", parsed.path)
    if not match:
        raise LabError("请输入 X 帖子数字 ID 或 /用户名/status/数字 格式的链接。")
    return match[1]


def validate_payload(payload, status=200):
    if status in (401,):
        raise LabError("X 登录状态已失效，需要重新登录。", 3)
    if status == 429:
        raise LabError("X 请求被限流（HTTP 429），已保留已获取的数据，请稍后重试。")
    if status != 200:
        raise LabError(f"X 页面请求返回 HTTP {status}，请检查网络或账号访问状态。")
    if not isinstance(payload, dict):
        raise LabError("X 未返回 JSON 数据，可能是网络验证页面。")
    if payload.get("errors"):
        codes = {error.get("code") for error in payload["errors"] if isinstance(error, dict)}
        if codes & {32, 89, 215}:
            raise LabError("X 服务端拒绝了当前登录状态，需要重新登录。", 3)
        if 88 in codes:
            raise LabError("X 请求被限流，已保留已获取的数据，请稍后重试。")
        raise LabError("X 接口返回错误，无法确认本次数据完整；原始响应已保存在结果目录。")
    if not isinstance(payload.get("data"), dict):
        raise LabError("X 响应缺少 data，接口结构可能变化。")


def unwrap_tweet(value):
    for _ in range(4):
        if not isinstance(value, dict):
            return {}
        if isinstance(value.get("tweet"), dict):
            value = value["tweet"]
        else:
            return value
    return {}


def user_record(value):
    if not isinstance(value, dict) or value.get("__typename") in ("UserUnavailable", "UserTombstone"):
        return None
    legacy = value.get("legacy") or {}
    core = value.get("core") or legacy
    handle = core.get("screen_name") or legacy.get("screen_name")
    identifier = value.get("rest_id") or legacy.get("id_str")
    if not handle or not identifier:
        return None
    return {"creatorId": username(handle), "userId": str(identifier),
            "user": {"name": core.get("name") or legacy.get("name") or handle},
            "sources": ["following"], "isFollowed": True,
            "protected": bool((value.get("privacy") or {}).get("protected", legacy.get("protected", False)))}


def tweet_author(tweet):
    result = ((tweet.get("core") or {}).get("user_results") or {}).get("result")
    return user_record(result)


def original_tweet(tweet):
    nested = ((tweet.get("legacy") or {}).get("retweeted_status_result") or {}).get("result")
    return unwrap_tweet(nested) if nested else tweet


@dataclass
class Timeline:
    items: list[dict]
    cursor: str | None
    ended: bool


def timeline(payload: dict, kind: str) -> Timeline:
    validate_payload(payload)
    data = payload["data"]
    user = ((data.get("user") or {}).get("result") or {})
    roots = [user.get("timeline"), user.get("timeline_v2"),
             data.get("threaded_conversation_with_injections_v2")]
    instructions = None
    for root in roots:
        if isinstance(root, dict):
            root = root.get("timeline", root)
            if isinstance(root, dict) and isinstance(root.get("instructions"), list):
                instructions = root["instructions"]
                break
    if instructions is None:
        raise LabError("X 时间线结构未识别，无法将空响应当作读取完毕。")
    items, cursor, ended = [], None, False

    def entry(value):
        nonlocal cursor
        content = value.get("content") or value.get("item") or value
        if not isinstance(content, dict) or content.get("promotedMetadata"):
            return
        if content.get("cursorType") == "Bottom":
            cursor = content.get("value")
            return
        item = content.get("itemContent", content)
        if item.get("promotedMetadata"):
            return
        key = "user_results" if kind == "users" else "tweet_results"
        if isinstance(item.get(key), dict) and isinstance(item[key].get("result"), dict):
            result = item[key]["result"]
            items.append(result if kind == "users" else unwrap_tweet(result))
        for child in content.get("items", []):
            entry(child)

    for instruction in instructions:
        if instruction.get("type") == "TimelineTerminateTimeline" and instruction.get("direction") == "Bottom":
            ended = True
        for value in instruction.get("entries", []):
            entry(value)
        if isinstance(instruction.get("entry"), dict):
            entry(instruction["entry"])
        for value in instruction.get("moduleItems", []):
            entry(value)
    ended = ended or not cursor or (kind == "users" and cursor.startswith(("0|", "-1|")))
    return Timeline(items, cursor, ended)


def detail_tweets(payload: dict) -> list[dict]:
    validate_payload(payload)
    data = payload["data"]
    for key in ("tweetResult", "tweet_result"):
        result = (data.get(key) or {}).get("result")
        if isinstance(result, dict):
            return [unwrap_tweet(result)]
    return timeline(payload, "tweets").items


def original_image_url(url: str) -> str:
    parsed = urlsplit(url)
    query = parse_qs(parsed.query)
    query["name"] = ["orig"]
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query, doseq=True), ""))


def normalize_tweet(value: dict, seen=None) -> dict:
    tweet = unwrap_tweet(value)
    identifier = str(tweet.get("rest_id") or (tweet.get("legacy") or {}).get("id_str") or "")
    if not identifier:
        raise LabError("X 帖子缺少 ID，不能保存为完整帖子。")
    seen = set(seen or ())
    seen.add(identifier)
    legacy = tweet.get("legacy") or {}
    original = original_tweet(tweet)
    if original is not tweet and original.get("legacy"):
        result = normalize_tweet(original, seen)
        result["repost_id"] = identifier
        result["reposted_by"] = tweet_author(tweet)
        return result
    author = tweet_author(tweet)
    note = ((tweet.get("note_tweet") or {}).get("note_tweet_results") or {}).get("result") or {}
    text = html.unescape(note.get("text") or legacy.get("full_text") or legacy.get("text") or "")
    entities = note.get("entity_set") or legacy.get("entities") or {}
    for url in entities.get("urls", []):
        if url.get("url") and url.get("expanded_url"):
            text = text.replace(url["url"], url["expanded_url"])
    assets, missing = [], []
    media = (legacy.get("extended_entities") or {}).get("media", (legacy.get("entities") or {}).get("media", []))
    for item in media:
        kind = item.get("type")
        asset_id = str(item.get("id_str") or item.get("media_key") or len(assets) + 1)
        if kind == "photo" and item.get("media_url_https"):
            url = original_image_url(item["media_url_https"])
            extension = (parse_qs(urlsplit(url).query).get("format") or [urlsplit(url).path.rsplit(".", 1)[-1]])[0]
            assets.append({"kind": "image", "id": asset_id, "name": f"{identifier}-{asset_id}",
                           "extension": extension, "url": url})
        elif kind in ("video", "animated_gif"):
            variants = [v for v in (item.get("video_info") or {}).get("variants", [])
                        if v.get("content_type") == "video/mp4" and v.get("url")]
            if variants:
                best = max(variants, key=lambda v: v.get("bitrate") or 0)
                assets.append({"kind": "video", "id": asset_id, "name": f"{identifier}-{asset_id}",
                               "extension": "mp4", "url": best["url"], "bitrate": best.get("bitrate")})
            else:
                missing.append({"type": kind, "id": asset_id, "reason": "没有可直接下载的 MP4，可能仅提供 HLS。"})
        else:
            missing.append({"type": kind, "id": asset_id, "reason": "媒体结构未识别，保留于原始响应。"})
    if legacy.get("truncated") and not note.get("text"):
        missing.append({"type": "truncated_text", "reason": "长帖全文尚未取得。"})
    if tweet.get("article"):
        missing.append({"type": "article", "reason": "X 长文章保留原始数据，未声明文章资源完整。"})
    quote = (tweet.get("quoted_status_result") or {}).get("result")
    quoted = unwrap_tweet(quote)
    if quoted.get("legacy") and quoted.get("rest_id") not in seen and len(seen) < 4:
        other = normalize_tweet(quoted, seen)
        text += f"\n\n引用 @{other['creator_id'] or 'unknown'}：\n{other['text']}"
        urls = {asset["url"] for asset in assets}
        assets.extend(asset for asset in other["assets"] if asset["url"] not in urls)
        missing.extend(other["external_embeds"])
    elif legacy.get("is_quote_status"):
        missing.append({"type": "quoted_post", "reason": "引用帖内容不可用或尚未完整取得。"})
    date = legacy.get("created_at")
    if date:
        try:
            date = datetime.strptime(date, "%a %b %d %H:%M:%S %z %Y").isoformat()
        except ValueError:
            pass
    access = "available" if legacy and author else "unavailable"
    return {"id": identifier, "platform": "x", "creator_id": author["creatorId"] if author else None,
            "title": text.replace("\n", " ")[:100] or f"X 帖子 {identifier}", "type": "tweet",
            "access": access, "fee_required": None, "published_at": date, "updated_at": None,
            "text": text, "assets": assets, "external_embeds": missing,
            "content": {"text": text, "tweet": tweet}, "url": f"https://x.com/i/status/{identifier}"}
