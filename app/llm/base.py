"""LLM 适配器的统一接口。

全系统只有一种主结构化机制：**强制 function calling**。不做能力探测、
不做多档回退——供应商不支持强制 ``tool_choice`` 就是配置错误，控制台给出明确报错，
而不是悄悄退化成"解析模型自由文本"。

**唯一例外**是一条有界的降级通道，且它必须**响亮**：写 WARNING 日志、
把通道记进草稿的 ``llm_path``、在确认页上标出来。

为什么必须有这条例外（两条都是实测/文档确认的，不是想当然）：

1. DeepSeek 在 **thinking 模式下拒绝** ``tool_choice`` 传 ``required`` 或具名工具，
   直接返回 400。也就是说"强制工具调用"与"开思考"目前不可兼得。
2. "模型没产出 function call"是一个真实的、无法事前探测的失败模式：
   响应 200 但内容里没有工具调用。

所以允许降级，但降级不等于静默。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

#: ``LLMResult.path`` 的取值，与 ``ingest_drafts.llm_path`` 的 CHECK 一致。
PATH_TOOL_CALL = "tool_call"
PATH_JSON_SCHEMA = "json_schema"
PATH_JSON_OBJECT = "json_object"

ALL_PATHS = (PATH_TOOL_CALL, PATH_JSON_SCHEMA, PATH_JSON_OBJECT)


class LLMError(RuntimeError):
    """LLM 调用失败的基类。"""


class LLMNotConfigured(LLMError):
    """配置缺失或供应商不受支持。"""


class LLMRequestFailed(LLMError):
    """请求层面失败：网络、超时、HTTP 错误码。"""


class LLMOutputTruncated(LLMError):
    """输出被 ``max_tokens`` 截断，JSON 必然不完整。

    单独一个类型是因为它和"模型乱吐格式"是完全不同的病：
    前者调大 ``max_tokens`` 就好，后者要改 prompt。混在一起会把排查方向指错。
    """


class LLMTransient(LLMError):
    """临时性失败：连接、超时、429、5xx。

    可以安全重试——我们的请求是单发无副作用的（服务端不保存任何东西），
    重试不会造成重复写入。
    """

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class LLMChannelRejected(LLMError):
    """当前通道拿不到结构化输出 —— 换下一条通道，重试没有意义。

    两种触发情形：供应商明确拒绝该形状（HTTP 400/422），
    或者 200 响应里根本没有可用的结构化内容。
    """


class LLMToolCallMissing(LLMError):
    """所有通道都没拿到可用的结构化输出。"""


class LLMAllChannelsFailed(LLMToolCallMissing, LLMChannelRejected):
    """三条通道**全部**因形状/内容原因被拒。

    为什么让它同时是 :class:`LLMChannelRejected` 的子类：调用方（以及测试）
    关心的是"这是**拒绝**还是**临时故障**"——前者重试没有意义，后者可以。
    "全部失败"在语义上属于前者，所以必须能通过 ``except LLMChannelRejected``
    捕获到。否则调用方只能拿到一个笼统的 LLMToolCallMissing，
    分不清"该改 prompt"还是"该重试"。

    类名与消息都会带上每一步的原因（``notes``），这才是排查的入口。
    """

    def __init__(self, message: str, *, notes: list[str] | None = None) -> None:
        super().__init__(message)
        self.notes = notes or []


@dataclass
class LLMResult:
    """一次抽取的结果。"""

    #: 已解析并做过结构校验的 items（语义校验在 services/normalize.py）
    items: list[dict[str, Any]]
    #: 原始输出文本，落库供排查
    raw: str
    provider: str
    model: str
    elapsed_ms: float
    usage: dict[str, Any] = field(default_factory=dict)
    #: 走的是哪条通道：tool_call / json_schema / json_object
    path: str = PATH_TOOL_CALL
    #: 降级原因（仅在降级时有值），用于日志与确认页
    fallback_note: str | None = None
    #: 实际发出的尝试次数（含重试），便于判断供应商稳不稳定
    attempts: int = 1
    #: thinking 标记：本次是否开启了思考模式
    thinking: bool = False

    @property
    def degraded(self) -> bool:
        return self.path != PATH_TOOL_CALL


@dataclass(frozen=True)
class LLMRequest:
    """一次抽取请求。"""

    system: str
    user_text: str
    #: 已按顺序排好的图片（data URL 或原始字节，由适配器决定怎么塞）
    images: list[bytes] = field(default_factory=list)
    image_mimes: list[str] = field(default_factory=list)
    #: 供日志与落库用
    image_count: int = 0


@runtime_checkable
class StructuredLLM(Protocol):
    """所有适配器实现这一个方法。"""

    name: str

    async def extract(self, request: LLMRequest) -> LLMResult:
        """从文本与（可选的）图片中抽取结构化事项。"""
        ...

    async def aclose(self) -> None:
        """释放底层连接池。"""
        ...
