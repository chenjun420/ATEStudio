import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router'

/**
 * Route definitions for ATE Studio
 * 
 * Structure:
 * - /                     - Redirects to sequence editor (default landing)
 * - /sequence             - Sequence diagram editor (main workspace)
 * - /sequence/:id         - Open specific sequence
 * - /dashboard            - Production dashboard
 * - /measurements         - Measurement explorer with SPC charts
 * - /history              - Execution history with filters and detail panel
 * - /stations             - Station management
 * - /settings             - Application settings
 * - /operator/:station_id - Read-only operator interaction mode (no sidebar)
 */
const routes: RouteRecordRaw[] = [
  {
    path: '/',
    redirect: '/sequence',
  },
  {
    path: '/sequence',
    name: 'SequenceEditor',
    component: () => import('@/views/SequenceEditor/index.vue'),
    meta: {
      title: 'Sequence Editor',
      description: 'Create and edit sequence diagrams',
    },
  },
  {
    path: '/sequence/:id',
    name: 'SequenceEditorById',
    component: () => import('@/views/SequenceEditor/index.vue'),
    meta: {
      title: 'Sequence Editor',
      description: 'Edit sequence diagram',
    },
    props: true,
  },
  {
    path: '/dashboard',
    name: 'Dashboard',
    component: () => import('@/views/Dashboard.vue'),
    meta: {
      title: 'Dashboard',
      description: 'Production overview dashboard',
    },
  },
  {
    path: '/measurements',
    name: 'MeasurementExplorer',
    component: () => import('@/components/MeasurementExplorer.vue'),
    meta: {
      title: 'Measurement Explorer',
      description: 'SPC control charts and measurement analysis',
    },
  },
  {
    path: '/history',
    name: 'ExecutionHistory',
    component: () => import('@/views/ExecutionHistory.vue'),
    meta: {
      title: 'Execution History',
      description: 'Browse and filter execution history',
    },
  },
  {
    path: '/stations',
    name: 'StationManagement',
    component: () => import('@/views/StationManagement.vue'),
    meta: {
      title: 'Station Management',
      description: 'Workstation monitoring and management',
    },
  },
  {
    path: '/settings',
    name: 'Settings',
    component: () => import('@/views/Settings/index.vue'),
    meta: {
      title: 'Settings',
      description: 'Application settings',
    },
  },
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

// Navigation guard for page title updates
router.beforeEach((to) => {
  const title = to.meta.title as string | undefined
  document.title = title ? `${title} | ATE Studio` : 'ATE Studio'
})

export default router