"""On-demand web search for one explicit user request.

The host owns the network and credentials; this module only validates the
bounded query/result contract. Search is never enabled by a background turn.
"""

import asyncio
from datetime import datetime

from companion_v2.execution.models import TaskError, encode
from companion_v2.foundation.refs import ScopeKind
from companion_v2.model_gateway.models import ToolSpec


ACTION = "web.search"
INSTRUCTION = (
    "web.search 只在用户明确要求查网页、新闻或实时资料，或当前事实明显需要外部核实时使用；"
    "不要为了补充闲聊、搜索私聊原文、账号、关系记忆或无关个人信息而调用。一次只查一个具体问题，"
    "优先使用已有上下文；结果标注来源和发布日期，摘要可能过期或不可信，不要当成全文或群内共识。"
    "查不到、超时或没有可用 Provider 时直接说明，不猜搜索结果。"
)
MAX_QUERY_CHARS = 128
MAX_RESULTS = 8
DEFAULT_RESULTS = 5
MAX_FETCH_SECONDS = 12
MAX_SEARCHES_PER_TURN = 2


class WebSearchTools:
    actions = frozenset({ACTION})

    def __init__(self, factory=None):
        self.factory = factory if callable(factory) else None
        self._calls = {}
        self.catalog = (ToolSpec(ACTION, "按一个明确问题搜索公开网页，返回标题、摘要、来源和日期。", encode({
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "一个具体搜索问题，最多 128 字"},
                "count": {"type": "integer", "description": "返回条数，1 到 8，默认 5"},
            },
            "required": ["query"],
            "additionalProperties": False,
        })),)

    def catalog_for(self, context):
        if self.factory is None or context.scope.kind not in (ScopeKind.PRIVATE, ScopeKind.GROUP):
            return ()
        return self.catalog

    async def execute(self, run, context, arguments, *, observed_at):
        query, count = self._arguments(arguments)
        if self.factory is None:
            return {"state": "failed", "code": "web_search_unavailable", "effect": "none"}
        task_id = getattr(getattr(run, "task", None), "task_id", "")
        if type(task_id) is not str or not task_id:
            return {"state": "failed", "code": "web_search_unavailable", "effect": "none"}
        used = self._calls.get(task_id, 0)
        if used >= MAX_SEARCHES_PER_TURN:
            return {"state": "failed", "code": "web_search_turn_limit", "effect": "none"}
        if len(self._calls) > 512:
            self._calls.pop(next(iter(self._calls)))
        self._calls[task_id] = used + 1
        try:
            port = self.factory(context, query, count)
        except Exception:
            port = None
        if port is None:
            return {"state": "failed", "code": "web_search_unavailable", "effect": "none"}
        try:
            batch = await asyncio.wait_for(port.fetch(), timeout=MAX_FETCH_SECONDS)
        except Exception:
            return {"state": "failed", "code": "web_search_unavailable", "effect": "none"}
        run.validate()
        results = []
        items = tuple(getattr(batch, "items", ())) if batch is not None else ()
        for item in items[:count]:
            title = str(getattr(item, "title", "") or "").strip()
            url = str(getattr(item, "url", "") or "").strip()
            if not title or not url:
                continue
            published = getattr(item, "published_at", None)
            results.append({
                "title": title[:240],
                "url": url[:2048],
                "source": str(getattr(item, "source", "") or "").strip()[:120],
                "summary": str(getattr(item, "summary", "") or "").strip()[:1200],
                "published_at": published.isoformat() if isinstance(published, datetime) and published.tzinfo else None,
            })
        return {"state": "succeeded", "effect": "none",
                "payload": {"query": query, "results": results,
                            "observed_at": observed_at.isoformat() if hasattr(observed_at, "isoformat") else None}}

    def validate(self, row, context, result, observed_at):
        if result.get("state") != "succeeded" or result.get("effect") != "none":
            raise TaskError("web_search_result_invalid", "the original web search result changed")
        payload = result.get("payload")
        if type(payload) is not dict or set(payload) != {"query", "results", "observed_at"}:
            raise TaskError("web_search_result_invalid", "the original web search payload is invalid")
        if (type(payload["query"]) is not str or not 1 <= len(payload["query"]) <= MAX_QUERY_CHARS
                or type(payload["results"]) is not list or len(payload["results"]) > MAX_RESULTS):
            raise TaskError("web_search_result_invalid", "the original web search result is invalid")
        for item in payload["results"]:
            if (type(item) is not dict
                    or set(item) != {"title", "url", "source", "summary", "published_at"}
                    or any(type(item[key]) is not str for key in ("title", "url", "source", "summary"))
                    or item["published_at"] is not None and type(item["published_at"]) is not str):
                raise TaskError("web_search_result_invalid", "the original web search item is invalid")
        return result, None

    @staticmethod
    def _arguments(arguments):
        if type(arguments) is not dict or set(arguments) - {"query", "count"} or "query" not in arguments:
            raise TaskError("invalid_tool_arguments", "web.search requires one bounded query")
        query = arguments["query"]
        count = arguments.get("count", DEFAULT_RESULTS)
        if (type(query) is not str or not 1 <= len(query.strip()) <= MAX_QUERY_CHARS
                or "\x00" in query or type(count) is not int or not 1 <= count <= MAX_RESULTS):
            raise TaskError("invalid_tool_arguments", "web.search requires a bounded query and count")
        return query.strip(), count


def search_action(action):
    return action in WebSearchTools.actions
