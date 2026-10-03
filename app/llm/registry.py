"""按配置解析出 LLM 适配器。

实现放在 :mod:`app.llm.structured`（两个协议共用骨架），这里只做名字到类的映射，
让"支持哪些 provider"这件事只有一个落点。

============================ ============================================
``responses``                ``{base_url}/responses``，OpenAI Responses 协议
``chat``                     ``{base_url}/chat/completions``，Chat 兼容协议
``mock``                     离线测试，不访问网络
============================ ============================================

不做"自动探测能力后回退"：``provider`` 写的是什么就用什么，不支持就明确报错。
这样出问题时原因只有一个，不需要猜。
"""

from __future__ import annotations

from app.llm.base import LLMNotConfigured, StructuredLLM  # noqa: F401  (re-export)
from app.llm.structured import ChatCompatLLM, ResponsesLLM, build_llm

SUPPORTED_PROVIDERS = ("responses", "chat", "mock")

DEFAULT_PROVIDER = "responses"

__all__ = [
    "ChatCompatLLM",
    "ResponsesLLM",
    "SUPPORTED_PROVIDERS",
    "DEFAULT_PROVIDER",
    "build_llm",
]
