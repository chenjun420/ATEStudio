import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router'
import { useAuth } from '@/composables/useAuth'

/**
 * Route definitions for ATE Studio
 *
 * Structure:
 * - /login               - Login page (public, no layout)
 * - /                     - Portal home page (app entry cards)
 * - /node/*               - Node Management app (test station nodes, wrapped in AppLayout)
 * - /flow/*               - Flow Management app (sequences, flow node templates, scripts, binding, wrapped in AppLayout)
 * - /monitor/*            - Execution Monitoring app (wrapped in AppLayout)
 * - /system/*             - System Management app (wrapped in AppLayout)
 * - /operator/:station_id - Operator view (standalone, no layout)
 *
 * App menus are loaded from the database via GET /api/v1/apps/{id}/menus
 * The AppLayout component renders the sidebar dynamically based on the active app.
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

  // Portal home page
  {
    path: '/',
    name: 'Portal',
    component: () => import('@/views/Portal.vue'),
    meta: {
      title: 'ATE Studio',
    },
  },

  // Node Management app
  {
    path: '/node',
    component: () => import('@/layouts/AppLayout.vue'),
    children: [
      {
        path: 'stations',
        name: 'StationManagement',
        component: () => import('@/views/StationManagement.vue'),
        meta: { title: '节点列表' },
      },
      {
        path: 'stations/:id',
        name: 'NodeDetail',
        component: () => import('@/views/StationManagement.vue'),
        meta: { title: '节点详情' },
        props: true,
      },
    ],
  },

  // Flow Management app
  {
    path: '/flow',
    component: () => import('@/layouts/AppLayout.vue'),
    children: [
      {
        path: 'sequences',
        name: 'SequenceList',
        component: () => import('@/views/SequenceEditor/index.vue'),
        meta: { title: '流程列表' },
      },
      {
        path: 'editor',
        name: 'SequenceEditor',
        component: () => import('@/views/SequenceEditor/index.vue'),
        meta: { title: '流程编排' },
      },
      {
        path: 'editor/:id',
        name: 'SequenceEditorById',
        component: () => import('@/views/SequenceEditor/index.vue'),
        meta: { title: '流程编排' },
        props: true,
      },
      {
        path: 'templates',
        name: 'NodeTemplates',
        component: () => import('@/views/NodeTemplates.vue'),
        meta: { title: '流程节点模板' },
      },
      {
        path: 'scripts',
        name: 'ScriptManagement',
        component: () => import('@/views/ScriptManagement.vue'),
        meta: { title: '脚本管理' },
      },
      {
        path: 'binding',
        name: 'NodeFlowBinding',
        component: () => import('@/views/NodeFlowBinding.vue'),
        meta: { title: '节点流程绑定' },
      },
      {
        path: 'fixture-designer',
        name: 'FixtureDesigner',
        component: () => import('@/views/FixtureDesigner.vue'),
        meta: { title: '工装设计调试器' },
      },
    ],
  },

  // Execution Monitoring app
  {
    path: '/monitor',
    component: () => import('@/layouts/AppLayout.vue'),
    children: [
      {
        path: 'dashboard',
        name: 'Dashboard',
        component: () => import('@/views/Dashboard.vue'),
        meta: { title: '实时看板' },
      },
      {
        path: 'history',
        name: 'ExecutionHistory',
        component: () => import('@/views/ExecutionHistory.vue'),
        meta: { title: '执行历史' },
      },
      {
        path: 'measurements',
        name: 'MeasurementExplorer',
        component: () => import('@/components/MeasurementExplorer.vue'),
        meta: { title: '测量数据' },
      },
      {
        path: 'reports',
        name: 'Reports',
        component: () => import('@/views/Reports.vue'),
        meta: { title: '测试报告' },
      },
      {
        path: 'tracing',
        name: 'TracingViewer',
        component: () => import('@/views/TracingViewer.vue'),
        meta: { title: '追溯查询' },
      },
      {
        path: 'simulation',
        name: 'SimulationConsole',
        component: () => import('@/views/SimulationConsole.vue'),
        meta: { title: '仿真调试控制台' },
      },
    ],
  },

  // System Management app
  {
    path: '/system',
    component: () => import('@/layouts/AppLayout.vue'),
    children: [
      {
        path: 'settings',
        name: 'Settings',
        component: () => import('@/views/Settings/index.vue'),
        meta: { title: '系统设置' },
      },
      {
        path: 'users',
        name: 'UserManagement',
        component: () => import('@/views/UserManagement.vue'),
        meta: { title: '用户管理', requiresAdmin: true },
      },
      {
        path: 'roles',
        name: 'RoleManagement',
        component: () => import('@/views/RoleManagement.vue'),
        meta: { title: '角色权限', requiresAuth: true, requiresAdmin: true },
      },
      {
        path: 'changeover',
        name: 'ProductChangeover',
        component: () => import('@/views/ProductChangeover.vue'),
        meta: { title: '产品切换' },
      },
      {
        path: 'calibration',
        name: 'CalibrationPanel',
        component: () => import('@/views/CalibrationPanel.vue'),
        meta: { title: '校准管理' },
      },
      {
        path: 'fmea',
        name: 'FmeaManagement',
        component: () => import('@/views/FmeaManagement.vue'),
        meta: { title: 'FMEA管理' },
      },
      {
        path: 'knowledge-graph',
        name: 'KnowledgeGraph',
        component: () => import('@/views/KnowledgeGraph.vue'),
        meta: { title: '知识图谱' },
      },
      {
        path: 'traceability',
        name: 'TraceabilityMatrix',
        component: () => import('@/views/TraceabilityMatrix.vue'),
        meta: { title: '需求追溯矩阵' },
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
    },
    props: true,
  },

  // Legacy redirects
  { path: '/sequence', redirect: '/flow/sequences' },
  { path: '/sequence/:id', redirect: (to) => `/flow/editor/${to.params.id}` },
  { path: '/dashboard', redirect: '/monitor/dashboard' },
  { path: '/history', redirect: '/monitor/history' },
  { path: '/stations', redirect: '/node/stations' },
  { path: '/settings', redirect: '/system/settings' },
  { path: '/measurements', redirect: '/monitor/measurements' },

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
