<script setup lang="ts">
/**
 * 导航：手机上是底部 dock，PC 上变成左侧栏（同一份组件，靠 CSS 换方向）。
 *
 * 指示器用**一个绝对定位的元素滑动**，而不是给每个按钮加下边框：
 * 滑动的小块让"从哪来到哪去"变得可见，这是"类 Flutter"手感里最省力的一个技巧。
 */
import { computed, nextTick, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { state } from '@/state/store'

interface Tab {
  name: string
  label: string
  icon: string
  /** 需要显示计数的 key（对应 state.counts） */
  badge?: 'drafts' | 'ordered'
}

const tabs: Tab[] = [
  { name: 'home', label: '任务', icon: '✓', badge: 'ordered' },
  { name: 'drafts', label: '草稿', icon: '◫' },
  { name: 'courses', label: '课表', icon: '▦' },
  { name: 'memories', label: '记忆', icon: '❖' },
  { name: 'settings', label: '设置', icon: '⚙' },
]

const route = useRoute()
const dockRef = ref<HTMLElement | null>(null)
const indicator = ref({ left: 0, top: 0, width: 0, height: 0, ready: false })

const activeName = computed(() => String(route.meta.tab ?? route.name ?? 'home'))

/** 草稿数：首页也要显示，所以单独取一次 */
const draftCount = ref(0)
const taskCount = computed(() => state.counts.ordered ?? 0)

async function refreshBadges(): Promise<void> {
  if (!state.authenticated) return
  try {
    const { counts } = await (await import('@/api/client')).api.drafts()
    draftCount.value = counts.pending ?? 0
  } catch {
    /* 计数失败不影响导航 */
  }
}

function badgeFor(tab: Tab): number {
  if (tab.badge === 'drafts') return draftCount.value
  if (tab.badge === 'ordered') return taskCount.value
  return 0
}

/** 把指示器移到当前激活项上。用 offsetLeft/Top 而不是自己算宽度，
 *  这样 padding、字体变化都不用跟着改。 */
async function moveIndicator(): Promise<void> {
  await nextTick()
  const host = dockRef.value
  if (!host) return
  const target = host.querySelector<HTMLElement>('[data-active="true"]')
  if (!target) return
  indicator.value = {
    left: target.offsetLeft,
    top: target.offsetTop,
    width: target.offsetWidth,
    height: target.offsetHeight,
    ready: true,
  }
}

watch(() => [activeName.value, draftCount.value], moveIndicator, { immediate: true })
watch(
  () => state.authenticated,
  (value) => {
    if (value) void refreshBadges()
  },
  { immediate: true },
)

// 草稿数需要随"确认/丢弃"变化，所以定期刷一次。
// 5 秒是个折中：足够让计数看起来是活的，又不至于一直打服务端。
window.setInterval(refreshBadges, 5000)
</script>

<template>
  <nav ref="dockRef" class="dock" aria-label="主导航">
    <span
      class="dock__indicator"
      :class="{ 'dock__indicator--ready': indicator.ready }"
      :style="{
        transform: `translate(${indicator.left}px, ${indicator.top}px)`,
        width: `${indicator.width}px`,
        height: `${indicator.height}px`,
      }"
      aria-hidden="true"
    />

    <RouterLink
      v-for="tab in tabs"
      :key="tab.name"
      class="dock__item"
      :class="{ 'dock__item--active': activeName === tab.name }"
      :data-active="activeName === tab.name ? 'true' : 'false'"
      :to="{ name: tab.name }"
    >
      <span class="dock__icon">{{ tab.icon }}</span>
      <span class="dock__label">{{ tab.label }}</span>
      <span v-if="badgeFor(tab) > 0" class="dock__badge">{{ badgeFor(tab) }}</span>
    </RouterLink>
  </nav>
</template>

<style scoped>
.dock {
  position: fixed;
  left: 0;
  right: 0;
  bottom: 0;
  z-index: 30;
  display: grid;
  grid-template-columns: repeat(5, 1fr);
  height: calc(var(--dock-h) + var(--safe-bottom));
  padding-bottom: var(--safe-bottom);
  background: var(--bg);
  border-top: 1px solid var(--border);
}

@supports (backdrop-filter: blur(12px)) {
  .dock {
    background: color-mix(in srgb, var(--bg) 82%, transparent);
    backdrop-filter: saturate(180%) blur(14px);
  }
}

/* 滑动的激活指示块。默认不可见（首帧还没测量出位置），
   测量完加 --ready 才淡入——否则会看到它从左上角"飞"到正确位置。 */
.dock__indicator {
  position: absolute;
  left: 0;
  top: 0;
  border-radius: var(--radius-sm);
  background: var(--accent-soft);
  opacity: 0;
  transition: transform var(--dur) var(--ease), width var(--dur) var(--ease),
    height var(--dur) var(--ease), opacity var(--dur-fast) var(--ease);
  pointer-events: none;
}

.dock__indicator--ready {
  opacity: 1;
}

.dock__item {
  position: relative;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 1px;
  color: var(--text-tertiary);
  transition: color var(--dur-fast) var(--ease), transform var(--dur-fast) var(--ease);
  -webkit-user-select: none;
  user-select: none;
}

.dock__item:active {
  transform: scale(0.93);
}

.dock__item--active {
  color: var(--text);
}

.dock__icon {
  font-size: 17px;
  line-height: 1;
}

.dock__label {
  font-size: 10px;
  font-weight: 600;
}

.dock__badge {
  position: absolute;
  top: 4px;
  left: 50%;
  margin-left: 6px;
  min-width: 15px;
  height: 15px;
  padding: 0 4px;
  border-radius: var(--radius-pill);
  background: var(--danger);
  color: #fff;
  font-size: 9px;
  font-weight: 700;
  display: grid;
  place-items: center;
  line-height: 1;
}

/* ── PC：竖排侧栏 ─────────────────────────────────────────────── */
@media (min-width: 900px) {
  .dock {
    position: static;
    grid-template-columns: 1fr;
    grid-template-rows: repeat(5, 52px);
    align-content: start;
    gap: 4px;
    height: auto;
    padding: 12px 10px;
    border-top: none;
    border-right: 1px solid var(--border);
    background: transparent;
    backdrop-filter: none;
  }

  .dock__item {
    flex-direction: row;
    justify-content: flex-start;
    gap: 10px;
    padding: 0 12px;
    border-radius: var(--radius);
  }

  .dock__icon {
    font-size: 16px;
    width: 18px;
    text-align: center;
  }

  .dock__label {
    font-size: 13px;
  }

  .dock__badge {
    position: static;
    margin-left: auto;
  }
}
</style>
