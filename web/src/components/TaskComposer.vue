<script setup lang="ts">
/**
 * 新建任务。
 *
 * 最有教育意义的一处是这个表单的**二选一交互**：
 * 服务端强制"截止时间与优先级恰好一个非空"，但前端不能等提交了才告诉用户。
 * 做法是一个显式的模式切换（有序 / 无序），选了哪个就只显示那一个输入框——
 * 用户不需要理解"为什么不能都填"，因为界面根本没给他这个机会。
 * 这比"两个都填完了再报错"友好得多。
 */
import { computed, ref, watch } from 'vue'
import { api, CATEGORY_LABELS, PRIORITY_HINTS, PRIORITY_LABELS, type Category } from '@/api/client'
import { loadTasks, notify, report } from '@/state/store'
import { isoToLocalInput, localInputToIso } from '@/utils/format'

const emit = defineEmits<{ (event: 'created'): void }>()

const open = ref(false)
const mode = ref<'ordered' | 'unordered'>('ordered')
const title = ref('')
const category = ref<Category>('homework')
const dueLocal = ref('')
const priority = ref(3)
const notes = ref('')
const saving = ref(false)

const titleRef = ref<HTMLInputElement | null>(null)

const categories = Object.keys(CATEGORY_LABELS) as Category[]
const priorities = [1, 2, 3, 4, 5]

/** 默认截止时间设为今天 23:59。作业绝大多数是当天 23:59，
 *  预填一个合理的值比留空让用户去点日期选择器省事得多。 */
function defaultDue(): string {
  const now = new Date()
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}T23:59`
}

watch(open, async (value) => {
  if (!value) return
  title.value = ''
  notes.value = ''
  dueLocal.value = defaultDue()
  priority.value = 3
  category.value = 'homework'
  // 打开后自动聚焦标题：多一次点击就多一分"懒得记"
  await new Promise((resolve) => window.setTimeout(resolve, 60))
  titleRef.value?.focus()
})

const canSubmit = computed(() => title.value.trim().length > 0 && !saving.value)

async function submit(): Promise<void> {
  if (!canSubmit.value) return
  saving.value = true
  try {
    await api.createTask({
      title: title.value.trim(),
      category: category.value,
      notes: notes.value.trim(),
      // 二选一：按当前模式只发一个字段
      ...(mode.value === 'ordered'
        ? { due_at: localInputToIso(dueLocal.value) }
        : { priority: priority.value }),
    })
    open.value = false
    notify('已添加', 'ok')
    await loadTasks('ordered')
    await loadTasks('unordered')
    emit('created')
  } catch (error) {
    report(error)
  } finally {
    saving.value = false
  }
}

/** 供外部（首页的"新建"按钮与空状态）调用 */
defineExpose({ open: () => (open.value = true) })

const previewIso = computed(() => isoToLocalInput(localInputToIso(dueLocal.value)))
</script>

<template>
  <button class="fab" type="button" aria-label="新建任务" @click="open = true">
    <span aria-hidden="true">＋</span>
  </button>

  <Teleport to="body">
    <Transition name="sheet">
      <div v-if="open" class="sheet" @click.self="open = false">
        <div class="sheet__panel" role="dialog" aria-modal="true" aria-label="新建任务">
          <div class="sheet__grabber" aria-hidden="true" />

          <div class="sheet__head">
            <h2>新建任务</h2>
            <button class="sheet__close" type="button" aria-label="关闭" @click="open = false">
              ✕
            </button>
          </div>

          <!-- 模式切换：这是"二选一"约束的界面表达 -->
          <div class="mode" role="radiogroup" aria-label="任务类型">
            <button
              class="mode__item"
              :class="{ 'mode__item--active': mode === 'ordered' }"
              type="button"
              role="radio"
              :aria-checked="mode === 'ordered'"
              @click="mode = 'ordered'"
            >
              <span class="mode__title">有截止时间</span>
              <span class="mode__hint">进有序表，按时间排</span>
            </button>
            <button
              class="mode__item"
              :class="{ 'mode__item--active': mode === 'unordered' }"
              type="button"
              role="radio"
              :aria-checked="mode === 'unordered'"
              @click="mode = 'unordered'"
            >
              <span class="mode__title">按优先级</span>
              <span class="mode__hint">进无序表，分 Ⅰ–Ⅴ 档</span>
            </button>
          </div>

          <div class="sk-field">
            <label class="sk-label" for="task-title">标题</label>
            <input
              id="task-title"
              ref="titleRef"
              v-model="title"
              class="sk-input"
              type="text"
              maxlength="200"
              placeholder="例如：第三章习题"
              @keyup.enter="submit"
            />
          </div>

          <div class="sk-field">
            <span class="sk-label">类别</span>
            <div class="pills">
              <button
                v-for="item in categories"
                :key="item"
                class="pill"
                :class="{ 'pill--active': category === item }"
                type="button"
                @click="category = item"
              >
                {{ CATEGORY_LABELS[item] }}
              </button>
            </div>
          </div>

          <!-- 按模式只显示一个输入 -->
          <div v-if="mode === 'ordered'" class="sk-field">
            <label class="sk-label" for="task-due">截止时间</label>
            <input id="task-due" v-model="dueLocal" class="sk-input" type="datetime-local" />
            <p v-if="previewIso" class="hint">将按你的本地时区提交：{{ previewIso }}</p>
          </div>

          <div v-else class="sk-field">
            <span class="sk-label">优先级</span>
            <div class="pills">
              <button
                v-for="item in priorities"
                :key="item"
                class="pill pill--priority"
                :class="{ 'pill--active': priority === item }"
                :data-p="item"
                type="button"
                @click="priority = item"
              >
                {{ PRIORITY_LABELS[item] }}
              </button>
            </div>
            <p class="hint">{{ PRIORITY_HINTS[priority] }}</p>
          </div>

          <div class="sk-field">
            <label class="sk-label" for="task-notes">备注（可选）</label>
            <textarea
              id="task-notes"
              v-model="notes"
              class="sk-input"
              rows="2"
              placeholder="只做奇数题……"
            />
          </div>

          <div class="sheet__actions">
            <button class="sk-btn sk-btn--ghost" type="button" @click="open = false">取消</button>
            <button class="sk-btn" type="button" :disabled="!canSubmit" @click="submit">
              {{ saving ? '保存中…' : '添加' }}
            </button>
          </div>
        </div>
      </div>
    </Transition>
  </Teleport>
</template>

<style scoped>
/* 悬浮按钮：右下角，避开底部 dock 与安全区 */
.fab {
  position: fixed;
  right: 16px;
  bottom: calc(var(--dock-h) + var(--safe-bottom) + 16px);
  z-index: 40;
  width: 52px;
  height: 52px;
  border-radius: 50%;
  background: var(--accent);
  color: var(--bg-elevated);
  font-size: 24px;
  line-height: 1;
  display: grid;
  place-items: center;
  box-shadow: var(--shadow-lg);
  transition: transform var(--dur-fast) var(--ease);
}

.fab:active {
  transform: scale(0.9);
}

/* ── 底部抽屉 ───────────────────────────────────────────────── */
.sheet {
  position: fixed;
  inset: 0;
  z-index: 60;
  background: rgba(0, 0, 0, 0.42);
  display: flex;
  align-items: flex-end;
  justify-content: center;
}

.sheet__panel {
  width: min(100%, 560px);
  max-height: 92dvh;
  overflow-y: auto;
  background: var(--bg-elevated);
  border-radius: var(--radius-lg) var(--radius-lg) 0 0;
  padding: 8px 16px calc(18px + var(--safe-bottom));
  box-shadow: var(--shadow-lg);
}

.sheet__grabber {
  width: 38px;
  height: 4px;
  border-radius: 2px;
  background: var(--border-strong);
  margin: 4px auto 12px;
}

.sheet__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 14px;
}

.sheet__head h2 {
  margin: 0;
  font-size: 17px;
}

.sheet__close {
  width: 30px;
  height: 30px;
  border-radius: var(--radius-sm);
  color: var(--text-tertiary);
  display: grid;
  place-items: center;
}

.sheet__actions {
  display: grid;
  grid-template-columns: 1fr 2fr;
  gap: 10px;
  margin-top: 6px;
}

/* ── 抽屉进出场 ─────────────────────────────────────────────── */
/* 面板从下方滑入，遮罩淡入。时长比普通过渡长一点（320ms），
   因为位移距离大；太快会显得"甩"进来。 */
.sheet-enter-active .sheet__panel,
.sheet-leave-active .sheet__panel {
  transition: transform var(--dur-slow) var(--ease-out);
}

.sheet-enter-active,
.sheet-leave-active {
  transition: opacity var(--dur) var(--ease);
}

.sheet-enter-from,
.sheet-leave-to {
  opacity: 0;
}

.sheet-enter-from .sheet__panel,
.sheet-leave-to .sheet__panel {
  transform: translateY(100%);
}

/* ── 模式切换 ───────────────────────────────────────────────── */
.mode {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 8px;
  margin-bottom: 16px;
  padding: 4px;
  background: var(--bg-sunken);
  border-radius: var(--radius);
}

.mode__item {
  display: flex;
  flex-direction: column;
  gap: 1px;
  padding: 9px 10px;
  border-radius: var(--radius-sm);
  text-align: left;
  transition: background var(--dur) var(--ease), box-shadow var(--dur) var(--ease);
}

.mode__item--active {
  background: var(--bg-elevated);
  box-shadow: var(--shadow-sm);
}

.mode__title {
  font-size: 13px;
  font-weight: 600;
}

.mode__hint {
  font-size: 10px;
  color: var(--text-tertiary);
}

/* ── 药丸按钮 ───────────────────────────────────────────────── */
.pills {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.pill {
  min-width: 42px;
  padding: 7px 12px;
  border-radius: var(--radius-sm);
  background: var(--accent-soft);
  font-size: 13px;
  font-weight: 600;
  color: var(--text-secondary);
  transition: background var(--dur-fast) var(--ease), color var(--dur-fast) var(--ease),
    transform var(--dur-fast) var(--ease);
}

.pill:active {
  transform: scale(0.95);
}

.pill--active {
  background: var(--accent);
  color: var(--bg-elevated);
}

.pill--priority[data-p='1'].pill--active {
  background: var(--danger);
  color: #fff;
}
.pill--priority[data-p='2'].pill--active {
  background: var(--warn);
  color: #fff;
}

.hint {
  margin: 6px 0 0;
  font-size: 11px;
  color: var(--text-tertiary);
  overflow-wrap: anywhere;
}

@media (min-width: 900px) {
  .fab {
    bottom: 24px;
  }
  .sheet {
    align-items: center;
  }
  .sheet__panel {
    border-radius: var(--radius-lg);
    max-height: 86dvh;
  }
}
</style>
