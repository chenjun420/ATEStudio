<script setup lang="ts">
/**
 * Requirement ↔ test-case traceability matrix view (task 26).
 *
 * Renders requirement → cases → DSL-step coverage from the knowledge READ API:
 *
 * - `GET /knowledge/traceability` — the requirement → cases → DSL-step tree
 *   that drives the matrix (cases without a requirement surface in a separate
 *   "unlinked cases" gap section).
 * - `GET /knowledge/requirements` — paged requirement list; its `total` drives
 *   the header requirement count.
 * - `GET /knowledge/cases` — paged case list; its `total` drives the header
 *   case count (and confirms the requirement↔case join).
 *
 * A coverage indicator per requirement shows whether it has verifying cases and
 * whether those cases are linked to DSL steps (step_id). Element Plus + Tailwind
 * conventions mirror FmeaManagement.
 *
 * API: /api/v1/knowledge/* (see frontend/src/api/knowledge.ts).
 */
import { computed, onMounted, ref } from 'vue'
import {
  ElCard,
  ElButton,
  ElInput,
  ElTag,
  ElAlert,
  ElEmpty,
  ElSkeleton,
  ElTable,
  ElTableColumn,
} from 'element-plus'
import {
  fetchTraceability,
  fetchRequirements,
  fetchCases,
  type TraceabilityTree,
  type TraceabilityRequirement,
  type TraceabilityCase,
} from '@/api/knowledge'

// ─── State ───────────────────────────────────────────────────────────────────

const tree = ref<TraceabilityTree>({ product_code: null, requirements: [], unlinked_cases: [] })
const requirementTotal = ref(0)
const caseTotal = ref(0)
const loading = ref(false)
const error = ref<string | null>(null)

const productFilter = ref('')

// ─── Derived coverage ────────────────────────────────────────────────────────

/** Requirements that have at least one verifying case. */
const coveredCount = computed(
  () => tree.value.requirements.filter((r) => r.cases.length > 0).length,
)

/** Requirements with no verifying case (traceability gaps). */
const uncoveredCount = computed(
  () => tree.value.requirements.length - coveredCount.value,
)

/** Percentage of requirements covered by at least one case (0 when no reqs). */
const coveragePercent = computed(() => {
  const total = tree.value.requirements.length
  if (total === 0) return 0
  return Math.round((coveredCount.value / total) * 100)
})

/** Coverage band for a requirement, driving the tag type. */
function coverageTagType(req: TraceabilityRequirement): 'success' | 'danger' | 'warning' {
  if (req.cases.length === 0) return 'danger'
  // Covered but some cases lack a DSL step link → partial (warning).
  const linked = req.cases.filter((c) => c.step_id).length
  return linked === req.cases.length ? 'success' : 'warning'
}

function coverageLabel(req: TraceabilityRequirement): string {
  if (req.cases.length === 0) return 'Uncovered'
  const linked = req.cases.filter((c) => c.step_id).length
  return linked === req.cases.length ? 'Covered' : 'Partial'
}

/** DSL step id for a case, or a dash when the case is not linked to a step. */
function stepLabel(c: TraceabilityCase): string {
  return c.step_id || '—'
}

// ─── Data fetching ───────────────────────────────────────────────────────────

function extractError(e: unknown, fallback: string): string {
  const detail = (e as { response?: { data?: { detail?: unknown } } | undefined } | null)?.response
    ?.data?.detail
  if (typeof detail === 'string' && detail) return detail
  if (e instanceof Error && e.message) return e.message
  return fallback
}

async function loadMatrix(): Promise<void> {
  loading.value = true
  error.value = null
  const product = productFilter.value.trim() || undefined
  try {
    // The matrix tree is the primary payload; the paged totals accompany it.
    const [treeRes, reqRes, caseRes] = await Promise.all([
      fetchTraceability(product),
      fetchRequirements({ product_code: product, limit: 1 }),
      fetchCases({ product_code: product, limit: 1 }),
    ])
    tree.value = treeRes
    requirementTotal.value = reqRes.total
    caseTotal.value = caseRes.total
  } catch (e: unknown) {
    tree.value = { product_code: product ?? null, requirements: [], unlinked_cases: [] }
    error.value = extractError(e, 'Failed to load traceability matrix.')
  } finally {
    loading.value = false
  }
}

function applyFilters(): void {
  void loadMatrix()
}

function resetFilters(): void {
  productFilter.value = ''
  void loadMatrix()
}

onMounted(() => {
  void loadMatrix()
})
</script>

<template>
  <div class="tm-panel">
    <!-- ─── Header ─── -->
    <header class="tm-header">
      <div class="tm-header-left">
        <h1 class="tm-title">Requirement Traceability</h1>
        <ElTag type="info" size="small" data-testid="count-requirements">
          Requirements: {{ requirementTotal }}
        </ElTag>
        <ElTag type="info" size="small" data-testid="count-cases">
          Cases: {{ caseTotal }}
        </ElTag>
        <ElTag type="success" size="small" data-testid="count-covered">
          Covered: {{ coveredCount }}
        </ElTag>
        <ElTag :type="uncoveredCount > 0 ? 'danger' : 'info'" size="small" data-testid="count-uncovered">
          Uncovered: {{ uncoveredCount }}
        </ElTag>
        <ElTag type="warning" size="small" data-testid="coverage-percent">
          Coverage: {{ coveragePercent }}%
        </ElTag>
      </div>
      <div class="tm-header-right">
        <ElButton size="small" @click="loadMatrix" data-testid="btn-reload">Reload</ElButton>
      </div>
    </header>

    <!-- ─── Filters ─── -->
    <ElCard class="tm-filter-card" shadow="never">
      <div class="tm-filter-row">
        <ElInput
          v-model="productFilter"
          placeholder="Filter by product code"
          clearable
          size="small"
          class="tm-filter-input"
          data-testid="filter-product"
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
      title="Failed to load traceability matrix"
      :description="error"
      type="error"
      :closable="false"
      show-icon
    />

    <!-- ─── Matrix ─── -->
    <ElCard class="tm-table-card" shadow="never" data-testid="table-card">
      <ElSkeleton v-if="loading" :rows="6" animated data-testid="table-skeleton" />
      <ElEmpty
        v-else-if="tree.requirements.length === 0 && !error"
        data-testid="empty-matrix"
        description="No requirements found for the current product filter."
      />
      <ElTable
        v-else
        :data="tree.requirements"
        stripe
        border
        style="width: 100%"
        data-testid="traceability-table"
      >
        <ElTableColumn prop="requirement_code" label="Requirement" min-width="150" />
        <ElTableColumn prop="title" label="Title" min-width="200" show-overflow-tooltip />
        <ElTableColumn prop="source" label="Source" width="100" align="center" />
        <ElTableColumn label="Coverage" width="120" align="center">
          <template #default="{ row }">
            <ElTag
              :type="coverageTagType(row as TraceabilityRequirement)"
              size="small"
              data-testid="coverage-tag"
            >
              {{ coverageLabel(row as TraceabilityRequirement) }}
              ({{ (row as TraceabilityRequirement).cases.length }})
            </ElTag>
          </template>
        </ElTableColumn>
        <ElTableColumn label="Linked Test Cases → DSL Steps" min-width="320">
          <template #default="{ row }">
            <div
              v-if="(row as TraceabilityRequirement).cases.length > 0"
              class="tm-case-list"
              data-testid="case-list"
            >
              <div
                v-for="c in (row as TraceabilityRequirement).cases"
                :key="c.id"
                class="tm-case-row"
                :data-testid="`case-${c.case_code}`"
              >
                <ElTag size="small" type="info" class="tm-case-code">{{ c.case_code }}</ElTag>
                <span class="tm-case-title">{{ c.title }}</span>
                <ElTag
                  size="small"
                  :type="c.step_id ? 'success' : 'warning'"
                  data-testid="step-tag"
                >
                  step: {{ stepLabel(c) }}
                </ElTag>
              </div>
            </div>
            <span v-else class="tm-no-cases" data-testid="no-cases">No linked cases</span>
          </template>
        </ElTableColumn>
      </ElTable>
    </ElCard>

    <!-- ─── Unlinked cases (traceability gaps) ─── -->
    <ElCard
      v-if="!loading && tree.unlinked_cases.length > 0"
      class="tm-table-card"
      shadow="never"
      data-testid="unlinked-card"
    >
      <template #header>
        <span class="tm-unlinked-title">
          Unlinked Cases ({{ tree.unlinked_cases.length }}) — ingested without a requirement
        </span>
      </template>
      <ElTable :data="tree.unlinked_cases" stripe border style="width: 100%" data-testid="unlinked-table">
        <ElTableColumn prop="case_code" label="Case" min-width="150" />
        <ElTableColumn prop="title" label="Title" min-width="220" show-overflow-tooltip />
        <ElTableColumn label="DSL Step" width="160" align="center">
          <template #default="{ row }">
            <ElTag size="small" :type="(row as TraceabilityCase).step_id ? 'success' : 'warning'">
              step: {{ stepLabel(row as TraceabilityCase) }}
            </ElTag>
          </template>
        </ElTableColumn>
      </ElTable>
    </ElCard>
  </div>
</template>

<style scoped>
.tm-panel {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-md);
  padding: var(--spacing-md) var(--spacing-lg);
  min-height: 100vh;
  background-color: var(--color-bg-secondary);
}

.tm-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: var(--spacing-sm);
}

.tm-header-left {
  display: flex;
  align-items: center;
  gap: var(--spacing-sm);
  flex-wrap: wrap;
}

.tm-title {
  font-size: 1.25rem;
  font-weight: 600;
  color: var(--color-text-primary);
  margin: 0;
}

.tm-filter-card,
.tm-table-card {
  background-color: var(--color-bg-primary);
  border: 1px solid var(--color-border-default);
  border-radius: var(--radius-xl);
}

.tm-filter-row {
  display: flex;
  align-items: center;
  gap: var(--spacing-sm);
  flex-wrap: wrap;
}

.tm-filter-input {
  width: 240px;
}

.tm-case-list {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.tm-case-row {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.tm-case-code {
  flex-shrink: 0;
}

.tm-case-title {
  font-size: 0.8125rem;
  color: var(--color-text-secondary, #606266);
}

.tm-no-cases {
  font-size: 0.75rem;
  color: var(--color-text-tertiary, #909399);
}

.tm-unlinked-title {
  font-weight: 600;
  color: var(--color-text-primary);
}

@media (max-width: 768px) {
  .tm-panel {
    padding: var(--spacing-sm);
  }

  .tm-filter-input {
    width: 100%;
  }
}
</style>
