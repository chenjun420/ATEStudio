<script setup lang="ts">
/**
 * FMEA Management view.
 *
 * Browse and edit Failure Mode and Effects Analysis records against the
 * task-13 backend API (GET/POST/PUT/DELETE /api/v1/fmea). Each row carries
 * severity (S), occurrence (O) and detection (D) ratings (integers 1-10); the
 * Risk Priority Number RPN = S*O*D is computed SERVER-SIDE and is
 * authoritative. The client mirrors the computation for live preview while
 * editing and for high-RPN row highlighting (RPN >= 100 danger, >= 60 warning).
 *
 * Write actions (create/edit/save/delete) are gated behind the `system:write`
 * scope, following CalibrationPanel. Out-of-range ratings are blocked
 * client-side; a server 422 (e.g. race on validation) surfaces as an inline
 * error in the dialog.
 *
 * API: /api/v1/fmea (see frontend/src/api/fmea.ts).
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
  ElMessage,
  ElMessageBox,
  ElEmpty,
  ElSkeleton,
  ElAlert,
} from 'element-plus'
import {
  fetchFmeas,
  createFmea,
  updateFmea,
  deleteFmea,
  computeRpn,
  isValidRating,
  rpnBand,
  RPN_DANGER_THRESHOLD,
  RPN_WARNING_THRESHOLD,
  type FmeaRecord,
  type FmeaCreate,
  type FmeaUpdate,
  type RpnBand,
} from '@/api/fmea'
import { useAuth } from '@/composables/useAuth'

const { hasScope } = useAuth()
const canWrite = computed(() => hasScope('system:write'))

// ─── State ───────────────────────────────────────────────────────────────────

const records = ref<FmeaRecord[]>([])
const total = ref(0)
const loading = ref(false)
const error = ref<string | null>(null)

// Filters (applied via the query params on reload).
const componentFilter = ref('')
const faultFilter = ref('')

// Create / edit dialog state.
const dialogVisible = ref(false)
const dialogMode = ref<'create' | 'edit'>('create')
const dialogLoading = ref(false)
const editTargetId = ref<string>('')
const formError = ref<string | null>(null)

const form = ref<{
  component_code: string
  function_name: string
  fault_code: string
  failure_mode: string
  effects: string
  cause: string
  severity: number | null
  occurrence: number | null
  detection: number | null
  recommended_action: string
}>({
  component_code: '',
  function_name: '',
  fault_code: '',
  failure_mode: '',
  effects: '',
  cause: '',
  severity: null,
  occurrence: null,
  detection: null,
  recommended_action: '',
})

// ─── RPN helpers (client-side display; server is authoritative) ──────────────

/** Live RPN preview while editing — null until all three ratings are valid. */
const previewRpn = computed<number | null>(() => {
  const { severity, occurrence, detection } = form.value
  if (!isValidRating(severity) || !isValidRating(occurrence) || !isValidRating(detection)) {
    return null
  }
  return computeRpn(severity as number, occurrence as number, detection as number)
})

/** Per-field validation messages for the three ratings. */
const ratingErrors = computed(() => ({
  severity: isValidRating(form.value.severity) ? '' : 'Severity must be an integer from 1 to 10',
  occurrence: isValidRating(form.value.occurrence) ? '' : 'Occurrence must be an integer from 1 to 10',
  detection: isValidRating(form.value.detection) ? '' : 'Detection must be an integer from 1 to 10',
}))

function bandTagType(rpn: number): 'danger' | 'warning' | 'success' {
  const band = rpnBand(rpn)
  if (band === 'danger') return 'danger'
  if (band === 'warning') return 'warning'
  return 'success'
}

/** el-table row class for high-RPN highlighting. */
function tableRowClass({ row }: { row: FmeaRecord }): string {
  const band: RpnBand = rpnBand(row.rpn)
  return band === 'normal' ? '' : `fmea-row-${band}`
}

// ─── Data fetching ───────────────────────────────────────────────────────────

async function loadRecords(): Promise<void> {
  loading.value = true
  error.value = null
  try {
    const res = await fetchFmeas({
      component_code: componentFilter.value.trim() || undefined,
      fault_code: faultFilter.value.trim() || undefined,
      limit: 1000,
    })
    records.value = res.items
    total.value = res.total
  } catch (e: unknown) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    loading.value = false
  }
}

function applyFilters(): void {
  void loadRecords()
}

function resetFilters(): void {
  componentFilter.value = ''
  faultFilter.value = ''
  void loadRecords()
}

// ─── Dialog (create / edit) ──────────────────────────────────────────────────

function openCreateDialog(): void {
  dialogMode.value = 'create'
  editTargetId.value = ''
  formError.value = null
  form.value = {
    component_code: componentFilter.value.trim(),
    function_name: '',
    fault_code: faultFilter.value.trim(),
    failure_mode: '',
    effects: '',
    cause: '',
    severity: 5,
    occurrence: 5,
    detection: 5,
    recommended_action: '',
  }
  dialogVisible.value = true
}

function openEditDialog(row: FmeaRecord): void {
  dialogMode.value = 'edit'
  editTargetId.value = row.id
  formError.value = null
  form.value = {
    component_code: row.component_code,
    function_name: row.function_name ?? '',
    fault_code: row.fault_code ?? '',
    failure_mode: row.failure_mode,
    effects: row.effects ?? '',
    cause: row.cause ?? '',
    severity: row.severity,
    occurrence: row.occurrence,
    detection: row.detection,
    recommended_action: row.recommended_action ?? '',
  }
  dialogVisible.value = true
}

function validateForm(): string | null {
  if (!form.value.component_code.trim()) return 'Component code is required'
  if (!form.value.failure_mode.trim()) return 'Failure mode is required'
  if (!isValidRating(form.value.severity)) return 'Severity must be an integer from 1 to 10'
  if (!isValidRating(form.value.occurrence)) return 'Occurrence must be an integer from 1 to 10'
  if (!isValidRating(form.value.detection)) return 'Detection must be an integer from 1 to 10'
  return null
}

/** Extract a human-readable message from an axios error (FastAPI 422 detail). */
function extractError(e: unknown, fallback: string): string {
  const detail = (e as { response?: { data?: { detail?: unknown } } | undefined } | null)?.response
    ?.data?.detail
  if (typeof detail === 'string' && detail) return detail
  if (Array.isArray(detail) && detail.length > 0) {
    const first = detail[0] as { msg?: string } | undefined
    if (first?.msg) return first.msg
  }
  if (e instanceof Error && e.message) return e.message
  return fallback
}

async function submitForm(): Promise<void> {
  formError.value = null

  // Client-side guard: out-of-range ratings never leave the browser.
  const validationError = validateForm()
  if (validationError) {
    formError.value = validationError
    return
  }

  dialogLoading.value = true
  try {
    if (dialogMode.value === 'create') {
      // NOTE: rpn is intentionally NOT sent — the server computes S*O*D.
      const payload: FmeaCreate = {
        component_code: form.value.component_code.trim(),
        function_name: form.value.function_name.trim() || null,
        fault_code: form.value.fault_code.trim() || null,
        failure_mode: form.value.failure_mode.trim(),
        effects: form.value.effects.trim() || null,
        cause: form.value.cause.trim() || null,
        severity: form.value.severity as number,
        occurrence: form.value.occurrence as number,
        detection: form.value.detection as number,
        recommended_action: form.value.recommended_action.trim() || null,
      }
      await createFmea(payload)
      ElMessage.success('FMEA entry created')
    } else {
      const payload: FmeaUpdate = {
        component_code: form.value.component_code.trim(),
        function_name: form.value.function_name.trim() || null,
        fault_code: form.value.fault_code.trim() || null,
        failure_mode: form.value.failure_mode.trim(),
        effects: form.value.effects.trim() || null,
        cause: form.value.cause.trim() || null,
        severity: form.value.severity as number,
        occurrence: form.value.occurrence as number,
        detection: form.value.detection as number,
        recommended_action: form.value.recommended_action.trim() || null,
      }
      await updateFmea(editTargetId.value, payload)
      ElMessage.success('FMEA entry updated')
    }
    dialogVisible.value = false
    await loadRecords()
  } catch (e: unknown) {
    // Server 422 / validation rejection surfaces inline in the dialog.
    formError.value = extractError(e, 'Failed to save FMEA entry')
  } finally {
    dialogLoading.value = false
  }
}

// ─── Delete ──────────────────────────────────────────────────────────────────

async function handleDelete(row: FmeaRecord): Promise<void> {
  try {
    await ElMessageBox.confirm(
      `Delete FMEA entry for '${row.component_code}' / '${row.failure_mode}'?`,
      'Confirm Delete',
      { type: 'warning', confirmButtonText: 'Delete', cancelButtonText: 'Cancel' },
    )
  } catch {
    return // user cancelled
  }
  try {
    await deleteFmea(row.id)
    ElMessage.success('Deleted')
    await loadRecords()
  } catch (e: unknown) {
    ElMessage.error(extractError(e, 'Failed to delete FMEA entry'))
  }
}

// ─── Mount ───────────────────────────────────────────────────────────────────

onMounted(() => {
  void loadRecords()
})
</script>

<template>
  <div class="fmea-panel">
    <!-- ─── Header ─── -->
    <header class="fp-header">
      <div class="fp-header-left">
        <h1 class="fp-title">FMEA Management</h1>
        <ElTag type="danger" size="small" data-testid="count-danger">
          RPN ≥ {{ RPN_DANGER_THRESHOLD }}:
          {{ records.filter((r) => r.rpn >= RPN_DANGER_THRESHOLD).length }}
        </ElTag>
        <ElTag type="warning" size="small" data-testid="count-warning">
          RPN ≥ {{ RPN_WARNING_THRESHOLD }}:
          {{ records.filter((r) => r.rpn >= RPN_WARNING_THRESHOLD && r.rpn < RPN_DANGER_THRESHOLD).length }}
        </ElTag>
        <ElTag type="info" size="small" data-testid="count-total">
          Total: {{ total }}
        </ElTag>
      </div>
      <div class="fp-header-right">
        <ElButton size="small" @click="loadRecords" data-testid="btn-reload">Reload</ElButton>
        <ElButton
          v-if="canWrite"
          type="primary"
          size="small"
          @click="openCreateDialog"
          data-testid="btn-create"
        >
          New FMEA
        </ElButton>
      </div>
    </header>

    <!-- ─── Filters ─── -->
    <ElCard class="fp-filter-card" shadow="never">
      <div class="fp-filter-row">
        <ElInput
          v-model="componentFilter"
          placeholder="Filter by component code"
          clearable
          size="small"
          class="fp-filter-input"
          data-testid="filter-component"
          @keyup.enter="applyFilters"
        />
        <ElInput
          v-model="faultFilter"
          placeholder="Filter by fault code"
          clearable
          size="small"
          class="fp-filter-input"
          data-testid="filter-fault"
          @keyup.enter="applyFilters"
        />
        <ElButton size="small" type="primary" @click="applyFilters" data-testid="btn-filter">
          Search
        </ElButton>
        <ElButton size="small" @click="resetFilters" data-testid="btn-filter-reset">
          Reset
        </ElButton>
      </div>
    </ElCard>

    <!-- ─── Error banner ─── -->
    <ElAlert
      v-if="error"
      data-testid="error-alert"
      title="Failed to load FMEA records"
      :description="error"
      type="error"
      :closable="false"
      show-icon
    />

    <!-- ─── Table ─── -->
    <ElCard class="fp-table-card" shadow="never" data-testid="table-card">
      <ElSkeleton v-if="loading" :rows="5" animated data-testid="table-skeleton" />
      <ElEmpty
        v-else-if="records.length === 0"
        data-testid="empty-records"
        description="No FMEA records. Click 'New FMEA' to add one."
      />
      <ElTable
        v-else
        :data="records"
        stripe
        border
        style="width: 100%"
        :row-class-name="tableRowClass"
        data-testid="fmea-table"
      >
        <ElTableColumn prop="component_code" label="Component" min-width="140" />
        <ElTableColumn prop="function_name" label="Function" min-width="140" show-overflow-tooltip>
          <template #default="{ row }">{{ row.function_name || '—' }}</template>
        </ElTableColumn>
        <ElTableColumn prop="failure_mode" label="Failure Mode" min-width="180" show-overflow-tooltip />
        <ElTableColumn prop="effects" label="Effects" min-width="160" show-overflow-tooltip>
          <template #default="{ row }">{{ row.effects || '—' }}</template>
        </ElTableColumn>
        <ElTableColumn prop="cause" label="Cause" min-width="160" show-overflow-tooltip>
          <template #default="{ row }">{{ row.cause || '—' }}</template>
        </ElTableColumn>
        <ElTableColumn prop="fault_code" label="Fault Code" min-width="110">
          <template #default="{ row }">{{ row.fault_code || '—' }}</template>
        </ElTableColumn>
        <ElTableColumn prop="severity" label="S" width="64" align="center" />
        <ElTableColumn prop="occurrence" label="O" width="64" align="center" />
        <ElTableColumn prop="detection" label="D" width="64" align="center" />
        <ElTableColumn label="RPN" width="90" align="center" fixed="right">
          <template #default="{ row }">
            <ElTag :type="bandTagType(row.rpn)" size="small" data-testid="rpn-tag">
              {{ row.rpn }}
            </ElTag>
          </template>
        </ElTableColumn>
        <ElTableColumn label="Actions" width="140" fixed="right">
          <template #default="{ row }">
            <ElButton
              v-if="canWrite"
              size="small"
              link
              @click="openEditDialog(row as FmeaRecord)"
              data-testid="btn-edit"
            >
              Edit
            </ElButton>
            <ElButton
              v-if="canWrite"
              size="small"
              link
              type="danger"
              @click="handleDelete(row as FmeaRecord)"
              data-testid="btn-delete"
            >
              Delete
            </ElButton>
            <span v-else class="fp-readonly">read-only</span>
          </template>
        </ElTableColumn>
      </ElTable>
    </ElCard>

    <!-- ─── Create / Edit dialog ─── -->
    <ElDialog
      v-model="dialogVisible"
      :title="dialogMode === 'create' ? 'New FMEA Entry' : 'Edit FMEA Entry'"
      width="640px"
      data-testid="fmea-dialog"
    >
      <ElAlert
        v-if="formError"
        data-testid="dialog-error"
        title="Cannot save"
        :description="formError"
        type="error"
        :closable="false"
        show-icon
        class="fp-dialog-error"
      />
      <ElForm label-width="150px" :disabled="dialogLoading">
        <ElFormItem label="Component Code" required>
          <ElInput v-model="form.component_code" placeholder="e.g., PSU-12V" data-testid="field-component" />
        </ElFormItem>
        <ElFormItem label="Function">
          <ElInput v-model="form.function_name" placeholder="Component function" data-testid="field-function" />
        </ElFormItem>
        <ElFormItem label="Fault Code">
          <ElInput v-model="form.fault_code" placeholder="e.g., voltage_drift" data-testid="field-fault" />
        </ElFormItem>
        <ElFormItem label="Failure Mode" required>
          <ElInput v-model="form.failure_mode" placeholder="How it can fail" data-testid="field-mode" />
        </ElFormItem>
        <ElFormItem label="Effects">
          <ElInput v-model="form.effects" type="textarea" :rows="2" data-testid="field-effects" />
        </ElFormItem>
        <ElFormItem label="Cause">
          <ElInput v-model="form.cause" type="textarea" :rows="2" data-testid="field-cause" />
        </ElFormItem>
        <ElFormItem label="Severity (S)" required :error="ratingErrors.severity">
          <ElInputNumber
            v-model="form.severity"
            :min="1"
            :max="10"
            :step="1"
            :precision="0"
            controls-position="right"
            data-testid="field-severity"
          />
        </ElFormItem>
        <ElFormItem label="Occurrence (O)" required :error="ratingErrors.occurrence">
          <ElInputNumber
            v-model="form.occurrence"
            :min="1"
            :max="10"
            :step="1"
            :precision="0"
            controls-position="right"
            data-testid="field-occurrence"
          />
        </ElFormItem>
        <ElFormItem label="Detection (D)" required :error="ratingErrors.detection">
          <ElInputNumber
            v-model="form.detection"
            :min="1"
            :max="10"
            :step="1"
            :precision="0"
            controls-position="right"
            data-testid="field-detection"
          />
        </ElFormItem>
        <ElFormItem label="RPN (S×O×D)">
          <ElTag
            :type="previewRpn === null ? 'info' : bandTagType(previewRpn)"
            size="large"
            data-testid="rpn-preview"
          >
            {{ previewRpn === null ? '—' : previewRpn }}
          </ElTag>
          <span class="fp-rpn-hint">Computed server-side on save</span>
        </ElFormItem>
        <ElFormItem label="Recommended Action">
          <ElInput
            v-model="form.recommended_action"
            type="textarea"
            :rows="2"
            data-testid="field-action"
          />
        </ElFormItem>
      </ElForm>
      <template #footer>
        <ElButton @click="dialogVisible = false" :disabled="dialogLoading">Cancel</ElButton>
        <ElButton
          type="primary"
          :loading="dialogLoading"
          @click="submitForm"
          data-testid="btn-save"
        >
          Save
        </ElButton>
      </template>
    </ElDialog>
  </div>
</template>

<style scoped>
.fmea-panel {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-md);
  padding: var(--spacing-md) var(--spacing-lg);
  min-height: 100vh;
  background-color: var(--color-bg-secondary);
}

/* ─── Header ─── */
.fp-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: var(--spacing-sm);
}

.fp-header-left {
  display: flex;
  align-items: center;
  gap: var(--spacing-sm);
  flex-wrap: wrap;
}

.fp-header-right {
  display: flex;
  align-items: center;
  gap: var(--spacing-xs);
}

.fp-title {
  font-size: 1.25rem;
  font-weight: 600;
  color: var(--color-text-primary);
  margin: 0;
}

/* ─── Filters ─── */
.fp-filter-card {
  background-color: var(--color-bg-primary);
  border: 1px solid var(--color-border-default);
  border-radius: var(--radius-xl);
}

.fp-filter-row {
  display: flex;
  align-items: center;
  gap: var(--spacing-sm);
  flex-wrap: wrap;
}

.fp-filter-input {
  width: 220px;
}

/* ─── Table ─── */
.fp-table-card {
  background-color: var(--color-bg-primary);
  border: 1px solid var(--color-border-default);
  border-radius: var(--radius-xl);
}

.fp-readonly {
  font-size: 0.75rem;
  color: var(--color-text-tertiary);
}

.fp-rpn-hint {
  margin-left: var(--spacing-sm);
  font-size: 0.75rem;
  color: var(--color-text-tertiary);
}

.fp-dialog-error {
  margin-bottom: var(--spacing-md);
}

/* ─── High-RPN row highlighting ─── */
.fmea-panel :deep(.fmea-row-danger) {
  background-color: var(--color-danger-bg, rgba(245, 108, 108, 0.12));
}

.fmea-panel :deep(.fmea-row-warning) {
  background-color: var(--color-warning-bg, rgba(230, 162, 60, 0.12));
}

/* ─── Responsive ─── */
@media (max-width: 768px) {
  .fmea-panel {
    padding: var(--spacing-sm);
  }

  .fp-filter-input {
    width: 100%;
  }
}
</style>
