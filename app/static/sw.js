/* ============================================================================
 * ScheduleKit Service Worker —— 只做"应用外壳"缓存。
 *
 * 三条规矩，每条都对应一类踩过的坑：
 *
 * 1. **绝不缓存任何 /api/ 响应。**
 *    任务列表、草稿这类数据必须实时。缓存了会出现"勾选完刷新又变回来"
 *    这种极难排查的错觉——因为问题不在勾选逻辑，而在于读到了旧数据。
 *
 * 2. **页面导航走 network-first。**
 *    服务端渲染的页面拿旧 HTML 就等于看到旧数据。本项目虽然是 SPA、
 *    HTML 几乎不变，但"服务端是真相"这条原则不该在缓存层被破例。
 *
 * 3. **静态资源也走 network-first，断网才回退缓存。**
 *    这条原本是 cache-first（理由是"静态资源有哈希指纹、很少变"）。
 *    **前提不成立**：本项目没有构建步骤产生的文件名指纹对 sw.js 本身不适用，
 *    而 VERSION 常量是手写的，没人会记得改。实测踩到过"改了 CSS 却拿不到新版，
 *    还以为是样式写错了"。代价是每个静态资源多一次条件请求
 *    （有 ETag/304 兜着，通常是空响应），换来的是"改了就能看到"。
 * ========================================================================== */

// 换版本号会让 activate 清掉旧缓存。改成 network-first 之后它不再承担
// "让改动生效"的责任，只负责别让废弃缓存永远堆着。
const VERSION = 'schedulekit-v1'

/** 预缓存的应用外壳。单个失败不该让整个安装失败。 */
const SHELL = ['/', '/manifest.webmanifest', '/static/icons/icon-192.png']

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches
      .open(VERSION)
      .then((cache) => Promise.allSettled(SHELL.map((url) => cache.add(url))))
      .then(() => self.skipWaiting()),
  )
})

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) => Promise.all(keys.filter((key) => key !== VERSION).map((key) => caches.delete(key))))
      .then(() => self.clients.claim()),
  )
})

self.addEventListener('fetch', (event) => {
  const { request } = event

  // 只处理 GET。POST/PUT/DELETE 一律直连——它们有副作用，缓存毫无意义。
  if (request.method !== 'GET') return

  const url = new URL(request.url)

  // 跨域请求不插手：我们只对自己同源的资源负责。
  if (url.origin !== self.location.origin) return

  // 红线 1：接口绝不进缓存，也不做离线回退。
  // 让它正常失败，前端才能给出"网络不可用"这种准确提示；
  // 如果这里返回一个旧的 JSON，前端会以为数据是新鲜的。
  if (url.pathname.startsWith('/api/') || url.pathname === '/healthz') return

  const isNavigation = request.mode === 'navigate'

  event.respondWith(
    fetch(request)
      .then((response) => {
        // 只缓存"完整成功"的响应：206 部分内容与 opaque 响应缓存了会出怪问题
        if (response.ok && response.type === 'basic') {
          const copy = response.clone()
          caches.open(VERSION).then((cache) => cache.put(request, copy)).catch(() => {})
        }
        return response
      })
      .catch(async () => {
        const cached = await caches.match(request)
        if (cached) return cached

        // 导航请求断网时回退到外壳：至少让 SPA 能启动，
        // 由前端自己去显示"网络不可用"。这比浏览器的离线错误页更有用。
        if (isNavigation) {
          const shell = await caches.match('/')
          if (shell) return shell
        }

        // 其它情况如实失败
        return new Response('离线，且没有缓存副本', {
          status: 503,
          statusText: 'Offline',
          headers: { 'Content-Type': 'text/plain; charset=utf-8' },
        })
      }),
  )
})
