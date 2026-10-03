"""``/responses`` 适配器。

实现放在 :mod:`app.llm.structured`（与 chat 共用重试与降级骨架），
这里只做一层具名再导出。

**协议形状要点**（改之前先看 ``tests/test_llm_channels.py``）：
工具定义与 ``tool_choice`` 都是**扁平**的（没有嵌套的 ``function`` 键），
system prompt 走 ``instructions``，思考开关是 ``reasoning.effort``。
照抄 chat 的形状会被服务端当未知字段——而错误信息不会告诉你抄错了。
"""

from app.llm.structured import ResponsesLLM

__all__ = ["ResponsesLLM"]
