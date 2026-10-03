<script setup lang="ts">
/**
 * 首页：有序表 / 无序表 / 已完成三个视图。
 *
 * 分栏用**分段控件 + 滑动指示器**而不是两个按钮：滑动的小块让切换有方向感
 * （从"有序"到"无序"是往右滑），这是需求里"注重交互动画"落到实处的第一处。
 *
 * 三个视图的权重差异是刻意的：
 *   * 有序表：卡片式，间距大，一屏 4–5 条
 *   * 无序表：按 Ⅰ–Ⅴ 分组，行式，一屏 8–10 条
 *   * 已完成：折叠在最后，默认收起
 */
import { computed, onMounted, ref, watch } from 'vue'
import TaskCard from '@/components/TaskCard.vue'
import TaskComposer from '@/components/TaskComposer.vue'
import { PRIORITY_HINTS, PRIORITY_LABELS } from '@/api/client'
import { deleteTask, loadTasks, state, toggleTask } from '@/state/store'

const composer = ref<InstanceType<typeof TaskComposer> | null>(null)

type Tab = 'ordered' | 'unordered' | 'done'

const tab = ref<Tab>('ordered')
const segRef = ref<HTMLElement | null>(null)
const segIndicator = ref({ left: 0, width: 0, ready: false })

const TABS: Array<{ key: Tab; label: string }> = [
  { key: 'ordered', label: '有序表' },
  { key: 'unordered', label: '无序表' },
  { key: 'done', label: '已完成' },
]

const tasks = computed(() => state.tasks)

/** 无序表按优先级分组。分组数据来自服务端已排好序的列表，这里只做归类。 */
const groups = computed(() => {
  const map = new Map<number, typeof tasks.value>()
  for (const task of tasks.value) {
    if (task.priority === null) continue
    const bucket = map.get(task.priority) ?? []
    bucket.push(task)
    map.set(task.priority, bucket)
  }
  return [...map.entries()]
    .map(([priority, items]) => ({ priority, items }))
    .sort((a, b) => a.priority - b.priority)
})

async function refresh(): Promise<void> {
  await loadTasks(tab.value)
}

async function moveIndicator(): Promise<void> {
  const host = segRef.value
  if (!host) return
  await new Promise((resolve) => window.requestAnimationFrame(resolve))
  const target = host.querySelector<HTMLElement>('[data-active="true"]')
  if (!target) return
  segIndicator.value = { left: target.offsetLeft, width: target.offsetWidth, ready: true }
}

watch(tab, async () => {
  await refresh()
  await moveIndicator()
})

watch(() => state.counts, moveIndicator, { deep: true })

onMounted(async () => {
  await refresh()
  await moveIndicator()
})

async function onToggle(id: number): Promise<void> {
  const updated = await toggleTask(id)
  if (updated && tab.value === 'done') await refresh()
}

async function onRemove(id: number): Promise<void> {
  await deleteTask(id)
}

/** 空状态的文案要指出**下一步做什么**，而不是只说"没有数据" */
const emptyHint = computed(() => {
  if (tab.value === 'ordered') {
    return {
      title: '没有带截止时间的任务',
      body: '点右下角的 ＋ 添加，或用 iPhone 快捷指令拍一张作业截图。',
    }
  }
  if (tab.value === 'unordered') {
    return {
      title: '没有按优先级排的任务',
      body: '没有明确截止时间的事情放这里，按 Ⅰ–Ⅴ 分档。',
    }
  }
  return { title: '还没有完成的任务', body: '勾选完成后会出现在这里。' }
})
</script>

<template>
  <div class="home">
    <!-- 分段控件 -->
    <div ref="segRef" class="seg" role="tablist" aria-label="任务视图">
      <span
        class="seg__indicator"
        :class="{ 'seg__indicator--ready': segIndicator.ready }"
        :style="{
          transform: `translateX(${segIndicator.left}px)`,
          width: `${segIndicator.width}px`,
        }"
        aria-hidden="true"
      />
      <button
        v-for="item in TABS"
        :key="item.key"
        class="seg__item"
        :class="{ 'seg__item--active': tab === item.key }"
        :data-active="tab === item.key ? 'true' : 'false'"
        type="button"
        role="tab"
        :aria-selected="tab === item.key"
        @click="tab = item.key"
      >
        {{ item.label }}
        <span v-if="item.key === 'ordered'" class="seg__count">{{ state.counts.ordered ?? 0 }}</span>
        <span v-else-if="item.key === 'unordered'" class="seg__count">
          {{ state.counts.unordered ?? 0 }}
        </span>
        <span v-else class="seg__count">{{ state.counts.done ?? 0 }}</span>
      </button>
    </div>

    <!-- 有序表 -->
    <template v-if="tab === 'ordered'">
      <TransitionGroup v-if="tasks.length" name="list" tag="div" class="stack">
        <TaskCard
          v-for="task in tasks"
          :key="task.id"
          :task="task"
          @toggle="onToggle"
          @remove="onRemove"
        />
      </TransitionGroup>
      <div v-else class="empty">
        <div class="empty__mark" aria-hidden="true">✓</div>
        <h3>{{ emptyHint.title }}</h3>
        <p>{{ emptyHint.body }}</p>
      </div>
    </template>

    <!-- 无序表：按档位分组 -->
    <template v-else-if="tab === 'unordered'">
      <div v-if="groups.length" class="groups">
        <TransitionGroup name="group">
          <section v-for="group in groups" :key="group.priority" class="group">
            <header class="group__head">
              <span class="group__badge" :data-p="group.priority">
                {{ PRIORITY_LABELS[group.priority] }}
              </span>
              <span class="group__hint">{{ PRIORITY_HINTS[group.priority] }}</span>
              <span class="group__count">{{ group.items.length }}</span>
            </header>
            <TransitionGroup name="list" tag="div" class="group__body">
              <TaskCard
                v-for="task in group.items"
                :key="task.id"
                :task="task"
                compact
                @toggle="onToggle"
                @remove="onRemove"
              />
            </TransitionGroup>
          </section>
        </TransitionGroup>
      </div>
      <div v-else class="empty">
        <div class="empty__mark" aria-hidden="true">❖</div>
        <h3>{{ emptyHint.title }}</h3>
        <p>{{ emptyHint.body }}</p>
      </div>
    </template>

    <!-- 已完成 -->
    <template v-else>
      <TransitionGroup v-if="tasks.length" name="list" tag="div" class="stack">
        <TaskCard
          v-for="task in tasks"
          :key="task.id"
          :task="task"
          compact
          @toggle="onToggle"
          @remove="onRemove"
        />
      </TransitionGroup>
      <div v-else class="empty">
        <div class="empty__mark" aria-hidden="true">◷</div>
        <h3>{{ emptyHint.title }}</h3>
        <p>{{ emptyHint.body }}</p>
      </div>
    </template>

    <TaskComposer ref="composer" @created="refresh" />
  </div>
</template>

<style scoped>
.home {
  padding: 12px 12px 0;
}

/* ── 分段控件 ───────────────────────────────────────────────── */
.seg {
  position: relative;
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 2px;
  padding: 3px;
  margin-bottom: 14px;
  background: var(--bg-sunken);
  border-radius: var(--radius);
}

.seg__indicator {
  position: absolute;
  top: 3px;
  left: 3px;
  bottom: 3px;
  border-radius: var(--radius-sm);
  background: var(--bg-elevated);
  box-shadow: var(--shadow-sm);
  opacity: 0;
  pointer-events: none;
  transition: transform var(--dur) var(--ease), width var(--dur) var(--ease),
    opacity var(--dur-fast) var(--ease);
}

.seg__indicator--ready {
  opacity: 1;
}

.seg__item {
  position: relative;
  z-index: 1;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 5px;
  min-height: 34px;
  border-radius: var(--radius-sm);
  font-size: 13px;
  font-weight: 600;
  color: var(--text-tertiary);
  transition: color var(--dur-fast) var(--ease);
}

.seg__item--active {
  color: var(--text);
}

.seg__count {
  min-width: 17px;
  padding: 0 4px;
  border-radius: var(--radius-pill);
  background: var(--accent-soft);
  font-size: 10px;
  font-weight: 700;
  line-height: 16px;
}

/* ── 列表 ───────────────────────────────────────────────────── */
.stack {
  position: relative;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.groups {
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.group__head {
  display: flex;
  align-items: center;
  gap: 7px;
  margin-bottom: 4px;
  padding: 0 2px;
}

.group__badge {
  display: grid;
  place-items: center;
  width: 22px;
  height: 20px;
  border-radius: var(--radius-sm);
  background: var(--accent-soft);
  font-size: 12px;
  font-weight: 700;
  color: var(--text-secondary);
}

.group__badge[data-p='1'] {
  background: var(--danger-soft);
  color: var(--danger);
}
.group__badge[data-p='2'] {
  background: var(--warn-soft);
  color: var(--warn);
}

.group__hint {
  font-size: 11px;
  color: var(--text-tertiary);
}

.group__count {
  margin-left: auto;
  font-size: 11px;
  color: var(--text-tertiary);
  font-variant-numeric: tabular-nums;
}

.group__body {
  position: relative;
  background: var(--bg-elevated);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  overflow: hidden;
}

/* ── 空状态 ─────────────────────────────────────────────────── */
.empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  text-align: center;
  padding: 56px 24px;
  color: var(--text-tertiary);
}

.empty__mark {
  width: 54px;
  height: 54px;
  border-radius: 50%;
  background: var(--accent-soft);
  display: grid;
  place-items: center;
  font-size: 22px;
  margin-bottom: 14px;
}

.empty h3 {
  margin: 0 0 5px;
  font-size: 15px;
  color: var(--text-secondary);
}

.empty p {
  margin: 0;
  font-size: 12px;
  max-width: 260px;
  line-height: 1.6;
}

/* 分组自身的进出场 */
.group-enter-active,
.group-leave-active {
  transition: opacity var(--dur) var(--ease), transform var(--dur) var(--ease);
}
.group-enter-from,
.group-leave-to {
  opacity: 0;
  transform: translateY(-4px);
}

@media (min-width: 900px) {
  .home {
    padding: 20px 28px 0;
    max-width: 760px;
    margin: 0 auto;
  }
}
</style>
