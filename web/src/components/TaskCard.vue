<script setup lang="ts">
/**
 * 单条任务卡片。
 *
 * 两个列表的"权重差异"就落在这里：有序表（有截止时间）用高权重卡片，
 * 有显著的截止时间与倒计时；无序表（有优先级）用更矮、更轻的卡片。
 * 这不是审美选择——需求里明确要求"无序表 UI 权重明显小于有序表"，
 * 因为会过期的东西应该更抢眼。
 *
 * 勾选完成的动画刻意做得比别的操作慢一点（320ms）：它是这个应用里
 * 最高频的"成就时刻"，值得多花一点时间。
 */
import { computed, ref } from 'vue'
import type { Task } from '@/api/client'
import { categoryLabel, dueState, formatDateTime, priorityLabel } from '@/utils/format'

const props = defineProps<{
  task: Task
  /** 紧凑模式（无序表 / 已完成列表） */
  compact?: boolean
}>()

const emit = defineEmits<{
  (event: 'toggle', id: number): void
  (event: 'remove', id: number): void
}>()

const expanded = ref(false)
const completing = ref(false)

const hasNotes = computed(() => props.task.notes.trim().length > 0)

/** 需求里的"二选一"决定了卡片显示哪一种主信息 */
const isOrdered = computed(() => props.task.due_at !== null)

const isDone = computed(() => props.task.status === 'done')

/**
 * 卡片的紧急度状态。
 *
 * **已完成的一律是 'done'**：否则逾期后完成的任务会一直带着红色逾期左边条，
 * 而那是给"还没做且已经晚了"的东西用的标记。用户明确提过
 * 已完成条目不该显示逾期标记。
 */
const state = computed(() => (isDone.value ? 'done' : dueState(props.task)))

/**
 * 卡片上显示的那个绝对时间。
 *
 * 已完成时显示**完成时间**而不是原截止时间：对一个已经做完的事，
 * 更相关的是"什么时候做完的"。如果两者相差很大（比如提前一周做完），
 * 光看截止时间会误以为它早就该交了。
 */
const displayTime = computed(() => {
  if (isDone.value) {
    return props.task.completed_at
      ? `完成于 ${formatDateTime(props.task.completed_at)}`
      : null
  }
  return props.task.due_at ? `截止 ${formatDateTime(props.task.due_at)}` : null
})

async function onToggle(): Promise<void> {
  if (completing.value) return
  completing.value = true
  // 让"划掉 + 沉降"的动画先跑完，再真的把任务从列表里摘掉。
  // 直接 emit 的话任务会立刻消失，用户看不到任何反馈。
  await new Promise((resolve) => window.setTimeout(resolve, props.compact ? 220 : 320))
  emit('toggle', props.task.id)
}
</script>

<template>
  <article
    class="task"
    :class="[
      compact ? 'task--compact' : 'task--roomy',
      `task--${state}`,
      { 'task--done': task.status === 'done', 'task--completing': completing },
    ]"
  >
    <button
      class="task__check"
      type="button"
      :aria-label="task.status === 'done' ? '标记为未完成' : '标记为完成'"
      :aria-pressed="task.status === 'done'"
      @click="onToggle"
    >
      <svg viewBox="0 0 24 24" aria-hidden="true">
        <path d="M5 12.5l4.5 4.5L19 7.5" />
      </svg>
    </button>

    <div class="task__body">
      <div class="task__head">
        <h3 class="task__title">{{ task.title }}</h3>

        <div class="task__meta">
          <span class="sk-chip">{{ categoryLabel(task.category) }}</span>

          <!-- 有序表且**未完成**：显示倒计时（来自服务端，不自己算）。
               已完成的任务刻意不显示它 —— 见下面 done_at 的注释。 -->
          <template v-if="isOrdered && !isDone">
            <span class="task__due" :class="`task__due--${state}`">
              {{ task.due_in_human }}
            </span>
          </template>

          <!-- 无序表且**未完成**：显示优先级档位 -->
          <template v-else-if="!isDone">
            <span class="task__priority" :data-p="task.priority">
              {{ priorityLabel(task.priority) }}
            </span>
          </template>
          <!-- 已完成的条目不再显示优先级："Ⅴ 有空再说"对一个做完的事没有意义 -->
        </div>
      </div>

      <!-- 时间标注：未完成显示截止时间，已完成显示**完成时间**。
           两者都只用绝对时间，不显示"几小时后 / 逾期"这类倒计时 ——
           已完成的任务没有时间压力，红色逾期标记与倒计时只会干扰阅读。 -->
      <div v-if="displayTime" class="task__when" :class="{ 'task__when--done': isDone }">
        {{ displayTime }}
      </div>

      <button
        v-if="hasNotes"
        class="task__notes-toggle"
        type="button"
        :aria-expanded="expanded"
        @click="expanded = !expanded"
      >
        <span class="task__notes-label">备注</span>
        <span v-if="!expanded" class="task__notes-peek">{{ task.notes }}</span>
        <span v-else class="task__notes-full">{{ task.notes }}</span>
      </button>

      <div v-if="task.memory_ids.length" class="task__mem">
        参考了 {{ task.memory_ids.length }} 条记忆
      </div>
    </div>

    <button class="task__remove" type="button" aria-label="删除" @click="emit('remove', task.id)">
      ✕
    </button>
  </article>
</template>

<style scoped>
.task {
  display: grid;
  grid-template-columns: auto 1fr auto;
  gap: 10px;
  align-items: start;
  background: var(--bg-elevated);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  transition: transform var(--dur) var(--ease), opacity var(--dur) var(--ease),
    border-color var(--dur) var(--ease), background var(--dur) var(--ease);
}

/* 有序表：更高、圆角更大、阴影更明显 —— 权重更高 */
.task--roomy {
  padding: 13px 12px;
  box-shadow: var(--shadow-sm);
}

/* 无序表：更矮、无阴影 —— 权重明显更低 */
.task--compact {
  padding: 8px 10px;
  background: transparent;
  border-color: transparent;
  border-bottom: 1px solid var(--border);
  border-radius: 0;
}

.task--compact:last-child {
  border-bottom: none;
}

.task--compact .task__title {
  font-size: 14px;
  font-weight: 500;
}

/* 逾期：左侧一道红条，比整卡变色更克制，也不会盖过其它信息 */
.task--overdue {
  border-left: 3px solid var(--danger);
}

.task--today {
  border-left: 3px solid var(--warn);
}

.task--soon {
  border-left: 3px solid var(--info);
}

.task--compact.task--overdue,
.task--compact.task--today,
.task--compact.task--soon {
  border-radius: 0 var(--radius-sm) var(--radius-sm) 0;
}

.task--completing {
  transform: translateX(10px) scale(0.98);
  opacity: 0.35;
}

/* 已完成：不再有任何紧急度语义。
 *
 * 显式写出来而不是依赖"状态是 done 所以不命中上面三条"——因为逾期后完成的
 * 任务在后端看仍然有过去的 due_at，不做这一步它就会一直带着红色左边条，
 * 而那是给"还没做且已经晚了"用的标记。 */
.task--done {
  border-left-color: transparent;
}

.task--done .task__title {
  text-decoration: line-through;
  color: var(--text-tertiary);
}

/* ── 勾选框 ─────────────────────────────────────────────────── */
.task__check {
  width: 24px;
  height: 24px;
  margin-top: 1px;
  border-radius: 50%;
  border: 2px solid var(--border-strong);
  display: grid;
  place-items: center;
  flex: 0 0 auto;
  transition: border-color var(--dur-fast) var(--ease), background var(--dur-fast) var(--ease),
    transform var(--dur-fast) var(--ease);
}

.task__check:active {
  transform: scale(0.86);
}

.task__check svg {
  width: 14px;
  height: 14px;
  fill: none;
  stroke: var(--bg-elevated);
  stroke-width: 3.2;
  stroke-linecap: round;
  stroke-linejoin: round;
  /* 勾号自己画出来：用路径长度做描边动画，比直接显示一个 ✓ 字符有质感 */
  stroke-dasharray: 26;
  stroke-dashoffset: 26;
  transition: stroke-dashoffset var(--dur-slow) var(--ease) 60ms;
}

.task--done .task__check,
.task--completing .task__check {
  background: var(--ok);
  border-color: var(--ok);
}

.task--done .task__check svg,
.task--completing .task__check svg {
  stroke-dashoffset: 0;
}

/* ── 内容 ───────────────────────────────────────────────────── */
.task__body {
  min-width: 0;
}

.task__head {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
}

.task__title {
  margin: 0;
  font-size: 15px;
  font-weight: 600;
  line-height: 1.35;
  overflow-wrap: anywhere;
}

.task__meta {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 5px;
}

.task__due {
  font-size: 11px;
  font-weight: 600;
  color: var(--text-secondary);
  white-space: nowrap;
}

.task__due--overdue {
  color: var(--danger);
}
.task__due--today {
  color: var(--warn);
}
.task__due--soon {
  color: var(--info);
}

.task__priority {
  display: inline-grid;
  place-items: center;
  min-width: 20px;
  height: 18px;
  padding: 0 5px;
  border-radius: var(--radius-sm);
  background: var(--accent-soft);
  font-size: 11px;
  font-weight: 700;
  color: var(--text-secondary);
}

/* 优先级 Ⅰ/Ⅱ 用暖色：它们代表"现在就该动" */
.task__priority[data-p='1'] {
  background: var(--danger-soft);
  color: var(--danger);
}
.task__priority[data-p='2'] {
  background: var(--warn-soft);
  color: var(--warn);
}

.task__when {
  margin-top: 3px;
  font-size: 11px;
  color: var(--text-tertiary);
  font-variant-numeric: tabular-nums;
}

/* 已完成条目上的时间再淡一档：它是"记录"，不是"待办"。
   与标题的删除线一起，让整行明确读作"已经过去了"。 */
.task__when--done {
  opacity: 0.72;
}

.task__notes-toggle {
  display: block;
  width: 100%;
  margin-top: 5px;
  text-align: left;
  font-size: 12px;
  color: var(--text-secondary);
  line-height: 1.45;
}

.task__notes-label {
  display: inline-block;
  margin-right: 5px;
  padding: 0 5px;
  border-radius: var(--radius-sm);
  background: var(--accent-soft);
  font-size: 10px;
  font-weight: 600;
  color: var(--text-tertiary);
  vertical-align: 1px;
}

/* 折叠时只显示一行：备注可能是模型抽出来的长句，
   全部展开会让列表失去"一屏能看几条"的可扫读性。 */
.task__notes-peek {
  display: -webkit-box;
  -webkit-line-clamp: 1;
  line-clamp: 1;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

.task__notes-full {
  display: block;
  overflow-wrap: anywhere;
}

.task__mem {
  margin-top: 4px;
  font-size: 10px;
  color: var(--text-tertiary);
}

.task__remove {
  width: 26px;
  height: 26px;
  border-radius: var(--radius-sm);
  color: var(--text-tertiary);
  font-size: 12px;
  display: grid;
  place-items: center;
  opacity: 0.65;
  transition: opacity var(--dur-fast) var(--ease), background var(--dur-fast) var(--ease);
}

.task__remove:active {
  background: var(--danger-soft);
  color: var(--danger);
  opacity: 1;
}

@media (hover: hover) {
  .task__remove {
    opacity: 0;
  }
  .task:hover .task__remove {
    opacity: 0.8;
  }
  .task__remove:hover {
    background: var(--danger-soft);
    color: var(--danger);
  }
}
</style>
