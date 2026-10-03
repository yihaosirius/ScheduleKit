"""离线 mock LLM —— 测试与"没配 Key"时的兜底。

设计目标不是"假装智能"，而是**让编排逻辑可测**。所以它做两件事：

1. 按**确定性规则**从文本里抽事项（不联网、不随机），
   这样端到端测试可以对结果做精确断言。
2. 通过 ``mode`` 精确复现四种真实失败模式，让每条降级路径都能被测到：
   ``tool_call`` / ``text_only``（200 但没有工具调用）/ ``truncated`` /
   ``http_400``（供应商拒绝该形状）。

``mode="auto"`` 时会看 system prompt 里有没有降级后缀来决定返回工具调用还是 JSON——
这样 mock 的行为与真实模型在"被要求换格式就会换"这一点上一致，
编排逻辑不需要为 mock 开特例。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any

from app.llm.base import (
    LLMChannelRejected,
    LLMOutputTruncated,
    LLMRequest,
    LLMResult,
    PATH_JSON_OBJECT,
    PATH_TOOL_CALL,
)
from app.llm.tools import JSON_FALLBACK_SUFFIX, MAX_ITEMS

MODE_AUTO = "auto"
MODE_TEXT_ONLY = "text_only"
MODE_TRUNCATED = "truncated"
MODE_HTTP_400 = "http_400"

#: 明确的截止时间线索 → 生成 due_at（用当天本地日期，由测试自己换算）
_DUE_HINTS = ("截止", "ddl", "DDL", "deadline", "下周一", "周一交", "本周日", "周日交", "明天", "后天")
#: 相对时间映射到"几天后"
_RELATIVE_DAYS = (
    ("明天", 1),
    ("后天", 2),
    ("下周一", 7),
    ("下周三", 9),
    ("下周五", 11),
    ("本周日", 7),
    ("周日交", 7),
)
_CATEGORY_HINTS = (
    ("考试", "exam"),
    ("考", "exam"),
    ("作业", "homework"),
    ("习题", "homework"),
    ("练习", "practice"),
    ("背单词", "practice"),
    ("约", "appointment"),
    ("会", "appointment"),
)
_PRIORITY_HINTS = (
    ("立刻", 1),
    ("马上", 1),
    ("急", 1),
    ("本周", 2),
    ("重要", 2),
    ("有空", 5),
    ("随便", 5),
    ("闲", 4),
)


class MockLLM:
    """不联网的抽取器。"""

    name = "mock"

    def __init__(self, mode: str = MODE_AUTO, *, delay: float = 0.0) -> None:
        self.mode = mode
        self.delay = delay
        self.calls: list[LLMRequest] = []

    async def extract(self, request: LLMRequest) -> LLMResult:
        self.calls.append(request)
        started = time.perf_counter()
        if self.delay:
            await asyncio.sleep(self.delay)

        if self.mode == MODE_HTTP_400:
            raise LLMChannelRejected("mock：供应商拒绝该请求形状（模拟 400）")
        if self.mode == MODE_TRUNCATED:
            raise LLMOutputTruncated("mock：输出被 max_tokens 截断")

        items = self._extract(request)
        payload = {"items": items}
        raw = json.dumps(payload, ensure_ascii=False)
        wants_json = JSON_FALLBACK_SUFFIX.strip() in request.system

        if self.mode == MODE_TEXT_ONLY or wants_json:
            return LLMResult(
                items=items,
                raw=raw,
                provider=self.name,
                model="mock",
                elapsed_ms=(time.perf_counter() - started) * 1000,
                path=PATH_JSON_OBJECT if wants_json else PATH_TOOL_CALL,
                fallback_note=None if wants_json else "mock：只在 content 里返回了 JSON",
                usage={"input_tokens": 100, "output_tokens": 50},
            )

        return LLMResult(
            items=items,
            raw=raw,
            provider=self.name,
            model="mock",
            elapsed_ms=(time.perf_counter() - started) * 1000,
            path=PATH_TOOL_CALL,
            usage={"input_tokens": 100, "output_tokens": 50},
        )

    async def aclose(self) -> None:
        return None

    # ------------------------------------------------------------------ 抽取
    def _extract(self, request: LLMRequest) -> list[dict[str, Any]]:
        from app.services.timetable import WEEKDAY_NAMES

        text = request.user_text
        # 提示词里固定的说明行不算待识别内容。mock 只处理"实际内容"部分：
        # 真实适配器把上下文与内容一起发，这里按同样的结构切出内容段。
        content = _content_of(text)
        candidates = _candidates(content)

        items: list[dict[str, Any]] = []
        # 从周几推导日期需要"今天是周几"，mock 不解析上下文里的日期，
        # 只用内容里出现的相对时间词，保持确定性。
        for line in candidates[:MAX_ITEMS]:
            item = self._one(line, has_images=bool(request.images))
            if item:
                items.append(item)
        return items

    def _one(self, line: str, *, has_images: bool) -> dict[str, Any] | None:
        title = _title_of(line)
        if not title:
            return None

        due_days = _relative_days(line)
        has_due_hint = due_days is not None or any(hint in line for hint in _DUE_HINTS)
        category = _category_of(line)

        item: dict[str, Any] = {
            "title": title,
            "category": category,
            "due_at": None,
            "priority": None,
            "source_quote": line[:20],
        }
        if has_due_hint:
            # 偏移量交给 normalize 处理：mock 只输出"相对天数"这个信号，
            # 这样它的行为与真实模型"给出 ISO 时间"是不同的，但都在 normalize 的处理范围内。
            item["due_at"] = _placeholder_due(due_days)
        else:
            item["priority"] = _priority_of(line)
        return item


def _placeholder_due(days: int | None) -> str:
    """Mock 只给一个明显是模板的占位符，真正的日期由 normalize 依据上下文补。

    刻意**不**在这里算日期：如果 mock 自己算，测试就测不到"补时区/补日期"的逻辑。
    """
    return f"__RELATIVE_DAYS__{days if days is not None else 3}"


def _content_of(text: str) -> str:
    """切出"待识别内容"段。

    真实请求的 user message 由"上下文 + 内容"拼成，用同一个分隔标记切分，
    这样 mock 测的就是真实结构，而不是一个只有 mock 见过的形状。
    """
    marker = "【待识别内容】"
    if marker in text:
        return text.split(marker, 1)[1]
    return text


def _candidates(content: str) -> list[str]:
    """把内容切成候选行。以换行、分号、句号、问号、感叹号为界。"""
    raw_lines = re.split(r"[\n;；。!！？?]+", content)
    return [line.strip(" \t-·•*-—　") for line in raw_lines if line.strip(" \t-·•*-—　")]


def _title_of(line: str) -> str:
    # 去掉明显的时间前缀/后缀，模拟"title 不含时间"的约定
    title = re.sub(r"(截止|ddl|DDL|deadline)[:：]?", "", line)
    title = re.sub(r"(明天|后天|下周一|下周三|下周五|本周日|周一交|周日交)", "", title)
    title = re.sub(r"\d{1,2}\s*月\s*\d{1,2}\s*[日号]", "", title)
    title = re.sub(r"\s+", " ", title).strip(" :：,，-")
    return title[:20]


def _relative_days(line: str) -> int | None:
    for word, days in _RELATIVE_DAYS:
        if word in line:
            return days
    return None


def _category_of(line: str) -> str:
    for word, category in _CATEGORY_HINTS:
        if word in line:
            return category
    return "other"


def _priority_of(line: str) -> int:
    for word, value in _PRIORITY_HINTS:
        if word in line:
            return value
    return 3
