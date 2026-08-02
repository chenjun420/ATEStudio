<script setup lang="ts">
/**
 * MeasurementExplorer — SPC measurement exploration view.
 *
 * Provides:
 *   - Product type dropdown selector
 *   - Measurement name dropdown selector
 *   - Time range picker (shortcuts: today, 7 days, 30 days)
 *   - SPC charts integration (X-bar, R, Cpk gauge)
 *   - SPC alerts table
 *
 * Data flows from useSPC composable which fetches statistics, chart, and
 * alerts endpoints in parallel and auto-refreshes every 30s.
 */
import { computed, ref, watch } from 'vue'
import {
  ElCard,
  ElSelect,
  ElOption,
  ElDatePicker,
  ElButton,
  ElTable,
  ElTableColumn,
  ElTag,
  ElAlert,
  ElSkeleton,
  ElEmpty,
} from 'element-plus'
import SPCCharts from '@/components/SPCCharts.vue'
import { useSPC } from '@/composables/useSPC'
import type { SPCAlert } from '@/api/spc'

// ─── Composable state ────────────────────────────────────────────────────────

const {
  statistics,
  chart,
  alerts,
  loading,
  error,
  productType,
  measurementName,
  refresh,
} = useSPC()

// ─── Product type & measurement options ──────────────────────────────────────

const productOptions = [
  { label: '5G Base Station Board', value: '5g_bsb' },
  { label: 'Server Motherboard', value: 'srv_mb' },
  { label: 'Consumer IoT Module', value: 'iot_mod' },
]

const measurementOptions = computed(() => {
  // Common measurement names per product type
  const common = [
    { label: 'Voltage (V)', value: 'voltage' },
    { label: 'Current (A)', value: 'current' },
    { label: 'Frequency (Hz)', value: 'frequency' },
    { label: 'Temperature (°C)', value: 'temperature' },
    { label: 'Resistance (Ω)', value: 'resistance' },
  ]
  return common
})

// ─── Time range ──────────────────────────────────────────────────────────────

const dateRange = ref<[Date, Date] | null>(null)

const datePickerShortcuts = [
  {
    text: 'Today',
    value: () => {
      const now = new Date()
      const start = new Date(now.getFullYear(), now.getMonth(), now.getDate())
      return [start, now] as [Date, Date]
    },
  },
  {
    text: 'Last 7 days',
    value: () => {
      const now = new Date()
      const start = new Date(now)
      start.setDate(start.getDate() - 7)
      return [start, now] as [Date, Date]
    },
  },
  {
    text: 'Last 30 days',
    value: () => {
      const now = new Date()
      const start = new Date(now)
      start.setDate(start.getDate() - 30)
      return [start, now] as [Date, Date]
    },
  },
]

// ─── Alert severity tag ──────────────────────────────────────────────────────

function severityTagType(severity: string): 'danger' | 'warning' | 'info' {
  switch (severity) {
    case 'critical': return 'danger'
    case 'warning': return 'warning'
    default: return 'info'
  }
}

// ─── Alert pagination ────────────────────────────────────────────────────────

const currentPage = ref(1)
const pageSize = ref(10)
const pagedAlerts = computed<SPCAlert[]>(() => {
  const start = (currentPage.value - 1) * pageSize.value
  return alerts.value.slice(start, start + pageSize.value)
})

// ─── Selection handlers ──────────────────────────────────────────────────────

async function onProductChange(val: string): Promise<void> {
  productType.value = val
  if (val && measurementName.value) {
    await refresh()
  }
}

async function onMeasurementChange(val: string): Promise<void> {
  measurementName.value = val
  if (val && productType.value) {
    await refresh()
  }
}

async function onDateChange(): Promise<void> {
  if (productType.value && measurementName.value) {
    await refresh()
  }
}

// ─── Computed: has selection ─────────────────────────────────────────────────

const hasSelection = computed(() => !!productType.value && !!measurementName.value)

// ─── Watch: reset page when alerts change ────────────────────────────────────

watch(alerts, () => {
  currentPage.value = 1
})
</script>

<template>
  <div class="measurement-explorer" data-testid="measurement-explorer">
    <!-- ─── Header ─── -->
    <header class="me-header">
      <div class="me-header-left">
        <h1 class="me-title">Measurement Explorer</h1>
        <span class="me-subtitle">SPC Control Charts & Process Capability</span>
      </div>
      <div class="me-header-right">
        <ElButton
          size="small"
          :loading="loading"
          :disabled="!hasSelection"
          @click="refresh"
          data-testid="btn-refresh"
        >
          Refresh
        </ElButton>
      </div>
    </header>

    <!-- ─── Filters ─── -->
    <ElCard class="me-filter-card" shadow="never" data-testid="card-filters">
      <div class="me-filters">
        <div class="me-filter-item">
          <label class="me-filter-label" for="select-product">Product Type</label>
          <ElSelect
            id="select-product"
            v-model="productType"
            placeholder="Select product type"
            class="me-filter-select"
            data-testid="select-product"
            @change="onProductChange"
          >
            <ElOption
              v-for="opt in productOptions"
              :key="opt.value"
              :label="opt.label"
              :value="opt.value"
            />
          </ElSelect>
        </div>

        <div class="me-filter-item">
          <label class="me-filter-label" for="select-measurement">Measurement</label>
          <ElSelect
            id="select-measurement"
            v-model="measurementName"
            placeholder="Select measurement"
            class="me-filter-select"
            data-testid="select-measurement"
            :disabled="!productType"
            @change="onMeasurementChange"
          >
            <ElOption
              v-for="opt in measurementOptions"
              :key="opt.value"
              :label="opt.label"
              :value="opt.value"
            />
          </ElSelect>
        </div>

        <div class="me-filter-item">
          <label class="me-filter-label">Time Range</label>
          <ElDatePicker
            v-model="dateRange"
            type="datetimerange"
            range-separator="—"
            start-placeholder="Start"
            end-placeholder="End"
            :shortcuts="datePickerShortcuts"
            class="me-filter-datepicker"
            data-testid="datepicker-range"
            @change="onDateChange"
          />
        </div>
      </div>
    </ElCard>

    <!-- ─── Error ─── -->
    <ElAlert
      v-if="error"
      data-testid="error-alert"
      title="Failed to load SPC data"
      :description="error"
      type="error"
      :closable="false"
      show-icon
    />

    <!-- ─── No selection prompt ─── -->
    <ElCard v-if="!hasSelection" class="me-prompt-card" shadow="never" data-testid="card-no-selection">
      <ElEmpty description="Select a product type and measurement to view SPC charts" :image-size="80" />
    </ElCard>

    <!-- ─── Loading skeleton ─── -->
    <div v-if="loading && !statistics" data-testid="me-loading" class="me-loading">
      <ElSkeleton :rows="8" animated />
    </div>

    <!-- ─── SPC Charts ─── -->
    <SPCCharts
      v-if="hasSelection"
      :chart="chart"
      :statistics="statistics"
      :loading="loading"
    />

    <!-- ─── Alerts table ─── -->
    <ElCard
      v-if="hasSelection"
      class="me-alerts-card"
      shadow="never"
      data-testid="card-alerts"
    >
      <template #header>
        <div class="me-alerts-header">
          <span class="me-chart-title">SPC Alerts</span>
          <ElTag v-if="alerts.length > 0" type="warning" size="small" data-testid="alert-count">
            {{ alerts.length }} alert(s)
          </ElTag>
        </div>
      </template>
      <ElEmpty
        v-if="alerts.length === 0"
        description="No alerts — process is in control"
        :image-size="40"
        data-testid="empty-alerts"
      />
      <ElTable
        v-else
        :data="pagedAlerts"
        stripe
        class="me-alerts-table"
        data-testid="alerts-table"
      >
        <ElTableColumn prop="timestamp" label="Time" width="200" data-testid="col-timestamp">
          <template #default="{ row }">
            {{ new Date(row.timestamp).toLocaleString() }}
          </template>
        </ElTableColumn>
        <ElTableColumn prop="product_type" label="Product" width="120" />
        <ElTableColumn prop="measurement_name" label="Measurement" width="120" />
        <ElTableColumn prop="rule" label="Rule" min-width="180" />
        <ElTableColumn prop="severity" label="Severity" width="100" data-testid="col-severity">
          <template #default="{ row }">
            <ElTag :type="severityTagType(row.severity)" size="small">
              {{ row.severity }}
            </ElTag>
          </template>
        </ElTableColumn>
        <ElTableColumn prop="value" label="Value" width="100">
          <template #default="{ row }">
            {{ row.value != null ? row.value.toFixed(4) : '-' }}
          </template>
        </ElTableColumn>
        <ElTableColumn prop="sample_count" label="Samples" width="80" />
        <ElTableColumn prop="message" label="Message" min-width="200" show-overflow-tooltip />
      </ElTable>
    </ElCard>
  </div>
</template>

<style scoped>
.measurement-explorer {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-md);
  padding: var(--spacing-md) var(--spacing-lg);
  min-height: 100vh;
  background-color: var(--color-bg-secondary);
}

/* ─── Header ─── */
.me-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: var(--spacing-sm);
}

.me-header-left {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.me-title {
  font-size: 1.25rem;
  font-weight: 600;
  color: var(--color-text-primary);
  margin: 0;
}

.me-subtitle {
  font-size: 0.8125rem;
  color: var(--color-text-secondary);
}

.me-header-right {
  display: flex;
  align-items: center;
  gap: var(--spacing-xs);
}

/* ─── Filter card ─── */
.me-filter-card {
  background-color: var(--color-bg-primary);
  border: 1px solid var(--color-border-default);
  border-radius: var(--radius-xl);
  margin-bottom: 0;
}

.me-filters {
  display: flex;
  flex-wrap: wrap;
  gap: var(--spacing-md);
  align-items: flex-end;
}

.me-filter-item {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-xs);
  flex: 1;
  min-width: 200px;
}

.me-filter-label {
  font-size: 0.75rem;
  font-weight: 600;
  color: var(--color-text-secondary);
  text-transform: uppercase;
  letter-spacing: 0.04em;
}

.me-filter-select {
  width: 100%;
}

.me-filter-datepicker {
  width: 100%;
}

/* ─── Prompt / empty ─── */
.me-prompt-card {
  background-color: var(--color-bg-primary);
  border: 1px solid var(--color-border-default);
  border-radius: var(--radius-xl);
}

.me-loading {
  background-color: var(--color-bg-primary);
  border: 1px solid var(--color-border-default);
  border-radius: var(--radius-xl);
  padding: var(--spacing-lg);
}

/* ─── Alerts ─── */
.me-alerts-card {
  background-color: var(--color-bg-primary);
  border: 1px solid var(--color-border-default);
  border-radius: var(--radius-xl);
  margin-bottom: 0;
}

.me-alerts-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--spacing-sm);
}

.me-chart-title {
  font-size: 0.9375rem;
  font-weight: 600;
  color: var(--color-text-primary);
}

.me-alerts-table {
  width: 100%;
}

/* ─── Responsive ─── */
@media (max-width: 768px) {
  .measurement-explorer {
    padding: var(--spacing-sm);
  }

  .me-filters {
    flex-direction: column;
  }

  .me-filter-item {
    min-width: 100%;
  }
}
</style>
