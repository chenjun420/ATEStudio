<script setup lang="ts">
/**
 * Calibration Management Panel.
 *
 * Two-section layout for instrument calibration tracking:
 *   1. el-table view - all calibration records with color-coded status
 *      badges (VALID=success, EXPIRING=warning, EXPIRED=danger), inline
 *      record-calibration dialog, and refresh/check-expiry actions.
 *   2. el-calendar view - monthly calendar with dots on next-due dates
 *      color-coded by status.
 *
 * API: /api/v1/calibrations (see frontend/src/api/calibrations.ts).
 * Route: /calibration
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
  ElDatePicker,
  ElMessage,
  ElMessageBox,
  ElCalendar,
  ElEmpty,
  ElSkeleton,
  ElAlert,
} from 'element-plus'
import {
  fetchCalibrations,
  recordCalibration,
  updateCalibration,
  deleteCalibration,
  checkExpiry,
  type CalibrationRecord,
  type CalibrationCreate,
  type CalibrationStatus,
} from '@/api/calibrations'
import { useAuth } from '@/composables/useAuth'

const { hasScope } = useAuth()

// ─── State ───────────────────────────────────────────────────────────────────

const records = ref<CalibrationRecord[]>([])
const loading = ref(false)
const error = ref<string | null>(null)
const statusFilter = ref<CalibrationStatus | ''>('')

// Calendar selected date (defaults to today).
const calendarDate = ref<Date>(new Date())

// Record-calibration dialog state.
const dialogVisible = ref(false)
const dialogMode = ref<'create' | 'edit'>('create')
const dialogLoading = ref(false)
const form = ref<{
  instrument_id: string
  last_calibration: string
  interval_days: number
  notes: string
}>({
  instrument_id: '',
  last_calibration: new Date().toISOString(),
  interval_days: 365,
  notes: '',
})

// ─── Computed ────────────────────────────────────────────────────────────────

/** Records filtered by the selected status (client-side filter). */
const filteredRecords = computed<CalibrationRecord[]>(() => {
  if (statusFilter.value === '') return records.value
  return records.value.filter((r) => r.status === statusFilter.value)
})

/** Summary counts for the header badges. */
const summary = computed(() => {
  const counts = { VALID: 0, EXPIRING: 0, EXPIRED: 0 } as Record<string, number>
  for (const r of records.value) {
    counts[r.status] = (counts[r.status] ?? 0) + 1
  }
  return counts
})

/** Map next_due dates (YYYY-MM-DD) to records for calendar rendering. */
const dueDateMap = computed<Map<string, CalibrationRecord[]>>(() => {
  const m = new Map<string, CalibrationRecord[]>()
  for (const r of records.value) {
    const key = r.next_due.slice(0, 10)
    const arr = m.get(key)
    if (arr) {
      arr.push(r)
    } else {
      m.set(key, [r])
    }
  }
  return m
})

// ─── Status -> tag type mapping ──────────────────────────────────────────────

function statusTagType(
  status: CalibrationStatus,
): 'success' | 'warning' | 'danger' | 'info' {
  switch (status) {
    case 'VALID':
      return 'success'
    case 'EXPIRING':
      return 'warning'
    case 'EXPIRED':
      return 'danger'
    default:
      return 'info'
  }
}

function statusLabel(status: CalibrationStatus): string {
  const labels: Record<CalibrationStatus, string> = {
    VALID: 'Valid',
    EXPIRING: 'Expiring',
    EXPIRED: 'Expired',
    UNKNOWN: 'Unknown',
  }
  return labels[status] ?? status
}

// ─── Date formatting helpers ─────────────────────────────────────────────────

function formatDate(iso: string): string {
  if (!iso) return '-'
  const d = new Date(iso)
  return d.toLocaleString(undefined, {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  })
}

function dateKey(d: Date): string {
  const y = d.getFullYear()
  const m = String(d.getMonth() + 1).padStart(2, '0')
  const day = String(d.getDate()).padStart(2, '0')
  return `${y}-${m}-${day}`
}

// ─── Data fetching ───────────────────────────────────────────────────────────

async function loadRecords(): Promise<void> {
  loading.value = true
  error.value = null
  try {
    records.value = await fetchCalibrations()
  } catch (e: unknown) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    loading.value = false
  }
}

async function handleCheckExpiry(): Promise<void> {
  try {
    const result = await checkExpiry()
    ElMessage.success(`Refreshed statuses; ${result.updated} record(s) updated.`)
    await loadRecords()
  } catch (e: unknown) {
    ElMessage.error(e instanceof Error ? e.message : 'Failed to refresh statuses')
  }
}

// ─── Dialog (create / edit) ──────────────────────────────────────────────────

function openCreateDialog(): void {
  dialogMode.value = 'create'
  form.value = {
    instrument_id: '',
    last_calibration: new Date().toISOString(),
    interval_days: 365,
    notes: '',
  }
  dialogVisible.value = true
}

function openEditDialog(record: CalibrationRecord): void {
  dialogMode.value = 'edit'
  form.value = {
    instrument_id: record.instrument_id,
    last_calibration: record.last_calibration,
    interval_days: record.interval_days,
    notes: record.notes ?? '',
  }
  // Stash the original instrument_id for the PUT path.
  editTargetId.value = record.instrument_id
  dialogVisible.value = true
}

const editTargetId = ref<string>('')

async function submitForm(): Promise<void> {
  if (!form.value.instrument_id.trim()) {
    ElMessage.warning('Instrument ID is required')
    return
  }
  dialogLoading.value = true
  try {
    if (dialogMode.value === 'create') {
      const payload: CalibrationCreate = {
        instrument_id: form.value.instrument_id.trim(),
        last_calibration: form.value.last_calibration,
        interval_days: form.value.interval_days,
        notes: form.value.notes.trim() || undefined,
      }
      await recordCalibration(payload)
      ElMessage.success('Calibration recorded')
    } else {
      await updateCalibration(editTargetId.value, {
        instrument_id: form.value.instrument_id.trim(),
        last_calibration: form.value.last_calibration,
        interval_days: form.value.interval_days,
        notes: form.value.notes.trim() || undefined,
      })
      ElMessage.success('Calibration updated')
    }
    dialogVisible.value = false
    await loadRecords()
  } catch (e: unknown) {
    ElMessage.error(e instanceof Error ? e.message : 'Failed to save calibration')
  } finally {
    dialogLoading.value = false
  }
}

// ─── Delete ──────────────────────────────────────────────────────────────────

async function handleDelete(record: CalibrationRecord): Promise<void> {
  try {
    await ElMessageBox.confirm(
      `Delete all calibration records for instrument '${record.instrument_id}'?`,
      'Confirm Delete',
      { type: 'warning', confirmButtonText: 'Delete', cancelButtonText: 'Cancel' },
    )
  } catch {
    return // user cancelled
  }
  try {
    await deleteCalibration(record.instrument_id)
    ElMessage.success('Deleted')
    await loadRecords()
  } catch (e: unknown) {
    ElMessage.error(e instanceof Error ? e.message : 'Failed to delete')
  }
}

// ─── Calendar slot data ──────────────────────────────────────────────────────

function calendarCellDots(date: Date): CalibrationRecord[] {
  return dueDateMap.value.get(dateKey(date)) ?? []
}

// ─── Mount ───────────────────────────────────────────────────────────────────

onMounted(() => {
  void loadRecords()
})
</script>

<template>
  <div class="calibration-panel">
    <!-- ─── Header ─── -->
    <header class="cp-header">
      <div class="cp-header-left">
        <h1 class="cp-title">Instrument Calibration</h1>
        <!-- Summary badges -->
        <ElTag type="success" size="small" data-testid="summary-valid">
          Valid: {{ summary.VALID }}
        </ElTag>
        <ElTag type="warning" size="small" data-testid="summary-expiring">
          Expiring: {{ summary.EXPIRING }}
        </ElTag>
        <ElTag type="danger" size="small" data-testid="summary-expired">
          Expired: {{ summary.EXPIRED }}
        </ElTag>
      </div>
      <div class="cp-header-right">
        <ElButton size="small" @click="handleCheckExpiry" data-testid="btn-check-expiry">
          Refresh Statuses
        </ElButton>
        <ElButton size="small" @click="loadRecords" data-testid="btn-reload">Reload</ElButton>
        <ElButton v-if="hasScope('system:write')" type="primary" size="small" @click="openCreateDialog" data-testid="btn-create">
          Record Calibration
        </ElButton>
      </div>
    </header>

    <!-- ─── Error banner ─── -->
    <ElAlert
      v-if="error"
      data-testid="error-alert"
      title="Failed to load calibration records"
      :description="error"
      type="error"
      :closable="false"
      show-icon
    />

    <!-- ─── Table view ─── -->
    <ElCard class="cp-table-card" shadow="never" data-testid="table-card">
      <template #header>
        <div class="cp-card-header">
          <span class="cp-card-title">Calibration Records ({{ filteredRecords.length }})</span>
          <div class="cp-filter">
            <span class="cp-filter-label">Filter:</span>
            <ElButton
              :type="statusFilter === '' ? 'primary' : 'default'"
              size="small"
              @click="statusFilter = ''"
            >
              All
            </ElButton>
            <ElButton
              :type="statusFilter === 'VALID' ? 'success' : 'default'"
              size="small"
              @click="statusFilter = 'VALID'"
            >
              Valid
            </ElButton>
            <ElButton
              :type="statusFilter === 'EXPIRING' ? 'warning' : 'default'"
              size="small"
              @click="statusFilter = 'EXPIRING'"
            >
              Expiring
            </ElButton>
            <ElButton
              :type="statusFilter === 'EXPIRED' ? 'danger' : 'default'"
              size="small"
              @click="statusFilter = 'EXPIRED'"
            >
              Expired
            </ElButton>
          </div>
        </div>
      </template>

      <ElSkeleton v-if="loading" :rows="4" animated data-testid="table-skeleton" />
      <ElEmpty
        v-else-if="filteredRecords.length === 0"
        data-testid="empty-records"
        description="No calibration records. Click 'Record Calibration' to add one."
      />
      <ElTable
        v-else
        :data="filteredRecords"
        stripe
        style="width: 100%"
        data-testid="calibration-table"
      >
        <ElTableColumn prop="instrument_id" label="Instrument ID" min-width="180" />
        <ElTableColumn label="Status" width="120">
          <template #default="{ row }">
            <ElTag :type="statusTagType(row.status)" size="small">
              {{ statusLabel(row.status as CalibrationStatus) }}
            </ElTag>
          </template>
        </ElTableColumn>
        <ElTableColumn label="Last Calibration" width="180">
          <template #default="{ row }">{{ formatDate(row.last_calibration) }}</template>
        </ElTableColumn>
        <ElTableColumn prop="interval_days" label="Interval (days)" width="130" />
        <ElTableColumn label="Next Due" width="180">
          <template #default="{ row }">{{ formatDate(row.next_due) }}</template>
        </ElTableColumn>
        <ElTableColumn prop="notes" label="Notes" min-width="160" show-overflow-tooltip />
        <ElTableColumn label="Actions" width="160" fixed="right">
          <template #default="{ row }">
            <ElButton v-if="hasScope('system:write')" size="small" link @click="openEditDialog(row as CalibrationRecord)">Edit</ElButton>
            <ElButton v-if="hasScope('system:write')" size="small" link type="danger" @click="handleDelete(row as CalibrationRecord)">Delete</ElButton>
          </template>
        </ElTableColumn>
      </ElTable>
    </ElCard>

    <!-- ─── Calendar view ─── -->
    <ElCard class="cp-calendar-card" shadow="never" data-testid="calendar-card">
      <template #header>
        <span class="cp-card-title">Calibration Due Calendar</span>
      </template>
      <ElCalendar v-model="calendarDate" data-testid="due-calendar">
        <template #date-cell="{ data }">
          <div class="cp-cal-cell" :class="{ 'cp-cal-cell-selected': data.isSelected }">
            <span class="cp-cal-day">{{ data.day.split('-').slice(-1)[0] }}</span>
            <div class="cp-cal-dots">
              <span
                v-for="rec in calendarCellDots(new Date(data.day))"
                :key="rec.id"
                class="cp-cal-dot"
                :class="`cp-cal-dot-${rec.status.toLowerCase()}`"
                :title="`${rec.instrument_id} (${rec.status})`"
              ></span>
            </div>
          </div>
        </template>
      </ElCalendar>
    </ElCard>

    <!-- ─── Record / Edit dialog ─── -->
    <ElDialog
      v-model="dialogVisible"
      :title="dialogMode === 'create' ? 'Record Calibration' : 'Edit Calibration'"
      width="500px"
      data-testid="calibration-dialog"
    >
      <ElForm label-width="140px" :disabled="dialogLoading">
        <ElFormItem label="Instrument ID" required>
          <ElInput v-model="form.instrument_id" placeholder="e.g., oscilloscope-1" />
        </ElFormItem>
        <ElFormItem label="Last Calibration">
          <ElDatePicker
            v-model="form.last_calibration"
            type="datetime"
            placeholder="Select date/time"
            style="width: 100%"
          />
        </ElFormItem>
        <ElFormItem label="Interval (days)" required>
          <ElInputNumber v-model="form.interval_days" :min="1" :max="36500" />
        </ElFormItem>
        <ElFormItem label="Notes">
          <ElInput v-model="form.notes" type="textarea" :rows="3" placeholder="Optional notes" />
        </ElFormItem>
      </ElForm>
      <template #footer>
        <ElButton @click="dialogVisible = false" :disabled="dialogLoading">Cancel</ElButton>
        <ElButton type="primary" :loading="dialogLoading" @click="submitForm">Save</ElButton>
      </template>
    </ElDialog>
  </div>
</template>

<style scoped>
.calibration-panel {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-md);
  padding: var(--spacing-md) var(--spacing-lg);
  min-height: 100vh;
  background-color: var(--color-bg-secondary);
}

/* ─── Header ─── */
.cp-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: var(--spacing-sm);
}

.cp-header-left {
  display: flex;
  align-items: center;
  gap: var(--spacing-sm);
  flex-wrap: wrap;
}

.cp-header-right {
  display: flex;
  align-items: center;
  gap: var(--spacing-xs);
}

.cp-title {
  font-size: 1.25rem;
  font-weight: 600;
  color: var(--color-text-primary);
  margin: 0;
}

/* ─── Cards ─── */
.cp-table-card,
.cp-calendar-card {
  background-color: var(--color-bg-primary);
  border: 1px solid var(--color-border-default);
  border-radius: var(--radius-xl);
}

.cp-card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--spacing-sm);
  flex-wrap: wrap;
}

.cp-card-title {
  font-size: 0.9375rem;
  font-weight: 600;
  color: var(--color-text-primary);
}

.cp-filter {
  display: flex;
  align-items: center;
  gap: var(--spacing-xs);
}

.cp-filter-label {
  font-size: 0.8125rem;
  color: var(--color-text-secondary);
}

/* ─── Calendar cell ─── */
.cp-cal-cell {
  display: flex;
  flex-direction: column;
  align-items: center;
  height: 100%;
  padding: 2px;
}

.cp-cal-cell-selected {
  background-color: var(--color-border-accent);
  border-radius: var(--radius-md);
}

.cp-cal-day {
  font-size: 0.8125rem;
  color: var(--color-text-primary);
}

.cp-cal-dots {
  display: flex;
  flex-wrap: wrap;
  gap: 2px;
  margin-top: 2px;
  justify-content: center;
  max-width: 100%;
}

.cp-cal-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  display: inline-block;
}

.cp-cal-dot-valid {
  background-color: var(--color-success);
}

.cp-cal-dot-expiring {
  background-color: var(--color-warning);
}

.cp-cal-dot-expired {
  background-color: var(--color-error);
}

/* ─── Responsive ─── */
@media (max-width: 768px) {
  .calibration-panel {
    padding: var(--spacing-sm);
  }
}
</style>
