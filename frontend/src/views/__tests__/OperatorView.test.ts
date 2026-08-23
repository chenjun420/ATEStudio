/**
 * Tests for OperatorView.vue — 操作员控制台（T42，v41-gap-analysis #42）。
 *
 * Verifies:
 * - Renders the operator-view root container + station tag from route param
 * - Run status card: active run rendered from polled executions list
 * - Pending checkpoint list rendering + 确认 dialog flow (operator + note)
 * - Checkpoint advance gated: start-gate banner while pending exists
 * - Ack success moves item to completed history
 * - Completion signature captured after terminal run with no pending
 * - Offline banner shown after consecutive poll failures
 * - Read-only mode: no admin actions exposed
 *
 * The @/api/executions module is mocked to isolate the view test.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { nextTick } from 'vue'
import ElementPlus from 'element-plus'
import { createRouter, createMemoryHistory, type Router } from 'vue-router'
import OperatorView from '../OperatorView.vue'
import type { ExecutionSearchResponse } from '@/api/executions'
import type { PendingCheckpointResponse } from '@/utils/checkpointFlow'

// ─── Mock @/api/executions ───────────────────────────────────────────────────

const listExecutionsMock = vi.fn<() => Promise<ExecutionSearchResponse>>()
const fetchPendingCheckpointMock = vi.fn<() => Promise<PendingCheckpointResponse>>()
const acknowledgeRunCheckpointMock = vi.fn<
  (runId: string, payload: unknown) => Promise<{ operator: string; note: string | null; acknowledged_at: string }>
>()

vi.mock('@/api/executions', () => ({
  listExecutions: (...args: unknown[]) => listExecutionsMock(...(args as [])),
  fetchPendingCheckpoint: (...args: unknown[]) => fetchPendingCheckpointMock(...(args as [])),
  acknowledgeRunCheckpoint: (...args: unknown[]) =>
    acknowledgeRunCheckpointMock(...(args as [string, unknown])),
}))

// ─── Fixtures ────────────────────────────────────────────────────────────────

function runningRun(): ExecutionSearchResponse {
  return {
    items: [
      {
        id: 'run-42',
        sequence_id: 'seq-1',
        status: 'RUNNING',
        dut_serial: 'DUT-001',
        product_type: 'AP-01',
        started_at: '2026-08-24T01:00:00Z',
        completed_at: null,
        pass_rate: null,
        error: null,
      },
    ],
    total: 1,
    skip: 0,
    limit: 10,
  }
}

function emptyRuns(): ExecutionSearchResponse {
  return { items: [], total: 0, skip: 0, limit: 10 }
}

function pendingPayload(pending = true): PendingCheckpointResponse {
  return {
    run_id: 'run-42',
    pending,
    step_id: pending ? 'step-cp-1' : null,
    checkpoint: pending
      ? { type: 'confirm', prompt: '确认工装就绪', timeout_sec: 30, validation_regex: null }
      : null,
    created_at: pending ? '2026-08-24T02:00:00Z' : null,
  }
}

// ─── Helpers ─────────────────────────────────────────────────────────────────

function createRouterWithParam(stationId: string): Router {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      {
        path: '/operator/:station_id',
        name: 'OperatorView',
        component: OperatorView,
        props: true,
      },
      { path: '/', redirect: '/operator/test' },
    ],
  })
  void stationId
  return router
}

async function mountView(stationId = 'w001') {
  const router = createRouterWithParam(stationId)
  await router.push(`/operator/${stationId}`)
  await router.isReady()
  const wrapper = mount(OperatorView, {
    global: { plugins: [router, ElementPlus] },
    attachTo: document.body,
  })
  await flushPromises()
  return wrapper
}

type Exposed = {
  checkpoints: unknown[]
  consecutiveFailures: number
  signature: unknown
  activeRunId: string | null
  lastRunId: string | null
  openAckDialog: (item: unknown) => void
  applyManualRunId: () => void
  pollRuns: () => Promise<void>
  pollPending: () => Promise<void>
}

function exposed(wrapper: ReturnType<typeof mount>): Exposed {
  return wrapper.vm as unknown as Exposed
}

/** defineExpose 自动解包 ref —— 直接赋值即可驱动内部状态。 */
function trackRun(wrapper: ReturnType<typeof mount>, runId: string): void {
  ;(wrapper.vm as unknown as { activeRunId: string }).activeRunId = runId
}

// ─── Tests ───────────────────────────────────────────────────────────────────

describe('OperatorView', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    listExecutionsMock.mockResolvedValue(emptyRuns())
    fetchPendingCheckpointMock.mockResolvedValue(pendingPayload(false))
    acknowledgeRunCheckpointMock.mockResolvedValue({
      operator: '张三',
      note: null,
      acknowledged_at: '2026-08-24T05:00:00Z',
    })
  })

  it('test_renders_operator_view_container_and_station_tag', async () => {
    const wrapper = await mountView('STN-42')
    expect(wrapper.find('[data-testid="operator-view"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="station-tag"]').text()).toContain('STN-42')
    wrapper.unmount()
  })

  it('test_run_status_card_shows_active_run_from_poll', async () => {
    listExecutionsMock.mockResolvedValue(runningRun())
    const wrapper = await mountView()
    await exposed(wrapper).pollRuns()
    await flushPromises()
    expect(wrapper.find('[data-testid="run-id"]').text()).toContain('run-42')
    expect(wrapper.find('[data-testid="run-status"]').text()).toContain('RUNNING')
    expect(wrapper.text()).toContain('DUT-001')
    wrapper.unmount()
  })

  it('test_run_empty_state_when_no_runs', async () => {
    const wrapper = await mountView()
    expect(wrapper.find('[data-testid="run-empty"]').exists()).toBe(true)
    wrapper.unmount()
  })

  it('test_pending_checkpoint_listed_after_poll', async () => {
    fetchPendingCheckpointMock.mockResolvedValue(pendingPayload(true))
    const wrapper = await mountView()
    // 手动跟踪 run-42 后轮询待确认检查点
    ;trackRun(wrapper, 'run-42')
    await exposed(wrapper).pollPending()
    await flushPromises()
    expect(wrapper.find('[data-testid="pending-list"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="pending-item"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('确认工装就绪')
    wrapper.unmount()
  })

  it('test_pending_empty_state_when_none', async () => {
    const wrapper = await mountView()
    expect(wrapper.find('[data-testid="pending-empty"]').exists()).toBe(true)
    wrapper.unmount()
  })

  it('test_checkpoint_advance_gated_with_reason_while_pending', async () => {
    fetchPendingCheckpointMock.mockResolvedValue(pendingPayload(true))
    const wrapper = await mountView()
    ;trackRun(wrapper, 'run-42')
    await exposed(wrapper).pollPending()
    await flushPromises()
    const gate = wrapper.find('[data-testid="start-gate"]')
    expect(gate.exists()).toBe(true)
    expect(gate.text()).toContain('未确认检查点')
    wrapper.unmount()
  })

  it('test_ack_dialog_rejects_empty_operator_without_posting', async () => {
    fetchPendingCheckpointMock.mockResolvedValue(pendingPayload(true))
    const wrapper = await mountView()
    const ex = exposed(wrapper)
    ex.openAckDialog(ex.checkpoints[0])
    await flushPromises()
    expect(wrapper.find('[data-testid="ack-dialog"]').exists()).toBe(true)

    const opInput = wrapper.find('[data-testid="ack-operator-input"]')
    await opInput.setValue('')
    await wrapper.find('[data-testid="ack-confirm-btn"]').trigger('click')
    await flushPromises()
    expect(acknowledgeRunCheckpointMock).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('test_ack_success_moves_item_to_completed_history', async () => {
    fetchPendingCheckpointMock.mockResolvedValue(pendingPayload(true))
    acknowledgeRunCheckpointMock.mockResolvedValue({
      operator: '张三',
      note: '工装正常',
      acknowledged_at: '2026-08-24T05:00:00Z',
    })
    const wrapper = await mountView()
    const ex = exposed(wrapper)
    ;trackRun(wrapper, 'run-42')
    await ex.pollPending()
    await flushPromises()

    ex.openAckDialog(ex.checkpoints[0])
    await flushPromises()
    const opInput = wrapper.find('[data-testid="ack-operator-input"]')
    await opInput.setValue('张三')
    const noteInput = wrapper.find('[data-testid="ack-note-input"]')
    await noteInput.setValue('工装正常')
    await wrapper.find('[data-testid="ack-confirm-btn"]').trigger('click')
    await flushPromises()

    expect(acknowledgeRunCheckpointMock).toHaveBeenCalledWith('run-42', {
      step_id: 'step-cp-1',
      operator: '张三',
      note: '工装正常',
    })
    expect(wrapper.find('[data-testid="history-item"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="history-item"]').text()).toContain('张三')
    expect(wrapper.find('[data-testid="pending-item"]').exists()).toBe(false)
    wrapper.unmount()
  })

  it('test_offline_banner_after_consecutive_failures', async () => {
    listExecutionsMock.mockRejectedValue(new Error('network down'))
    const wrapper = await mountView()
    const ex = exposed(wrapper)
    ex.consecutiveFailures = 3
    await nextTick()
    expect(wrapper.find('[data-testid="offline-banner"]').exists()).toBe(true)
    wrapper.unmount()
  })

  it('test_no_offline_banner_below_threshold', async () => {
    const wrapper = await mountView()
    exposed(wrapper).consecutiveFailures = 2
    await nextTick()
    expect(wrapper.find('[data-testid="offline-banner"]').exists()).toBe(false)
    wrapper.unmount()
  })

  it('test_completion_signature_captured_on_terminal_run', async () => {
    listExecutionsMock.mockResolvedValue({
      items: [
        {
          id: 'run-42',
          sequence_id: 'seq-1',
          status: 'COMPLETED',
          dut_serial: 'DUT-001',
          product_type: null,
          started_at: '2026-08-24T01:00:00Z',
          completed_at: '2026-08-24T04:00:00Z',
          pass_rate: 100,
          error: null,
        },
      ],
      total: 1,
      skip: 0,
      limit: 10,
    })
    const wrapper = await mountView()
    const ex = exposed(wrapper)
    await ex.pollRuns()
    await flushPromises()
    // 操作员此前已跟踪 run-42（运行结束后仍保持跟踪）
    trackRun(wrapper, 'run-42')
    await flushPromises()

    expect(wrapper.find('[data-testid="signature-card"]').exists()).toBe(true)
    const signInput = wrapper.find('[data-testid="sign-input"]')
    await signInput.setValue('李四')
    await wrapper.find('[data-testid="sign-btn"]').trigger('click')
    await flushPromises()

    expect(ex.signature).toEqual({
      run_id: 'run-42',
      operator: '李四',
      signed_at: expect.any(String),
    })
    expect(wrapper.find('[data-testid="signature-done"]').text()).toContain('李四')
    wrapper.unmount()
  })

  it('test_signature_card_hidden_while_running', async () => {
    listExecutionsMock.mockResolvedValue(runningRun())
    const wrapper = await mountView()
    await exposed(wrapper).pollRuns()
    await flushPromises()
    expect(wrapper.find('[data-testid="signature-card"]').exists()).toBe(false)
    wrapper.unmount()
  })

  it('test_read_only_mode_no_admin_actions', async () => {
    const wrapper = await mountView()
    const text = wrapper.text()
    expect(text).not.toContain('中止运行')
    expect(text).not.toContain('删除')
    expect(text).not.toContain('编辑序列')
    expect(text).not.toContain('Abort')
    wrapper.unmount()
  })

  it('test_dut_swap_banner_between_runs', async () => {
    const wrapper = await mountView()
    const vm = exposed(wrapper)
    vm.lastRunId = 'run-old'
    vm.activeRunId = 'run-new'
    await nextTick()
    expect(wrapper.find('[data-testid="dut-swap-banner"]').exists()).toBe(true)
    wrapper.unmount()
  })
})
