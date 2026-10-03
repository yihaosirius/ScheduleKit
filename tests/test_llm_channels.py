"""LLM 通道编排的测试 —— 用 httpx MockTransport，全程不联网。

这个文件守的是三条最容易出错、也最难在真机上排查的性质：

1. **两套协议的请求形状不能混**。Responses 的 ``tool_choice`` 是扁平的
   ``{"type":"function","name":...}``，Chat 的是嵌套的
   ``{"type":"function","function":{"name":...}}``；照抄另一套会被服务端当未知字段。
   混了之后的表现是"接口 400"，而错误信息不会告诉你形状抄错了。
2. **thinking 与强制工具调用不可兼得**。DeepSeek 在 thinking 模式下拒绝
   具名 ``tool_choice``；所以主通道必须显式关掉思考。
3. **降级必须有界且响亮**。每一条降级路径都要能被触发、能报出原因。
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from app.llm.base import (
    LLMChannelRejected,
    LLMOutputTruncated,
    LLMRequest,
    LLMTransient,
    PATH_JSON_OBJECT,
    PATH_JSON_SCHEMA,
    PATH_TOOL_CALL,
)
from app.llm.chat import ChatCompatLLM
from app.llm.responses import ResponsesLLM
from app.llm.tools import JSON_FALLBACK_SUFFIX


class Cfg:
    """最小配置替身。

    注意它**同时提供** ``llm`` 段与 ``validate_for_llm()``——因为适配器的构造函数
    收的是整份 Config，而 ``extract`` 会调 ``validate_for_llm()``。
    早先这里只提供 llm 段的字段，于是"传错对象"这类错误被这个替身掩盖了
    （真实代码传的是 ``cfg.llm``，调用 ``validate_for_llm`` 时 AttributeError）。
    所以替身的形状必须与真实配置一致，否则它就是bug的掩体。
    """

    def __init__(self, **overrides: Any) -> None:
        self.llm = _LLMSection(**overrides)

    def validate_for_llm(self) -> None:
        if not self.llm.api_key:
            raise _ConfigError("llm.api_key 为空。")

    # 便于测试直接读字段
    def __getattr__(self, name: str) -> Any:
        return getattr(self.llm, name)


class _ConfigError(RuntimeError):
    """对应 app.config.ConfigError。单独定义一个避免测试依赖真实配置类。"""


class _LLMSection:
    def __init__(self, **overrides: Any) -> None:
        self.provider = "responses"
        self.base_url = "https://api.example.com"
        self.model = "test-model"
        self.api_key = "SECRET"
        self.temperature = 1.0
        self.timeout_seconds = 10
        self.max_tokens = 4096
        self.thinking = False
        self.reasoning_effort = "high"
        self.retry_count = 0
        self.retry_backoff_seconds = 0.0
        for key, value in overrides.items():
            setattr(self, key, value)


def make_request(text: str = "写作业", images: list[bytes] | None = None) -> LLMRequest:
    return LLMRequest(
        system="SYSTEM-PROMPT",
        user_text=text,
        images=images or [],
        image_mimes=["image/png"] * len(images or []),
        image_count=len(images or []),
    )


def responses_payload(items: list[dict] | None = None) -> dict:
    return {
        "id": "resp_1",
        "object": "response",
        "status": "completed",
        "model": "test-model",
        "output": [
            {
                "type": "function_call",
                "name": "submit_tasks",
                "arguments": json.dumps({"items": items or [{"title": "写作业"}]}),
            }
        ],
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }


def chat_payload(items: list[dict] | None = None) -> dict:
    return {
        "id": "chat_1",
        "object": "chat.completion",
        "choices": [
            {
                "index": 0,
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "submit_tasks",
                                "arguments": json.dumps({"items": items or [{"title": "写作业"}]}),
                            },
                        }
                    ],
                },
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }


def attach(llm, handler) -> list[httpx.Request]:
    """挂上 MockTransport，返回记录下来的请求列表。"""
    seen: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    llm._client = httpx.AsyncClient(transport=httpx.MockTransport(wrapped))
    return seen


# --------------------------------------------------------------------------- #
# /responses 的请求形状
# --------------------------------------------------------------------------- #
async def test_responses_tool_call_shape() -> None:
    llm = ResponsesLLM(Cfg())
    seen = attach(llm, lambda request: httpx.Response(200, json=responses_payload()))

    result = await llm.extract(make_request())
    assert result.path == PATH_TOOL_CALL
    assert result.items == [{"title": "写作业"}]

    assert len(seen) == 1
    assert seen[0].url.path == "/responses"
    body = json.loads(seen[0].content)

    assert body["model"] == "test-model"
    # Responses 用 instructions 承载 system prompt，不是 messages
    assert body["instructions"] == "SYSTEM-PROMPT"
    assert isinstance(body["input"], list)
    assert body["input"][0]["role"] == "user"

    # 工具定义是**扁平**的
    tool = body["tools"][0]
    assert tool["type"] == "function"
    assert tool["name"] == "submit_tasks"
    assert "function" not in tool, "Responses 的工具定义不该嵌套 function 键"

    # tool_choice 同样是扁平的具名选择
    assert body["tool_choice"] == {"type": "function", "name": "submit_tasks"}

    # 关掉 thinking 才能用具名 tool_choice
    assert body["reasoning"] == {"effort": "none"}
    assert body["max_output_tokens"] == 4096


async def test_responses_carries_images_as_data_urls() -> None:
    llm = ResponsesLLM(Cfg())
    seen = attach(llm, lambda request: httpx.Response(200, json=responses_payload()))

    await llm.extract(make_request(images=[b"\x89PNG-fake"]))
    body = json.loads(seen[0].content)
    content = body["input"][0]["content"]
    kinds = [part["type"] for part in content]
    assert "input_text" in kinds and "input_image" in kinds

    # 用 data URL 内联：模型侧拿不到我们的鉴权，传 URL 就得开匿名图片端点
    image_part = next(part for part in content if part["type"] == "input_image")
    assert image_part["image_url"].startswith("data:image/png;base64,")


async def test_responses_thinking_switches_to_auto() -> None:
    """thinking 下不能用 required/具名 tool_choice，只能 auto。"""
    llm = ResponsesLLM(Cfg(thinking=True))
    seen = attach(llm, lambda request: httpx.Response(200, json=responses_payload()))

    result = await llm.extract(make_request())
    body = json.loads(seen[0].content)
    assert body["tool_choice"] == "auto"
    assert body["reasoning"] == {"effort": "high"}
    assert result.thinking is True


async def test_responses_parses_incomplete_as_truncated() -> None:
    llm = ResponsesLLM(Cfg(thinking=True))
    attach(
        llm,
        lambda request: httpx.Response(
            200,
            json={
                "status": "incomplete",
                "incomplete_details": {"reason": "max_output_tokens"},
                "output": [],
            },
        ),
    )
    with pytest.raises(LLMOutputTruncated) as excinfo:
        await llm.extract(make_request())
    assert "max_output_tokens" in str(excinfo.value)


async def test_responses_parses_failed_status() -> None:
    llm = ResponsesLLM(Cfg(thinking=True))
    attach(
        llm,
        lambda request: httpx.Response(
            200,
            json={"status": "failed", "error": {"code": "server_error", "message": "崩了"}},
        ),
    )
    with pytest.raises(LLMChannelRejected):
        await llm.extract(make_request())


# --------------------------------------------------------------------------- #
# /chat/completions 的请求形状
# --------------------------------------------------------------------------- #
async def test_chat_tool_call_shape() -> None:
    llm = ChatCompatLLM(Cfg())
    seen = attach(llm, lambda request: httpx.Response(200, json=chat_payload()))

    result = await llm.extract(make_request())
    assert result.path == PATH_TOOL_CALL
    assert result.items == [{"title": "写作业"}]

    assert seen[0].url.path == "/chat/completions"
    body = json.loads(seen[0].content)

    assert body["messages"][0] == {"role": "system", "content": "SYSTEM-PROMPT"}
    # 工具定义是**嵌套**的
    tool = body["tools"][0]
    assert tool["type"] == "function"
    assert tool["function"]["name"] == "submit_tasks"
    assert tool["function"]["strict"] is True

    assert body["tool_choice"] == {
        "type": "function",
        "function": {"name": "submit_tasks"},
    }
    # 显式关思考（这是 DeepSeek 上用强制工具调用的前提）
    assert body["thinking"] == {"type": "disabled"}
    assert body["max_tokens"] == 4096


async def test_chat_thinking_uses_auto_and_never_named() -> None:
    llm = ChatCompatLLM(Cfg(thinking=True))
    seen = attach(llm, lambda request: httpx.Response(200, json=chat_payload()))

    await llm.extract(make_request())
    body = json.loads(seen[0].content)
    assert body["tool_choice"] == "auto"
    assert body["thinking"] == {"type": "enabled"}
    assert body["reasoning_effort"] == "high"


async def test_chat_parses_content_json_when_no_tool_call() -> None:
    """有些供应商/模型会把 JSON 放在 content 里，即使提到了工具。"""
    llm = ChatCompatLLM(Cfg())
    attach(
        llm,
        lambda request: httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": json.dumps({"items": [{"title": "从 content 来"}]}),
                        },
                    }
                ]
            },
        ),
    )
    result = await llm.extract(make_request())
    assert result.items == [{"title": "从 content 来"}]


async def test_chat_parses_fenced_json() -> None:
    """模型偶尔还是会给代码围栏，要能吃下（但只在首尾是围栏时剥一层）。"""
    llm = ChatCompatLLM(Cfg())
    fenced = "```json\n" + json.dumps({"items": [{"title": "带围栏"}]}) + "\n```"
    attach(
        llm,
        lambda request: httpx.Response(
            200,
            json={"choices": [{"finish_reason": "stop", "message": {"content": fenced}}]},
        ),
    )
    result = await llm.extract(make_request())
    assert result.items == [{"title": "带围栏"}]


async def test_chat_length_finish_is_truncated() -> None:
    llm = ChatCompatLLM(Cfg(thinking=True))
    attach(
        llm,
        lambda request: httpx.Response(
            200, json={"choices": [{"finish_reason": "length", "message": {"content": ""}}]}
        ),
    )
    with pytest.raises(LLMOutputTruncated):
        await llm.extract(make_request())


async def test_chat_no_content_no_tool_calls_rejects_channel() -> None:
    llm = ChatCompatLLM(Cfg(thinking=True))
    attach(
        llm,
        lambda request: httpx.Response(
            200, json={"choices": [{"finish_reason": "stop", "message": {"content": ""}}]}
        ),
    )
    with pytest.raises(LLMChannelRejected):
        await llm.extract(make_request())


# --------------------------------------------------------------------------- #
# 降级链
# --------------------------------------------------------------------------- #
async def test_falls_back_from_tool_call_to_json_schema() -> None:
    """主通道被拒（400）→ 降到 json_schema，且原因要报出来。"""
    llm = ResponsesLLM(Cfg())
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if "tools" in body:
            calls.append("tool")
            return httpx.Response(400, json={"error": {"message": "tool_choice not supported"}})
        calls.append("json_schema")
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {
                                "type": "output_text",
                                "text": json.dumps({"items": [{"title": "降级来的"}]}),
                            }
                        ],
                    }
                ],
            },
        )

    attach(llm, handler)
    result = await llm.extract(make_request())

    assert calls == ["tool", "json_schema"]
    assert result.path == PATH_JSON_SCHEMA
    assert result.degraded is True
    assert result.fallback_note and "不可用" in result.fallback_note
    assert result.items == [{"title": "降级来的"}]


async def test_falls_back_all_the_way_to_json_object() -> None:
    llm = ResponsesLLM(Cfg())
    shapes: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if "tools" in body:
            shapes.append("tool")
            return httpx.Response(400)
        fmt = (body.get("text") or {}).get("format", {})
        if fmt.get("type") == "json_schema":
            shapes.append("json_schema")
            return httpx.Response(422)
        shapes.append("json_object")
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {"type": "output_text", "text": json.dumps({"items": [{"title": "最后一条"}]})}
                        ],
                    }
                ],
            },
        )

    attach(llm, handler)
    result = await llm.extract(make_request())

    assert shapes == ["tool", "json_schema", "json_object"]
    assert result.path == PATH_JSON_OBJECT
    assert result.degraded is True


async def test_degraded_channel_prompt_overrides_tool_instruction() -> None:
    """降级时必须把"改为输出 JSON"的指令追加进去，覆盖前面的工具指令。

    漏了这一步，模型会继续尝试调工具，而我们这一轮没给它工具。
    """
    llm = ChatCompatLLM(Cfg())
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body["messages"][0]["content"])
        if body.get("tools"):
            return httpx.Response(400)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": json.dumps({"items": [{"title": "x"}]})},
                    }
                ]
            },
        )

    attach(llm, handler)
    await llm.extract(make_request())

    assert len(seen) == 2
    assert JSON_FALLBACK_SUFFIX.strip() in seen[1]
    assert "【输出格式变更】" in seen[1]


# --------------------------------------------------------------------------- #
# 重试
# --------------------------------------------------------------------------- #
async def test_transient_status_is_retried() -> None:
    llm = ResponsesLLM(Cfg(retry_count=2))
    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        if len(attempts) < 3:
            return httpx.Response(503, text="upstream busy")
        return httpx.Response(200, json=responses_payload())

    attach(llm, handler)
    result = await llm.extract(make_request())

    assert len(attempts) == 3
    assert result.attempts == 3
    assert result.path == PATH_TOOL_CALL


async def test_429_is_retried() -> None:
    llm = ResponsesLLM(Cfg(retry_count=1))
    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        if len(attempts) == 1:
            return httpx.Response(429, headers={"Retry-After": "0"}, text="slow down")
        return httpx.Response(200, json=responses_payload())

    attach(llm, handler)
    assert (await llm.extract(make_request())).attempts == 2


async def test_400_is_not_retried() -> None:
    """确定性错误重试没有意义，只会把真正的原因埋起来。"""
    llm = ResponsesLLM(Cfg(retry_count=3))
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append("tools" in body and "tool" or "json")
        return httpx.Response(400, json={"error": "bad shape"})

    attach(llm, handler)
    with pytest.raises(LLMChannelRejected):
        # 三条通道各试一次，每条都不重试
        await llm.extract(make_request())
    assert calls == ["tool", "json", "json"], "400 不该被重试"


async def test_401_is_fatal_not_channel_rejection() -> None:
    """Key 不对是所有通道都失败，不该被当成"换个形状再试"。"""
    llm = ResponsesLLM(Cfg(retry_count=2))
    attach(llm, lambda request: httpx.Response(401, json={"error": "invalid api key"}))
    with pytest.raises(Exception) as excinfo:
        await llm.extract(make_request())
    assert not isinstance(excinfo.value, LLMChannelRejected)


async def test_network_error_is_transient() -> None:
    llm = ResponsesLLM(Cfg(retry_count=1))

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("连不上", request=request)

    attach(llm, handler)
    with pytest.raises(Exception) as excinfo:
        await llm.extract(make_request())
    # 所有通道都因临时错误失败 → 最终抛出临时错误（可重试语义）
    assert "临时" in str(excinfo.value) or isinstance(excinfo.value, LLMTransient)


async def test_malformed_tool_arguments_reject_channel() -> None:
    llm = ResponsesLLM(Cfg())
    attach(
        llm,
        lambda request: httpx.Response(
            200,
            json={
                "status": "completed",
                "output": [
                    {"type": "function_call", "name": "submit_tasks", "arguments": "{不是 json"}
                ],
            },
        ),
    )
    with pytest.raises(LLMChannelRejected):
        await llm.extract(make_request())


async def test_other_tool_name_is_ignored_not_accepted() -> None:
    """模型调了别的工具（甚至幻觉出来的）不能当成功。"""
    llm = ResponsesLLM(Cfg())
    attach(
        llm,
        lambda request: httpx.Response(
            200,
            json={
                "status": "completed",
                "output": [
                    {"type": "function_call", "name": "do_something_else", "arguments": "{}"}
                ],
            },
        ),
    )
    with pytest.raises(LLMChannelRejected):
        await llm.extract(make_request())


# --------------------------------------------------------------------------- #
# registry
# --------------------------------------------------------------------------- #
def test_registry_builds_by_provider() -> None:
    from app.config import Config, LLMConfig, ServerConfig
    from app.llm.mock import MockLLM
    from app.llm.registry import build_llm

    def cfg(provider: str):
        return Config(
            path=__import__("pathlib").Path("x.toml"),
            server=ServerConfig(),
            llm=LLMConfig(provider=provider),
        )

    assert isinstance(build_llm(cfg("responses")), ResponsesLLM)
    assert isinstance(build_llm(cfg("chat")), ChatCompatLLM)
    assert isinstance(build_llm(cfg("mock")), MockLLM)


async def test_registry_adapters_can_validate_configuration() -> None:
    """真实适配器必须能调到 ``validate_for_llm``。

    这条测试存在的唯一原因：曾经 ``build_llm`` 把 ``cfg.llm``（段）传给了适配器，
    而适配器在 ``extract`` 里调 ``validate_for_llm()``——AttributeError。
    测试全绿，因为测试用的 provider 是 mock，而 mock 不校验配置。
    真机上表现为**上传直接 500**。

    所以这里对**真实适配器**也跑一次 extract（缺 Key，会在发请求之前就失败），
    确认报的是配置错误而不是 AttributeError。
    """
    from pathlib import Path

    from app.config import Config, ConfigError, LLMConfig, ServerConfig
    from app.llm.registry import build_llm

    for provider in ("responses", "chat"):
        config = Config(
            path=Path("x.toml"),
            server=ServerConfig(),
            llm=LLMConfig(provider=provider, api_key=""),
        )
        llm = build_llm(config)
        with pytest.raises(ConfigError):
            await llm.extract(LLMRequest(system="s", user_text="t"))
        await llm.aclose()


def test_registry_rejects_unknown_provider() -> None:
    from pathlib import Path

    from app.config import Config, LLMConfig, ServerConfig
    from app.llm.base import LLMNotConfigured
    from app.llm.registry import build_llm

    config = Config(path=Path("x.toml"), server=ServerConfig(), llm=LLMConfig(provider="gemini"))
    with pytest.raises(LLMNotConfigured):
        build_llm(config)


async def test_aclose_is_safe_without_client() -> None:
    """连接池还没建就关闭不该报错（比如配置错误时提前返回）。"""
    llm = ResponsesLLM(Cfg())
    await llm.aclose()
    await llm.aclose()


# --------------------------------------------------------------------------- #
# mock 适配器
# --------------------------------------------------------------------------- #
async def test_mock_is_deterministic_and_offline() -> None:
    from app.llm.mock import MockLLM

    llm = MockLLM()
    request = make_request("下周一交第三章习题。明天背单词")
    first = await llm.extract(request)
    second = await llm.extract(request)

    assert first.items == second.items
    assert len(first.items) == 2
    assert first.path == PATH_TOOL_CALL


async def test_mock_switches_to_json_when_prompt_says_so() -> None:
    """mock 要"像真模型一样"尊重降级指令，否则编排逻辑测不到真实路径。"""
    from app.llm.mock import MockLLM

    llm = MockLLM()
    request = LLMRequest(
        system="SYSTEM" + JSON_FALLBACK_SUFFIX,
        user_text="写作业",
    )
    result = await llm.extract(request)
    assert result.path == PATH_JSON_OBJECT


async def test_mock_failure_modes() -> None:
    from app.llm.mock import MODE_HTTP_400, MODE_TRUNCATED, MockLLM

    with pytest.raises(LLMOutputTruncated):
        await MockLLM(MODE_TRUNCATED).extract(make_request())

    with pytest.raises(LLMChannelRejected):
        await MockLLM(MODE_HTTP_400).extract(make_request())


async def test_mock_extracts_placeholder_relative_dates() -> None:
    """mock 输出的相对天数占位符由 normalize 解析，这里只确认它确实输出了占位符。"""
    from app.llm.mock import MockLLM

    result = await MockLLM().extract(make_request("明天交数学作业"))
    assert result.items[0]["due_at"].startswith("__RELATIVE_DAYS__")
