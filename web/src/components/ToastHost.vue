<script setup lang="ts">
/** 提示条。错误带追踪号，用户截图就能回查服务端日志。 */
import { dismiss, toasts } from '@/state/store'
</script>

<template>
  <div class="toasts" role="status" aria-live="polite">
    <TransitionGroup name="toast">
      <div
        v-for="toast in toasts.items"
        :key="toast.id"
        class="toast"
        :class="`toast--${toast.kind}`"
        @click="dismiss(toast.id)"
      >
        {{ toast.message }}
      </div>
    </TransitionGroup>
  </div>
</template>

<style scoped>
.toasts {
  position: fixed;
  left: 50%;
  transform: translateX(-50%);
  bottom: calc(var(--dock-h) + var(--safe-bottom) + 14px);
  z-index: 100;
  display: flex;
  flex-direction: column;
  gap: 8px;
  align-items: center;
  pointer-events: none;
  width: min(92vw, 520px);
}

.toast {
  pointer-events: auto;
  max-width: 100%;
  padding: 10px 15px;
  border-radius: var(--radius);
  background: var(--bg-elevated);
  border: 1px solid var(--border-strong);
  box-shadow: var(--shadow-lg);
  font-size: 13px;
  line-height: 1.45;
  /* 长错误要能换行，不能撑破屏幕 */
  overflow-wrap: anywhere;
  cursor: pointer;
}

.toast--error {
  border-color: color-mix(in srgb, var(--danger) 45%, transparent);
  color: var(--danger);
}

.toast--ok {
  border-color: color-mix(in srgb, var(--ok) 45%, transparent);
  color: var(--ok);
}

/* 从下方浮起：与"操作产生了反馈"这个心智模型一致 */
.toast-enter-active,
.toast-leave-active {
  transition: opacity var(--dur) var(--ease), transform var(--dur) var(--ease);
}

.toast-enter-from {
  opacity: 0;
  transform: translateY(12px) scale(0.96);
}

.toast-leave-to {
  opacity: 0;
  transform: translateY(-6px) scale(0.97);
}

@media (min-width: 900px) {
  .toasts {
    bottom: 24px;
  }
}
</style>
