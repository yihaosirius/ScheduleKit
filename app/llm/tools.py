"""``submit_tasks`` 的字段定义 —— 全系统唯一真相，外加三套协议形状。

约束设计：

* ``required`` 只有 4 个核心字段。``notes`` / ``source_quote`` 选填，
  没有内容时模型就不输出这个键，避免为了填满 schema 而吐空字符串。
* **没有 ``confidence``**：LLM 自评置信度校准极差（几乎总是 0.85+），
  提供不了判别力，反而会让人误以为"低置信度的那些我们变谨慎了"。
* ``additionalProperties: false`` + 严格 ``enum``，让约束解码器替我们挡住
  大部分格式漂移；**语义规则（二选一）仍由服务端强制**——约束解码只能保证
  类型与取值合法，保证不了"due_at 与 priority 恰好一个非空"。

同一个 schema 要喂给三种不同的请求形状，这是它们唯一的差别：

============ ==========================================================
形状          用途
============ ==========================================================
``chat``     ``/chat/completions``：工具定义嵌在 ``function`` 里，
             ``tool_choice`` 也是嵌套的
``responses`` ``/responses``：工具定义是**扁平**的，``tool_choice``
             同样扁平 —— 照抄 chat 的形状会被服务端当成未知字段
``json``     降级通道：不用工具，改用 ``response_format`` /
             ``text.format`` 强制 JSON
============ ==========================================================
"""

from __future__ import annotations

import copy
import json
from typing import Any

TOOL_NAME = "submit_tasks"

CATEGORY_ENUM = ["homework", "practice", "exam", "appointment", "other"]

MAX_ITEMS = 20
TITLE_MAX = 20
NOTES_MAX = 60
QUOTE_MAX = 20

TOOL_DESCRIPTION = "提交从图片或文本中抽取出的全部事项。必须调用本工具，不要输出解释性文字。"

#: 工具参数。两边都必须 **深拷贝** 后再放进请求体，避免任何一方就地改写它。
SUBMIT_TASKS_PARAMS: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["items"],
    "properties": {
        "items": {
            "type": "array",
            "maxItems": MAX_ITEMS,
            "description": "抽取出的事项；没有识别到任何事项时返回空数组",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["title", "category", "due_at", "priority"],
                "properties": {
                    "title": {
                        "type": "string",
                        "description": f"≤{TITLE_MAX}字，不含时间、地点、课程名（已由其它字段承载）",
                    },
                    "category": {
                        "type": "string",
                        "enum": CATEGORY_ENUM,
                        "description": "作业 / 练习 / 考试 / 要约 / 其他",
                    },
                    "due_at": {
                        "type": ["string", "null"],
                        "description": "ISO8601 且带时区偏移；没有明确截止时间时为 null",
                    },
                    "priority": {
                        "type": ["integer", "null"],
                        "minimum": 1,
                        "maximum": 5,
                        "description": "仅在 due_at 为 null 时给出：1=Ⅰ 最紧急 … 5=Ⅴ 最不紧急",
                    },
                    "notes": {
                        "type": "string",
                        "description": f"≤{NOTES_MAX}字；没有额外信息就不要输出这个键",
                    },
                    "source_quote": {
                        "type": "string",
                        "description": f"≤{QUOTE_MAX}字，图片或文本中支撑该判定的原文片段",
                    },
                },
            },
        }
    },
}


def _params() -> dict[str, Any]:
    return copy.deepcopy(SUBMIT_TASKS_PARAMS)


# --------------------------------------------------------------------------- #
# 形状一：/chat/completions（嵌套 function）
# --------------------------------------------------------------------------- #
def chat_tool() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": TOOL_NAME,
            "description": TOOL_DESCRIPTION,
            "parameters": _params(),
            # strict=True 是 Beta 能力：让服务端按 schema 约束解码工具参数。
            # 这是主通道价值最大的一个开关，少了它格式漂移全靠后处理兜。
            "strict": True,
        },
    }


def chat_forced_tool_choice() -> dict[str, Any]:
    """强制模型调用 ``submit_tasks``（嵌套形状）。"""
    return {"type": "function", "function": {"name": TOOL_NAME}}


# --------------------------------------------------------------------------- #
# 形状二：/responses（扁平）
# --------------------------------------------------------------------------- #
def responses_tool() -> dict[str, Any]:
    return {
        "type": "function",
        "name": TOOL_NAME,
        "description": TOOL_DESCRIPTION,
        "parameters": _params(),
    }


def responses_forced_tool_choice() -> dict[str, Any]:
    """强制模型调用 ``submit_tasks``（扁平形状）。

    注意这里**没有**嵌套的 ``function`` 键 —— Responses API 的具名工具选择是
    ``{"type": "function", "name": "..."}``。写成 chat 那种嵌套形状，
    服务端认不出来（这是两个协议最容易混淆的一处）。
    """
    return {"type": "function", "name": TOOL_NAME}


# --------------------------------------------------------------------------- #
# 形状三：JSON 降级通道
# --------------------------------------------------------------------------- #
#: 降级时给模型看的示例。刻意覆盖两种情形（有 deadline / 有 priority），
#: 因为"二选一"是这套 schema 里最容易搞错的地方。
JSON_FALLBACK_EXAMPLE: dict[str, Any] = {
    "items": [
        {
            "title": "第三章习题",
            "category": "homework",
            "due_at": "2026-09-15T23:59:00+08:00",
            "priority": None,
            "notes": "只做奇数题",
            "source_quote": "下周一交",
        },
        {
            "title": "背单词",
            "category": "practice",
            "due_at": None,
            "priority": 3,
            "notes": "",
            "source_quote": "有空就做",
        },
    ]
}

#: 追加到 system prompt 后面，用于把"必须调用工具"改写成"必须输出 JSON"。
#:
#: 三个约束都不是装饰，各有明确原因：
#:
#: 1. 必须出现 ``json`` 这个词并给出格式示例 —— DeepSeek 的 JSON Output 文档
#:    把它列为启用条件。缺了它模型可能一直吐空白直到撞满 max_tokens
#:    （表现为"请求卡了很久然后失败"）。
#: 2. 必须显式覆盖前半段"必须调用工具"的指令，否则两段 prompt 互相打架，
#:    模型可能仍然尝试调工具，而这一轮我们没给它工具。
#: 3. 明确禁止代码围栏 —— ```json ... ``` 会让 ``json.loads`` 直接失败。
JSON_FALLBACK_SUFFIX = (
    "\n\n---\n"
    "【输出格式变更】上面的工具调用指令作废：本轮**不要调用任何工具**，"
    "改为直接输出一个 json 对象。\n"
    '顶层结构为 {"items": [...]}，数组每个元素的字段与取值：\n'
    "  title         字符串，必填，≤20字，不含时间地点\n"
    f"  category      字符串，必填，取值之一：{', '.join(CATEGORY_ENUM)}\n"
    "  due_at        字符串或 null，必填（可为 null）。ISO8601 带时区偏移\n"
    "  priority      整数或 null，必填（可为 null）。1=Ⅰ 最紧急 … 5=Ⅴ 最不紧急\n"
    "  notes         字符串，选填，≤60字\n"
    "  source_quote  字符串，选填，≤20字，支撑判定的原文片段\n"
    "硬性规则：due_at 与 priority **必须且只能有一个非 null**；"
    "有明确截止时间就给 due_at、priority 填 null，没有截止时间才给 priority。\n"
    f"最多 {MAX_ITEMS} 条；没有识别到任何事项时输出 {{\"items\": []}}。\n"
    "示例 json：\n"
    + json.dumps(JSON_FALLBACK_EXAMPLE, ensure_ascii=False, indent=2)
    + "\n只输出这个 json 对象本身，前后不要有任何解释文字，也不要用 ``` 代码围栏包裹。\n"
)


def json_object_format() -> dict[str, Any]:
    """``response_format``（chat）的 JSON 模式取值。"""
    return {"type": "json_object"}


def json_schema_text_format() -> dict[str, Any]:
    """``text.format``（responses）的**结构化输出**取值。

    Responses 协议支持 ``json_schema``，比 ``json_object`` 强得多：约束解码会
    保证结构正确。所以降级次序是 ``json_schema`` 优先，``json_object`` 兜底。

    schema 这里与工具参数**刻意不同**：把 ``notes`` / ``source_quote`` 也放进
    ``required``（值可为空字符串）。原因是结构化输出对 nullable/联合类型的支持
    在各方实现里不一致，"全字段必填、用空串表示没有"是兼容性最好的写法，
    真正的裁剪交给 :mod:`app.services.normalize`。
    """
    return {
        "type": "json_schema",
        "name": "submit_tasks",
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["items"],
            "properties": {
                "items": {
                    "type": "array",
                    "maxItems": MAX_ITEMS,
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": [
                            "title",
                            "category",
                            "due_at",
                            "priority",
                            "notes",
                            "source_quote",
                        ],
                        "properties": {
                            "title": {"type": "string"},
                            "category": {"type": "string", "enum": CATEGORY_ENUM},
                            "due_at": {"type": ["string", "null"]},
                            "priority": {"type": ["integer", "null"], "minimum": 1, "maximum": 5},
                            "notes": {"type": "string"},
                            "source_quote": {"type": "string"},
                        },
                    },
                }
            },
        },
    }
