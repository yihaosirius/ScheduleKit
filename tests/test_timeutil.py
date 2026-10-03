"""时间工具测试。

这个模块的错误后果最严重也最隐蔽：差 8 小时的任务在界面上看起来完全正常。
所以这里覆盖的不是"函数能跑"，而是**边界与纠错的可观测性**：

* 裸时间被我们补时区时必须报告（调用方要打点）。
* 学期周次用日期差而不是 ISO 周编号。
* 读库遇到脏时间必须抛错，而不是悄悄当 UTC。
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

import pytest

from app.timeutil import (
    TimeParseError,
    combine_local,
    human_delta,
    local_iso,
    now_utc,
    now_utc_iso,
    parse_clock,
    parse_local_or_offset,
    parse_utc,
    term_week,
    term_week_now,
    to_utc_iso,
    tz,
    week_dates,
)

TZ = "Asia/Shanghai"  # UTC+8，没有夏令时，适合做确定性断言


# --------------------------------------------------------------------------- #
# 基本往返
# --------------------------------------------------------------------------- #
def test_now_utc_iso_is_parseable_and_utc() -> None:
    text = now_utc_iso()
    parsed = parse_utc(text)
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == timedelta(0)
    assert abs((now_utc() - parsed).total_seconds()) < 5


def test_to_utc_iso_converts_offset() -> None:
    moment = datetime(2026, 10, 10, 23, 59, tzinfo=tz(TZ))
    assert to_utc_iso(moment) == "2026-10-10T15:59:00+00:00"


def test_to_utc_iso_treats_naive_as_utc() -> None:
    """裸 datetime 按 UTC 解释。这条口径必须稳定，否则不同模块会各写一套。"""
    assert to_utc_iso(datetime(2026, 10, 10, 12, 0)) == "2026-10-10T12:00:00+00:00"


def test_parse_utc_accepts_zulu_and_naive() -> None:
    assert parse_utc("2026-10-10T15:59:00Z").utcoffset() == timedelta(0)
    # 库里理论上不该有裸时间，但真读到了按 UTC 解释而不是抛错
    assert parse_utc("2026-10-10T15:59:00").hour == 15


def test_parse_utc_rejects_garbage() -> None:
    with pytest.raises(TimeParseError):
        parse_utc("上周三下午")


def test_microseconds_are_dropped() -> None:
    moment = datetime(2026, 10, 10, 12, 0, 0, 123456, tzinfo=timezone.utc)
    assert to_utc_iso(moment) == "2026-10-10T12:00:00+00:00"


# --------------------------------------------------------------------------- #
# parse_local_or_offset：补时区必须可观测
# --------------------------------------------------------------------------- #
def test_offset_is_preserved_and_not_flagged() -> None:
    moment, inferred = parse_local_or_offset("2026-10-10T23:59:00+08:00", TZ)
    assert moment == datetime(2026, 10, 10, 15, 59, tzinfo=timezone.utc)
    assert inferred is False


def test_naive_datetime_is_flagged_as_inferred() -> None:
    moment, inferred = parse_local_or_offset("2026-10-10T23:59:00", TZ)
    assert moment == datetime(2026, 10, 10, 15, 59, tzinfo=timezone.utc)
    assert inferred is True, "补了时区必须报告，否则差 8 小时的错误会静默发生"


def test_zulu_is_not_flagged() -> None:
    moment, inferred = parse_local_or_offset("2026-10-10T15:59:00Z", TZ)
    assert moment == datetime(2026, 10, 10, 15, 59, tzinfo=timezone.utc)
    assert inferred is False


def test_date_only_becomes_local_midnight() -> None:
    """只给日期时，按本地零点理解——不是当天 23:59，也不是 UTC 零点。

    "2026-10-10 交作业"在不同理解下能差 8 小时以上。
    """
    moment, inferred = parse_local_or_offset("2026-10-10", TZ)
    assert moment == datetime(2026, 10, 9, 16, 0, tzinfo=timezone.utc)
    assert inferred is True


@pytest.mark.parametrize(
    "text",
    [
        "2026/10/10 23:59",
        "2026-10-10 23:59:00",
        "2026年10月10日 23:59",
        "2026年10月10日",
    ],
)
def test_accepted_human_formats(text: str) -> None:
    moment, _ = parse_local_or_offset(text, TZ)
    assert moment.astimezone(tz(TZ)).date() == date(2026, 10, 10)


def test_empty_string_is_rejected() -> None:
    with pytest.raises(TimeParseError):
        parse_local_or_offset("   ", TZ)


# --------------------------------------------------------------------------- #
# 学期周次
# --------------------------------------------------------------------------- #
def test_term_week_boundaries() -> None:
    start = date(2026, 9, 14)  # 周一
    assert term_week(start, start) == 1
    assert term_week(start + timedelta(days=6), start) == 1   # 同周周日
    assert term_week(start + timedelta(days=7), start) == 2   # 下周一
    assert term_week(start + timedelta(days=13), start) == 2
    assert term_week(start + timedelta(days=14), start) == 3


def test_term_week_before_term_start_is_zero_or_negative() -> None:
    """假期里的日期会算出 ≤0 的周次。调用方要能识别出来，所以这里不能夹到 1。"""
    start = date(2026, 9, 14)
    assert term_week(start - timedelta(days=1), start) == 0
    assert term_week(start - timedelta(days=7), start) == 0
    assert term_week(start - timedelta(days=8), start) == -1


def test_term_week_not_iso_week_number() -> None:
    """跨年学期：ISO 周编号会跳变，日期差不会。"""
    start = date(2026, 12, 28)  # 周一，ISO 第 53 周
    assert term_week(start + timedelta(days=7), start) == 2
    assert term_week(date(2027, 1, 4), start) == 2


def test_term_week_now_uses_local_date() -> None:
    start = date(2026, 9, 14)
    # UTC 周日 20:00 == 上海周一 04:00 → 属于第 2 周
    moment = datetime(2026, 9, 20, 20, 0, tzinfo=timezone.utc)
    assert term_week_now(moment, start, TZ) == 2


def test_week_dates_returns_monday_to_sunday() -> None:
    start = date(2026, 9, 14)
    monday, sunday = week_dates(3, start)
    assert monday == date(2026, 9, 28)
    assert monday.weekday() == 0
    assert sunday == date(2026, 10, 4)
    assert sunday.weekday() == 6


# --------------------------------------------------------------------------- #
# 渲染辅助
# --------------------------------------------------------------------------- #
def test_local_iso_renders_in_configured_timezone() -> None:
    moment = datetime(2026, 10, 10, 15, 59, tzinfo=timezone.utc)
    assert local_iso(moment, TZ) == "2026-10-10T23:59:00+08:00"


def test_combine_local_builds_offset_aware_datetime() -> None:
    moment = combine_local(date(2026, 10, 10), time(8, 0), TZ)
    assert moment.utcoffset() == timedelta(hours=8)
    assert moment.isoformat() == "2026-10-10T08:00:00+08:00"


@pytest.mark.parametrize("text", ["08:00", "8:00", "08:00:00"])
def test_parse_clock_accepts_common_forms(text: str) -> None:
    assert parse_clock(text) == time(8, 0)


def test_parse_clock_rejects_garbage() -> None:
    with pytest.raises(TimeParseError):
        parse_clock("早上八点")


def test_human_delta_directions() -> None:
    now = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)
    assert human_delta(now + timedelta(minutes=30), now) == "30 分钟后"
    assert human_delta(now - timedelta(hours=3), now) == "3 小时前"
    assert human_delta(now + timedelta(days=2), now) == "2 天后"
    assert human_delta(now + timedelta(days=75), now) == "2 个月后"
    assert human_delta(now + timedelta(seconds=20), now) == "1 分钟内"


def test_unknown_timezone_falls_back_to_utc_instead_of_crashing() -> None:
    """配置里写错时区名不该让服务起不来；出错点应体现在别处（状态接口）。"""
    assert tz("Mars/Olympus").utcoffset(datetime(2026, 1, 1)) == timedelta(0)
