<script setup lang="ts">
/**
 * 记忆条目管理。
 *
 * 这一页要传达一个容易误解的概念：**记忆是给模型的参考素材，不是待办**。
 * 所以文案里反复强调"不会变成任务"，而这一点确实需要强调——
 * 用户第一次看到"记忆"很可能以为它是"以后要做的事"。
 *
 * 「单条注入」是这一页最重要的交互：`pin_single` 是**一次性**意图，
 * 用掉之后服务端会自动清除。UI 必须让这件事可见，
 * 否则用户会以为开关失灵（他明明点了，下次却没带上）。
 */
import { computed, onMounted, ref } from 'vue'
import { api, type Course, type Memory } from '@/api/client'
import { notify, report } from '@/state/store'

const memories = ref<Memory[]>([])
const courses = ref<Course[]>([])
const selectedIds = ref<number[]>([])
const loading = ref(true)
const creating = ref(false)
const busy = ref<number | null>(null)

/** 新建/编辑表单 */
const form = ref({
  id: 0,
  title: '',
  content: '',
  tags: '',
  scope: 'global' as 'global' | 'course',
  course_id: null as number | null,
  enabled: true,
})

const isEditing = computed(() => form.value.id > 0)

async function load(): Promise<void> {
  loading.value = true
  try {
    const [list, timetable, selected] = await Promise.all([
      api.memories(),
      api.timetable().catch(() => null),
      api.selectedMemories().catch(() => null),
    ])
    memories.value = list.items
    courses.value = timetable?.courses ?? []
    selectedIds.value = selected?.memory_ids ?? []
  } catch (error) {
    report(error)
  } finally {
    loading.value = false
  }
}

function resetForm(): void {
  form.value = {
    id: 0,
    title: '',
    content: '',
    tags: '',
    scope: 'global',
    course_id: null,
    enabled: true,
  }
  creating.value = false
}

function startCreate(): void {
  resetForm()
  creating.value = true
}

function startEdit(memory: Memory): void {
  form.value = {
    id: memory.id,
    title: memory.title,
    content: memory.content,
    tags: memory.tags.join(','),
    scope: memory.scope,
    course_id: memory.course_id,
    enabled: memory.enabled,
  }
  creating.value = true
}

async function submit(): Promise<void> {
  if (!form.value.title.trim() || !form.value.content.trim()) {
    notify('标题与内容都不能为空', 'error')
    return
  }
  if (form.value.scope === 'course' && !form.value.course_id) {
    notify('scope 选「某门课」时必须选课程', 'error')
    return
  }
  busy.value = -1
  try {
    const payload = {
      title: form.value.title.trim(),
      content: form.value.content.trim(),
      tags: form.value.tags.trim(),
      scope: form.value.scope,
      course_id: form.value.scope === 'course' ? form.value.course_id : null,
      enabled: form.value.enabled,
    }
    if (isEditing.value) {
      await api.updateMemory(form.value.id, {
        ...payload,
        course_id_set: true,
      })
      notify('已更新', 'ok')
    } else {
      await api.createMemory(payload)
      notify('已添加', 'ok')
    }
    resetForm()
    await load()
  } catch (error) {
    report(error)
  } finally {
    busy.value = null
  }
}

async function toggleEnabled(memory: Memory): Promise<void> {
  busy.value = memory.id
  try {
    const updated = await api.updateMemory(memory.id, { enabled: !memory.enabled })
    const index = memories.value.findIndex((item) => item.id === memory.id)
    if (index >= 0) memories.value[index] = updated
  } catch (error) {
    report(error)
  } finally {
    busy.value = null
  }
}

async function togglePin(memory: Memory): Promise<void> {
  busy.value = memory.id
  try {
    const updated = memory.pin_single
      ? await api.unpinMemory(memory.id)
      : await api.pinMemory(memory.id)
    const index = memories.value.findIndex((item) => item.id === memory.id)
    if (index >= 0) memories.value[index] = updated
    if (updated.pin_single) {
      notify('已标记：下一次识别会带上这条', 'ok')
    }
    await refreshSelected()
  } catch (error) {
    report(error)
  } finally {
    busy.value = null
  }
}

async function refreshSelected(): Promise<void> {
  try {
    const selected = await api.selectedMemories()
    selectedIds.value = selected.memory_ids
  } catch {
    /* 只是提示，失败不影响主流程 */
  }
}

async function remove(memory: Memory): Promise<void> {
  if (!window.confirm(`删除记忆「${memory.title}」？`)) return
  busy.value = memory.id
  try {
    await api.deleteMemory(memory.id)
    memories.value = memories.value.filter((item) => item.id !== memory.id)
    notify('已删除', 'ok')
  } catch (error) {
    report(error)
  } finally {
    busy.value = null
  }
}

async function clearPins(): Promise<void> {
  try {
    const result = await api.clearPins()
    notify(result.message, 'ok')
    await load()
  } catch (error) {
    report(error)
  }
}

const pinnedCount = computed(() => memories.value.filter((item) => item.pin_single).length)

/** 是否会在下一次识别中被注入（用于给条目打"会注入"的标记） */
function willInject(memory: Memory): boolean {
  if (!memory.enabled && !memory.pin_single) return false
  return selectedIds.value.includes(memory.id)
}

onMounted(load)
</script>

<template>
  <div class="mem">
    <header class="mem__top">
      <div>
        <h2 class="mem__title">记忆</h2>
        <p class="mem__sub">
          长期约定与偏好，用来帮助模型消歧。<strong>它们不会变成任务。</strong>
        </p>
      </div>
      <button class="mem__btn" type="button" @click="startCreate">新建</button>
    </header>

    <div v-if="pinnedCount" class="mem__pinned">
      <span>已标记 {{ pinnedCount }} 条「单条注入」——下一次识别会带上它们，用掉后自动清除。</span>
      <button type="button" @click="clearPins">全部取消</button>
    </div>

    <!-- 编辑表单 -->
    <Transition name="expand">
      <section v-if="creating" class="form">
        <div class="sk-field">
          <label class="sk-label" for="mem-title">标题</label>
          <input
            id="mem-title"
            v-model="form.title"
            class="sk-input"
            type="text"
            maxlength="40"
            placeholder="例如：电子电路基础"
          />
        </div>

        <div class="sk-field">
          <label class="sk-label" for="mem-content">内容</label>
          <textarea
            id="mem-content"
            v-model="form.content"
            class="sk-input"
            rows="3"
            placeholder="例如：作业周三交，迟交扣分"
          />
          <p class="hint">写"约定"或"偏好"，不要写"要做的事"。</p>
        </div>

        <div class="sk-field">
          <span class="sk-label">作用范围</span>
          <div class="pills">
            <button
              class="pill"
              :class="{ 'pill--active': form.scope === 'global' }"
              type="button"
              @click="form.scope = 'global'"
            >
              所有识别都用
            </button>
            <button
              class="pill"
              :class="{ 'pill--active': form.scope === 'course' }"
              type="button"
              :disabled="courses.length === 0"
              @click="form.scope = 'course'"
            >
              只在某门课的上下文里
            </button>
          </div>
        </div>

        <div v-if="form.scope === 'course'" class="sk-field">
          <label class="sk-label" for="mem-course">课程</label>
          <select id="mem-course" v-model.number="form.course_id" class="sk-input">
            <option :value="null">请选择</option>
            <option v-for="course in courses" :key="course.id" :value="course.id">
              {{ course.name }}
            </option>
          </select>
          <p class="hint">
            「只在某门课」的条目只在那门课出现在上下文里时注入——这样识别英语作业时
            不会带上复变的约定。
          </p>
        </div>

        <div class="sk-field">
          <label class="sk-label" for="mem-tags">标签（逗号分隔，可选）</label>
          <input id="mem-tags" v-model="form.tags" class="sk-input" type="text" placeholder="课业,惯例" />
        </div>

        <label class="switch">
          <input v-model="form.enabled" type="checkbox" />
          <span>默认参与注入</span>
        </label>

        <div class="form__actions">
          <button class="sk-btn sk-btn--ghost" type="button" @click="resetForm">取消</button>
          <button class="sk-btn" type="button" :disabled="busy === -1" @click="submit">
            {{ busy === -1 ? '保存中…' : isEditing ? '保存修改' : '添加' }}
          </button>
        </div>
      </section>
    </Transition>

    <div v-if="loading" class="sk-muted mem__loading">加载中…</div>

    <TransitionGroup v-else-if="memories.length" name="list" tag="div" class="list">
      <article v-for="memory in memories" :key="memory.id" class="entry">
        <div class="entry__head">
          <h3 class="entry__title">{{ memory.title }}</h3>
          <span v-if="memory.pin_single" class="sk-chip sk-chip--warn">单条注入</span>
          <span v-else-if="willInject(memory)" class="sk-chip sk-chip--ok">会注入</span>
          <span v-else-if="!memory.enabled" class="sk-chip">已关闭</span>
          <span v-if="memory.scope === 'course'" class="sk-chip sk-chip--info">
            仅
            {{ courses.find((c) => c.id === memory.course_id)?.name ?? '某门课' }}
          </span>
        </div>

        <p class="entry__content">{{ memory.content }}</p>

        <div class="entry__foot">
          <div class="entry__tags">
            <span v-for="tag in memory.tags" :key="tag" class="entry__tag">{{ tag }}</span>
          </div>
          <div class="entry__acts">
            <button type="button" :disabled="busy === memory.id" @click="togglePin(memory)">
              {{ memory.pin_single ? '取消注入' : '单条注入' }}
            </button>
            <button type="button" :disabled="busy === memory.id" @click="toggleEnabled(memory)">
              {{ memory.enabled ? '关闭' : '启用' }}
            </button>
            <button type="button" @click="startEdit(memory)">编辑</button>
            <button
              class="entry__act--danger"
              type="button"
              :disabled="busy === memory.id"
              @click="remove(memory)"
            >
              删除
            </button>
          </div>
        </div>
      </article>
    </TransitionGroup>

    <div v-else class="empty">
      <div class="empty__mark" aria-hidden="true">❖</div>
      <h3>还没有记忆条目</h3>
      <p>
        加一条试试：「电子电路基础 —— 作业周三交」。
        之后拍一张只写着「这次作业」的截图，模型就能靠它补出课程名与日期。
      </p>
    </div>
  </div>
</template>

<style scoped>
.mem {
  padding: 14px 12px 0;
}

.mem__top {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 10px;
}

.mem__title {
  margin: 0;
  font-size: 16px;
}

.mem__sub {
  margin: 3px 0 0;
  font-size: 11px;
  line-height: 1.6;
  color: var(--text-tertiary);
  max-width: 260px;
}

.mem__btn {
  flex: 0 0 auto;
  padding: 7px 14px;
  border-radius: var(--radius-sm);
  background: var(--accent-soft);
  font-size: 12px;
  font-weight: 600;
}

.mem__pinned {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  padding: 9px 11px;
  margin-bottom: 12px;
  border-radius: var(--radius);
  background: var(--warn-soft);
  font-size: 11px;
  line-height: 1.55;
  color: var(--text-secondary);
}

.mem__pinned button {
  flex: 0 0 auto;
  font-size: 11px;
  font-weight: 600;
  color: var(--warn);
}

.mem__loading {
  padding: 40px 0;
  text-align: center;
  font-size: 13px;
}

/* ── 表单 ───────────────────────────────────────────────────── */
.form {
  padding: 14px;
  margin-bottom: 14px;
  background: var(--bg-elevated);
  border: 1px solid var(--border-strong);
  border-radius: var(--radius);
}

.form__actions {
  display: grid;
  grid-template-columns: auto 1fr;
  gap: 10px;
  margin-top: 6px;
}

.hint {
  margin: 6px 0 0;
  font-size: 11px;
  line-height: 1.6;
  color: var(--text-tertiary);
}

.pills {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.pill {
  padding: 7px 12px;
  border-radius: var(--radius-sm);
  background: var(--accent-soft);
  font-size: 12px;
  font-weight: 600;
  color: var(--text-secondary);
}

.pill--active {
  background: var(--accent);
  color: var(--bg-elevated);
}

.pill:disabled {
  opacity: 0.4;
}

.switch {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
  margin-bottom: 14px;
}

.switch input {
  width: 18px;
  height: 18px;
}

/* 展开动画：用 scaleY 会拉伸文字，用 grid-template-rows 0fr→1fr 才自然 */
.expand-enter-active,
.expand-leave-active {
  transition: opacity var(--dur) var(--ease), transform var(--dur) var(--ease);
}
.expand-enter-from,
.expand-leave-to {
  opacity: 0;
  transform: translateY(-8px);
}

/* ── 列表 ───────────────────────────────────────────────────── */
.list {
  position: relative;
  display: flex;
  flex-direction: column;
  gap: 9px;
}

.entry {
  padding: 11px 12px;
  background: var(--bg-elevated);
  border: 1px solid var(--border);
  border-radius: var(--radius);
}

.entry__head {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
  margin-bottom: 5px;
}

.entry__title {
  margin: 0;
  font-size: 14px;
  font-weight: 600;
}

.entry__content {
  margin: 0 0 8px;
  font-size: 13px;
  line-height: 1.6;
  color: var(--text-secondary);
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

.entry__foot {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
}

.entry__tags {
  display: flex;
  gap: 4px;
}

.entry__tag {
  padding: 1px 6px;
  border-radius: var(--radius-pill);
  background: var(--accent-soft);
  font-size: 10px;
  color: var(--text-tertiary);
}

.entry__acts {
  display: flex;
  gap: 10px;
  margin-left: auto;
  flex-wrap: wrap;
}

.entry__acts button {
  font-size: 11px;
  font-weight: 600;
  color: var(--text-secondary);
}

.entry__acts button:disabled {
  opacity: 0.4;
}

.entry__act--danger {
  color: var(--danger) !important;
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
  max-width: 300px;
  line-height: 1.65;
}

@media (min-width: 900px) {
  .mem {
    padding: 20px 28px 0;
    max-width: 760px;
    margin: 0 auto;
  }
}
</style>
