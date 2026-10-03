"""LLM 适配层。

对外只暴露一件事：``build_llm(config) -> StructuredLLM``。

模块分工：

* :mod:`app.llm.base`       —— 接口、错误分级、结果类型
* :mod:`app.llm.tools`      —— ``submit_tasks`` 的 schema 与三套协议形状（唯一真相）
* :mod:`app.llm.structured` —— 两个协议的传输层 + 重试 + 三通道降级
* :mod:`app.llm.registry`   —— provider 名 → 适配器
* :mod:`app.llm.mock`       —— 离线确定性抽取器（测试用）
"""

from app.llm.registry import SUPPORTED_PROVIDERS, build_llm

__all__ = ["build_llm", "SUPPORTED_PROVIDERS"]
