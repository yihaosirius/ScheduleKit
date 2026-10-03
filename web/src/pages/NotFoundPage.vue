<script setup lang="ts">
/**
 * 404。给一个明确的"这里没有东西"，而不是静默回首页。
 *
 * 静默回首页会让"链接失效"和"页面正常打开"看起来完全一样——
 * 用户会以为自己点错了，而不是链接坏了。
 */
import { useRouter } from 'vue-router'

const router = useRouter()
</script>

<template>
  <div class="nf">
    <div class="nf__panel">
      <div class="nf__mark" aria-hidden="true">◌</div>
      <h1>这条路径不存在</h1>
      <p>
        可能是链接过期了，或者手输地址时打错了。
      </p>
      <button class="sk-btn" type="button" @click="router.replace({ name: 'home' })">
        回到任务
      </button>
    </div>
  </div>
</template>

<style scoped>
.nf {
  min-height: 100dvh;
  display: grid;
  place-items: center;
  padding: 24px;
}

.nf__panel {
  text-align: center;
  max-width: 300px;
}

.nf__mark {
  font-size: 40px;
  color: var(--text-tertiary);
  margin-bottom: 10px;
}

.nf__panel h1 {
  margin: 0 0 6px;
  font-size: 18px;
}

.nf__panel p {
  margin: 0 0 18px;
  font-size: 12px;
  line-height: 1.7;
  color: var(--text-tertiary);
}
</style>
