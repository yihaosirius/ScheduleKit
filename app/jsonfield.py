"""JSON 文本字段的编解码。

库里有几个字段用 JSON 数组文本存（``items.memory_ids``、``ingest_drafts.items_json``、
``ingest_drafts.memory_ids``）。集中在这里处理的原因是：**读库时不能信任内容**。

如果直接 ``json.loads(row["memory_ids"])``，一条脏数据（比如手工改库写了个空串）
会让整个列表接口 500，而前端只会看到"加载失败"。所以这里一律"读不出来就当空"，
并写 warning 日志——出问题的应该是那一条数据，而不是整个应用。
"""

from __future__ import annotations

import json
from typing import Any

from app.logging import get_logger, kv

log = get_logger("jsonfield")


def loads_list(raw: Any) -> list[Any]:
    if raw is None or raw == "":
        return []
    if isinstance(raw, list):
        return raw
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        log.warning("jsonfield.malformed %s", kv(value=str(raw)[:60]))
        return []
    if not isinstance(parsed, list):
        log.warning("jsonfield.not_a_list %s", kv(value=str(raw)[:60], type=type(parsed).__name__))
        return []
    return parsed


def loads_int_list(raw: Any) -> list[int]:
    """id 列表。非整数元素被丢弃而不是让整个调用失败。"""
    result: list[int] = []
    for item in loads_list(raw):
        try:
            result.append(int(item))
        except (TypeError, ValueError):
            log.warning("jsonfield.bad_int %s", kv(value=repr(item)[:40]))
    return result


def loads_dict_list(raw: Any) -> list[dict[str, Any]]:
    return [item for item in loads_list(raw) if isinstance(item, dict)]


def dumps(value: Any) -> str:
    """写库。``ensure_ascii=False`` 是为了让 dump 出来的数据肉眼可读。"""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def dumps_int_list(values: list[int]) -> str:
    return dumps([int(value) for value in values])
