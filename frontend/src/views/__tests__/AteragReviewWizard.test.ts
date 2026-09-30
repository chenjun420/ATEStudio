/**
 * Tests for the ATERag review wizard (AteragReviewWizard.vue).
 *
 * Covers the three things the review screen must get right, each of which was
 * wrong in a way that failed silently rather than loudly:
 *
 * 1. **Step 1 names what it actually does.** It reads "导入抽取结果", not
 *    "上传规格". The old name promised a control that does not exist here:
 *    specs are uploaded to ATERag, and by the time anything reaches this
 *    wizard extraction has already run. An engineer looking for a file input
 *    for a .md document found none, and the step looked broken rather than
 *    misplaced.
 *
 * 2. **The spec clause is on screen while signing.** Signing means confirming
 *    the clause says what the extracted condition claims. A distilled fragment
 *    can invert the original's meaning, so the comparison needs the original
 *    next to it. Without the panel the signature is taken on a blank form.
 *
 * 3. **Hand-written clauses are marked.** `annotation_draft` marks conditions
 *    a person wrote rather than the rules cut. Those are the criteria the red
 *    line is about — unapproved, and not usable as production bounds — and
 *    with the marker missing they render like any other row.
 *
 * The component is mounted with the API module mocked: these tests are about
 * what the screen shows a reviewer, not about the transport.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import AteragReviewWizard from '../AteragReviewWizard.vue'

const fetchConditions = vi.fn()
const fetchConditionSummary = vi.fn()
const approveConditions = vi.fn()
const importBundle = vi.fn()
const planBundle = vi.fn()

vi.mock('@/api/testConditions', async (orig) => {
  const actual = await orig<typeof import('@/api/testConditions')>()
  return {
    ...actual,
    fetchConditions: (...a: unknown[]) => fetchConditions(...a),
    fetchConditionSummary: (...a: unknown[]) => fetchConditionSummary(...a),
    approveConditions: (...a: unknown[]) => approveConditions(...a),
  }
})

vi.mock('@/api/ateragImport', async (orig) => {
  const actual = await orig<typeof import('@/api/ateragImport')>()
  return {
    ...actual,
    importBundle: (...a: unknown[]) => importBundle(...a),
    planBundle: (...a: unknown[]) => planBundle(...a),
  }
})

/** One extracted condition, as the list endpoint returns it. */
function condition(overrides: Record<string, unknown> = {}) {
  return {
    id: 'c1',
    owner_type: 'requirement',
    owner_id: 'r1',
    side: 'output',
    kind: 'timing',
    text: '低温下限启动: 开机输出延时≤12s',
    value: { max: 12, unit: 's' },
    source: 'notes',
    confidence: 'proposed',
    status: 'draft',
    method_ref: null,
    cond_fingerprint: 'fp1',
    created_at: '2026-09-30T00:00:00Z',
    updated_at: '2026-09-30T00:00:00Z',
    requirement_code: 'SR-1',
    requirement_title: '开机输出延迟',
    section_path: '4.3.1',
    requirement_description: '在 -25℃ 低温下限启动时, 开机输出延时应不大于 12s',
    requirement_notes: '启动过程中允许跌落',
    requirement_flags: [] as string[],
    requirement_assessment: {},
    ...overrides,
  }
}

const summaryRow = {
  requirement_id: 'r1',
  requirement_code: 'SR-1',
  title: '开机输出延迟',
  section_path: '4.3.1',
  requirement_status: 'active',
  draft: 1,
  approved: 0,
  total: 1,
}

/**
 * Mount with the real Element Plus plugin.
 *
 * Without it the child components never resolve, `el-table` renders no rows,
 * and `ElSelect` throws "maximum recursive updates" — which reads like a
 * component bug when it is only a missing plugin.
 */
function mountWizard() {
  return mount(AteragReviewWizard, { global: { plugins: [ElementPlus] } })
}

/** Mount and walk to step 3 (条件评审) with the given conditions loaded. */
async function mountAtReviewStep(items: ReturnType<typeof condition>[]): Promise<{
  wrapper: ReturnType<typeof mountWizard>
  text: () => string
}> {
  fetchConditionSummary.mockResolvedValue({ items: [summaryRow], pending_total: 1 })
  fetchConditions.mockResolvedValue({ items, total: items.length })

  const wrapper = mountWizard()
  await flushPromises()

  // Step 1 needs a bundle + dry-run, step 4 needs a plan. Advancing the
  // wizard is not what these tests are about, so drive the step index
  // directly rather than reproducing the whole import flow.
  const vm = wrapper.vm as unknown as { step: number; openRequirement: (r: unknown) => Promise<void> }
  vm.step = 2
  await vm.openRequirement(summaryRow)
  await flushPromises()

  return { wrapper, text: () => wrapper.text() }
}

beforeEach(() => {
  vi.clearAllMocks()
  importBundle.mockResolvedValue({
    contract_hash: 'x',
    contract_ok: true,
    requirements: { created: 0, updated: 0 },
    conditions: { created: 0, updated: 0 },
    cases: { created: 0, updated: 0 },
    limits: { created: 0, updated: 0 },
    conflicts: [],
    removed: [],
  })
  planBundle.mockResolvedValue({ segments: 1, steps: 1, pending: [], gaps: [] })
})

describe('step 1 names what it actually does', () => {
  it('does not claim to accept a spec document', async () => {
    const wrapper = mountWizard()
    await flushPromises()

    const body = wrapper.text()
    // The old label promised a control this screen does not have.
    expect(body).not.toContain('上传规格')
    expect(body).toContain('导入抽取结果')
  })

  it('says where the spec is actually uploaded', async () => {
    const wrapper = mountWizard()
    await flushPromises()

    // Without this the step is a dead end: the engineer has a .md file, this
    // screen wants a bundle, and nothing on screen says so.
    const body = wrapper.text()
    expect(body).toContain('ingest_document')
    expect(body).toContain('studio_bundle.json')
  })
})

describe('signing has the spec clause in view', () => {
  it('shows the clause the conditions were extracted from', async () => {
    const { text } = await mountAtReviewStep([condition()])
    const body = text()
    expect(body).toContain('规格书原文')
    expect(body).toContain('在 -25℃ 低温下限启动时, 开机输出延时应不大于 12s')
  })

  it('shows the spec note when there is one', async () => {
    const { text } = await mountAtReviewStep([condition()])
    expect(text()).toContain('原文备注')
    expect(text()).toContain('启动过程中允许跌落')
  })

  it('says so plainly when a requirement has no clause text', async () => {
    const { text } = await mountAtReviewStep([
      condition({ requirement_description: null, requirement_notes: null }),
    ])
    // Silently rendering an empty box reads as "nothing to check", which is
    // exactly the state where a signature is meaningless.
    expect(text()).toContain('该需求没有原文描述')
  })
})

describe('hand-written clauses are marked as such', () => {
  it('marks a condition from an annotation draft', async () => {
    const { text } = await mountAtReviewStep([
      condition({ requirement_flags: ['annotation_draft'] }),
    ])
    const body = text()
    expect(body).toContain('人工注记')
    // The reason matters as much as the marker: a bare tag reads as a
    // category, not as a red line.
    expect(body).toContain('未经签字不得作为判据')
  })

  it('leaves rule-cut conditions unmarked', async () => {
    const { text } = await mountAtReviewStep([condition({ requirement_flags: [] })])
    expect(text()).not.toContain('人工注记')
  })

  it('marks per row, so a mixed requirement shows only the hand-written ones', async () => {
    // A requirement can carry both: some clauses cut by rule, some written by
    // a person. Marking the whole requirement would be wrong in the other
    // direction — it would imply the rule-cut clauses are also unapproved.
    const { text } = await mountAtReviewStep([
      condition({ id: 'c1', kind: 'timing', requirement_flags: ['annotation_draft'] }),
      condition({ id: 'c2', kind: 'output_voltage', text: '输出电压 54V', requirement_flags: [] }),
    ])
    const body = text()
    expect(body).toContain('开机输出延时≤12s')
    expect(body).toContain('输出电压 54V')
    // Exactly one marker for two rows.
    expect(body.split('人工注记').length - 1).toBe(1)
  })
})
