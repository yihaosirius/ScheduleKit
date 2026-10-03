"""统一日志与 trace id。

全项目**只此一套**日志（AGENTS.md §1.3）。所有模块用 ``get_logger(name)``，
不要出现裸 ``print()``。

格式::

    2026-10-03T18:32:01.123 INFO  sk.http          [t=8f3a2b1c] request.end status=201 elapsed_ms=4697

两条硬规则：

1. **同一个请求共享 `[t=xxxxxxxx]`**。中间件注入，并回写响应头 ``X-Trace-Id``，
   这样客户端报错时能直接把服务端日志捞出来。
2. **密钥不进日志**。``kv()`` 自动屏蔽字段名含 key/token/password/secret/cookie
   的键；其余长值用 ``mask()`` 截断。
"""

from __future__ import annotations

import contextvars
import json
import logging
import re
import secrets
import sys
from typing import Any

#: 当前请求的 trace id。用 contextvars 而不是全局变量：asyncio 下并发请求
#: 会互相覆盖，而 contextvars 天然按任务隔离。
_trace_id: contextvars.ContextVar[str] = contextvars.ContextVar("sk_trace_id", default="")

#: 字段名含这些子串时，值一律替换成 ***。
_SENSITIVE = re.compile(r"key|token|password|secret|cookie|authorization", re.IGNORECASE)

#: 单条日志里单个值的最大长度。超长值（如整段模型输出）会刷屏，
#: 截断并标注原始长度，需要全文时去库里取。
MAX_VALUE_CHARS = 300

_LOGGER_ROOT = "sk"


# --------------------------------------------------------------------------- #
# trace id
# --------------------------------------------------------------------------- #
def new_trace_id() -> str:
    return secrets.token_hex(4)


def set_trace_id(trace_id: str) -> contextvars.Token[str]:
    return _trace_id.set(trace_id)


def reset_trace_id(token: contextvars.Token[str]) -> None:
    _trace_id.reset(token)


def current_trace_id() -> str:
    return _trace_id.get()


# --------------------------------------------------------------------------- #
# 取值处理
# --------------------------------------------------------------------------- #
def mask(value: Any) -> str:
    """把任意值变成可安全写日志的短字符串。"""
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, bytes):
        return f"<bytes len={len(value)}>"
    text = str(value)
    text = text.replace("\n", "\\n").replace("\r", "").replace("\t", "\\t")
    if len(text) > MAX_VALUE_CHARS:
        return f"{text[:MAX_VALUE_CHARS]}…(+{len(text) - MAX_VALUE_CHARS})"
    return text


def kv(**fields: Any) -> str:
    """把字段渲染成 ``k=v k=v``。

    值里带空格或为空时用 JSON 引号包起来，保证 ``grep 'a=b'`` 不会误命中。
    敏感字段名自动打码——**这是唯一的口径**，不要在各模块自己判断。
    """
    parts: list[str] = []
    for name, value in fields.items():
        if _SENSITIVE.search(name):
            shown = "***" if value else "-"
        else:
            shown = mask(value)
        if shown == "" or " " in shown or '"' in shown or "=" in shown:
            parts.append(f"{name}={json.dumps(shown, ensure_ascii=False)}")
        else:
            parts.append(f"{name}={shown}")
    return " ".join(parts)


# --------------------------------------------------------------------------- #
# handler / formatter
# --------------------------------------------------------------------------- #
class _TraceFormatter(logging.Formatter):
    """带 trace id 的格式化器。

    不用 ``%(trace_id)s`` 的 Filter 方案：Filter 在 handler 上挂一次就要求
    每条日志都经过它，而我们要的是"没有请求上下文时也能打日志"。
    """

    def format(self, record: logging.LogRecord) -> str:
        tid = current_trace_id()
        record.trace_field = f"[t={tid}]" if tid else "[t=--------]"
        return super().format(record)


def setup_logging(level: str = "INFO") -> None:
    """装配日志。幂等：重复调用不会叠加 handler。"""
    root = logging.getLogger(_LOGGER_ROOT)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    root.propagate = False

    # uvicorn 自己的 logger 也收进来，避免两套时间格式
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logging.getLogger(name).handlers.clear()
        logging.getLogger(name).propagate = True

    if not root.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(
            _TraceFormatter(
                fmt="%(asctime)s %(levelname)-5s %(name)-16s %(trace_field)s %(message)s",
                datefmt="%Y-%m-%dT%H:%M:%S",
            )
        )
        root.addHandler(handler)


def get_logger(name: str) -> logging.Logger:
    """``get_logger("ingest")`` → ``sk.ingest``。"""
    return logging.getLogger(f"{_LOGGER_ROOT}.{name}")
