import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router'
import { useAuth } from '@/composables/useAuth'

/**
 * Route definitions for ATE Studio
 *
 * Two top-level modes, matching the navigation the design spec settled on:
 *
 * - /dev/*       产测开发 — author the test: requirements, cases, sequences,
 *                scripts, fixtures, and which station runs which flow.
 * - /ops/*       运行监控 — run the test: dashboards, execution data, station
 *                operations, fault diagnosis.
 * - /system/*    System administration, reached from the account dropdown. It
 *                is deliberately not a third tab: the spec's top bar has two.
 * - /operator/:station_id — the line screen at one station. Standalone, no
 *                layout, because it renders on the station's own display.
 *
 * Menus are not declared here. They come from the database via
 * GET /api/v1/apps/{id}/menus and are grouped two levels deep (group → page);
 * AppLayout renders whatever the server sends. That direction is deliberate:
 * the previous hardcoded horizontal menu and the seeded data could disagree,
 * and nothing noticed. tests/cloud/test_menu_routes_resolve.py now asserts that
 * every seeded path resolves against the literals below.
 *
 * Terminology: 节点 means two different things and only one of them was
 * renamed to 工位. A flow node is a step in the sequence graph (流程节点模板) and
 * keeps the word. A station is a physical position on the line, and everything
 * that used to say 节点 for that now says 工位.
 */

const routes: RouteRecordRaw[] = [
  // Login page (public, no layout)
  {
    path: '/login',
    name: 'Login',
    component: () => import('@/views/Login.vue'),
    meta: {
      title: 'Login',
      public: true,
    },
  },

  // The Portal page used to be a set of cards, one per app, because the top
  // bar had no notion of a mode. It now redirects: the shell carries the mode
  // switch, so the landing page is whichever mode you were last in.
  {
    path: '/',
    redirect: '/dev/traceability',
  },

  // ── 产测开发 ────────────────────────────────────────────────────────────
  {
    path: '/dev',
    component: () => import('@/layouts/AppLayout.vue'),
    children: [
      {
        path: 'traceability',
        name: 'TraceabilityMatrix',
        component: () => import('@/views/TraceabilityMatrix.vue'),
        meta: { title: '需求追溯矩阵', mode: 'test-dev' },
      },
      {
        path: 'condition-review',
        name: 'AteragReviewWizard',
        component: () => import('@/views/AteragReviewWizard.vue'),
        meta: { title: '条件评审向导', mode: 'test-dev' },
      },
      {
        path: 'sequences',
        name: 'SequenceList',
        component: () => import('@/views/SequenceEditor/index.vue'),
        meta: { title: '流程列表', mode: 'test-dev' },
      },
      {
        // Parameterless editor. The menu points here rather than at
        // `sequences/:id` because clicking a menu strips `:param` segments, so
        // a dynamic path would open the list while claiming to open the editor.
        path: 'editor',
        name: 'SequenceEditor',
        component: () => import('@/views/SequenceEditor/index.vue'),
        meta: { title: '流程编排', mode: 'test-dev' },
      },
      {
        path: 'editor/:id',
        name: 'SequenceEditorById',
        component: () => import('@/views/SequenceEditor/index.vue'),
        meta: { title: '流程编排', mode: 'test-dev' },
        props: true,
      },
      {
        path: 'templates',
        name: 'NodeTemplates',
        component: () => import('@/views/NodeTemplates.vue'),
        meta: { title: '流程节点模板', mode: 'test-dev' },
      },
      {
        path: 'scripts',
        name: 'ScriptManagement',
        component: () => import('@/views/ScriptManagement.vue'),
        meta: { title: '脚本管理', mode: 'test-dev' },
      },
      {
        path: 'fixture-designer',
        name: 'FixtureDesigner',
        component: () => import('@/views/FixtureDesigner.vue'),
        meta: { title: '工装设计调试器', mode: 'test-dev' },
      },
      {
        // 工位管理 — which station runs which flow. Was 节点流程绑定, which named
        // both halves with the same ambiguous word.
        path: 'station-management',
        name: 'StationManagement',
        component: () => import('@/views/StationManagement.vue'),
        meta: { title: '工位管理', mode: 'test-dev' },
      },
    ],
  },

  // ── 运行监控 ────────────────────────────────────────────────────────────
  {
    path: '/ops',
    component: () => import('@/layouts/AppLayout.vue'),
    children: [
      {
        path: 'dashboard',
        name: 'Dashboard',
        component: () => import('@/views/Dashboard.vue'),
        meta: { title: '实时看板', mode: 'runtime' },
      },
      {
        path: 'history',
        name: 'ExecutionHistory',
        component: () => import('@/views/ExecutionHistory.vue'),
        meta: { title: '执行历史', mode: 'runtime' },
      },
      {
        path: 'measurements',
        name: 'MeasurementExplorer',
        component: () => import('@/components/MeasurementExplorer.vue'),
        meta: { title: '测量数据', mode: 'runtime' },
      },
      {
        path: 'reports',
        name: 'Reports',
        component: () => import('@/views/Reports.vue'),
        meta: { title: '测试报告', mode: 'runtime' },
      },
      {
        path: 'tracing',
        name: 'TracingViewer',
        component: () => import('@/views/TracingViewer.vue'),
        meta: { title: '追溯查询', mode: 'runtime' },
      },
      {
        path: 'simulation',
        name: 'SimulationConsole',
        component: () => import('@/views/SimulationConsole.vue'),
        meta: { title: '仿真调试控制台', mode: 'runtime' },
      },
      {
        // The station registry: reads the plants / stations tables built for
        // the two-level context. Distinct from 工位执行器 below, which lists
        // the executor processes reporting heartbeats.
        path: 'stations',
        name: 'StationList',
        component: () => import('@/views/StationList.vue'),
        meta: { title: '工位列表', mode: 'runtime' },
      },
      {
        path: 'workers',
        name: 'WorkerRegistry',
        component: () => import('@/views/WorkerRegistry.vue'),
        meta: { title: '工位执行器', mode: 'runtime' },
      },
      {
        path: 'calibration',
        name: 'CalibrationPanel',
        component: () => import('@/views/CalibrationPanel.vue'),
        meta: { title: '校准管理', mode: 'runtime' },
      },
      {
        path: 'changeover',
        name: 'ProductChangeover',
        component: () => import('@/views/ProductChangeover.vue'),
        meta: { title: '产品切换', mode: 'runtime' },
      },
      {
        path: 'fault-cases',
        name: 'FaultCaseLibrary',
        component: () => import('@/views/FaultCaseLibrary.vue'),
        meta: { title: '工位故障案例库', mode: 'runtime' },
      },
      {
        path: 'fmea',
        name: 'FmeaManagement',
        component: () => import('@/views/FmeaManagement.vue'),
        meta: { title: 'FMEA管理', mode: 'runtime' },
      },
    ],
  },

  // ── 系统 ────────────────────────────────────────────────────────────────
  // Reached from the account dropdown, not the top bar. The pages stay menu
  // rows with server-checked `required_permissions` rather than dropdown
  // items gated on a client-side isAdmin flag; see
  // tests/cloud/test_admin_pages_in_menu.py for why that distinction matters.
  {
    path: '/system',
    component: () => import('@/layouts/AppLayout.vue'),
    children: [
      {
        path: 'settings',
        name: 'Settings',
        component: () => import('@/views/Settings/index.vue'),
        meta: { title: '系统设置', mode: 'system' },
      },
      {
        path: 'users',
        name: 'UserManagement',
        component: () => import('@/views/UserManagement.vue'),
        meta: { title: '用户管理', mode: 'system', requiresAdmin: true },
      },
      {
        path: 'roles',
        name: 'RoleManagement',
        component: () => import('@/views/RoleManagement.vue'),
        meta: { title: '角色与权限', mode: 'system', requiresAdmin: true },
      },
    ],
  },

  // Operator view (standalone — no layout)
  {
    path: '/operator/:station_id',
    name: 'OperatorView',
    component: () => import('@/views/OperatorView.vue'),
    meta: {
      title: 'Operator Station',
      description: 'Read-only operator interaction mode',
      public: false,
      mode: 'operator',
    },
    props: true,
  },

  // Redirects for links that predate the two-mode navigation. Kept rather than
  // removed: bookmarks and the operations manual point at these paths, and a
  // 404 is a worse answer than a redirect.
  { path: '/sequence', redirect: '/dev/sequences' },
  { path: '/sequence/:id', redirect: (to) => `/dev/editor/${to.params.id}` },
  { path: '/dashboard', redirect: '/ops/dashboard' },
  { path: '/history', redirect: '/ops/history' },
  { path: '/measurements', redirect: '/ops/measurements' },
  { path: '/settings', redirect: '/system/settings' },
  { path: '/stations', redirect: '/ops/stations' },
  // 节点管理 collapsed: the station registry and the worker registry are now
  // two pages under 运行监控, so the old prefix has no single destination.
  { path: '/node/stations', redirect: '/ops/stations' },
  { path: '/flow/binding', redirect: '/dev/station-management' },
  { path: '/flow/sequences', redirect: '/dev/sequences' },
  { path: '/flow/editor/:id', redirect: (to) => `/dev/editor/${to.params.id}` },
  { path: '/flow/editor', redirect: '/dev/editor' },
  { path: '/flow/templates', redirect: '/dev/templates' },
  { path: '/flow/scripts', redirect: '/dev/scripts' },
  { path: '/flow/fixture-designer', redirect: '/dev/fixture-designer' },
  { path: '/monitor/dashboard', redirect: '/ops/dashboard' },
  { path: '/monitor/history', redirect: '/ops/history' },
  { path: '/monitor/measurements', redirect: '/ops/measurements' },
  { path: '/monitor/reports', redirect: '/ops/reports' },
  { path: '/monitor/tracing', redirect: '/ops/tracing' },
  { path: '/monitor/simulation', redirect: '/ops/simulation' },
  { path: '/monitor/stations', redirect: '/ops/stations' },
  { path: '/system/changeover', redirect: '/ops/changeover' },
  { path: '/system/calibration', redirect: '/ops/calibration' },
  { path: '/system/fmea', redirect: '/ops/fmea' },
  { path: '/system/traceability', redirect: '/dev/traceability' },
  { path: '/system/aterag-review', redirect: '/dev/condition-review' },

  // 404
  {
    path: '/:pathMatch(.*)*',
    name: 'NotFound',
    component: () => import('@/views/NotFound.vue'),
    meta: {
      title: 'Page Not Found',
    },
  },
]

const router = createRouter({
  history: createWebHistory(),
  routes,
  scrollBehavior(_to, _from, savedPosition) {
    if (savedPosition) {
      return savedPosition
    }
    return { top: 0 }
  },
})

// Navigation guard — auth check + page title
router.beforeEach((to) => {
  const title = to.meta.title as string | undefined
  document.title = title ? `${title} | ATE Studio` : 'ATE Studio'

  const { isAuthenticated, isAdmin } = useAuth()

  // If going to /login and already authenticated, redirect to /
  if (to.path === '/login' && isAuthenticated.value) {
    return { path: '/' }
  }

  // If route is not public and user is not authenticated, redirect to login
  if (!to.meta.public && !isAuthenticated.value) {
    return { path: '/login', query: { redirect: to.fullPath } }
  }

  // Admin-only routes
  if (to.meta.requiresAdmin && !isAdmin.value) {
    return { path: '/' }
  }
})

export default router
