"""课表业务规则：解析、校验、以及"当下上什么课"。

**课表是提示词的一部分**，不是装饰：它让"下节课交""这门课"这类相对指代能被
解析成具体日期。所以这里的输出会直接进 LLM 的用户消息，准确性要求高于一般列表接口。

``weeks`` 字段支持三种写法，都必须支持：``"1-16"``、``"1,3,5"``、``"2-8,10"``。
解析失败时**不抛错**而是当作"全周生效"并打点——课表里一行写错不该让整个识别功能挂掉，
但用户需要能从日志看出是哪一行有问题。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from app.db import transaction
from app.logging import get_logger, kv
from app.text import clean_text
from app.timeutil import (
    combine_local,
    now_utc_iso,
    parse_clock,
    term_week,
    tz,
)

log = get_logger("timetable")

WEEKDAY_NAMES = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


class TimetableError(ValueError):
    """课表数据不合法。"""


# --------------------------------------------------------------------------- #
# weeks 解析
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class WeekRange:
    raw: str
    weeks: frozenset[int]
    fallback: bool = False

    def covers(self, week: int) -> bool:
        if self.fallback:
            return True
        return week in self.weeks


def parse_weeks(raw: str, *, total_weeks: int = 30) -> WeekRange:
    """``"1-16"`` / ``"1,3,5"`` / ``"2-8,10"`` → 周次集合。"""
    text = (raw or "").strip()
    if not text or text in ("*", "all", "全部"):
        return WeekRange(raw=text, weeks=frozenset(), fallback=True)

    weeks: set[int] = set()
    for chunk in text.replace("，", ",").replace("－", "-").split(","):
        piece = chunk.strip()
        if not piece:
            continue
        if "-" in piece:
            left, _, right = piece.partition("-")
            try:
                start, end = int(left.strip()), int(right.strip())
            except ValueError:
                return _fallback(raw, reason="range_not_int")
            if start > end:
                start, end = end, start
            weeks.update(range(max(1, start), min(total_weeks, end) + 1))
        else:
            try:
                weeks.add(int(piece))
            except ValueError:
                return _fallback(raw, reason="not_int")

    if not weeks:
        return _fallback(raw, reason="empty")
    return WeekRange(raw=text, weeks=frozenset(w for w in weeks if 1 <= w <= total_weeks))


def _fallback(raw: str, *, reason: str) -> WeekRange:
    """解析失败 <p>不抛错</p>，改为"全周生效"并打点。

    取舍：课表里一行写错（比如手输成 "1~16"）如果让整个识别接口 500，
    用户会以为系统坏了；而当"全周生效"，最坏情况只是上下文多写一行课。
    后者可恢复，前者不可用。
    """
    log.warning("timetable.weeks_fallback %s", kv(raw=raw, reason=reason))
    return WeekRange(raw=raw, weeks=frozenset(), fallback=True)


# --------------------------------------------------------------------------- #
# 课程与时段
# --------------------------------------------------------------------------- #
@dataclass
class Session:
    id: int
    course_id: int
    weekday: int
    start_time: str
    end_time: str
    weeks: str
    location: str = ""
    course_name: str = ""

    def start_local(self, day: date, tz_name: str) -> datetime:
        return combine_local(day, parse_clock(self.start_time), tz_name)

    def end_local(self, day: date, tz_name: str) -> datetime:
        return combine_local(day, parse_clock(self.end_time), tz_name)


@dataclass
class Course:
    id: int
    name: str
    teacher: str = ""
    location: str = ""
    color: str = ""
    note: str = ""
    sessions: list[Session] = field(default_factory=list)


def _validate_weekday(weekday: int) -> int:
    value = int(weekday)
    if not 1 <= value <= 7:
        raise TimetableError(f"weekday 必须是 1(周一)–7(周日)，收到 {value}")
    return value


def _validate_times(start: str, end: str) -> tuple[str, str]:
    try:
        start_clock = parse_clock(start)
        end_clock = parse_clock(end)
    except ValueError as exc:
        raise TimetableError(f"上课时间格式应为 HH:MM，收到 {start!r}–{end!r}") from exc
    if start_clock >= end_clock:
        raise TimetableError(f"下课时间必须晚于上课时间：{start}–{end}")
    return start_clock.strftime("%H:%M"), end_clock.strftime("%H:%M")


# --------------------------------------------------------------------------- #
# 读取
# --------------------------------------------------------------------------- #
def list_courses(con: sqlite3.Connection) -> list[Course]:
    rows = con.execute("SELECT * FROM courses ORDER BY name").fetchall()
    sessions_by_course: dict[int, list[Session]] = {}
    for row in con.execute(
        "SELECT s.*, c.name AS course_name FROM course_sessions s"
        " JOIN courses c ON c.id = s.course_id"
        " ORDER BY s.weekday, s.start_time"
    ):
        sessions_by_course.setdefault(row["course_id"], []).append(
            Session(
                id=row["id"],
                course_id=row["course_id"],
                weekday=row["weekday"],
                start_time=row["start_time"],
                end_time=row["end_time"],
                weeks=row["weeks"],
                location=row["location"],
                course_name=row["course_name"],
            )
        )
    return [
        Course(
            id=row["id"],
            name=row["name"],
            teacher=row["teacher"],
            location=row["location"],
            color=row["color"],
            note=row["note"],
            sessions=sessions_by_course.get(row["id"], []),
        )
        for row in rows
    ]


def get_course(con: sqlite3.Connection, course_id: int) -> Course | None:
    return next((c for c in list_courses(con) if c.id == course_id), None)


# --------------------------------------------------------------------------- #
# 写入
# --------------------------------------------------------------------------- #
def replace_timetable(con: sqlite3.Connection, payload: list[dict]) -> list[Course]:
    """整体替换课表。

    选"整体替换"而不是逐条增删改的 API：课表在 UI 里是**整张周表**编辑的，
    用户改完一次提交；这样做同时避免了"删了两门课、改了三门课"这种部分失败的中间态。
    """
    prepared: list[tuple[str, str, str, str, str, list[dict]]] = []
    seen: set[str] = set()

    for entry in payload:
        name = clean_text(entry.get("name"), limit=60, allow_empty=False)
        if name in seen:
            raise TimetableError(f"课程名重复：{name}")
        seen.add(name)
        sessions: list[dict] = []
        for session in entry.get("sessions", []) or []:
            weekday = _validate_weekday(session.get("weekday"))
            start, end = _validate_times(
                str(session.get("start_time", "")), str(session.get("end_time", ""))
            )
            weeks = clean_text(session.get("weeks") or "", limit=64)
            parse_weeks(weeks)  # 触发一次校验与打点
            sessions.append(
                {
                    "weekday": weekday,
                    "start_time": start,
                    "end_time": end,
                    "weeks": weeks,
                    "location": clean_text(session.get("location") or "", limit=60),
                }
            )
        prepared.append(
            (
                name,
                clean_text(entry.get("teacher") or "", limit=60),
                clean_text(entry.get("location") or "", limit=60),
                clean_text(entry.get("color") or "", limit=20),
                clean_text(entry.get("note") or "", limit=200),
                sessions,
            )
        )

    now = now_utc_iso()
    with transaction(con):
        con.execute("DELETE FROM course_sessions")
        con.execute("DELETE FROM courses")
        for name, teacher, location, color, note, sessions in prepared:
            cursor = con.execute(
                "INSERT INTO courses (name, teacher, location, color, note, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (name, teacher, location, color, note, now, now),
            )
            course_id = int(cursor.lastrowid)
            for session in sessions:
                con.execute(
                    "INSERT INTO course_sessions"
                    " (course_id, weekday, start_time, end_time, weeks, location)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        course_id,
                        session["weekday"],
                        session["start_time"],
                        session["end_time"],
                        session["weeks"],
                        session["location"],
                    ),
                )

    log.info("timetable.replaced %s", kv(courses=len(prepared)))
    return list_courses(con)


# --------------------------------------------------------------------------- #
# 「现在上什么课」
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class CurrentCourse:
    course_name: str
    location: str
    weekday: int
    start_time: str
    end_time: str
    #: "ongoing"（正在上）/ "just_ended"（刚下课）/ "upcoming"（即将开始）
    relation: str
    #: 与"现在"的时间差（分钟）；ongoing 时为 0
    minutes_away: int
    #: 对应的具体日期
    day: date


def find_around(
    courses: list[Course],
    now: datetime,
    *,
    start_date: date,
    total_weeks: int,
    tz_name: str,
    lookahead_minutes: int = 120,
    just_ended_minutes: int = 30,
) -> list[CurrentCourse]:
    """找出"正在上 / 刚结束 / 即将开始"的课。

    窗口是**刻意**选的：默认看未来 120 分钟、回看 30 分钟。
    太短会让"下节课交作业"在课间休息时失去上下文；太长会把明天的课也扯进来，
    反而让模型把"下节课"解析成错误的日期。这两个数字是可以调的，
    但它们必须是**显式参数**而不是散在代码里的魔数。
    """
    local_now = now.astimezone(tz(tz_name))
    local_date = local_now.date()
    results: list[CurrentCourse] = []

    # 同时看今天与明天：晚上 23:30 时"明天早上 8 点的课"也在 lookahead 窗口内。
    # 假期里的日期由 week 范围天然排除（term_week 返回 ≤0 或 >total_weeks）。
    for day_offset in (0, 1):
        day = local_date + timedelta(days=day_offset)
        day_week = term_week(day, start_date)
        if day_week <= 0 or day_week > total_weeks:
            continue
        for course in courses:
            for session in course.sessions:
                if session.weekday != day.isoweekday():
                    continue
                if not parse_weeks(session.weeks, total_weeks=total_weeks).covers(day_week):
                    continue
                start = session.start_local(day, tz_name)
                end = session.end_local(day, tz_name)
                location = session.location or course.location
                if start <= local_now <= end:
                    relation, minutes = "ongoing", 0
                elif end < local_now <= end + timedelta(minutes=just_ended_minutes):
                    relation = "just_ended"
                    minutes = int((end - local_now).total_seconds() // 60)
                elif local_now < start <= local_now + timedelta(minutes=lookahead_minutes):
                    relation = "upcoming"
                    minutes = int((start - local_now).total_seconds() // 60)
                else:
                    continue
                results.append(
                    CurrentCourse(
                        course_name=course.name,
                        location=location,
                        weekday=session.weekday,
                        start_time=session.start_time,
                        end_time=session.end_time,
                        relation=relation,
                        minutes_away=minutes,
                        day=day,
                    )
                )

    order = {"ongoing": 0, "just_ended": 1, "upcoming": 2}
    return sorted(results, key=lambda item: (order[item.relation], item.minutes_away))


def weekday_schedule(
    courses: list[Course], day: date, *, total_weeks: int, start_date: date
) -> list[Session]:
    """某一天的全部课程（按时间排序）。给"今日课表"与提示词用。"""
    week = term_week(day, start_date)
    if week <= 0 or week > total_weeks:
        return []
    result: list[Session] = []
    for course in courses:
        for session in course.sessions:
            if session.weekday != day.isoweekday():
                continue
            if parse_weeks(session.weeks, total_weeks=total_weeks).covers(week):
                result.append(session)
    return sorted(result, key=lambda item: item.start_time)


def week_matrix(courses: list[Course]) -> dict[str, list[dict]]:
    """整周课表（不含周次判定），给 UI 渲染周视图网格。"""
    matrix: dict[str, list[dict]] = {name: [] for name in WEEKDAY_NAMES}
    for course in courses:
        for session in course.sessions:
            matrix[WEEKDAY_NAMES[session.weekday - 1]].append(
                {
                    "course_id": course.id,
                    "course_name": course.name,
                    "session_id": session.id,
                    "start_time": session.start_time,
                    "end_time": session.end_time,
                    "weeks": session.weeks,
                    "location": session.location or course.location,
                    "color": course.color,
                }
            )
    for name in matrix:
        matrix[name].sort(key=lambda item: item["start_time"])
    return matrix
