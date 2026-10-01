<!--
  应用外壳。

  顶栏:模式切换(2 项) · 上下文选择器 · 离线状态 · 语言 · 用户下拉
  侧栏:当前模式的分组菜单(app → group → page,来自数据库)

  为什么顶栏只有两项
  ------------------
  NI TestStand 的架构卡原文是「depending on **mode**, edit, execute, and debug
  test sequences」—— 工程态与操作态以**模式**区分, 而不是以功能模块区分。所以顶层
  就是 产测开发 / 运行监控 两项, 其余全部收进侧栏分组。

  菜单来自数据库, 不在这里硬编码
  -------------------------------
  ``GET /api/v1/apps`` 给 app 列表, ``GET /api/v1/apps/{id}`` 给分组树。之前顶栏
  那排菜单是「数据库菜单 + 前端 staticMenus」拼出来的, 两处真相, 且不一致时没有人
  会发现 —— 那正是 ``test_menu_routes_resolve.py`` 存在的原因。

  「系统」不是第三个模式
  --------------------
  顶栏只有两项, 所以系统管理从账号下拉进入。但用户管理 / 角色与权限**仍然是菜单行**,
  而不是下拉项: 下拉里的 ``v-if="isAdmin"`` 是建议性显示, 服务端从不校验; 菜单项的
  ``required_permissions`` 才由服务端求值。见 ``tests/cloud/test_admin_pages_in_menu.py``。

  没有「＋导入」按钮
  -----------------
  规格 §4.1 的顶栏画了一个「＋导入」, 但导入向导是 P5。在它存在之前放一个按钮,
  就是宣布一个没有的能力 —— 要么点了没反应, 要么点到一个空页面。所以它在 P5 之前
  不出现。
-->
<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRouter, useRoute, RouterView } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { useApps } from '@/composables/useApps'
import { useAuth } from '@/composables/useAuth'
import { appLabel, menuLabel } from '@/composables/menuLabels'
import { useLocale } from '@/composables/useLocale'
import { useContext } from '@/composables/useContext'
import PasswordChange from '@/views/PasswordChange.vue'
import OfflineStatusIndicator from '@/components/OfflineStatusIndicator.vue'
import ContextSelector from '@/components/ContextSelector.vue'
import {
  Monitor,
  DataLine,
  User,
  ArrowDown,
} from '@element-plus/icons-vue'

const router = useRouter()
const route = useRoute()
const { t } = useI18n()
const { apps, currentAppMenus, loading, loadApps, loadAppMenus } = useApps()
const { user, logout, isAdmin } = useAuth()
const { locale, locales, setLocale } = useLocale()
const { load: loadContext } = useContext()

const passwordChangeRef = ref<InstanceType<typeof PasswordChange> | null>(null)
const sidebarCollapsed = ref(false)

/**
 * The two modes, in the order they appear in the top bar.
 *
 * Declared here rather than derived from the app list, because the app list also
 * contains `system`, which is deliberately not a tab. Deriving the tabs from
 * "all apps" is what put a third tab there before.
 */
const MODES = [
  { code: 'test-dev', labelKey: 'menu.testDevelopment', icon: Monitor },
  { code: 'runtime', labelKey: 'menu.runtimeMonitoring', icon: DataLine },
] as const

type ModeCode = (typeof MODES)[number]['code']

const activeMode = computed<ModeCode | null>(() => {
  const mode = route.meta.mode as string | undefined
  if (mode === 'test-dev' || mode === 'runtime') return mode
  if (mode === 'system') return null
  // Operator view and any route without a mode: fall back to the path prefix so
  // the top bar still highlights something rather than going blank.
  return route.path.startsWith('/ops') ? 'runtime' : 'test-dev'
})

const activeApp = computed(() => {
  const code = route.meta.mode as string | undefined
  return apps.value.find((a) => a.code === code) ?? null
})

watch(
  activeMode,
  async (mode) => {
    if (!mode) return
    const app = apps.value.find((a) => a.code === mode)
    if (app) {
      await loadAppMenus(app.id)
      // Load the mode's context options alongside its menu, so the selector is
      // populated by the time the user reaches for it.
      void loadContext(mode)
    }
  },
  { immediate: true },
)

onMounted(async () => {
  if (apps.value.length === 0) {
    await loadApps()
  }
  if (activeMode.value) {
    const app = apps.value.find((a) => a.code === activeMode.value)
    if (app) await loadAppMenus(app.id)
  }
})

/** Groups for the active mode. A group has no route; a page does. */
interface MenuNode {
  id: string
  code: string
  name: string
  route_path: string | null
  icon?: string | null
  children: MenuNode[]
}

const groups = computed<MenuNode[]>(() => {
  const raw = currentAppMenus.value?.menus as unknown as MenuNode[] | undefined
  if (!raw) return []
  // The API already prunes empty groups; this only drops anything with neither
  // a route nor children, which would render as a header above nothing.
  return raw.filter((n) => n.route_path || n.children.length > 0)
})

/**
 * Menu trees by app id, so switching modes can land on a real page even before
 * that mode's menu has been fetched.
 */
const menuCacheById = ref<Map<string, MenuNode[]>>(new Map())

watch(
  currentAppMenus,
  (menus) => {
    if (!menus) return
    const next = new Map(menuCacheById.value)
    next.set(menus.id, (menus.menus as unknown as MenuNode[]) ?? [])
    menuCacheById.value = next
  },
  { deep: false },
)

function groupName(node: MenuNode): string {
  // Groups carry a code the page table does not, so they fall through to the
  // server-provided name — which is Chinese, like every other seed name.
  const table: Record<string, string> = {
    requirements: 'menu.groupRequirements',
    process: 'menu.groupProcess',
    'station-binding': 'menu.groupStationBinding',
    line: 'menu.groupLine',
    execution: 'menu.groupExecution',
    debug: 'menu.groupDebug',
    'station-ops': 'menu.groupStationOps',
    fault: 'menu.groupFault',
  }
  const key = table[node.code]
  if (!key) return node.name
  const translated = t(key)
  return translated && translated !== key ? translated : node.name
}

const activeMenu = computed(() => route.path)

function goPage(routePath: string): void {
  if (routePath) router.push(routePath)
}

function switchMode(mode: ModeCode): void {
  const target = MODES.find((m) => m.code === mode)
  if (!target) return
  // Land on the mode's first real page rather than a route that only exists
  // after the menu arrives — an empty content area reads as a broken load.
  const app = apps.value.find((a) => a.code === mode)
  const first = app ? firstPageOf(app.id) : null
  router.push(first ?? `/${mode === 'test-dev' ? 'dev' : 'ops'}`)
}

/**
 * First page of an app, read from the cached menu tree.
 *
 * Returns null before the menu has loaded, which is why the caller falls back
 * to the path prefix — a redirect to a route that does not exist yet is worse
 * than a redirect to the section root.
 */
function firstPageOf(appId: string): string | null {
  const tree = menuCacheById.value.get(appId)
  if (!tree) return null
  for (const group of tree) {
    if (group.route_path) return group.route_path
    const page = group.children.find((c) => c.route_path)
    if (page?.route_path) return page.route_path
  }
  return null
}

function goHome(): void {
  router.push('/')
}

function handleCommand(command: string): void {
  switch (command) {
    case 'settings':
      router.push('/system/settings')
      break
    // 'users' and 'roles' used to be dropdown commands gated on a client-side
    // isAdmin. They are menu rows now, server-checked; the commands below only
    // navigate, and the guard is the route's own requiresAdmin plus the
    // endpoint's scope check.
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
    <header class="app-header">
      <div class="header-left">
        <el-icon :size="22" class="header-logo" @click="goHome">
          <Monitor />
        </el-icon>
        <span class="header-title" @click="goHome">ATE Studio</span>
      </div>

      <!-- Mode switch: two items, per the spec's top bar. -->
      <nav class="mode-switch">
        <button
          v-for="m in MODES"
          :key="m.code"
          type="button"
          class="mode-btn"
          :class="{ 'is-active': activeMode === m.code }"
          :data-testid="`mode-${m.code}`"
          @click="switchMode(m.code)"
        >
          <el-icon><component :is="m.icon" /></el-icon>
          <span>{{ t(m.labelKey) }}</span>
        </button>
      </nav>

      <!-- Context selector: 型号 in 产测开发, 厂区 → 工位 in 运行监控. -->
      <div class="header-context">
        <ContextSelector v-if="activeMode" :mode="activeMode" />
      </div>

      <div class="header-right">
        <OfflineStatusIndicator />

        <!--
          Language switch.

          A radio group rather than a toggle: with two locales a toggle has to
          invent a label for the state you are switching *to*, which is wrong
          half the time. The group shows the current choice directly.

          The labels are the language's own name ("中文" / "English") rather than
          a translation of it — a user looking for English is looking for the
          word "English", and "英文" only helps if they read Chinese, which is
          the case this control exists to escape.
        -->
        <el-radio-group
          class="locale-switch"
          :model-value="locale"
          size="small"
          @update:model-value="setLocale($event as 'zh-CN' | 'en')"
        >
          <el-radio-button v-for="loc in locales" :key="loc" :value="loc">
            {{ loc === 'zh-CN' ? '中文' : 'English' }}
          </el-radio-button>
        </el-radio-group>

        <el-dropdown class="user-dropdown" @command="handleCommand">
          <div class="user-trigger">
            <el-icon><User /></el-icon>
            <span class="user-name">{{ user?.username || '' }}</span>
            <el-icon><ArrowDown /></el-icon>
          </div>
          <template #dropdown>
            <el-dropdown-menu>
              <!-- 个人 -->
              <el-dropdown-item command="password">
                {{ t('auth.changePassword') }}
              </el-dropdown-item>
              <!-- 系统: navigates into menu-backed pages. isAdmin gates the
                   two admin items for convenience; the server enforces them. -->
              <el-dropdown-item command="settings" divided>
                {{ t('menu.settings') }}
              </el-dropdown-item>
              <el-dropdown-item v-if="isAdmin" command="users">
                {{ t('menu.userManagement') }}
              </el-dropdown-item>
              <el-dropdown-item v-if="isAdmin" command="roles">
                {{ t('menu.roleManagement') }}
              </el-dropdown-item>
              <el-dropdown-item command="logout" divided>
                {{ t('auth.logout') }}
              </el-dropdown-item>
            </el-dropdown-menu>
          </template>
        </el-dropdown>
      </div>
    </header>

    <div class="app-body">
      <!-- Sidebar: groups from the server. Depth is group → page, two levels. -->
      <aside class="app-sidebar" :class="{ 'is-collapsed': sidebarCollapsed }">
        <div v-if="activeApp" class="sidebar-title">
          {{ appLabel(activeApp, t) }}
        </div>
        <el-scrollbar>
          <el-menu
            :default-active="activeMenu"
            :collapse="sidebarCollapsed"
            :collapse-transition="false"
            class="side-menu"
            @select="(index: string) => goPage(index)"
          >
            <template v-for="node in groups" :key="node.id">
              <!-- A page with no group. -->
              <el-menu-item
                v-if="node.route_path"
                :index="node.route_path"
                :data-testid="`menu-${node.code}`"
              >
                <span>{{ menuLabel(node, t) }}</span>
              </el-menu-item>
              <!-- A group: expands, does not navigate. -->
              <el-sub-menu v-else :index="`group-${node.code}`">
                <template #title>
                  <span :data-testid="`group-${node.code}`">{{ groupName(node) }}</span>
                </template>
                <el-menu-item
                  v-for="child in node.children.filter((c) => c.route_path)"
                  :key="child.id"
                  :index="child.route_path!"
                  :data-testid="`menu-${child.code}`"
                >
                  <span>{{ menuLabel(child, t) }}</span>
                </el-menu-item>
              </el-sub-menu>
            </template>
          </el-menu>
        </el-scrollbar>
        <button
          type="button"
          class="collapse-toggle"
          @click="sidebarCollapsed = !sidebarCollapsed"
        >
          {{ sidebarCollapsed ? '»' : '«' }}
        </button>
      </aside>

      <main class="app-content" v-loading="loading">
        <RouterView />
      </main>
    </div>

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
  gap: 16px;
  padding: 0 20px;
  box-shadow: 0 2px 8px rgba(64, 158, 255, 0.15);
  flex-shrink: 0;
}

.header-left {
  display: flex;
  align-items: center;
  gap: 8px;
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
  cursor: pointer;
}

/* ── Mode switch ─────────────────────────────────────────────────────── */

.mode-switch {
  display: flex;
  gap: 4px;
  flex-shrink: 0;
}

.mode-btn {
  display: flex;
  align-items: center;
  gap: 6px;
  height: 34px;
  padding: 0 14px;
  border: none;
  border-radius: 4px;
  background: transparent;
  color: rgba(255, 255, 255, 0.85);
  font-size: 14px;
  cursor: pointer;
  transition: background-color 0.15s;
}

.mode-btn:hover {
  background-color: rgba(255, 255, 255, 0.15);
  color: #fff;
}

.mode-btn.is-active {
  background-color: rgba(255, 255, 255, 0.22);
  color: #fff;
  font-weight: 600;
}

.header-context {
  flex: 1;
  min-width: 0;
  display: flex;
  align-items: center;
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

/* ── Body / sidebar ──────────────────────────────────────────────────── */

.app-body {
  flex: 1;
  display: flex;
  overflow: hidden;
  min-height: 0;
}

.app-sidebar {
  width: 210px;
  flex-shrink: 0;
  border-right: 1px solid var(--el-border-color-light);
  background: var(--color-bg-primary);
  display: flex;
  flex-direction: column;
  position: relative;
  transition: width 0.2s;
}

.app-sidebar.is-collapsed {
  width: 64px;
}

.sidebar-title {
  padding: 12px 16px 8px;
  font-size: 13px;
  font-weight: 600;
  color: var(--el-text-color-secondary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.side-menu {
  border-right: none;
}

.collapse-toggle {
  position: absolute;
  right: -1px;
  bottom: 8px;
  width: 20px;
  height: 20px;
  border: 1px solid var(--el-border-color);
  border-radius: 4px;
  background: var(--color-bg-primary);
  color: var(--el-text-color-secondary);
  cursor: pointer;
  font-size: 11px;
  line-height: 1;
}

.app-content {
  flex: 1;
  overflow: auto;
  min-width: 0;
}
</style>
