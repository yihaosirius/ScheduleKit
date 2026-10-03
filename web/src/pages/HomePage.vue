<script setup lang="ts">
/**
 * 首页。
 *
 * **同一份 DOM，两种结构**，靠媒体查询切换（而不是渲染两套模板）：
 *
 *   移动端（< 900px）：顶部标签切「有序表 / 无序表」，一次只显示一列
 *   PC（≥ 900px）    ：有序表与无序表**平铺并排**，没有标签
 *
 * 为什么坚持一份 DOM：两套模板意味着两处要同步改，而它们必然会分叉
 * （改了一处忘了另一处，表现是"手机上好了、电脑上还是旧的"）。
 * 这里用 CSS 控制"显示哪些列、怎么排"，逻辑只有一份。
 *
 * 「已完成」在两种布局下都是**下方可展开、默认收起**：
 *   * 它不是待办，占主视野没有收益；
 *   * 但它会一直增长，平铺成第三列会把有序表挤得很难看。
 * 默认收起还有一个好处：不必为看不见的列表发请求（展开时才拉）。
 */
import { computed, onMounted, ref } from 'vue'
import TaskCard from '@/components/TaskCard.vue'
import TaskComposer from '@/components/TaskComposer.vue'
import CollapsibleSection from '@/components/CollapsibleSection.vue'
import { PRIORITY_HINTS, PRIORITY_LABELS } from '@/api/client'
import {
  deleteTask,
  doneTasks,
  groupByPriority,
  isLoaded,
  loadTasks,
  orderedTasks,
  state,
  toggleTask,
} from '@/state/store'

const composer = ref<InstanceType<typeof TaskComposer> | null>(null)

/** 移动端标签的两个视图（已完成不在标签里，见文件头说明） */
type Tab = 'ordered' | 'unordered'

const tab = ref<Tab>('ordered')
const segRef = ref<HTMLElement | null>(null)
const segIndicator = ref({ left: 0, width: 0, ready: false })

const TABS: Array<{ key: Tab; label: string; count: () => number }> = [
  { key: 'ordered', label: '有序表', count: () => state.counts.ordered ?? 0 },
  { key: 'unordered', label: '无序表', count: () => state.counts.unordered ?? 0 },
]

const ordered = computed(() => orderedTasks.value)
const unordered = computed(() => state.lists.unordered)
const done = computed(() => doneTasks.value)
const unorderedGroups = computed(() => groupByPriority(unordered.value))

/**
 * 是否 PC。用 `matchMedia` 而不是量窗口宽度：它能在跨过断点时**被动收到变化**
 * （例如把窗口拖窄），而不需要自己监听 resize 再算一遍。
 */
const desktop = ref(false)
let media: MediaQueryList | null = null

async function refresh(): Promise<void> {
  // 平铺要两列都填满（已完成留到展开时）；标签版只拉当前那个。
  // 这就是"PC 一次拉三个、移动端一次拉一个"的取舍落点。
  await loadTasks(desktop.value ? 'all' : tab.value)
}

async function ensureDoneLoaded(): Promise<void> {
  if (isLoaded('done')) return
  await loadTasks('done')
}

async function moveIndicator(): Promise<void> {
  const host = segRef.value
  if (!host) return
  await new Promise((resolve) => window.requestAnimationFrame(resolve))
  const target = host.querySelector<HTMLElement>('[data-active="true"]')
  if (!target) return
  segIndicator.value = { left: target.offsetLeft, width: target.offsetWidth, ready: true }
}

async function onTabChange(key: Tab): Promise<void> {
  tab.value = key
  if (!isLoaded(key)) await loadTasks(key)
  await moveIndicator()
}

async function onToggle(id: number): Promise<void> {
  await toggleTask(id)
}

async function onRemove(id: number): Promise<void> {
  await deleteTask(id)
}

onMounted(async () => {
  media = window.matchMedia('(min-width: 900px)')
  desktop.value = media.matches
  // 跨过断点时按新布局重新拉一次（标签版切平铺版需要补齐另一列）
  media.addEventListener('change', (event) => {
    desktop.value = event.matches
    void refresh().then(moveIndicator)
  })

  await refresh()
  await moveIndicator()
})

const orderedEmpty = {
  title: '没有带截止时间的任务',
  body: '点右下角的 ＋ 添加，或用 iPhone 快捷指令拍一张作业截图。',
}
const unorderedEmpty = {
  title: '没有按优先级排的任务',
  body: '没有明确截止时间的事情放这里，按 Ⅰ–Ⅴ 分档。',
}
</script>

<template>
  <div class="home" :class="{ 'home--tiled': desktop }">
    <!-- 标签：只有移动端有。PC 平铺时两列本身就是"选择"，
         再加标签会出现"点了标签但内容没变"的迷惑。 -->
    <div v-if="!desktop" ref="segRef" class="seg" role="tablist" aria-label="任务视图">
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
        @click="onTabChange(item.key)"
      >
        {{ item.label }}
        <span class="seg__count">{{ item.count() }}</span>
      </button>
    </div>

    <div class="home__body">
      <!-- ── 有序表 ─────────────────────────────────────────────── -->
      <section
        class="col col--ordered"
        :class="{ 'col--hidden-mobile': tab !== 'ordered' }"
        data-col="ordered"
        aria-label="有序表"
      >
        <header class="col__head">
          <h2 class="col__title">有序表</h2>
          <span class="col__sub">按截止时间</span>
          <span class="col__count">{{ state.counts.ordered ?? 0 }}</span>
        </header>

        <TransitionGroup v-if="ordered.length" name="list" tag="div" class="stack">
          <TaskCard
            v-for="task in ordered"
            :key="task.id"
            :task="task"
            @toggle="onToggle"
            @remove="onRemove"
          />
        </TransitionGroup>
        <div v-else class="empty">
          <div class="empty__mark" aria-hidden="true">✓</div>
          <h3>{{ orderedEmpty.title }}</h3>
          <p>{{ orderedEmpty.body }}</p>
        </div>
      </section>

      <!-- ── 无序表 ─────────────────────────────────────────────── -->
      <section
        class="col col--unordered"
        :class="{ 'col--hidden-mobile': tab !== 'unordered' }"
        data-col="unordered"
        aria-label="无序表"
      >
        <header class="col__head">
          <h2 class="col__title">无序表</h2>
          <span class="col__sub">按优先级</span>
          <span class="col__count">{{ state.counts.unordered ?? 0 }}</span>
        </header>

        <div v-if="unorderedGroups.length" class="groups">
          <section v-for="group in unorderedGroups" :key="group.priority" class="group">
            <header class="group__head">
              <span class="group__badge" :data-p="group.priority">
                {{ PRIORITY_LABELS[group.priority] }}
              </span>
              <span class="group__hint">{{ PRIORITY_HINTS[group.priority] }}</span>
              <span class="group__count">{{ group.items.length }}</span>
            </header>
            <div class="group__body">
              <TaskCard
                v-for="task in group.items"
                :key="task.id"
                :task="task"
                compact
                @toggle="onToggle"
                @remove="onRemove"
              />
            </div>
          </section>
        </div>
        <div v-else class="empty">
          <div class="empty__mark" aria-hidden="true">❖</div>
          <h3>{{ unorderedEmpty.title }}</h3>
          <p>{{ unorderedEmpty.body }}</p>
        </div>
      </section>
    </div>

    <!-- ── 已完成：两种布局都在下方，默认收起 ──────────────────── -->
    <CollapsibleSection
      title="已完成"
      :count="state.counts.done ?? 0"
      :loaded="isLoaded('done')"
      @expand="ensureDoneLoaded"
    >
      <div class="done-wrap">
        <TransitionGroup v-if="done.length" name="list" tag="div" class="done-list">
          <TaskCard
            v-for="task in done"
            :key="task.id"
            :task="task"
            compact
            @toggle="onToggle"
            @remove="onRemove"
          />
        </TransitionGroup>
        <p v-else class="done-empty">还没有完成的任务。勾选完成后会出现在这里。</p>
      </div>
    </CollapsibleSection>

    <TaskComposer ref="composer" @created="refresh" />
  </div>
</template>

<style scoped>
.home {
  padding: 12px 12px 0;
}

/* ── 标签（移动端） ─────────────────────────────────────────── */
.seg {
  position: relative;
  display: grid;
  grid-template-columns: repeat(2, 1fr);
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

/* ── 列 ─────────────────────────────────────────────────────── */
.home__body {
  display: flex;
  flex-direction: column;
  gap: 14px;
}

/* 移动端：不是当前标签的那一列直接不渲染。
   用 display:none 而不是 visibility/opacity —— 后两者仍参与布局，会留下空白。 */
.col--hidden-mobile {
  display: none;
}

.col__head {
  display: flex;
  align-items: baseline;
  gap: 7px;
  margin-bottom: 8px;
  padding: 0 2px;
}

.col__title {
  margin: 0;
  font-size: 12px;
  font-weight: 700;
  color: var(--text-secondary);
}

.col__sub {
  font-size: 10px;
  color: var(--text-tertiary);
}

.col__count {
  margin-left: auto;
  font-size: 11px;
  color: var(--text-tertiary);
  font-variant-numeric: tabular-nums;
}

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
  background: var(--bg-elevated);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  overflow: hidden;
}

/* ── 已完成 ─────────────────────────────────────────────────── */
.done-wrap {
  padding: 6px 0 2px;
}

.done-list {
  background: var(--bg-elevated);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  overflow: hidden;
}

.done-empty {
  margin: 0;
  padding: 14px 12px;
  font-size: 12px;
  color: var(--text-tertiary);
  text-align: center;
}

/* ── 空状态 ─────────────────────────────────────────────────── */
.empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  text-align: center;
  padding: 44px 24px;
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

/* ── PC：平铺 ───────────────────────────────────────────────── */
@media (min-width: 900px) {
  .home {
    padding: 20px 28px 24px;
    max-width: 1180px;
    margin: 0 auto;
  }

  /* 有序表与无序表并排平铺；已完成不在这行里（它在下方折叠区） */
  .home__body {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 22px;
    align-items: start;
  }

  /* PC 上两列都要显示（标签不存在，这个类不会命中，但显式写出来更清楚） */
  .col--hidden-mobile {
    display: block;
  }

  /* 平铺时列头是必要信息（标签没了） */
  .col__title {
    font-size: 13px;
  }

  /* 每列各自独立滚动：某一列很长时不该把整页撑开，
     否则另一列会跟着滚走，"左右对照"就失效了。 */
  .stack,
  .groups {
    max-height: calc(100vh - var(--header-h) - 168px);
    overflow-y: auto;
    padding-right: 2px;
  }
}
</style>
