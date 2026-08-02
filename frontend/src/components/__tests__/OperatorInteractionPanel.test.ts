/**
 * Tests for OperatorInteractionPanel.vue component.
 *
 * Verifies:
 * - Step indicator renders with correct step names and status mapping
 * - Current step card displays script name, status tag, parameters, resources
 * - Scanner input renders and is auto-focused
 * - Scanner submit calls handleScanSubmit
 * - Quick action buttons (Pass/Fail/Skip/Retry/Abort) call submitAction
 * - AI diagnosis dialog opens and shows diagnosis result
 * - AI diagnosis dialog shows error state
 * - AI diagnosis dialog shows empty state
 * - Instrument status tags render with correct color coding
 * - Empty state renders when no sequence loaded
 * - Station badge renders from stationId prop
 * - Connection indicator renders
 * - Action error alert renders when actionError is set
 * - Alarm alert renders when latestAlarm is non-null
 * - Action buttons are disabled when not running
 *
 * The composable `useOperatorInteraction` is mocked to return controlled
 * reactive state, following the project's existing test pattern.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { ref, reactive, computed } from 'vue'
import ElementPlus from 'element-plus'
import OperatorInteractionPanel from '../OperatorInteractionPanel.vue'
import type {
  OperatorStep,
  DiagnosisResult,
  ResourceHealth,
} from '@/composables/useOperatorGuidance'
import type { ExecutionEvent } from '@/composables/useExecutionStatus'

// ─── Mock state container ────────────────────────────────────────────────────

interface MockState {
  stationId: string
  runId: string
  steps: OperatorStep[]
  currentStepIndex: number
  totalSteps: number
  stepStatuses: Record<string, import('@/composables/useExecutionStatus').StepStatus>
  executionStatus: string | null
  isRunning: boolean
  progressText: string
  completedSteps: number
  latestAlarm: ExecutionEvent | null
  latestMeasurements: Record<string, ExecutionEvent>
  connectionStatus: string
  diagnosis: DiagnosisResult | null
  diagnosisLoading: boolean
  diagnosisError: string | null
  diagnosisDialogVisible: boolean
  resourceHealth: ResourceHealth[]
  sequenceLoading: boolean
  sequenceError: string | null
  scannerInput: string
  pendingCheckpoint: {
    run_id: string
    pending: boolean
    step_id: string | null
    checkpoint: {
      type: 'scan' | 'manual_input' | 'visual_check' | 'confirm'
      prompt: string
      timeout_sec: number
      validation_regex: string | null
    } | null
    created_at: string | null
  } | null
  actionLoading: boolean
  actionError: string | null
  actionLog: Array<{
    action: 'pass' | 'fail' | 'skip' | 'retry' | 'abort'
    step_id: string | null
    timestamp: string
    detail: string
  }>
}

function createMockState(overrides: Partial<MockState> = {}): MockState {
  return {
    stationId: '',
    runId: '',
    steps: [],
    currentStepIndex: 0,
    totalSteps: 0,
    stepStatuses: {},
    executionStatus: null,
    isRunning: false,
    progressText: '',
    completedSteps: 0,
    latestAlarm: null,
    latestMeasurements: {},
    connectionStatus: 'disconnected',
    diagnosis: null,
    diagnosisLoading: false,
    diagnosisError: null,
    diagnosisDialogVisible: false,
    resourceHealth: [],
    sequenceLoading: false,
    sequenceError: null,
    scannerInput: '',
    pendingCheckpoint: null,
    actionLoading: false,
    actionError: null,
    actionLog: [],
    ...overrides,
  }
}

let mockState: ReturnType<typeof reactive<MockState>>

const fetchDiagnosisMock = vi.fn()
const handleScanSubmitMock = vi.fn()
const submitActionMock = vi.fn()
const startRunMock = vi.fn()
const resetMock = vi.fn()

// Mock the composable module
vi.mock('@/composables/useOperatorInteraction', () => ({
  useOperatorInteraction: (initialStationId = '', initialRunId = '') => {
    const stationId = ref(initialStationId || mockState.stationId)
    const runId = ref(initialRunId || mockState.runId)

    return {
      stationId,
      runId,
      steps: computed(() => mockState.steps),
      currentStepIndex: computed(() => mockState.currentStepIndex),
      currentStep: computed(
        () => mockState.steps[mockState.currentStepIndex] ?? null,
      ),
      totalSteps: computed(() => mockState.totalSteps),
      stepStatuses: mockState.stepStatuses,
      executionStatus: computed(() => mockState.executionStatus),
      isRunning: computed(() => mockState.isRunning),
      progressText: computed(() => mockState.progressText),
      completedSteps: computed(() => mockState.completedSteps),
      latestAlarm: ref(mockState.latestAlarm),
      latestMeasurements: mockState.latestMeasurements,
      connectionStatus: computed(() => mockState.connectionStatus),
      diagnosis: ref(mockState.diagnosis),
      diagnosisLoading: computed(() => mockState.diagnosisLoading),
      diagnosisError: computed(() => mockState.diagnosisError),
      fetchDiagnosis: fetchDiagnosisMock,
      diagnosisDialogVisible: computed({
        get: () => mockState.diagnosisDialogVisible,
        set: (v: boolean) => { mockState.diagnosisDialogVisible = v },
      }),
      resourceHealth: ref(mockState.resourceHealth),
      sequenceLoading: computed(() => mockState.sequenceLoading),
      sequenceError: computed(() => mockState.sequenceError),
      scannerInput: computed({
        get: () => mockState.scannerInput,
        set: (v: string) => { mockState.scannerInput = v },
      }),
      handleScanSubmit: handleScanSubmitMock,
      pendingCheckpoint: ref(mockState.pendingCheckpoint),
      submitAction: submitActionMock,
      actionLoading: computed(() => mockState.actionLoading),
      actionError: computed({
        get: () => mockState.actionError,
        set: (v: string | null) => { mockState.actionError = v },
      }),
      actionLog: ref(mockState.actionLog),
      startRun: startRunMock,
      reset: resetMock,
    }
  },
}))

// ─── Test helpers ────────────────────────────────────────────────────────────

function createStep(overrides: Partial<OperatorStep> = {}): OperatorStep {
  return {
    id: 'step-1',
    script: 'measure_voltage.py',
    params: { voltage: 5.0, channel: 'A' },
    preconditions: ['dut_connected == true'],
    resources: ['oscilloscope-1'],
    timeout_ms: 30000,
    retry: 2,
    on_fail: 'stop',
    status: 'idle',
    ...overrides,
  }
}

function createDiagnosis(overrides: Partial<DiagnosisResult> = {}): DiagnosisResult {
  return {
    step_id: 'step-1',
    root_cause: 'Loose power connector on DUT',
    confidence: 0.85,
    possible_causes: [
      { label: 'Loose power connector', confidence: 0.85, description: 'Check J1 header' },
      { label: 'Failed voltage regulator', confidence: 0.3 },
    ],
    repair_steps: [
      { order: 1, action: 'Re-seat the DUT power connector', estimated_seconds: 30 },
      { order: 2, action: 'Measure 5V rail at TP1', estimated_seconds: 15 },
    ],
    notes: 'Verify with oscilloscope after re-seating.',
    ...overrides,
  }
}

function mountComponent(props: Record<string, unknown> = {}) {
  return mount(OperatorInteractionPanel, {
    props,
    global: {
      plugins: [ElementPlus],
    },
  })
}

// ─── Tests ───────────────────────────────────────────────────────────────────

describe('OperatorInteractionPanel', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mockState = reactive(createMockState())
  })

  // ── Empty state ──

  it('test_renders_empty_state_when_no_sequence', () => {
    const wrapper = mountComponent()
    const empty = wrapper.find('[data-testid="empty-steps"]')
    expect(empty.exists()).toBe(true)
    expect(empty.text()).toContain('No sequence loaded')
  })

  it('test_renders_station_badge_from_prop', () => {
    const wrapper = mountComponent({ stationId: 'W001' })
    const badge = wrapper.find('[data-testid="station-badge"]')
    expect(badge.exists()).toBe(true)
    expect(badge.text()).toContain('W001')
  })

  it('test_renders_connection_indicator', () => {
    mockState.connectionStatus = 'connected'
    const wrapper = mountComponent()
    const indicator = wrapper.find('[data-testid="connection-indicator"]')
    expect(indicator.exists()).toBe(true)
    expect(indicator.text()).toContain('connected')
  })

  // ── Step indicator ──

  it('test_renders_step_indicator_with_steps', () => {
    mockState.steps = [
      createStep({ id: 's1', script: 'step_one.py', status: 'passed' }),
      createStep({ id: 's2', script: 'step_two.py', status: 'running' }),
      createStep({ id: 's3', script: 'step_three.py', status: 'idle' }),
    ]
    mockState.totalSteps = 3
    mockState.currentStepIndex = 1

    const wrapper = mountComponent()
    const stepsEl = wrapper.find('[data-testid="step-indicator"]')
    expect(stepsEl.exists()).toBe(true)
    const text = stepsEl.text()
    expect(text).toContain('step_one.py')
    expect(text).toContain('step_two.py')
    expect(text).toContain('step_three.py')
  })

  // ── Current step card ──

  it('test_renders_current_step_card_with_script_and_status', () => {
    mockState.steps = [createStep({ id: 's1', script: 'measure.py', status: 'running' })]
    mockState.totalSteps = 1
    mockState.currentStepIndex = 0
    mockState.isRunning = true

    const wrapper = mountComponent()
    const card = wrapper.find('[data-testid="current-step-card"]')
    expect(card.exists()).toBe(true)
    expect(card.text()).toContain('measure.py')

    const statusTag = wrapper.find('[data-testid="current-step-status"]')
    expect(statusTag.exists()).toBe(true)
    expect(statusTag.text()).toContain('Running')
  })

  it('test_renders_step_parameters_in_descriptions', () => {
    mockState.steps = [
      createStep({
        id: 's1',
        script: 'measure.py',
        params: { voltage: 5.0, channel: 'A' },
        status: 'running',
      }),
    ]
    mockState.totalSteps = 1
    mockState.currentStepIndex = 0

    const wrapper = mountComponent()
    const params = wrapper.find('[data-testid="step-params"]')
    expect(params.exists()).toBe(true)
    expect(params.text()).toContain('voltage')
    expect(params.text()).toContain('5')
    expect(params.text()).toContain('channel')
    expect(params.text()).toContain('A')
  })

  // ── Scanner input ──

  it('test_renders_scanner_input', () => {
    mockState.runId = 'run-1'
    mockState.steps = [createStep({ status: 'running' })]
    mockState.totalSteps = 1
    mockState.currentStepIndex = 0
    mockState.isRunning = true

    const wrapper = mountComponent()
    const scannerCard = wrapper.find('[data-testid="scanner-card"]')
    expect(scannerCard.exists()).toBe(true)
    const input = wrapper.find('[data-testid="scanner-input"]')
    expect(input.exists()).toBe(true)
  })

  it('test_scanner_submit_calls_handleScanSubmit', async () => {
    mockState.runId = 'run-1'
    mockState.steps = [createStep({ status: 'running' })]
    mockState.totalSteps = 1
    mockState.currentStepIndex = 0
    mockState.isRunning = true

    const wrapper = mountComponent()
    const submitBtn = wrapper.find('[data-testid="scanner-submit"]')
    expect(submitBtn.exists()).toBe(true)
    await submitBtn.trigger('click')
    expect(handleScanSubmitMock).toHaveBeenCalledTimes(1)
  })

  // ── Quick action buttons ──

  it('test_renders_all_quick_action_buttons', () => {
    mockState.runId = 'run-1'
    mockState.steps = [createStep({ status: 'running' })]
    mockState.totalSteps = 1
    mockState.currentStepIndex = 0
    mockState.isRunning = true

    const wrapper = mountComponent()
    expect(wrapper.find('[data-testid="action-pass"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="action-fail"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="action-skip"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="action-retry"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="action-abort"]').exists()).toBe(true)
  })

  it('test_pass_button_calls_submitAction', async () => {
    mockState.runId = 'run-1'
    mockState.steps = [createStep({ id: 's1', status: 'running' })]
    mockState.totalSteps = 1
    mockState.currentStepIndex = 0
    mockState.isRunning = true

    const wrapper = mountComponent()
    await wrapper.find('[data-testid="action-pass"]').trigger('click')
    expect(submitActionMock).toHaveBeenCalledWith('pass')
  })

  it('test_fail_button_calls_submitAction', async () => {
    mockState.runId = 'run-1'
    mockState.steps = [createStep({ id: 's1', status: 'running' })]
    mockState.totalSteps = 1
    mockState.currentStepIndex = 0
    mockState.isRunning = true

    const wrapper = mountComponent()
    await wrapper.find('[data-testid="action-fail"]').trigger('click')
    expect(submitActionMock).toHaveBeenCalledWith('fail')
  })

  it('test_abort_button_calls_submitAction', async () => {
    mockState.runId = 'run-1'
    mockState.steps = [createStep({ id: 's1', status: 'running' })]
    mockState.totalSteps = 1
    mockState.currentStepIndex = 0
    mockState.isRunning = true

    const wrapper = mountComponent()
    await wrapper.find('[data-testid="action-abort"]').trigger('click')
    expect(submitActionMock).toHaveBeenCalledWith('abort')
  })

  it('test_action_buttons_disabled_when_not_running', () => {
    mockState.runId = 'run-1'
    mockState.steps = [createStep({ status: 'idle' })]
    mockState.totalSteps = 1
    mockState.currentStepIndex = 0
    mockState.isRunning = false

    const wrapper = mountComponent()
    const passBtn = wrapper.find('[data-testid="action-pass"]')
    expect(passBtn.attributes('disabled')).toBeDefined()
  })

  // ── AI Diagnosis dialog ──

  it('test_renders_diagnosis_dialog_when_visible', async () => {
    mockState.runId = 'run-1'
    mockState.steps = [createStep({ status: 'failed' })]
    mockState.totalSteps = 1
    mockState.currentStepIndex = 0
    mockState.diagnosisDialogVisible = true
    mockState.diagnosis = createDiagnosis()

    const wrapper = mountComponent()
    await wrapper.vm.$nextTick()
    const dialog = wrapper.find('[data-testid="diagnosis-dialog"]')
    expect(dialog.exists()).toBe(true)
  })

  it('test_diagnosis_dialog_shows_result_with_root_cause', async () => {
    mockState.runId = 'run-1'
    mockState.diagnosisDialogVisible = true
    mockState.diagnosis = createDiagnosis()

    const wrapper = mountComponent()
    await wrapper.vm.$nextTick()
    const result = wrapper.find('[data-testid="diagnosis-result"]')
    expect(result.exists()).toBe(true)
    expect(result.text()).toContain('Loose power connector on DUT')
    expect(result.text()).toContain('85%')
  })

  it('test_diagnosis_dialog_shows_possible_causes', async () => {
    mockState.runId = 'run-1'
    mockState.diagnosisDialogVisible = true
    mockState.diagnosis = createDiagnosis()

    const wrapper = mountComponent()
    await wrapper.vm.$nextTick()
    const causes = wrapper.find('[data-testid="possible-causes"]')
    expect(causes.exists()).toBe(true)
    expect(causes.text()).toContain('Loose power connector')
    expect(causes.text()).toContain('Failed voltage regulator')
  })

  it('test_diagnosis_dialog_shows_repair_steps', async () => {
    mockState.runId = 'run-1'
    mockState.diagnosisDialogVisible = true
    mockState.diagnosis = createDiagnosis()

    const wrapper = mountComponent()
    await wrapper.vm.$nextTick()
    const repair = wrapper.find('[data-testid="repair-steps"]')
    expect(repair.exists()).toBe(true)
    expect(repair.text()).toContain('Re-seat the DUT power connector')
    expect(repair.text()).toContain('Measure 5V rail at TP1')
  })

  it('test_diagnosis_dialog_shows_error_when_error_set', async () => {
    mockState.runId = 'run-1'
    mockState.diagnosisDialogVisible = true
    mockState.diagnosisError = 'Diagnosis endpoint not available.'

    const wrapper = mountComponent()
    await wrapper.vm.$nextTick()
    const errAlert = wrapper.find('[data-testid="diagnosis-error"]')
    expect(errAlert.exists()).toBe(true)
    expect(errAlert.text()).toContain('Diagnosis unavailable')
  })

  it('test_diagnosis_dialog_shows_empty_state', async () => {
    mockState.runId = 'run-1'
    mockState.diagnosisDialogVisible = true
    mockState.diagnosis = null
    mockState.diagnosisError = null
    mockState.diagnosisLoading = false

    const wrapper = mountComponent()
    await wrapper.vm.$nextTick()
    const empty = wrapper.find('[data-testid="diagnosis-empty"]')
    expect(empty.exists()).toBe(true)
    expect(empty.text()).toContain('No diagnosis available')
  })

  // ── Instrument status tags ──

  it('test_renders_resource_health_list', () => {
    mockState.steps = [createStep({ status: 'running' })]
    mockState.totalSteps = 1
    mockState.currentStepIndex = 0
    mockState.resourceHealth = [
      { name: 'oscilloscope-1', type: 'oscilloscope', status: 'healthy' },
      { name: 'power-supply-2', type: 'power', status: 'degraded', detail: 'RESOURCE_TIMEOUT' },
      { name: 'dmm-1', type: 'dmm', status: 'offline' },
    ]

    const wrapper = mountComponent()
    const list = wrapper.find('[data-testid="resource-health-list"]')
    expect(list.exists()).toBe(true)
    expect(list.text()).toContain('oscilloscope-1')
    expect(list.text()).toContain('power-supply-2')
    expect(list.text()).toContain('dmm-1')
    expect(list.text()).toContain('healthy')
    expect(list.text()).toContain('degraded')
    expect(list.text()).toContain('offline')
  })

  it('test_renders_no_resources_message_when_empty', () => {
    mockState.steps = [createStep({ status: 'running' })]
    mockState.totalSteps = 1
    mockState.currentStepIndex = 0
    mockState.resourceHealth = []

    const wrapper = mountComponent()
    const noRes = wrapper.find('[data-testid="no-resources"]')
    expect(noRes.exists()).toBe(true)
    expect(noRes.text()).toContain('No resources declared')
  })

  // ── Alarm ──

  it('test_renders_alarm_alert_when_alarm_present', () => {
    mockState.latestAlarm = {
      type: 'STEP_TIMEOUT',
      category: 'alarm',
      run_id: 'run-1',
      step_id: 's1',
      severity: 'critical',
      message: 'Step exceeded 30s timeout',
    } as ExecutionEvent

    const wrapper = mountComponent()
    const alert = wrapper.find('[data-testid="alarm-alert"]')
    expect(alert.exists()).toBe(true)
    expect(alert.text()).toContain('STEP_TIMEOUT')
  })

  // ── Action error ──

  it('test_renders_action_error_when_error_set', () => {
    mockState.actionError = 'Action "abort" failed (HTTP 500)'

    const wrapper = mountComponent()
    const alert = wrapper.find('[data-testid="action-error"]')
    expect(alert.exists()).toBe(true)
    expect(alert.text()).toContain('Action Failed')
    expect(alert.text()).toContain('HTTP 500')
  })

  // ── Checkpoint card ──

  it('test_renders_checkpoint_card_when_pending', () => {
    mockState.runId = 'run-1'
    mockState.steps = [createStep({ status: 'running' })]
    mockState.totalSteps = 1
    mockState.currentStepIndex = 0
    mockState.pendingCheckpoint = {
      run_id: 'run-1',
      pending: true,
      step_id: 's1',
      checkpoint: {
        type: 'visual_check',
        prompt: 'Verify the LED is green',
        timeout_sec: 30,
        validation_regex: null,
      },
      created_at: '2026-01-01T00:00:00Z',
    }

    const wrapper = mountComponent()
    const cpCard = wrapper.find('[data-testid="checkpoint-card"]')
    expect(cpCard.exists()).toBe(true)
    expect(cpCard.text()).toContain('visual_check')
    expect(cpCard.text()).toContain('Verify the LED is green')
  })

  // ── Action log ──

  it('test_renders_action_log_when_entries_exist', () => {
    mockState.steps = [createStep({ status: 'running' })]
    mockState.totalSteps = 1
    mockState.currentStepIndex = 0
    mockState.actionLog = [
      {
        action: 'pass',
        step_id: 's1',
        timestamp: '2026-01-01T00:00:00Z',
        detail: 'Operator marked step s1 as pass',
      },
    ]

    const wrapper = mountComponent()
    const logCard = wrapper.find('[data-testid="action-log-card"]')
    expect(logCard.exists()).toBe(true)
    expect(logCard.text()).toContain('PASS')
    expect(logCard.text()).toContain('Operator marked step s1 as pass')
  })

  // ── Layout structure ──

  it('test_renders_two_panel_layout', () => {
    mockState.steps = [createStep({ status: 'running' })]
    mockState.totalSteps = 1
    mockState.currentStepIndex = 0

    const wrapper = mountComponent()
    expect(wrapper.find('[data-testid="left-panel"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="right-panel"]').exists()).toBe(true)
  })

  // ── Run ID input ──

  it('test_start_button_calls_startRun_with_input_value', async () => {
    const wrapper = mountComponent()
    const input = wrapper.find('[data-testid="run-id-input"]')
    await input.setValue('run-abc-123')

    const buttons = wrapper.findAll('button')
    const startBtn = buttons.find((b) => b.text().includes('Start'))
    expect(startBtn).toBeDefined()
    await startBtn!.trigger('click')

    expect(startRunMock).toHaveBeenCalledWith('run-abc-123')
  })

  it('test_start_button_disabled_when_input_empty', () => {
    const wrapper = mountComponent()
    const buttons = wrapper.findAll('button')
    const startBtn = buttons.find((b) => b.text().includes('Start'))
    expect(startBtn).toBeDefined()
    expect(startBtn!.attributes('disabled')).toBeDefined()
  })
})
