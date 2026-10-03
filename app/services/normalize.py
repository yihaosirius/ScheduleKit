"""模型输出的强制后处理 —— 服务端**唯一**的校验权威。

不可信前提的落地点：模型返回的结构只能当"建议"，最终形态由这里决定。

一条铁律（AGENTS.md §3）：**凡代码推翻了模型输出的地方，必须打点。**
否则事后无法区分"模型错了"还是"后处理改的"——这两种情况的修法完全相反
（前者改 prompt，后者改代码）。

所以本模块的返回值分两部分：

* ``items``      —— 通过校验的最终条目，字段名即 :func:`app.services.tasks.create_task` 的参数
* ``overrides``  —— 人类可读的覆盖说明，会写进 ``ingest_drafts.overrides``
  并在确认页上显示。例如"优先级已丢弃（该项有截止时间）"。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from app.llm import tools
from app.logging import get_logger, kv
from app.services import tasks as tasks_service
from app.text import clean_text
from app.timeutil import (
    TimeParseError,
    combine_local,
    local_iso,
    parse_local_or_offset,
    tz,
)

log = get_logger("normalize")

#: mock 与部分模型会输出这种"相对天数"占位符。真实模型偶尔也会这样
#: （被上下文里的"明天/后天"带跑），所以服务端必须能接住。
_RELATIVE_PLACEHOLDER = re.compile(r"^__RELATIVE_DAYS__(-?\d+)$")

#: 中文数字星期 → 距本周一的天数
_WEEKDAY_TOKENS = {
    "周一": 1, "周二": 2, "周三": 3, "周四": 4, "周五": 5, "周六": 6, "周日": 7,
    "星期一": 1, "星期二": 2, "星期三": 3, "星期四": 4, "星期五": 5, "星期六": 6,
    "星期天": 7, "星期日": 7,
}

# --------------------------------------------------------------------------- #
# 字段长度上限：**以模型 schema 为准，而不是以手工录入为准**
#
# 这里踩过一次：``tasks`` 里的 TITLE_MAX = 60 / NOTES_MAX = 500 是**手工录入**的上限
# （用户自己打字，宽松一点合理），而 ``app/llm/tools.py`` 里给模型规定的是
# 20 / 60 字。当初 normalize 直接复用了前者，结果模型吐 60 字的标题也能入库，
# 而 prompt 里明明写着"≤20字"——于是"标题太长"这个信号被静默吃掉了，
# 用户只会觉得界面排版莫名其妙地乱。
#
# 两者的关系是：schema 是**给模型的约束**，也是**接受其输出的上限**；
# 手工录入走另一套更宽的上限。二者共用同一个常量必然是错的。
# --------------------------------------------------------------------------- #
TITLE_MAX = tools.TITLE_MAX
NOTES_MAX = tools.NOTES_MAX
QUOTE_MAX = tools.QUOTE_MAX


@dataclass
class NormalizeResult:
    items: list[dict[str, Any]]
    #: 人类可读的覆盖说明，供确认页与排查使用
    overrides: list[str] = field(default_factory=list)
    #: 被丢弃的条目数与原因
    dropped: list[str] = field(default_factory=list)
    #: 原始条目数（含被丢弃的）
    received: int = 0

    @property
    def override_summary(self) -> str:
        return "；".join(self.overrides)

    @property
    def dropped_count(self) -> int:
        return len(self.dropped)


def normalize_items(
    raw_items: list[dict[str, Any]],
    *,
    now: datetime,
    tz_name: str,
    max_items: int = 20,
) -> NormalizeResult:
    """把模型输出规范化成可入库的条目。"""
    result = NormalizeResult(items=[], received=len(raw_items or []))

    if not raw_items:
        return result

    if len(raw_items) > max_items:
        result.overrides.append(f"条目数 {len(raw_items)} 超过上限 {max_items}，已截断")
        log.warning("normalize.truncated %s", kv(received=len(raw_items), limit=max_items))

    for index, raw in enumerate(raw_items[:max_items]):
        if not isinstance(raw, dict):
            result.dropped.append(f"第 {index + 1} 条不是对象")
            log.warning("normalize.dropped %s", kv(index=index, reason="not_object"))
            continue
        item = _one(raw, index=index, now=now, tz_name=tz_name, result=result)
        if item is None:
            continue
        result.items.append(item)

    log.info(
        "normalize.done %s",
        kv(received=result.received, kept=len(result.items), dropped=result.dropped_count,
           overrides=len(result.overrides)),
    )
    return result


def _one(
    raw: dict[str, Any],
    *,
    index: int,
    now: datetime,
    tz_name: str,
    result: NormalizeResult,
) -> dict[str, Any] | None:
    # ---------------------------------------------------------------- 标题
    title = _title(raw.get("title"), index=index, result=result)
    if not title:
        result.dropped.append(f"第 {index + 1} 条没有可用标题")
        log.warning("normalize.dropped %s", kv(index=index, reason="empty_title"))
        return None

    # ---------------------------------------------------------------- 类别
    category = _category(raw.get("category"), index=index, result=result)

    # ---------------------------------------------------------------- 二选一
    due_raw = raw.get("due_at")
    priority_raw = raw.get("priority")
    due_at, priority = _shape(
        due_raw, priority_raw, index=index, now=now, tz_name=tz_name, result=result
    )

    # ---------------------------------------------------------------- 选填
    notes, notes_truncated = _clip(
        raw.get("notes"), limit=NOTES_MAX, index=index, label="备注", result=result
    )
    source_quote, _ = _clip(
        raw.get("source_quote"), limit=QUOTE_MAX, index=index, label="原文片段",
        result=result, report=False,
    )

    item: dict[str, Any] = {
        "title": title,
        "category": category,
        "due_at": due_at,
        "priority": priority,
        "notes": notes,
    }
    # source_quote 只用于内部排查与"这句话支撑了这个判定"的展示，不进任务字段。
    # 但排查时它很有价值（用户说"识别错了"时，能看出模型引的是哪句）。
    if source_quote:
        item["_source_quote"] = source_quote
    return item


def _title(raw: Any, *, index: int, result: NormalizeResult) -> str:
    text, truncated_flag = _clip(
        raw, limit=TITLE_MAX, index=index, label="标题", result=result
    )
    return text


def _clip(
    raw: Any,
    *,
    limit: int,
    index: int,
    label: str,
    result: NormalizeResult,
    report: bool = True,
) -> tuple[str, bool]:
    """清洗并截断，返回 ``(文本, 是否被截断)``。

    截断**必须报告**（``report=True``）：模型明明被要求写 ≤20 字却写了 40 字，
    这件事是判断 prompt 效果的直接信号。静默截断会让这个信号消失，
    我们看到的是"一切正常"，而实际上模型一直没守规矩。
    """
    text = clean_text(raw)
    if len(text) <= limit:
        return text, False

    if report:
        result.overrides.append(
            f"第 {index + 1} 条{label}超长（{len(text)} 字）已截断到 {limit} 字"
        )
        log.warning(
            "normalize.%s_truncated %s",
            "title" if label == "标题" else "field",
            kv(index=index, original=len(text), limit=limit),
        )
    return text[:limit].rstrip(), True


def _category(raw: Any, *, index: int, result: NormalizeResult) -> str:
    text = clean_text(raw, limit=20).lower()
    if text in tasks_service.CATEGORIES:
        return text
    # 模型常见漂移：写中文、写同义词、写大写
    alias = {
        "homework": "homework", "作业": "homework", "hw": "homework",
        "practice": "practice", "练习": "practice",
        "exam": "exam", "考试": "exam", "测验": "exam",
        "appointment": "appointment", "约会": "appointment", "约定": "appointment",
        "other": "other", "其他": "other", "其它": "other",
    }
    if text in alias:
        mapped = alias[text]
        result.overrides.append(f"第 {index + 1} 条类别 {raw!r} 已归一为 {mapped}")
        log.warning("normalize.category_normalized %s", kv(index=index, raw=str(raw)[:20]))
        return mapped
    if text:
        result.overrides.append(f"第 {index + 1} 条类别 {raw!r} 无法识别，已归为 other")
        log.warning("normalize.category_downgraded %s", kv(index=index, raw=str(raw)[:20]))
    return "other"


def _shape(
    due_raw: Any,
    priority_raw: Any,
    *,
    index: int,
    now: datetime,
    tz_name: str,
    result: NormalizeResult,
) -> tuple[str | None, int | None]:
    """强制"截止时间与优先级恰好一个非空"。

    优先级规则（**不是随意定的**，对应两条真实失败模式）：

    * 有可解析的 ``due_at`` 时**丢弃 priority**。有序表的定义就是"有截止时间"，
      而模型经常两个都给（它觉得有截止时间的事也很急）。
    * 两个都没给时**兜底给 3（Ⅲ 常规）**，并把这件事记进 overrides。
      直接丢弃这条任务更糟：用户看到的是"识别掉了"，很难发现是被丢的。
    """
    due_raw_text = "" if due_raw is None else str(due_raw).strip()
    priority_value = _priority(priority_raw)

    if due_raw_text and due_raw_text.lower() not in ("null", "none", "无"):
        parsed = _parse_due(due_raw_text, index=index, now=now, tz_name=tz_name, result=result)
        if parsed is not None:
            if priority_value is not None:
                result.overrides.append(
                    f"第 {index + 1} 条同时给了截止时间与优先级，已丢弃优先级"
                    f"（有截止时间的任务进有序表）"
                )
                log.warning(
                    "normalize.priority_dropped %s",
                    kv(index=index, priority=priority_value, due=parsed),
                )
            return parsed, None
        if priority_value is None:
            result.overrides.append(
                f"第 {index + 1} 条截止时间无法解析（{due_raw_text[:20]!r}），"
                f"已按优先级 3（Ⅲ 常规）处理"
            )
            log.warning(
                "normalize.due_unparseable %s", kv(index=index, raw=due_raw_text[:30])
            )
            return None, 3
        return None, priority_value

    if priority_value is None:
        result.overrides.append(f"第 {index + 1} 条既没有截止时间也没有优先级，已按 3（Ⅲ 常规）处理")
        log.warning("normalize.needs_priority %s", kv(index=index, defaulted=3))
        return None, 3

    return None, priority_value


def _priority(raw: Any) -> int | None:
    if raw is None or raw == "":
        return None
    if isinstance(raw, bool):  # bool 是 int 的子类，必须先挡掉
        return None
    if isinstance(raw, int):
        return raw if 1 <= raw <= 5 else None
    text = clean_text(raw, limit=8)
    if not text or text.lower() in ("null", "none", "无"):
        return None
    # 罗马数字与中文档位是模型常见的输出形态
    roman = {"Ⅰ": 1, "Ⅱ": 2, "Ⅲ": 3, "Ⅳ": 4, "Ⅴ": 5, "I": 1, "II": 2, "III": 3, "IV": 4, "V": 5}
    if text.upper() in roman:
        return roman[text.upper()]
    try:
        value = int(float(text))
    except ValueError:
        return None
    return value if 1 <= value <= 5 else None


def _parse_due(
    text: str,
    *,
    index: int,
    now: datetime,
    tz_name: str,
    result: NormalizeResult,
) -> str | None:
    # mock 与"被上下文带跑"的模型可能输出相对天数占位符
    placeholder = _RELATIVE_PLACEHOLDER.match(text)
    if placeholder:
        days = int(placeholder.group(1))
        day = now.astimezone(tz(tz_name)).date() + timedelta(days=days)
        resolved = local_iso(combine_local(day, _end_of_day(), tz_name), tz_name)
        result.overrides.append(
            f"第 {index + 1} 条模型只给了相对时间（{days} 天后），已按当天 23:59 解析"
        )
        log.warning("normalize.relative_resolved %s", kv(index=index, days=days, due=resolved))
        return _to_utc(resolved)

    # "本周三""下周一"这类写法：模型有时只给星期，不给日期
    weekday_hit = _relative_weekday(text, now=now, tz_name=tz_name)
    if weekday_hit is not None:
        resolved = local_iso(weekday_hit, tz_name)
        result.overrides.append(
            f"第 {index + 1} 条模型只给了星期（{text[:12]!r}），已按下一个该星期解析"
        )
        log.warning("normalize.weekday_resolved %s", kv(index=index, raw=text[:20], due=resolved))
        return _to_utc(resolved)

    try:
        moment, timezone_inferred = parse_local_or_offset(text, tz_name)
    except TimeParseError:
        return None

    if timezone_inferred:
        result.overrides.append(
            f"第 {index + 1} 条的截止时间没有时区，已按 {tz_name} 解释为 {local_iso(moment, tz_name)}"
        )
        log.warning(
            "normalize.timezone_inferred %s", kv(index=index, raw=text[:24], localized=local_iso(moment, tz_name))
        )

    # 只给日期时 parse_local_or_offset 会落到当天 00:00。作业几乎不会是半夜零点交，
    # 统一推到当天 23:59，这与用户"周日交"的直觉一致。
    if _looks_date_only(text):
        local = moment.astimezone(tz(tz_name))
        moment = combine_local(local.date(), _end_of_day(), tz_name)
        result.overrides.append(
            f"第 {index + 1} 条只给了日期，已按当天 23:59 处理（{local_iso(moment, tz_name)}）"
        )
        log.warning("normalize.date_only %s", kv(index=index, raw=text[:24]))
    return _to_utc(moment)


def _end_of_day():
    from datetime import time as clock_time

    return clock_time(23, 59)


def _looks_date_only(text: str) -> bool:
    stripped = text.strip()
    if "T" in stripped or ":" in stripped:
        return False
    return bool(re.search(r"\d{4}[-/年]\d{1,2}[-/月]\d{1,2}", stripped))


def _relative_weekday(text: str, *, now: datetime, tz_name: str) -> datetime | None:
    """解析"周三""本周三""下周一" → 具体时刻（当天 23:59）。"""
    if not any(token in text for token in _WEEKDAY_TOKENS):
        return None
    # 已经是完整日期的时间串不该被这里劫走
    if re.search(r"\d{4}[-/年]", text):
        return None

    target = None
    for token, iso_weekday in _WEEKDAY_TOKENS.items():
        if token in text:
            target = iso_weekday
            break
    if target is None:
        return None

    local_now = now.astimezone(tz(tz_name))
    today = local_now.date()
    if "下周" in text:
        # 下周三 = 本周三再 +7
        days = (target - today.isoweekday()) % 7 + 7
    elif "本周" in text or "这周" in text:
        days = (target - today.isoweekday()) % 7
    else:
        # 只说"周三"：取最近的那个（含今天）
        days = (target - today.isoweekday()) % 7
    target_day: date = today + timedelta(days=days)
    return combine_local(target_day, _end_of_day(), tz_name)


def _to_utc(local_iso_text: str) -> str:
    """把带偏移量的本地 ISO 串转成 UTC ISO 串。"""
    from app.timeutil import parse_utc, to_utc_iso

    return to_utc_iso(parse_utc(local_iso_text))
