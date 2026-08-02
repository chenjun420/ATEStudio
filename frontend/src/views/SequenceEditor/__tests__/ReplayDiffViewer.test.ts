/**
 * Tests for ReplayDiffViewer.vue component.
 *
 * Verifies:
 * - Empty state renders when both event arrays are empty
 * - Summary header shows correct counts (total/same/added/removed/changed)
 * - Column headers show original vs replayed counts
 * - Rows render in 2-column layout aligned by index
 * - Added rows highlight the right column in green
 * - Removed rows highlight the left column in red
 * - Changed rows highlight both columns in yellow
 * - Same rows have no highlight
 * - diffEntries prop drives per-step highlight when provided
 * - Virtual scroller renders all rows (RecycleScroller mocked)
 *
 * vue-virtual-scroller is mocked to render all items directly (no virtual
 * windowing) so we can assert on the rendered DOM. This mirrors how jsdom
 * tests handle virtual-scroller components.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { h, defineComponent } from 'vue'
import type { RecordedEventResponse, ReplayDiffEntry } from '@/api/simulation'

// ─── Mock vue-virtual-scroller ───────────────────────────────────────────────
//
// RecycleScroller uses IntersectionObserver + scroll containers that don't
// work reliably in jsdom. Replace it with a simple div that renders every
// item via the default slot. This preserves the slot contract so the
// component under test renders its row template for every item.
//
vi.mock('vue-virtual-scroller', () => {
  const RecycleScroller = defineComponent({
    name: 'RecycleScroller',
    props: {
      items: { type: Array, default: () => [] },
      itemSize: { type: Number, default: 50 },
      buffer: { type: Number, default: 200 },
      keyField: { type: String, default: 'id' },
    },
    setup(props, { slots }) {
      return () =>
        h(
          'div',
          { class: 'mock-recycle-scroller', 'data-testid': 'diff-scroller' },
          props.items.map((item: unknown, index: number) => {
            const slot = slots.default?.({ item, index })
            return h('div', { key: index }, slot)
          }),
        )
    },
  })
  return { RecycleScroller }
})

// The CSS import is a side-effect import in the component; mock it to a
// no-op so vitest doesn't try to parse CSS.
vi.mock('vue-virtual-scroller/dist/vue-virtual-scroller.css', () => ({}), { virtual: true })

import ReplayDiffViewer from '../components/ReplayDiffViewer.vue'

// ─── Test helpers ────────────────────────────────────────────────────────────

/** Build a RecordedEventResponse with sensible defaults. */
function createEvent(overrides: Partial<RecordedEventResponse> = {}): RecordedEventResponse {
  return {
    timestamp: '2026-01-01T00:00:00.000Z',
    event_type: 'STEP_COMPLETED',
    session_id: 'run-001',
    step_id: 'step-1',
    data: { status: 'passed' },
    ...overrides,
  }
}

/** Build a ReplayDiffEntry with sensible defaults. */
function createDiffEntry(overrides: Partial<ReplayDiffEntry> = {}): ReplayDiffEntry {
  return {
    kind: 'changed',
    step_id: 'step-1',
    event_type: 'STEP_COMPLETED',
    original: null,
    replayed: null,
    ...overrides,
  }
}

/** Mount the component with default props. */
function mountComponent(props: Record<string, unknown> = {}) {
  return mount(ReplayDiffViewer, { props })
}

// ─── Tests ───────────────────────────────────────────────────────────────────

describe('ReplayDiffViewer', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  // ── Empty state ──

  it('test_renders_empty_state_when_no_events', () => {
    const wrapper = mountComponent({
      originalEvents: [],
      replayedEvents: [],
    })
    const empty = wrapper.find('[data-testid="diff-empty"]')
    expect(empty.exists()).toBe(true)
    expect(empty.text()).toContain('暂无对比数据')
  })

  it('test_does_not_render_scroller_when_empty', () => {
    const wrapper = mountComponent({
      originalEvents: [],
      replayedEvents: [],
    })
    expect(wrapper.find('[data-testid="diff-scroller"]').exists()).toBe(false)
  })

  // ── Summary header ──

  it('test_summary_shows_correct_counts_all_same', () => {
    const events = [createEvent(), createEvent({ step_id: 'step-2' })]
    const wrapper = mountComponent({
      originalEvents: events,
      replayedEvents: events,
    })
    const summary = wrapper.find('[data-testid="diff-summary"]')
    expect(summary.text()).toContain('总计: 2')
    expect(summary.text()).toContain('相同: 2')
    expect(summary.text()).toContain('新增: 0')
    expect(summary.text()).toContain('删除: 0')
    expect(summary.text()).toContain('变更: 0')
  })

  it('test_summary_shows_added_count', () => {
    const wrapper = mountComponent({
      originalEvents: [createEvent()],
      replayedEvents: [createEvent(), createEvent({ step_id: 'step-2' })],
    })
    const summary = wrapper.find('[data-testid="diff-summary"]')
    expect(summary.text()).toContain('新增: 1')
  })

  it('test_summary_shows_removed_count', () => {
    const wrapper = mountComponent({
      originalEvents: [createEvent(), createEvent({ step_id: 'step-2' })],
      replayedEvents: [createEvent()],
    })
    const summary = wrapper.find('[data-testid="diff-summary"]')
    expect(summary.text()).toContain('删除: 1')
  })

  it('test_summary_shows_changed_count', () => {
    const wrapper = mountComponent({
      originalEvents: [createEvent({ data: { status: 'passed' } })],
      replayedEvents: [createEvent({ data: { status: 'failed' } })],
    })
    const summary = wrapper.find('[data-testid="diff-summary"]')
    expect(summary.text()).toContain('变更: 1')
  })

  // ── Column headers ──

  it('test_column_headers_show_counts', () => {
    const wrapper = mountComponent({
      originalEvents: [createEvent(), createEvent({ step_id: 's2' })],
      replayedEvents: [createEvent()],
    })
    const headers = wrapper.findAll('.diff-col-header')
    expect(headers.length).toBe(2)
    expect(headers[0]!.text()).toContain('原始录制')
    expect(headers[0]!.text()).toContain('2')
    expect(headers[1]!.text()).toContain('新执行')
    expect(headers[1]!.text()).toContain('1')
  })

  // ── Row highlighting ──

  it('test_same_row_has_no_highlight', () => {
    const events = [createEvent()]
    const wrapper = mountComponent({
      originalEvents: events,
      replayedEvents: events,
    })
    const row = wrapper.find('[data-testid="diff-row-row-0"]')
    expect(row.exists()).toBe(true)
    expect(row.attributes('data-row-kind')).toBe('same')
    expect(row.classes()).toContain('diff-row-same')
  })

  it('test_added_row_highlights_right_column', () => {
    const wrapper = mountComponent({
      originalEvents: [],
      replayedEvents: [createEvent()],
    })
    const row = wrapper.find('[data-testid="diff-row-row-0"]')
    expect(row.exists()).toBe(true)
    expect(row.attributes('data-row-kind')).toBe('added')
    expect(row.classes()).toContain('diff-row-added')
  })

  it('test_removed_row_highlights_left_column', () => {
    const wrapper = mountComponent({
      originalEvents: [createEvent()],
      replayedEvents: [],
    })
    const row = wrapper.find('[data-testid="diff-row-row-0"]')
    expect(row.exists()).toBe(true)
    expect(row.attributes('data-row-kind')).toBe('removed')
    expect(row.classes()).toContain('diff-row-removed')
  })

  it('test_changed_row_highlights_both_columns', () => {
    const wrapper = mountComponent({
      originalEvents: [createEvent({ data: { status: 'passed' } })],
      replayedEvents: [createEvent({ data: { status: 'failed' } })],
    })
    const row = wrapper.find('[data-testid="diff-row-row-0"]')
    expect(row.exists()).toBe(true)
    expect(row.attributes('data-row-kind')).toBe('changed')
    expect(row.classes()).toContain('diff-row-changed')
  })

  // ── Row content rendering ──

  it('test_row_renders_event_type_and_step_id', () => {
    const wrapper = mountComponent({
      originalEvents: [createEvent({ step_id: 'step-abc', event_type: 'STEP_STARTED' })],
      replayedEvents: [createEvent({ step_id: 'step-abc', event_type: 'STEP_STARTED' })],
    })
    const row = wrapper.find('[data-testid="diff-row-row-0"]')
    expect(row.text()).toContain('STEP_STARTED')
    expect(row.text()).toContain('step-abc')
  })

  it('test_added_row_shows_placeholder_in_original_column', () => {
    const wrapper = mountComponent({
      originalEvents: [],
      replayedEvents: [createEvent()],
    })
    const row = wrapper.find('[data-testid="diff-row-row-0"]')
    // The original cell should show a placeholder "-"
    const originalCell = row.find('.diff-cell-original')
    expect(originalCell.exists()).toBe(true)
    const placeholder = originalCell.find('.cell-placeholder')
    expect(placeholder.exists()).toBe(true)
    // Check the rendered HTML contains a placeholder dash
    expect(placeholder.html()).toContain('cell-placeholder')
  })

  it('test_removed_row_shows_placeholder_in_replayed_column', () => {
    const wrapper = mountComponent({
      originalEvents: [createEvent()],
      replayedEvents: [],
    })
    const row = wrapper.find('[data-testid="diff-row-row-0"]')
    const replayedCell = row.find('.diff-cell-replayed')
    expect(replayedCell.exists()).toBe(true)
    const placeholder = replayedCell.find('.cell-placeholder')
    expect(placeholder.exists()).toBe(true)
    expect(placeholder.html()).toContain('cell-placeholder')
  })

  // ── Diff entries drive highlight ──

  it('test_diff_entries_drive_changed_highlight', () => {
    // Two events with identical data - without diffEntries they'd be 'same'.
    // With a diffEntries entry marking step-1 as 'changed', the row should
    // be highlighted as 'changed'.
    const event = createEvent({ step_id: 'step-1' })
    const wrapper = mountComponent({
      originalEvents: [event],
      replayedEvents: [event],
      diffEntries: [createDiffEntry({ kind: 'changed', step_id: 'step-1' })],
    })
    const row = wrapper.find('[data-testid="diff-row-row-0"]')
    expect(row.attributes('data-row-kind')).toBe('changed')
  })

  it('test_diff_entries_drive_added_highlight', () => {
    const event = createEvent({ step_id: 'step-new' })
    // Both columns have the event (so positional alignment would say 'same'),
    // but diffEntries says it was 'added' for that step_id.
    const wrapper = mountComponent({
      originalEvents: [event],
      replayedEvents: [event],
      diffEntries: [createDiffEntry({ kind: 'added', step_id: 'step-new' })],
    })
    const row = wrapper.find('[data-testid="diff-row-row-0"]')
    expect(row.attributes('data-row-kind')).toBe('added')
  })

  it('test_diff_entries_drive_removed_highlight', () => {
    const event = createEvent({ step_id: 'step-gone' })
    const wrapper = mountComponent({
      originalEvents: [event],
      replayedEvents: [event],
      diffEntries: [createDiffEntry({ kind: 'removed', step_id: 'step-gone' })],
    })
    const row = wrapper.find('[data-testid="diff-row-row-0"]')
    expect(row.attributes('data-row-kind')).toBe('removed')
  })

  // ── Multiple rows alignment ──

  it('test_aligns_multiple_rows_by_index', () => {
    const orig = [
      createEvent({ step_id: 's1', event_type: 'STEP_STARTED' }),
      createEvent({ step_id: 's2', event_type: 'STEP_COMPLETED' }),
      createEvent({ step_id: 's3', event_type: 'STEP_FAILED' }),
    ]
    const replay = [
      createEvent({ step_id: 's1', event_type: 'STEP_STARTED' }),
      createEvent({ step_id: 's2', event_type: 'STEP_COMPLETED' }),
      createEvent({ step_id: 's3', event_type: 'STEP_SKIPPED' }),
    ]
    const wrapper = mountComponent({
      originalEvents: orig,
      replayedEvents: replay,
    })
    const rows = wrapper.findAll('[data-testid^="diff-row-"]')
    expect(rows.length).toBe(3)
    // Row 0 and 1 should be 'same', row 2 should be 'changed'
    expect(rows[0]!.attributes('data-row-kind')).toBe('same')
    expect(rows[1]!.attributes('data-row-kind')).toBe('same')
    expect(rows[2]!.attributes('data-row-kind')).toBe('changed')
  })

  it('test_handles_unequal_array_lengths', () => {
    const orig = [createEvent({ step_id: 's1' }), createEvent({ step_id: 's2' })]
    const replay = [createEvent({ step_id: 's1' })]
    const wrapper = mountComponent({
      originalEvents: orig,
      replayedEvents: replay,
    })
    const rows = wrapper.findAll('[data-testid^="diff-row-"]')
    expect(rows.length).toBe(2)
    // Row 0: same, Row 1: removed (only in original)
    expect(rows[0]!.attributes('data-row-kind')).toBe('same')
    expect(rows[1]!.attributes('data-row-kind')).toBe('removed')
  })

  // ── Step ID truncation ──

  it('test_truncates_long_step_id', () => {
    const longId = 'step-with-a-very-long-identifier-12345'
    const wrapper = mountComponent({
      originalEvents: [createEvent({ step_id: longId })],
      replayedEvents: [createEvent({ step_id: longId })],
    })
    const row = wrapper.find('[data-testid="diff-row-row-0"]')
    // The truncated ID should end with '…' and be shorter than the original
    expect(row.text()).toContain('…')
    expect(row.text()).not.toContain(longId)
  })
})
