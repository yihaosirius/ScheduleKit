<script setup lang="ts">
/**
 * 确认页 —— 这条链路的终点，也是最重要的一屏。
 *
 * 布局是"材料 ↔ 结果"的左右对照（手机上上下排列）：
 *   左边是原图与原始文字，右边是可编辑的解析结果。
 *   这个对照关系是刻意的：用户核对时最需要的是"这句话是从哪儿读出来的"。
 *
 * 三条关键行为：
 *
 * 1. **二选一在前端联动**。改截止时间就自动清优先级（反之亦然），
 *    与服务端约束一致，用户不会填完被打回。
 * 2. **服务端的 override 要显示出来**。`overrides` 是"代码推翻了模型输出"
 *    的记录——不显示的话，用户永远不知道那次识别被系统改过。
 * 3. **失效会话要能引导登录**。这个页面常常是从快捷指令直接打开的链接，
 *    Safari 里可能没有有效会话；401 时给出明确的下一步，而不是一个红条。
 */
import { computed, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import {
  api,
  CATEGORY_LABELS,
  PRIORITY_HINTS,
  PRIORITY_LABELS,
  type Category,
  type Draft,
  type DraftItem,
} from '@/api/client'
import { ApiError } from '@/api/client'
import { notify, report } from '@/state/store'
import { formatDraftLeft, isoToLocalInput, localInputToIso } from '@/utils/format'

const props = defineProps<{ id: string }>()
const router = useRouter()

const draft = ref<Draft | null>(null)
const items = ref<DraftItem[]>([])
const loading = ref(true)
const saving = ref(false)
const confirming = ref(false)
const needsLogin = ref(false)
const showRaw = ref(false)
const raw = ref<{ raw_output: string; fallback_note: string | null; overrides: string } | null>(null)

const categories = Object.keys(CATEGORY_LABELS) as Category[]
const priorities = [1, 2, 3, 4, 5]

const LLM_PATH_LABEL: Record<string, { text: string; kind: string }> = {
  tool_call: { text: '工具调用（约束解码）', kind: 'ok' },
  json_schema: { text: '结构化输出', kind: 'info' },
  json_object: { text: 'JSON 降级通道', kind: 'warn' },
}

/** 空结果（识别失败）与"识别到了但都是空"是两件事，处理不同 */
const isFailed = computed(() => (draft.value?.item_count ?? 0) === 0)

const canConfirm = computed(() => items.value.length > 0 && !confirming.value && !isFailed.value)

async function load(): Promise<void> {
  loading.value = true
  needsLogin.value = false
  try {
    const data = await api.draft(Number(props.id))
    draft.value = data
    items.value = data.items.map((item) => ({ ...item }))
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) {
      needsLogin.value = true
    } else {
      report(error)
    }
  } finally {
    loading.value = false
  }
}

async function confirm(): Promise<void> {
  if (!canConfirm.value) return
  confirming.value = true
  try {
    const itemId = draft.value!.id
    const result = await api.confirmDraft(
      itemId,
      items.value.map((item) => ({
        title: item.title,
        category: item.category,
        due_at: item.due_at,
        priority: item.priority,
        notes: item.notes,
      })),
    )
    notify(`已入库 ${result.created.length} 条任务`, 'ok')
    await router.replace({ name: 'home' })
  } catch (error) {
    report(error)
  } finally {
    confirming.value = false
  }
}

async function saveOnly(): Promise<void> {
  if (!draft.value) return
  saving.value = true
  try {
    await api.saveDraftItems(draft.value.id, items.value)
    notify('已暂存，稍后回来继续', 'ok')
  } catch (error) {
    report(error)
  } finally {
    saving.value = false
  }
}

async function discard(): Promise<void> {
  if (!draft.value) return
  if (!window.confirm('丢弃这条草稿？原图与解析结果都会被标记为已丢弃。')) return
  try {
    await api.discardDraft(draft.value.id)
    notify('已丢弃')
    await router.replace({ name: 'drafts' })
  } catch (error) {
    report(error)
  }
}

async function retry(): Promise<void> {
  if (!draft.value) return
  confirming.value = true
  try {
    const fresh = await api.retryDraft(draft.value.id)
    notify(`重新识别出 ${fresh.item_count} 条`, 'ok')
    // 重试会生成一条新草稿（旧的被标记丢弃），所以换到新 id
    await router.replace({ name: 'draft-confirm', params: { id: String(fresh.id) } })
    await load()
  } catch (error) {
    report(error)
  } finally {
    confirming.value = false
  }
}

async function toggleRaw(): Promise<void> {
  if (!draft.value) return
  showRaw.value = !showRaw.value
  if (showRaw.value && !raw.value) {
    try {
      raw.value = await api.draftRaw(draft.value.id)
    } catch (error) {
      report(error)
    }
  }
}

function removeItem(index: number): void {
  items.value.splice(index, 1)
}

function addItem(): void {
  items.value.push({ title: '', category: 'other', due_at: null, priority: 3, notes: '' })
}

/** 二选一联动：与服务端约束保持一致，避免"填完再被打回" */
function setDue(index: number, value: string): void {
  const item = items.value[index]
  if (!item) return
  const iso = localInputToIso(value)
  item.due_at = iso
  if (iso) item.priority = null
}

function setPriority(index: number, value: number | null): void {
  const item = items.value[index]
  if (!item) return
  item.priority = value
  if (value !== null) item.due_at = null
}

/** 编辑标题时防止空标题提交（服务端会 422，但提前反馈更好） */
const invalidTitles = computed(() => items.value.some((item) => !item.title.trim()))

watch(invalidTitles, (value) => {
  if (value) confirmError.value = '有标题为空，请先填写或删掉那一条。'
  else confirmError.value = ''
})

const confirmError = ref('')

onMounted(load)
</script>

<template>
  <div class="confirm">
    <div v-if="loading" class="confirm__loading sk-muted">加载中…</div>

    <!-- 会话失效：这个页面常常是从快捷指令打开的链接，Safari 里可能没有会话 -->
    <div v-else-if="needsLogin" class="confirm__blocked">
      <div class="confirm__blocked-mark" aria-hidden="true">🔒</div>
      <h2>需要先登录网页</h2>
      <p>
        这个确认页需要会话。Safari 里还没有登录，或者密码改过导致会话失效了。
      </p>
      <button
        class="sk-btn"
        type="button"
        @click="router.push({ name: 'login', query: { redirect: `/drafts/${props.id}` } })"
      >
        去登录
      </button>
    </div>

    <template v-else-if="draft">
      <!-- 识别失败：给重试与丢弃，而不是一个空的编辑表单 -->
      <div v-if="isFailed" class="confirm__failed">
        <div class="confirm__failed-mark" aria-hidden="true">⚠</div>
        <h2>没有解析出条目</h2>
        <p v-if="draft.fallback_note" class="confirm__failed-why">{{ draft.fallback_note }}</p>
        <p class="sk-dim">
          原图与文字都还在草稿里。可以重试识别，或者直接丢弃。
        </p>
        <div class="confirm__failed-actions">
          <button class="sk-btn sk-btn--ghost" type="button" @click="discard">丢弃</button>
          <button class="sk-btn" type="button" :disabled="confirming" @click="retry">
            {{ confirming ? '重试中…' : '重试识别' }}
          </button>
        </div>
      </div>

      <template v-else>
        <!-- 顶部状态条 -->
        <header class="confirm__status">
          <span class="sk-chip">{{ draft.item_count }} 条待确认</span>
          <span
            class="sk-chip"
            :class="`sk-chip--${LLM_PATH_LABEL[draft.llm_path]?.kind ?? 'info'}`"
          >
            {{ LLM_PATH_LABEL[draft.llm_path]?.text ?? draft.llm_path }}
          </span>
          <span v-if="draft.memory_ids.length" class="sk-chip sk-chip--info">
            参考 {{ draft.memory_ids.length }} 条记忆
          </span>
          <span class="confirm__expire">{{ formatDraftLeft(draft.seconds_left) }}</span>
        </header>

        <!-- 服务端对模型输出的改动：必须让用户看见 -->
        <section v-if="draft.overrides.length" class="overrides">
          <header class="overrides__head">
            <span aria-hidden="true">✎</span>
            服务端改动了模型的输出（{{ draft.overrides.length }} 处）
          </header>
          <ul class="overrides__list">
            <li v-for="(note, index) in draft.overrides" :key="index">{{ note }}</li>
          </ul>
        </section>

        <div class="confirm__grid">
          <!-- 左：材料 -->
          <section class="material">
            <h3 class="material__title">原始材料</h3>
            <img
              v-if="draft.has_image && draft.image_url"
              class="material__image"
              :src="draft.image_url"
              alt="上传的原图"
              loading="lazy"
            />
            <pre v-if="draft.text_input" class="material__text">{{ draft.text_input }}</pre>
            <p v-if="!draft.has_image && !draft.text_input" class="sk-dim">没有原始材料。</p>
          </section>

          <!-- 右：可编辑的结果 -->
          <section class="results">
            <h3 class="results__title">解析结果（可修改）</h3>

            <TransitionGroup name="list" tag="div" class="results__list">
              <article v-for="(item, index) in items" :key="index" class="item">
                <div class="item__head">
                  <span class="item__index">{{ index + 1 }}</span>
                  <input
                    v-model="item.title"
                    class="item__title"
                    type="text"
                    maxlength="60"
                    placeholder="标题"
                    aria-label="标题"
                  />
                  <button
                    class="item__remove"
                    type="button"
                    aria-label="删除这一条"
                    @click="removeItem(index)"
                  >
                    ✕
                  </button>
                </div>

                <div class="item__row">
                  <span class="item__key">类别</span>
                  <div class="pills pills--sm">
                    <button
                      v-for="cat in categories"
                      :key="cat"
                      class="pill"
                      :class="{ 'pill--active': item.category === cat }"
                      type="button"
                      @click="item.category = cat"
                    >
                      {{ CATEGORY_LABELS[cat] }}
                    </button>
                  </div>
                </div>

                <!-- 二选一：填了截止时间就禁用优先级，反之亦然 -->
                <div class="item__shape">
                  <div class="item__shape-col">
                    <label class="item__key" :for="`due-${index}`">截止时间</label>
                    <input
                      :id="`due-${index}`"
                      class="sk-input sk-input--sm"
                      type="datetime-local"
                      :value="isoToLocalInput(item.due_at)"
                      @change="setDue(index, ($event.target as HTMLInputElement).value)"
                    />
                    <button
                      v-if="item.due_at"
                      class="item__clear"
                      type="button"
                      @click="setDue(index, '')"
                    >
                      清除截止时间，改用优先级
                    </button>
                  </div>

                  <div class="item__shape-or" aria-hidden="true">或</div>

                  <div class="item__shape-col">
                    <span class="item__key">优先级</span>
                    <div class="pills pills--sm">
                      <button
                        v-for="p in priorities"
                        :key="p"
                        class="pill pill--priority"
                        :class="{ 'pill--active': item.priority === p }"
                        :data-p="p"
                        type="button"
                        :disabled="item.due_at !== null"
                        @click="setPriority(index, p)"
                      >
                        {{ PRIORITY_LABELS[p] }}
                      </button>
                    </div>
                    <p v-if="item.priority" class="item__hint">
                      {{ PRIORITY_HINTS[item.priority] }}
                    </p>
                    <p v-else class="item__hint sk-dim">已按截止时间处理</p>
                  </div>
                </div>

                <div class="item__row item__row--notes">
                  <span class="item__key">备注</span>
                  <input
                    v-model="item.notes"
                    class="sk-input sk-input--sm"
                    type="text"
                    maxlength="60"
                    placeholder="可留空"
                    aria-label="备注"
                  />
                </div>
              </article>
            </TransitionGroup>

            <button class="results__add" type="button" @click="addItem">＋ 再加一条</button>
          </section>
        </div>

        <!-- 动作区 -->
        <footer class="actions">
          <p v-if="confirmError" class="actions__error">{{ confirmError }}</p>
          <div class="actions__row">
            <button class="sk-btn sk-btn--danger" type="button" :disabled="confirming" @click="discard">
              丢弃
            </button>
            <button class="sk-btn sk-btn--ghost" type="button" :disabled="saving" @click="saveOnly">
              {{ saving ? '暂存中…' : '暂存' }}
            </button>
            <button
              class="sk-btn actions__primary"
              type="button"
              :disabled="!canConfirm || invalidTitles"
              @click="confirm"
            >
              {{ confirming ? '入库中…' : `确认入库（${items.length}）` }}
            </button>
          </div>
        </footer>

        <!-- 排查入口 -->
        <section class="raw">
          <button class="raw__toggle" type="button" @click="toggleRaw">
            {{ showRaw ? '▾' : '▸' }} 模型原始输出（排查用）
          </button>
          <pre v-if="showRaw && raw" class="raw__body">{{ raw.raw_output || '（空）' }}</pre>
        </section>
      </template>
    </template>

    <div v-else class="confirm__loading sk-muted">找不到这条草稿。</div>
  </div>
</template>

<style scoped>
.confirm {
  padding: 12px 12px 24px;
}

.confirm__loading {
  padding: 48px 0;
  text-align: center;
  font-size: 13px;
}

/* ── 会话失效 ───────────────────────────────────────────────── */
.confirm__blocked,
.confirm__failed {
  display: flex;
  flex-direction: column;
  align-items: center;
  text-align: center;
  padding: 48px 20px;
  gap: 6px;
}

.confirm__blocked-mark,
.confirm__failed-mark {
  font-size: 30px;
  margin-bottom: 6px;
}

.confirm__blocked h2,
.confirm__failed h2 {
  margin: 0;
  font-size: 17px;
}

.confirm__blocked p,
.confirm__failed p {
  margin: 0;
  font-size: 12px;
  color: var(--text-secondary);
  max-width: 300px;
  line-height: 1.65;
}

.confirm__blocked .sk-btn {
  margin-top: 14px;
}

.confirm__failed-why {
  padding: 8px 10px;
  border-radius: var(--radius-sm);
  background: var(--danger-soft);
  color: var(--danger) !important;
  font-size: 11px !important;
  overflow-wrap: anywhere;
}

.confirm__failed-actions {
  display: flex;
  gap: 10px;
  margin-top: 14px;
}

/* ── 状态条 ─────────────────────────────────────────────────── */
.confirm__status {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
  margin-bottom: 10px;
}

.confirm__expire {
  margin-left: auto;
  font-size: 11px;
  color: var(--text-tertiary);
}

/* ── override 提示 ─────────────────────────────────────────── */
.overrides {
  padding: 10px 12px;
  margin-bottom: 12px;
  border-radius: var(--radius);
  background: var(--warn-soft);
  border: 1px solid color-mix(in srgb, var(--warn) 30%, transparent);
}

.overrides__head {
  font-size: 12px;
  font-weight: 600;
  color: var(--warn);
  margin-bottom: 4px;
}

.overrides__list {
  margin: 0;
  padding-left: 16px;
  font-size: 11px;
  line-height: 1.7;
  color: var(--text-secondary);
}

/* ── 左右对照 ───────────────────────────────────────────────── */
.confirm__grid {
  display: grid;
  gap: 14px;
}

.material__title,
.results__title {
  margin: 0 0 8px;
  font-size: 12px;
  font-weight: 700;
  color: var(--text-secondary);
  text-transform: none;
}

.material__image {
  width: 100%;
  border-radius: var(--radius);
  border: 1px solid var(--border);
  display: block;
}

.material__text {
  margin: 8px 0 0;
  padding: 10px 12px;
  border-radius: var(--radius);
  background: var(--bg-sunken);
  font-size: 12px;
  line-height: 1.65;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  font-family: inherit;
}

/* ── 条目编辑 ───────────────────────────────────────────────── */
.results__list {
  position: relative;
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.item {
  padding: 11px 12px;
  background: var(--bg-elevated);
  border: 1px solid var(--border);
  border-radius: var(--radius);
}

.item__head {
  display: grid;
  grid-template-columns: auto 1fr auto;
  align-items: center;
  gap: 8px;
  margin-bottom: 10px;
}

.item__index {
  width: 20px;
  height: 20px;
  border-radius: 50%;
  background: var(--accent-soft);
  display: grid;
  place-items: center;
  font-size: 11px;
  font-weight: 700;
  color: var(--text-secondary);
}

.item__title {
  width: 100%;
  min-height: 34px;
  padding: 5px 9px;
  border-radius: var(--radius-sm);
  border: 1px solid var(--border-strong);
  background: var(--bg);
  font-size: 14px;
  font-weight: 600;
}

.item__title:focus {
  outline: none;
  border-color: var(--text-secondary);
}

.item__remove {
  width: 26px;
  height: 26px;
  border-radius: var(--radius-sm);
  color: var(--text-tertiary);
  font-size: 12px;
}

.item__remove:active {
  background: var(--danger-soft);
  color: var(--danger);
}

.item__row {
  display: grid;
  grid-template-columns: 44px 1fr;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
}

.item__row--notes {
  margin-bottom: 0;
  margin-top: 8px;
}

.item__key {
  font-size: 11px;
  font-weight: 600;
  color: var(--text-tertiary);
}

.item__shape {
  display: grid;
  gap: 8px;
  padding: 9px 10px;
  border-radius: var(--radius-sm);
  background: var(--bg-sunken);
}

.item__shape-col {
  display: flex;
  flex-direction: column;
  gap: 5px;
}

.item__shape-or {
  font-size: 10px;
  color: var(--text-tertiary);
  text-align: center;
}

.item__clear {
  align-self: flex-start;
  font-size: 10px;
  color: var(--info);
}

.item__hint {
  margin: 0;
  font-size: 10px;
  color: var(--text-tertiary);
}

.sk-input--sm {
  min-height: 34px;
  padding: 5px 9px;
  font-size: 12px;
}

/* ── 药丸 ───────────────────────────────────────────────────── */
.pills {
  display: flex;
  flex-wrap: wrap;
  gap: 5px;
}

.pills--sm .pill {
  min-width: 34px;
  padding: 5px 9px;
  font-size: 11px;
}

.pill {
  padding: 7px 12px;
  border-radius: var(--radius-sm);
  background: var(--accent-soft);
  font-size: 12px;
  font-weight: 600;
  color: var(--text-secondary);
  transition: background var(--dur-fast) var(--ease), color var(--dur-fast) var(--ease),
    transform var(--dur-fast) var(--ease);
}

.pill:active:not(:disabled) {
  transform: scale(0.94);
}

.pill:disabled {
  opacity: 0.35;
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

.results__add {
  width: 100%;
  margin-top: 10px;
  padding: 11px;
  border-radius: var(--radius);
  border: 1px dashed var(--border-strong);
  font-size: 13px;
  color: var(--text-secondary);
  transition: background var(--dur-fast) var(--ease);
}

.results__add:active {
  background: var(--accent-soft);
}

/* ── 动作区 ─────────────────────────────────────────────────── */
.actions {
  position: sticky;
  bottom: 0;
  margin-top: 16px;
  padding: 12px 0 calc(4px + var(--safe-bottom));
  background: var(--bg);
}

.actions__error {
  margin: 0 0 8px;
  font-size: 12px;
  color: var(--danger);
}

.actions__row {
  display: grid;
  grid-template-columns: auto auto 1fr;
  gap: 8px;
}

.actions__primary {
  width: 100%;
}

/* ── 原始输出 ───────────────────────────────────────────────── */
.raw {
  margin-top: 14px;
}

.raw__toggle {
  font-size: 11px;
  color: var(--text-tertiary);
}

.raw__body {
  margin: 8px 0 0;
  padding: 10px;
  max-height: 320px;
  overflow: auto;
  border-radius: var(--radius-sm);
  background: var(--bg-sunken);
  font-size: 11px;
  line-height: 1.6;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
}

@media (min-width: 900px) {
  .confirm {
    padding: 20px 28px 24px;
    max-width: 980px;
    margin: 0 auto;
  }

  /* PC 上左右并排：材料在左、结果在右 */
  .confirm__grid {
    grid-template-columns: minmax(0, 340px) minmax(0, 1fr);
    gap: 20px;
    align-items: start;
  }

  .material {
    position: sticky;
    top: calc(var(--header-h) + 12px);
  }

  .item__shape {
    grid-template-columns: 1fr auto 1fr;
    align-items: start;
  }

  .actions {
    padding-bottom: 12px;
  }
}
</style>
