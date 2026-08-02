<script setup lang="ts">
/**
 * NodeTemplates — CRUD management for graph editor node templates.
 *
 * Features:
 *   - Table listing all templates (name, type, created_at, updated_at).
 *   - "新建模板" button → dialog with name, type, appearance (JSON), default_data (JSON).
 *   - "编辑" button per row → same dialog pre-filled.
 *   - "删除" button per row → confirm dialog.
 *   - Search filter by name.
 *
 * Route: /node-templates
 */
import { computed, onMounted, ref } from 'vue'
import {
  ElButton,
  ElCard,
  ElDialog,
  ElEmpty,
  ElForm,
  ElFormItem,
  ElInput,
  ElMessage,
  ElMessageBox,
  ElOption,
  ElSelect,
  ElTable,
  ElTableColumn,
  ElTag,
  type FormInstance,
  type FormRules,
} from 'element-plus'
import { Plus, Search } from '@element-plus/icons-vue'
import {
  createNodeTemplate,
  deleteNodeTemplate,
  listNodeTemplates,
  updateNodeTemplate,
  type NodeTemplate,
  type NodeTemplateCreate,
  type NodeTemplateUpdate,
} from '@/api/nodeTemplates'

// ─── Types ───────────────────────────────────────────────────────────────────

/** Supported node types for the type selector. */
const NODE_TYPES = [
  'script',
  'loop',
  'decision',
  'start',
  'end',
  'variable',
] as const

/** Dialog form model — JSON fields are edited as strings and parsed on save. */
interface TemplateForm {
  name: string
  type: string
  appearance: string
  default_data: string
}

// ─── State ───────────────────────────────────────────────────────────────────

const templates = ref<NodeTemplate[]>([])
const loading = ref<boolean>(false)
const searchQuery = ref<string>('')

const dialogVisible = ref<boolean>(false)
const dialogMode = ref<'create' | 'edit'>('create')
const editingId = ref<string | null>(null)
const formRef = ref<FormInstance | null>(null)
const saving = ref<boolean>(false)

const form = ref<TemplateForm>({
  name: '',
  type: 'script',
  appearance: '{}',
  default_data: '{}',
})

const formRules: FormRules<TemplateForm> = {
  name: [
    { required: true, message: '请输入模板名称', trigger: 'blur' },
    {
      min: 1,
      max: 100,
      message: '名称长度应在 1–100 个字符之间',
      trigger: 'blur',
    },
  ],
  type: [
    { required: true, message: '请选择节点类型', trigger: 'change' },
  ],
  appearance: [
    {
      validator: (_rule, value: string, callback) => {
        if (!value.trim()) {
          callback()
          return
        }
        try {
          JSON.parse(value)
          callback()
        } catch {
          callback(new Error('JSON 格式无效'))
        }
      },
      trigger: 'blur',
    },
  ],
  default_data: [
    {
      validator: (_rule, value: string, callback) => {
        if (!value.trim()) {
          callback()
          return
        }
        try {
          JSON.parse(value)
          callback()
        } catch {
          callback(new Error('JSON 格式无效'))
        }
      },
      trigger: 'blur',
    },
  ],
}

// ─── Computed ────────────────────────────────────────────────────────────────

/** Filtered template list based on search query (name only). */
const filteredTemplates = computed<NodeTemplate[]>(() => {
  const q = searchQuery.value.trim().toLowerCase()
  if (!q) return templates.value
  return templates.value.filter((t) => t.name.toLowerCase().includes(q))
})

// ─── Helpers ─────────────────────────────────────────────────────────────────

function tagTypeForNodeType(
  type: string,
): 'primary' | 'success' | 'warning' | 'danger' | 'info' {
  switch (type) {
    case 'start':
      return 'success'
    case 'end':
      return 'danger'
    case 'decision':
      return 'warning'
    case 'loop':
      return 'primary'
    case 'variable':
      return 'info'
    default:
      return 'info'
  }
}

function formatDateTime(iso: string): string {
  if (!iso) return '—'
  try {
    return new Date(iso).toLocaleString()
  } catch {
    return iso
  }
}

/** Safely stringify an optional record for textarea display. */
function jsonStringify(value: Record<string, unknown> | undefined): string {
  if (!value) return '{}'
  try {
    return JSON.stringify(value, null, 2)
  } catch {
    return '{}'
  }
}

/** Parse a JSON string, returning {} on empty or invalid input. */
function parseJsonField(
  value: string,
): Record<string, unknown> | undefined {
  const trimmed = value.trim()
  if (!trimmed) return undefined
  try {
    return JSON.parse(trimmed) as Record<string, unknown>
  } catch {
    return undefined
  }
}

// ─── Data loading ────────────────────────────────────────────────────────────

async function loadTemplates(): Promise<void> {
  loading.value = true
  try {
    templates.value = await listNodeTemplates()
  } catch (err) {
    console.error('Failed to load node templates:', err)
    ElMessage.error('加载节点模板失败')
  } finally {
    loading.value = false
  }
}

// ─── Dialog ──────────────────────────────────────────────────────────────────

function resetForm(): void {
  form.value = {
    name: '',
    type: 'script',
    appearance: '{}',
    default_data: '{}',
  }
  editingId.value = null
}

function openCreateDialog(): void {
  dialogMode.value = 'create'
  resetForm()
  dialogVisible.value = true
}

function openEditDialog(row: NodeTemplate): void {
  dialogMode.value = 'edit'
  editingId.value = row.id
  form.value = {
    name: row.name,
    type: row.type,
    appearance: jsonStringify(row.appearance),
    default_data: jsonStringify(row.default_data),
  }
  dialogVisible.value = true
}

async function handleSave(): Promise<void> {
  if (!formRef.value) return
  const valid = await formRef.value.validate().catch(() => false)
  if (!valid) return

  saving.value = true
  try {
    const appearance = parseJsonField(form.value.appearance)
    const defaultData = parseJsonField(form.value.default_data)

    if (dialogMode.value === 'create') {
      const payload: NodeTemplateCreate = {
        name: form.value.name,
        type: form.value.type,
        appearance,
        default_data: defaultData,
      }
      await createNodeTemplate(payload)
      ElMessage.success('模板创建成功')
    } else if (editingId.value) {
      const payload: NodeTemplateUpdate = {
        name: form.value.name,
        type: form.value.type,
        appearance,
        default_data: defaultData,
      }
      await updateNodeTemplate(editingId.value, payload)
      ElMessage.success('模板更新成功')
    }

    dialogVisible.value = false
    await loadTemplates()
  } catch (err) {
    console.error('Failed to save template:', err)
    ElMessage.error(dialogMode.value === 'create' ? '创建模板失败' : '更新模板失败')
  } finally {
    saving.value = false
  }
}

// ─── Delete ──────────────────────────────────────────────────────────────────

async function handleDelete(row: NodeTemplate): Promise<void> {
  try {
    await ElMessageBox.confirm(
      `确定要删除模板 "${row.name}" 吗？此操作不可撤销。`,
      '删除确认',
      {
        confirmButtonText: '删除',
        cancelButtonText: '取消',
        type: 'warning',
        confirmButtonClass: 'el-button--danger',
      },
    )
  } catch {
    return // user cancelled
  }

  try {
    await deleteNodeTemplate(row.id)
    ElMessage.success('模板已删除')
    await loadTemplates()
  } catch (err) {
    console.error('Failed to delete template:', err)
    ElMessage.error('删除模板失败')
  }
}

// ─── Lifecycle ───────────────────────────────────────────────────────────────

onMounted(() => {
  loadTemplates()
})
</script>

<template>
  <div class="node-templates-view">
    <!-- ─── Header ─── -->
    <div class="nt-header">
      <div class="nt-header-left">
        <h2 class="nt-title">节点模板管理</h2>
        <span class="nt-subtitle">管理流程图编辑器中的节点模板</span>
      </div>
      <div class="nt-header-right">
        <ElInput
          v-model="searchQuery"
          placeholder="搜索模板名称…"
          :prefix-icon="Search"
          clearable
          class="nt-search"
          data-testid="nt-search-input"
        />
        <ElButton
          type="primary"
          :icon="Plus"
          @click="openCreateDialog"
          data-testid="nt-btn-create"
        >
          新建模板
        </ElButton>
        <ElButton
          :loading="loading"
          @click="loadTemplates"
          data-testid="nt-btn-refresh"
        >
          刷新
        </ElButton>
      </div>
    </div>

    <!-- ─── Template Table ─── -->
    <ElCard class="nt-card" shadow="never">
      <ElTable
        v-loading="loading"
        :data="filteredTemplates"
        stripe
        style="width: 100%"
        row-key="id"
        empty-text="暂无节点模板"
        data-testid="nt-templates-table"
      >
        <ElTableColumn
          label="名称"
          min-width="200"
          prop="name"
          sortable
        >
          <template #default="{ row }">
            <span class="nt-name">{{ row.name }}</span>
          </template>
        </ElTableColumn>

        <ElTableColumn
          label="类型"
          width="140"
          prop="type"
          sortable
        >
          <template #default="{ row }">
            <ElTag :type="tagTypeForNodeType(row.type)" size="small">
              {{ row.type }}
            </ElTag>
          </template>
        </ElTableColumn>

        <ElTableColumn
          label="创建时间"
          width="200"
          prop="created_at"
          sortable
        >
          <template #default="{ row }">
            <span class="nt-time">{{ formatDateTime(row.created_at) }}</span>
          </template>
        </ElTableColumn>

        <ElTableColumn
          label="更新时间"
          width="200"
          prop="updated_at"
          sortable
        >
          <template #default="{ row }">
            <span class="nt-time">{{ formatDateTime(row.updated_at) }}</span>
          </template>
        </ElTableColumn>

        <ElTableColumn
          label="操作"
          width="160"
          fixed="right"
        >
          <template #default="{ row }">
            <div class="nt-actions">
              <ElButton
                size="small"
                type="primary"
                @click="openEditDialog(row)"
                data-testid="nt-btn-edit"
              >
                编辑
              </ElButton>
              <ElButton
                size="small"
                type="danger"
                @click="handleDelete(row)"
                data-testid="nt-btn-delete"
              >
                删除
              </ElButton>
            </div>
          </template>
        </ElTableColumn>
      </ElTable>

      <ElEmpty
        v-if="!loading && filteredTemplates.length === 0"
        description="暂无匹配的节点模板"
      />
    </ElCard>

    <!-- ─── Create / Edit Dialog ─── -->
    <ElDialog
      v-model="dialogVisible"
      :title="dialogMode === 'create' ? '新建模板' : '编辑模板'"
      width="600px"
      :close-on-click-modal="false"
      destroy-on-close
      data-testid="nt-dialog"
    >
      <ElForm
        ref="formRef"
        :model="form"
        :rules="formRules"
        label-width="100px"
        label-position="right"
        class="nt-form"
      >
        <ElFormItem label="名称" prop="name">
          <ElInput
            v-model="form.name"
            placeholder="请输入模板名称"
            maxlength="100"
            show-word-limit
            data-testid="nt-form-name"
          />
        </ElFormItem>

        <ElFormItem label="类型" prop="type">
          <ElSelect
            v-model="form.type"
            placeholder="请选择节点类型"
            class="nt-type-select"
            data-testid="nt-form-type"
          >
            <ElOption
              v-for="t in NODE_TYPES"
              :key="t"
              :label="t"
              :value="t"
            />
          </ElSelect>
        </ElFormItem>

        <ElFormItem label="外观 (JSON)" prop="appearance">
          <ElInput
            v-model="form.appearance"
            type="textarea"
            :rows="5"
            placeholder='{"fill": "#ffffff", "stroke": "#409eff"}'
            class="nt-json-input"
            data-testid="nt-form-appearance"
          />
        </ElFormItem>

        <ElFormItem label="默认数据 (JSON)" prop="default_data">
          <ElInput
            v-model="form.default_data"
            type="textarea"
            :rows="5"
            placeholder='{"timeout": 30, "retries": 3}'
            class="nt-json-input"
            data-testid="nt-form-default-data"
          />
        </ElFormItem>
      </ElForm>

      <template #footer>
        <ElButton
          @click="dialogVisible = false"
          data-testid="nt-dialog-cancel"
        >
          取消
        </ElButton>
        <ElButton
          type="primary"
          :loading="saving"
          @click="handleSave"
          data-testid="nt-dialog-save"
        >
          {{ dialogMode === 'create' ? '创建' : '保存' }}
        </ElButton>
      </template>
    </ElDialog>
  </div>
</template>

<style scoped>
/* ─── Layout ─── */
.node-templates-view {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-md);
  padding: var(--spacing-md) var(--spacing-lg);
  min-height: 100vh;
  background-color: var(--color-bg-secondary);
}

/* ─── Header ─── */
.nt-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: var(--spacing-sm);
}

.nt-header-left {
  display: flex;
  align-items: baseline;
  gap: var(--spacing-sm);
}

.nt-title {
  font-size: 1.25rem;
  font-weight: 600;
  color: var(--color-text-primary);
  margin: 0;
}

.nt-subtitle {
  font-size: 0.8125rem;
  color: var(--color-text-secondary);
}

.nt-header-right {
  display: flex;
  align-items: center;
  gap: var(--spacing-xs);
  flex-wrap: wrap;
}

.nt-search {
  width: 240px;
}

/* ─── Card ─── */
.nt-card {
  background-color: var(--color-bg-primary);
  border: 1px solid var(--color-border-default);
  border-radius: var(--radius-xl);
}

/* ─── Table cells ─── */
.nt-name {
  font-size: 0.875rem;
  font-weight: 500;
  color: var(--color-text-primary);
}

.nt-time {
  font-size: 0.8125rem;
  color: var(--color-text-secondary);
  font-family: monospace;
}

/* ─── Actions ─── */
.nt-actions {
  display: flex;
  gap: var(--spacing-xs);
  flex-wrap: nowrap;
}

/* ─── Dialog form ─── */
.nt-form {
  padding: var(--spacing-sm) var(--spacing-md);
}

.nt-type-select {
  width: 100%;
}

.nt-json-input :deep(.el-textarea__inner) {
  font-family: monospace;
  font-size: 0.8125rem;
}

/* ─── Responsive ─── */
@media (max-width: 768px) {
  .node-templates-view {
    padding: var(--spacing-sm);
  }

  .nt-header {
    flex-direction: column;
    align-items: stretch;
  }

  .nt-search {
    width: 100%;
  }
}
</style>
