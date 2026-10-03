# HTTP 接口规格

给 **Apple 快捷指令**、**Scriptable 小组件**、脚本与将来的 agent 对接使用。

- 基地址：`https://canisa1ph.duckdns.org:8443`
- 所有响应都是 JSON；错误体统一为 `{"detail": "...", "trace_id": "8f3a2b1c"}`
- 每个响应都带 `X-Trace-Id` 头，与服务端日志一一对应（报错截图里带上它就能直接定位）

---

## 鉴权

两条**互斥**的凭据路径（见 `docs/decisions.md` D-008）：

### 1. API Key（快捷指令 / 小组件 / 脚本）

```
Authorization: Bearer sk_xxxxxxxx...
```

- **不需要 CSRF token**：`Authorization` 头不会被浏览器自动携带，所以天然免疫 CSRF。
  给 API Key 加 CSRF 只会让快捷指令无法工作。
- `scope = "write"` 可读写；`scope = "read"` 只能读（小组件用只读 Key 更合适）。
- 明文只在创建时返回一次；服务端只存 sha256。
- 吊销后立即失效。

创建：

```bash
# 网页控制台 → 设置 → API Key → 新建
# 或命令行
cd /opt/schedulekit && sudo -u schedulekit env SK_CONFIG=/etc/schedulekit/config.toml \
  ./.venv/bin/python -m app.cli new-key iphone-shortcut
```

### 2. Web 会话（浏览器）

- 签名 Cookie `sk_session`（`HttpOnly`）
- **写操作必须带** `X-CSRF-Token` 头，值取自 `sk_csrf` Cookie
- 改密码后所有会话立即失效

**CSRF 只对 Cookie 会话生效。** 用 API Key 时传不传都不影响。

---

## 端点一览

### 认证 `/api/auth`

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/auth/me` | 会话状态 + 下发 CSRF。**未登录也返回 200**，`authenticated=false` |
| POST | `/api/auth/login` | `{"password": "..."}`；成功下发两个 Cookie |
| POST | `/api/auth/logout` | 幂等，永远 200 |
| GET | `/api/auth/whoami` | 当前凭据身份（排查 Key 是否生效用） |

`/api/auth/me` 刻意用 200 而不是 401 表达"未登录"：前端首屏本来就需要在两种情况下
都拿到 CSRF token，而 401 无法区分"没登录"和"鉴权服务坏了"。

### 任务 `/api/tasks`

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/tasks?view=ordered\|unordered\|done\|all` | 列表 + 各视图计数 |
| GET | `/api/tasks/{id}` | 单条 |
| POST | `/api/tasks` | 新建 |
| PATCH | `/api/tasks/{id}` | 局部更新 |
| POST | `/api/tasks/{id}/toggle` | 切换完成状态（最高频写操作，单独一个端点） |
| DELETE | `/api/tasks/{id}` | 删除 |

**核心约束：`due_at` 与 `priority` 恰好一个非空。**

```jsonc
// 有序表任务：有明确截止时间
{ "title": "交第三章习题", "category": "homework",
  "due_at": "2026-10-10T23:59:00+08:00" }

// 无序表任务：没有截止时间，用优先级
{ "title": "背单词", "category": "practice", "priority": 3 }
```

两者都给或都不给会返回 **422**，`detail` 是中文说明。

`priority` 含义：`1=Ⅰ 24小时内` / `2=Ⅱ 本周关键` / `3=Ⅲ 常规` / `4=Ⅳ 可延后` / `5=Ⅴ 有空再说`。

`client_uuid` 是**幂等键**：快捷指令网络超时重试时带同一个值，服务端返回已有任务
而不是录两条。

PATCH 的 `due_at` / `priority` 需要用 `due_at_set` / `priority_set` 标志显式声明
"我要改这个字段"——因为 `null` 同时可能是"不改"和"清空"，而二选一约束要求能表达
"清空一个、填上另一个"：

```jsonc
// 把任务从无序表移到有序表（优先级会被自动清掉）
{ "due_at": "2026-10-10T23:59:00+08:00", "due_at_set": true }
```

### 录入 `/api/ingest`

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/ingest` | **multipart**：`image`（文件）与 `text`（表单字段），至少给一个 |
| GET | `/api/ingest/context` | 查看当前会被注入的提示词上下文（排查用） |

```bash
# 纯图片
curl -X POST "https://canisa1ph.duckdns.org:8443/api/ingest" \
  -H "Authorization: Bearer $SK_KEY" \
  -F "image=@photo.jpg"

# 图片 + 文字备注
curl -X POST "https://canisa1ph.duckdns.org:8443/api/ingest" \
  -H "Authorization: Bearer $SK_KEY" \
  -F "image=@photo.jpg" -F "text=下周一交"
```

返回：

```jsonc
{
  "draft": {
    "id": 12, "status": "pending", "channel": "shortcut",
    "llm_path": "tool_call",         // tool_call | json_schema | json_object
    "fallback_note": null,           // 非 null 表示本次走了降级通道
    "items": [ { "title": "...", "category": "homework",
                 "due_at": "2026-10-10T15:59:00+00:00", "priority": null, "notes": "" } ],
    "item_count": 1,
    "memory_ids": [3],               // 本次识别参考了哪些记忆条目
    "overrides": [],                 // 服务端对模型输出做过的改动（人话）
    "seconds_left": 172800
  },
  "confirm_url": "https://canisa1ph.duckdns.org:8443/#/drafts/12",
  "message": "识别到 1 条事项，请在确认页核对后入库。"
}
```

**关键不变量：此时 `items` 表零写入。** 只有确认后才入库。

图片要求：JPEG / PNG / WebP / GIF，`[server].max_image_bytes` 以内（默认 8MB）。
服务端用 Pillow 真解一次，**不信** multipart 里的 content-type——
iPhone 会把 HEIC 标成 `image/jpeg`，所以**快捷指令里要先转成 JPEG**。

### 草稿 `/api/drafts`

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/drafts?status_filter=pending` | 草稿箱列表 + 计数 |
| GET | `/api/drafts/{id}` | 单条 |
| GET | `/api/drafts/{id}/image` | 原图（需鉴权：图里可能有群名、学号） |
| GET | `/api/drafts/{id}/raw` | 模型原始输出（排查用，单独接口避免列表变重） |
| POST | `/api/drafts/{id}/confirm` | **唯一入库点** |
| PUT | `/api/drafts/{id}/items` | 暂存二次修改（不入库） |
| POST | `/api/drafts/{id}/discard` | 丢弃（保留记录用于统计） |
| POST | `/api/drafts/{id}/retry` | 用原图重跑识别（原地更新同一条草稿） |
| POST | `/api/drafts/purge` | 清空草稿箱（**仅网页会话**，API Key 不行） |

确认时可以不传 body（用解析结果），也可以传用户改过的条目：

```jsonc
POST /api/drafts/12/confirm
{ "items": [ { "title": "改过的标题", "category": "homework", "priority": 1 } ] }
```

**二次修改会重跑全部业务校验**——二选一不能只在前端拦。

错误码：草稿不存在 → 404；已确认/已丢弃 → 409（`detail` 会说明是哪个状态）。

`purge` 要求网页会话：一次性删掉所有待确认草稿是不可逆的批量操作，
API Key（可能只存在手机快捷指令里、更难保管）不该能做。

### 记忆 `/api/memories`

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/memories?enabled_only=false` | 列表 + 计数 |
| GET | `/api/memories/selected` | **下一次识别会注入哪些条目**（排查用） |
| GET | `/api/memories/{id}` | 单条 |
| POST | `/api/memories` | 新建 |
| PATCH | `/api/memories/{id}` | 修改 |
| POST | `/api/memories/{id}/pin` | 标记"下次识别单条注入" |
| DELETE | `/api/memories/{id}/pin` | 取消标记 |
| DELETE | `/api/memories/{id}` | 删除 |
| POST | `/api/memories/clear-pins` | 清除全部单条注入标记 |

记忆是**给模型的参考素材，不是待办事项**。它解决消歧：
"电子电路基础的作业周三交"这条约定能让只写着"这次作业"的截图补出课程名与日期。

```jsonc
{ "title": "电子电路基础", "content": "作业周三交，迟交扣分",
  "scope": "global", "enabled": true }
```

`scope`：
- `"global"` —— 每次都注入（`course_id` 会被强制清空）
- `"course"` —— **只在对应课程出现在上下文里时**注入（必须给 `course_id`）

`pin_single` 是**一次性**的：用完（下次识别）后服务端自动清除。
所以它表达的是"这一次带上它"，不是持久偏好。

### 课表 `/api/courses` `/api/timetable`

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/courses` | 课程 + 时段 |
| PUT | `/api/courses` | **整体替换**（传空数组即清空） |
| GET | `/api/timetable` | 整周矩阵 + 学期信息 |
| PUT | `/api/timetable` | 整体替换 |
| GET | `/api/timetable/now` | 正在上 / 刚下课 / 即将开始的课 |

整体替换而不是逐条增删改：课表在 UI 里是整张周表编辑的，改完一次提交，
也就不存在"删了两门、改了三门"的部分失败中间态。

```jsonc
{ "courses": [ {
    "name": "电子电路基础", "teacher": "张老师", "location": "三教 201",
    "sessions": [ { "weekday": 3, "start_time": "08:00", "end_time": "09:40",
                    "weeks": "1-16" } ] } ] }
```

`weekday`：1=周一 … 7=周日。
`weeks` 支持 `"1-16"` / `"1,3,5"` / `"2-8,10"`；留空表示全周生效。
解析失败**不报错**，退化成"全周生效"并打 WARNING（一行课表写错不该让识别功能挂掉）。

`/api/timetable/now` 的输出驱动首页顶栏的"第 N 周 · 正在上：课程名"。
周次与"正在上"的判定在**服务端**算：它依赖配置里的学期起始日，
前端各自实现会与注入提示词的口径不一致——而那种不一致最难排查，因为两边看起来都对。

### 控制台 `/api`

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/settings` | LLM / 学期 / 服务器设置（**不含密钥明文**，只有 `has_api_key`） |
| PUT | `/api/settings` | 改 LLM 配置，**立即生效** |
| PUT | `/api/settings/term` | 改学期起始日与总周数 |
| GET | `/api/keys` | API Key 列表（只有前缀） |
| POST | `/api/keys` | 新建，**明文只返回一次** |
| DELETE | `/api/keys/{id}` | 吊销 |
| GET | `/api/status` | 版本 / 运行时长 / 计数 / 存储 / 告警 |

`api_key` 字段的语义：**不传表示不改；传空字符串表示清空。**
控制台表单里那个输入框平时是空的（不回显密钥），如果空值等于清空，
用户每次改别的设置都会顺手把 Key 清掉。

`/api/status` 的 `warnings` 会列出"能跑但有问题"的状态，例如学期周数已超出配置——
那会让提示词里的课表上下文静默不准。

### 其他

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/healthz` | 健康检查（Caddy 里跳过日志） |
| GET | `/manifest.webmanifest` | PWA manifest |
| GET | `/sw.js` | Service Worker（`no-cache`） |

---

## 错误码约定

| 码 | 含义 | 典型场景 |
|---|---|---|
| 401 | 未认证 / 凭据失效 | 没带 Key、会话过期、Key 被吊销 |
| 403 | 认证了但没权限 | 只读 Key 尝试写、CSRF 校验失败、`purge` 用 Key 调 |
| 404 | 对象不存在 | `GET /api/tasks/99999` |
| 409 | **状态冲突** | 草稿已确认又确认一次、重试已丢弃的草稿 |
| 413 | 请求体过大 | 图片超过 `max_image_bytes`、文字超过 4000 字 |
| 422 | 参数或业务规则不合法 | 二选一违反、类别不合法、时间无法解析 |
| 429 | 限流 | 登录失败过于频繁（带 `Retry-After`） |
| 500 | 服务端内部错误 | 有 bug；把 `trace_id` 提供给排查 |

409 与 422 的区分是刻意的：前者是"状态不对，去别处看看"（例如"这条已经确认过了"），
后者是"你给的参数不合法"（改一下再提交）。前端据此给出不同的提示。
