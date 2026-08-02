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
  ElTag,
  ElSwitch,
  ElMessage,
  ElMessageBox,
  ElAlert,
} from 'element-plus'
import {
  listRoles,
  listPermissions,
  createRole,
  updateRole,
  deleteRole,
  seedRBAC,
  type Role,
  type Permission,
  type RoleCreate,
  type RoleUpdate,
} from '@/api/rbac'
import { useAuth } from '@/composables/useAuth'

const { t } = useI18n()
const { hasScope } = useAuth()

const roles = ref<Role[]>([])
const permissions = ref<Permission[]>([])
const loading = ref(false)

const dialogVisible = ref(false)
const dialogMode = ref<'create' | 'edit'>('create')
const editingRoleId = ref<string | null>(null)
const dialogLoading = ref(false)

const form = reactive({
  name: '',
  description: '',
  permissions: [] as string[],
  is_active: true,
})

async function fetchData(): Promise<void> {
  loading.value = true
  try {
    const [rolesResp, permsResp] = await Promise.all([listRoles(), listPermissions()])
    roles.value = rolesResp.items
    permissions.value = permsResp.items
  } catch {
    ElMessage.error(t('common.error'))
  } finally {
    loading.value = false
  }
}

function resetForm(): void {
  form.name = ''
  form.description = ''
  form.permissions = []
  form.is_active = true
}

function openCreateDialog(): void {
  dialogMode.value = 'create'
  editingRoleId.value = null
  resetForm()
  dialogVisible.value = true
}

function openEditDialog(row: Role): void {
  dialogMode.value = 'edit'
  editingRoleId.value = row.id
  form.name = row.name
  form.description = row.description || ''
  form.permissions = [...row.permissions]
  form.is_active = row.is_active
  dialogVisible.value = true
}

async function handleSubmit(): Promise<void> {
  if (!form.name) {
    ElMessage.error(t('rbac.roleName'))
    return
  }

  dialogLoading.value = true
  try {
    if (dialogMode.value === 'create') {
      const data: RoleCreate = {
        name: form.name,
        description: form.description || undefined,
        permissions: form.permissions,
      }
      await createRole(data)
      ElMessage.success(t('common.success'))
    } else if (editingRoleId.value) {
      const data: RoleUpdate = {
        name: form.name,
        description: form.description || undefined,
        permissions: form.permissions,
        is_active: form.is_active,
      }
      await updateRole(editingRoleId.value, data)
      ElMessage.success(t('common.success'))
    }
    dialogVisible.value = false
    await fetchData()
  } catch {
    ElMessage.error(t('common.error'))
  } finally {
    dialogLoading.value = false
  }
}

async function handleDelete(row: Role): Promise<void> {
  if (row.is_system) {
    ElMessage.warning(t('rbac.cannotDeleteSystem'))
    return
  }
  try {
    await ElMessageBox.confirm(t('rbac.deleteConfirm'), t('common.confirm'), {
      type: 'warning',
    })
    await deleteRole(row.id)
    ElMessage.success(t('common.success'))
    await fetchData()
  } catch {
    // User cancelled or delete failed
  }
}

async function handleSeed(): Promise<void> {
  try {
    await seedRBAC()
    ElMessage.success(t('rbac.seedSuccess'))
    await fetchData()
  } catch {
    ElMessage.error(t('common.error'))
  }
}

onMounted(() => {
  fetchData()
})
</script>

<template>
  <div class="role-management">
    <header class="page-header">
      <h1>{{ t('rbac.title') }}</h1>
      <div class="header-actions">
        <el-button @click="handleSeed">
          {{ t('rbac.seed') }}
        </el-button>
        <el-button v-if="hasScope('admin')" type="primary" @click="openCreateDialog">
          {{ t('rbac.createRole') }}
        </el-button>
      </div>
    </header>

    <el-table
      :data="roles"
      v-loading="loading"
      class="role-table"
    >
      <el-table-column prop="name" :label="t('rbac.roleName')" />
      <el-table-column prop="description" :label="t('rbac.roleDesc')" />
      <el-table-column :label="t('rbac.systemRole')" width="100">
        <template #default="{ row }">
          <el-tag v-if="row.is_system" type="danger" size="small">
            {{ t('rbac.systemRole') }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column :label="t('rbac.active')" width="80">
        <template #default="{ row }">
          <el-tag :type="row.is_active ? 'success' : 'info'" size="small">
            {{ row.is_active ? t('common.success') : t('common.close') }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column :label="t('rbac.permissions')" min-width="200">
        <template #default="{ row }">
          <el-tag
            v-for="perm in row.permissions"
            :key="perm"
            size="small"
            class="perm-tag"
          >
            {{ perm }}
          </el-tag>
          <span v-if="row.permissions.length === 0" class="no-perms">—</span>
        </template>
      </el-table-column>
      <el-table-column :label="t('common.actions')" width="160">
        <template #default="{ row }">
          <el-button v-if="hasScope('admin')" size="small" @click="openEditDialog(row)">
            {{ t('common.edit') }}
          </el-button>
          <el-button
            v-if="hasScope('admin')"
            size="small"
            type="danger"
            :disabled="row.is_system"
            @click="handleDelete(row)"
          >
            {{ t('common.delete') }}
          </el-button>
        </template>
      </el-table-column>
    </el-table>

    <el-dialog
      v-model="dialogVisible"
      :title="dialogMode === 'create' ? t('rbac.createRole') : t('rbac.editRole')"
      width="520px"
    >
      <el-form label-position="top">
        <el-form-item :label="t('rbac.roleName')">
          <el-input
            v-model="form.name"
            :disabled="dialogMode === 'edit' && roles.find(r => r.id === editingRoleId)?.is_system"
          />
        </el-form-item>
        <el-form-item :label="t('rbac.description')">
          <el-input v-model="form.description" type="textarea" :rows="2" />
        </el-form-item>
        <el-form-item :label="t('rbac.selectPermissions')">
          <el-select
            v-model="form.permissions"
            multiple
            filterable
            style="width: 100%"
          >
            <el-option
              v-for="perm in permissions"
              :key="perm.id"
              :label="`${perm.code} (${perm.module})`"
              :value="perm.code"
            />
          </el-select>
        </el-form-item>
        <el-form-item v-if="dialogMode === 'edit'" :label="t('rbac.active')">
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
  </div>
</template>

<style scoped>
.role-management {
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

.header-actions {
  display: flex;
  gap: 8px;
}

.role-table {
  width: 100%;
}

.perm-tag {
  margin-right: 4px;
  margin-bottom: 4px;
}

.no-perms {
  color: var(--color-text-secondary);
}
</style>
