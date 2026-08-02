<script setup lang="ts">
/**
 * ScriptManagement — manage test scripts with full CRUD, versioning, and AI tools.
 *
 * Features:
 *   - Table listing all scripts (name, description, version, category, tags, updated_at)
 *   - Create script dialog
 *   - Edit content dialog (code editor with commit message)
 *   - Version history dialog with per-version content viewer
 *   - Delete with confirmation
 *   - AI Generate dialog (spec_text → generated code + confidence/validation/suggestions)
 *   - AI Refine dialog (code + feedback → refined code)
 *   - Search filter by name
 *   - Auto-refresh on mount
 *
 * Route: /scripts
 */
import { Search } from '@element-plus/icons-vue'
import axios from 'axios'
import { computed, onMounted, ref } from 'vue'
import {
  ElButton,
  ElCard,
  ElDescriptions,
  ElDescriptionsItem,
  ElDialog,
  ElEmpty,
  ElForm,
  ElFormItem,
  ElInput,
  ElMessage,
  ElMessageBox,
  ElTable,
  ElTableColumn,
  ElTag,
} from 'element-plus'
import {
  fetchScriptContent,
  fetchScripts,
  fetchScriptVersionContent,
  fetchScriptVersions,
  updateScriptContent,
  type Script,
  type ScriptContentResponse,
  type ScriptVersionInfo,
} from '@/api/scripts'
import { useAuth } from '@/composables/useAuth'

const { hasScope } = useAuth()

// ─── Local axios instance for AI generate/refine endpoints ──────────────────

const aiApi = axios.create({
  baseURL: '/api/v1',
  timeout: 60000,
  headers: { 'Content-Type': 'application/json' },
})

interface AIResult {
  code: string
  confidence: number
  validation_errors: string[]
  suggestions: string[]
}

// ─── State: script list ─────────────────────────────────────────────────────

const scripts = ref<Script[]>([])
const loading = ref(false)
const searchQuery = ref('')

const filteredScripts = computed<Script[]>(() => {
  if (!searchQuery.value.trim()) return scripts.value
  const q = searchQuery.value.toLowerCase()
  return scripts.value.filter((s) => s.name.toLowerCase().includes(q))
})

// ─── State: create dialog ───────────────────────────────────────────────────

const createDialogVisible = ref(false)
const createForm = ref({
  name: '',
  description: '',
  version: '1.0.0',
  script_path: '',
  tags: '',
})

function resetCreateForm(): void {
  createForm.value = { name: '', description: '', version: '1.0.0', script_path: '', tags: '' }
}

function openCreateDialog(): void {
  resetCreateForm()
  createDialogVisible.value = true
}

// ─── State: edit content dialog ─────────────────────────────────────────────

const editDialogVisible = ref(false)
const editScript = ref<Script | null>(null)
const editContent = ref('')
const editCommitMessage = ref('')
const editLoading = ref(false)

async function openEditDialog(row: Script): Promise<void> {
  editScript.value = row
  editContent.value = ''
  editCommitMessage.value = ''
  editDialogVisible.value = true
  editLoading.value = true
  try {
    const resp = await fetchScriptContent(row.id)
    editContent.value = resp.content
  } catch {
    ElMessage.error('加载脚本内容失败')
  } finally {
    editLoading.value = false
  }
}

async function saveEditContent(): Promise<void> {
  if (!editScript.value) return
  editLoading.value = true
  try {
    await updateScriptContent(editScript.value.id, {
      content: editContent.value,
      commit_message: editCommitMessage.value || `更新脚本 ${editScript.value.name}`,
    })
    ElMessage.success('脚本内容已保存')
    editDialogVisible.value = false
    await loadScripts()
  } catch {
    ElMessage.error('保存失败')
  } finally {
    editLoading.value = false
  }
}

// ─── State: version history dialog ──────────────────────────────────────────

const versionDialogVisible = ref(false)
const versionScript = ref<Script | null>(null)
const versions = ref<ScriptVersionInfo[]>([])
const versionLoading = ref(false)

async function openVersionDialog(row: Script): Promise<void> {
  versionScript.value = row
  versions.value = []
  versionDialogVisible.value = true
  versionLoading.value = true
  try {
    const resp = await fetchScriptVersions(row.id)
    versions.value = resp.versions
  } catch {
    ElMessage.error('加载版本历史失败')
  } finally {
    versionLoading.value = false
  }
}

// ─── State: version content viewer dialog ───────────────────────────────────

const versionContentDialogVisible = ref(false)
const versionContentTitle = ref('')
const versionContent = ref('')
const versionContentLoading = ref(false)

async function viewVersionContent(hash: string): Promise<void> {
  if (!versionScript.value) return
  versionContentTitle.value = `版本 ${hash.slice(0, 8)} 的内容`
  versionContentDialogVisible.value = true
  versionContentLoading.value = true
  try {
    const resp = await fetchScriptVersionContent(versionScript.value.id, hash)
    versionContent.value = resp.content
  } catch {
    ElMessage.error('加载版本内容失败')
  } finally {
    versionContentLoading.value = false
  }
}

// ─── State: delete ──────────────────────────────────────────────────────────

async function handleDelete(row: Script): Promise<void> {
  try {
    await ElMessageBox.confirm(
      `确定要删除脚本 "${row.name}" 吗？此操作不可恢复。`,
      '删除确认',
      { confirmButtonText: '删除', cancelButtonText: '取消', type: 'warning' },
    )
  } catch {
    return // user cancelled
  }
  // Delete via local axios (no dedicated API function needed)
  try {
    await axios.delete(`/api/v1/scripts/${row.id}`)
    ElMessage.success('脚本已删除')
    await loadScripts()
  } catch {
    ElMessage.error('删除失败')
  }
}

// ─── State: AI Generate dialog ──────────────────────────────────────────────

const generateDialogVisible = ref(false)
const generateForm = ref({ spec_text: '', product_type: '' })
const generateResult = ref<AIResult | null>(null)
const generateLoading = ref(false)

// Save generated code as new script
const generateSaveForm = ref({ name: '', script_path: '', tags: '' })
const generateSaveVisible = ref(false)

function openGenerateDialog(): void {
  generateForm.value = { spec_text: '', product_type: '' }
  generateResult.value = null
  generateDialogVisible.value = true
}

async function runGenerate(): Promise<void> {
  if (!generateForm.value.spec_text.trim()) {
    ElMessage.warning('请输入需求描述')
    return
  }
  generateLoading.value = true
  try {
    const resp = await aiApi.post<AIResult>('/scripts/generate', {
      spec_text: generateForm.value.spec_text,
      product_type: generateForm.value.product_type,
    })
    generateResult.value = resp.data
  } catch {
    ElMessage.error('AI生成失败')
  } finally {
    generateLoading.value = false
  }
}

function openGenerateSave(): void {
  generateSaveForm.value = { name: '', script_path: '', tags: '' }
  generateSaveVisible.value = true
}

async function saveGeneratedScript(): Promise<void> {
  if (!generateResult.value) return
  if (!generateSaveForm.value.name.trim() || !generateSaveForm.value.script_path.trim()) {
    ElMessage.warning('请填写脚本名称和路径')
    return
  }
  // Create the script then set its content
  try {
    const createResp = await axios.post<Script>('/api/v1/scripts', {
      name: generateSaveForm.value.name,
      description: `AI generated for ${generateForm.value.product_type || 'unknown'}`,
      version: '1.0.0',
      script_path: generateSaveForm.value.script_path,
      tags: generateSaveForm.value.tags
        ? generateSaveForm.value.tags.split(',').map((t) => t.trim()).filter(Boolean)
        : [],
    })
    await updateScriptContent(createResp.data.id, {
      content: generateResult.value.code,
      commit_message: 'AI生成初始版本',
    })
    ElMessage.success('AI生成脚本已保存')
    generateSaveVisible.value = false
    generateDialogVisible.value = false
    await loadScripts()
  } catch {
    ElMessage.error('保存失败')
  }
}

// ─── State: AI Refine dialog ────────────────────────────────────────────────

const refineDialogVisible = ref(false)
const refineForm = ref({ code: '', feedback: '', product_type: '' })
const refineResult = ref<AIResult | null>(null)
const refineLoading = ref(false)

function openRefineDialog(): void {
  refineForm.value = { code: '', feedback: '', product_type: '' }
  refineResult.value = null
  refineDialogVisible.value = true
}

async function runRefine(): Promise<void> {
  if (!refineForm.value.code.trim() || !refineForm.value.feedback.trim()) {
    ElMessage.warning('请填写代码和优化反馈')
    return
  }
  refineLoading.value = true
  try {
    const resp = await aiApi.post<AIResult>('/scripts/refine', {
      code: refineForm.value.code,
      feedback: refineForm.value.feedback,
      product_type: refineForm.value.product_type,
    })
    refineResult.value = resp.data
  } catch {
    ElMessage.error('AI优化失败')
  } finally {
    refineLoading.value = false
  }
}

// ─── State: create script submit ────────────────────────────────────────────

const createLoading = ref(false)

async function submitCreate(): Promise<void> {
  if (!createForm.value.name.trim() || !createForm.value.script_path.trim()) {
    ElMessage.warning('请填写脚本名称和路径')
    return
  }
  createLoading.value = true
  try {
    const tags = createForm.value.tags
      ? createForm.value.tags.split(',').map((t) => t.trim()).filter(Boolean)
      : []
    await axios.post<Script>('/api/v1/scripts', {
      name: createForm.value.name,
      description: createForm.value.description,
      version: createForm.value.version || '1.0.0',
      script_path: createForm.value.script_path,
      tags,
    })
    ElMessage.success('脚本已创建')
    createDialogVisible.value = false
    await loadScripts()
  } catch {
    ElMessage.error('创建失败')
  } finally {
    createLoading.value = false
  }
}

// ─── Data loading ───────────────────────────────────────────────────────────

async function loadScripts(): Promise<void> {
  loading.value = true
  try {
    scripts.value = await fetchScripts()
  } catch {
    ElMessage.error('加载脚本列表失败')
    scripts.value = []
  } finally {
    loading.value = false
  }
}

// ─── Formatting helpers ─────────────────────────────────────────────────────

function formatTime(value?: string): string {
  if (!value) return '—'
  const date = new Date(value)
  return date.toLocaleString('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  })
}

function confidenceType(confidence: number): 'success' | 'warning' | 'danger' {
  if (confidence >= 0.8) return 'success'
  if (confidence >= 0.5) return 'warning'
  return 'danger'
}

// ─── Lifecycle ──────────────────────────────────────────────────────────────

onMounted(() => {
  loadScripts()
})
</script>

<template>
  <div class="script-management">
    <!-- Toolbar -->
    <div class="toolbar">
      <div class="toolbar-left">
        <ElInput
          v-model="searchQuery"
          placeholder="搜索脚本名称..."
          clearable
          class="search-input"
          :prefix-icon="Search"
        />
      </div>
      <div class="toolbar-right">
        <ElButton type="primary" @click="openGenerateDialog">AI 生成</ElButton>
        <ElButton type="success" @click="openRefineDialog">AI 优化</ElButton>
        <ElButton type="primary" plain @click="loadScripts">刷新</ElButton>
        <ElButton v-if="hasScope('flow:write')" type="primary" @click="openCreateDialog">新建脚本</ElButton>
      </div>
    </div>

    <!-- Scripts Table -->
    <ElCard v-loading="loading" class="table-card">
      <ElTable
        v-if="filteredScripts.length > 0"
        :data="filteredScripts"
        stripe
        style="width: 100%"
        row-key="id"
      >
        <ElTableColumn label="脚本名称" min-width="220">
          <template #default="{ row }">
            <div class="script-name-cell">
              <span class="script-name">{{ row.name }}</span>
              <span class="script-desc">{{ row.description || '暂无描述' }}</span>
            </div>
          </template>
        </ElTableColumn>

        <ElTableColumn prop="version" label="版本" width="100" align="center" />

        <ElTableColumn label="分类" width="120" align="center">
          <template #default="{ row }">
            <ElTag v-if="row.category" size="small" type="info">{{ row.category }}</ElTag>
            <span v-else class="muted">—</span>
          </template>
        </ElTableColumn>

        <ElTableColumn label="标签" min-width="180">
          <template #default="{ row }">
            <div v-if="row.tags && row.tags.length > 0" class="tags-cell">
              <ElTag
                v-for="tag in row.tags"
                :key="tag"
                size="small"
                class="tag-item"
              >
                {{ tag }}
              </ElTag>
            </div>
            <span v-else class="muted">—</span>
          </template>
        </ElTableColumn>

        <ElTableColumn label="更新时间" width="180" align="center">
          <template #default="{ row }">
            {{ formatTime(row.updated_at) }}
          </template>
        </ElTableColumn>

        <ElTableColumn label="操作" width="340" fixed="right" align="center">
          <template #default="{ row }">
            <ElButton v-if="hasScope('flow:write')" size="small" type="primary" link @click="openEditDialog(row)">
              编辑内容
            </ElButton>
            <ElButton size="small" type="info" link @click="openVersionDialog(row)">
              版本历史
            </ElButton>
            <ElButton v-if="hasScope('flow:write')" size="small" type="danger" link @click="handleDelete(row)">
              删除
            </ElButton>
          </template>
        </ElTableColumn>
      </ElTable>

      <ElEmpty v-else description="暂无脚本数据" />
    </ElCard>

    <!-- ─── Create Dialog ──────────────────────────────────────────────── -->
    <ElDialog
      v-model="createDialogVisible"
      title="新建脚本"
      width="600px"
      destroy-on-close
    >
      <ElForm :model="createForm" label-width="100px" label-position="right">
        <ElFormItem label="脚本名称" required>
          <ElInput v-model="createForm.name" placeholder="请输入脚本名称" />
        </ElFormItem>
        <ElFormItem label="描述">
          <ElInput
            v-model="createForm.description"
            type="textarea"
            :rows="2"
            placeholder="简要描述脚本功能"
          />
        </ElFormItem>
        <ElFormItem label="版本">
          <ElInput v-model="createForm.version" placeholder="1.0.0" />
        </ElFormItem>
        <ElFormItem label="脚本路径" required>
          <ElInput v-model="createForm.script_path" placeholder="scripts/example.py" />
        </ElFormItem>
        <ElFormItem label="标签">
          <ElInput v-model="createForm.tags" placeholder="多个标签用逗号分隔" />
        </ElFormItem>
      </ElForm>
      <template #footer>
        <ElButton @click="createDialogVisible = false">取消</ElButton>
        <ElButton type="primary" :loading="createLoading" @click="submitCreate">
          创建
        </ElButton>
      </template>
    </ElDialog>

    <!-- ─── Edit Content Dialog ────────────────────────────────────────── -->
    <ElDialog
      v-model="editDialogVisible"
      :title="`编辑内容 - ${editScript?.name ?? ''}`"
      width="900px"
      destroy-on-close
    >
      <div v-loading="editLoading" class="edit-content-wrapper">
        <ElInput
          v-model="editContent"
          type="textarea"
          :rows="24"
          class="code-textarea"
          placeholder="脚本内容"
        />
        <div class="commit-message-row">
          <ElInput
            v-model="editCommitMessage"
            placeholder="提交信息（可选）"
            class="commit-input"
          />
        </div>
      </div>
      <template #footer>
        <ElButton @click="editDialogVisible = false">取消</ElButton>
        <ElButton type="primary" :loading="editLoading" @click="saveEditContent">
          保存
        </ElButton>
      </template>
    </ElDialog>

    <!-- ─── Version History Dialog ─────────────────────────────────────── -->
    <ElDialog
      v-model="versionDialogVisible"
      :title="`版本历史 - ${versionScript?.name ?? ''}`"
      width="800px"
      destroy-on-close
    >
      <div v-loading="versionLoading">
        <ElTable
          v-if="versions.length > 0"
          :data="versions"
          stripe
          style="width: 100%"
          max-height="400"
        >
          <ElTableColumn label="Hash" width="120">
            <template #default="{ row }">
              <code class="hash-text">{{ row.hash.slice(0, 8) }}</code>
            </template>
          </ElTableColumn>
          <ElTableColumn prop="message" label="提交信息" min-width="200" />
          <ElTableColumn prop="author" label="作者" width="120" />
          <ElTableColumn label="时间" width="180">
            <template #default="{ row }">
              {{ formatTime(row.timestamp) }}
            </template>
          </ElTableColumn>
          <ElTableColumn label="操作" width="100" align="center">
            <template #default="{ row }">
              <ElButton
                size="small"
                type="primary"
                link
                @click="viewVersionContent(row.hash)"
              >
                查看此版本
              </ElButton>
            </template>
          </ElTableColumn>
        </ElTable>
        <ElEmpty v-else description="暂无版本历史" />
      </div>
    </ElDialog>

    <!-- ─── Version Content Dialog ─────────────────────────────────────── -->
    <ElDialog
      v-model="versionContentDialogVisible"
      :title="versionContentTitle"
      width="900px"
      destroy-on-close
      append-to-body
    >
      <div v-loading="versionContentLoading">
        <ElInput
          v-model="versionContent"
          type="textarea"
          :rows="24"
          readonly
          class="code-textarea"
        />
      </div>
      <template #footer>
        <ElButton @click="versionContentDialogVisible = false">关闭</ElButton>
      </template>
    </ElDialog>

    <!-- ─── AI Generate Dialog ─────────────────────────────────────────── -->
    <ElDialog
      v-model="generateDialogVisible"
      title="AI 生成脚本"
      width="900px"
      destroy-on-close
    >
      <div class="ai-section">
        <ElForm label-width="100px" label-position="right">
          <ElFormItem label="需求描述">
            <ElInput
              v-model="generateForm.spec_text"
              type="textarea"
              :rows="6"
              placeholder="描述你需要的测试脚本功能、测试步骤、预期结果..."
              class="ai-spec-input"
            />
          </ElFormItem>
          <ElFormItem label="产品类型">
            <ElInput
              v-model="generateForm.product_type"
              placeholder="如：通信模块、服务器、消费电子"
            />
          </ElFormItem>
        </ElForm>
        <div class="ai-action-row">
          <ElButton type="primary" :loading="generateLoading" @click="runGenerate">
            生成脚本
          </ElButton>
        </div>
      </div>

      <!-- Generate Result -->
      <div v-if="generateResult" class="ai-result-section">
        <ElDescriptions :column="1" border class="result-descriptions">
          <ElDescriptionsItem label="置信度">
            <ElTag :type="confidenceType(generateResult.confidence)">
              {{ (generateResult.confidence * 100).toFixed(1) }}%
            </ElTag>
          </ElDescriptionsItem>
        </ElDescriptions>

        <div class="result-block">
          <div class="result-label">生成代码</div>
          <ElInput
            v-model="generateResult.code"
            type="textarea"
            :rows="12"
            class="code-textarea"
          />
        </div>

        <div v-if="generateResult.validation_errors.length > 0" class="result-block">
          <div class="result-label">验证错误</div>
          <ul class="result-list error-list">
            <li v-for="(err, i) in generateResult.validation_errors" :key="i">
              {{ err }}
            </li>
          </ul>
        </div>

        <div v-if="generateResult.suggestions.length > 0" class="result-block">
          <div class="result-label">优化建议</div>
          <ul class="result-list suggestion-list">
            <li v-for="(sug, i) in generateResult.suggestions" :key="i">
              {{ sug }}
            </li>
          </ul>
        </div>

        <div class="ai-action-row">
          <ElButton type="success" @click="openGenerateSave">保存为脚本</ElButton>
        </div>
      </div>

      <!-- Save Generated Script Sub-Dialog -->
      <ElDialog
        v-model="generateSaveVisible"
        title="保存 AI 生成脚本"
        width="500px"
        append-to-body
        destroy-on-close
      >
        <ElForm :model="generateSaveForm" label-width="80px" label-position="right">
          <ElFormItem label="名称" required>
            <ElInput v-model="generateSaveForm.name" placeholder="脚本名称" />
          </ElFormItem>
          <ElFormItem label="路径" required>
            <ElInput v-model="generateSaveForm.script_path" placeholder="scripts/xxx.py" />
          </ElFormItem>
          <ElFormItem label="标签">
            <ElInput v-model="generateSaveForm.tags" placeholder="逗号分隔" />
          </ElFormItem>
        </ElForm>
        <template #footer>
          <ElButton @click="generateSaveVisible = false">取消</ElButton>
          <ElButton type="primary" @click="saveGeneratedScript">保存</ElButton>
        </template>
      </ElDialog>
    </ElDialog>

    <!-- ─── AI Refine Dialog ───────────────────────────────────────────── -->
    <ElDialog
      v-model="refineDialogVisible"
      title="AI 优化脚本"
      width="900px"
      destroy-on-close
    >
      <div class="ai-section">
        <ElForm label-width="100px" label-position="right">
          <ElFormItem label="原始代码">
            <ElInput
              v-model="refineForm.code"
              type="textarea"
              :rows="10"
              placeholder="粘贴需要优化的代码"
              class="code-textarea"
            />
          </ElFormItem>
          <ElFormItem label="优化反馈">
            <ElInput
              v-model="refineForm.feedback"
              type="textarea"
              :rows="3"
              placeholder="描述需要优化的方向，如：增加错误处理、优化性能..."
            />
          </ElFormItem>
          <ElFormItem label="产品类型">
            <ElInput
              v-model="refineForm.product_type"
              placeholder="可选，如：通信模块"
            />
          </ElFormItem>
        </ElForm>
        <div class="ai-action-row">
          <ElButton type="primary" :loading="refineLoading" @click="runRefine">
            优化代码
          </ElButton>
        </div>
      </div>

      <!-- Refine Result -->
      <div v-if="refineResult" class="ai-result-section">
        <ElDescriptions :column="1" border class="result-descriptions">
          <ElDescriptionsItem label="置信度">
            <ElTag :type="confidenceType(refineResult.confidence)">
              {{ (refineResult.confidence * 100).toFixed(1) }}%
            </ElTag>
          </ElDescriptionsItem>
        </ElDescriptions>

        <div class="result-block">
          <div class="result-label">优化后代码</div>
          <ElInput
            v-model="refineResult.code"
            type="textarea"
            :rows="12"
            class="code-textarea"
          />
        </div>

        <div v-if="refineResult.validation_errors.length > 0" class="result-block">
          <div class="result-label">验证错误</div>
          <ul class="result-list error-list">
            <li v-for="(err, i) in refineResult.validation_errors" :key="i">
              {{ err }}
            </li>
          </ul>
        </div>

        <div v-if="refineResult.suggestions.length > 0" class="result-block">
          <div class="result-label">优化建议</div>
          <ul class="result-list suggestion-list">
            <li v-for="(sug, i) in refineResult.suggestions" :key="i">
              {{ sug }}
            </li>
          </ul>
        </div>
      </div>
    </ElDialog>
  </div>
</template>

<style scoped>
.script-management {
  padding: 20px;
}

/* ─── Toolbar ─────────────────────────────────────────────────────────── */

.toolbar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 16px;
  gap: 12px;
}

.toolbar-left {
  flex: 1;
  max-width: 360px;
}

.toolbar-right {
  display: flex;
  gap: 8px;
  flex-shrink: 0;
}

.search-input {
  width: 100%;
}

/* ─── Table Card ──────────────────────────────────────────────────────── */

.table-card {
  border-radius: 8px;
}

.script-name-cell {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.script-name {
  font-weight: 600;
  color: var(--color-text-primary);
  font-size: 14px;
}

.script-desc {
  font-size: 12px;
  color: var(--color-text-secondary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.tags-cell {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
}

.tag-item {
  margin: 0;
}

.muted {
  color: var(--color-text-tertiary);
}

.hash-text {
  font-family: 'Consolas', 'Monaco', monospace;
  font-size: 13px;
  color: var(--color-primary);
}

/* ─── Edit Content ────────────────────────────────────────────────────── */

.edit-content-wrapper {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.commit-message-row {
  margin-top: 4px;
}

.code-textarea :deep(.el-textarea__inner) {
  font-family: 'Consolas', 'Monaco', 'Courier New', monospace;
  font-size: 13px;
  line-height: 1.6;
  min-height: 500px;
}

/* ─── AI Sections ─────────────────────────────────────────────────────── */

.ai-section {
  margin-bottom: 16px;
}

.ai-spec-input :deep(.el-textarea__inner) {
  min-height: 120px;
}

.ai-action-row {
  display: flex;
  justify-content: flex-end;
  margin-top: 12px;
}

.ai-result-section {
  margin-top: 20px;
  border-top: 1px solid var(--color-border-default);
  padding-top: 16px;
}

.result-descriptions {
  margin-bottom: 16px;
}

.result-block {
  margin-bottom: 16px;
}

.result-label {
  font-weight: 600;
  color: var(--color-text-primary);
  margin-bottom: 8px;
  font-size: 14px;
}

.result-list {
  margin: 0;
  padding-left: 20px;
  list-style-type: disc;
  font-size: 13px;
  line-height: 1.8;
}

.error-list li {
  color: var(--color-error);
}

.suggestion-list li {
  color: var(--color-warning);
}

/* ─── Responsive ──────────────────────────────────────────────────────── */

@media (max-width: 768px) {
  .toolbar {
    flex-direction: column;
    align-items: stretch;
  }

  .toolbar-left {
    max-width: 100%;
  }

  .toolbar-right {
    flex-wrap: wrap;
  }
}
</style>
