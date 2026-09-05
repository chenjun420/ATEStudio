<script setup lang="ts">
import { ref, reactive, onMounted } from 'vue'
import { useI18n } from 'vue-i18n'
import {
  ElTable,
  ElTableColumn,
  ElButton,
  ElDialog,
  ElForm,
  ElFormItem,
  ElInput,
  ElSelect,
  ElOption,
  ElSwitch,
  ElTag,
  ElMessage,
  ElMessageBox,
  ElAlert,
} from 'element-plus'
import { useAuth } from '@/composables/useAuth'
import {
  listUsers,
  createUser,
  updateUser,
  deleteUser,
  deactivateAccount,
  type UserCreate,
  type UserUpdate,
} from '@/api/users'
import type { UserResponse } from '@/api/auth'

const { t } = useI18n()
const { isAdmin, hasScope, user: currentUser, logout } = useAuth()

const users = ref<UserResponse[]>([])
const loading = ref(false)

const dialogVisible = ref(false)
const dialogMode = ref<'create' | 'edit'>('create')
const editingUserId = ref<string | null>(null)
const dialogLoading = ref(false)

const form = reactive({
  username: '',
  password: '',
  confirmPassword: '',
  role: 'read',
  scopes: [] as string[],
  is_active: true,
})

const roleOptions = [
  { label: t('users.adminRole'), value: 'admin' },
  { label: t('users.writeRole'), value: 'write' },
  { label: t('users.readRole'), value: 'read' },
  { label: t('users.executeRole'), value: 'execute' },
]

const scopeOptions = [
  { label: t('users.readRole'), value: 'read' },
  { label: t('users.writeRole'), value: 'write' },
  { label: t('users.executeRole'), value: 'execute' },
]

async function fetchUsers(): Promise<void> {
  loading.value = true
  try {
    const resp = await listUsers()
    users.value = resp.items
  } catch {
    ElMessage.error(t('common.error'))
  } finally {
    loading.value = false
  }
}

function resetForm(): void {
  form.username = ''
  form.password = ''
  form.confirmPassword = ''
  form.role = 'read'
  form.scopes = []
  form.is_active = true
}

function openCreateDialog(): void {
  dialogMode.value = 'create'
  editingUserId.value = null
  resetForm()
  dialogVisible.value = true
}

function openEditDialog(row: UserResponse): void {
  dialogMode.value = 'edit'
  editingUserId.value = row.id
  form.username = row.username
  form.password = ''
  form.confirmPassword = ''
  form.role = row.role
  form.scopes = [...row.scopes]
  form.is_active = row.is_active
  dialogVisible.value = true
}

async function handleSubmit(): Promise<void> {
  if (!form.username) {
    ElMessage.error(t('users.enterUsername'))
    return
  }
  if (dialogMode.value === 'create' && !form.password) {
    ElMessage.error(t('users.enterPassword'))
    return
  }
  if (form.password && form.password !== form.confirmPassword) {
    ElMessage.error(t('users.passwordMismatch'))
    return
  }

  dialogLoading.value = true
  try {
    if (dialogMode.value === 'create') {
      const data: UserCreate = {
        username: form.username,
        password: form.password,
        role: form.role,
        scopes: form.scopes.length > 0 ? form.scopes : undefined,
        is_active: form.is_active,
      }
      await createUser(data)
      ElMessage.success(t('users.createSuccess'))
    } else if (editingUserId.value) {
      const data: UserUpdate = {
        role: form.role,
        scopes: form.scopes,
        is_active: form.is_active,
      }
      if (form.password) {
        data.password = form.password
      }
      await updateUser(editingUserId.value, data)
      ElMessage.success(t('users.updateSuccess'))
    }
    dialogVisible.value = false
    await fetchUsers()
  } catch {
    ElMessage.error(t('common.error'))
  } finally {
    dialogLoading.value = false
  }
}

async function handleDelete(row: UserResponse): Promise<void> {
  try {
    await ElMessageBox.confirm(t('users.confirmDelete'), t('common.confirm'), {
      type: 'warning',
    })
    await deleteUser(row.id)
    ElMessage.success(t('users.deleteSuccess'))
    await fetchUsers()
  } catch {
    // User cancelled or delete failed
  }
}

function getRoleTagType(role: string): 'danger' | 'warning' | 'info' | 'success' {
  switch (role) {
    case 'admin':
      return 'danger'
    case 'write':
      return 'warning'
    case 'execute':
      return 'success'
    default:
      return 'info'
  }
}

async function handleDeactivate(): Promise<void> {
  try {
    await ElMessageBox.confirm(t('auth.deactivateConfirm'), t('auth.deactivate'), {
      type: 'error',
      confirmButtonText: t('common.confirm'),
      cancelButtonText: t('common.cancel'),
    })
    await deactivateAccount()
    ElMessage.success(t('auth.deactivated'))
    logout()
  } catch {
    // User cancelled or deactivate failed
  }
}

onMounted(() => {
  if (isAdmin.value) {
    fetchUsers()
  }
})
</script>

<template>
  <div class="user-management">
    <header class="page-header">
      <h1>{{ t('users.userList') }}</h1>
      <el-button v-if="hasScope('user:write')" type="primary" @click="openCreateDialog">
        {{ t('users.createUser') }}
      </el-button>
    </header>

    <el-alert
      v-if="!isAdmin"
      :title="t('users.adminOnly')"
      type="warning"
      show-icon
      :closable="false"
      class="admin-alert"
    />

    <el-table
      v-else
      :data="users"
      v-loading="loading"
      class="user-table"
    >
      <el-table-column prop="username" :label="t('users.userName')" />
      <el-table-column :label="t('users.role')">
        <template #default="{ row }">
          <el-tag :type="getRoleTagType(row.role)">
            {{ row.role }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column :label="t('users.scopes')">
        <template #default="{ row }">
          <el-tag v-for="scope in row.scopes" :key="scope" size="small" class="scope-tag">
            {{ scope }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column :label="t('users.isActive')">
        <template #default="{ row }">
          <el-tag :type="row.is_active ? 'success' : 'info'">
            {{ row.is_active ? t('common.success') : t('common.close') }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column :label="t('common.actions')" width="160">
        <template #default="{ row }">
          <el-button v-if="hasScope('user:write')" size="small" @click="openEditDialog(row as UserResponse)">
            {{ t('common.edit') }}
          </el-button>
          <el-button
            v-if="hasScope('user:write')"
            size="small"
            type="danger"
            :disabled="row.id === currentUser?.id"
                @click="handleDelete(row as UserResponse)"
          >
            {{ t('common.delete') }}
          </el-button>
        </template>
      </el-table-column>
    </el-table>

    <el-dialog
      v-model="dialogVisible"
      :title="dialogMode === 'create' ? t('users.createUser') : t('users.editUser')"
      width="480px"
    >
      <el-form label-position="top">
        <el-form-item :label="t('users.userName')">
          <el-input v-model="form.username" :disabled="dialogMode === 'edit'" />
        </el-form-item>
        <el-form-item :label="t('users.password')">
          <el-input v-model="form.password" type="password" show-password />
        </el-form-item>
        <el-form-item :label="t('users.confirmPassword')">
          <el-input v-model="form.confirmPassword" type="password" show-password />
        </el-form-item>
        <el-form-item :label="t('users.role')">
          <el-select v-model="form.role" style="width: 100%">
            <el-option
              v-for="opt in roleOptions"
              :key="opt.value"
              :label="opt.label"
              :value="opt.value"
            />
          </el-select>
        </el-form-item>
        <el-form-item :label="t('users.scopes')">
          <el-select v-model="form.scopes" multiple style="width: 100%">
            <el-option
              v-for="opt in scopeOptions"
              :key="opt.value"
              :label="opt.label"
              :value="opt.value"
            />
          </el-select>
        </el-form-item>
        <el-form-item :label="t('users.isActive')">
          <el-switch v-model="form.is_active" />
        </el-form-item>
      </el-form>

      <template #footer>
        <el-button @click="dialogVisible = false">{{ t('common.cancel') }}</el-button>
        <el-button type="primary" :loading="dialogLoading" @click="handleSubmit">
          {{ t('common.confirm') }}
        </el-button>
      </template>
    </el-dialog>

    <div v-if="currentUser" class="danger-zone">
      <el-alert
        :title="t('auth.deactivate')"
        type="error"
        :description="t('auth.deactivateConfirm')"
        show-icon
        :closable="false"
      >
        <template #default>
          <div class="danger-zone-content">
            <div class="danger-zone-text">
              <strong>{{ t('auth.deactivate') }}</strong>
              <p>{{ t('auth.deactivateConfirm') }}</p>
            </div>
            <el-button type="danger" @click="handleDeactivate">
              {{ t('auth.deactivate') }}
            </el-button>
          </div>
        </template>
      </el-alert>
    </div>
  </div>
</template>

<style scoped>
.user-management {
  padding: var(--spacing-lg);
}

.page-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: var(--spacing-lg);
}

.page-header h1 {
  font-size: 1.5rem;
  font-weight: 600;
  color: var(--color-text-primary);
  margin: 0;
}

.admin-alert {
  margin-bottom: var(--spacing-md);
}

.user-table {
  width: 100%;
}

.scope-tag {
  margin-right: 4px;
  margin-bottom: 4px;
}

.danger-zone {
  margin-top: var(--spacing-xl);
}

.danger-zone-content {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--spacing-md);
}

.danger-zone-text p {
  margin: 4px 0 0 0;
  font-size: 0.875rem;
}
</style>
