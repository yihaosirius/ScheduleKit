"""录入编排：一次识别请求的完整链路。

链路顺序（每一步都有理由，改动前先读）：

1. **记录来源渠道**（web / shortcut / api）——状态监测里要区分它们，
   因为"快捷指令的失败率比网页高"是可能发生的（网络与图片质量都更差）。
2. **图片先落盘再建草稿**：万一 LLM 调用超时，图片还在，用户可以在草稿箱里重试识别
   而不必重新拍照。
3. **建上下文**（时间 + 课表 + 记忆），并把记忆**挑选**过程留痕。
4. **调 LLM**（三通道编排在 app/llm/structured.py）。
5. **normalize**（不可信前提的落地点），输出 items + overrides。
6. **建草稿**（``draft`` 阶段 ``items`` 表零写入）。
7. **清掉本次用过的一次性"单条注入"标记**。

第 7 步容易被忽略：``pin_single`` 是"这一次"的意图而不是持久偏好。
不清掉的话，用户上次点的那条记忆会一直参与每次识别，他会以为开关坏了。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from app.config import Config
from app.llm.base import LLMError, LLMRequest
from app.logging import get_logger, kv
from app.services import context as context_service
from app.services import drafts as drafts_service
from app.services import memory as memory_service
from app.services import normalize as normalize_service
from app.services import timetable as timetable_service
from app.services.timetable import CurrentCourse
from app.timeutil import now_utc, parse_utc

log = get_logger("ingest")

#: 用户消息里"待识别内容"的起始标记。
#: **它必须与 app/llm/mock.py 里的同名常量一致**：mock 用同一个标记切分内容，
#: 这样测试测的是真实的消息结构，而不是一个只有 mock 见过的形状。
CONTENT_MARKER = "【待识别内容】"


class IngestError(ValueError):
    """录入请求本身不合法（不是 LLM 的错）。"""


@dataclass
class Intake:
    """一次录入的输入。"""

    #: 纯文字可以为空——只要有图片
    text: str = ""
    #: 已经过 media.store_image 校验并落盘的图片信息
    image_path: str | None = None
    image_mime: str | None = None
    image_bytes: bytes | None = None
    image_size: int | None = None
    image_sha256: str | None = None
    channel: str = "web"
    #: 本次显式要求注入的记忆条目（快捷指令可用)
    manual_memory_ids: list[int] = field(default_factory=list)


@dataclass
class IntakeResult:
    draft: drafts_service.Draft
    items: list[dict[str, Any]]
    #: 本次注入的记忆
    memory_ids: list[int]
    #: 因预算被裁掉的记忆
    memory_dropped: list[int]
    #: 此刻正在上/刚下课/即将开始的课，供 UI 状态栏
    around: list[CurrentCourse]
    week: int
    context_chars: int
    overrides: list[str] = field(default_factory=list)


def compose_user_message(context_text: str, content: str) -> str:
    """拼出用户消息：上下文在前，待识别内容在后并加标记。

    顺序很关键：上下文在前符合"先给背景再给材料"的阅读顺序，
    而标记让内容与上下文在结构上分开——两者混在一起时，模型容易把
    上下文里的课程名当成待办事项的一部分。
    """
    body = content.strip() or "（本次没有文字内容，请只根据图片识别）"
    return f"{context_text}\n\n{CONTENT_MARKER}\n{body}"


async def run_intake(
    cfg: Config,
    con: sqlite3.Connection,
    intake: Intake,
    *,
    llm=None,
) -> IntakeResult:
    """执行一次识别并生成草稿。"""
    if not intake.text.strip() and not intake.image_path:
        raise IngestError("必须提供文字或图片中的至少一项。")

    now = now_utc()
    term_start = _term_start(cfg)
    courses = timetable_service.list_courses(con)

    # 哪门课"出现在本次上下文里"：正在上/刚下课/即将开始的课。
    # 只有这些课关联的 scope='course' 记忆才注入——否则"复变的作业周三交"
    # 会在识别英语作业时也被塞进去，增加干扰。
    around_preview = timetable_service.find_around(
        courses,
        now,
        start_date=term_start,
        total_weeks=cfg.term.total_weeks,
        tz_name=cfg.server.timezone,
    )
    course_ids_in_context = {
        course.id
        for course in courses
        if any(item.course_name == course.name for item in around_preview)
    }
    memories = memory_service.select_for_injection(
        con,
        course_ids_in_context=course_ids_in_context,
        manual_ids=list(intake.manual_memory_ids),
    )

    built = context_service.build(
        context_service.ContextInput(
            now=now,
            tz_name=cfg.server.timezone,
            term_start=term_start,
            total_weeks=cfg.term.total_weeks,
            courses=courses,
            memories=memories,
            memory_char_budget=cfg.ingest.memory_char_budget,
        )
    )

    user_text = compose_user_message(built.text, intake.text)
    images = [intake.image_bytes] if intake.image_bytes else []
    mimes = [intake.image_mime or "image/jpeg"] if intake.image_bytes else []

    reader = llm or _build_llm(cfg)
    try:
        result = await reader.extract(
            LLMRequest(
                system=cfg.llm.system_prompt,
                user_text=user_text,
                images=images,
                image_mimes=mimes,
                image_count=len(images),
            )
        )
    except LLMError as exc:
        # 失败也要**留下图片与文字**：用户拍了照却因为模型抽风而丢掉材料，
        # 是比"这次识别失败"糟糕得多的结果。
        draft = drafts_service.create_draft(
            con,
            channel=intake.channel,
            text_input=intake.text,
            items=[],
            memory_ids=built.memory_ids,
            llm_path="tool_call",
            llm_model=cfg.llm.model,
            llm_elapsed_ms=0.0,
            raw_output="",
            fallback_note=f"识别失败：{exc}",
            overrides=f"识别失败，草稿里没有解析结果：{exc}",
            image_path=intake.image_path,
            image_mime=intake.image_mime,
            image_bytes=intake.image_size,
            image_sha256=intake.image_sha256,
            ttl_hours=cfg.ingest.confirm_ttl_hours,
        )
        log.error(
            "ingest.llm_failed %s",
            kv(channel=intake.channel, error=type(exc).__name__, detail=str(exc)),
        )
        return IntakeResult(
            draft=draft,
            items=[],
            memory_ids=built.memory_ids,
            memory_dropped=built.memory_dropped,
            around=built.around,
            week=built.week,
            context_chars=len(built.text),
            overrides=[f"识别失败：{exc}"],
        )

    normalized = normalize_service.normalize_items(
        result.items, now=now, tz_name=cfg.server.timezone, max_items=cfg.ingest.max_items
    )

    overrides = list(normalized.overrides)
    if normalized.dropped:
        overrides.append("丢弃的条目：" + "；".join(normalized.dropped))
    if built.memory_dropped:
        overrides.append(
            f"因长度预算省略了 {len(built.memory_dropped)} 条记忆"
            f"（id：{', '.join(str(i) for i in built.memory_dropped)}）"
        )
    if result.fallback_note:
        overrides.append(result.fallback_note)

    draft = drafts_service.create_draft(
        con,
        channel=intake.channel,
        text_input=intake.text,
        items=normalized.items,
        memory_ids=built.memory_ids,
        llm_path=result.path,
        llm_model=result.model or cfg.llm.model,
        llm_elapsed_ms=result.elapsed_ms,
        raw_output=result.raw,
        fallback_note=result.fallback_note,
        overrides="；".join(overrides),
        image_path=intake.image_path,
        image_mime=intake.image_mime,
        image_bytes=intake.image_size,
        image_sha256=intake.image_sha256,
        ttl_hours=cfg.ingest.confirm_ttl_hours,
    )

    # 一次性意图用掉就清：不清的话下次识别还会带上它，看起来像开关失灵
    cleared = memory_service.clear_pin(con)
    if cleared:
        log.info("ingest.pin_cleared %s", kv(count=cleared))

    return IntakeResult(
        draft=draft,
        items=normalized.items,
        memory_ids=built.memory_ids,
        memory_dropped=built.memory_dropped,
        around=built.around,
        week=built.week,
        context_chars=len(built.text),
        overrides=overrides,
    )


def _term_start(cfg: Config) -> date:
    try:
        return parse_utc(cfg.term.start_date).date()
    except Exception:
        # 配置里的日期写错时，退化成"今天"而不是让整个识别功能挂掉：
        # 最坏情况只是提示词里的周次不准，用户仍能录入任务。
        log.warning("ingest.term_start_unparseable %s", kv(raw=cfg.term.start_date))
        return now_utc().date()


def _build_llm(cfg: Config):
    from app.llm import build_llm

    return build_llm(cfg)


def preview_context(cfg: Config, con: sqlite3.Connection) -> str:
    """给控制台看的"当前提示词上下文长什么样"。

    这个接口的价值在于：识别结果不对时，第一件事就是看上下文对不对。
    没有它，用户只能靠猜。
    """
    courses = timetable_service.list_courses(con)
    around = timetable_service.find_around(
        courses,
        now_utc(),
        start_date=_term_start(cfg),
        total_weeks=cfg.term.total_weeks,
        tz_name=cfg.server.timezone,
    )
    course_ids = {
        course.id
        for course in courses
        if any(item.course_name == course.name for item in around)
    }
    memories = memory_service.select_for_injection(con, course_ids_in_context=course_ids)
    built = context_service.build(
        context_service.ContextInput(
            now=now_utc(),
            tz_name=cfg.server.timezone,
            term_start=_term_start(cfg),
            total_weeks=cfg.term.total_weeks,
            courses=courses,
            memories=memories,
            memory_char_budget=cfg.ingest.memory_char_budget,
        )
    )
    return built.text
