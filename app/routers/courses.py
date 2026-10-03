"""课表接口。

课表在 UI 里是**整张周表**编辑的（改完一次提交），所以这里只有 GET / PUT，
没有逐条增删改。这样做的额外好处是没有"删了两门、改了三门"的部分失败中间态。
"""

from __future__ import annotations

import sqlite3
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException

from app.config import Config
from app.deps import ReadAuth, WriteAuth, get_config, get_db
from app.logging import get_logger, kv
from app.schemas import CourseOut, SessionOut, TimetableIn, TimetableOut
from app.services import timetable as timetable_service
from app.services.timetable import Course, TimetableError
from app.timeutil import now_utc, now_utc_iso, parse_utc, term_week

log = get_logger("api.courses")

#: 关系的展示文案。放在路由层是因为它只影响 HTTP 输出，
#: 服务层返回的是稳定的 relation 枚举值。
RELATION_LABELS = {"ongoing": "正在上", "just_ended": "刚下课", "upcoming": "即将开始"}

router = APIRouter()
#: 同一天课表挂在 /api/timetable 下，与 /api/courses 分开是因为它们的语义不同：
#: courses 是"有哪些课"，timetable 是"整周怎么排"。
timetable_router = APIRouter()


def course_to_out(course: Course) -> CourseOut:
    return CourseOut(
        id=course.id,
        name=course.name,
        teacher=course.teacher,
        location=course.location,
        color=course.color,
        note=course.note,
        sessions=[
            SessionOut(
                id=session.id,
                course_id=session.course_id,
                course_name=session.course_name or course.name,
                weekday=session.weekday,
                start_time=session.start_time,
                end_time=session.end_time,
                weeks=session.weeks,
                location=session.location,
            )
            for session in course.sessions
        ],
    )


def _handle(exc: TimetableError) -> HTTPException:
    return HTTPException(status_code=422, detail=str(exc))


def _current_week(cfg: Config) -> int:
    """当前教学周；学期起始日配错时返回 0（视为假期）而不是抛错。"""
    start = _term_start(cfg)
    if start is None:
        return 0
    return term_week(now_utc().astimezone(_tz_of(cfg)).date(), start)


def _term_start(cfg: Config):
    try:
        return parse_utc(cfg.term.start_date).date()
    except Exception:
        log.warning("courses.term_start_unparseable %s", kv(raw=cfg.term.start_date))
        return None


def _tz_of(cfg: Config):
    from app.timeutil import tz

    return tz(cfg.server.timezone)


@router.get("", response_model=list[CourseOut], summary="读取课程列表")
async def list_courses(
    principal: ReadAuth,
    con: Annotated[sqlite3.Connection, Depends(get_db)],
) -> list[CourseOut]:
    return [course_to_out(course) for course in timetable_service.list_courses(con)]


@router.put("", response_model=list[CourseOut], summary="读取/整体替换课程与时段")
async def replace_courses(
    payload: TimetableIn,
    principal: WriteAuth,
    con: Annotated[sqlite3.Connection, Depends(get_db)],
) -> list[CourseOut]:
    try:
        courses = timetable_service.replace_timetable(
            con, [entry.model_dump() for entry in payload.courses]
        )
    except TimetableError as exc:
        raise _handle(exc) from exc
    return [course_to_out(course) for course in courses]


@timetable_router.get("", response_model=TimetableOut, summary="读取整周课表")
async def get_timetable(
    principal: ReadAuth,
    con: Annotated[sqlite3.Connection, Depends(get_db)],
    cfg: Annotated[Config, Depends(get_config)],
) -> TimetableOut:
    courses = timetable_service.list_courses(con)
    return TimetableOut(
        courses=[course_to_out(course) for course in courses],
        matrix=timetable_service.week_matrix(courses),
        term={
            "start_date": cfg.term.start_date,
            "total_weeks": cfg.term.total_weeks,
            "current_week": _current_week(cfg),
        },
        server_time=now_utc_iso(),
    )


@timetable_router.put("", response_model=TimetableOut, summary="整体替换整周课表")
async def put_timetable(
    payload: TimetableIn,
    principal: WriteAuth,
    con: Annotated[sqlite3.Connection, Depends(get_db)],
    cfg: Annotated[Config, Depends(get_config)],
) -> TimetableOut:
    try:
        courses = timetable_service.replace_timetable(
            con, [entry.model_dump() for entry in payload.courses]
        )
    except TimetableError as exc:
        raise _handle(exc) from exc
    log.info("timetable.saved %s", kv(courses=len(courses)))
    return TimetableOut(
        courses=[course_to_out(course) for course in courses],
        matrix=timetable_service.week_matrix(courses),
        term={
            "start_date": cfg.term.start_date,
            "total_weeks": cfg.term.total_weeks,
            "current_week": _current_week(cfg),
        },
        server_time=now_utc_iso(),
    )


@timetable_router.get("/now", summary="现在正在上/刚下课/即将开始的课")
async def now_around(
    principal: ReadAuth,
    con: Annotated[sqlite3.Connection, Depends(get_db)],
    cfg: Annotated[Config, Depends(get_config)],
) -> dict[str, object]:
    """首页顶栏的"第 N 周 · 正在上：课程名"由它驱动。

    服务端算而不是前端算：周次与"正在上"的判定依赖配置里的学期起始日，
    前端各自实现会与服务端注入提示词的口径不一致——那种不一致最难排查，
    因为两边看起来都对。
    """
    courses = timetable_service.list_courses(con)
    around = timetable_service.find_around(
        courses,
        now_utc(),
        start_date=_term_start(cfg) or now_utc().astimezone(_tz_of(cfg)).date(),
        total_weeks=cfg.term.total_weeks,
        tz_name=cfg.server.timezone,
    )
    return {
        "week": _current_week(cfg),
        "total_weeks": cfg.term.total_weeks,
        "around": [
            {
                "course_name": item.course_name,
                "location": item.location,
                "relation": item.relation,
                "relation_label": RELATION_LABELS[item.relation],
                "minutes_away": item.minutes_away,
                "start_time": item.start_time,
                "end_time": item.end_time,
                "day": item.day.isoformat(),
            }
            for item in around
        ],
        "server_time": now_utc_iso(),
    }
