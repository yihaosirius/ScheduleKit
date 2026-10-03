"""时间处理。

三条约定（AGENTS.md §2）：

1. **库内一律 UTC ISO8601**；只有渲染层做时区换算。所以这里
   ``to_utc_iso`` / ``parse_utc`` 是唯一的进出口。
2. **模型返回的裸时间必须有偏移量**。``parse_local_or_offset`` 对没有偏移量的
   字符串按 ``server.timezone`` 解释（DeepSeek 偶尔会漏掉偏移），但会把
   "我替你补了时区"这件事回报给调用方，由 normalize 打点——静默纠正时间是
   最危险的一类错误（差 8 小时的任务看起来完全正常）。
3. 学期周次用**日期差值**算，不用 ``isocalendar()``：后者是 ISO 周编号，
   与"第几教学周"在跨年、非周一起始的学期里会对不上。
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

UTC = timezone.utc

#: 用户输入与 LLM 输出里常见的几种日期时间写法，按特化程度从高到低尝试。
_ACCEPTED_FORMATS = (
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y/%m/%d %H:%M:%S",
    "%Y/%m/%d %H:%M",
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%Y年%m月%d日 %H:%M",
    "%Y年%m月%d日",
)


class TimeParseError(ValueError):
    """时间字符串无法解释成带时区的时刻。"""


def tz(name: str) -> ZoneInfo:
    """取时区。``Asia/Shanghai`` 在 Windows 上依赖 ``tzdata`` 包（已在依赖里）。"""
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        # 配置里写错时区名不该让服务起不来，但要在调用点能看出异常——
        # 所以这里只回退，由 status 接口把时区可用性报出来。
        return ZoneInfo("UTC")


def now_utc() -> datetime:
    return datetime.now(UTC)


def now_utc_iso() -> str:
    """库内写时间统一走这个。秒级精度足够，且比微秒更容易肉眼比对。"""
    return to_utc_iso(now_utc())


def to_utc_iso(moment: datetime) -> str:
    """带时区的 datetime → UTC ISO8601 文本。裸 datetime 视为 UTC。"""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).replace(microsecond=0).isoformat()


def parse_utc(text: str) -> datetime:
    """读库内时间。非法值直接抛错——读到脏数据还继续算下去只会放大问题。"""
    value = str(text).strip()
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(value)
    except ValueError as exc:
        raise TimeParseError(f"无法解析时间：{text!r}") from exc
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def parse_local_or_offset(text: str, tz_name: str) -> tuple[datetime, bool]:
    """解析模型/用户给的时间，返回 ``(UTC 时刻, 是否由我们补了时区)``。

    第二个返回值不是装饰：补时区意味着"任务差 8 小时"这类错误最容易发生的地方，
    调用方必须把它记进 overrides 并打点。
    """
    value = str(text).strip()
    if not value:
        raise TimeParseError("时间为空")

    normalized = value.replace("Z", "+00:00") if value.endswith("Z") else value

    # 先试标准 ISO（能带上偏移量或时区名的写法）
    try:
        moment = datetime.fromisoformat(normalized)
    except ValueError:
        moment = None

    if moment is None:
        for fmt in _ACCEPTED_FORMATS:
            try:
                moment = datetime.strptime(value, fmt)
                break
            except ValueError:
                continue

    if moment is None:
        raise TimeParseError(f"无法解析时间：{text!r}")

    if moment.tzinfo is not None:
        return moment.astimezone(UTC), False

    # 裸时间：按本地时区解释，并报告"补了时区"
    local = moment.replace(tzinfo=tz(tz_name))
    return local.astimezone(UTC), True


# --------------------------------------------------------------------------- #
# 学期周次
# --------------------------------------------------------------------------- #
def term_week(day: date, start_date: date) -> int:
    """教学周次。第 1 周 = ``start_date`` 所在的周。

    ``start_date`` 是"第 1 周的周一"（配置里写死）。用日期差而不是 ISO 周编号，
    是为了让"第几周"的语义完全由配置决定，跨年也不出意外。
    """
    delta = (day - start_date).days
    return delta // 7 + 1


def term_week_now(now: datetime, start_date: date, tz_name: str) -> int:
    return term_week(now.astimezone(tz(tz_name)).date(), start_date)


def week_dates(week: int, start_date: date) -> tuple[date, date]:
    """某一教学周的 (周一, 周日)。"""
    monday = start_date + timedelta(days=(week - 1) * 7)
    return monday, monday + timedelta(days=6)


# --------------------------------------------------------------------------- #
# 渲染辅助（前端模板/接口共用）
# --------------------------------------------------------------------------- #
def local_iso(moment: datetime, tz_name: str) -> str:
    return moment.astimezone(tz(tz_name)).replace(microsecond=0).isoformat()


def combine_local(day: date, clock: time, tz_name: str) -> datetime:
    """把"某天 + 某时刻"组合成带时区的 datetime（本地时区）。"""
    return datetime.combine(day, clock).replace(tzinfo=tz(tz_name))


def parse_clock(text: str) -> time:
    """``"08:00"`` / ``"8:00"`` → :class:`datetime.time`。"""
    value = str(text).strip()
    for fmt in ("%H:%M", "%H:%M:%S"):
        try:
            return datetime.strptime(value, fmt).time()
        except ValueError:
            continue
    raise TimeParseError(f"无法解析时刻：{text!r}")


def human_delta(moment: datetime, now: datetime) -> str:
    """"3 天后"这类相对描述。UI 与提示词都用它，避免两处口径不一致。"""
    seconds = (moment - now).total_seconds()
    future = seconds >= 0
    seconds = abs(seconds)
    if seconds < 60:
        return "刚刚" if not future else "1 分钟内"
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes} 分钟{'后' if future else '前'}"
    hours = int(minutes // 60)
    if hours < 24:
        return f"{hours} 小时{'后' if future else '前'}"
    days = int(hours // 24)
    if days < 30:
        return f"{days} 天{'后' if future else '前'}"
    months = int(days // 30)
    return f"{months} 个月{'后' if future else '前'}"
