<script setup lang="ts">
/**
 * Reports — test report export and traceability.
 *
 * Features:
 *   - Execution list table (loaded from listExecutions) with search filter.
 *   - Export buttons per row: ATML (XML preview), CSV (download), Parquet (download, may fall back to CSV).
 *   - Collapsible ATML XML preview (read-only textarea).
 *   - TracingViewer integration: embed the component for DUT serial trace search.
 *
 * Route: /reports
 */
import { computed, onMounted, ref } from 'vue'
import {
  ElButton,
  ElCard,
  ElCollapse,
  ElCollapseItem,
  ElDescriptions,
  ElDescriptionsItem,
  ElEmpty,
  ElInput,
  ElMessage,
  ElTable,
  ElTableColumn,
  ElTag,
} from 'element-plus'
import { Search } from '@element-plus/icons-vue'
import {
  downloadReport,
  getAtmlReport,
  triggerFileDownload,
} from '@/api/reports'
import {
  listExecutions,
  type ExecutionListItem,
} from '@/api/executions'
import TracingViewer from '@/views/TracingViewer.vue'

// ─── State ───────────────────────────────────────────────────────────────────

const executions = ref<ExecutionListItem[]>([])
const loading = ref<boolean>(false)
const searchQuery = ref<string>('')

/** Currently expanded ATML preview row's execution ID. */
const atmlExecutionId = ref<string | null>(null)
/** ATML XML content for the expanded row. */
const atmlContent = ref<string>('')
const atmlLoading = ref<boolean>(false)

/** Per-row download loading state, keyed by executionId + format. */
const downloadLoading = ref<Map<string, boolean>>(new Map())

/** Collapse model for ATML XML preview (always expanded when shown). */
const atmlCollapse = ref<string[]>(['atml'])

// ─── Computed ────────────────────────────────────────────────────────────────

/** Filtered execution list based on search query (DUT serial or execution ID). */
const filteredExecutions = computed<ExecutionListItem[]>(() => {
  const q = searchQuery.value.trim().toLowerCase()
  if (!q) return executions.value
  return executions.value.filter(
    (e) =>
      e.id.toLowerCase().includes(q) ||
      (e.dut_serial ?? '').toLowerCase().includes(q),
  )
})

// ─── Helpers ─────────────────────────────────────────────────────────────────

function statusTagType(
  status: string,
): 'success' | 'warning' | 'danger' | 'info' {
  switch (status) {
    case 'COMPLETED':
    case 'PASSED':
      return 'success'
    case 'RUNNING':
      return 'warning'
    case 'FAILED':
    case 'ABORTED':
    case 'ERROR':
      return 'danger'
    default:
      return 'info'
  }
}

function truncateId(id: string, maxLen: number = 12): string {
  if (id.length <= maxLen) return id
  return `${id.slice(0, maxLen)}…`
}

function formatDateTime(iso: string | null): string {
  if (!iso) return '—'
  try {
    return new Date(iso).toLocaleString()
  } catch {
    return iso
  }
}

function getDownloadKey(executionId: string, format: string): string {
  return `${executionId}:${format}`
}

function setDownloadLoading(executionId: string, format: string, val: boolean): void {
  const key = getDownloadKey(executionId, format)
  const map = new Map(downloadLoading.value)
  if (val) {
    map.set(key, true)
  } else {
    map.delete(key)
  }
  downloadLoading.value = map
}

function isDownloadLoading(executionId: string, format: string): boolean {
  return downloadLoading.value.get(getDownloadKey(executionId, format)) === true
}

// ─── Data loading ────────────────────────────────────────────────────────────

async function loadExecutions(): Promise<void> {
  loading.value = true
  try {
    const resp = await listExecutions(0, 50)
    executions.value = resp.items
  } catch (err) {
    console.error('Failed to load executions:', err)
    ElMessage.error('加载执行记录失败')
  } finally {
    loading.value = false
  }
}

// ─── Actions ─────────────────────────────────────────────────────────────────

/** Toggle the ATML XML preview for a given execution row. */
async function toggleAtmlPreview(row: ExecutionListItem): Promise<void> {
  // If clicking the same row that's already open → close it.
  if (atmlExecutionId.value === row.id) {
    atmlExecutionId.value = null
    atmlContent.value = ''
    return
  }

  atmlExecutionId.value = row.id
  atmlContent.value = ''
  atmlLoading.value = true
  try {
    atmlContent.value = await getAtmlReport(row.id)
  } catch (err) {
    console.error('Failed to load ATML report:', err)
    ElMessage.error('加载 ATML 报告失败')
    atmlExecutionId.value = null
  } finally {
    atmlLoading.value = false
  }
}

/** Download a report file in the specified format. */
async function handleDownload(
  row: ExecutionListItem,
  format: 'csv' | 'parquet',
): Promise<void> {
  setDownloadLoading(row.id, format, true)
  try {
    const blob = await downloadReport(row.id, format)
    const ext = format === 'parquet' ? 'parquet' : 'csv'
    const filename = `report_${row.id}.${ext}`
    triggerFileDownload(blob, filename)
    ElMessage.success(`报告已下载: ${filename}`)
  } catch (err) {
    console.error(`Failed to download ${format} report:`, err)
    ElMessage.error(`下载 ${format.toUpperCase()} 报告失败`)
  } finally {
    setDownloadLoading(row.id, format, false)
  }
}

// ─── Lifecycle ───────────────────────────────────────────────────────────────

onMounted(() => {
  loadExecutions()
})
</script>

<template>
  <div class="reports-view">
    <!-- ─── Header ─── -->
    <div class="rp-header">
      <div class="rp-header-left">
        <h2 class="rp-title">测试报告导出</h2>
        <span class="rp-subtitle">选择执行记录以导出报告</span>
      </div>
      <div class="rp-header-right">
        <ElInput
          v-model="searchQuery"
          placeholder="搜索执行 ID 或 DUT 序列号…"
          :prefix-icon="Search"
          clearable
          class="rp-search"
          data-testid="rp-search-input"
        />
        <ElButton
          :loading="loading"
          @click="loadExecutions"
          data-testid="rp-refresh-btn"
        >
          刷新
        </ElButton>
      </div>
    </div>

    <!-- ─── Execution List ─── -->
    <ElCard class="rp-card" shadow="never">
      <template #header>
        <span class="rp-card-title">最近执行记录</span>
      </template>

      <ElTable
        v-loading="loading"
        :data="filteredExecutions"
        stripe
        style="width: 100%"
        row-key="id"
        empty-text="暂无执行记录"
        data-testid="rp-executions-table"
      >
        <ElTableColumn
          label="执行 ID"
          width="160"
          prop="id"
        >
          <template #default="{ row }">
            <span class="rp-mono" :title="row.id">
              {{ truncateId(row.id) }}
            </span>
          </template>
        </ElTableColumn>

        <ElTableColumn label="状态" width="120" prop="status">
          <template #default="{ row }">
            <ElTag :type="statusTagType(row.status)" size="small">
              {{ row.status }}
            </ElTag>
          </template>
        </ElTableColumn>

        <ElTableColumn label="DUT 序列号" width="180" prop="dut_serial">
          <template #default="{ row }">
            <span class="rp-mono">{{ row.dut_serial ?? '—' }}</span>
          </template>
        </ElTableColumn>

        <ElTableColumn label="产品类型" width="140" prop="product_type">
          <template #default="{ row }">
            {{ row.product_type ?? '—' }}
          </template>
        </ElTableColumn>

        <ElTableColumn
          label="开始时间"
          width="180"
          prop="started_at"
        >
          <template #default="{ row }">
            {{ formatDateTime(row.started_at) }}
          </template>
        </ElTableColumn>

        <ElTableColumn
          label="完成时间"
          width="180"
          prop="completed_at"
        >
          <template #default="{ row }">
            {{ formatDateTime(row.completed_at) }}
          </template>
        </ElTableColumn>

        <ElTableColumn
          label="操作"
          width="280"
          fixed="right"
        >
          <template #default="{ row }">
            <div class="rp-actions">
              <ElButton
                size="small"
                :type="atmlExecutionId === row.id ? 'primary' : 'default'"
                :loading="atmlLoading && atmlExecutionId === row.id"
                @click="toggleAtmlPreview(row)"
                data-testid="rp-btn-atml"
              >
                ATML
              </ElButton>
              <ElButton
                size="small"
                type="success"
                :loading="isDownloadLoading(row.id, 'csv')"
                @click="handleDownload(row, 'csv')"
                data-testid="rp-btn-csv"
              >
                CSV
              </ElButton>
              <ElButton
                size="small"
                type="warning"
                :loading="isDownloadLoading(row.id, 'parquet')"
                @click="handleDownload(row, 'parquet')"
                data-testid="rp-btn-parquet"
              >
                Parquet
              </ElButton>
            </div>
          </template>
        </ElTableColumn>
      </ElTable>

      <ElEmpty
        v-if="!loading && filteredExecutions.length === 0"
        description="暂无匹配的执行记录"
      />
    </ElCard>

    <!-- ─── ATML XML Preview ─── -->
    <ElCollapse
      v-if="atmlExecutionId && atmlContent"
      v-model="atmlCollapse"
      class="rp-atml-collapse"
    >
      <ElCollapseItem
        title="ATML XML 预览"
        name="atml"
      >
        <ElDescriptions :column="1" border size="small" class="rp-atml-meta">
          <ElDescriptionsItem label="执行 ID">
            <span class="rp-mono">{{ atmlExecutionId }}</span>
          </ElDescriptionsItem>
        </ElDescriptions>
        <textarea
          class="rp-atml-textarea"
          readonly
          :value="atmlContent"
          rows="20"
          spellcheck="false"
          data-testid="rp-atml-preview"
        ></textarea>
      </ElCollapseItem>
    </ElCollapse>

    <!-- ─── TracingViewer integration ─── -->
    <ElCard class="rp-card rp-trace-card" shadow="never">
      <template #header>
        <span class="rp-card-title">追溯查询 (TracingViewer)</span>
      </template>
      <p class="rp-trace-desc">
        输入 DUT 序列号以查询完整的测试追溯链路，包含执行步骤、使用仪器和测量数据。
      </p>
      <TracingViewer />
    </ElCard>
  </div>
</template>

<style scoped>
/* ─── Layout ─── */
.reports-view {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-md);
  padding: var(--spacing-md) var(--spacing-lg);
  min-height: 100vh;
  background-color: var(--color-bg-secondary);
}

/* ─── Header ─── */
.rp-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: var(--spacing-sm);
}

.rp-header-left {
  display: flex;
  align-items: baseline;
  gap: var(--spacing-sm);
}

.rp-title {
  font-size: 1.25rem;
  font-weight: 600;
  color: var(--color-text-primary);
  margin: 0;
}

.rp-subtitle {
  font-size: 0.8125rem;
  color: var(--color-text-secondary);
}

.rp-header-right {
  display: flex;
  align-items: center;
  gap: var(--spacing-xs);
  flex-wrap: wrap;
}

.rp-search {
  width: 280px;
}

/* ─── Card ─── */
.rp-card {
  background-color: var(--color-bg-primary);
  border: 1px solid var(--color-border-default);
  border-radius: var(--radius-xl);
}

.rp-card-title {
  font-size: 0.9375rem;
  font-weight: 600;
  color: var(--color-text-primary);
}

/* ─── Table cells ─── */
.rp-mono {
  font-family: monospace;
  font-size: 0.8125rem;
  color: var(--color-text-primary);
}

/* ─── Actions ─── */
.rp-actions {
  display: flex;
  gap: var(--spacing-xs);
  flex-wrap: nowrap;
}

/* ─── ATML Preview ─── */
.rp-atml-collapse {
  background-color: var(--color-bg-primary);
  border: 1px solid var(--color-border-default);
  border-radius: var(--radius-xl);
}

.rp-atml-meta {
  margin-bottom: var(--spacing-sm);
}

.rp-atml-textarea {
  width: 100%;
  font-family: monospace;
  font-size: 0.75rem;
  line-height: 1.5;
  color: var(--color-text-primary);
  background-color: var(--color-bg-secondary);
  border: 1px solid var(--color-border-default);
  border-radius: var(--radius-md);
  padding: var(--spacing-sm);
  resize: vertical;
  outline: none;
}

.rp-atml-textarea:focus {
  border-color: var(--color-primary);
}

/* ─── Trace section ─── */
.rp-trace-card {
  overflow: hidden;
}

.rp-trace-desc {
  font-size: 0.8125rem;
  color: var(--color-text-secondary);
  margin: 0 0 var(--spacing-sm) 0;
}

/* ─── Responsive ─── */
@media (max-width: 768px) {
  .reports-view {
    padding: var(--spacing-sm);
  }

  .rp-header {
    flex-direction: column;
    align-items: stretch;
  }

  .rp-search {
    width: 100%;
  }
}
</style>
