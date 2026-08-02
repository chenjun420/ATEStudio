/**
 * useExecutionHistory — composable for execution history view.
 *
 * Provides:
 * - Reactive filter state (serial_number, product_type, status, date range).
 * - Pagination state (currentPage, pageSize, total).
 * - `executions` ref — list of ExecutionListItem from API.
 * - `detail` ref — full Execution for selected row.
 * - `loading` / `error` refs for UI state.
 * - `fetchList()` — fetch executions list (with or without search filters).
 * - `fetchDetail(runId)` — fetch full execution by ID.
 * - `applyFilters()` — reset to page 1 and re-fetch.
 * - `resetFilters()` — clear all filters and re-fetch.
 * - `exportCsv()` — trigger CSV download of current results.
 */
import { computed, onMounted, ref } from 'vue'
import {
  listExecutions,
  searchExecutions,
  getExecution,
  type ExecutionListItem,
  type Execution,
  type ExecutionSearchRequest,
} from '@/api/executions'

export function useExecutionHistory() {
  // ─── Filter state ─────────────────────────────────────────────────────────
  const serialNumber = ref<string>('')
  const productType = ref<string>('')
  const statusFilter = ref<string>('')
  const dateRange = ref<[string, string] | null>(null)

  // ─── Pagination state ──────────────────────────────────────────────────────
  const currentPage = ref<number>(1)
  const pageSize = ref<number>(20)
  const total = ref<number>(0)

  // ─── Data state ─────────────────────────────────────────────────────────────
  const executions = ref<ExecutionListItem[]>([])
  const detail = ref<Execution | null>(null)
  const loading = ref<boolean>(false)
  const error = ref<string | null>(null)
  const detailLoading = ref<boolean>(false)
  const detailError = ref<string | null>(null)

  // ─── Computed ────────────────────────────────────────────────────────────────

  /** Whether any filter is active (non-empty). */
  const hasActiveFilters = computed<boolean>(() => {
    return (
      serialNumber.value.trim() !== '' ||
      productType.value.trim() !== '' ||
      statusFilter.value !== '' ||
      dateRange.value !== null
    )
  })

  /** Build the search request from current filter state. */
  function buildSearchRequest(): ExecutionSearchRequest {
    const params: ExecutionSearchRequest = {
      skip: (currentPage.value - 1) * pageSize.value,
      limit: pageSize.value,
    }
    if (serialNumber.value.trim()) {
      params.serial_number = serialNumber.value.trim()
    }
    if (productType.value.trim()) {
      params.product_type = productType.value.trim()
    }
    if (statusFilter.value) {
      params.status = statusFilter.value
    }
    if (dateRange.value) {
      params.date_from = dateRange.value[0]
      params.date_to = dateRange.value[1]
    }
    return params
  }

  // ─── Actions ─────────────────────────────────────────────────────────────────

  /** Fetch the execution list (search if filters active, else plain list). */
  async function fetchList(): Promise<void> {
    loading.value = true
    error.value = null
    try {
      let resp
      if (hasActiveFilters.value) {
        resp = await searchExecutions(buildSearchRequest())
      } else {
        resp = await listExecutions(
          (currentPage.value - 1) * pageSize.value,
          pageSize.value,
        )
      }
      executions.value = resp.items
      total.value = resp.total
    } catch (e: unknown) {
      error.value = e instanceof Error ? e.message : String(e)
      executions.value = []
      total.value = 0
    } finally {
      loading.value = false
    }
  }

  /** Fetch full execution detail for the detail panel. */
  async function fetchDetail(runId: string): Promise<void> {
    detailLoading.value = true
    detailError.value = null
    detail.value = null
    try {
      detail.value = await getExecution(runId)
    } catch (e: unknown) {
      detailError.value = e instanceof Error ? e.message : String(e)
    } finally {
      detailLoading.value = false
    }
  }

  /** Clear detail panel state. */
  function clearDetail(): void {
    detail.value = null
    detailError.value = null
  }

  /** Reset to page 1 and re-fetch (call when filters change). */
  async function applyFilters(): Promise<void> {
    currentPage.value = 1
    await fetchList()
  }

  /** Re-fetch when page or page size changes. */
  async function onPaginationChange(): Promise<void> {
    await fetchList()
  }

  /** Reset all filters and re-fetch. */
  async function resetFilters(): Promise<void> {
    serialNumber.value = ''
    productType.value = ''
    statusFilter.value = ''
    dateRange.value = null
    currentPage.value = 1
    await fetchList()
  }

  /** Export current list as CSV. */
  function exportCsv(): void {
    const items = executions.value
    if (items.length === 0) return

    const headers = [
      'ID',
      'Sequence ID',
      'Status',
      'DUT Serial',
      'Product Type',
      'Started At',
      'Completed At',
      'Pass Rate',
      'Error',
    ]
    const rows = items.map((item) => [
      item.id,
      item.sequence_id ?? '',
      item.status,
      item.dut_serial ?? '',
      item.product_type ?? '',
      item.started_at ?? '',
      item.completed_at ?? '',
      item.pass_rate !== null ? `${item.pass_rate.toFixed(1)}%` : '',
      (item.error ?? '').replace(/"/g, '""'),
    ])

    const csv = [
      headers.join(','),
      ...rows.map((row) => row.map((cell) => `"${cell}"`).join(',')),
    ].join('\n')

    const blob = new Blob([csv], { type: 'text/csv;charset=utf-8' })
    const url = URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = url
    link.download = `execution_history_${new Date().toISOString().slice(0, 10)}.csv`
    document.body.appendChild(link)
    link.click()
    document.body.removeChild(link)
    URL.revokeObjectURL(url)
  }

  // ─── Lifecycle ──────────────────────────────────────────────────────────────

  onMounted(() => {
    void fetchList()
  })

  return {
    // Filter state
    serialNumber,
    productType,
    statusFilter,
    dateRange,
    hasActiveFilters,
    // Pagination state
    currentPage,
    pageSize,
    total,
    // Data state
    executions,
    detail,
    loading,
    error,
    detailLoading,
    detailError,
    // Actions
    fetchList,
    fetchDetail,
    clearDetail,
    applyFilters,
    onPaginationChange,
    resetFilters,
    exportCsv,
    buildSearchRequest,
  }
}
