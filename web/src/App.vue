<script setup lang="ts">
/**
 * 应用外壳：顶栏 + 内容区 + 底栏 dock（移动端）/ 侧栏（PC）。
 *
 * 布局策略是"同一套结构的两个方向"，不是两套 UI：
 *   * 手机：顶栏固定，底部是 dock，内容区可滚动
 *   * PC：顶栏与侧栏固定，dock 变成竖排侧栏
 * 断点用 900px——它在"手机横屏"与"平板竖屏"之间，
 * 低于它的设备用底部 dock 更顺手（拇指够得到）。
 */
import { computed, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import ToastHost from '@/components/ToastHost.vue'
import BottomDock from '@/components/BottomDock.vue'
import AppHeader from '@/components/AppHeader.vue'
import { bootstrap, state } from '@/state/store'

const route = useRoute()
const router = useRouter()

const isPublic = computed(() => route.meta.public === true)
const showShell = computed(() => !isPublic.value && state.authenticated)

onMounted(async () => {
  await bootstrap()

  // 未登录且目标页需要鉴权 → 去登录页。
  // 带 redirect 参数，登录后能回到用户原本想去的页面。
  if (!state.authenticated && !isPublic.value) {
    await router.replace({ name: 'login', query: { redirect: route.fullPath } })
  }
})
</script>

<template>
  <div class="shell" :class="{ 'shell--bare': !showShell }">
    <AppHeader v-if="showShell" />

    <main class="shell__main">
      <RouterView v-slot="{ Component }">
        <!-- 页面切换用淡入 + 轻微位移。用 mode="out-in" 避免两页同时存在时
             高度不一致造成的跳动。 -->
        <Transition name="page" mode="out-in">
          <component :is="Component" />
        </Transition>
      </RouterView>
    </main>

    <BottomDock v-if="showShell" />
    <ToastHost />
  </div>
</template>

<style scoped>
.shell {
  display: grid;
  grid-template-rows: auto 1fr auto;
  height: 100%;
  min-height: 100dvh;
}

.shell--bare {
  grid-template-rows: 1fr;
}

.shell__main {
  overflow-y: auto;
  overscroll-behavior-y: contain;
  /* 底部留出 dock 的高度 + 安全区，否则最后一条任务会被挡住 */
  padding-bottom: calc(var(--dock-h) + var(--safe-bottom) + 8px);
  -webkit-overflow-scrolling: touch;
}

/* 页面过渡 */
.page-enter-active,
.page-leave-active {
  transition: opacity var(--dur) var(--ease), transform var(--dur) var(--ease);
}

.page-enter-from {
  opacity: 0;
  transform: translateY(8px);
}

.page-leave-to {
  opacity: 0;
  transform: translateY(-4px);
}

/* ── PC：dock 变侧栏，内容区收窄居中 ───────────────────────────── */
@media (min-width: 900px) {
  .shell {
    grid-template-columns: var(--sidebar-w) 1fr;
    grid-template-rows: auto 1fr;
    grid-template-areas:
      'header header'
      'dock main';
  }

  .shell__main {
    grid-area: main;
    padding-bottom: 24px;
  }

  .shell :deep(.header) {
    grid-area: header;
  }

  .shell :deep(.dock) {
    grid-area: dock;
  }
}
</style>
