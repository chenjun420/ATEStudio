<script setup lang="ts">
import { ref, onMounted, watch, computed } from 'vue'
import { useRouter, useRoute, RouterView } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { useApps } from '@/composables/useApps'
import { useAuth } from '@/composables/useAuth'
import PasswordChange from '@/views/PasswordChange.vue'
import {
  Monitor,
  Connection,
  DataLine,
  Setting,
  ArrowLeft,
  User,
  ArrowDown,
} from '@element-plus/icons-vue'

const router = useRouter()
const route = useRoute()
const { t } = useI18n()
const { apps, currentAppMenus, loading, loadApps, loadAppMenus } = useApps()
const { user, isAdmin, logout } = useAuth()

const passwordChangeRef = ref<InstanceType<typeof PasswordChange> | null>(null)

// Icon mapping
const iconMap: Record<string, typeof Monitor> = {
  Monitor,
  Connection,
  DataLine,
  Setting,
}

// Menu icon mapping for individual menu items
const menuIconMap: Record<string, typeof Monitor> = {
  List: Monitor,
  View: Monitor,
  CopyDocument: Monitor,
  Edit: Monitor,
  Document: Monitor,
  Link: Connection,
  Odometer: DataLine,
  Clock: DataLine,
  TrendCharts: DataLine,
  Tickets: DataLine,
  Tools: Setting,
  Switch: Setting,
  Aim: Setting,
}

// Determine which app is active based on the current route path
const activeApp = computed(() => {
  const path = route.path
  const found = apps.value.find((app) => {
    // Match by route prefix — each app's menus have routes under a known prefix
    const prefixes: Record<string, string> = {
      'node-mgmt': '/node/',
      'flow-mgmt': '/flow/',
      'exec-monitor': '/monitor/',
      'system': '/system/',
    }
    const prefix = prefixes[app.code]
    return prefix && path.startsWith(prefix)
  })
  return found || null
})

// Load menus when active app changes
watch(
  activeApp,
  async (app) => {
    if (app) {
      await loadAppMenus(app.id)
    }
  },
  { immediate: true }
)

// Ensure apps are loaded
onMounted(async () => {
  if (apps.value.length === 0) {
    await loadApps()
  }
  // If active app is set but menus not loaded, load them
  if (activeApp.value) {
    await loadAppMenus(activeApp.value.id)
  }
})

// Flatten menus for el-menu (handle top-level only, no nesting for now)
const flatMenus = computed(() => {
  if (!currentAppMenus.value) return []
  return currentAppMenus.value.menus
})

// Active menu based on current route
const activeMenu = computed(() => {
  const path = route.path
  // Find the menu that best matches the current path
  const match = flatMenus.value.find((m) => {
    // Convert route_path pattern (e.g. /node/stations/:id) to prefix
    const prefix = m.route_path.split('/:')[0]
    return path.startsWith(prefix)
  })
  return match?.route_path || route.path
})

function handleMenuSelect(index: string) {
  // Replace any :id params with empty for navigation
  const cleanPath = index.replace(/\/:[^/]+/g, '')
  router.push(cleanPath)
}

function goHome() {
  router.push('/')
}

function handleCommand(command: string): void {
  switch (command) {
    case 'settings':
      router.push('/system/settings')
      break
    case 'users':
      router.push('/system/users')
      break
    case 'roles':
      router.push('/system/roles')
      break
    case 'password':
      passwordChangeRef.value?.open()
      break
    case 'logout':
      logout()
      break
  }
}
</script>

<template>
  <div class="app-layout">
    <!-- Header: blue gradient, full width -->
    <header class="app-header">
      <div class="header-left">
        <!-- Logo + App name -->
        <el-icon :size="22" class="header-logo" @click="goHome">
          <component :is="iconMap[activeApp?.icon || 'Monitor'] || Monitor" />
        </el-icon>
        <span class="header-title">{{ activeApp?.name || 'ATE Studio' }}</span>
      </div>

      <!-- Horizontal menu in center -->
      <nav class="header-menu">
        <el-menu
          :default-active="activeMenu"
          mode="horizontal"
          class="top-menu"
          @select="handleMenuSelect"
          :ellipsis="false"
        >
          <el-menu-item
            v-for="menu in flatMenus"
            :key="menu.route_path"
            :index="menu.route_path"
          >
            <el-icon><component :is="menuIconMap[menu.icon || 'List'] || Monitor" /></el-icon>
            <span>{{ menu.name }}</span>
          </el-menu-item>
        </el-menu>
      </nav>

      <div class="header-right">
        <el-button text class="home-btn" @click="goHome">
          <el-icon><ArrowLeft /></el-icon>
          <span>{{ t('common.home') }}</span>
        </el-button>

        <el-dropdown class="user-dropdown" @command="handleCommand">
          <div class="user-trigger">
            <el-icon><User /></el-icon>
            <span class="user-name">{{ user?.username || '' }}</span>
            <el-icon><ArrowDown /></el-icon>
          </div>
          <template #dropdown>
            <el-dropdown-menu>
              <el-dropdown-item command="settings">
                {{ t('menu.settings') }}
              </el-dropdown-item>
              <el-dropdown-item v-if="isAdmin" command="users">
                {{ t('menu.userManagement') }}
              </el-dropdown-item>
              <el-dropdown-item v-if="isAdmin" command="roles">
                {{ t('rbac.title') }}
              </el-dropdown-item>
              <el-dropdown-item command="password" divided>
                {{ t('auth.changePassword') }}
              </el-dropdown-item>
              <el-dropdown-item command="logout" divided>
                {{ t('auth.logout') }}
              </el-dropdown-item>
            </el-dropdown-menu>
          </template>
        </el-dropdown>
      </div>
    </header>

    <!-- Content: full width below header -->
    <main class="app-content" v-loading="loading">
      <RouterView />
    </main>

    <!-- Password Change Dialog -->
    <PasswordChange ref="passwordChangeRef" />
  </div>
</template>

<style scoped>
.app-layout {
  display: flex;
  flex-direction: column;
  height: 100vh;
  overflow: hidden;
}

.app-header {
  background: linear-gradient(135deg, #409eff 0%, #337ecc 100%);
  height: 56px;
  display: flex;
  align-items: center;
  padding: 0 24px;
  box-shadow: 0 2px 8px rgba(64, 158, 255, 0.15);
  flex-shrink: 0;
}

.header-left {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-shrink: 0;
}

.header-logo {
  color: #fff;
  cursor: pointer;
}

.header-title {
  color: #fff;
  font-size: 16px;
  font-weight: 600;
  white-space: nowrap;
}

.header-menu {
  flex: 1;
  display: flex;
  justify-content: center;
  min-width: 0;
}

.top-menu {
  background: transparent !important;
  border-bottom: none !important;
  height: 56px;
}

.top-menu:deep(> .el-sub-menu__title),
.top-menu :deep(.el-menu-item) {
  height: 56px;
  line-height: 56px;
  background-color: transparent !important;
  color: rgba(255, 255, 255, 0.85) !important;
  border-bottom: 2px solid transparent;
}

.top-menu :deep(.el-menu-item:hover) {
  background-color: rgba(255, 255, 255, 0.15) !important;
  color: #fff !important;
}

.top-menu :deep(.el-menu-item.is-active) {
  color: #fff !important;
  border-bottom-color: #fff !important;
  background-color: rgba(255, 255, 255, 0.1) !important;
}

.header-right {
  flex-shrink: 0;
  display: flex;
  align-items: center;
  gap: 12px;
}

.user-dropdown {
  cursor: pointer;
}

.user-trigger {
  display: flex;
  align-items: center;
  gap: 6px;
  color: rgba(255, 255, 255, 0.85);
  cursor: pointer;
  padding: 4px 8px;
  border-radius: var(--radius-md);
  transition: background-color var(--transition-fast);
}

.user-trigger:hover {
  background-color: rgba(255, 255, 255, 0.15);
  color: #fff;
}

.user-name {
  font-size: 14px;
  white-space: nowrap;
}

.home-btn {
  color: rgba(255, 255, 255, 0.85);
}

.app-content {
  flex: 1;
  overflow: auto;
  background-color: var(--color-bg-primary);
  padding: 0;
}
</style>
