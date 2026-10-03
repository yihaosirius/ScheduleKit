<script setup lang="ts">
/**
 * 可展开的分区（首页「已完成」用它，移动端与 PC 共用）。
 *
 * 两个刻意的选择：
 *
 * 1. **展开时才拉数据**。已完成列表会一直增长，而它默认是收起的——
 *    为了一个看不见的列表去请求一次没有意义。首次展开时才去拉，
 *    之后复用（`listsLoaded` 记录过状态）。
 *
 * 2. **高度用 `grid-template-rows: 0fr → 1fr` 动画，不用 `max-height`**。
 *    `max-height` 需要一个猜出来的上限值：猜小了下拉不到位，猜大了动画
 *    会在内容还没展开完时就结束（看起来像"卡了一下"）。grid 方案是按内容
 *    真实高度插值，没有这个取舍。
 */
import { ref, watch } from 'vue'

const props = withDefaults(
  defineProps<{
    title: string
    count?: number
    /** 是否已经加载过内容（决定展开时要不要拉） */
    loaded?: boolean
  }>(),
  { count: 0, loaded: false },
)

const emit = defineEmits<{ (event: 'expand'): void }>()

const open = ref(false)

// 首次展开时通知父组件去拉数据
watch(open, (value) => {
  if (value && !props.loaded) emit('expand')
})
</script>

<template>
  <section class="collapsible" :class="{ 'collapsible--open': open }">
    <button
      class="collapsible__head"
      type="button"
      :aria-expanded="open"
      @click="open = !open"
    >
      <span class="collapsible__chevron" aria-hidden="true">▸</span>
      <span class="collapsible__title">{{ title }}</span>
      <span v-if="count > 0" class="collapsible__count">{{ count }}</span>
      <span class="collapsible__spacer" />
      <span class="collapsible__hint">{{ open ? '收起' : '展开' }}</span>
    </button>

    <!-- 高度动画：外层 grid 0fr→1fr，内层 overflow:hidden -->
    <div class="collapsible__wrap">
      <div class="collapsible__body">
        <slot />
      </div>
    </div>
  </section>
</template>

<style scoped>
.collapsible {
  margin-top: 4px;
}

.collapsible__head {
  display: flex;
  align-items: center;
  gap: 7px;
  width: 100%;
  min-height: 38px;
  padding: 0 10px;
  border-radius: var(--radius-sm);
  color: var(--text-tertiary);
  transition: background var(--dur-fast) var(--ease), color var(--dur-fast) var(--ease);
}

.collapsible__head:active {
  background: var(--accent-soft);
}

@media (hover: hover) {
  .collapsible__head:hover {
    background: var(--accent-soft);
    color: var(--text-secondary);
  }
}

/* 展开时箭头转 90°。只有 transform，不动布局，所以不会引起重排。 */
.collapsible__chevron {
  display: inline-block;
  font-size: 10px;
  transition: transform var(--dur) var(--ease);
}

.collapsible--open .collapsible__chevron {
  transform: rotate(90deg);
}

.collapsible__title {
  font-size: 12px;
  font-weight: 600;
  letter-spacing: 0.01em;
}

.collapsible__count {
  min-width: 17px;
  padding: 0 5px;
  border-radius: var(--radius-pill);
  background: var(--accent-soft);
  font-size: 10px;
  font-weight: 700;
  line-height: 16px;
  color: var(--text-tertiary);
}

.collapsible__spacer {
  flex: 1;
}

.collapsible__hint {
  font-size: 10px;
  color: var(--text-tertiary);
  opacity: 0.75;
}

.collapsible__wrap {
  display: grid;
  grid-template-rows: 0fr;
  transition: grid-template-rows var(--dur-slow) var(--ease-out);
}

.collapsible--open .collapsible__wrap {
  grid-template-rows: 1fr;
}

.collapsible__body {
  overflow: hidden;
  min-height: 0;
}
</style>
