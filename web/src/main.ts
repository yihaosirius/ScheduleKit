import { createApp } from 'vue'
import App from './App.vue'
import router from './router'
import './styles/base.css'

/**
 * 主题：跟随系统，但允许手动覆盖。
 *
 * 手动选择存在 localStorage。**不跟随用户手动选择而实时改变**是刻意的：
 * 用户明确选了深色，就不该因为系统在日落时切换而跟着变。
 */
function applyTheme(): void {
  const saved = localStorage.getItem('sk-theme')
  const prefersDark = window.matchMedia('(prefers-color-scheme: dark)').matches
  const theme = saved === 'light' || saved === 'dark' ? saved : prefersDark ? 'dark' : 'light'
  document.documentElement.dataset.theme = theme
}

applyTheme()
window
  .matchMedia('(prefers-color-scheme: dark)')
  .addEventListener('change', () => {
    // 只有"没手动选过"时才跟随系统
    if (!localStorage.getItem('sk-theme')) applyTheme()
  })

const app = createApp(App)
app.use(router)
app.mount('#app')

/**
 * Service Worker：只做应用外壳缓存，**绝不缓存 /api/**。
 *
 * 只在 HTTPS 下注册：HTTP 页面里 navigator.serviceWorker 不可用，
 * 而生产环境（Caddy + 8443）是 HTTPS，本地开发用 dev server 不需要它。
 */
if ('serviceWorker' in navigator && window.isSecureContext) {
  window.addEventListener('load', () => {
    navigator.serviceWorker.register('/sw.js').catch(() => {
      // 注册失败不该影响应用本身：它只是"离线可用"这一个增强
    })
  })
}
