"""两个协议的传输层 + 三通道编排。

这个文件是整条识别链路里最需要小心的部分，因为它同时处理三件容易出错的事：
HTTP 重试、协议形状差异、以及有界降级。三条原则：

1. **协议形状由子类负责**，重试与降级由基类负责。两者混在一起时，
   一个"responses 的 tool_choice 写成了 chat 形状"的 bug 会被误判成网络问题。
2. **哪些错误重试、哪些不重试要显式分类**。401/400 这类确定性错误重试三次
   只会浪费 3 倍时间并把真正的原因埋起来。
3. **降级必须带原因**。``fallback_note`` 会一路带到草稿与确认页。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx

from app.llm.base import (
    LLMAllChannelsFailed,
    LLMChannelRejected,
    LLMNotConfigured,
    LLMOutputTruncated,
    LLMRequest,
    LLMRequestFailed,
    LLMResult,
    LLMToolCallMissing,
    LLMTransient,
    PATH_JSON_OBJECT,
    PATH_JSON_SCHEMA,
    PATH_TOOL_CALL,
)
from app.llm.tools import (
    JSON_FALLBACK_SUFFIX,
    json_object_format,
    json_schema_text_format,
)
from app.logging import get_logger, kv

log = get_logger("llm")

#: 整个请求（含重试）的总时间预算。timeout_seconds 是单次请求的超时，
#: 重试三次可能累积到 3 倍，这里再兜一层，避免请求挂住十几分钟。
TOTAL_BUDGET_SECONDS = 180.0


def _channels(cfg) -> list[str]:
    """通道顺序：强制工具调用 → 结构化 JSON → 自由 JSON。

    顺序**不随配置变化**，这是刻意的：三条通道的价值依次下降
    （约束解码 > 结构化输出 > 格式靠 prompt 约束），所以顺序也应当是固定的。
    ``thinking`` 影响的是"第一条通道用什么 tool_choice"（见各适配器的 ``_payload``），
    而不是"先试哪条通道"——把两件事混在一起会让"为什么这次走了降级"变得难以解释。
    """
    return [PATH_TOOL_CALL, PATH_JSON_SCHEMA, PATH_JSON_OBJECT]


class HttpStructuredLLM:
    """两个协议共用的骨架。子类只需实现三个钩子。

    构造函数收的是**整份** :class:`app.config.Config`，不是 ``[llm]`` 段。
    这一点踩过一次：最初传的是 ``cfg.llm``，于是 ``self.cfg.validate_for_llm()``
    抛 ``AttributeError``（那个方法在 Config 上、不在 LLMConfig 上），
    而**测试全绿**——因为测试里 provider 是 mock，mock 不校验配置。
    教训：适配器需要配置的"校验入口"时，要传能提供它的那个对象。
    """

    name = "http"

    def __init__(self, cfg) -> None:
        self.config = cfg
        self.cfg = cfg.llm
        self._client: httpx.AsyncClient | None = None

    # ------------------------------------------------------------------ 钩子
    def _endpoint(self) -> str:
        raise NotImplementedError

    def _payload(self, request: LLMRequest, *, channel: str, system: str) -> dict[str, Any]:
        raise NotImplementedError

    def _parse(self, data: dict[str, Any], *, channel: str) -> tuple[list[dict[str, Any]], str]:
        """解析响应 → ``(items, raw_text)``。拿不到结构化输出时抛
        :class:`LLMChannelRejected`。"""
        raise NotImplementedError

    # ------------------------------------------------------------------ 编排
    async def extract(self, request: LLMRequest) -> LLMResult:
        # 用整份配置的校验入口：它会检查 api_key、base_url 等本次调用真正需要的字段，
        # 并抛出可读的 ConfigError（而不是等到发出请求才拿到 401）。
        self.config.validate_for_llm()
        started = time.perf_counter()
        deadline = started + TOTAL_BUDGET_SECONDS

        attempts = 0
        last_error: Exception | None = None
        notes: list[str] = []

        for channel in _channels(self.cfg):
            system, note = self._system_for(request, channel)
            if note:
                notes.append(note)
            try:
                items, raw, used, attempts_delta = await self._run_channel(
                    request, channel=channel, system=system, deadline=deadline
                )
            except LLMChannelRejected as exc:
                attempts += 1
                notes.append(f"{channel} 通道不可用：{exc}")
                log.warning(
                    "llm.channel_rejected %s",
                    kv(channel=channel, reason=str(exc)),
                )
                last_error = exc
                continue
            except LLMTransient as exc:
                last_error = exc
                notes.append(f"{channel} 通道临时失败：{exc}")
                continue

            attempts += attempts_delta
            result = LLMResult(
                items=items,
                raw=raw,
                provider=self.name,
                model=self.cfg.model,
                elapsed_ms=(time.perf_counter() - started) * 1000,
                usage=used.get("usage", {}) if isinstance(used, dict) else {},
                path=channel,
                fallback_note="；".join(notes) or None,
                attempts=attempts,
                thinking=self.cfg.thinking,
            )
            log.info(
                "llm.response %s",
                kv(
                    tier=result.path,
                    degraded=result.degraded,
                    items=len(items),
                    raw_chars=len(raw),
                    attempts=attempts,
                    elapsed_ms=round(result.elapsed_ms, 1),
                    note=result.fallback_note,
                ),
            )
            return result

        log.error("llm.all_channels_failed %s", kv(notes=" | ".join(notes)))
        if last_error is not None and isinstance(last_error, LLMTransient):
            # 临时性失败：保留"可重试"的语义，交给上层决定退避重试
            raise last_error
        raise LLMAllChannelsFailed(
            "所有结构化通道都失败了：" + ("；".join(notes) or "没有可用通道"),
            notes=notes,
        )

    def _system_for(self, request: LLMRequest, channel: str) -> tuple[str, str | None]:
        if channel == PATH_TOOL_CALL:
            return request.system, None
        # 降级通道：必须把"改为输出 JSON"的指令追加进去，且它要**覆盖**前面的工具指令
        return request.system + JSON_FALLBACK_SUFFIX, f"{channel} 降级通道"

    async def _run_channel(
        self,
        request: LLMRequest,
        *,
        channel: str,
        system: str,
        deadline: float,
    ) -> tuple[list[dict[str, Any]], str, dict[str, Any], int]:
        """跑一条通道，内含重试。返回 ``(items, raw, body, attempts)``。"""
        attempts = 0
        backoff = max(0.1, float(self.cfg.retry_backoff_seconds))
        max_attempts = max(1, int(self.cfg.retry_count) + 1)

        while True:
            if time.perf_counter() > deadline:
                raise LLMTransient("总时间预算耗尽")
            attempts += 1
            try:
                data = await self._post(request, channel=channel, system=system)
            except LLMTransient as exc:
                if attempts >= max_attempts:
                    raise
                wait = exc.retry_after if exc.retry_after is not None else backoff
                wait = min(wait, max(0.0, deadline - time.perf_counter()))
                log.warning(
                    "llm.retry %s",
                    kv(channel=channel, attempt=attempts, wait_s=round(wait, 2), reason=str(exc)),
                )
                await asyncio.sleep(wait)
                backoff = min(backoff * 2, 8.0)
                continue

            items, raw = self._parse(data, channel=channel)
            return items, raw, data, attempts

    async def _post(
        self, request: LLMRequest, *, channel: str, system: str
    ) -> dict[str, Any]:
        payload = self._payload(request, channel=channel, system=system)
        endpoint = self._endpoint()
        timeout = httpx.Timeout(float(self.cfg.timeout_seconds), connect=15.0)

        log.info(
            "llm.request %s",
            kv(
                provider=self.name,
                channel=channel,
                model=self.cfg.model,
                endpoint=endpoint,
                thinking=self.cfg.thinking,
                images=len(request.images),
                chars=len(request.user_text),
            ),
        )

        client = await self._ensure_client()
        try:
            response = await client.post(
                endpoint,
                json=payload,
                headers=self._headers(),
                timeout=timeout,
            )
        except httpx.TimeoutException as exc:
            raise LLMTransient(f"请求超时（{self.cfg.timeout_seconds}s）") from exc
        except httpx.HTTPError as exc:
            raise LLMTransient(f"网络错误：{type(exc).__name__}") from exc

        if response.status_code in (408, 409, 425, 429) or response.status_code >= 500:
            raise LLMTransient(
                f"HTTP {response.status_code}：{_short(response.text)}"
            )

        if response.status_code >= 400:
            # 400/401/403/422 都是确定性的：形状不被接受、Key 不对、余额不足……
            # 重试没有任何意义，直接换通道或报错。
            detail = _short(response.text, 300)
            log.error(
                "llm.http_error %s",
                kv(status=response.status_code, channel=channel, detail=detail),
            )
            if response.status_code in (400, 422):
                raise LLMChannelRejected(
                    f"HTTP {response.status_code}，该通道形状不被接受：{detail}"
                )
            raise LLMRequestFailed(f"HTTP {response.status_code}：{detail}")

        try:
            return response.json()
        except ValueError as exc:
            raise LLMChannelRejected("响应不是合法 JSON") from exc

    # ------------------------------------------------------------------ 连接
    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                # 不跟随重定向：base_url 配错时应立刻报错，而不是被引到别处
                follow_redirects=False,
                headers={"Accept": "application/json"},
            )
        return self._client

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.cfg.api_key}",
            "Content-Type": "application/json",
        }

    async def aclose(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None


def _short(text: str, limit: int = 200) -> str:
    cleaned = " ".join(str(text).split())
    return cleaned if len(cleaned) <= limit else cleaned[:limit] + "…"


# --------------------------------------------------------------------------- #
# /responses
# --------------------------------------------------------------------------- #
class ResponsesLLM(HttpStructuredLLM):
    """OpenAI Responses 协议（DeepSeek 也提供，见其 API 文档）。"""

    name = "responses"

    def _endpoint(self) -> str:
        return f"{self.cfg.base_url}/responses"

    def _payload(self, request: LLMRequest, *, channel: str, system: str) -> dict[str, Any]:
        from app.llm.tools import responses_forced_tool_choice, responses_tool

        content: list[dict[str, Any]] = [{"type": "input_text", "text": request.user_text}]
        for data, mime in zip(request.images, request.image_mimes):
            from app.media import to_llm_data_url

            content.append(
                {
                    "type": "input_image",
                    # 用 data URL 内联，而不是传可访问的 URL：模型侧拿不到我们的鉴权，
                    # 传 URL 就得开一个匿名可读的图片端点
                    "image_url": to_llm_data_url(data, mime),
                    "detail": "high",
                }
            )

        payload: dict[str, Any] = {
            "model": self.cfg.model,
            "instructions": system,
            "input": [{"role": "user", "content": content}],
            "max_output_tokens": self.cfg.max_tokens,
        }

        if self.cfg.thinking:
            # reasoning.effort 而非 thinking.type —— 这是 Responses 协议的参数名
            payload["reasoning"] = {"effort": self.cfg.reasoning_effort}
        else:
            # 显式关闭思考：不开的话默认是开的，而开启后具名 tool_choice 会被拒
            payload["reasoning"] = {"effort": "none"}
            payload["temperature"] = self.cfg.temperature

        if channel == PATH_TOOL_CALL:
            payload["tools"] = [responses_tool()]
            if self.cfg.thinking:
                # thinking 下不能用 required/具名，只能 auto
                payload["tool_choice"] = "auto"
            else:
                payload["tool_choice"] = responses_forced_tool_choice()
        elif channel == PATH_JSON_SCHEMA:
            payload["text"] = {"format": json_schema_text_format()}
        else:
            payload["text"] = {"format": json_object_format()}
        return payload

    def _parse(
        self, data: dict[str, Any], *, channel: str
    ) -> tuple[list[dict[str, Any]], str]:
        status = data.get("status")
        if status == "failed":
            error = data.get("error") or {}
            raise LLMChannelRejected(
                f"响应失败：{error.get('code') or ''} {error.get('message') or ''}".strip()
            )
        if status == "incomplete":
            reason = (data.get("incomplete_details") or {}).get("reason")
            if reason == "max_output_tokens":
                raise LLMOutputTruncated(
                    f"输出被 max_output_tokens 截断（当前 {self.cfg.max_tokens}，请调大）"
                )
            raise LLMChannelRejected(f"响应不完整：{reason}")

        output = data.get("output") or []
        raw_parts: list[str] = []
        for item in output:
            if item.get("type") != "function_call":
                continue
            if item.get("name") != "submit_tasks":
                continue
            arguments = item.get("arguments") or ""
            raw_parts.append(arguments)
            try:
                import json

                parsed = json.loads(arguments)
            except ValueError as exc:
                raise LLMChannelRejected(f"工具参数不是合法 JSON：{_short(arguments)}") from exc
            items = parsed.get("items")
            if not isinstance(items, list):
                raise LLMChannelRejected("工具参数缺少 items 数组")
            return items, arguments

        # 没有 function_call：看看是不是走结构化输出返回了文本
        text = _collect_output_text(output)
        raw_parts.append(text)
        if text:
            items = _items_from_json(text)
            if items is not None:
                return items, text

        raise LLMChannelRejected(
            "响应里没有 function_call，也没有可解析的 JSON 文本"
            f"（status={status}，output 类型={[i.get('type') for i in output]}）"
        )


def _collect_output_text(output: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for item in output:
        if item.get("type") != "message":
            continue
        for part in item.get("content") or []:
            if part.get("type") == "output_text":
                parts.append(part.get("text") or "")
    return "".join(parts).strip()


# --------------------------------------------------------------------------- #
# /chat/completions
# --------------------------------------------------------------------------- #
class ChatCompatLLM(HttpStructuredLLM):
    """OpenAI Chat Completions 兼容协议。"""

    name = "chat"

    def _endpoint(self) -> str:
        return f"{self.cfg.base_url}/chat/completions"

    def _payload(self, request: LLMRequest, *, channel: str, system: str) -> dict[str, Any]:
        from app.llm.tools import chat_forced_tool_choice, chat_tool

        content: list[dict[str, Any]] = [{"type": "text", "text": request.user_text}]
        for data, mime in zip(request.images, request.image_mimes):
            from app.media import to_llm_data_url

            content.append(
                {"type": "image_url", "image_url": {"url": to_llm_data_url(data, mime)}}
            )

        payload: dict[str, Any] = {
            "model": self.cfg.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": content if request.images else request.user_text},
            ],
            "max_tokens": self.cfg.max_tokens,
        }

        if self.cfg.thinking:
            payload["thinking"] = {"type": "enabled"}
            payload["reasoning_effort"] = self.cfg.reasoning_effort
        else:
            # 关键：thinking 开着时服务端会拒绝 required/具名 tool_choice（400），
            # 所以主通道必须显式关掉思考。
            payload["thinking"] = {"type": "disabled"}
            payload["temperature"] = self.cfg.temperature

        if channel == PATH_TOOL_CALL:
            payload["tools"] = [chat_tool()]
            payload["tool_choice"] = "auto" if self.cfg.thinking else chat_forced_tool_choice()
        else:
            # chat 协议只有 json_object，没有 json_schema。
            # 这也解释了为什么 responses 通道优先：它能拿到约束解码。
            payload["response_format"] = json_object_format()
        return payload

    def _parse(
        self, data: dict[str, Any], *, channel: str
    ) -> tuple[list[dict[str, Any]], str]:
        choices = data.get("choices") or []
        if not choices:
            raise LLMChannelRejected("响应里没有 choices")

        choice = choices[0]
        finish = choice.get("finish_reason")
        message = choice.get("message") or {}

        if finish == "length":
            raise LLMOutputTruncated(
                f"输出被 max_tokens 截断（当前 {self.cfg.max_tokens}，请调大）"
            )

        tool_calls = message.get("tool_calls") or []
        for call in tool_calls:
            function = call.get("function") or {}
            if function.get("name") != "submit_tasks":
                continue
            arguments = function.get("arguments") or ""
            try:
                import json

                parsed = json.loads(arguments)
            except ValueError as exc:
                raise LLMChannelRejected(f"工具参数不是合法 JSON：{_short(arguments)}") from exc
            items = parsed.get("items")
            if not isinstance(items, list):
                raise LLMChannelRejected("工具参数缺少 items 数组")
            return items, arguments

        content = message.get("content") or ""
        if content:
            items = _items_from_json(content)
            if items is not None:
                return items, content
            raise LLMChannelRejected(f"content 不是可解析的 JSON：{_short(content)}")

        raise LLMChannelRejected(
            f"响应既没有 tool_calls 也没有 content（finish_reason={finish}）"
        )


def _items_from_json(text: str) -> list[dict[str, Any]] | None:
    """从文本里取出 ``{"items": [...]}``。

    容忍一层代码围栏：即使 prompt 明确禁止，真实模型仍会偶尔加上 ```json```。
    只在"围栏模式"下剥一层，不做正则搜括号——那样容易把解释文字里的 JSON 误当结果。
    """
    import json

    candidate = text.strip()
    if candidate.startswith("```"):
        lines = candidate.splitlines()
        lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        candidate = "\n".join(lines).strip()

    try:
        parsed = json.loads(candidate)
    except ValueError:
        return None
    if isinstance(parsed, dict):
        items = parsed.get("items")
        if isinstance(items, list):
            return [item for item in items if isinstance(item, dict)]
        return None
    if isinstance(parsed, list):
        return [item for item in parsed if isinstance(item, dict)]
    return None


def build_llm(cfg):
    """按 ``[llm].provider`` 构建适配器。

    不做"自动探测能力后回退"：``provider`` 写的是什么就用什么，
    不支持就明确报错。这样出问题时原因只有一个，不需要猜。

    ``cfg`` 必须是整份 :class:`app.config.Config`（适配器需要它的校验入口）。
    """
    provider = (cfg.llm.provider or "").strip() or "responses"

    if provider == "mock":
        from app.llm.mock import MockLLM

        return MockLLM()

    if provider == "responses":
        return ResponsesLLM(cfg)

    if provider == "chat":
        return ChatCompatLLM(cfg)

    raise LLMNotConfigured(
        f"不支持的 LLM provider：{provider!r}。可选：responses / chat / mock"
    )
