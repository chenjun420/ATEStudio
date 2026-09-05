<script setup lang="ts">
/**
 * NodeFlowBinding — management view for node-flow bindings.
 *
 * Binds test sequences to worker nodes so that a sequence can be
 * executed on a specific worker with a priority and active flag.
 *
 * Features:
 *   - Table listing all bindings (worker_id, sequence_name, is_active,
 *     priority, config).
 *   - Filter by worker_id.
 *   - Create binding dialog (worker select, sequence select, priority,
 *     is_active toggle).
 *   - Edit binding dialog (is_active, priority, config JSON textarea).
 *   - Delete with confirm dialog.
 *   - Execute binding — shows ElMessage with execution_id.
 *   - Auto-refresh on mount.
 *
 * Route: /node-flow-bindings
 */
import { onMounted, ref, computed } from 'vue'
import {
  ElButton,
  ElCard,
  ElDialog,
  ElEmpty,
  ElForm,
  ElFormItem,
  ElInput,
  ElInputNumber,
  ElMessage,
  ElMessageBox,
  ElOption,
  ElSelect,
  ElSwitch,
  ElTable,
  ElTableColumn,
  ElTag,
} from 'element-plus'
import {
  createNodeFlowBinding,
  deleteNodeFlowBinding,
  executeBinding,
  getWorkers,
  listNodeFlowBindings,
  updateNodeFlowBinding,
  type NodeFlowBinding,
  type NodeFlowBindingCreate,
  type NodeFlowBindingUpdate,
  type WorkerInfo,
} from '@/api/stations'
import { fetchSequences, type Sequence } from '@/api/sequences'
import { useAuth } from '@/composables/useAuth'

const { hasScope } = useAuth()

// ─── State ──────────────────────────────────────────────────────────────────

const bindings = ref<NodeFlowBinding[]>([])
const workers = ref<WorkerInfo[]>([])
const sequences = ref<Sequence[]>([])
const loading = ref(false)

const filterWorkerId = ref<string>('')

// Create dialog
const createDialogVisible = ref(false)
const createLoading = ref(false)
const createForm = ref<{
  worker_id: string
  sequence_id: string
  priority: number
  is_active: boolean
}>({
  worker_id: '',
  sequence_id: '',
  priority: 0,
  is_active: true,
})

// Edit dialog
const editDialogVisible = ref(false)
const editLoading = ref(false)
const editingId = ref<string>('')
const editForm = ref<{
  is_active: boolean
  priority: number
  config: string
}>({
  is_active: true,
  priority: 0,
  config: '',
})

// ─── Computed ───────────────────────────────────────────────────────────────

const filteredBindings = computed<NodeFlowBinding[]>(() => {
  if (!filterWorkerId.value) return bindings.value
  return bindings.value.filter((b) => b.worker_id === filterWorkerId.value)
})

// ─── Data loading ───────────────────────────────────────────────────────────

async function loadBindings(): Promise<void> {
  loading.value = true
  try {
    const resp = await listNodeFlowBindings()
    bindings.value = resp.items
  } catch (err) {
    ElMessage.error('加载绑定列表失败')
    console.error(err)
  } finally {
    loading.value = false
  }
}

async function loadWorkers(): Promise<void> {
  try {
    const resp = await getWorkers()
    workers.value = resp.workers
  } catch (err) {
    console.error('加载 worker 列表失败', err)
  }
}

async function loadSequences(): Promise<void> {
  try {
    sequences.value = await fetchSequences()
  } catch (err) {
    console.error('加载序列列表失败', err)
  }
}

// ─── Create ─────────────────────────────────────────────────────────────────

function openCreateDialog(): void {
  createForm.value = {
    worker_id: '',
    sequence_id: '',
    priority: 0,
    is_active: true,
  }
  createDialogVisible.value = true
}

async function handleCreate(): Promise<void> {
  if (!createForm.value.worker_id) {
    ElMessage.warning('请选择 Worker')
    return
  }
  if (!createForm.value.sequence_id) {
    ElMessage.warning('请选择测试序列')
    return
  }

  createLoading.value = true
  try {
    const payload: NodeFlowBindingCreate = {
      worker_id: createForm.value.worker_id,
      sequence_id: createForm.value.sequence_id,
      priority: createForm.value.priority,
      is_active: createForm.value.is_active,
    }
    await createNodeFlowBinding(payload)
    ElMessage.success('绑定创建成功')
    createDialogVisible.value = false
    await loadBindings()
  } catch (err) {
    ElMessage.error('创建绑定失败')
    console.error(err)
  } finally {
    createLoading.value = false
  }
}

// ─── Edit ───────────────────────────────────────────────────────────────────

function openEditDialog(row: NodeFlowBinding): void {
  editingId.value = row.id
  editForm.value = {
    is_active: row.is_active,
    priority: row.priority,
    config: row.config ? JSON.stringify(row.config, null, 2) : '',
  }
  editDialogVisible.value = true
}

async function handleEdit(): Promise<void> {
  editLoading.value = true
  try {
    let parsedConfig: Record<string, unknown> | null = null
    if (editForm.value.config.trim()) {
      try {
        parsedConfig = JSON.parse(editForm.value.config)
      } catch {
        ElMessage.error('Config JSON 格式错误')
        editLoading.value = false
        return
      }
    }

    const payload: NodeFlowBindingUpdate = {
      is_active: editForm.value.is_active,
      priority: editForm.value.priority,
      config: parsedConfig,
    }
    await updateNodeFlowBinding(editingId.value, payload)
    ElMessage.success('绑定更新成功')
    editDialogVisible.value = false
    await loadBindings()
  } catch (err) {
    ElMessage.error('更新绑定失败')
    console.error(err)
  } finally {
    editLoading.value = false
  }
}

// ─── Delete ─────────────────────────────────────────────────────────────────

async function handleDelete(row: NodeFlowBinding): Promise<void> {
  try {
    await ElMessageBox.confirm(
      `确定删除绑定 "${row.worker_id} → ${row.sequence_name ?? row.sequence_id}" 吗？`,
      '删除确认',
      {
        confirmButtonText: '删除',
        cancelButtonText: '取消',
        type: 'warning',
      },
    )
    await deleteNodeFlowBinding(row.id)
    ElMessage.success('删除成功')
    await loadBindings()
  } catch (err) {
    if (err !== 'cancel') {
      ElMessage.error('删除绑定失败')
      console.error(err)
    }
  }
}

// ─── Execute ────────────────────────────────────────────────────────────────

async function handleExecute(row: NodeFlowBinding): Promise<void> {
  try {
    const resp = await executeBinding(row.id)
    ElMessage.success(`执行已触发，execution_id: ${resp.execution_id}`)
  } catch (err) {
    ElMessage.error('执行绑定失败')
    console.error(err)
  }
}

// ─── Lifecycle ──────────────────────────────────────────────────────────────

onMounted(() => {
  loadBindings()
  loadWorkers()
  loadSequences()
})
</script>

<template>
  <div class="node-flow-binding">
    <ElCard v-loading="loading" shadow="never" class="binding-card">
      <!-- Toolbar -->
      <div class="toolbar">
        <span class="toolbar-title">节点流程绑定</span>
        <div class="toolbar-actions">
          <ElSelect
            v-model="filterWorkerId"
            placeholder="按 Worker 筛选"
            clearable
            class="filter-select"
          >
            <ElOption label="全部 Worker" value="" />
            <ElOption
              v-for="w in workers"
              :key="w.worker_id"
              :label="w.worker_id"
              :value="w.worker_id"
            />
          </ElSelect>
          <ElButton v-if="hasScope('flow:write')" type="primary" @click="openCreateDialog">创建绑定</ElButton>
        </div>
      </div>

      <!-- Table -->
      <ElTable
        v-if="filteredBindings.length > 0"
        :data="filteredBindings"
        border
        stripe
        class="binding-table"
      >
        <ElTableColumn prop="worker_id" label="Worker ID" min-width="140" />
        <ElTableColumn label="测试序列" min-width="180">
          <template #default="{ row }">
            {{ row.sequence_name ?? row.sequence_id }}
          </template>
        </ElTableColumn>
        <ElTableColumn label="状态" width="100" align="center">
          <template #default="{ row }">
            <ElTag :type="row.is_active ? 'success' : 'info'" size="small">
              {{ row.is_active ? '启用' : '停用' }}
            </ElTag>
          </template>
        </ElTableColumn>
        <ElTableColumn prop="priority" label="优先级" width="90" align="center" />
        <ElTableColumn label="配置" min-width="200">
          <template #default="{ row }">
            <span v-if="row.config" class="config-cell">
              {{ JSON.stringify(row.config) }}
            </span>
            <span v-else class="config-empty">—</span>
          </template>
        </ElTableColumn>
        <ElTableColumn label="操作" width="220" align="center" fixed="right">
          <template #default="{ row }">
            <ElButton v-if="hasScope('flow:write')" size="small" @click="openEditDialog(row as NodeFlowBinding)">编辑</ElButton>
            <ElButton v-if="hasScope('flow:write')" size="small" type="danger" @click="handleDelete(row as NodeFlowBinding)">删除</ElButton>
            <ElButton v-if="hasScope('exec:run')" size="small" type="success" @click="handleExecute(row as NodeFlowBinding)">执行</ElButton>
          </template>
        </ElTableColumn>
      </ElTable>

      <!-- Empty state -->
      <ElEmpty v-else description="暂无绑定数据" />
    </ElCard>

    <!-- Create dialog -->
    <ElDialog
      v-model="createDialogVisible"
      title="创建绑定"
      width="500px"
      :close-on-click-modal="false"
    >
      <ElForm label-width="100px" label-position="right">
        <ElFormItem label="Worker" required>
          <ElSelect
            v-model="createForm.worker_id"
            placeholder="选择 Worker"
            filterable
            class="full-width"
          >
            <ElOption
              v-for="w in workers"
              :key="w.worker_id"
              :label="`${w.worker_id} (${w.hostname})`"
              :value="w.worker_id"
            />
          </ElSelect>
        </ElFormItem>
        <ElFormItem label="测试序列" required>
          <ElSelect
            v-model="createForm.sequence_id"
            placeholder="选择测试序列"
            filterable
            class="full-width"
          >
            <ElOption
              v-for="s in sequences"
              :key="s.id"
              :label="s.name"
              :value="s.id"
            />
          </ElSelect>
        </ElFormItem>
        <ElFormItem label="优先级">
          <ElInputNumber v-model="createForm.priority" :min="0" :max="999" />
        </ElFormItem>
        <ElFormItem label="启用">
          <ElSwitch v-model="createForm.is_active" />
        </ElFormItem>
      </ElForm>
      <template #footer>
        <ElButton @click="createDialogVisible = false">取消</ElButton>
        <ElButton type="primary" :loading="createLoading" @click="handleCreate">确定</ElButton>
      </template>
    </ElDialog>

    <!-- Edit dialog -->
    <ElDialog
      v-model="editDialogVisible"
      title="编辑绑定"
      width="500px"
      :close-on-click-modal="false"
    >
      <ElForm label-width="100px" label-position="right">
        <ElFormItem label="启用">
          <ElSwitch v-model="editForm.is_active" />
        </ElFormItem>
        <ElFormItem label="优先级">
          <ElInputNumber v-model="editForm.priority" :min="0" :max="999" />
        </ElFormItem>
        <ElFormItem label="配置 (JSON)">
          <ElInput
            v-model="editForm.config"
            type="textarea"
            :rows="6"
            placeholder='{"key": "value"}'
            class="full-width"
          />
        </ElFormItem>
      </ElForm>
      <template #footer>
        <ElButton @click="editDialogVisible = false">取消</ElButton>
        <ElButton type="primary" :loading="editLoading" @click="handleEdit">保存</ElButton>
      </template>
    </ElDialog>
  </div>
</template>

<style scoped>
.node-flow-binding {
  padding: 20px;
}

.binding-card {
  border-radius: 8px;
}

.toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 16px;
}

.toolbar-title {
  font-size: 18px;
  font-weight: 600;
  color: var(--color-text-primary);
}

.toolbar-actions {
  display: flex;
  align-items: center;
  gap: 12px;
}

.filter-select {
  width: 200px;
}

.binding-table {
  width: 100%;
}

.config-cell {
  display: inline-block;
  max-width: 100%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--color-text-secondary);
  font-family: monospace;
  font-size: 13px;
}

.config-empty {
  color: var(--color-text-tertiary);
}

.full-width {
  width: 100%;
}
</style>
