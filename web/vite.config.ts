import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { fileURLToPath, URL } from 'node:url'

// 构建产物直接落到服务端的静态目录，由 FastAPI 托管。
//
// 为什么产物提交入库：服务器上不装 Node。代价是仓库里多了一坨生成物，
// 换来的是"部署 = 传代码 + 重启"，没有构建步骤就没有"构建环境不对"这类问题。
// （见 docs/decisions.md：这是刻意的取舍，不是忘了写 CI。）
export default defineConfig({
  plugins: [vue()],

  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },

  build: {
    outDir: '../app/static/spa',
    emptyOutDir: true,
    // 带内容哈希的文件名：Caddy 对 /assets/* 设了 immutable 长缓存，
    // 内容变了文件名就变，不会拿到旧文件。
    assetsDir: 'assets',
    sourcemap: false,
    // 目标设为能跑到 iOS 15 的 Safari（PWA 的主战场）
    target: 'es2020',
    rollupOptions: {
      output: {
        // 把 Vue 单独拆出来：它几乎不会变，改业务代码时用户只需下载小包
        manualChunks: {
          vendor: ['vue', 'vue-router'],
        },
      },
    },
  },

  server: {
    port: 5173,
    // 开发时把 /api 代理到本地后端，避免跨域与 Cookie 问题。
    // 生产环境由 Caddy 统一处理，不需要这个。
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: false,
      },
      '/healthz': { target: 'http://127.0.0.1:8000' },
      '/manifest.webmanifest': { target: 'http://127.0.0.1:8000' },
    },
  },
})
