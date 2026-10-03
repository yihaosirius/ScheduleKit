<script setup lang="ts">
/**
 * 顶栏：左边是"此刻正在发生什么"，右边是当前页标题与主题切换。
 *
 * 左边那块是本应用的"时间感"来源：`第 3 周 · 正在上：电子电路基础`。
 * 它的数据来自服务端 `/api/timetable/now`——**不在前端自己算周次**，
 * 因为周次与"正在上"的判定依赖配置里的学期起始日，
 * 前端各算一套会与注入提示词的口径不一致（那种不一致最难排查）。
 */
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api, type NowInfo } from '@/api/client'
import { logout, state } from '@/state/store'

const route = useRoute()
const router = useRouter()

const now = ref<NowInfo | null>(null)
const theme = ref(document.documentElement.dataset.theme ?? 'light')
let timer: number | undefined

const TITLES: Record<string, string> = {
  home: '任务',
  drafts: '草稿箱',
  'draft-confirm': '确认录入',
  courses: '课表',
  memories: '记忆',
  settings: '设置',
  status: '状态监测',
}

async function refresh(): Promise<void> {
  if (!state.authenticated) return
  try {
    now.value = await api.now()
  } catch {
    // 顶栏的上下文是"锦上添花"，失败时保持上一次的值，不弹错误
    // （否则每一次网络抖动都会在顶栏弹一个红条）
  }
}

function toggleTheme(): void {
  const next = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark'
  document.documentElement.dataset.theme = next
  theme.value = next
  localStorage.setItem('sk-theme', next)
}

async function signOut(): Promise<void> {
  await logout()
  await router.replace({ name: 'login' })
}

onMounted(() => {
  void refresh()
  // 每分钟刷新一次：课表状态的变化粒度是分钟级，没必要更频繁
  timer = window.setInterval(refresh, 60_000)
})

onBeforeUnmount(() => {
  if (timer) window.clearInterval(timer)
})
</script>

<template>
  <header class="header">
    <div class="header__left">
      <div class="header__week">
        <template v-if="now && now.week > 0">第 {{ now.week }} 周</template>
        <template v-else-if="now">假期</template>
        <template v-else>ScheduleKit</template>
      </div>
      <div v-if="now?.around?.length" class="header__now">
        <span class="header__pulse" aria-hidden="true" />
        <span class="header__now-text">
          {{ now.around[0]!.relation_label }}：{{ now.around[0]!.course_name }}
        </span>
      </div>
      <div v-else-if="now" class="header__now header__now--idle">此刻没有课</div>
    </div>

    <div class="header__right">
      <span class="header__title">{{ TITLES[String(route.name)] ?? '' }}</span>
      <button
        class="header__icon"
        type="button"
        :aria-label="theme === 'dark' ? '切换到浅色' : '切换到深色'"
        @click="toggleTheme"
      >
        {{ theme === 'dark' ? '☀' : '☾' }}
      </button>
      <button class="header__icon" type="button" aria-label="退出登录" @click="signOut">⏻</button>
    </div>
  </header>
</template>

<style scoped>
.header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  height: var(--header-h);
  padding: 0 14px;
  padding-top: var(--safe-top);
  /* 半透明 + 模糊：滚动时内容从下面透出来，是 iOS 的手感来源。
     不支持 backdrop-filter 的浏览器会退化成纯色背景（下方有 fallback）。 */
  background: var(--bg);
  border-bottom: 1px solid var(--border);
  position: sticky;
  top: 0;
  z-index: 20;
}

@supports (backdrop-filter: blur(12px)) {
  .header {
    background: color-mix(in srgb, var(--bg) 78%, transparent);
    backdrop-filter: saturate(180%) blur(14px);
  }
}

.header__left {
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 1px;
}

.header__week {
  font-size: 13px;
  font-weight: 700;
  letter-spacing: 0.01em;
}

.header__now {
  display: flex;
  align-items: center;
  gap: 5px;
  font-size: 11px;
  color: var(--text-secondary);
  min-width: 0;
}

.header__now--idle {
  color: var(--text-tertiary);
}

.header__now-text {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* 一个小小的呼吸点：让"正在上课"这件事在余光里也能感知到 */
.header__pulse {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--ok);
  flex: 0 0 auto;
  animation: pulse 2.4s var(--ease) infinite;
}

@keyframes pulse {
  0%,
  100% {
    opacity: 1;
    transform: scale(1);
  }
  50% {
    opacity: 0.45;
    transform: scale(0.82);
  }
}

.header__right {
  display: flex;
  align-items: center;
  gap: 4px;
  flex: 0 0 auto;
}

.header__title {
  font-size: 12px;
  color: var(--text-tertiary);
  margin-right: 4px;
}

.header__icon {
  width: 32px;
  height: 32px;
  border-radius: var(--radius-sm);
  font-size: 15px;
  color: var(--text-secondary);
  display: grid;
  place-items: center;
  transition: background var(--dur-fast) var(--ease), transform var(--dur-fast) var(--ease);
}

.header__icon:active {
  transform: scale(0.9);
  background: var(--accent-soft);
}

@media (hover: hover) {
  .header__icon:hover {
    background: var(--accent-soft);
  }
}
</style>
