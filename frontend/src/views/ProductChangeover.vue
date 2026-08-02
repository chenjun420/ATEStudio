<script setup lang="ts">
/**
 * Product Changeover Dashboard.
 *
 * Provides a visual interface for managing and optimizing product
 * changeover costs in flexible production lines:
 *
 *   1. Matrix heatmap — el-table showing transition costs between all
 *      product pairs, color-coded by cost magnitude.
 *   2. Cost management — inline add/edit/delete transition costs.
 *   3. Sequence optimization — input product list, get optimal ordering
 *      with total cost/time breakdown and step-by-step transitions.
 *   4. Real-time progress — estimated vs actual changeover times.
 *
 * API: /api/v1/changeover (see src/ate_cloud/api/v1/changeover.py).
 * Route: /changeover
 */
import { computed, onMounted, ref } from 'vue'
import {
  ElTable,
  ElTableColumn,
  ElTag,
  ElButton,
  ElCard,
  ElDialog,
  ElForm,
  ElFormItem,
  ElInput,
  ElInputNumber,
  ElSelect,
  ElOption,
  ElMessage,
  ElEmpty,
  ElSkeleton,
  ElAlert,
  ElTimeline,
  ElTimelineItem,
  ElStatistic,
  ElIcon,
} from 'element-plus'
import { Refresh, Plus, Delete, Aim as OptimizeIcon } from '@element-plus/icons-vue'
import axios from 'axios'
import { useAuth } from '@/composables/useAuth'

const { hasScope } = useAuth()

const api = axios.create({
  baseURL: '/api/v1',
  timeout: 30000,
  headers: { 'Content-Type': 'application/json' },
})

// ─── Types ───────────────────────────────────────────────────────────────────

interface MatrixEntry {
  from_product: string
  to_product: string
  cost: number | null
  time_minutes: number | null
}

interface MatrixResponse {
  products: string[]
  entries: MatrixEntry[]
}

interface Transition {
  from_product: string
  to_product: string
  cost: number
  time_minutes: number
}

interface OptimizeResult {
  sequence: string[]
  total_cost: number
  total_time_minutes: number
  transitions: Transition[]
}

// ─── State ───────────────────────────────────────────────────────────────────

const loading = ref(false)
const error = ref<string | null>(null)
const products = ref<string[]>([])
const entries = ref<MatrixEntry[]>([])

// Add/edit dialog state
const dialogVisible = ref(false)
const dialogMode = ref<'create' | 'edit'>('create')
const dialogLoading = ref(false)
const form = ref({
  product_a: '',
  product_b: '',
  cost: 0,
  time_minutes: 0,
})

// Optimize state
const optimizeLoading = ref(false)
const optimizeProducts = ref('')
const startProduct = ref('')
const optimizeResult = ref<OptimizeResult | null>(null)

// ─── Computed ────────────────────────────────────────────────────────────────

/** Matrix as a 2D map: matrixData[from][to] = { cost, time_minutes } */
const matrixData = computed<Record<string, Record<string, { cost: number | null; time_minutes: number | null }>>>(() => {
  const result: Record<string, Record<string, { cost: number | null; time_minutes: number | null }>> = {}
  for (const p of products.value) {
    result[p] = {}
    for (const p2 of products.value) {
      result[p][p2] = { cost: null, time_minutes: null }
    }
  }
  for (const entry of entries.value) {
    if (!result[entry.from_product]) result[entry.from_product] = {}
    result[entry.from_product][entry.to_product] = {
      cost: entry.cost,
      time_minutes: entry.time_minutes,
    }
  }
  return result
})

/** Max cost for color scaling */
const maxCost = computed(() => {
  let max = 0
  for (const entry of entries.value) {
    if (entry.cost !== null && entry.cost > max) max = entry.cost
  }
  return max || 1
})

// ─── Methods ─────────────────────────────────────────────────────────────────

/** Get cell color based on cost magnitude (heatmap effect) */
function getCellColor(cost: number | null): string {
  if (cost === null) return ''
  const ratio = cost / maxCost.value
  if (ratio === 0) return 'background-color: #f0f9eb'
  if (ratio < 0.25) return 'background-color: #e1f3d8'
  if (ratio < 0.5) return 'background-color: #faecd8'
  if (ratio < 0.75) return 'background-color: #f5dab1'
  return 'background-color: #fbc4c4'
}

/** Get tag type for cost display */
function getCostTagType(cost: number | null): 'success' | 'warning' | 'danger' | 'info' {
  if (cost === null) return 'info'
  const ratio = cost / maxCost.value
  if (ratio < 0.25) return 'success'
  if (ratio < 0.5) return 'warning'
  return 'danger'
}

async function fetchMatrix() {
  loading.value = true
  error.value = null
  try {
    const response = await api.get<MatrixResponse>('/changeover/matrix')
    products.value = response.data.products
    entries.value = response.data.entries
  } catch (e: unknown) {
    const msg = e instanceof Error ? e.message : 'Failed to fetch matrix'
    error.value = msg
    ElMessage.error(msg)
  } finally {
    loading.value = false
  }
}

function openCreateDialog() {
  dialogMode.value = 'create'
  form.value = { product_a: '', product_b: '', cost: 0, time_minutes: 0 }
  dialogVisible.value = true
}

function openEditDialog(fromP: string, toP: string) {
  const cell = matrixData.value[fromP]?.[toP]
  if (!cell || cell.cost === null) return
  dialogMode.value = 'edit'
  form.value = {
    product_a: fromP,
    product_b: toP,
    cost: cell.cost,
    time_minutes: cell.time_minutes ?? 0,
  }
  dialogVisible.value = true
}

async function saveCost() {
  if (!form.value.product_a || !form.value.product_b) {
    ElMessage.warning('Please specify both product types')
    return
  }
  if (form.value.product_a === form.value.product_b) {
    ElMessage.warning('Source and target products must be different')
    return
  }
  dialogLoading.value = true
  try {
    await api.put(`/changeover/${form.value.product_a}/${form.value.product_b}`, {
      cost: form.value.cost,
      time_minutes: form.value.time_minutes,
    })
    ElMessage.success('Changeover cost saved')
    dialogVisible.value = false
    await fetchMatrix()
  } catch (e: unknown) {
    const msg = e instanceof Error ? (e as { response?: { data?: { detail?: string } } }).response?.data?.detail ?? e.message : 'Save failed'
    ElMessage.error(msg)
  } finally {
    dialogLoading.value = false
  }
}

async function deleteCost(fromP: string, toP: string) {
  try {
    await api.delete(`/changeover/${fromP}/${toP}`)
    ElMessage.success(`Removed ${fromP}→${toP}`)
    await fetchMatrix()
  } catch (e: unknown) {
    const msg = e instanceof Error ? e.message : 'Delete failed'
    ElMessage.error(msg)
  }
}

async function runOptimize() {
  const productList = optimizeProducts.value
    .split(/[,\s\n]+/)
    .map((s) => s.trim())
    .filter((s) => s.length > 0)

  if (productList.length < 2) {
    ElMessage.warning('Please enter at least 2 products to optimize')
    return
  }

  optimizeLoading.value = true
  try {
    const payload: Record<string, unknown> = {
      products: productList,
      time_limit: 10.0,
    }
    if (startProduct.value.trim()) {
      payload.start_product = startProduct.value.trim()
    }
    const response = await api.post<OptimizeResult>('/changeover/optimize', payload)
    optimizeResult.value = response.data
    ElMessage.success('Optimization complete')
  } catch (e: unknown) {
    const msg = e instanceof Error ? (e as { response?: { data?: { detail?: string } } }).response?.data?.detail ?? e.message : 'Optimization failed'
    ElMessage.error(msg)
    optimizeResult.value = null
  } finally {
    optimizeLoading.value = false
  }
}

/** Parse optimize products for preview */
const optimizeProductList = computed(() => {
  return optimizeProducts.value
    .split(/[,\s\n]+/)
    .map((s) => s.trim())
    .filter((s) => s.length > 0)
})

// ─── Lifecycle ───────────────────────────────────────────────────────────────

onMounted(() => {
  fetchMatrix()
})
</script>

<template>
  <div class="changeover-dashboard">
    <!-- Header -->
    <div class="dashboard-header">
      <h1 class="text-2xl font-bold mb-2">Product Changeover</h1>
      <p class="text-gray-500 mb-4">
        Manage and optimize product transition costs for flexible production lines.
      </p>
      <ElButton :icon="Refresh" @click="fetchMatrix" :loading="loading">Refresh</ElButton>
    </div>

    <ElAlert v-if="error" :title="error" type="error" :closable="false" class="mb-4" />

    <!-- Matrix Section -->
    <ElCard class="mb-6" shadow="hover">
      <template #header>
        <div class="flex items-center justify-between">
          <span class="text-lg font-semibold">Changeover Cost Matrix</span>
          <ElButton v-if="hasScope('system:write')" type="primary" :icon="Plus" size="small" @click="openCreateDialog">
            Add Transition
          </ElButton>
        </div>
      </template>

      <ElSkeleton v-if="loading" :rows="5" animated />

      <ElEmpty v-else-if="products.length === 0" description="No products registered. Add a transition to get started." />

      <div v-else class="matrix-container">
        <ElTable :data="products.map((p) => ({ product: p }))" border size="small" style="width: 100%">
          <ElTableColumn prop="product" label="From \ To" width="140" fixed />
          <ElTableColumn
            v-for="toP in products"
            :key="toP"
            :label="toP"
            :prop="toP"
            min-width="120"
            align="center"
          >
            <template #default="{ row }">
              <div
                v-if="row.product !== toP"
                class="cell-content"
                :style="getCellColor(matrixData[row.product]?.[toP]?.cost ?? null)"
                @click="hasScope('system:write') && openEditDialog(row.product, toP)"
              >
                <template v-if="matrixData[row.product]?.[toP]?.cost !== null && matrixData[row.product]?.[toP]?.cost !== undefined">
                  <ElTag :type="getCostTagType(matrixData[row.product]?.[toP]?.cost ?? null)" size="small">
                    Cost: {{ matrixData[row.product]?.[toP]?.cost }}
                  </ElTag>
                  <div class="text-xs text-gray-400 mt-1">
                    {{ matrixData[row.product]?.[toP]?.time_minutes ?? 0 }} min
                  </div>
                  <ElButton
                    v-if="hasScope('system:write')"
                    type="danger"
                    :icon="Delete"
                    size="small"
                    circle
                    class="mt-1"
                    @click.stop="deleteCost(row.product, toP)"
                  />
                </template>
                <template v-else>
                  <span class="text-gray-300">—</span>
                </template>
              </div>
              <div v-else class="diagonal-cell">
                <span class="text-gray-300">×</span>
              </div>
            </template>
          </ElTableColumn>
        </ElTable>
      </div>
    </ElCard>

    <!-- Optimization Section -->
    <ElCard shadow="hover">
      <template #header>
        <span class="text-lg font-semibold">Sequence Optimization</span>
      </template>

      <ElForm label-width="160px" class="mb-4">
        <ElFormItem label="Products to Sequence">
          <ElInput
            v-model="optimizeProducts"
            type="textarea"
            :rows="3"
            placeholder="Enter product types separated by commas or new lines (e.g., product_a, product_b, product_c)"
          />
        </ElFormItem>
        <ElFormItem label="Start Product (Optional)">
          <ElInput
            v-model="startProduct"
            placeholder="Force sequence to start with this product"
          />
        </ElFormItem>
        <ElFormItem>
          <ElButton
            type="primary"
            :icon="OptimizeIcon"
            :loading="optimizeLoading"
            @click="runOptimize"
          >
            Optimize Sequence
          </ElButton>
        </ElFormItem>
      </ElForm>

      <!-- Optimize Result -->
      <div v-if="optimizeResult" class="optimize-result">
        <ElAlert type="success" :closable="false" class="mb-4">
          <template #title>
            Optimal sequence: {{ optimizeResult.sequence.join(' → ') }}
          </template>
        </ElAlert>

        <div class="grid grid-cols-2 gap-4 mb-4">
          <ElStatistic title="Total Changeover Cost" :value="optimizeResult.total_cost" />
          <ElStatistic title="Total Changeover Time" :value="optimizeResult.total_time_minutes" suffix="min" />
        </div>

        <h4 class="text-md font-semibold mb-2">Transition Breakdown</h4>
        <ElTimeline>
          <ElTimelineItem
            v-for="(transition, idx) in optimizeResult.transitions"
            :key="idx"
            :timestamp="`Step ${idx + 1}`"
            placement="top"
          >
            <div class="transition-item">
              <ElTag type="info">{{ transition.from_product }}</ElTag>
              <span class="mx-2 text-gray-400">→</span>
              <ElTag type="success">{{ transition.to_product }}</ElTag>
              <div class="mt-2 text-sm">
                <ElTag size="small" :type="getCostTagType(transition.cost)">
                  Cost: {{ transition.cost }}
                </ElTag>
                <ElTag size="small" type="warning" class="ml-2">
                  Time: {{ transition.time_minutes }} min
                </ElTag>
              </div>
            </div>
          </ElTimelineItem>
        </ElTimeline>

        <!-- Estimated vs Actual Progress -->
        <div class="estimated-progress mt-4">
          <h4 class="text-md font-semibold mb-2">Estimated Changeover Progress</h4>
          <div class="progress-bar-container">
            <div
              v-for="(transition, idx) in optimizeResult.transitions"
              :key="`prog-${idx}`"
              class="progress-segment"
              :style="{
                flex: transition.time_minutes || 1,
                backgroundColor: idx % 2 === 0 ? '#409eff' : '#67c23a',
              }"
            >
              <span class="progress-label">
                {{ transition.from_product }}→{{ transition.to_product }}
              </span>
            </div>
          </div>
          <div class="text-xs text-gray-400 mt-1">
            Each segment proportional to transition time. Total: {{ optimizeResult.total_time_minutes }} min
          </div>
        </div>
      </div>
    </ElCard>

    <!-- Add/Edit Dialog -->
    <ElDialog
      v-model="dialogVisible"
      :title="dialogMode === 'create' ? 'Add Changeover Cost' : 'Edit Changeover Cost'"
      width="500px"
    >
      <ElForm :model="form" label-width="140px">
        <ElFormItem label="From Product">
          <ElInput v-model="form.product_a" :disabled="dialogMode === 'edit'" placeholder="e.g., comm_module_v2" />
        </ElFormItem>
        <ElFormItem label="To Product">
          <ElInput v-model="form.product_b" :disabled="dialogMode === 'edit'" placeholder="e.g., sensor_board_v1" />
        </ElFormItem>
        <ElFormItem label="Cost">
          <ElInputNumber v-model="form.cost" :min="0" controls-position="right" />
        </ElFormItem>
        <ElFormItem label="Time (minutes)">
          <ElInputNumber v-model="form.time_minutes" :min="0" controls-position="right" />
        </ElFormItem>
      </ElForm>
      <template #footer>
        <ElButton @click="dialogVisible = false">Cancel</ElButton>
        <ElButton type="primary" :loading="dialogLoading" @click="saveCost">Save</ElButton>
      </template>
    </ElDialog>
  </div>
</template>

<style scoped>
.changeover-dashboard {
  padding: 24px;
  max-width: 1400px;
  margin: 0 auto;
}

.dashboard-header {
  margin-bottom: 24px;
}

.matrix-container {
  overflow-x: auto;
}

.cell-content {
  cursor: pointer;
  padding: 8px 4px;
  border-radius: 4px;
  transition: all 0.2s;
}

.cell-content:hover {
  transform: scale(1.05);
}

.diagonal-cell {
  display: flex;
  align-items: center;
  justify-content: center;
}

.optimize-result {
  margin-top: 16px;
}

.transition-item {
  padding: 8px 0;
}

.progress-bar-container {
  display: flex;
  width: 100%;
  height: 40px;
  border-radius: 6px;
  overflow: hidden;
  background-color: var(--color-bg-secondary);
}

.progress-segment {
  display: flex;
  align-items: center;
  justify-content: center;
  color: white;
  font-size: 11px;
  font-weight: 500;
  text-align: center;
  transition: all 0.3s;
  min-width: 30px;
}

.progress-segment:hover {
  opacity: 0.85;
}

.progress-label {
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  padding: 0 4px;
}
</style>
