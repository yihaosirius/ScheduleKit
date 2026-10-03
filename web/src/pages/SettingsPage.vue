<script setup lang="ts">
/**
 * 设置页：LLM 配置、学期设置、API Key。
 *
 * 两个关键的交互细节：
 *
 * 1. **API Key 输入框永远不预填**。服务端只返回 `has_api_key`，不回显密钥。
 *    这带来一个语义问题：空值到底是"不改"还是"清空"？
 *    解决方式是显式的两个按钮（"保存其他设置"用不传 key 的请求，
 *    "清除 Key"单独一次调用），而不是让用户去猜空字符串的含义。
 * 2. **改 provider 立即生效**（服务端每请求重读配置），但要提示
 *    `thinking` 与强制工具调用的互斥关系——这是 DeepSeek 的硬约束，
 *    用户不知道就会配出一个必然 400 的组合。
 */
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { api, type ApiKey, type Settings } from '@/api/client'
import { notify, report } from '@/state/store'
import { formatRelative } from '@/utils/format'

const router = useRouter()

const settings = ref<Settings | null>(null)
const keys = ref<ApiKey[]>([])
const loading = ref(true)
const saving = ref(false)

const form = ref({
  provider: 'responses' as 'responses' | 'chat' | 'mock',
  base_url: '',
  model: '',
  api_key: '',
  temperature: 1,
  timeout_seconds: 60,
  max_tokens: 32000,
  thinking: false,
  reasoning_effort: 'high',
  retry_count: 3,
  retry_backoff_seconds: 0.8,
})

const term = ref({ start_date: '', total_weeks: 16 })

const newKey = ref({ name: '', read_only: false })
const createdKey = ref<string | null>(null)
const busyKey = ref(false)

const PROVIDERS = [
  { value: 'responses', label: 'Responses 协议', hint: 'OpenAI /responses，支持结构化输出' },
  { value: 'chat', label: 'Chat 兼容', hint: 'OpenAI /chat/completions' },
  { value: 'mock', label: 'Mock（离线）', hint: '不联网，确定性抽取，用于验证链路' },
] as const

const hasKey = computed(() => settings.value?.llm.has_api_key ?? false)

async function load(): Promise<void> {
  loading.value = true
  try {
    const [config, keyList] = await Promise.all([api.settings(), api.keys()])
    settings.value = config
    keys.value = keyList
    form.value = {
      provider: config.llm.provider,
      base_url: config.llm.base_url,
      model: config.llm.model,
      api_key: '',
      temperature: config.llm.temperature,
      timeout_seconds: config.llm.timeout_seconds,
      max_tokens: config.llm.max_tokens,
      thinking: config.llm.thinking,
      reasoning_effort: config.llm.reasoning_effort,
      retry_count: config.llm.retry_count,
      retry_backoff_seconds: config.llm.retry_backoff_seconds,
    }
    term.value = {
      start_date: config.term.start_date,
      total_weeks: config.term.total_weeks,
    }
  } catch (error) {
    report(error)
  } finally {
    loading.value = false
  }
}

async function saveLlm(): Promise<void> {
  saving.value = true
  try {
    const payload: Record<string, unknown> = {
      provider: form.value.provider,
      base_url: form.value.base_url,
      model: form.value.model,
      temperature: form.value.temperature,
      timeout_seconds: form.value.timeout_seconds,
      max_tokens: form.value.max_tokens,
      thinking: form.value.thinking,
      reasoning_effort: form.value.reasoning_effort,
      retry_count: form.value.retry_count,
      retry_backoff_seconds: form.value.retry_backoff_seconds,
    }
    // 只有用户真的输入了才传 api_key：留空表示"不改"，
    // 否则每次改别的设置都会顺手把 Key 清掉。
    if (form.value.api_key.trim()) payload.api_key = form.value.api_key.trim()

    settings.value = await api.saveSettings(payload)
    form.value.api_key = ''
    notify('已保存，立即生效', 'ok')
  } catch (error) {
    report(error)
  } finally {
    saving.value = false
  }
}

async function clearApiKey(): Promise<void> {
  if (!window.confirm('清除已保存的 LLM API Key？清除后需要重新填写才能识别。')) return
  try {
    settings.value = await api.saveSettings({ api_key: '' })
    notify('已清除', 'ok')
  } catch (error) {
    report(error)
  }
}

async function saveTerm(): Promise<void> {
  try {
    settings.value = await api.saveTerm(term.value)
    notify('学期设置已保存', 'ok')
  } catch (error) {
    report(error)
  }
}

async function createKey(): Promise<void> {
  if (!newKey.value.name.trim()) {
    notify('给 Key 起个名字，便于日后辨认', 'error')
    return
  }
  busyKey.value = true
  try {
    const created = await api.createKey(newKey.value.name.trim(), newKey.value.read_only)
    createdKey.value = created.api_key
    newKey.value = { name: '', read_only: false }
    keys.value = await api.keys()
  } catch (error) {
    report(error)
  } finally {
    busyKey.value = false
  }
}

async function revokeKey(key: ApiKey): Promise<void> {
  if (!window.confirm(`吊销「${key.name}」？已在使用它的快捷指令会立刻失效。`)) return
  try {
    await api.revokeKey(key.id)
    keys.value = await api.keys()
    notify('已吊销', 'ok')
  } catch (error) {
    report(error)
  }
}

async function copyKey(): Promise<void> {
  if (!createdKey.value) return
  try {
    await navigator.clipboard.writeText(createdKey.value)
    notify('已复制到剪贴板', 'ok')
  } catch {
    notify('复制失败，请手动选中复制', 'error')
  }
}

onMounted(load)
</script>

<template>
  <div class="settings">
    <div v-if="loading" class="sk-muted settings__loading">加载中…</div>

    <template v-else-if="settings">
      <!-- ── 学期 ─────────────────────────────────────────────── -->
      <section class="card">
        <h2 class="card__title">学期</h2>
        <p class="card__sub">
          第 1 周的周一。它决定提示词里的「第几教学周」，也决定课表按周生效的判定。
        </p>
        <div class="two">
          <div class="sk-field">
            <label class="sk-label" for="term-start">第 1 周周一</label>
            <input id="term-start" v-model="term.start_date" class="sk-input" type="date" />
          </div>
          <div class="sk-field">
            <label class="sk-label" for="term-weeks">总周数</label>
            <input
              id="term-weeks"
              v-model.number="term.total_weeks"
              class="sk-input"
              type="number"
              min="1"
              max="52"
            />
          </div>
        </div>
        <p class="card__now">当前第 {{ settings.term.current_week || '—' }} 周</p>
        <button class="sk-btn sk-btn--block" type="button" @click="saveTerm">保存学期设置</button>
      </section>

      <!-- ── LLM ──────────────────────────────────────────────── -->
      <section class="card">
        <h2 class="card__title">LLM 识别</h2>
        <p class="card__sub">
          保存后<strong>立即生效</strong>，不需要重启服务。
        </p>

        <div class="sk-field">
          <span class="sk-label">协议</span>
          <div class="providers">
            <button
              v-for="item in PROVIDERS"
              :key="item.value"
              class="provider"
              :class="{ 'provider--active': form.provider === item.value }"
              type="button"
              @click="form.provider = item.value"
            >
              <span class="provider__label">{{ item.label }}</span>
              <span class="provider__hint">{{ item.hint }}</span>
            </button>
          </div>
        </div>

        <template v-if="form.provider !== 'mock'">
          <div class="sk-field">
            <label class="sk-label" for="llm-url">Base URL</label>
            <input
              id="llm-url"
              v-model="form.base_url"
              class="sk-input"
              type="url"
              placeholder="https://api.deepseek.com"
            />
          </div>

          <div class="sk-field">
            <label class="sk-label" for="llm-model">模型</label>
            <input id="llm-model" v-model="form.model" class="sk-input" type="text" />
          </div>

          <div class="sk-field">
            <label class="sk-label" for="llm-key">
              API Key
              <span v-if="hasKey" class="sk-chip sk-chip--ok">已配置</span>
              <span v-else class="sk-chip sk-chip--danger">未配置</span>
            </label>
            <input
              id="llm-key"
              v-model="form.api_key"
              class="sk-input"
              type="password"
              autocomplete="off"
              :placeholder="hasKey ? '留空表示不修改' : '粘贴你的 API Key'"
            />
            <p class="hint">
              接口<strong>从不回显明文</strong>，只告诉你"有没有配"。
              明文存在服务器的配置文件里（权限 0600，属服务账号）。
            </p>
          </div>

          <div class="two">
            <div class="sk-field">
              <label class="sk-label" for="llm-temp">温度</label>
              <input
                id="llm-temp"
                v-model.number="form.temperature"
                class="sk-input"
                type="number"
                step="0.1"
                min="0"
                max="2"
              />
            </div>
            <div class="sk-field">
              <label class="sk-label" for="llm-timeout">超时（秒）</label>
              <input
                id="llm-timeout"
                v-model.number="form.timeout_seconds"
                class="sk-input"
                type="number"
                min="5"
                max="600"
              />
            </div>
          </div>

          <div class="sk-field">
            <label class="sk-label" for="llm-tokens">max_tokens</label>
            <input
              id="llm-tokens"
              v-model.number="form.max_tokens"
              class="sk-input"
              type="number"
              min="256"
            />
            <p class="hint">
              输出的硬上限（Responses 协议里含思考 token）。撞到上限会被判定为"被截断"
              而不是"格式不对"——这两种情况的修法不同。
            </p>
          </div>

          <label class="switch">
            <input v-model="form.thinking" type="checkbox" />
            <span>开启思考模式</span>
          </label>

          <!-- 这是 DeepSeek 的硬约束，必须让用户知道 -->
          <p v-if="form.thinking" class="warn">
            ⚠️ <strong>思考模式与强制工具调用不可兼得。</strong>
            开启后服务端会拒绝具名 <code>tool_choice</code>（直接返回 400），
            所以主通道会退成「模型自行决定是否调工具」，识别可靠性下降。
            除了排查问题，一般建议关闭。
          </p>

          <div v-if="form.thinking" class="sk-field">
            <label class="sk-label" for="llm-effort">思考强度</label>
            <select id="llm-effort" v-model="form.reasoning_effort" class="sk-input">
              <option value="low">low</option>
              <option value="high">high</option>
              <option value="max">max</option>
            </select>
          </div>
        </template>

        <p v-else class="hint">
          Mock 模式不联网，按固定规则从文字里抽条目。适合先验证整条链路，
          以及在没有 Key 的时候试用。
        </p>

        <div class="card__actions">
          <button
            v-if="hasKey && form.provider !== 'mock'"
            class="sk-btn sk-btn--danger"
            type="button"
            @click="clearApiKey"
          >
            清除 Key
          </button>
          <button class="sk-btn" type="button" :disabled="saving" @click="saveLlm">
            {{ saving ? '保存中…' : '保存 LLM 设置' }}
          </button>
        </div>
      </section>

      <!-- ── API Key ──────────────────────────────────────────── -->
      <section class="card">
        <h2 class="card__title">API Key</h2>
        <p class="card__sub">
          给快捷指令与小组件用。它们不需要 CSRF，直接放
          <code>Authorization: Bearer</code> 头里。
        </p>

        <!-- 明文只显示一次 -->
        <div v-if="createdKey" class="created">
          <p class="created__warn">
            ⚠️ <strong>这串明文只显示这一次。</strong>关掉就再也看不到了，请立刻存好。
          </p>
          <code class="created__value">{{ createdKey }}</code>
          <div class="created__actions">
            <button class="sk-btn sk-btn--ghost" type="button" @click="copyKey">复制</button>
            <button class="sk-btn" type="button" @click="createdKey = null">我已存好</button>
          </div>
        </div>

        <div class="newkey">
          <input
            v-model="newKey.name"
            class="sk-input"
            type="text"
            maxlength="60"
            placeholder="用途备注，如 iphone-shortcut"
          />
          <label class="switch switch--inline">
            <input v-model="newKey.read_only" type="checkbox" />
            <span>只读</span>
          </label>
          <button class="sk-btn" type="button" :disabled="busyKey" @click="createKey">新建</button>
        </div>

        <ul v-if="keys.length" class="keys">
          <li v-for="key in keys" :key="key.id" class="key" :class="{ 'key--revoked': key.revoked_at }">
            <div class="key__main">
              <div class="key__name">
                {{ key.name }}
                <span class="sk-chip">{{ key.scope === 'read' ? '只读' : '可写' }}</span>
                <span v-if="key.revoked_at" class="sk-chip sk-chip--danger">已吊销</span>
              </div>
              <div class="key__meta">
                <code>{{ key.prefix }}…</code>
                <span>
                  {{
                    key.last_used_at
                      ? `最近使用 ${formatRelative(key.last_used_at)}`
                      : '从未使用'
                  }}
                </span>
              </div>
            </div>
            <button
              v-if="!key.revoked_at"
              class="key__revoke"
              type="button"
              @click="revokeKey(key)"
            >
              吊销
            </button>
          </li>
        </ul>
        <p v-else class="hint">还没有 API Key。快捷指令需要它才能上传。</p>

        <p class="hint">
          建议给小组件用<strong>只读</strong> Key：小组件只需要读，
          即使泄漏也改不了你的任务。
        </p>
      </section>

      <!-- ── 链接 ─────────────────────────────────────────────── -->
      <section class="card card--flat">
        <button class="link" type="button" @click="router.push({ name: 'status' })">
          <span>状态监测</span>
          <span class="link__arrow">›</span>
        </button>
        <div class="server">
          <div><span>对外地址</span><code>{{ settings.server.public_url }}</code></div>
          <div><span>时区</span><code>{{ settings.server.timezone }}</code></div>
          <div><span>数据目录</span><code>{{ settings.server.data_dir }}</code></div>
          <div><span>监听</span><code>{{ settings.server.listen_host }}:{{ settings.server.listen_port }}</code></div>
        </div>
      </section>
    </template>
  </div>
</template>

<style scoped>
.settings {
  padding: 14px 12px 0;
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.settings__loading {
  padding: 48px 0;
  text-align: center;
  font-size: 13px;
}

.card {
  padding: 14px;
  background: var(--bg-elevated);
  border: 1px solid var(--border);
  border-radius: var(--radius);
}

.card--flat {
  padding: 0;
  overflow: hidden;
}

.card__title {
  margin: 0 0 3px;
  font-size: 15px;
}

.card__sub {
  margin: 0 0 14px;
  font-size: 11px;
  line-height: 1.65;
  color: var(--text-tertiary);
}

.card__sub code,
.hint code,
.warn code {
  padding: 1px 5px;
  border-radius: 4px;
  background: var(--accent-soft);
  font-size: 10px;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
}

.card__now {
  margin: 0 0 12px;
  font-size: 12px;
  color: var(--text-secondary);
}

.card__actions {
  display: grid;
  grid-auto-flow: column;
  grid-auto-columns: 1fr;
  gap: 10px;
  margin-top: 6px;
}

.two {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 10px;
}

.hint {
  margin: 6px 0 0;
  font-size: 11px;
  line-height: 1.65;
  color: var(--text-tertiary);
}

.warn {
  margin: 10px 0 14px;
  padding: 9px 11px;
  border-radius: var(--radius-sm);
  background: var(--warn-soft);
  font-size: 11px;
  line-height: 1.7;
  color: var(--text-secondary);
}

.warn strong {
  color: var(--warn);
}

.switch {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
  margin-bottom: 12px;
}

.switch--inline {
  margin-bottom: 0;
  white-space: nowrap;
}

.switch input {
  width: 18px;
  height: 18px;
}

/* ── 协议选择 ───────────────────────────────────────────────── */
.providers {
  display: grid;
  gap: 6px;
}

.provider {
  display: flex;
  flex-direction: column;
  gap: 1px;
  padding: 9px 11px;
  border-radius: var(--radius-sm);
  border: 1px solid var(--border-strong);
  text-align: left;
  transition: border-color var(--dur-fast) var(--ease), background var(--dur-fast) var(--ease);
}

.provider--active {
  border-color: var(--text-secondary);
  background: var(--accent-soft);
}

.provider__label {
  font-size: 13px;
  font-weight: 600;
}

.provider__hint {
  font-size: 10px;
  color: var(--text-tertiary);
}

/* ── 新建 Key ───────────────────────────────────────────────── */
.newkey {
  display: grid;
  grid-template-columns: 1fr auto auto;
  gap: 8px;
  align-items: center;
  margin-bottom: 12px;
}

.created {
  padding: 12px;
  margin-bottom: 14px;
  border-radius: var(--radius);
  background: var(--ok-soft);
  border: 1px solid color-mix(in srgb, var(--ok) 35%, transparent);
}

.created__warn {
  margin: 0 0 8px;
  font-size: 11px;
  line-height: 1.6;
  color: var(--text-secondary);
}

.created__warn strong {
  color: var(--ok);
}

.created__value {
  display: block;
  padding: 9px 10px;
  border-radius: var(--radius-sm);
  background: var(--bg);
  font-size: 11px;
  overflow-wrap: anywhere;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
}

.created__actions {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 8px;
  margin-top: 10px;
}

/* ── Key 列表 ───────────────────────────────────────────────── */
.keys {
  list-style: none;
  margin: 0 0 10px;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.key {
  display: grid;
  grid-template-columns: 1fr auto;
  align-items: center;
  gap: 10px;
  padding: 9px 11px;
  border-radius: var(--radius-sm);
  background: var(--bg-sunken);
}

.key--revoked {
  opacity: 0.5;
}

.key__name {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
  font-weight: 600;
}

.key__meta {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-top: 3px;
  font-size: 10px;
  color: var(--text-tertiary);
}

.key__meta code {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
}

.key__revoke {
  font-size: 11px;
  font-weight: 600;
  color: var(--danger);
}

/* ── 链接与服务器信息 ───────────────────────────────────────── */
.link {
  display: flex;
  align-items: center;
  justify-content: space-between;
  width: 100%;
  padding: 13px 14px;
  font-size: 14px;
  border-bottom: 1px solid var(--border);
}

.link:active {
  background: var(--accent-soft);
}

.link__arrow {
  color: var(--text-tertiary);
}

.server {
  padding: 12px 14px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.server > div {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 12px;
  font-size: 11px;
  color: var(--text-tertiary);
}

.server code {
  font-size: 10px;
  color: var(--text-secondary);
  overflow-wrap: anywhere;
  text-align: right;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
}

@media (min-width: 900px) {
  .settings {
    padding: 20px 28px 0;
    max-width: 720px;
    margin: 0 auto;
  }
}
</style>
