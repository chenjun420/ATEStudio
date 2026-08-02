<script setup lang="ts">
import { ref, reactive } from 'vue'
import { useI18n } from 'vue-i18n'
import {
  ElDialog,
  ElForm,
  ElFormItem,
  ElInput,
  ElButton,
  ElMessage,
} from 'element-plus'
import { changePassword } from '@/api/users'
import { useAuth } from '@/composables/useAuth'

const { t } = useI18n()
const { logout } = useAuth()

const visible = ref(false)
const loading = ref(false)
const form = reactive({
  old_password: '',
  new_password: '',
  confirm_password: '',
})

const emit = defineEmits<{
  success: []
}>()

function open(): void {
  form.old_password = ''
  form.new_password = ''
  form.confirm_password = ''
  visible.value = true
}

function close(): void {
  visible.value = false
}

async function handleSubmit(): Promise<void> {
  if (form.new_password.length < 8) {
    ElMessage.error(t('auth.passwordTooShort'))
    return
  }
  if (form.new_password !== form.confirm_password) {
    ElMessage.error(t('auth.passwordMismatch'))
    return
  }

  loading.value = true
  try {
    await changePassword(form.old_password, form.new_password)
    ElMessage.success(t('auth.passwordChanged'))
    emit('success')
    visible.value = false
    // Logout after password change since tokens may be invalid
    logout()
  } catch {
    ElMessage.error(t('common.error'))
  } finally {
    loading.value = false
  }
}

defineExpose({ open, close })
</script>

<template>
  <el-dialog
    v-model="visible"
    :title="t('auth.changePassword')"
    width="440px"
  >
    <el-form label-position="top">
      <el-form-item :label="t('auth.oldPassword')">
        <el-input
          v-model="form.old_password"
          type="password"
          show-password
        />
      </el-form-item>
      <el-form-item :label="t('auth.newPassword')">
        <el-input
          v-model="form.new_password"
          type="password"
          show-password
        />
      </el-form-item>
      <el-form-item :label="t('auth.confirmNewPassword')">
        <el-input
          v-model="form.confirm_password"
          type="password"
          show-password
          @keyup.enter="handleSubmit"
        />
      </el-form-item>
    </el-form>

    <template #footer>
      <el-button @click="close">{{ t('common.cancel') }}</el-button>
      <el-button type="primary" :loading="loading" @click="handleSubmit">
        {{ t('common.confirm') }}
      </el-button>
    </template>
  </el-dialog>
</template>
