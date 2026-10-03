-- 0001_init.sql —— ScheduleKit 初始 schema
--
-- 约定：
--   * 时间一律 UTC ISO8601 文本（如 2026-10-03T12:00:00+00:00），只有渲染层做时区换算。
--   * 单租户：没有 users 表，也没有 user_id 外键。
--   * 所有 CHECK 都是"最后一道防线"：服务层会先校验并给出可读错误，
--     这里再挡一次，防止绕过服务层的写入（比如手改库、未来的脚本）。

-- ---------------------------------------------------------------------------
-- 任务
-- ---------------------------------------------------------------------------
CREATE TABLE items (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    title        TEXT    NOT NULL,
    notes        TEXT    NOT NULL DEFAULT '',
    category     TEXT    NOT NULL
                 CHECK (category IN ('homework', 'practice', 'exam', 'appointment', 'other')),

    -- 二选一的核心约束：
    --   due_at 非空  → 有序表（按截止时间升序）
    --   priority 非空 → 无序表（Ⅰ..Ⅴ）
    due_at       TEXT,
    priority     INTEGER CHECK (priority IS NULL OR (priority BETWEEN 1 AND 5)),

    status       TEXT    NOT NULL DEFAULT 'open'
                 CHECK (status IN ('open', 'done', 'cancelled')),
    source       TEXT    NOT NULL DEFAULT 'web'
                 CHECK (source IN ('web', 'shortcut', 'llm', 'api')),

    -- 客户端幂等键：快捷指令网络重试时用它去重，避免同一个作业录两条。
    client_uuid  TEXT    UNIQUE,

    -- 记忆选择留痕：本次录入参考了哪些记忆条目（不是推理溯源，只是"用了哪些材料"）。
    -- 存 JSON 数组文本，形如 [1,7,9]。
    memory_ids   TEXT    NOT NULL DEFAULT '[]',

    created_at   TEXT    NOT NULL,
    updated_at   TEXT    NOT NULL,
    completed_at TEXT,

    -- CHECK 的布尔运算：SQLite 里 (x IS NULL) 得到 0/1。
    -- "<>" 表示"恰好一个为真"，即 due_at 与 priority 不允许同时为空或同时非空。
    CHECK ((due_at IS NULL) <> (priority IS NULL))
);

CREATE INDEX idx_items_status_due   ON items (status, due_at);
CREATE INDEX idx_items_status_prio  ON items (status, priority);
CREATE INDEX idx_items_completed_at ON items (completed_at);

-- ---------------------------------------------------------------------------
-- 记忆条目
--
-- 用途：把用户长期偏好/历史约定注入提示词，帮助消歧（"这门课周三交"）。
-- 它**不是**任务，绝不直接体现在待办里。
-- ---------------------------------------------------------------------------
CREATE TABLE memories (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    title         TEXT    NOT NULL,
    content       TEXT    NOT NULL,
    -- 逗号分隔的标签，仅用于检索展示
    tags          TEXT    NOT NULL DEFAULT '',
    -- global：所有识别都自动注入；course：只在上下文涉及该课程时注入
    scope         TEXT    NOT NULL DEFAULT 'global'
                  CHECK (scope IN ('global', 'course')),
    course_id     INTEGER REFERENCES courses (id) ON DELETE SET NULL,
    enabled       INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    -- 用户在 UI 打开"单条注入"开关后，下一次识别会强制带上它，
    -- 且不受字符预算裁剪。
    pin_single    INTEGER NOT NULL DEFAULT 0 CHECK (pin_single IN (0, 1)),
    sort_order    INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT    NOT NULL,
    updated_at    TEXT    NOT NULL
);

CREATE INDEX idx_memories_enabled ON memories (enabled, sort_order);
CREATE INDEX idx_memories_course  ON memories (course_id);

-- ---------------------------------------------------------------------------
-- 课表
-- ---------------------------------------------------------------------------
CREATE TABLE courses (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT    NOT NULL UNIQUE,
    teacher    TEXT    NOT NULL DEFAULT '',
    location   TEXT    NOT NULL DEFAULT '',
    color      TEXT    NOT NULL DEFAULT '',
    note       TEXT    NOT NULL DEFAULT '',
    created_at TEXT    NOT NULL,
    updated_at TEXT    NOT NULL
);

CREATE TABLE course_sessions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    course_id  INTEGER NOT NULL REFERENCES courses (id) ON DELETE CASCADE,
    -- 1=周一 .. 7=周日（ISO 8601 的 weekday 编号）
    weekday    INTEGER NOT NULL CHECK (weekday BETWEEN 1 AND 7),
    start_time TEXT    NOT NULL,  -- "HH:MM" 本地时间
    end_time   TEXT    NOT NULL,  -- "HH:MM" 本地时间
    -- 生效周次，支持 "1-16" / "1,3,5" / "2-8,10" 三种写法
    weeks      TEXT    NOT NULL DEFAULT '1-16',
    location   TEXT    NOT NULL DEFAULT '',
    CHECK (start_time < end_time)
);

CREATE INDEX idx_sessions_weekday ON course_sessions (weekday, start_time);

-- ---------------------------------------------------------------------------
-- 录入草稿
--
-- 两阶段录入的中间态。**关键不变量：草稿阶段 items 表零写入。**
-- 只有 confirm 才把解析结果写进 items，且与"标记草稿已确认"同事务。
-- ---------------------------------------------------------------------------
CREATE TABLE ingest_drafts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    status        TEXT    NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending', 'confirmed', 'discarded')),

    -- 来源渠道，用于状态监测里分辨"网页传的"还是"快捷指令传的"
    channel       TEXT    NOT NULL DEFAULT 'web'
                  CHECK (channel IN ('web', 'shortcut', 'api')),

    text_input    TEXT    NOT NULL DEFAULT '',
    -- 相对 data_dir 的路径，如 uploads/ab/abcdef....jpg
    image_path    TEXT,
    image_mime    TEXT,
    image_bytes   INTEGER,
    image_sha256  TEXT,

    -- 走的是哪条结构化通道。三个取值都要能在确认页上标出来，
    -- 用户才知道这次结果可不可信。
    llm_path      TEXT    NOT NULL DEFAULT 'tool_call'
                  CHECK (llm_path IN ('tool_call', 'json_schema', 'json_object')),
    fallback_note TEXT,
    llm_model     TEXT    NOT NULL DEFAULT '',
    llm_elapsed_ms REAL   NOT NULL DEFAULT 0,
    raw_output    TEXT    NOT NULL DEFAULT '',

    -- 解析出的事项（服务端校验后的最终形态），JSON 数组文本
    items_json    TEXT    NOT NULL DEFAULT '[]',
    -- 本次参考的记忆条目 id 数组文本
    memory_ids    TEXT    NOT NULL DEFAULT '[]',

    -- 服务端覆盖过模型输出的摘要，供确认页与排查使用
    overrides     TEXT    NOT NULL DEFAULT '',

    created_at    TEXT    NOT NULL,
    expires_at    TEXT    NOT NULL,
    confirmed_at  TEXT
);

CREATE INDEX idx_drafts_status  ON ingest_drafts (status, created_at DESC);
CREATE INDEX idx_drafts_expires ON ingest_drafts (expires_at);

-- ---------------------------------------------------------------------------
-- API Key
--
-- 只存哈希。明文仅创建时返回一次。
-- ---------------------------------------------------------------------------
CREATE TABLE api_keys (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT    NOT NULL,
    -- sha256(明文)。用 sha256 而非 scrypt：Key 本身是 32 字节随机串，
    -- 不存在弱口令问题，而每次请求都跑 scrypt 会让接口慢得离谱。
    key_hash     TEXT    NOT NULL UNIQUE,
    -- read：只能读；write：可写（快捷指令上传、勾选完成）
    scope        TEXT    NOT NULL DEFAULT 'write' CHECK (scope IN ('read', 'write')),
    prefix       TEXT    NOT NULL DEFAULT '',
    created_at   TEXT    NOT NULL,
    last_used_at TEXT,
    revoked_at   TEXT
);

CREATE INDEX idx_api_keys_hash ON api_keys (key_hash);
