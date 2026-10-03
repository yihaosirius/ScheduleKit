/**
 * 展示层的格式化工具。
 *
 * 这些函数都只做"把服务端给的值渲染成人看得懂的字"——
 * **不重新计算业务语义**。
 *
 * 具体地说：到期倒计时用服务端返回的 `due_in_human`，不在这里自己算。
 * 原因见 `docs/decisions.md`：倒计时要与排序用同一个时间基准，
 * 前端各自算会与服务端的排序结果不一致，而那种不一致最难排查
 * （两边看起来都对）。
 */

import type { Category, DraftItem, Task } from '@/api/client'
import { CATEGORY_LABELS, PRIORITY_LABELS } from '@/api/client'

/** ISO8601 → "10月10日 23:59"（按浏览器本地时区） */
export function formatDateTime(iso: string | null): string {
  if (!iso) return '—'
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return iso
  const month = date.getMonth() + 1
  const day = date.getDate()
  const hh = String(date.getHours()).padStart(2, '0')
  const mm = String(date.getMinutes()).padStart(2, '0')
  return `${month}月${day}日 ${hh}:${mm}`
}

/** 短日期，列表里用 */
export function formatDateShort(iso: string | null): string {
  if (!iso) return '—'
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return iso
  return `${date.getMonth() + 1}/${date.getDate()}`
}

/** 相对时间，用于"更新于"这类次要信息 */
export function formatRelative(iso: string | null): string {
  if (!iso) return '—'
  const then = new Date(iso).getTime()
  if (Number.isNaN(then)) return iso
  const seconds = Math.round((then - Date.now()) / 1000)
  const abs = Math.abs(seconds)
  const suffix = seconds >= 0 ? '后' : '前'
  if (abs < 60) return '刚刚'
  if (abs < 3600) return `${Math.round(abs / 60)} 分钟${suffix}`
  if (abs < 86400) return `${Math.round(abs / 3600)} 小时${suffix}`
  return `${Math.round(abs / 86400)} 天${suffix}`
}

export function formatBytes(bytes: number): string {
  if (!bytes) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB']
  let value = bytes
  let index = 0
  while (value >= 1024 && index < units.length - 1) {
    value /= 1024
    index += 1
  }
  return `${value.toFixed(value >= 100 || index === 0 ? 0 : 1)} ${units[index]}`
}

export function formatDuration(seconds: number): string {
  if (seconds < 60) return `${Math.round(seconds)} 秒`
  if (seconds < 3600) return `${Math.round(seconds / 60)} 分钟`
  if (seconds < 86400) return `${(seconds / 3600).toFixed(1)} 小时`
  return `${(seconds / 86400).toFixed(1)} 天`
}

/** 草稿剩余有效时间：过期就没了，所以文案要明确 */
export function formatDraftLeft(seconds: number): string {
  if (seconds <= 0) return '已过期'
  if (seconds < 3600) return `${Math.round(seconds / 60)} 分钟后过期`
  if (seconds < 86400) return `${Math.round(seconds / 3600)} 小时后过期`
  return `${Math.round(seconds / 86400)} 天后过期`
}

export function categoryLabel(category: Category | string): string {
  return CATEGORY_LABELS[category as Category] ?? String(category)
}

export function priorityLabel(priority: number | null): string {
  if (!priority) return '—'
  return PRIORITY_LABELS[priority] ?? String(priority)
}

/** 任务是否属于"今天到期"这类高关注状态 */
export function dueState(task: Task): 'overdue' | 'today' | 'soon' | 'normal' {
  if (task.is_overdue) return 'overdue'
  if (!task.due_at) return 'normal'
  const diff = new Date(task.due_at).getTime() - Date.now()
  if (diff <= 0) return 'overdue'
  if (diff <= 86400e3) return 'today'
  if (diff <= 3 * 86400e3) return 'soon'
  return 'normal'
}

/** 把草稿条目转成"人能编辑的本地时间串"（datetime-local 输入框要的格式） */
export function isoToLocalInput(iso: string | null): string {
  if (!iso) return ''
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return ''
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(
    date.getHours(),
  )}:${pad(date.getMinutes())}`
}

/** datetime-local 的值 → 带时区偏移的 ISO8601（服务端要偏移量） */
export function localInputToIso(value: string): string | null {
  if (!value) return null
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return null
  const offsetMinutes = -date.getTimezoneOffset()
  const sign = offsetMinutes >= 0 ? '+' : '-'
  const abs = Math.abs(offsetMinutes)
  const pad = (n: number) => String(n).padStart(2, '0')
  const local = `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(
    date.getHours(),
  )}:${pad(date.getMinutes())}:00`
  return `${local}${sign}${pad(Math.floor(abs / 60))}:${pad(abs % 60)}`
}

/** 草稿条目的二选一：填了截止时间就清优先级，反之亦然。
 *
 * 服务端也会强制这件事，但前端必须**同步**地表现出这个约束，
 * 否则用户会看到"两个都填了"，提交后被打回——那体验很差。
 */
export function applyShape(item: DraftItem, changed: 'due' | 'priority'): DraftItem {
  if (changed === 'due' && item.due_at) return { ...item, priority: null }
  if (changed === 'priority' && item.priority !== null) return { ...item, due_at: null }
  return item
}

/** 本地日期 → YYYY-MM-DD */
export function todayIso(): string {
  const now = new Date()
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`
}
