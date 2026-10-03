<script setup lang="ts">
/**
 * 登录页。
 *
 * 刻意做得很朴素：一个密码框。这是个人应用，没有用户名、没有注册、
 * 没有"忘记密码"——后者需要邮件通道，而邮件通道需要的东西比这个应用本身还多。
 * 忘了密码就在服务器上跑 `app.cli set-password`。
 */
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { login, state } from '@/state/store'

const route = useRoute()
const router = useRouter()

const password = ref('')
const busy = ref(false)
const passwordRef = ref<HTMLInputElement | null>(null)

const canSubmit = computed(() => password.value.length > 0 && !busy.value)

async function submit(): Promise<void> {
  if (!canSubmit.value) return
  busy.value = true
  const ok = await login(password.value)
  busy.value = false
  if (!ok) {
    password.value = ''
    passwordRef.value?.focus()
    return
  }
  const redirect = typeof route.query.redirect === 'string' ? route.query.redirect : null
  await router.replace(redirect ?? { name: 'home' })
}

onMounted(() => {
  // 已登录就不该看到这一页
  if (state.authenticated) void router.replace({ name: 'home' })
  passwordRef.value?.focus()
})
</script>

<template>
  <div class="login">
    <div class="login__panel">
      <div class="login__brand">
        <div class="login__mark" aria-hidden="true">✓</div>
        <h1>ScheduleKit</h1>
        <p>围绕学业构建的待办系统</p>
      </div>

      <form class="login__form" @submit.prevent="submit">
        <!-- 隐藏的账号字段：只为满足浏览器的可访问性/密码管理器要求。
             本应用是单用户、没有"用户名"这个概念，但 Chromium 会因为
             "密码表单缺少 username 字段" 报警告，而且密码管理器也更愿意保存
             带 username 的表单（否则可能每次都问"保存密码？"）。
             `autocomplete="username"` + 视觉隐藏是标准做法。 -->
        <input
          class="sr-only"
          type="text"
          name="username"
          autocomplete="username"
          tabindex="-1"
          aria-hidden="true"
          value="schedulekit"
          readonly
        />

        <label class="sk-label" for="password">管理员密码</label>
        <input
          id="password"
          ref="passwordRef"
          v-model="password"
          class="sk-input"
          type="password"
          name="password"
          autocomplete="current-password"
          placeholder="••••••••"
        />
        <button class="sk-btn sk-btn--block login__submit" type="submit" :disabled="!canSubmit">
          {{ busy ? '登录中…' : '登录' }}
        </button>
      </form>

      <p class="login__hint">
        忘了密码？在服务器上跑
        <code>python -m app.cli set-password</code>
        ——改密码会让所有既有会话立即失效。
      </p>
    </div>
  </div>
</template>

<style scoped>
.login {
  min-height: 100dvh;
  display: grid;
  place-items: center;
  padding: 24px;
}

.login__panel {
  width: min(100%, 340px);
}

.login__brand {
  text-align: center;
  margin-bottom: 28px;
}

.login__mark {
  width: 62px;
  height: 62px;
  margin: 0 auto 14px;
  border-radius: var(--radius-lg);
  background: var(--accent);
  color: var(--bg-elevated);
  display: grid;
  place-items: center;
  font-size: 30px;
  box-shadow: var(--shadow);
}

.login__brand h1 {
  margin: 0 0 4px;
  font-size: 21px;
  letter-spacing: -0.01em;
}

.login__brand p {
  margin: 0;
  font-size: 12px;
  color: var(--text-tertiary);
}

.login__submit {
  margin-top: 16px;
}

.login__hint {
  margin: 22px 0 0;
  font-size: 11px;
  line-height: 1.65;
  color: var(--text-tertiary);
  text-align: center;
}

.login__hint code {
  padding: 1px 5px;
  border-radius: 5px;
  background: var(--accent-soft);
  font-size: 10px;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
}
</style>
