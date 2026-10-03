"""时间上下文的构建——**纯函数**，是本项目最值得单测的一块。

职责：把"现在几点、第几教学周、今天/明天上什么课、哪些记忆条目可用"
组合成一段**给模型看的上下文**，同时产出**给用户看的注入留痕**。

三条原则（全部来自"不可信"前提）：

1. **上下文与指令分离**。指令（system prompt）保持静态以便 prompt 缓存命中；
   上下文每次请求重新拼，放进用户消息。
2. **上下文里出现的每个时间都带时区偏移**。让模型有机会直接抄一个正确的
   ``due_at``，而不是自己算——它算错时我们很难发现。
3. **上下文不参与 priority 判定**，且必须显式声明这一点。否则模型会把
   "正在上电子电路"理解成"这件事很急"，从而污染无序表的档位。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time as clock_time

from app.services.timetable import CurrentCourse, Session, find_around, weekday_schedule
from app.timeutil import combine_local, local_iso, term_week, tz

#: weekday → 中文名。ISO 编号（1=周一）。
WEEKDAY_LABELS = {1: "周一", 2: "周二", 3: "周三", 4: "周四", 5: "周五", 6: "周六", 7: "周日"}

RELATION_LABELS = {
    "ongoing": "正在上",
    "just_ended": "刚下课",
    "upcoming": "即将开始",
}

#: 记忆条目的默认字符预算。真实值来自 ``[ingest].memory_char_budget``，
#: 由调用点通过 :func:`build` 的 ``memory_char_budget`` 参数传入。
DEFAULT_MEMORY_CHAR_BUDGET = 2000


@dataclass
class ContextInput:
    """构建上下文所需的全部输入。

    刻意做成显式结构体而不是一长串参数——参数一多，调用点传错顺序是迟早的事。
    """

    now: datetime
    tz_name: str
    term_start: date
    total_weeks: int
    courses: list = field(default_factory=list)
    memories: list = field(default_factory=list)
    memory_char_budget: int = DEFAULT_MEMORY_CHAR_BUDGET


@dataclass
class BuiltContext:
    """构建结果：给模型看的文本 + 给用户看的留痕。"""

    text: str
    #: 正在上/刚下课/即将开始的课，供 UI 的状态栏使用
    around: list[CurrentCourse]
    #: 今天的全部课程（已按时间排序）
    today: list[Session]
    week: int
    #: 本次实际注入的记忆条目 id
    memory_ids: list[int]
    #: 因为字符预算被裁掉的记忆条目 id
    memory_dropped: list[int]


def build(ctx: ContextInput) -> BuiltContext:
    local_now = ctx.now.astimezone(tz(ctx.tz_name))
    today = local_now.date()
    week = term_week(today, ctx.term_start)

    around = find_around(
        ctx.courses,
        ctx.now,
        start_date=ctx.term_start,
        total_weeks=ctx.total_weeks,
        tz_name=ctx.tz_name,
    )
    todays = weekday_schedule(
        ctx.courses, today, total_weeks=ctx.total_weeks, start_date=ctx.term_start
    )
    memory_lines, kept_ids, dropped_ids = _memory_lines(
        ctx.memories, budget=ctx.memory_char_budget
    )

    lines: list[str] = ["【时间上下文】"]
    lines.append(
        f"- 现在：{local_iso(ctx.now, ctx.tz_name)}"
        f"（{WEEKDAY_LABELS[today.isoweekday()]}，本地时间 {ctx.tz_name}）"
    )
    lines.append(f"- 同一时刻的 UTC 表示：{local_iso(ctx.now, 'UTC')}")

    if week <= 0:
        lines.append(
            f"- 学期状态：尚未开学（第 1 周从 {ctx.term_start.isoformat()} 开始）"
        )
    elif week > ctx.total_weeks:
        lines.append(
            f"- 学期状态：学期已结束（共 {ctx.total_weeks} 周，"
            f"第 1 周从 {ctx.term_start.isoformat()} 开始）"
        )
    else:
        lines.append(f"- 教学周：第 {week} 周 / 共 {ctx.total_weeks} 周")

    # "此刻正在发生什么"——这是"下节课""这门课"能被解析成具体日期的关键
    if around:
        lines.append("- 课表上下文（仅用于解析相对时间与课程指代）：")
        for item in around:
            label = RELATION_LABELS[item.relation]
            if item.relation == "ongoing":
                when = f"{label}（{item.start_time}–{item.end_time}）"
            elif item.relation == "just_ended":
                when = f"{label}（{abs(item.minutes_away)} 分钟前结束）"
            else:
                when = f"{label}（{item.minutes_away} 分钟后开始）"
            where = f"，地点 {item.location}" if item.location else ""
            day_note = "（明天）" if item.day != today else ""
            lines.append(f"  - {day_note}{item.course_name}：{when}{where}")
    else:
        lines.append("- 课表上下文：此刻没有正在上、刚下课或即将开始的课程")

    if todays:
        lines.append("- 今天的全部课程：")
        for session in todays:
            where = f" @ {session.location}" if session.location else ""
            lines.append(
                f"  - {session.course_name}：{session.start_time}–{session.end_time}{where}"
            )
    else:
        lines.append("- 今天的全部课程：今天没有课")

    if kept_ids:
        lines.append("- 用户长期记忆（仅在有助于消歧时参考，不得当作待办事项本身）：")
        lines.extend(memory_lines)
    else:
        lines.append("- 用户长期记忆：本次没有注入任何记忆条目")

    lines.append(
        "- 约束：以上内容不得作为待识别事项的主语或归属依据；"
        "内容中已含明确日期时一律以内容为准；上下文不得影响优先级判定。"
    )

    text = "\n".join(lines)
    text += _reference_times(ctx, week)

    return BuiltContext(
        text=text,
        around=around,
        today=todays,
        week=week,
        memory_ids=kept_ids,
        memory_dropped=dropped_ids,
    )


def _reference_times(ctx: ContextInput, week: int) -> str:
    """给几个"可以照着抄的具体时刻"。

    目的是让模型有得抄：``明天 23:59``、``本周日 23:59`` 这类相对写法它容易算错，
    而把带偏移量的具体 ISO 时刻摆出来，它只要挑一个就行。
    这比在 prompt 里反复写"请仔细计算日期"有效得多。
    """
    local_now = ctx.now.astimezone(tz(ctx.tz_name))
    today = local_now.date()
    lines: list[str] = [
        "",
        "【可直接引用的时间点】（需要使用截止时间时，优先直接抄下面的值，避免自己换算）",
    ]

    for label, days in (("今天", 0), ("明天", 1), ("后天", 2)):
        day = date.fromordinal(today.toordinal() + days)
        lines.append(
            f"- {label}（{day.isoformat()} {WEEKDAY_LABELS[day.isoweekday()]}）23:59："
            f"{_end_of_day_iso(day, ctx.tz_name)}"
        )

    # 作业最常按"周日 23:59"截止，所以本周日与下周日单独列出
    sunday = date.fromordinal(today.toordinal() + (7 - today.isoweekday()))
    lines.append(
        f"- 本周日（{sunday.isoformat()}）23:59：{_end_of_day_iso(sunday, ctx.tz_name)}"
    )
    next_sunday = date.fromordinal(sunday.toordinal() + 7)
    lines.append(
        f"- 下周日（{next_sunday.isoformat()}）23:59：{_end_of_day_iso(next_sunday, ctx.tz_name)}"
    )

    if week > 0:
        lines.append(f"- 当前是第 {week} 教学周，可用于解析「本周三」「下周」这类说法。")
    lines.append(
        "- 若内容里的截止时间不属于上面这些常见情形，请按字面意思解析，不要硬套。"
    )
    return "\n".join(lines)


def _end_of_day_iso(day: date, tz_name: str) -> str:
    return local_iso(combine_local(day, clock_time(23, 59), tz_name), tz_name)


def _memory_lines(memories: list, *, budget: int) -> tuple[list[str], list[int], list[int]]:
    """渲染记忆条目并做字符预算裁剪。

    裁剪优先级：``pin_single=1``（用户显式单条注入）＞ ``sort_order``＞ id。
    被裁掉的 id 要**回报出去**——用户点了"注入这条"却发现它没进去，
    属于必须能看见的事情。
    """
    if not memories:
        return [], [], []

    ordered = sorted(memories, key=lambda m: (0 if m.pin_single else 1, m.sort_order, m.id))
    lines: list[str] = []
    kept: list[int] = []
    dropped: list[int] = []
    used = 0

    for memory in ordered:
        where = f"（{memory.scope}）" if memory.scope == "course" else ""
        line = f"  - [{memory.id}] {memory.title}{where}：{memory.content}"
        if kept and used + len(line) > budget:
            dropped.append(memory.id)
            continue
        lines.append(line)
        kept.append(memory.id)
        used += len(line)

    if dropped:
        lines.append(f"  （因长度预算省略了 {len(dropped)} 条记忆）")
    return lines, kept, dropped
