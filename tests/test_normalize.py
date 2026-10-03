"""模型输出后处理的测试 —— 不可信前提的守门人。

这个模块的价值全在**边界**上：模型会给出"两个都填""两个都不填""日期算错"
"类别写中文""只给星期"这类输出。每一条都必须有确定性的处理方式，
而且**处理过就必须能在 overrides 里看见**。

``overrides`` 那一半断言和后一半一样重要：只测"结果对不对"、不测"我们改过什么"，
就等于允许静默改写——而那正是最难排查的失败。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.services import normalize
from app.timeutil import tz

TZ = "Asia/Shanghai"
NOW = datetime(2026, 9, 16, 9, 0, tzinfo=tz(TZ)).astimezone(timezone.utc)


def run(items: list[dict], *, max_items: int = 20):
    return normalize.normalize_items(items, now=NOW, tz_name=TZ, max_items=max_items)


def one(**kwargs) -> dict:
    base = {"title": "写作业", "category": "homework", "due_at": None, "priority": 3}
    base.update(kwargs)
    return base


# --------------------------------------------------------------------------- #
# 基本形态
# --------------------------------------------------------------------------- #
def test_clean_item_passes_through() -> None:
    result = run([one(due_at=None, priority=2)])
    assert len(result.items) == 1
    item = result.items[0]
    assert item["title"] == "写作业"
    assert item["priority"] == 2
    assert item["due_at"] is None
    assert result.overrides == []


def test_due_at_is_normalized_to_utc() -> None:
    result = run([one(due_at="2026-10-10T23:59:00+08:00", priority=None)])
    assert result.items[0]["due_at"] == "2026-10-10T15:59:00+00:00"
    assert result.items[0]["priority"] is None


def test_empty_input_returns_nothing() -> None:
    result = run([])
    assert result.items == []
    assert result.overrides == []


def test_non_dict_entries_are_dropped_and_reported() -> None:
    result = run([one(), "我不是对象", 42])
    assert len(result.items) == 1
    assert len(result.dropped) == 2


def test_max_items_truncates_loudly() -> None:
    result = run([one(title=f"任务{i}") for i in range(30)], max_items=5)
    assert len(result.items) == 5
    assert any("截断" in note for note in result.overrides)


# --------------------------------------------------------------------------- #
# 二选一
# --------------------------------------------------------------------------- #
def test_both_given_drops_priority_and_reports() -> None:
    """有截止时间的任务进有序表，所以优先级必须让位。"""
    result = run([one(due_at="2026-10-10T23:59:00+08:00", priority=1)])
    item = result.items[0]
    assert item["due_at"] is not None
    assert item["priority"] is None
    assert any("丢弃优先级" in note for note in result.overrides)


def test_neither_given_defaults_to_priority_3_and_reports() -> None:
    """默认档位而不是丢弃：用户看到"识别掉了"比看到"档位不对"更难发现。"""
    result = run([one(due_at=None, priority=None)])
    assert result.items[0]["priority"] == 3
    assert any("3（Ⅲ 常规）" in note for note in result.overrides)


def test_unparseable_due_falls_back_to_priority() -> None:
    """模型给出根本不含日期的内容时，落到优先级而不是丢掉任务。"""
    result = run([one(due_at="找个时间", priority=None)])
    assert result.items[0]["due_at"] is None
    assert result.items[0]["priority"] == 3
    assert any("无法解析" in note for note in result.overrides)


def test_unparseable_due_keeps_model_priority() -> None:
    result = run([one(due_at="说不清", priority=1)])
    assert result.items[0]["priority"] == 1
    assert result.items[0]["due_at"] is None


def test_literal_null_strings_are_treated_as_missing() -> None:
    result = run([one(due_at="null", priority=2)])
    assert result.items[0]["due_at"] is None
    assert result.items[0]["priority"] == 2


def test_out_of_range_priority_is_dropped() -> None:
    """priority=9 是模型越界，不能让它进有序表以外的任何地方。"""
    result = run([one(due_at=None, priority=9)])
    assert result.items[0]["priority"] == 3, "越界档位应当落到默认值"
    assert any("3（Ⅲ 常规）" in note for note in result.overrides)


# --------------------------------------------------------------------------- #
# 优先级的各种写法
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "raw,expected",
    [
        (1, 1), (5, 5), ("3", 3), (3.0, 3), ("Ⅲ", 3), ("IV", 4), ("Ⅴ", 5),
        (None, 3), ("", 3), ("无", 3), (0, 3), (7, 3), ("紧急", 3),
    ],
)
def test_priority_forms(raw, expected: int) -> None:
    result = run([one(due_at=None, priority=raw)])
    assert result.items[0]["priority"] == expected


def test_bool_priority_is_not_treated_as_int() -> None:
    """Python 里 True == 1，但模型给 true 不是"给优先级 1"的意思。"""
    result = run([one(due_at=None, priority=True)])
    assert result.items[0]["priority"] == 3


# --------------------------------------------------------------------------- #
# 时间解析
# --------------------------------------------------------------------------- #
def test_relative_placeholder_is_resolved_and_reported() -> None:
    """mock 与"被上下文带跑"的模型都会输出相对天数，必须能接住。"""
    result = run([one(due_at="__RELATIVE_DAYS__2", priority=None)])
    item = result.items[0]
    assert item["due_at"] is not None
    # 当天 23:59（本地）= 2026-09-18T23:59+08:00 = 15:59Z
    assert item["due_at"] == "2026-09-18T15:59:00+00:00"
    assert any("相对时间" in note for note in result.overrides)


def test_date_only_is_pushed_to_end_of_day() -> None:
    """只给日期时落到当天 23:59：作业几乎不会是半夜零点交。"""
    result = run([one(due_at="2026-10-10", priority=None)])
    assert result.items[0]["due_at"] == "2026-10-10T15:59:00+00:00"
    assert any("23:59" in note for note in result.overrides)


def test_missing_timezone_is_inferred_and_reported() -> None:
    """补时区必须留痕：差 8 小时的任务在界面上看起来完全正常。"""
    result = run([one(due_at="2026-10-10T23:59:00", priority=None)])
    assert result.items[0]["due_at"] == "2026-10-10T15:59:00+00:00"
    assert any("没有时区" in note for note in result.overrides)


def test_explicit_offset_is_not_reported() -> None:
    result = run([one(due_at="2026-10-10T23:59:00+08:00", priority=None)])
    assert not any("没有时区" in note for note in result.overrides)


def test_weekday_only_resolves_to_next_occurrence() -> None:
    """"周三"这种写法：今天是周三，所以取今天。"""
    result = run([one(due_at="周三", priority=None)])
    assert result.items[0]["due_at"] == "2026-09-16T15:59:00+00:00"
    assert any("星期" in note for note in result.overrides)


def test_next_week_weekday_adds_seven_days() -> None:
    result = run([one(due_at="下周三", priority=None)])
    assert result.items[0]["due_at"] == "2026-09-23T15:59:00+00:00"


def test_weekday_does_not_hijack_full_dates() -> None:
    """含完整日期的时间串不该被"周三"规则劫走。"""
    result = run([one(due_at="2026-10-10T23:59:00+08:00", priority=None)])
    assert result.items[0]["due_at"] == "2026-10-10T15:59:00+00:00"


def test_zulu_suffix_accepted() -> None:
    result = run([one(due_at="2026-10-10T15:59:00Z", priority=None)])
    assert result.items[0]["due_at"] == "2026-10-10T15:59:00+00:00"


# --------------------------------------------------------------------------- #
# 类别
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("homework", "homework"),
        ("作业", "homework"),
        ("HW", "homework"),
        ("exam", "exam"),
        ("考试", "exam"),
        ("约会", "appointment"),
        ("其他", "other"),
        ("nonsense", "other"),
        (None, "other"),
    ],
)
def test_category_normalization(raw, expected: str) -> None:
    result = run([one(category=raw)])
    assert result.items[0]["category"] == expected


def test_category_rewrite_is_reported() -> None:
    result = run([one(category="作业")])
    assert any("归一" in note for note in result.overrides)

    result = run([one(category="nonsense")])
    assert any("other" in note for note in result.overrides)


# --------------------------------------------------------------------------- #
# 文本字段
# --------------------------------------------------------------------------- #
def test_title_truncated_to_limit_and_reported() -> None:
    result = run([one(title="标" * 80)])
    assert len(result.items[0]["title"]) == 20
    assert any("超长" in note for note in result.overrides)


def test_title_whitespace_and_zero_width_cleaned() -> None:
    result = run([one(title="  写\u200b  作业  ")])
    assert result.items[0]["title"] == "写 作业"


def test_missing_title_drops_item() -> None:
    result = run([one(title=""), one(title="   "), one(title=None)])
    assert result.items == []
    assert len(result.dropped) == 3


def test_notes_cleaned_and_bounded() -> None:
    result = run([one(notes="备" * 200)])
    assert len(result.items[0]["notes"]) == 60


def test_source_quote_kept_as_internal_field() -> None:
    """source_quote 不进任务字段，但排查"模型引了哪句话"时很有用。"""
    result = run([one(source_quote="下周一交")])
    assert result.items[0]["_source_quote"] == "下周一交"


def test_multiple_items_kept_in_order() -> None:
    result = run([one(title="第一"), one(title="第二"), one(title="第三")])
    assert [item["title"] for item in result.items] == ["第一", "第二", "第三"]


def test_override_summary_joins_with_separator() -> None:
    # 两条都触发 override：第一条同时给了截止时间与优先级；第二条标题超长
    result = run([one(due_at="2026-10-10T23:59:00+08:00", priority=1), one(title="标" * 50)])
    assert len(result.overrides) == 2
    assert "；" in result.override_summary
    # 被丢弃的条目单独计数，不混进 overrides
    dropped = run([one(title="")])
    assert dropped.dropped_count == 1
    assert dropped.received == 1
    assert dropped.items == []
