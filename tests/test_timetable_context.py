"""课表与时间上下文的测试。

这两块是"识别对不对"的直接决定因素，而且错误**很难从界面看出来**：
提示词里的日期算错一天，用户只会看到"识别出来的截止时间不对"，
然后怀疑是模型不行。所以这里对边界（假期、跨天、周次过滤）都要覆盖。
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta, timezone

import pytest

from app.db import connect
from app.migrations import runner
from app.services import context as context_service
from app.services import timetable as timetable_service
from app.services.timetable import Course, Session, TimetableError
from app.timeutil import tz

TZ = "Asia/Shanghai"
#: 学期第 1 周的周一。所有用例都基于它，避免依赖真实日期。
TERM_START = date(2026, 9, 14)


@pytest.fixture
def con(work_dir) -> sqlite3.Connection:
    connection = connect(work_dir / "timetable.db")
    runner.run(connection)
    yield connection
    connection.close()


def local(*args: int) -> datetime:
    """构造本地时间并转 UTC。参数为 (年, 月, 日, 时, 分)。"""
    return datetime(*args, tzinfo=tz(TZ)).astimezone(timezone.utc)


def build_course(name: str, weekday: int, start: str, end: str, weeks: str = "1-16") -> Course:
    return Course(
        id=1,
        name=name,
        sessions=[
            Session(
                id=1,
                course_id=1,
                weekday=weekday,
                start_time=start,
                end_time=end,
                weeks=weeks,
                course_name=name,
            )
        ],
    )


# --------------------------------------------------------------------------- #
# weeks 解析
# --------------------------------------------------------------------------- #
def test_parse_weeks_range() -> None:
    parsed = timetable_service.parse_weeks("1-16", total_weeks=20)
    assert parsed.covers(1) and parsed.covers(16)
    assert not parsed.covers(17)
    assert not parsed.fallback


def test_parse_weeks_list() -> None:
    parsed = timetable_service.parse_weeks("1,3,5", total_weeks=20)
    assert parsed.covers(1) and parsed.covers(3) and parsed.covers(5)
    assert not parsed.covers(2)


def test_parse_weeks_mixed() -> None:
    parsed = timetable_service.parse_weeks("2-4,8", total_weeks=20)
    assert {2, 3, 4, 8} <= parsed.weeks
    assert not parsed.covers(5)


def test_parse_weeks_fullwidth_comma_and_dash() -> None:
    """手输课表时全角字符很常见，必须能吃下。"""
    parsed = timetable_service.parse_weeks("1，3－5", total_weeks=20)
    assert {1, 3, 4, 5} <= parsed.weeks


def test_parse_weeks_empty_means_all() -> None:
    parsed = timetable_service.parse_weeks("", total_weeks=20)
    assert parsed.fallback and parsed.covers(99)


def test_parse_weeks_reversed_range_is_tolerated() -> None:
    parsed = timetable_service.parse_weeks("16-1", total_weeks=20)
    assert parsed.covers(1) and parsed.covers(16)


def test_parse_weeks_garbage_falls_back_instead_of_raising() -> None:
    """一行课表写错不该让整个识别功能挂掉，但必须能被日志发现。"""
    parsed = timetable_service.parse_weeks("每周三下午", total_weeks=20)
    assert parsed.fallback
    assert parsed.covers(7)


def test_parse_weeks_clamps_to_total() -> None:
    parsed = timetable_service.parse_weeks("1-30", total_weeks=16)
    assert parsed.covers(16)
    assert not parsed.covers(17)


# --------------------------------------------------------------------------- #
# find_around
# --------------------------------------------------------------------------- #
def test_find_around_ongoing() -> None:
    course = build_course("电子电路基础", weekday=3, start="08:00", end="09:40")
    # 2026-09-16 是周三，第 1 周
    now = local(2026, 9, 16, 9, 0)
    found = timetable_service.find_around(
        [course], now, start_date=TERM_START, total_weeks=16, tz_name=TZ
    )
    assert len(found) == 1
    assert found[0].relation == "ongoing"
    assert found[0].minutes_away == 0


def test_find_around_upcoming_within_window() -> None:
    course = build_course("英语", weekday=3, start="10:00", end="11:40")
    now = local(2026, 9, 16, 9, 0)  # 距上课 60 分钟
    found = timetable_service.find_around(
        [course], now, start_date=TERM_START, total_weeks=16, tz_name=TZ
    )
    assert found and found[0].relation == "upcoming"
    assert found[0].minutes_away == 60


def test_find_around_too_far_is_excluded() -> None:
    """窗口太长会把明天的课也扯进来，让"下节课"解析成错误的日期。"""
    course = build_course("英语", weekday=3, start="16:00", end="17:40")
    now = local(2026, 9, 16, 9, 0)
    found = timetable_service.find_around(
        [course], now, start_date=TERM_START, total_weeks=16, tz_name=TZ
    )
    assert found == []


def test_find_around_just_ended() -> None:
    course = build_course("数学", weekday=3, start="08:00", end="09:40")
    now = local(2026, 9, 16, 9, 50)  # 下课后 10 分钟
    found = timetable_service.find_around(
        [course], now, start_date=TERM_START, total_weeks=16, tz_name=TZ
    )
    assert found and found[0].relation == "just_ended"
    assert found[0].minutes_away == -10


def test_find_around_respects_week_filter() -> None:
    """单周课（只上第 1、3、5 周）在第 2 周不该出现。"""
    course = build_course("单周研讨", weekday=3, start="08:00", end="09:40", weeks="1,3,5")
    week2 = local(2026, 9, 23, 9, 0)
    found = timetable_service.find_around(
        [course], week2, start_date=TERM_START, total_weeks=16, tz_name=TZ
    )
    assert found == []

    week3 = local(2026, 9, 30, 9, 0)
    found = timetable_service.find_around(
        [course], week3, start_date=TERM_START, total_weeks=16, tz_name=TZ
    )
    assert len(found) == 1


def test_find_around_skips_holiday() -> None:
    """学期开始前不算上课，即使星期与时间都对得上。"""
    course = build_course("预修课", weekday=3, start="08:00", end="09:40")
    before = local(2026, 9, 9, 9, 0)  # 开学前一周的周三
    found = timetable_service.find_around(
        [course], before, start_date=TERM_START, total_weeks=16, tz_name=TZ
    )
    assert found == []


def test_find_around_next_day_early_morning() -> None:
    """晚上 23:30 看"明天早上 8 点的课"，仍在 120 分钟窗口内。

    这条容易漏：只查"今天"的话，深夜录入的作业就丢失了课程指代的上下文。
    """
    course = build_course("早课", weekday=4, start="00:30", end="02:00")
    now = local(2026, 9, 16, 23, 30)  # 周三深夜
    found = timetable_service.find_around(
        [course], now, start_date=TERM_START, total_weeks=16, tz_name=TZ
    )
    assert found and found[0].relation == "upcoming"
    assert found[0].day == date(2026, 9, 17)


def test_find_around_orders_ongoing_before_upcoming() -> None:
    ongoing = build_course("正在上", weekday=3, start="08:00", end="10:00")
    ongoing.id = 1
    upcoming = build_course("马上要上", weekday=3, start="10:30", end="12:00")
    upcoming.id = 2
    for session in upcoming.sessions:
        session.course_id = 2

    now = local(2026, 9, 16, 9, 30)
    found = timetable_service.find_around(
        [upcoming, ongoing], now, start_date=TERM_START, total_weeks=16, tz_name=TZ
    )
    assert [item.relation for item in found] == ["ongoing", "upcoming"]


# --------------------------------------------------------------------------- #
# weekday_schedule / week_matrix
# --------------------------------------------------------------------------- #
def test_weekday_schedule_returns_sorted() -> None:
    course = Course(
        id=1,
        name="组合课",
        sessions=[
            Session(1, 1, 3, "14:00", "15:40", "1-16", course_name="组合课"),
            Session(2, 1, 3, "08:00", "09:40", "1-16", course_name="组合课"),
        ],
    )
    sessions = timetable_service.weekday_schedule(
        [course], date(2026, 9, 16), total_weeks=16, start_date=TERM_START
    )
    assert [s.start_time for s in sessions] == ["08:00", "14:00"]


def test_week_matrix_groups_by_weekday() -> None:
    course = build_course("电子电路基础", weekday=3, start="08:00", end="09:40")
    matrix = timetable_service.week_matrix([course])
    assert matrix["周三"][0]["course_name"] == "电子电路基础"
    assert matrix["周一"] == []
    assert len(matrix) == 7


# --------------------------------------------------------------------------- #
# replace_timetable
# --------------------------------------------------------------------------- #
def test_replace_timetable_persists(con) -> None:
    courses = timetable_service.replace_timetable(
        con,
        [
            {
                "name": "概率论",
                "teacher": "李老师",
                "sessions": [
                    {"weekday": 1, "start_time": "8:00", "end_time": "09:40", "weeks": "1-16"}
                ],
            }
        ],
    )
    assert len(courses) == 1
    assert courses[0].sessions[0].start_time == "08:00", "时间要归一成 HH:MM"
    assert timetable_service.list_courses(con)[0].name == "概率论"


def test_replace_timetable_is_wholesale(con) -> None:
    timetable_service.replace_timetable(con, [{"name": "A"}])
    timetable_service.replace_timetable(con, [{"name": "B"}])
    names = [course.name for course in timetable_service.list_courses(con)]
    assert names == ["B"]


def test_replace_timetable_rejects_bad_time(con) -> None:
    with pytest.raises(TimetableError):
        timetable_service.replace_timetable(
            con,
            [{"name": "X", "sessions": [{"weekday": 1, "start_time": "11:00", "end_time": "10:00"}]}],
        )


def test_replace_timetable_rejects_bad_weekday(con) -> None:
    with pytest.raises(TimetableError):
        timetable_service.replace_timetable(
            con,
            [{"name": "X", "sessions": [{"weekday": 8, "start_time": "08:00", "end_time": "09:00"}]}],
        )


def test_replace_timetable_is_atomic_on_error(con) -> None:
    """校验在写库前全部完成，所以失败时不该留下半张课表。"""
    timetable_service.replace_timetable(con, [{"name": "原有的课"}])
    with pytest.raises(TimetableError):
        timetable_service.replace_timetable(
            con,
            [
                {"name": "新的课"},
                {"name": "坏的课", "sessions": [{"weekday": 1, "start_time": "x", "end_time": "y"}]},
            ],
        )
    names = [course.name for course in timetable_service.list_courses(con)]
    assert names == ["原有的课"]


# --------------------------------------------------------------------------- #
# 上下文构建
# --------------------------------------------------------------------------- #
def _input(courses=None, memories=None, now=None, budget=2000):
    return context_service.ContextInput(
        now=now or local(2026, 9, 16, 9, 0),
        tz_name=TZ,
        term_start=TERM_START,
        total_weeks=16,
        courses=courses or [],
        memories=memories or [],
        memory_char_budget=budget,
    )


def test_context_contains_local_and_utc_time() -> None:
    built = context_service.build(_input())
    assert "2026-09-16T09:00:00+08:00" in built.text
    assert "2026-09-16T01:00:00+00:00" in built.text


def test_context_states_week_number() -> None:
    built = context_service.build(_input())
    assert built.week == 1
    assert "第 1 周 / 共 16 周" in built.text


def test_context_reports_before_term() -> None:
    built = context_service.build(_input(now=local(2026, 9, 9, 9, 0)))
    assert built.week == 0
    assert "尚未开学" in built.text


def test_context_reports_after_term() -> None:
    built = context_service.build(_input(now=local(2027, 3, 3, 9, 0)))
    assert built.week > 16
    assert "学期已结束" in built.text


def test_context_includes_ongoing_course() -> None:
    course = build_course("电子电路基础", weekday=3, start="08:00", end="10:00")
    built = context_service.build(_input(courses=[course]))
    assert "正在上" in built.text
    assert "电子电路基础" in built.text
    assert built.around[0].relation == "ongoing"


def test_context_says_so_when_no_class() -> None:
    built = context_service.build(_input())
    assert "没有正在上" in built.text or "此刻没有" in built.text
    assert "今天没有课" in built.text


def test_context_lists_today_courses() -> None:
    courses = [
        build_course("电子电路基础", weekday=3, start="08:00", end="09:40"),
        build_course("复变函数", weekday=3, start="14:00", end="15:40"),
    ]
    built = context_service.build(_input(courses=courses))
    assert "今天的全部课程" in built.text
    assert len(built.today) == 2


def test_context_offers_concrete_reference_times() -> None:
    """给模型"可以照抄"的具体时刻，比反复叮嘱它算日期有效得多。"""
    built = context_service.build(_input())
    assert "可直接引用的时间点" in built.text
    assert "2026-09-16T23:59:00+08:00" in built.text, "今天 23:59"
    assert "2026-09-17T23:59:00+08:00" in built.text, "明天 23:59"
    # 2026-09-20 是本周日
    assert "2026-09-20T23:59:00+08:00" in built.text


def test_context_declares_its_own_constraints() -> None:
    """必须显式声明"上下文不参与优先级判定"，否则模型会拿它污染档位。"""
    built = context_service.build(_input())
    assert "不得影响优先级判定" in built.text
    assert "一律以内容为准" in built.text


def _memory(memory_id: int, *, title: str = "记忆", content: str = "内容", pin: bool = False,
            order: int = 0, scope: str = "global"):
    from app.services.memory import Memory

    return Memory(
        id=memory_id,
        title=title,
        content=content,
        scope=scope,
        pin_single=pin,
        sort_order=order,
    )


def test_context_injects_memories_and_reports_ids() -> None:
    built = context_service.build(
        _input(memories=[_memory(7, title="电子电路", content="周三交")])
    )
    assert built.memory_ids == [7]
    assert "[7]" in built.text
    assert "电子电路" in built.text


def test_context_says_when_no_memory() -> None:
    built = context_service.build(_input())
    assert built.memory_ids == []
    assert "没有注入任何记忆" in built.text


def test_pinned_memory_survives_budget() -> None:
    """pin_single 的条目优先级最高：用户显式点了"这条要带上"，
    它不该因为别的记忆太长而被裁掉。"""
    memories = [
        _memory(1, title="A" * 50, content="B" * 200, order=0),
        _memory(2, title="被钉住的", content="C" * 50, order=1, pin=True),
    ]
    built = context_service.build(_input(memories=memories, budget=200))
    assert 2 in built.memory_ids, "pin 的条目必须在注入列表里"
    assert 1 in built.memory_dropped, "超预算的普通条目应当被裁掉"


def test_memory_dropped_ids_reported() -> None:
    memories = [_memory(i, title=f"T{i}", content="X" * 100, order=i) for i in range(1, 10)]
    built = context_service.build(_input(memories=memories, budget=200))
    assert built.memory_dropped, "超出预算时必须回报被裁掉的 id"
    assert set(built.memory_ids).isdisjoint(built.memory_dropped)
    assert "省略了" in built.text
