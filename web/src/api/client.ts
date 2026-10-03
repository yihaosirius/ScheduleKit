/**
 * 接口客户端。
 *
 * 三条约定，与前端的错误体验直接相关：
 *
 * 1. **所有错误都变成一个带中文说明的 ApiError**。服务端的错误体是
 *    `{detail, trace_id}`，这里把它抬成异常对象——组件里只需要 try/catch
 *    和 `e.message`，不必各自解析响应体。
 * 2. **写操作自动带 X-CSRF-Token**。漏带会被服务端 403 拦住，
 *    而那个错看起来像"操作失败"，很难联想到 CSRF。自动带上就没有这个坑。
 * 3. **trace_id 一路带着**。出错提示里带上它，用户截图给我就能直接定位日志
 *    （`journalctl -u schedulekit | grep 't=那个id'`）。这是本项目排查的核心动线。
 */

export class ApiError extends Error {
  status: number
  traceId: string

  constructor(message: string, status: number, traceId = '') {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.traceId = traceId
  }

  /** 给用户看的完整文案：带轨迹号，便于回查服务端日志 */
  get display(): string {
    return this.traceId ? `${this.message}（追踪号 ${this.traceId}）` : this.message
  }
}

function readCookie(name: string): string {
  const prefix = name + '='
  for (const part of document.cookie.split(';')) {
    const item = part.trim()
    if (item.startsWith(prefix)) return decodeURIComponent(item.slice(prefix.length))
  }
  return ''
}

interface RequestOptions {
  method?: string
  /** JSON 体；与 form 互斥 */
  body?: unknown
  /** FormData；用于图片上传 */
  form?: FormData
  signal?: AbortSignal
}

async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { method = 'GET', body, form, signal } = options
  const headers: Record<string, string> = { Accept: 'application/json' }

  let payload: BodyInit | undefined
  if (form) {
    payload = form
  } else if (body !== undefined) {
    headers['Content-Type'] = 'application/json'
    payload = JSON.stringify(body)
  }

  if (method !== 'GET' && method !== 'HEAD') {
    const csrf = readCookie('sk_csrf')
    if (csrf) headers['X-CSRF-Token'] = csrf
  }

  let response: Response
  try {
    response = await fetch(path, {
      method,
      headers,
      body: payload,
      credentials: 'same-origin',
      signal,
    })
  } catch (error) {
    // 网络层失败（离线、DNS、被中断）。与"服务端返回错误"是两件事，
    // 文案也不同——前者要检查网络，后者要看服务端日志。
    if ((error as Error).name === 'AbortError') throw error
    throw new ApiError('网络不可用，请检查连接后重试', 0)
  }

  const traceId = response.headers.get('X-Trace-Id') ?? ''
  const text = await response.text()

  let parsed: unknown = null
  if (text) {
    try {
      parsed = JSON.parse(text)
    } catch {
      parsed = { detail: text.slice(0, 200) }
    }
  }

  if (!response.ok) {
    const detail = (parsed as { detail?: unknown } | null)?.detail
    let message: string
    if (typeof detail === 'string' && detail) {
      message = detail
    } else if (Array.isArray(detail)) {
      // pydantic 的校验错误数组
      message = detail
        .map((item) => (item as { msg?: string }).msg ?? '')
        .filter(Boolean)
        .join('；')
    } else if (detail) {
      message = JSON.stringify(detail)
    } else {
      message = `请求失败（HTTP ${response.status}）`
    }
    throw new ApiError(message || '请求失败', response.status, traceId)
  }

  return parsed as T
}

/* ────────────────────────────────────────────────────────────────
 * 类型：与 app/schemas.py 一一对应
 * ──────────────────────────────────────────────────────────────── */

export type View = 'ordered' | 'unordered' | 'done' | 'all'
export type Category = 'homework' | 'practice' | 'exam' | 'appointment' | 'other'

export const CATEGORY_LABELS: Record<Category, string> = {
  homework: '作业',
  practice: '练习',
  exam: '考试',
  appointment: '要约',
  other: '其他',
}

/** 优先级档位。文案与后端 system prompt 里的说明保持一致。 */
export const PRIORITY_LABELS: Record<number, string> = {
  1: 'Ⅰ',
  2: 'Ⅱ',
  3: 'Ⅲ',
  4: 'Ⅳ',
  5: 'Ⅴ',
}

export const PRIORITY_HINTS: Record<number, string> = {
  1: '24 小时内必须处理',
  2: '本周关键',
  3: '常规',
  4: '可延后',
  5: '有空再说',
}

export interface Task {
  id: number
  title: string
  notes: string
  category: Category
  due_at: string | null
  priority: number | null
  status: 'open' | 'done' | 'cancelled'
  source: string
  view: 'ordered' | 'unordered'
  memory_ids: number[]
  created_at: string
  updated_at: string
  completed_at: string | null
  due_in_human: string | null
  is_overdue: boolean
}

export interface TaskList {
  view: View
  items: Task[]
  counts: Record<string, number>
  server_time: string
}

export interface DraftItem {
  title: string
  category: Category
  due_at: string | null
  priority: number | null
  notes: string
}

export interface Draft {
  id: number
  status: 'pending' | 'confirmed' | 'discarded'
  channel: 'web' | 'shortcut' | 'api'
  text_input: string
  has_image: boolean
  image_url: string | null
  llm_path: 'tool_call' | 'json_schema' | 'json_object'
  fallback_note: string | null
  llm_model: string
  llm_elapsed_ms: number
  items: DraftItem[]
  item_count: number
  memory_ids: number[]
  overrides: string[]
  created_at: string
  expires_at: string
  seconds_left: number
  confirmed_at: string | null
}

export interface IngestResult {
  draft: Draft
  confirm_url: string
  message: string
}

export interface ConfirmResult {
  draft: Draft
  created: Task[]
  message: string
}

export interface Memory {
  id: number
  title: string
  content: string
  tags: string[]
  scope: 'global' | 'course'
  course_id: number | null
  enabled: boolean
  pin_single: boolean
  sort_order: number
  created_at: string
  updated_at: string
}

/** 记忆的**入参**形状。与出参的区别见 `api.createMemory` 的注释。 */
export interface MemoryCreatePayload {
  title: string
  content: string
  /** 逗号分隔；服务端转成数组存 */
  tags?: string
  scope?: 'global' | 'course'
  course_id?: number | null
  enabled?: boolean
  pin_single?: boolean
  sort_order?: number
}

export interface MemoryUpdatePayload {
  title?: string
  content?: string
  tags?: string
  scope?: 'global' | 'course'
  course_id?: number | null
  /** 与 `course_id` 配合：置 true 表示要写入该字段（可为 null） */
  course_id_set?: boolean
  enabled?: boolean
  pin_single?: boolean
  sort_order?: number
}

export interface CourseSession {
  id: number
  course_id: number
  course_name: string
  weekday: number
  start_time: string
  end_time: string
  weeks: string
  location: string
}

export interface Course {
  id: number
  name: string
  teacher: string
  location: string
  color: string
  note: string
  sessions: CourseSession[]
}

export interface Timetable {
  courses: Course[]
  matrix: Record<string, Array<Record<string, unknown>>>
  term: { start_date: string; total_weeks: number; current_week: number }
  server_time: string
}

export interface AroundCourse {
  course_name: string
  location: string
  relation: 'ongoing' | 'just_ended' | 'upcoming'
  relation_label: string
  minutes_away: number
  start_time: string
  end_time: string
  day: string
}

export interface NowInfo {
  week: number
  total_weeks: number
  around: AroundCourse[]
  server_time: string
}

export interface LlmSettings {
  provider: 'responses' | 'chat' | 'mock'
  base_url: string
  model: string
  has_api_key: boolean
  temperature: number
  timeout_seconds: number
  max_tokens: number
  thinking: boolean
  reasoning_effort: string
  retry_count: number
  retry_backoff_seconds: number
  system_prompt: string
}

export interface Settings {
  llm: LlmSettings
  term: { start_date: string; total_weeks: number; current_week: number }
  server: {
    public_url: string
    timezone: string
    listen_host: string
    listen_port: number
    data_dir: string
  }
}

export interface Status {
  version: string
  uptime_seconds: number
  server_time: string
  week: number
  counts: {
    tasks: Record<string, number>
    drafts: Record<string, number>
    memories: Record<string, number>
    courses: number
  }
  storage: {
    data_dir: string
    db_bytes: number
    upload_files: number
    upload_bytes: number
    disk_total_bytes: number
    disk_free_bytes: number
    disk_used_percent: number
  }
  llm: {
    provider: string
    model: string
    base_url: string
    thinking: boolean
    has_api_key: boolean
  }
  timezone: string
  warnings: string[]
}

export interface ApiKey {
  id: number
  name: string
  scope: 'read' | 'write'
  prefix: string
  created_at: string
  last_used_at: string | null
  revoked_at: string | null
}

export interface SessionInfo {
  authenticated: boolean
  csrf_token: string
  session_epoch: number
}

/* ────────────────────────────────────────────────────────────────
 * 接口
 * ──────────────────────────────────────────────────────────────── */

export const api = {
  /* 鉴权 */
  me: () => request<SessionInfo>('/api/auth/me'),
  login: (password: string) =>
    request<SessionInfo>('/api/auth/login', { method: 'POST', body: { password } }),
  logout: () => request<{ ok: boolean }>('/api/auth/logout', { method: 'POST' }),

  /* 任务 */
  tasks: (view: View, limit?: number) => {
    const query = new URLSearchParams({ view })
    if (limit) query.set('limit', String(limit))
    return request<TaskList>(`/api/tasks?${query}`)
  },
  createTask: (payload: {
    title: string
    category?: Category
    due_at?: string | null
    priority?: number | null
    notes?: string
    client_uuid?: string
  }) => request<Task>('/api/tasks', { method: 'POST', body: payload }),
  updateTask: (id: number, payload: Record<string, unknown>) =>
    request<Task>(`/api/tasks/${id}`, { method: 'PATCH', body: payload }),
  toggleTask: (id: number) => request<Task>(`/api/tasks/${id}/toggle`, { method: 'POST' }),
  deleteTask: (id: number) =>
    request<{ ok: boolean; id: number }>(`/api/tasks/${id}`, { method: 'DELETE' }),

  /* 录入与草稿 */
  ingest: (form: FormData, signal?: AbortSignal) =>
    request<IngestResult>('/api/ingest', { method: 'POST', form, signal }),
  context: () => request<{ context: string; chars: number }>('/api/ingest/context'),
  drafts: (status = 'pending') =>
    request<{ items: Draft[]; counts: Record<string, number>; server_time: string }>(
      `/api/drafts?status_filter=${status}`,
    ),
  draft: (id: number) => request<Draft>(`/api/drafts/${id}`),
  confirmDraft: (id: number, items?: DraftItem[]) =>
    request<ConfirmResult>(`/api/drafts/${id}/confirm`, {
      method: 'POST',
      body: items ? { items } : {},
    }),
  saveDraftItems: (id: number, items: DraftItem[]) =>
    request<Draft>(`/api/drafts/${id}/items`, { method: 'PUT', body: { items } }),
  discardDraft: (id: number) =>
    request<{ ok: boolean; message: string }>(`/api/drafts/${id}/discard`, { method: 'POST' }),
  retryDraft: (id: number) => request<Draft>(`/api/drafts/${id}/retry`, { method: 'POST' }),
  purgeDrafts: () =>
    request<{ ok: boolean; message: string }>('/api/drafts/purge', { method: 'POST' }),
  draftRaw: (id: number) =>
    request<{ raw_output: string; llm_path: string; fallback_note: string | null; overrides: string }>(
      `/api/drafts/${id}/raw`,
    ),

  /* 记忆 */
  memories: (enabledOnly = false) =>
    request<{ items: Memory[]; counts: Record<string, number> }>(
      `/api/memories?enabled_only=${enabledOnly}`,
    ),
  selectedMemories: () =>
    request<{ memory_ids: number[]; items: Memory[]; note: string }>('/api/memories/selected'),

  /**
   * 新建记忆。
   *
   * 注意 `tags` 在这里是**逗号分隔的字符串**，而 `Memory` 类型里的
   * `tags` 是 `string[]`。这不是笔误：接口的**入参**用字符串（表单里就是一个
   * 输入框），**出参**用数组（前端渲染标签更方便），转换在服务端做。
   * 用 `MemoryCreatePayload` 明确区分这两者，避免后来者以为是类型定义错了。
   */
  createMemory: (payload: MemoryCreatePayload) =>
    request<Memory>('/api/memories', { method: 'POST', body: payload }),
  updateMemory: (id: number, payload: MemoryUpdatePayload) =>
    request<Memory>(`/api/memories/${id}`, { method: 'PATCH', body: payload }),
  deleteMemory: (id: number) =>
    request<{ ok: boolean; message: string }>(`/api/memories/${id}`, { method: 'DELETE' }),
  pinMemory: (id: number) => request<Memory>(`/api/memories/${id}/pin`, { method: 'POST' }),
  unpinMemory: (id: number) => request<Memory>(`/api/memories/${id}/pin`, { method: 'DELETE' }),
  clearPins: () =>
    request<{ ok: boolean; message: string }>('/api/memories/clear-pins', { method: 'POST' }),

  /* 课表 */
  timetable: () => request<Timetable>('/api/timetable'),
  saveTimetable: (courses: Array<Record<string, unknown>>) =>
    request<Timetable>('/api/timetable', { method: 'PUT', body: { courses } }),
  now: () => request<NowInfo>('/api/timetable/now'),

  /* 控制台 */
  settings: () => request<Settings>('/api/settings'),
  saveSettings: (payload: Record<string, unknown>) =>
    request<Settings>('/api/settings', { method: 'PUT', body: payload }),
  saveTerm: (payload: { start_date?: string; total_weeks?: number }) =>
    request<Settings>('/api/settings/term', { method: 'PUT', body: payload }),
  status: () => request<Status>('/api/status'),
  keys: () => request<ApiKey[]>('/api/keys'),
  createKey: (name: string, readOnly: boolean) =>
    request<{ id: number; name: string; scope: string; api_key: string }>('/api/keys', {
      method: 'POST',
      body: { name, read_only: readOnly },
    }),
  revokeKey: (id: number) =>
    request<{ ok: boolean; message: string }>(`/api/keys/${id}`, { method: 'DELETE' }),
}
