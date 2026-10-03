<script setup lang="ts">
/**
 * 草稿箱。
 *
 * 草稿是"识别结果还没进任务表"的中间态，所以这一页的核心信息是
 * **辨识度**：哪一条、识别出了几条、走的哪条通道（有没有降级）、还剩多久过期。
 *
 * 「走的哪条通道」这个徽标值得单独强调：`tool_call` 是约束解码的结果，
 * `json_object` 是靠 prompt 约束的结果，两者的可信度不一样。
 * 不标出来，用户就无从判断"这次结果要不要更仔细地看一遍"。
 */
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { api, type Draft } from '@/api/client'
import { notify, report } from '@/state/store'
import { formatDraftLeft } from '@/utils/format'

const router = useRouter()
const drafts = ref<Draft[]>([])
const loading = ref(true)
const busy = ref<number | null>(null)

const LLM_PATH_LABEL: Record<string, { text: string; kind: string }> = {
  tool_call: { text: '工具调用', kind: 'ok' },
  json_schema: { text: '结构化输出', kind: 'info' },
  json_object: { text: 'JSON 降级', kind: 'warn' },
}

const CHANNEL_LABEL: Record<string, string> = {
  web: '网页',
  shortcut: '快捷指令',
  api: '接口',
}

const hasAny = computed(() => drafts.value.length > 0)

async function load(): Promise<void> {
  loading.value = true
  try {
    const data = await api.drafts('pending')
    drafts.value = data.items
  } catch (error) {
    report(error)
  } finally {
    loading.value = false
  }
}

async function openDraft(draft: Draft): Promise<void> {
  // 没有解析结果的草稿（识别失败）直接进确认页意义不大，
  // 更适合"重试识别"。这里仍然进确认页，但页面会引导重试。
  await router.push({ name: 'draft-confirm', params: { id: String(draft.id) } })
}

async function discard(draft: Draft): Promise<void> {
  busy.value = draft.id
  try {
    await api.discardDraft(draft.id)
    drafts.value = drafts.value.filter((item) => item.id !== draft.id)
    notify('已丢弃', 'ok')
  } catch (error) {
    report(error)
  } finally {
    busy.value = null
  }
}

async function retry(draft: Draft): Promise<void> {
  busy.value = draft.id
  try {
    const fresh = await api.retryDraft(draft.id)
    // 重试是"原地更新"：服务端把旧草稿标成丢弃、生成同内容的新草稿。
    // 所以这里把它从列表里换掉，而不是追加。
    const index = drafts.value.findIndex((item) => item.id === draft.id)
    if (index >= 0) drafts.value.splice(index, 1)
    drafts.value.unshift(fresh)
    notify(`重新识别出 ${fresh.item_count} 条`, 'ok')
  } catch (error) {
    report(error)
  } finally {
    busy.value = null
  }
}

async function purgeAll(): Promise<void> {
  if (!window.confirm('清空草稿箱？这会永久删除所有待确认草稿，包括原图。')) return
  try {
    const result = await api.purgeDrafts()
    notify(result.message, 'ok')
    await load()
  } catch (error) {
    report(error)
  }
}

let timer: number | undefined

onMounted(() => {
  void load()
  // 过期倒计时要动，所以每 30 秒重算一次展示（不发请求）
  timer = window.setInterval(() => {
    // 触发响应式更新：把列表浅拷贝一份
    drafts.value = [...drafts.value]
  }, 30_000)
})

onUnmounted(() => {
  if (timer) window.clearInterval(timer)
})
</script>

<template>
  <div class="drafts">
    <div class="drafts__top">
      <h2 class="drafts__title">待确认 {{ drafts.length ? `(${drafts.length})` : '' }}</h2>
      <div class="drafts__actions">
        <button class="drafts__link" type="button" @click="load">刷新</button>
        <button v-if="hasAny" class="drafts__link drafts__link--danger" type="button" @click="purgeAll">
          清空
        </button>
      </div>
    </div>

    <p class="drafts__note">
      识别结果<strong>还没有进任务表</strong>。核对并确认后才会入库。
    </p>

    <div v-if="loading" class="sk-muted drafts__loading">加载中…</div>

    <TransitionGroup v-else-if="hasAny" name="list" tag="div" class="stack">
      <article
        v-for="draft in drafts"
        :key="draft.id"
        class="draft"
        :class="{ 'draft--degraded': draft.llm_path !== 'tool_call' }"
      >
        <button class="draft__main" type="button" @click="openDraft(draft)">
          <div class="draft__head">
            <span class="sk-chip">{{ CHANNEL_LABEL[draft.channel] ?? draft.channel }}</span>
            <span
              class="sk-chip"
              :class="`sk-chip--${LLM_PATH_LABEL[draft.llm_path]?.kind ?? 'info'}`"
            >
              {{ LLM_PATH_LABEL[draft.llm_path]?.text ?? draft.llm_path }}
            </span>
            <span class="draft__count">{{ draft.item_count }} 条</span>
          </div>

          <div v-if="draft.items.length" class="draft__items">
            <span v-for="(item, index) in draft.items.slice(0, 3)" :key="index" class="draft__item">
              {{ item.title }}
            </span>
            <span v-if="draft.items.length > 3" class="draft__more">
              还有 {{ draft.items.length - 3 }} 条
            </span>
          </div>
          <div v-else class="draft__items draft__items--empty">
            没有解析出条目{{ draft.fallback_note ? `：${draft.fallback_note}` : '' }}
          </div>

          <div class="draft__foot">
            <span v-if="draft.has_image" class="draft__flag">含图片</span>
            <span v-if="draft.overrides.length" class="draft__flag draft__flag--warn">
              服务端改了 {{ draft.overrides.length }} 处
            </span>
            <span class="draft__expire">{{ formatDraftLeft(draft.seconds_left) }}</span>
          </div>
        </button>

        <div class="draft__side">
          <button
            class="draft__act"
            type="button"
            :disabled="busy === draft.id"
            @click="retry(draft)"
          >
            重试
          </button>
          <button
            class="draft__act draft__act--danger"
            type="button"
            :disabled="busy === draft.id"
            @click="discard(draft)"
          >
            丢弃
          </button>
        </div>
      </article>
    </TransitionGroup>

    <div v-else class="empty">
      <div class="empty__mark" aria-hidden="true">◫</div>
      <h3>草稿箱是空的</h3>
      <p>
        用 iPhone 快捷指令拍一张作业截图，或手动录入的文字，都会先进入这里等你核对。
      </p>
    </div>
  </div>
</template>

<style scoped>
.drafts {
  padding: 14px 12px 0;
}

.drafts__top {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 6px;
}

.drafts__title {
  margin: 0;
  font-size: 16px;
}

.drafts__actions {
  display: flex;
  gap: 12px;
}

.drafts__link {
  font-size: 12px;
  color: var(--text-secondary);
}

.drafts__link--danger {
  color: var(--danger);
}

.drafts__note {
  margin: 0 0 14px;
  font-size: 11px;
  line-height: 1.6;
  color: var(--text-tertiary);
}

.drafts__loading {
  padding: 40px 0;
  text-align: center;
  font-size: 13px;
}

.stack {
  position: relative;
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.draft {
  display: grid;
  grid-template-columns: 1fr auto;
  background: var(--bg-elevated);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  overflow: hidden;
  transition: border-color var(--dur) var(--ease), transform var(--dur-fast) var(--ease);
}

/* 降级的草稿用琥珀色左条标出来：它意味着"这次结果可信度更低" */
.draft--degraded {
  border-left: 3px solid var(--warn);
}

.draft__main {
  display: block;
  width: 100%;
  padding: 11px 12px;
  text-align: left;
}

.draft__main:active {
  background: var(--accent-soft);
}

.draft__head {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
  margin-bottom: 7px;
}

.draft__count {
  margin-left: auto;
  font-size: 12px;
  font-weight: 700;
  font-variant-numeric: tabular-nums;
}

.draft__items {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.draft__item {
  font-size: 13px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.draft__items--empty {
  color: var(--text-tertiary);
  font-size: 12px;
  overflow-wrap: anywhere;
}

.draft__more {
  font-size: 11px;
  color: var(--text-tertiary);
}

.draft__foot {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 8px;
  font-size: 11px;
  color: var(--text-tertiary);
}

.draft__flag--warn {
  color: var(--warn);
}

.draft__expire {
  margin-left: auto;
  font-variant-numeric: tabular-nums;
}

.draft__side {
  display: flex;
  flex-direction: column;
  border-left: 1px solid var(--border);
}

.draft__act {
  flex: 1;
  min-width: 58px;
  padding: 0 12px;
  font-size: 12px;
  font-weight: 600;
  color: var(--text-secondary);
  transition: background var(--dur-fast) var(--ease);
}

.draft__act:active {
  background: var(--accent-soft);
}

.draft__act:disabled {
  opacity: 0.4;
}

.draft__act--danger {
  color: var(--danger);
  border-top: 1px solid var(--border);
}

.draft__act--danger:active {
  background: var(--danger-soft);
}

/* ── 空状态 ─────────────────────────────────────────────────── */
.empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  text-align: center;
  padding: 64px 24px;
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
  max-width: 280px;
  line-height: 1.6;
}

@media (min-width: 900px) {
  .drafts {
    padding: 20px 28px 0;
    max-width: 760px;
    margin: 0 auto;
  }
}
</style>
