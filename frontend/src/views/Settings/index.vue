<script setup lang="ts">
import { ref } from 'vue'
import { ElCard, ElForm, ElFormItem, ElSwitch, ElSelect, ElOption, ElButton } from 'element-plus'

const darkMode = ref(false)
const language = ref('en')
</script>

<template>
  <div class="settings-page">
    <header class="page-header">
      <h1>Settings</h1>
      <p class="subtitle">Configure your ATE Studio preferences</p>
    </header>

    <ElCard class="settings-card">
      <ElForm label-position="top">
        <ElFormItem label="Dark Mode">
          <ElSwitch v-model="darkMode" active-text="On" inactive-text="Off" />
        </ElFormItem>

        <ElFormItem label="Language">
          <ElSelect v-model="language" style="width: 200px">
            <ElOption label="English" value="en" />
            <ElOption label="Chinese (Simplified)" value="zh-CN" />
          </ElSelect>
        </ElFormItem>
      </ElForm>
    </ElCard>

    <div class="actions">
      <ElButton type="primary">Save Changes</ElButton>
      <ElButton>Reset to Defaults</ElButton>
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
}

.settings-card {
  margin-bottom: var(--spacing-lg);
}

.actions {
  display: flex;
  gap: var(--spacing-sm);
}
</style>