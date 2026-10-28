"""The chat web-search tool exposes only explicit, bounded search results."""

import unittest
from datetime import datetime, timezone
from types import SimpleNamespace

from companion_v2.conversation.web_tools import ACTION, WebSearchTools
from companion_v2.execution.models import TaskError
from companion_v2.foundation.refs import ScopeKind


class WebSearchToolTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def context():
        return SimpleNamespace(scope=SimpleNamespace(kind=ScopeKind.PRIVATE))

    @staticmethod
    def fake_run():
        return SimpleNamespace(task=SimpleNamespace(task_id="turn-1"), validate=lambda: None)

    @staticmethod
    def port(calls):
        class Port:
            async def fetch(self):
                calls.append("fetch")
                return SimpleNamespace(items=(
                    SimpleNamespace(title="标题", url="https://news.example/a",
                                    source="示例站", summary="一段摘要",
                                    published_at=datetime(2026, 10, 27, tzinfo=timezone.utc)),
                ))
        return Port()

    async def test_catalog_is_available_only_when_a_provider_factory_exists(self):
        self.assertEqual(WebSearchTools().catalog_for(self.context()), ())
        tool = WebSearchTools(lambda context, query, count: None)
        catalog = tool.catalog_for(self.context())
        self.assertEqual([item.action for item in catalog], [ACTION])

    async def test_execute_returns_a_bounded_normalized_result(self):
        calls = []
        tool = WebSearchTools(lambda context, query, count: self.port(calls))
        result = await tool.execute(self.fake_run(), self.context(),
            {"query": "  今天的科技新闻  ", "count": 3},
            observed_at=datetime(2026, 10, 27, tzinfo=timezone.utc))
        self.assertEqual(calls, ["fetch"])
        self.assertEqual(result["state"], "succeeded")
        self.assertEqual(result["effect"], "none")
        self.assertEqual(result["payload"]["query"], "今天的科技新闻")
        self.assertEqual(result["payload"]["results"], [{
            "title": "标题", "url": "https://news.example/a", "source": "示例站",
            "summary": "一段摘要", "published_at": "2026-10-27T00:00:00+00:00",
        }])
        validated, _ = tool.validate({}, self.context(), result, datetime.now(timezone.utc))
        self.assertEqual(validated, result)

    async def test_invalid_query_is_rejected_without_calling_the_provider(self):
        calls = []
        tool = WebSearchTools(lambda context, query, count: self.port(calls))
        for arguments in ({}, {"query": ""}, {"query": "x" * 129}, {"query": "x", "count": 9},
                          {"query": "x", "extra": True}):
            with self.assertRaises(TaskError):
                await tool.execute(self.fake_run(), self.context(), arguments,
                    observed_at=datetime.now(timezone.utc))
        self.assertEqual(calls, [])

    async def test_missing_provider_fails_closed(self):
        tool = WebSearchTools(lambda context, query, count: None)
        result = await tool.execute(self.fake_run(), self.context(), {"query": "新闻"},
            observed_at=datetime.now(timezone.utc))
        self.assertEqual((result["state"], result["code"]), ("failed", "web_search_unavailable"))

    async def test_one_turn_is_limited_to_two_searches(self):
        calls = []
        tool = WebSearchTools(lambda context, query, count: self.port(calls))
        run = self.fake_run()
        for _ in range(2):
            result = await tool.execute(run, self.context(), {"query": "新闻"},
                observed_at=datetime.now(timezone.utc))
            self.assertEqual(result["state"], "succeeded")
        result = await tool.execute(run, self.context(), {"query": "新闻"},
            observed_at=datetime.now(timezone.utc))
        self.assertEqual((result["state"], result["code"]), ("failed", "web_search_turn_limit"))
        self.assertEqual(calls, ["fetch", "fetch"])


if __name__ == "__main__":
    unittest.main()
