"""``/chat/completions`` 适配器。

实现放在 :mod:`app.llm.structured`（与 responses 共用重试与降级骨架），
这里只做一层具名再导出，让 ``from app.llm.chat import ChatCompatLLM``
这样的导入路径与协议名对应，读代码时不用先去 structured 里找。

**协议形状要点**（改之前先看 ``tests/test_llm_channels.py``）：
工具定义嵌在 ``function`` 里，``tool_choice`` 也是嵌套的；thinking 必须显式关掉
才能用具名 ``tool_choice``，否则服务端返回 400。
"""

from app.llm.structured import ChatCompatLLM

__all__ = ["ChatCompatLLM"]
