/**
 * 全局状态。
 *
 * 刻意**不引 Pinia**：应用只有三块共享状态（会话、任务、设置），
 * 一个 reactive 对象加几个函数就够了。多一个依赖要多一套心智模型，
 * 而这里的复杂度还没到那个程度。
 *
 * 重要约定：**服务端是真相**。所有本地改动都先发请求，成功后再用响应更新本地——
 * 不做乐观更新。理由是乐观更新在失败时要回滚，而回滚的代码比它省下的那 100ms
 * 值钱得多；这个应用也不追求那种级别的响应速度。
 */

import { computed, reactive } from 'vue'
import { api, ApiError, type Settings, type Task } from '@/api/client'

interface State {
  ready: boolean
  authenticated: boolean
  /** 当前视图的任务 */
  tasks: Task[]
  counts: Record<string, number>
  settings: Settings | null
  /** 服务端时间与本地时间的差（毫秒）。用于显示"数据新鲜度" */
  clockSkewMs: number
  loading: boolean
}

export const state = reactive<State>({
  ready: false,
  authenticated: false,
  tasks: [],
  counts: { ordered: 0, unordered: 0, done: 0 },
  settings: null,
  clockSkewMs: 0,
  loading: false,
})

/* ── Toast ───────────────────────────────────────────────────── */

interface Toast {
  id: number
  message: string
  kind: 'info' | 'error' | 'ok'
}

export const toasts = reactive<{ items: Toast[] }>({ items: [] })
let toastSeq = 0

export function notify(message: string, kind: Toast['kind'] = 'info'): void {
  const id = ++toastSeq
  toasts.items.push({ id, message, kind })
  // 错误的停留久一点：用户可能需要读追踪号
  const ttl = kind === 'error' ? 5200 : 2600
  window.setTimeout(() => dismiss(id), ttl)
}

export function dismiss(id: number): void {
  const index = toasts.items.findIndex((item) => item.id === id)
  if (index >= 0) toasts.items.splice(index, 1)
}

/** 把异常统一转成提示。用法：`catch (e) { report(e) }` */
export function report(error: unknown): void {
  if (error instanceof ApiError) {
    notify(error.display, 'error')
    return
  }
  if (error instanceof Error) {
    notify(error.message, 'error')
    return
  }
  notify(String(error), 'error')
}

/* ── 会话 ────────────────────────────────────────────────────── */

export async function bootstrap(): Promise<void> {
  try {
    const info = await api.me()
    state.authenticated = info.authenticated
  } catch {
    state.authenticated = false
  } finally {
    state.ready = true
  }
}

export async function login(password: string): Promise<boolean> {
  try {
    const info = await api.login(password)
    state.authenticated = info.authenticated
    notify('已登录', 'ok')
    return true
  } catch (error) {
    report(error)
    return false
  }
}

export async function logout(): Promise<void> {
  try {
    await api.logout()
  } catch {
    // 登出失败也要走：本地 Cookie 已由服务端清掉或本就无效
  }
  state.authenticated = false
  state.tasks = []
  notify('已登出')
}

/* ── 任务 ────────────────────────────────────────────────────── */

export async function loadTasks(view: 'ordered' | 'unordered' | 'done' | 'all' = 'ordered') {
  state.loading = true
  try {
    const data = await api.tasks(view)
    state.tasks = data.items
    state.counts = data.counts
    // 服务端时间是判断倒计时是否可信的基准，记下偏差
    state.clockSkewMs = new Date(data.server_time).getTime() - Date.now()
  } catch (error) {
    report(error)
  } finally {
    state.loading = false
  }
}

/** 勾选完成/取消完成。返回更新后的任务，供动画判断方向。 */
export async function toggleTask(id: number): Promise<Task | null> {
  try {
    const updated = await api.toggleTask(id)
    // 立刻从当前列表里移除：它已经不再属于这个视图了。
    // 交给 loadTasks 也可以，但那样用户会看到任务先"留在原地"再消失，
    // 中间那一帧会让人以为没点中。
    state.tasks = state.tasks.filter((task) => task.id !== id)
    await loadTasks(currentView.value)
    return updated
  } catch (error) {
    report(error)
    return null
  }
}

export async function deleteTask(id: number): Promise<boolean> {
  try {
    await api.deleteTask(id)
    state.tasks = state.tasks.filter((task) => task.id !== id)
    notify('已删除', 'ok')
    void loadTasks(currentView.value)
    return true
  } catch (error) {
    report(error)
    return false
  }
}

/* ── 当前视图（首页两个列表之间切换用） ─────────────────────── */

export const currentView = reactive({ value: 'ordered' as 'ordered' | 'unordered' | 'done' })

/* ── 派生 ────────────────────────────────────────────────────── */

export const orderedTasks = computed(() => state.tasks.filter((task) => task.due_at !== null))

/** 无序表按优先级分组。分组在**前端**做（服务端已排好序），
 *  因为"怎么展示分组"是纯展示决策，不需要服务端知道。 */
export const unorderedGroups = computed(() => {
  const groups: Array<{ priority: number; items: Task[] }> = []
  for (const task of state.tasks) {
    if (task.priority === null) continue
    const existing = groups.find((group) => group.priority === task.priority)
    if (existing) existing.items.push(task)
    else groups.push({ priority: task.priority, items: [task] })
  }
  return groups.sort((a, b) => a.priority - b.priority)
})

export const hasAnyTask = computed(
  () => (state.counts.ordered ?? 0) + (state.counts.unordered ?? 0) > 0,
)

/* ── 设置 ────────────────────────────────────────────────────── */

export async function loadSettings(): Promise<void> {
  try {
    state.settings = await api.settings()
  } catch (error) {
    report(error)
  }
}
