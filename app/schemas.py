"""HTTP 接口的请求/响应模型。

集中在一个文件里的理由：**接口契约应该能一眼扫完**。分散在各 router 里时，
"这个字段到底是可选还是必填"要翻好几个文件才能确认。

几条贯穿全文件的约定：

* 字段名与 :mod:`app.services` 里的领域字段保持一致，不做二次改名——
  改名会让"接口字段"与"库字段"的对应关系变成需要记忆的东西。
* 用 ``Field(description=...)`` 写中文说明。项目没有 ``openapi_url``，
  这些说明同时充当代码注释，也是将来接 agent 时的唯一文档来源。
* 校验只做"形状"（类型、范围），**业务规则（二选一、枚举、长度）在 services 层**。
  这样同一套规则不会被实现两遍，也不会出现"接口拦了但服务层没拦"的漏洞。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# --------------------------------------------------------------------------- #
# 通用
# --------------------------------------------------------------------------- #
class ErrorBody(BaseModel):
    detail: str = Field(description="给人看的错误说明")
    trace_id: str = Field(description="与服务端日志、响应头 X-Trace-Id 对应")


class OkBody(BaseModel):
    ok: bool = True
    message: str = ""


# --------------------------------------------------------------------------- #
# 鉴权
# --------------------------------------------------------------------------- #
class LoginRequest(BaseModel):
    password: str = Field(description="管理员密码，明文仅在 HTTPS 上传输")


class SessionInfo(BaseModel):
    authenticated: bool
    csrf_token: str = Field(default="", description="前端写操作要放进 X-CSRF-Token 头")
    session_epoch: int = 0


class ApiKeyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    scope: Literal["read", "write"]
    prefix: str = Field(description="展示用前缀，无法还原完整 Key")
    created_at: str
    last_used_at: str | None = None
    revoked_at: str | None = None


class ApiKeyCreated(BaseModel):
    id: int
    name: str
    scope: Literal["read", "write"]
    api_key: str = Field(description="明文，只在这里出现一次")


class CreateApiKeyRequest(BaseModel):
    name: str = Field(min_length=1, max_length=60, description="用途备注，如 iphone-shortcut")
    read_only: bool = Field(default=False, description="只读 Key 不能上传与勾选")


# --------------------------------------------------------------------------- #
# 任务
# --------------------------------------------------------------------------- #
class TaskOut(BaseModel):
    id: int
    title: str
    notes: str
    category: str
    due_at: str | None = Field(description="UTC ISO8601；非空即有序表")
    priority: int | None = Field(description="1–5 即 Ⅰ–Ⅴ；非空即无序表")
    status: str
    source: str
    view: Literal["ordered", "unordered"]
    memory_ids: list[int] = Field(default_factory=list)
    created_at: str
    updated_at: str
    completed_at: str | None = None
    due_in_human: str | None = Field(default=None, description="服务端算好的倒计时文案")
    is_overdue: bool = False


class TaskListOut(BaseModel):
    view: str
    items: list[TaskOut]
    counts: dict[str, int] = Field(description="各视图计数，供徽标使用")
    server_time: str = Field(description="UTC ISO8601，前端据此校正倒计时")


class TaskCreateRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200, description="服务端会截断到 60 字")
    category: Literal["homework", "practice", "exam", "appointment", "other"] = "other"
    due_at: str | None = Field(
        default=None,
        description="与 priority 二选一。ISO8601 建议带偏移量；只给日期时按当天 23:59 处理",
    )
    priority: int | None = Field(default=None, ge=1, le=5, description="与 due_at 二选一")
    notes: str = ""
    client_uuid: str | None = Field(
        default=None, max_length=64, description="幂等键：重复提交返回同一条任务"
    )


class TaskUpdateRequest(BaseModel):
    """局部更新。

    ``due_at`` / ``priority`` 用 ``*_set`` 布尔量显式表示"我要改这个字段"，
    因为 ``None`` 同时可能是"不改"和"清空"——而二选一约束要求能表达
    "清空一个、填上另一个"。
    """

    title: str | None = Field(default=None, max_length=200)
    category: Literal["homework", "practice", "exam", "appointment", "other"] | None = None
    notes: str | None = None
    status: Literal["open", "done", "cancelled"] | None = None
    due_at: str | None = None
    due_at_set: bool = Field(default=False, description="置 true 表示要写入 due_at（可为 null）")
    priority: int | None = Field(default=None, ge=1, le=5)
    priority_set: bool = Field(default=False, description="置 true 表示要写入 priority")


class TaskDeleteResult(BaseModel):
    ok: bool
    id: int


# --------------------------------------------------------------------------- #
# 草稿
# --------------------------------------------------------------------------- #
class DraftItem(BaseModel):
    """草稿里的一条待确认事项。可编辑字段与 tasks 一致。"""

    title: str
    category: Literal["homework", "practice", "exam", "appointment", "other"] = "other"
    due_at: str | None = None
    priority: int | None = Field(default=None, ge=1, le=5)
    notes: str = ""


class DraftOut(BaseModel):
    id: int
    status: Literal["pending", "confirmed", "discarded"]
    channel: Literal["web", "shortcut", "api"]
    text_input: str
    has_image: bool
    image_url: str | None = None
    llm_path: Literal["tool_call", "json_schema", "json_object"]
    fallback_note: str | None = Field(default=None, description="降级原因；非空即本次结果有降级")
    llm_model: str
    llm_elapsed_ms: float
    items: list[dict[str, Any]]
    item_count: int
    memory_ids: list[int] = Field(description="本次识别参考了哪些记忆条目")
    overrides: list[str] = Field(description="服务端对模型输出做过的改动")
    created_at: str
    expires_at: str
    seconds_left: int
    confirmed_at: str | None = None


class DraftListOut(BaseModel):
    items: list[DraftOut]
    counts: dict[str, int]
    server_time: str


class IngestResultOut(BaseModel):
    """上传后的返回。含确认页链接，快捷指令直接打开它。"""

    draft: DraftOut
    confirm_url: str = Field(description="可直接在手机浏览器打开的确认页地址")
    message: str = ""


class ConfirmRequest(BaseModel):
    items: list[DraftItem] | None = Field(
        default=None,
        description="用户二次修改后的条目；不传则用草稿里的解析结果",
    )


class ConfirmResultOut(BaseModel):
    draft: DraftOut
    created: list[TaskOut]
    message: str = ""


class ReplaceItemsRequest(BaseModel):
    items: list[DraftItem] = Field(description="暂存修改，不入库")


# --------------------------------------------------------------------------- #
# 记忆
# --------------------------------------------------------------------------- #
class MemoryOut(BaseModel):
    id: int
    title: str
    content: str
    tags: list[str] = Field(default_factory=list)
    scope: Literal["global", "course"]
    course_id: int | None = None
    enabled: bool
    pin_single: bool = Field(description="已标记单条注入；用过一次后会清除")
    sort_order: int
    created_at: str
    updated_at: str


class MemoryListOut(BaseModel):
    items: list[MemoryOut]
    counts: dict[str, int]


class MemoryCreateRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1, description="记忆正文；服务端截断到 500 字")
    tags: str = Field(default="", description="逗号分隔")
    scope: Literal["global", "course"] = "global"
    course_id: int | None = None
    enabled: bool = True
    pin_single: bool = False
    sort_order: int | None = None


class MemoryUpdateRequest(BaseModel):
    title: str | None = None
    content: str | None = None
    tags: str | None = None
    scope: Literal["global", "course"] | None = None
    course_id: int | None = None
    course_id_set: bool = False
    enabled: bool | None = None
    pin_single: bool | None = None
    sort_order: int | None = None


# --------------------------------------------------------------------------- #
# 课表
# --------------------------------------------------------------------------- #
class SessionIn(BaseModel):
    weekday: int = Field(ge=1, le=7, description="1=周一 … 7=周日")
    start_time: str = Field(description="HH:MM")
    end_time: str = Field(description="HH:MM")
    weeks: str = Field(default="", description="如 1-16 / 1,3,5 / 2-8,10；留空表示全周")
    location: str = ""


class SessionOut(SessionIn):
    id: int
    course_id: int
    course_name: str


class CourseIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    teacher: str = ""
    location: str = ""
    color: str = Field(default="", description="前端用的色标，任意短字符串")
    note: str = ""
    sessions: list[SessionIn] = Field(default_factory=list)


class CourseOut(CourseIn):
    id: int
    sessions: list[SessionOut] = Field(default_factory=list)


class TimetableOut(BaseModel):
    courses: list[CourseOut]
    matrix: dict[str, list[dict[str, Any]]] = Field(description="周一..周日 → 当天的课")
    term: dict[str, Any] = Field(description="term_start / total_weeks / current_week")
    server_time: str


class TimetableIn(BaseModel):
    courses: list[CourseIn] = Field(description="整体替换；传空数组即清空课表")


# --------------------------------------------------------------------------- #
# 控制台
# --------------------------------------------------------------------------- #
class LlmSettingsOut(BaseModel):
    provider: Literal["responses", "chat", "mock"]
    base_url: str
    model: str
    has_api_key: bool = Field(description="不返回明文，只告诉你有没有配")
    temperature: float
    timeout_seconds: int
    max_tokens: int
    thinking: bool
    reasoning_effort: str
    retry_count: int
    retry_backoff_seconds: float
    system_prompt: str


class LlmSettingsIn(BaseModel):
    provider: Literal["responses", "chat", "mock"] | None = None
    base_url: str | None = None
    model: str | None = None
    api_key: str | None = Field(
        default=None, description="不传表示不改；传空字符串表示清空"
    )
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    timeout_seconds: int | None = Field(default=None, ge=5, le=600)
    max_tokens: int | None = Field(default=None, ge=64, le=393216)
    thinking: bool | None = None
    reasoning_effort: Literal["none", "low", "high", "max"] | None = None
    retry_count: int | None = Field(default=None, ge=0, le=10)
    retry_backoff_seconds: float | None = Field(default=None, ge=0.0, le=30.0)
    system_prompt: str | None = None


class TermSettingsIn(BaseModel):
    start_date: str | None = Field(default=None, description="第 1 周的周一，ISO 日期")
    total_weeks: int | None = Field(default=None, ge=1, le=52)


class SettingsOut(BaseModel):
    llm: LlmSettingsOut
    term: dict[str, Any]
    server: dict[str, Any] = Field(description="只读：public_url / timezone / data_dir")


class StatusOut(BaseModel):
    version: str
    uptime_seconds: float
    server_time: str
    week: int
    counts: dict[str, Any] = Field(description="任务 / 草稿 / 记忆 / 课表 的计数")
    storage: dict[str, Any] = Field(description="数据目录占用、数据库大小、上传图片数量")
    llm: dict[str, Any] = Field(description="provider / model / 是否配了 Key")
    timezone: str
    warnings: list[str] = Field(default_factory=list)
