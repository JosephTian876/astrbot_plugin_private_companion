"""链接正文材料：只读一页、拒绝内网、失败要落到明确状态。"""

from __future__ import annotations

import asyncio
import unittest

from astrbot_host.web_read_material import (
    MAX_CHARS,
    _bounded,
    _read_with_fetch,
    address_allowed,
    extract_html,
    find_urls,
    read_page,
)


class FindUrlsTests(unittest.TestCase):
    def test_extracts_links_in_order_and_drops_wrapping_punctuation(self):
        text = "看看这个 https://example.com/a?b=1，还有 https://example.org/x。"
        self.assertEqual(find_urls(text), ("https://example.com/a?b=1", "https://example.org/x"))

    def test_caps_at_two_links_and_deduplicates(self):
        text = " ".join(f"https://example.com/{index}" for index in range(5)) + " https://example.com/0"
        urls = find_urls(text)
        self.assertEqual(len(urls), 2)
        self.assertEqual(urls[0], "https://example.com/0")

    def test_ignores_text_without_links(self):
        self.assertEqual(find_urls("今天天气不错"), ())
        self.assertEqual(find_urls(None), ())


class AddressGuardTests(unittest.TestCase):
    def test_private_loopback_and_metadata_addresses_are_refused(self):
        for host in ("127.0.0.1", "10.0.0.1", "192.168.1.10", "169.254.169.254", "localhost"):
            self.assertFalse(address_allowed(host), host)

    def test_public_address_is_allowed(self):
        # 数字 IP 不需要 DNS；解析不出来时跳过，避免在没有网络的环境里误报。
        if not address_allowed("8.8.8.8"):
            self.skipTest("当前环境无法解析公网地址")
        self.assertTrue(address_allowed("8.8.8.8"))

    def test_empty_host_is_refused(self):
        self.assertFalse(address_allowed(""))


class ExtractTests(unittest.TestCase):
    def test_title_and_body_are_separated(self):
        html = ("<html><head><title>一篇 文章</title></head>"
                "<body><h1>标题</h1><p>第一段正文</p><script>var x=1</script></body></html>")
        title, body = extract_html(html.encode("utf-8"))
        self.assertEqual(title, "一篇 文章")
        self.assertIn("第一段正文", body)
        self.assertNotIn("var x", body)

    def test_long_body_is_truncated_to_the_material_budget(self):
        result = _bounded({"url": "https://example.com"}, title="", body="字" * (MAX_CHARS + 500))
        self.assertEqual(result["status"], "full")
        self.assertEqual(len(result["text"]), MAX_CHARS)

    def test_empty_body_falls_back_to_title_only(self):
        result = _bounded({"url": "https://example.com"}, title="只有标题", body="   ")
        self.assertEqual(result["status"], "title_only")
        self.assertEqual(result["title"], "只有标题")
        self.assertEqual(result["text"], "")


class ReadPageGuardTests(unittest.IsolatedAsyncioTestCase):
    def test_private_address_is_blocked_before_any_request(self):
        result = asyncio.run(read_page(None, "http://127.0.0.1/admin"))
        self.assertEqual(result["status"], "blocked")

    def test_non_http_scheme_is_refused(self):
        result = asyncio.run(read_page(None, "file:///etc/passwd"))
        self.assertEqual(result["status"], "unsupported")

    async def test_deepseek_fetch_is_a_public_url_fallback(self):
        calls = []

        async def fetch(url):
            calls.append(url)
            return {"title": "抓取标题", "content": "抓取正文"}

        result = await _read_with_fetch(fetch, "https://8.8.8.8/article", 100)
        self.assertEqual(result["status"], "full")
        self.assertEqual(result["basis"], "deepseek_fetch")
        self.assertEqual(result["text"], "抓取正文")
        self.assertEqual(calls, ["https://8.8.8.8/article"])

    async def test_deepseek_fetch_never_receives_private_or_credentialed_urls(self):
        calls = []

        async def fetch(url):
            calls.append(url)
            return {"title": "不该抓取", "content": "正文"}

        for url in ("http://127.0.0.1/private", "https://user:pass@8.8.8.8/article"):
            self.assertIsNone(await _read_with_fetch(fetch, url, 100))
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
