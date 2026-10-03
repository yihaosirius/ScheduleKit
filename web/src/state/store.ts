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
  /** 按视图分开的任务。PC 平铺要同时显示三列，所以不能只有一个数组。 */
  lists: {
    ordered: Task[]
    unordered: Task[]
    done: Task[]
  }
  /** 哪些视图已经拉取过（避免重复请求）。 */
  listsLoaded: {
    ordered: boolean
    unordered: boolean
    done: boolean
  }
  counts: Record<string, number>
  settings: Settings | null
  /** 服务端时间与本地时间的差（毫秒） */
  clockSkewMs: number
  loading: boolean
}

export const state = reactive<State>({
  ready: false,
  authenticated: false,
  lists: { ordered: [], unordered: [], done: [] },
  listsLoaded: { ordered: false, unordered: false, done: false },
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
  state.lists = { ordered: [], unordered: [], done: [] }
  state.listsLoaded = { ordered: false, unordered: false, done: false }
  notify('已登出')
}

/* ── 任务 ────────────────────────────────────────────────────── */

/**
 * 拉取任务。
 *
 * ``loadTasks('ordered')`` 只拉一个视图（移动端标签用，省请求）；
 * ``loadTasks('all')`` 三个视图**并行**拉（PC 平铺用，一次把三列都填满）。
 *
 * 为什么要区分：平铺布局要同时显示有序/无序/已完成，但如果移动端也一次拉三个，
 * 就白白多两次请求——而移动端只看得到其中一个（已完成那节默认还收起）。
 */
export async function loadTasks(view: 'ordered' | 'unordered' | 'done' | 'all' = 'ordered') {
  state.loading = true
  try {
    if (view === 'all') {
      const [ordered, unordered, done] = await Promise.all([
        api.tasks('ordered'),
        api.tasks('unordered'),
        api.tasks('done'),
      ])
      state.lists.ordered = ordered.items
      state.lists.unordered = unordered.items
      state.lists.done = done.items
      // 三个响应的计数字段是同一份，取任意一个即可
      state.counts = ordered.counts
      state.clockSkewMs = new Date(ordered.server_time).getTime() - Date.now()
      // 顶栏的"今天有几条"之类的角标也依赖这个，标记一次展开即可
      state.listsLoaded = { ordered: true, unordered: true, done: true }
    } else {
      const data = await api.tasks(view)
      state.lists[view] = data.items
      state.counts = data.counts
      state.clockSkewMs = new Date(data.server_time).getTime() - Date.now()
      state.listsLoaded[view] = true
    }
  } catch (error) {
    report(error)
  } finally {
    state.loading = false
  }
}

/** 某个视图是否已经拉过。用于"只补拉缺的那一个"而不是每次全量重来。 */
export function isLoaded(view: 'ordered' | 'unordered' | 'done'): boolean {
  return state.listsLoaded[view]
}

/** 从**所有**已加载的视图里摘掉某条任务，并同步计数。
 *
 * 摘除是本地立刻做的（而不是等重新拉取），因为等请求回来才消失会让用户
 * 看到"点了但没反应"的那一帧。
 */
function dropLocal(id: number): void {
  for (const key of ['ordered', 'unordered', 'done'] as const) {
    const before = state.lists[key].length
    state.lists[key] = state.lists[key].filter((task) => task.id !== id)
    if (state.lists[key].length !== before) {
      state.counts[key] = Math.max(0, (state.counts[key] ?? 1) - 1)
    }
  }
}

/** 把一条任务放进它当前该在的视图的**开头**。
 *
 * 用在取消完成时：它要回到有序/无序表，而那个列表此刻可能已经加载着，
 * 不补进去就会出现"取消完成了但列表里没有它"的错觉。
 */
function insertLocal(task: Task): void {
  const key = task.status === 'done' ? 'done' : task.due_at ? 'ordered' : 'unordered'
  if (!state.listsLoaded[key]) return // 没加载过的视图不必维护
  if (state.lists[key].some((item) => item.id === task.id)) return
  state.lists[key] = [task, ...state.lists[key]]
  state.counts[key] = (state.counts[key] ?? 0) + 1
}

/** 勾选完成/取消完成。返回更新后的任务，供调用方决定后续动作。 */
export async function toggleTask(id: number): Promise<Task | null> {
  try {
    const updated = await api.toggleTask(id)
    dropLocal(id)

    // 目标视图必须已有正确内容：完成了补进"已完成"，取消完成要回到原列表。
    //
    // 这里**不能**无脑 `loadTasks('all')`：那会在移动端多两次请求，
    // 而在 PC 上是三次全量——用户只勾了一条，代价太大。
    const need = updated.status === 'done' ? 'done' : updated.due_at ? 'ordered' : 'unordered'
    if (!state.listsLoaded[need]) {
      await loadTasks(need)
    } else {
      insertLocal(updated)
    }
    // 计数在服务端是权威，后台默默对齐一次（失败不影响本地已做的变更）
    void refreshCounts()
    return updated
  } catch (error) {
    report(error)
    return null
  }
}

export async function deleteTask(id: number): Promise<boolean> {
  try {
    await api.deleteTask(id)
    dropLocal(id)
    notify('已删除', 'ok')
    void refreshCounts()
    return true
  } catch (error) {
    report(error)
    return false
  }
}

/** 只更新计数（拿一个轻量视图的响应即可），用于保持角标与真实状态一致。 */
async function refreshCounts(): Promise<void> {
  try {
    const data = await api.tasks('done')
    state.counts = data.counts
  } catch {
    // 计数是次要信息，失败就保持本地估算值
  }
}

/* ── 派生 ────────────────────────────────────────────────────── */

/** 有序表：有截止时间，服务端已按时间升序排好，这里不重排。 */
export const orderedTasks = computed(() => state.lists.ordered)

export const doneTasks = computed(() => state.lists.done)

/**
 * 无序表按优先级分组。分组在**前端**做（服务端只保证同一档位内的相对顺序），
 * 因为"怎么展示分组"是纯展示决策，不需要服务端知道。
 */
export function groupByPriority(tasks: Task[]): Array<{ priority: number; items: Task[] }> {
  const groups: Array<{ priority: number; items: Task[] }> = []
  for (const task of tasks) {
    if (task.priority === null) continue
    const existing = groups.find((group) => group.priority === task.priority)
    if (existing) existing.items.push(task)
    else groups.push({ priority: task.priority, items: [task] })
  }
  return groups.sort((a, b) => a.priority - b.priority)
}

export const unorderedGroups = computed(() => groupByPriority(state.lists.unordered))

/** 首页是否完全没有待办（已完成不算：它不是"待办"）。 */
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
