import unittest

from fanbox_lab import LabError, creator_id, list_items, normalize_post, post_id


class ParsingTests(unittest.TestCase):
    def test_official_creator_and_post_url_formats(self):
        for url in ("szcb911", "https://www.fanbox.cc/@szcb911", "https://szcb911.fanbox.cc/"):
            self.assertEqual(creator_id(url), "szcb911")
        for url in ("123", "https://www.fanbox.cc/@szcb911/posts/123?utm_source=test",
                    "https://szcb911.fanbox.cc/posts/123/"):
            self.assertEqual(post_id(url), "123")

    def test_untrusted_hosts_and_non_post_paths_are_rejected(self):
        for url in ("https://fanbox.cc.example.com/posts/123", "https://evilfanbox.cc/posts/123",
                    "https://www.fanbox.cc@evil.example/posts/123", "http://www.fanbox.cc/posts/123",
                    "https://www.fanbox.cc/@szcb911", "../123"):
            with self.subTest(url=url), self.assertRaises(LabError):
                post_id(url)

    def test_live_list_schema_and_unknown_schema(self):
        post = {"id": "123", "isRestricted": True}
        self.assertEqual(list_items({"body": {"posts": [post]}}), [post])
        self.assertEqual(list_items({"body": {"items": [post]}}), [post])
        self.assertEqual(list_items({"body": {"posts": []}}), [])
        with self.assertRaises(LabError):
            list_items({"body": {"newSchema": []}})

    def test_locked_and_missing_body_are_never_full_content(self):
        locked = normalize_post({"id": "123", "isRestricted": True, "body": None})
        self.assertEqual(locked["access"], "restricted")
        self.assertEqual(locked["assets"], [])
        missing = normalize_post({"id": "123", "isRestricted": False, "body": None})
        self.assertEqual(missing["access"], "unavailable")

    def test_article_preserves_blocks_embeds_and_deduplicates_assets(self):
        body = {
            "blocks": [{"type": "p", "text": "正文"}, {"type": "image", "imageId": "i"},
                       {"type": "embed", "embedId": "v"}],
            "imageMap": {"i": {"id": "i", "originalUrl": "https://example.test/image.jpg"}},
            "images": [{"id": "i", "originalUrl": "https://example.test/image.jpg"}],
            "fileMap": {"f": {"id": "f", "name": "source", "extension": "zip",
                              "url": "https://example.test/source.zip"}},
            "embedMap": {"v": {"serviceProvider": "youtube", "videoId": "test"}},
        }
        result = normalize_post({"id": "123", "type": "article", "body": body})
        self.assertEqual(result["access"], "available")
        self.assertEqual(result["text"], "正文")
        self.assertEqual(len(result["assets"]), 2)
        self.assertEqual(result["content"], body)


if __name__ == "__main__":
    unittest.main()
