"""文本清洗与长度裁剪。

模型输出与用户输入都要过这里。清洗规则**不改变语义**，只做这三件事：

1. 去掉控制字符与零宽字符（视觉模型偶尔会吐 ``\\u200b``、``\\x00``）。
2. 把各种空白折叠成单个空格，去掉首尾空白。
3. 按**可见宽度**截断，而不是 Python 的 ``len()``。

第 3 点容易踩：``len("你好") == 2``，但"20 字"在中文语境下指的是 20 个字符，
不是 20 个字节也不是 20 个显示列。所以统一按**字符数**（``len``）算，
但对组合字符做一次 NFC 归一化，避免"é"被算成两个字符。
"""

from __future__ import annotations

import re
import unicodedata

#: 控制字符（保留 \n \t 交给空白折叠处理）与零宽字符
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_ZERO_WIDTH = re.compile(r"[\u200b-\u200f\u202a-\u202e\ufeff]")
_WHITESPACE = re.compile(r"[ \t\r\n\u3000]+")


def clean_text(value: object, *, limit: int | None = None, allow_empty: bool = True) -> str:
    """清洗并（可选）截断。返回的字符串保证不含控制/零宽字符。"""
    text = "" if value is None else str(value)
    text = unicodedata.normalize("NFC", text)
    text = _CONTROL.sub("", text)
    text = _ZERO_WIDTH.sub("", text)
    text = _WHITESPACE.sub(" ", text).strip()
    if limit is not None and len(text) > limit:
        text = text[:limit].rstrip()
    if not text and not allow_empty:
        raise ValueError("文本为空")
    return text


def clean_multiline(value: object, *, limit: int | None = None) -> str:
    """保留换行的清洗（记忆内容、系统提示词这类需要段落的地方）。"""
    text = "" if value is None else str(value)
    text = unicodedata.normalize("NFC", text)
    text = _CONTROL.sub("", text)
    text = _ZERO_WIDTH.sub("", text)
    lines = [re.sub(r"[ \t\u3000]+", " ", line).strip() for line in text.splitlines()]
    text = "\n".join(line for line in lines if line)
    if limit is not None and len(text) > limit:
        text = text[:limit]
    return text


def truncated(value: str, limit: int) -> tuple[str, bool]:
    """返回 ``(截断后的文本, 是否发生了截断)``。

    调用方拿到 ``True`` 时必须打点或记进 overrides——"内容被我们截掉了"
    是用户必须能发现的事，否则会出现"我明明写了却被吞了"。
    """
    if len(value) <= limit:
        return value, False
    return value[:limit], True


def contains_cjk(value: str) -> bool:
    """粗略判断是否含中日韩字符。用于给"多少字"一个更贴近直觉的提示。"""
    return any("\u4e00" <= ch <= "\u9fff" or "\u3040" <= ch <= "\u30ff" for ch in value)
