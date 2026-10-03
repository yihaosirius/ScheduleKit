import { createRouter, createWebHashHistory, type RouteRecordRaw } from 'vue-router'

/**
 * 路由。
 *
 * **用 hash 路由（createWebHashHistory）而不是 history 路由**，这不是偷懒：
 *
 * 1. 服务端生成的确认页链接是 `/#/drafts/{id}`（见 app/routers/ingest.py）。
 *    快捷指令要在手机上直接打开它，hash 形式在任何静态托管/反代配置下都能用。
 * 2. history 路由要求服务端把**所有未知路径**回退到 index.html。
 *    而 `app/routers/ui.py` 里的回退必须小心排除 `/api/*`——一旦排除逻辑写错，
 *    接口 404 就会变成"返回了 HTML"，前端报的错会指向完全无关的地方。
 *    hash 路由让这个风险根本不存在。
 *
 * 代价：URL 里有个 `#`，SEO 不友好。但这是个自用应用，没有人会搜索它。
 */
const routes: RouteRecordRaw[] = [
  {
    path: '/',
    name: 'home',
    component: () => import('@/pages/HomePage.vue'),
    meta: { tab: 'tasks' },
  },
  {
    path: '/drafts',
    name: 'drafts',
    component: () => import('@/pages/DraftsPage.vue'),
    meta: { tab: 'drafts' },
  },
  {
    path: '/drafts/:id',
    name: 'draft-confirm',
    component: () => import('@/pages/DraftConfirmPage.vue'),
    props: true,
  },
  {
    path: '/courses',
    name: 'courses',
    component: () => import('@/pages/CoursesPage.vue'),
    meta: { tab: 'courses' },
  },
  {
    path: '/memories',
    name: 'memories',
    component: () => import('@/pages/MemoriesPage.vue'),
    meta: { tab: 'memories' },
  },
  {
    path: '/settings',
    name: 'settings',
    component: () => import('@/pages/SettingsPage.vue'),
    meta: { tab: 'settings' },
  },
  {
    path: '/status',
    name: 'status',
    component: () => import('@/pages/StatusPage.vue'),
    meta: { tab: 'settings' },
  },
  {
    path: '/login',
    name: 'login',
    component: () => import('@/pages/LoginPage.vue'),
    meta: { public: true },
  },
  {
    // 未知路径给一个明确的页面，而不是静默回首页——
    // 静默回首页会让"链接失效"和"页面正常打开"看起来一样
    path: '/:pathMatch(.*)*',
    name: 'not-found',
    component: () => import('@/pages/NotFoundPage.vue'),
    meta: { public: true },
  },
]

export const router = createRouter({
  history: createWebHashHistory(),
  routes,
  scrollBehavior(_to, _from, saved) {
    // 返回时恢复滚动位置：从确认页返回列表时，用户期望回到原来那条任务附近
    return saved ?? { top: 0 }
  },
})

export default router
