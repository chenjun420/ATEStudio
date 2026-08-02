<script setup lang="ts">
/**
 * ReplayDiffViewer - 2-column diff view for recorded vs replayed events.
 *
 * Layout:
 * - Left column:  original recording events (captured during recording).
 * - Right column: new execution events (captured during replay).
 * - Rows are aligned by timestamp; differences highlighted:
 *     - green background: event added in replay (right only)
 *     - red background:   event removed from replay (left only)
 *     - yellow background: event changed (same step_id, different data)
 *
 * For large event lists, uses RecycleScroller (vue-virtual-scroller) for
 * virtual scrolling - only visible rows are rendered.
 *
 * Props:
 *   originalEvents:  RecordedEventResponse[] from the original recording.
 *   replayedEvents:  RecordedEventResponse[] from the new replay.
 *   diffEntries:     ReplayDiffEntry[] from POST /replay/diff (optional -
 *                    when provided, drives the per-row highlight).
 *
 * The component computes an aligned row list from the two event arrays.
 * When `diffEntries` is provided, rows are tagged with their diff kind
 * (added/removed/changed) for highlight. When not provided, rows are
 * aligned by index and simple equality is used to detect changes.
 */
import { computed } from 'vue'
import { RecycleScroller } from 'vue-virtual-scroller'
import 'vue-virtual-scroller/dist/vue-virtual-scroller.css'
import type {
  RecordedEventResponse,
  ReplayDiffEntry,
} from '@/api/simulation'

const props = withDefaults(
  defineProps<{
    /** Events from the original recording. */
    originalEvents: RecordedEventResponse[]
    /** Events from the new replay. */
    replayedEvents: RecordedEventResponse[]
    /** Diff entries from POST /replay/diff (optional). */
    diffEntries?: ReplayDiffEntry[]
    /** Row height in pixels for virtual scrolling. */
    rowHeight?: number
  }>(),
  {
    diffEntries: () => [],
    rowHeight: 48,
  },
)

// ── Types ──

/** Diff kind for a single aligned row. */
type RowKind = 'same' | 'added' | 'removed' | 'changed'

/** A single aligned row in the 2-column diff view. */
interface DiffRow {
  /** Stable key for RecycleScroller. */
  id: string
  /** Index in the original events array (or -1 if added). */
  originalIndex: number
  /** Index in the replayed events array (or -1 if removed). */
  replayedIndex: number
  /** The original event (null if this row was added in replay). */
  original: RecordedEventResponse | null
  /** The replayed event (null if this row was removed in replay). */
  replayed: RecordedEventResponse | null
  /** Diff kind for highlight. */
  kind: RowKind
  /** Timestamp for alignment display. */
  timestamp: string
  /** Step ID for display. */
  stepId: string
  /** Event type for display. */
  eventType: string
}

// ── Build a lookup from diffEntries for per-step diff kind ──
const diffKindByStep = computed<Map<string, RowKind>>(() => {
  const map = new Map<string, RowKind>()
  for (const entry of props.diffEntries) {
    if (entry.kind === 'added' || entry.kind === 'removed' || entry.kind === 'changed') {
      map.set(entry.step_id, entry.kind)
    }
  }
  return map
})

// ── Build aligned rows ──
/**
 * Align the two event arrays by index (positional alignment). When diff
 * entries are available, use them to tag rows with their diff kind based
 * on step_id. Otherwise, fall back to comparing step_id + event_type +
 * JSON-serialized data.
 */
const alignedRows = computed<DiffRow[]>(() => {
  const orig = props.originalEvents
  const replay = props.replayedEvents
  const diffMap = diffKindByStep.value
  const rows: DiffRow[] = []
  const maxLen = Math.max(orig.length, replay.length)

  for (let i = 0; i < maxLen; i++) {
    const o = i < orig.length ? orig[i]! : null
    const r = i < replay.length ? replay[i]! : null

    let kind: RowKind = 'same'
    if (o === null && r !== null) {
      kind = 'added'
    } else if (o !== null && r === null) {
      kind = 'removed'
    } else if (o !== null && r !== null) {
      // Check diff map by step_id first
      const stepKey = o.step_id ?? r.step_id ?? ''
      const mapped = diffMap.get(stepKey)
      if (mapped) {
        kind = mapped
      } else if (!eventsEqual(o, r)) {
        kind = 'changed'
      }
    }

    rows.push({
      id: `row-${i}`,
      originalIndex: o !== null ? i : -1,
      replayedIndex: r !== null ? i : -1,
      original: o,
      replayed: r,
      kind,
      timestamp: (r ?? o)?.timestamp ?? '',
      stepId: (r ?? o)?.step_id ?? '',
      eventType: (r ?? o)?.event_type ?? '',
    })
  }
  return rows
})

// ── Summary stats ──
const summary = computed(() => {
  let added = 0
  let removed = 0
  let changed = 0
  let same = 0
  for (const row of alignedRows.value) {
    if (row.kind === 'added') added++
    else if (row.kind === 'removed') removed++
    else if (row.kind === 'changed') changed++
    else same++
  }
  return { added, removed, changed, same, total: alignedRows.value.length }
})

// ── Helpers ──

/** Shallow equality check for two RecordedEventResponse objects. */
function eventsEqual(a: RecordedEventResponse, b: RecordedEventResponse): boolean {
  if (a.event_type !== b.event_type) return false
  if ((a.step_id ?? '') !== (b.step_id ?? '')) return false
  return JSON.stringify(a.data) === JSON.stringify(b.data)
}

/** Format a timestamp for display. */
function formatTime(ts: string): string {
  if (!ts) return '--'
  try {
    const d = new Date(ts)
    return d.toLocaleTimeString('zh-CN', { hour12: false }) + '.' + String(d.getMilliseconds()).padStart(3, '0')
  } catch {
    return ts
  }
}

/** Truncate a step ID for display. */
function shortStepId(stepId: string | null | undefined): string {
  if (!stepId) return '--'
  return stepId.length > 12 ? stepId.slice(0, 12) + '…' : stepId
}

/** CSS class for a row based on its diff kind. */
function rowClass(kind: RowKind): string {
  switch (kind) {
    case 'added':
      return 'diff-row-added'
    case 'removed':
      return 'diff-row-removed'
    case 'changed':
      return 'diff-row-changed'
    default:
      return 'diff-row-same'
  }
}

/** Background class for an individual cell. */
function cellClass(row: DiffRow, side: 'original' | 'replayed'): string {
  if (row.kind === 'added' && side === 'original') return 'cell-empty'
  if (row.kind === 'removed' && side === 'replayed') return 'cell-empty'
  return rowClass(row.kind)
}
</script>

<template>
  <div class="replay-diff-viewer" data-testid="replay-diff-viewer">
    <!-- Summary header -->
    <div class="diff-summary" data-testid="diff-summary">
      <span class="summary-item summary-total">
        总计: <strong>{{ summary.total }}</strong>
      </span>
      <span class="summary-item summary-same">
        相同: <strong>{{ summary.same }}</strong>
      </span>
      <span class="summary-item summary-added">
        新增: <strong>{{ summary.added }}</strong>
      </span>
      <span class="summary-item summary-removed">
        删除: <strong>{{ summary.removed }}</strong>
      </span>
      <span class="summary-item summary-changed">
        变更: <strong>{{ summary.changed }}</strong>
      </span>
    </div>

    <!-- Empty state -->
    <div
      v-if="alignedRows.length === 0"
      class="diff-empty"
      data-testid="diff-empty"
    >
      <p>暂无对比数据</p>
      <p class="tw-text-xs tw-text-neutral-400">
        录制一次执行 → 修改序列 → 回放 → 此处显示差异
      </p>
    </div>

    <!-- Column headers -->
    <div v-else class="diff-header">
      <div class="diff-col-header diff-col-original">
        原始录制 ({{ originalEvents.length }})
      </div>
      <div class="diff-col-divider"></div>
      <div class="diff-col-header diff-col-replayed">
        新执行 ({{ replayedEvents.length }})
      </div>
    </div>

    <!-- Virtual scrolled rows -->
    <RecycleScroller
      v-if="alignedRows.length > 0"
      class="diff-scroller"
      :items="alignedRows"
      :item-size="rowHeight"
      :buffer="200"
      key-field="id"
      data-testid="diff-scroller"
    >
      <template #default="{ item }">
        <div
          class="diff-row"
          :class="rowClass((item as DiffRow).kind)"
          :data-row-kind="(item as DiffRow).kind"
          :data-testid="`diff-row-${(item as DiffRow).id}`"
        >
          <!-- Original column -->
          <div
            class="diff-cell diff-cell-original"
            :class="cellClass(item as DiffRow, 'original')"
          >
            <template v-if="(item as DiffRow).original">
              <span class="cell-time">{{ formatTime((item as DiffRow).original!.timestamp) }}</span>
              <span class="cell-step">{{ shortStepId((item as DiffRow).original!.step_id) }}</span>
              <span class="cell-type">{{ (item as DiffRow).original!.event_type }}</span>
            </template>
            <span v-else class="cell-placeholder">—</span>
          </div>

          <!-- Divider -->
          <div class="diff-cell-divider"></div>

          <!-- Replayed column -->
          <div
            class="diff-cell diff-cell-replayed"
            :class="cellClass(item as DiffRow, 'replayed')"
          >
            <template v-if="(item as DiffRow).replayed">
              <span class="cell-time">{{ formatTime((item as DiffRow).replayed!.timestamp) }}</span>
              <span class="cell-step">{{ shortStepId((item as DiffRow).replayed!.step_id) }}</span>
              <span class="cell-type">{{ (item as DiffRow).replayed!.event_type }}</span>
            </template>
            <span v-else class="cell-placeholder">—</span>
          </div>
        </div>
      </template>
    </RecycleScroller>
  </div>
</template>

<style scoped>
.replay-diff-viewer {
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 200px;
  border: 1px solid var(--color-border-default);
  border-radius: 6px;
  overflow: hidden;
  background: var(--color-bg-elevated);
}

/* Summary header */
.diff-summary {
  display: flex;
  gap: 1rem;
  padding: 0.5rem 0.75rem;
  background: var(--color-bg-secondary);
  border-bottom: 1px solid var(--color-border-default);
  font-size: 0.75rem;
  color: var(--color-text-secondary);
}

.summary-item strong {
  color: var(--color-text-primary);
}

.summary-added strong {
  color: #16a34a;
}

.summary-removed strong {
  color: #dc2626;
}

.summary-changed strong {
  color: #d97706;
}

/* Empty state */
.diff-empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  flex: 1;
  padding: 2rem;
  color: var(--color-text-secondary);
  text-align: center;
}

/* Column headers */
.diff-header {
  display: flex;
  border-bottom: 1px solid var(--color-border-default);
  background: var(--color-bg-tertiary);
}

.diff-col-header {
  flex: 1;
  padding: 0.5rem 0.75rem;
  font-size: 0.75rem;
  font-weight: 600;
  color: var(--color-text-primary);
}

.diff-col-original {
  border-right: 1px solid var(--color-border-default);
}

.diff-col-replayed {
  border-left: 1px solid var(--color-border-default);
}

.diff-col-divider {
  width: 1px;
  background: var(--color-border-default);
}

/* Virtual scroller */
.diff-scroller {
  flex: 1;
  overflow: auto;
}

/* Rows */
.diff-row {
  display: flex;
  height: 100%;
  border-bottom: 1px solid var(--color-border-default);
}

.diff-cell {
  flex: 1;
  display: flex;
  align-items: center;
  gap: 0.5rem;
  padding: 0 0.75rem;
  font-size: 0.75rem;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  overflow: hidden;
}

.diff-cell-original {
  border-right: 1px solid var(--color-border-default);
}

.diff-cell-replayed {
  border-left: 1px solid var(--color-border-default);
}

.diff-cell-divider {
  width: 1px;
  background: var(--color-border-default);
}

.cell-time {
  color: var(--color-text-secondary);
  white-space: nowrap;
}

.cell-step {
  color: var(--color-text-primary);
  font-weight: 500;
  white-space: nowrap;
}

.cell-type {
  color: var(--color-text-secondary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.cell-placeholder {
  color: var(--color-text-tertiary);
  font-style: italic;
}

/* Row highlight colors */
.diff-row-same {
  background: var(--color-bg-elevated);
}

.diff-row-added {
  background: #f0fdf4;
}

.diff-row-added .diff-cell-replayed {
  background: #dcfce7;
}

.diff-row-removed {
  background: #fef2f2;
}

.diff-row-removed .diff-cell-original {
  background: #fee2e2;
}

.diff-row-changed {
  background: #fffbeb;
}

.diff-row-changed .diff-cell-original {
  background: #fef3c7;
}

.diff-row-changed .diff-cell-replayed {
  background: #fef3c7;
}

.cell-empty {
  background: var(--color-bg-secondary);
}
</style>
