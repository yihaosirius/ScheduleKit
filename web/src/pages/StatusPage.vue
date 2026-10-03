<script setup lang="ts">
/**
 * 状态监测。
 *
 * 这一页的设计目标不是"展示一堆数字"，而是**回答"它还好吗"**。
 * 所以每一项都要有"正常范围"的暗示，异常项直接给出该做什么：
 *
 *   * `warnings` 放在最上面——它是服务端主动算出的"能跑但有问题"，
 *     正是用户不会主动检查、但出问题时回头会怀疑的东西。
 *   * 内存/磁盘给的是"用了多少 / 还有多少"，不是裸字节数。
 *   * LLM 那栏显示的是**当前实际生效**的配置，而不是"应该配成什么"。
 */
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { api, type Status } from '@/api/client'
import { report } from '@/state/store'
import { formatBytes, formatDuration } from '@/utils/format'

const router = useRouter()
const status = ref<Status | null>(null)
const loading = ref(true)
const refreshing = ref(false)
let timer: number | undefined

const PROVIDER_LABEL: Record<string, string> = {
  responses: 'Responses 协议',
  chat: 'Chat 兼容',
  mock: 'Mock（离线）',
}

async function load(): Promise<void> {
  refreshing.value = true
  try {
    status.value = await api.status()
  } catch (error) {
    report(error)
  } finally {
    loading.value = false
    refreshing.value = false
  }
}

/** 磁盘用量到了什么程度。80% 以上要提醒——这台机器还跑着别的东西。 */
const diskLevel = computed(() => {
  const percent = status.value?.storage.disk_used_percent ?? 0
  if (percent >= 90) return 'danger'
  if (percent >= 80) return 'warn'
  return 'ok'
})

const llmReady = computed(() => {
  const llm = status.value?.llm
  if (!llm) return false
  return llm.provider === 'mock' || llm.has_api_key
})

const totalTasks = computed(() => {
  const tasks = status.value?.counts.tasks ?? {}
  return (tasks.ordered ?? 0) + (tasks.unordered ?? 0) + (tasks.done ?? 0)
})

onMounted(() => {
  void load()
  // uptime / 存储会变，30 秒刷一次足够
  timer = window.setInterval(load, 30_000)
})

onUnmounted(() => {
  if (timer) window.clearInterval(timer)
})
</script>

<template>
  <div class="status">
    <header class="status__top">
      <h2 class="status__title">状态监测</h2>
      <button class="status__refresh" type="button" :disabled="refreshing" @click="load">
        {{ refreshing ? '刷新中…' : '刷新' }}
      </button>
    </header>

    <div v-if="loading" class="sk-muted status__loading">加载中…</div>

    <template v-else-if="status">
      <!-- 告警在最上面：这些是"能跑但有问题"的状态 -->
      <section v-if="status.warnings.length" class="warnings">
        <h3 class="warnings__title">需要注意</h3>
        <ul>
          <li v-for="(warning, index) in status.warnings" :key="index">{{ warning }}</li>
        </ul>
      </section>

      <!-- 概览 -->
      <section class="cards">
        <div class="tile">
          <span class="tile__label">运行时长</span>
          <strong class="tile__value">{{ formatDuration(status.uptime_seconds) }}</strong>
          <span class="tile__sub">版本 {{ status.version }}</span>
        </div>
        <div class="tile">
          <span class="tile__label">教学周</span>
          <strong class="tile__value">{{ status.week > 0 ? `第 ${status.week} 周` : '假期' }}</strong>
          <span class="tile__sub">按配置的学期起始日</span>
        </div>
        <div class="tile">
          <span class="tile__label">任务</span>
          <strong class="tile__value">{{ totalTasks }}</strong>
          <span class="tile__sub">
            有序 {{ status.counts.tasks.ordered ?? 0 }} · 无序
            {{ status.counts.tasks.unordered ?? 0 }} · 完成
            {{ status.counts.tasks.done ?? 0 }}
          </span>
        </div>
        <div class="tile" :class="{ 'tile--warn': (status.counts.drafts.pending ?? 0) > 0 }">
          <span class="tile__label">待确认草稿</span>
          <strong class="tile__value">{{ status.counts.drafts.pending ?? 0 }}</strong>
          <span class="tile__sub">
            已确认 {{ status.counts.drafts.confirmed ?? 0 }} · 已丢弃
            {{ status.counts.drafts.discarded ?? 0 }}
          </span>
        </div>
      </section>

      <!-- LLM -->
      <section class="card">
        <h3 class="card__title">
          LLM
          <span class="sk-chip" :class="llmReady ? 'sk-chip--ok' : 'sk-chip--danger'">
            {{ llmReady ? '可用' : '未配置' }}
          </span>
        </h3>
        <dl class="kv">
          <div><dt>协议</dt><dd>{{ PROVIDER_LABEL[status.llm.provider] ?? status.llm.provider }}</dd></div>
          <div><dt>模型</dt><dd><code>{{ status.llm.model }}</code></dd></div>
          <div><dt>Base URL</dt><dd><code>{{ status.llm.base_url }}</code></dd></div>
          <div>
            <dt>思考模式</dt>
            <dd>{{ status.llm.thinking ? '已开启（无法强制工具调用）' : '关闭' }}</dd>
          </div>
          <div>
            <dt>API Key</dt>
            <dd>{{ status.llm.has_api_key ? '已配置' : '未配置' }}</dd>
          </div>
        </dl>
        <p v-if="!llmReady" class="card__action">
          去
          <button type="button" @click="router.push({ name: 'settings' })">设置</button>
          填写 API Key，或先用 Mock 模式试用。
        </p>
      </section>

      <!-- 存储 -->
      <section class="card">
        <h3 class="card__title">存储</h3>
        <dl class="kv">
          <div><dt>磁盘</dt>
            <dd :class="`level--${diskLevel}`">
              {{ status.storage.disk_used_percent }}% 已用 ·
              剩 {{ formatBytes(status.storage.disk_free_bytes) }}
            </dd>
          </div>
          <div><dt>数据库</dt><dd>{{ formatBytes(status.storage.db_bytes) }}</dd></div>
          <div>
            <dt>上传图片</dt>
            <dd>
              {{ status.storage.upload_files }} 个文件 ·
              {{ formatBytes(status.storage.upload_bytes) }}
            </dd>
          </div>
          <div><dt>数据目录</dt><dd><code>{{ status.storage.data_dir }}</code></dd></div>
        </dl>
        <p v-if="diskLevel !== 'ok'" class="card__action card__action--warn">
          磁盘用量偏高。这台机器上还跑着别的服务，注意留出余量。
        </p>
      </section>

      <!-- 其它计数 -->
      <section class="card">
        <h3 class="card__title">其它</h3>
        <dl class="kv">
          <div><dt>记忆条目</dt>
            <dd>
              共 {{ status.counts.memories.total ?? 0 }} · 启用
              {{ status.counts.memories.enabled ?? 0 }} · 已标记
              {{ status.counts.memories.pinned ?? 0 }}
            </dd>
          </div>
          <div><dt>课程</dt><dd>{{ status.counts.courses }} 门</dd></div>
          <div><dt>时区</dt><dd>{{ status.timezone }}</dd></div>
          <div><dt>服务端时间</dt><dd><code>{{ status.server_time }}</code></dd></div>
        </dl>
      </section>
    </template>
  </div>
</template>

<style scoped>
.status {
  padding: 14px 12px 0;
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.status__top {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.status__title {
  margin: 0;
  font-size: 16px;
}

.status__refresh {
  font-size: 12px;
  color: var(--text-secondary);
}

.status__loading {
  padding: 48px 0;
  text-align: center;
  font-size: 13px;
}

/* ── 告警 ───────────────────────────────────────────────────── */
.warnings {
  padding: 12px 14px;
  border-radius: var(--radius);
  background: var(--warn-soft);
  border: 1px solid color-mix(in srgb, var(--warn) 32%, transparent);
}

.warnings__title {
  margin: 0 0 6px;
  font-size: 12px;
  font-weight: 700;
  color: var(--warn);
}

.warnings ul {
  margin: 0;
  padding-left: 16px;
  font-size: 11px;
  line-height: 1.75;
  color: var(--text-secondary);
}

/* ── 概览格 ─────────────────────────────────────────────────── */
.cards {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 8px;
}

.tile {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 11px 12px;
  border-radius: var(--radius);
  background: var(--bg-elevated);
  border: 1px solid var(--border);
}

.tile--warn {
  border-color: color-mix(in srgb, var(--warn) 38%, transparent);
}

.tile__label {
  font-size: 10px;
  font-weight: 600;
  color: var(--text-tertiary);
}

.tile__value {
  font-size: 19px;
  font-weight: 700;
  font-variant-numeric: tabular-nums;
  letter-spacing: -0.01em;
}

.tile__sub {
  font-size: 10px;
  color: var(--text-tertiary);
  line-height: 1.45;
}

/* ── 卡片 ───────────────────────────────────────────────────── */
.card {
  padding: 13px 14px;
  background: var(--bg-elevated);
  border: 1px solid var(--border);
  border-radius: var(--radius);
}

.card__title {
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 0 0 10px;
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  color: var(--text-tertiary);
}

.card__action {
  margin: 10px 0 0;
  font-size: 11px;
  color: var(--text-secondary);
}

.card__action button {
  font-weight: 700;
  color: var(--info);
}

.card__action--warn {
  color: var(--warn);
}

.kv {
  margin: 0;
  display: flex;
  flex-direction: column;
  gap: 7px;
}

.kv > div {
  display: grid;
  grid-template-columns: 78px 1fr;
  gap: 10px;
  align-items: baseline;
}

.kv dt {
  font-size: 11px;
  color: var(--text-tertiary);
}

.kv dd {
  margin: 0;
  font-size: 12px;
  overflow-wrap: anywhere;
}

.kv code {
  font-size: 10px;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  color: var(--text-secondary);
}

.level--ok {
  color: var(--ok);
}
.level--warn {
  color: var(--warn);
}
.level--danger {
  color: var(--danger);
}

@media (min-width: 900px) {
  .status {
    padding: 20px 28px 0;
    max-width: 760px;
    margin: 0 auto;
  }

  .cards {
    grid-template-columns: repeat(4, 1fr);
  }
}
</style>
