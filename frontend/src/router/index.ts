import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router'

/**
 * Route definitions for ATE Studio
 * 
 * Structure:
 * - /                     - Redirects to sequence editor (default landing)
 * - /sequence             - Sequence diagram editor (main workspace)
 * - /sequence/:id         - Open specific sequence
 * - /settings             - Application settings
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
    path: '/settings',
    name: 'Settings',
    component: () => import('@/views/Settings/index.vue'),
    meta: {
      title: 'Settings',
      description: 'Application settings',
    },
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