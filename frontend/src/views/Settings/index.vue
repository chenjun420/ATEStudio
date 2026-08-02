<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useI18n } from 'vue-i18n'
import { ElMessage } from 'element-plus'
import { useTheme, type ThemeMode } from '@/composables/useTheme'
import { useAuth } from '@/composables/useAuth'
import i18n from '@/i18n'

const { t } = useI18n()
const { mode, setMode } = useTheme()
const { isAuthenticated, savePreferences, loadPreferences, preferences } = useAuth()

const themeMode = ref<ThemeMode>(mode.value)
const language = ref<string>('en')
const saving = ref(false)

function applyTheme(value: ThemeMode): void {
  themeMode.value = value
  setMode(value)
}

function applyLanguage(value: string): void {
  language.value = value
  i18n.global.locale.value = value
}

async function handleSave(): Promise<void> {
  saving.value = true
  try {
    if (isAuthenticated.value) {
      await savePreferences(themeMode.value, language.value)
    } else {
      // Dev mode — persist locally only
      localStorage.setItem('ate-theme-mode', themeMode.value)
      localStorage.setItem('ate-language', language.value)
    }
    ElMessage.success(t('settings.settingsSaved'))
  } catch {
    ElMessage.error(t('settings.settingsSaveFailed'))
  } finally {
    saving.value = false
  }
}

function handleReset(): void {
  applyTheme('auto')
  applyLanguage('en')
}

onMounted(async () => {
  if (isAuthenticated.value) {
    await loadPreferences()
    if (preferences.value) {
      if (preferences.value.theme_mode) {
        applyTheme(preferences.value.theme_mode as ThemeMode)
      }
      if (preferences.value.language) {
        applyLanguage(preferences.value.language)
      }
    }
  } else {
    // Dev mode — load from localStorage
    const savedLang = localStorage.getItem('ate-language')
    if (savedLang) {
      applyLanguage(savedLang)
    }
  }
})
</script>

<template>
  <div class="settings-page">
    <header class="page-header">
      <h1>{{ t('menu.settings') }}</h1>
      <p class="subtitle">{{ t('settings.preferences') }}</p>
    </header>

    <el-card class="settings-card">
      <template #header>
        <span class="card-title">{{ t('settings.appearance') }}</span>
      </template>

      <el-form label-position="top">
        <el-form-item :label="t('settings.themeMode')">
          <el-radio-group :model-value="themeMode" @update:model-value="applyTheme">
            <el-radio-button value="light">{{ t('settings.themeLight') }}</el-radio-button>
            <el-radio-button value="dark">{{ t('settings.themeDark') }}</el-radio-button>
            <el-radio-button value="auto">{{ t('settings.themeAuto') }}</el-radio-button>
          </el-radio-group>
        </el-form-item>

        <el-form-item :label="t('settings.language')">
          <el-select :model-value="language" @update:model-value="applyLanguage" style="width: 240px">
            <el-option label="English" value="en" />
            <el-option label="中文" value="zh-CN" />
          </el-select>
        </el-form-item>
      </el-form>
    </el-card>

    <div class="actions">
      <el-button type="primary" :loading="saving" @click="handleSave">
        {{ t('settings.saveSettings') }}
      </el-button>
      <el-button @click="handleReset">{{ t('settings.resetDefaults') }}</el-button>
    </div>
  </div>
</template>

<style scoped>
.settings-page {
  max-width: 600px;
  margin: 0 auto;
  padding: var(--spacing-lg);
}

.page-header {
  margin-bottom: var(--spacing-lg);
}

.page-header h1 {
  font-size: 1.5rem;
  font-weight: 600;
  color: var(--color-text-primary);
  margin-bottom: var(--spacing-xs);
}

.subtitle {
  color: var(--color-text-secondary);
  font-size: 0.875rem;
  margin: 0;
}

.settings-card {
  margin-bottom: var(--spacing-lg);
}

.card-title {
  font-weight: 600;
  color: var(--color-text-primary);
}

.actions {
  display: flex;
  gap: var(--spacing-sm);
}
</style>
