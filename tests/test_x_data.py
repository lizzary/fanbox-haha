import unittest

from fanbox_lab import LabError
from x_data import (detail_tweets, normalize_tweet, status_id, timeline,
                    user_record, username, validate_payload)


def user(handle="artist", identifier="901"):
    return {"__typename": "User", "rest_id": identifier,
            "core": {"screen_name": handle, "name": handle.title()}, "legacy": {"protected": False}}


def tweet(identifier="111", handle="artist", text="Test post"):
    return {"__typename": "Tweet", "rest_id": identifier,
            "core": {"user_results": {"result": user(handle)}},
            "legacy": {"full_text": text, "created_at": "Mon Nov 02 10:00:00 +0000 2026", "entities": {}}}


def tweet_entry(value):
    return {"entryId": "tweet-" + value["rest_id"],
            "content": {"itemContent": {"tweet_results": {"result": value}}}}


def timeline_payload(entries, *, cursor=None, ended=False):
    entries = list(entries)
    if cursor:
        entries.append({"entryId": "cursor-bottom-test", "content": {"cursorType": "Bottom", "value": cursor}})
    instructions = [{"type": "TimelineAddEntries", "entries": entries}]
    if ended:
        instructions.append({"type": "TimelineTerminateTimeline", "direction": "Bottom"})
    return {"data": {"user": {"result": {"timeline_v2": {"timeline": {"instructions": instructions}}}}}}


class XDataTests(unittest.TestCase):
    def test_accepts_x_and_twitter_links_but_not_lookalike_hosts(self):
        self.assertEqual(username("https://x.com/artist"), "artist")
        self.assertEqual(username("@artist"), "artist")
        for value in ("https://twitter.com/artist/status/123?x=1", "https://x.com/i/web/status/123", "123"):
            self.assertEqual(status_id(value), "123")
        for value in ("https://x.com.example.com/artist/status/123", "https://x.com@evil.example/a/status/123"):
            with self.assertRaises(LabError):
                status_id(value)

    def test_timeline_keeps_modules_and_skips_promoted_and_nested_quotes(self):
        primary = tweet()
        primary["quoted_status_result"] = {"result": tweet("999", "other")}
        promoted = tweet_entry(tweet("998"))
        promoted["content"]["itemContent"]["promotedMetadata"] = {"advertiser": "test"}
        module = {"content": {"items": [{"item": tweet_entry(tweet("112"))["content"]}]}}
        parsed = timeline(timeline_payload([tweet_entry(primary), promoted, module], cursor="next"), "tweets")
        self.assertEqual([item["rest_id"] for item in parsed.items], ["111", "112"])
        self.assertEqual(parsed.cursor, "next")
        self.assertFalse(parsed.ended)

    def test_empty_unknown_response_is_not_treated_as_end(self):
        with self.assertRaises(LabError):
            timeline({"data": {}}, "tweets")
        self.assertTrue(timeline(timeline_payload([], cursor="0|end"), "users").ended)

    def test_long_text_original_photo_and_highest_mp4(self):
        value = tweet(text="short")
        value["note_tweet"] = {"note_tweet_results": {"result": {"text": "Long text " * 60}}}
        value["legacy"]["truncated"] = True
        value["legacy"]["extended_entities"] = {"media": [
            {"id_str": "1", "type": "photo", "media_url_https": "https://pbs.twimg.com/media/photo.jpg?name=small"},
            {"id_str": "2", "type": "video", "video_info": {"variants": [
                {"content_type": "application/x-mpegURL", "url": "https://video.twimg.com/a.m3u8"},
                {"content_type": "video/mp4", "bitrate": 100, "url": "https://video.twimg.com/low.mp4"},
                {"content_type": "video/mp4", "bitrate": 500, "url": "https://video.twimg.com/high.mp4"},
            ]}},
        ]}
        result = normalize_tweet(value)
        self.assertGreater(len(result["text"]), 280)
        self.assertIn("name=orig", result["assets"][0]["url"])
        self.assertEqual(result["assets"][1]["url"], "https://video.twimg.com/high.mp4")
        self.assertEqual(result["external_embeds"], [])

    def test_hls_only_and_truncated_text_are_explicitly_incomplete(self):
        value = tweet()
        value["legacy"]["truncated"] = True
        value["legacy"]["extended_entities"] = {"media": [{"id_str": "1", "type": "video", "video_info": {"variants": []}}]}
        result = normalize_tweet(value)
        self.assertEqual(len(result["external_embeds"]), 2)

    def test_quote_content_is_saved_and_repost_uses_original_identity(self):
        value = tweet()
        value["legacy"]["is_quote_status"] = True
        value["quoted_status_result"] = {"result": tweet("222", "quoted", "Quoted text")}
        result = normalize_tweet(value)
        self.assertIn("Quoted text", result["text"])
        self.assertFalse(result["external_embeds"])
        repost = tweet("333")
        repost["legacy"]["retweeted_status_result"] = {"result": value}
        self.assertEqual(normalize_tweet(repost)["id"], "111")
        self.assertEqual(normalize_tweet(repost)["repost_id"], "333")

    def test_auth_failure_and_rate_limit_have_different_outcomes(self):
        with self.assertRaises(LabError) as auth:
            validate_payload({"errors": [{"code": 89}]})
        self.assertEqual(auth.exception.code, 3)
        with self.assertRaises(LabError) as rate:
            validate_payload({"errors": [{"code": 88}]})
        self.assertEqual(rate.exception.code, 1)

    def test_unavailable_user_and_wrapped_detail(self):
        self.assertIsNone(user_record({"__typename": "UserUnavailable", "rest_id": "12"}))
        payload = {"data": {"tweetResult": {"result": {"__typename": "TweetWithVisibilityResults", "tweet": tweet()}}}}
        self.assertEqual(detail_tweets(payload)[0]["rest_id"], "111")
