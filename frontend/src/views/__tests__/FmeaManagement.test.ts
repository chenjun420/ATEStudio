/**
 * Tests for FmeaManagement.vue.
 *
 * Verifies:
 * - Seeded FMEA rows render (component code, failure mode, RPN values).
 * - Column headers include S / O / D / RPN.
 * - High-RPN rows get a danger/warning highlight class.
 * - Edit dialog: changing severity recomputes the client-side RPN preview;
 *   Save calls updateFmea with the new ratings and NO rpn in the payload.
 * - Out-of-range ratings are blocked client-side (save does not call API;
 *   an inline error is shown).
 * - A server 422 surfaces as an inline dialog error (message from FastAPI
 *   detail), and the dialog stays open.
 * - Create payload also omits rpn.
 * - RBAC: write actions hidden for users without system:write.
 *
 * The @/api/fmea async functions and useAuth are mocked; pure helpers
 * (computeRpn/isValidRating/rpnBand/thresholds) come from the real module.
 * ElTable/ElTableColumn are stubbed (jsdom has no layout) and ElDialog is
 * stubbed to render inline (the real one teleports to <body>).
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { h, defineComponent, nextTick } from 'vue'
import ElementPlus, { ElInputNumber } from 'element-plus'
import FmeaManagement from '../FmeaManagement.vue'
import type { FmeaRecord } from '@/api/fmea'

// ─── Mocks (hoisted) ─────────────────────────────────────────────────────────

const { fetchFmeasMock, createFmeaMock, updateFmeaMock, deleteFmeaMock, hasScopeMock } =
  vi.hoisted(() => ({
    fetchFmeasMock: vi.fn(),
    createFmeaMock: vi.fn(),
    updateFmeaMock: vi.fn(),
    deleteFmeaMock: vi.fn(),
    hasScopeMock: vi.fn(() => true),
  }))

vi.mock('@/api/fmea', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/fmea')>()
  return {
    ...actual,
    fetchFmeas: fetchFmeasMock,
    createFmea: createFmeaMock,
    updateFmea: updateFmeaMock,
    deleteFmea: deleteFmeaMock,
  }
})

vi.mock('@/composables/useAuth', () => ({
  useAuth: () => ({ hasScope: hasScopeMock }),
}))

// ElMessage/ElMessageBox render nothing in jsdom; leave the real ones (they
// no-op DOM safely for these flows).

// ─── jsdom polyfill ──────────────────────────────────────────────────────────
class ResizeObserverMock {
  observe(): void {}
  unobserve(): void {}
  disconnect(): void {}
}
globalThis.ResizeObserver = ResizeObserverMock as unknown as typeof ResizeObserver

// ─── Stubs ───────────────────────────────────────────────────────────────────

interface VNodeLike {
  props?: Record<string, unknown>
  children?: { default?: (scope: Record<string, unknown>) => unknown }
}

const ElTableStub = defineComponent({
  name: 'ElTable',
  props: {
    data: { type: Array, default: () => [] },
    rowClassName: { type: [Function, String], default: '' },
  },
  setup(props, { slots }) {
    return () => {
      const cols: VNodeLike[] = []
      const defaultSlot = slots.default?.()
      const flat = Array.isArray(defaultSlot) ? defaultSlot : [defaultSlot]
      for (const node of flat ?? []) {
        if (node && typeof node === 'object' && 'props' in node) cols.push(node as VNodeLike)
      }

      const rows = props.data as FmeaRecord[]
      return h('div', { class: 'el-table-stub', 'data-testid': 'fmea-table' }, [
        h(
          'div',
          { class: 'el-table-header' },
          cols.map((c, i) => h('span', { key: i, class: 'el-table-col-header' }, String(c.props?.label ?? ''))),
        ),
        ...rows.map((row, ri) => {
          const cls =
            typeof props.rowClassName === 'function'
              ? String((props.rowClassName as (arg: { row: FmeaRecord }) => string)({ row }))
              : ''
          return h(
            'div',
            { key: row.id || ri, class: ['el-table-row', cls] },
            cols.map((c, ci) => {
              const cellSlot = c.children?.default
              if (cellSlot) {
                return h('span', { key: ci, class: 'el-table-cell' }, cellSlot({ row, $index: ri }) as never)
              }
              const prop = c.props?.prop as string | undefined
              return h('span', { key: ci, class: 'el-table-cell' }, prop ? String((row as never)[prop] ?? '') : '')
            }),
          )
        }),
      ])
    }
  },
})

const ElTableColumnStub = defineComponent({
  name: 'ElTableColumn',
  props: { prop: { type: String, default: '' }, label: { type: String, default: '' } },
  setup(_props, { slots }) {
    return () => slots.default?.()
  },
})

const ElDialogStub = defineComponent({
  name: 'ElDialog',
  props: { modelValue: { type: Boolean, default: false }, title: { type: String, default: '' } },
  setup(props, { slots }) {
    return () =>
      props.modelValue
        ? h('div', { 'data-testid': 'fmea-dialog' }, [slots.default?.(), slots.footer?.()])
        : null
  },
})

// ─── Helpers ─────────────────────────────────────────────────────────────────

function makeRecord(overrides: Partial<FmeaRecord> = {}): FmeaRecord {
  return {
    id: 'f1',
    component_code: 'PSU-12V',
    function_name: 'Provide 12V rail',
    fault_code: 'voltage_drift',
    failure_mode: 'Output drifts high',
    effects: 'UUT damage',
    cause: 'Aging feedback resistor',
    severity: 7,
    occurrence: 4,
    detection: 3,
    rpn: 84,
    recommended_action: 'Replace resistor annually',
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
    ...overrides,
  }
}

function seedRecords(records: FmeaRecord[]): void {
  fetchFmeasMock.mockResolvedValue({ items: records, total: records.length })
}

async function mountView() {
  const wrapper = mount(FmeaManagement, {
    global: {
      plugins: [ElementPlus],
      stubs: {
        ElTable: ElTableStub,
        ElTableColumn: ElTableColumnStub,
        ElDialog: ElDialogStub,
      },
    },
  })
  await flushPromises()
  await nextTick()
  return wrapper
}

/** Open the edit dialog for the first row and return the three rating inputs. */
async function openEditAndGetRatings(wrapper: ReturnType<typeof mount>) {
  const editBtn = wrapper.findAll('button').find((b) => b.text().includes('Edit'))
  expect(editBtn).toBeTruthy()
  await editBtn!.trigger('click')
  await nextTick()
  const numbers = wrapper.findAllComponents(ElInputNumber)
  // Order in the dialog: severity, occurrence, detection.
  expect(numbers.length).toBeGreaterThanOrEqual(3)
  return { severity: numbers[0], occurrence: numbers[1], detection: numbers[2] }
}

async function setRating(
  comp: ReturnType<ReturnType<typeof mount>['findComponent']>,
  value: number | null,
): Promise<void> {
  comp.vm.$emit('update:modelValue', value)
  await nextTick()
}

// ─── Tests ───────────────────────────────────────────────────────────────────

describe('FmeaManagement', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    hasScopeMock.mockReturnValue(true)
  })

  it('renders seeded FMEA rows with S/O/D/RPN columns', async () => {
    seedRecords([makeRecord(), makeRecord({ id: 'f2', component_code: 'DMM-1', rpn: 30 })])
    const wrapper = await mountView()

    const table = wrapper.find('[data-testid="fmea-table"]')
    expect(table.exists()).toBe(true)
    const text = table.text()
    expect(text).toContain('PSU-12V')
    expect(text).toContain('Output drifts high')
    expect(text).toContain('DMM-1')
    // Column headers
    expect(text).toContain('S')
    expect(text).toContain('O')
    expect(text).toContain('D')
    expect(text).toContain('RPN')
    // RPN values render in the tag cells
    expect(text).toContain('84')
    expect(text).toContain('30')
  })

  it('highlights high-RPN rows with a danger class and mid-RPN with warning', async () => {
    seedRecords([
      makeRecord({ id: 'danger', rpn: 120 }),
      makeRecord({ id: 'warn', rpn: 72 }),
      makeRecord({ id: 'ok', rpn: 20 }),
    ])
    const wrapper = await mountView()

    const rows = wrapper.findAll('.el-table-row')
    expect(rows[0].classes()).toContain('fmea-row-danger')
    expect(rows[1].classes()).toContain('fmea-row-warning')
    expect(rows[2].classes().some((c) => c.startsWith('fmea-row-'))).toBe(false)
  })

  it('shows header risk counts', async () => {
    seedRecords([
      makeRecord({ id: 'a', rpn: 120 }),
      makeRecord({ id: 'b', rpn: 72 }),
      makeRecord({ id: 'c', rpn: 20 }),
    ])
    const wrapper = await mountView()
    expect(wrapper.find('[data-testid="count-danger"]').text()).toContain('1')
    expect(wrapper.find('[data-testid="count-warning"]').text()).toContain('1')
    expect(wrapper.find('[data-testid="count-total"]').text()).toContain('3')
  })

  it('edit: changing severity recomputes the RPN preview (S*O*D)', async () => {
    seedRecords([makeRecord({ severity: 7, occurrence: 4, detection: 3, rpn: 84 })])
    const wrapper = await mountView()

    const { severity } = await openEditAndGetRatings(wrapper)
    // Initial preview mirrors 7*4*3 = 84
    expect(wrapper.find('[data-testid="rpn-preview"]').text()).toContain('84')

    await setRating(severity, 10)
    // 10*4*3 = 120
    expect(wrapper.find('[data-testid="rpn-preview"]').text()).toContain('120')
  })

  it('edit save: calls updateFmea with new ratings and NO rpn in the payload', async () => {
    updateFmeaMock.mockResolvedValue(makeRecord({ severity: 10, rpn: 120 }))
    seedRecords([makeRecord({ id: 'f1', severity: 7, occurrence: 4, detection: 3, rpn: 84 })])
    const wrapper = await mountView()

    const { severity } = await openEditAndGetRatings(wrapper)
    await setRating(severity, 10)

    const saveBtn = wrapper.find('[data-testid="btn-save"]')
    await saveBtn.trigger('click')
    await flushPromises()

    expect(updateFmeaMock).toHaveBeenCalledTimes(1)
    const [id, payload] = updateFmeaMock.mock.calls[0]
    expect(id).toBe('f1')
    expect(payload).toMatchObject({ severity: 10, occurrence: 4, detection: 3 })
    expect(payload).not.toHaveProperty('rpn')
  })

  it('blocks an invalid (cleared/out-of-range) rating client-side (no API call, inline error)', async () => {
    seedRecords([makeRecord({ id: 'f1' })])
    const wrapper = await mountView()

    const { severity } = await openEditAndGetRatings(wrapper)
    // Clearing a rating makes it invalid (null) — the client guard must stop
    // the save before any request leaves the browser. (ElInputNumber itself
    // clamps typed 11 -> 10, so the empty-field path is the blocked one.)
    await setRating(severity, null)

    await wrapper.find('[data-testid="btn-save"]').trigger('click')
    await nextTick()

    expect(updateFmeaMock).not.toHaveBeenCalled()
    const err = wrapper.find('[data-testid="dialog-error"]')
    expect(err.exists()).toBe(true)
    expect(err.text()).toMatch(/1 to 10/)
  })

  it('surfaces a server 422 as an inline dialog error and keeps the dialog open', async () => {
    updateFmeaMock.mockRejectedValue({
      response: { status: 422, data: { detail: [{ msg: 'severity must be between 1 and 10' }] } },
    })
    seedRecords([makeRecord({ id: 'f1' })])
    const wrapper = await mountView()

    await openEditAndGetRatings(wrapper)
    await wrapper.find('[data-testid="btn-save"]').trigger('click')
    await flushPromises()
    await nextTick()

    const err = wrapper.find('[data-testid="dialog-error"]')
    expect(err.exists()).toBe(true)
    expect(err.text()).toContain('severity must be between 1 and 10')
    // Dialog stays open so the user can correct.
    expect(wrapper.find('[data-testid="fmea-dialog"]').exists()).toBe(true)
  })

  it('create: New FMEA posts a payload without rpn and uses the server response', async () => {
    createFmeaMock.mockResolvedValue(makeRecord({ id: 'new', rpn: 60 }))
    seedRecords([])
    const wrapper = await mountView()

    await wrapper.find('[data-testid="btn-create"]').trigger('click')
    await nextTick()

    // Fill required text fields (the testid falls through to the native input).
    await wrapper.find('input[data-testid="field-component"]').setValue('RAIL-5V')
    await wrapper.find('input[data-testid="field-mode"]').setValue('No output')

    // Defaults for ratings are 5/5/5 in create mode -> preview 125.
    expect(wrapper.find('[data-testid="rpn-preview"]').text()).toContain('125')

    await wrapper.find('[data-testid="btn-save"]').trigger('click')
    await flushPromises()

    expect(createFmeaMock).toHaveBeenCalledTimes(1)
    const payload = createFmeaMock.mock.calls[0][0] as Record<string, unknown>
    expect(payload).toMatchObject({ component_code: 'RAIL-5V', failure_mode: 'No output' })
    expect(payload).not.toHaveProperty('rpn')
  })

  it('hides write actions for users without system:write scope', async () => {
    hasScopeMock.mockReturnValue(false)
    seedRecords([makeRecord()])
    const wrapper = await mountView()

    expect(wrapper.find('[data-testid="btn-create"]').exists()).toBe(false)
    // No Edit button in the row actions; a read-only hint is shown instead.
    expect(wrapper.text()).toContain('read-only')
  })

  it('renders an error banner when the list call fails', async () => {
    fetchFmeasMock.mockRejectedValue(new Error('Network down'))
    const wrapper = await mountView()
    const banner = wrapper.find('[data-testid="error-alert"]')
    expect(banner.exists()).toBe(true)
    expect(banner.text()).toContain('Network down')
  })
})
