<script setup lang="ts">
import { onMounted, watch, computed } from 'vue'
import { useRouter, useRoute, RouterView } from 'vue-router'
import { useApps } from '@/composables/useApps'
import {
  Monitor,
  Connection,
  DataLine,
  Setting,
  ArrowLeft,
} from '@element-plus/icons-vue'

const router = useRouter()
const route = useRoute()
const { apps, currentAppMenus, loading, loadApps, loadAppMenus } = useApps()

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
          <span>返回首页</span>
        </el-button>
      </div>
    </header>

    <!-- Content: full width below header -->
    <main class="app-content" v-loading="loading">
      <RouterView />
    </main>
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
  --el-menu-bg-color: transparent;
  --el-menu-text-color: rgba(255, 255, 255, 0.85);
  --el-menu-active-color: #fff;
  --el-menu-hover-text-color: #fff;
  --el-menu-hover-bg-color: rgba(255, 255, 255, 0.15);
  border-bottom: none;
  height: 56px;
}

.header-right {
  flex-shrink: 0;
}

.home-btn {
  color: rgba(255, 255, 255, 0.85);
}

.app-content {
  flex: 1;
  overflow: auto;
  background-color: #f5f7fa;
  padding: 0;
}

.top-menu .el-menu-item {
  height: 56px;
  line-height: 56px;
  color: rgba(255, 255, 255, 0.85);
  border-bottom: 2px solid transparent;
}

.top-menu .el-menu-item:hover {
  background-color: rgba(255, 255, 255, 0.15) !important;
  color: #fff !important;
}

.top-menu .el-menu-item.is-active {
  color: #fff !important;
  border-bottom-color: #fff !important;
  background-color: rgba(255, 255, 255, 0.1) !important;
}
</style>
