<script setup lang="ts">
import { onMounted, watch, computed, ref } from 'vue'
import { useRouter, useRoute, RouterView } from 'vue-router'
import { useApps } from '@/composables/useApps'
import {
  Monitor,
  Connection,
  DataLine,
  Setting,
  ArrowLeft,
  Fold,
  Expand,
} from '@element-plus/icons-vue'

const router = useRouter()
const route = useRoute()
const { apps, currentAppMenus, loading, loadApps, loadAppMenus } = useApps()

const isCollapse = ref(false)

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

function toggleSidebar() {
  isCollapse.value = !isCollapse.value
}
</script>

<template>
  <div class="app-layout">
    <!-- Sidebar -->
    <aside class="app-sidebar" :class="{ collapsed: isCollapse }">
      <div class="sidebar-header">
        <el-icon :size="24" class="sidebar-logo" @click="goHome">
          <component :is="iconMap[activeApp?.icon || 'Monitor'] || Monitor" />
        </el-icon>
        <span v-show="!isCollapse" class="sidebar-title">
          {{ activeApp?.name || 'ATE Studio' }}
        </span>
      </div>
      <el-menu
        :default-active="activeMenu"
        :collapse="isCollapse"
        :collapse-transition="false"
        class="sidebar-menu"
        @select="handleMenuSelect"
      >
        <el-menu-item
          v-for="menu in flatMenus"
          :key="menu.route_path"
          :index="menu.route_path"
        >
          <el-icon>
            <component :is="menuIconMap[menu.icon || 'List'] || Monitor" />
          </el-icon>
          <template #title>{{ menu.name }}</template>
        </el-menu-item>
      </el-menu>
    </aside>

    <!-- Main area -->
    <div class="app-main">
      <!-- Top header -->
      <header class="app-header">
        <div class="header-left">
          <el-button text @click="toggleSidebar" class="collapse-btn">
            <el-icon :size="18">
              <component :is="isCollapse ? Expand : Fold" />
            </el-icon>
          </el-button>
          <el-button text @click="goHome" class="home-btn">
            <el-icon :size="16"><ArrowLeft /></el-icon>
            <span>返回首页</span>
          </el-button>
        </div>
        <div class="header-right">
          <span class="header-app-name">{{ activeApp?.name || '' }}</span>
        </div>
      </header>

      <!-- Content area -->
      <main class="app-content" v-loading="loading">
        <RouterView />
      </main>
    </div>
  </div>
</template>

<style scoped>
.app-layout {
  display: flex;
  height: 100vh;
  overflow: hidden;
}

.app-sidebar {
  width: 220px;
  background-color: #ffffff;
  border-right: 1px solid #e4e7ed;
  display: flex;
  flex-direction: column;
  transition: width 0.3s ease;
  flex-shrink: 0;
}

.app-sidebar.collapsed {
  width: 64px;
}

.sidebar-header {
  height: 56px;
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 0 16px;
  border-bottom: 1px solid #e4e7ed;
  flex-shrink: 0;
}

.sidebar-logo {
  color: #409eff;
  cursor: pointer;
  flex-shrink: 0;
}

.sidebar-title {
  font-size: 15px;
  font-weight: 600;
  color: #303133;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.sidebar-menu {
  flex: 1;
  border-right: none;
  overflow-y: auto;
  overflow-x: hidden;
}

.sidebar-menu:not(.el-menu--collapse) {
  width: 220px;
}

.app-main {
  flex: 1;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  min-width: 0;
}

.app-header {
  height: 48px;
  background-color: #ffffff;
  border-bottom: 1px solid #e4e7ed;
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 16px;
  flex-shrink: 0;
}

.header-left {
  display: flex;
  align-items: center;
  gap: 8px;
}

.collapse-btn {
  padding: 4px 8px;
}

.home-btn {
  padding: 4px 8px;
  color: #606266;
}

.header-right {
  display: flex;
  align-items: center;
  gap: 12px;
}

.header-app-name {
  font-size: 14px;
  font-weight: 500;
  color: #909399;
}

.app-content {
  flex: 1;
  overflow: auto;
  background-color: #f5f7fa;
  padding: 0;
}
</style>
