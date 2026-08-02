<script setup lang="ts">
import { onMounted, computed } from 'vue'
import { useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { useApps } from '@/composables/useApps'
import { useAuth } from '@/composables/useAuth'
import {
  Monitor,
  Connection,
  DataLine,
  Setting,
  ArrowRight,
  Loading,
} from '@element-plus/icons-vue'

const router = useRouter()
const { t } = useI18n()
const { apps, loading, loadApps, ensureSeed } = useApps()
const { user, isAuthenticated, init } = useAuth()

// Icon mapping — maps backend icon names to Element Plus icon components
const iconMap: Record<string, typeof Monitor> = {
  Monitor,
  Connection,
  DataLine,
  Setting,
}

onMounted(async () => {
  await init()
  if (!isAuthenticated.value) {
    router.push('/login')
    return
  }
  await ensureSeed()
})

const sortedApps = computed(() =>
  [...apps.value].sort((a, b) => a.sort_order - b.sort_order)
)

function enterApp(appId: string, firstRoutePath?: string) {
  if (firstRoutePath) {
    router.push(firstRoutePath)
  } else {
    // Navigate to the app's default route — the layout will load menus
    router.push(`/app/${appId}`)
  }
}

// Get first menu route for each app from cache or default
function getAppRoute(app: { code: string }): string {
  const defaultRoutes: Record<string, string> = {
    'node-mgmt': '/node/stations',
    'flow-mgmt': '/flow/sequences',
    'exec-monitor': '/monitor/dashboard',
    'system': '/system/settings',
  }
  return defaultRoutes[app.code] || '/'
}
</script>

<template>
  <div class="portal-container">
    <!-- Header -->
    <header class="portal-header">
      <div class="portal-header-content">
        <div class="portal-logo">
          <h1 class="portal-title">ATE Studio</h1>
          <span class="portal-subtitle">{{ t('portal.selectApp') }}</span>
        </div>
        <div v-if="user" class="portal-welcome">
          {{ t('auth.welcome') }}, {{ user.username }}
        </div>
      </div>
    </header>

    <!-- Content -->
    <main class="portal-main">
      <div v-loading="loading" class="portal-cards">
        <div
          v-for="app in sortedApps"
          :key="app.id"
          class="portal-card"
          @click="enterApp(app.id, getAppRoute(app))"
        >
          <div class="portal-card-icon">
            <el-icon :size="40">
              <component :is="iconMap[app.icon || 'Monitor'] || Monitor" />
            </el-icon>
          </div>
          <div class="portal-card-body">
            <h3 class="portal-card-title">{{ app.name }}</h3>
            <p class="portal-card-desc">{{ app.description || '' }}</p>
          </div>
          <div class="portal-card-arrow">
            <el-icon :size="20"><ArrowRight /></el-icon>
          </div>
        </div>
      </div>

      <!-- Loading skeleton -->
      <div v-if="loading && sortedApps.length === 0" class="portal-skeleton">
        <div v-for="i in 4" :key="i" class="portal-card skeleton-card">
          <el-skeleton :rows="2" animated />
        </div>
      </div>
    </main>

    <!-- Footer -->
    <footer class="portal-footer">
      <span>ATE Studio © 2026 — Powered by Aliyun DashScope</span>
    </footer>
  </div>
</template>

<style scoped>
.portal-container {
  min-height: 100vh;
  display: flex;
  flex-direction: column;
  background: var(--color-bg-secondary);
}

.portal-header {
  background: linear-gradient(135deg, #409eff 0%, #337ecc 100%);
  padding: 24px 48px;
  box-shadow: 0 2px 8px rgba(64, 158, 255, 0.2);
}

.portal-header-content {
  max-width: 1200px;
  margin: 0 auto;
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.portal-logo {
  display: flex;
  align-items: baseline;
  gap: 12px;
}

.portal-title {
  font-size: 28px;
  font-weight: 700;
  color: #ffffff;
  margin: 0;
  letter-spacing: 1px;
}

.portal-subtitle {
  font-size: 14px;
  color: rgba(255, 255, 255, 0.85);
}

.portal-welcome {
  font-size: 14px;
  color: rgba(255, 255, 255, 0.9);
  font-weight: 500;
}

.portal-main {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 48px 24px;
}

.portal-cards {
  max-width: 1200px;
  width: 100%;
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
  gap: 24px;
}

.portal-card {
  background: var(--color-bg-elevated);
  border-radius: 16px;
  padding: 32px 24px;
  display: flex;
  align-items: center;
  gap: 20px;
  cursor: pointer;
  transition: all 0.3s ease;
  box-shadow: var(--shadow-md);
  border: 1px solid var(--color-border-default);
  position: relative;
  overflow: hidden;
}

.portal-card::before {
  content: '';
  position: absolute;
  top: 0;
  left: 0;
  right: 0;
  height: 4px;
  background: linear-gradient(90deg, #409eff, #66b1ff);
  opacity: 0;
  transition: opacity 0.3s ease;
}

.portal-card:hover {
  transform: translateY(-4px);
  box-shadow: var(--shadow-lg);
  border-color: var(--color-primary);
}

.portal-card:hover::before {
  opacity: 1;
}

.portal-card-icon {
  flex-shrink: 0;
  width: 64px;
  height: 64px;
  border-radius: 12px;
  background: var(--color-bg-tertiary);
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--color-primary);
}

.portal-card-body {
  flex: 1;
  min-width: 0;
}

.portal-card-title {
  font-size: 18px;
  font-weight: 600;
  color: var(--color-text-primary);
  margin: 0 0 6px 0;
}

.portal-card-desc {
  font-size: 13px;
  color: var(--color-text-secondary);
  margin: 0;
  line-height: 1.5;
  overflow: hidden;
  text-overflow: ellipsis;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
}

.portal-card-arrow {
  flex-shrink: 0;
  color: var(--color-text-tertiary);
  transition: color 0.3s ease, transform 0.3s ease;
}

.portal-card:hover .portal-card-arrow {
  color: var(--color-primary);
  transform: translateX(4px);
}

.portal-skeleton {
  max-width: 1200px;
  width: 100%;
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
  gap: 24px;
}

.skeleton-card {
  padding: 32px 24px;
  border-radius: 16px;
  border: 1px solid var(--color-border-default);
}

.portal-footer {
  text-align: center;
  padding: 16px;
  color: var(--color-text-tertiary);
  font-size: 12px;
}
</style>
