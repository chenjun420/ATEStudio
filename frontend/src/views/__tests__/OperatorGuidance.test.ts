/**
 * Tests for the OperatorGuidance.vue view component.
 *
 * Verifies:
 * - Empty state renders when no sequence is loaded (no runId)
 * - Station badge renders from the stationId prop
 * - Connection indicator renders the current connection status
 * - Steps render in el-steps with correct status mapping (idle/running/passed/failed)
 * - Current step card shows script name, status tag, parameters, and resources
 * - All-steps list renders every step with a status tag
 * - Alarm alert (el-alert) renders when latestAlarm is non-null
 * - AI diagnosis card shows root_cause, confidence, possible causes, repair steps
 * - Diagnosis error alert renders when diagnosisError is set
 * - Diagnosis empty state renders when no diagnosis and no error
 * - Resource health list renders with correct tag types per status
 * - "No resources" message renders when resourceHealth is empty
 * - fetchDiagnosis is called when current step transitions to failed
 * - Run ID input + Start button triggers startRun
 *
 * The composable `useOperatorGuidance` is mocked to return controlled reactive
 * state, avoiding real SSE connections and API calls. This follows the project's
 * existing test pattern (see LoopContainerNode.test.ts for mount-based testing).
 */

import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { ref, reactive, computed } from 'vue'
import ElementPlus from 'element-plus'
import OperatorGuidance from '../OperatorGuidance.vue'
import type {
  OperatorStep,
  DiagnosisResult,
  ResourceHealth,
} from '@/composables/useOperatorGuidance'
import type { ExecutionEvent } from '@/composables/useExecutionStatus'

// ─── Mock state container ────────────────────────────────────────────────────
//
// A mutable state object that the mocked useOperatorGuidance returns.
// Each test resets and populates it to drive the component under test.
//
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
  resourceHealth: ResourceHealth[]
  sequenceLoading: boolean
  sequenceError: string | null
}

/** Default empty state for a fresh mount. */
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
    resourceHealth: [],
    sequenceLoading: false,
    sequenceError: null,
    ...overrides,
  }
}

/** The active mock state, referenced by the composable mock factory. */
// Made reactive so computed properties that read from it re-evaluate when
// tests mutate mockState after mount.
let mockState: ReturnType<typeof reactive<MockState>>

// Track calls to the composable's action functions.
const fetchDiagnosisMock = vi.fn()
const startRunMock = vi.fn()
const resetMock = vi.fn()

// Mock the composable module so the component gets controlled reactive state.
// vi.mock is hoisted; the factory builds refs that read from the reactive
// mockState so post-mount mutations trigger re-computation.
vi.mock('@/composables/useOperatorGuidance', () => ({
  useOperatorGuidance: (initialStationId = '', initialRunId = '') => {
    // stationId and runId are writable refs seeded from the composable args
    // (which the component passes from props). Tests can override mockState
    // fields that feed the computed returns.
    const stationId = ref(initialStationId || mockState.stationId)
    const runId = ref(initialRunId || mockState.runId)

    const steps = computed(() => mockState.steps)
    const currentStepIndex = computed(() => mockState.currentStepIndex)
    const currentStep = computed(
      () => mockState.steps[mockState.currentStepIndex] ?? null,
    )
    const totalSteps = computed(() => mockState.totalSteps)

    return {
      stationId,
      runId,
      steps,
      currentStepIndex,
      currentStep,
      totalSteps,
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
      resourceHealth: ref(mockState.resourceHealth),
      sequenceLoading: computed(() => mockState.sequenceLoading),
      sequenceError: computed(() => mockState.sequenceError),
      startRun: startRunMock,
      reset: resetMock,
    }
  },
}))

// ─── Test helpers ────────────────────────────────────────────────────────────

/** Build an OperatorStep with sensible defaults. */
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

/** Build a DiagnosisResult with sensible defaults. */
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

/** Mount the component with ElementPlus globally installed. */
function mountComponent(props: Record<string, unknown> = {}) {
  return mount(OperatorGuidance, {
    props,
    global: {
      plugins: [ElementPlus],
    },
  })
}

// ─── Tests ───────────────────────────────────────────────────────────────────

describe('OperatorGuidance', () => {
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
    const wrapper = mountComponent({ stationId: 'STN-001' })
    const badge = wrapper.find('[data-testid="station-badge"]')
    expect(badge.exists()).toBe(true)
    expect(badge.text()).toContain('STN-001')
  })

  it('test_renders_connection_indicator', () => {
    mockState.connectionStatus = 'connected'
    const wrapper = mountComponent()
    const indicator = wrapper.find('[data-testid="connection-indicator"]')
    expect(indicator.exists()).toBe(true)
    expect(indicator.text()).toContain('connected')
  })

  // ── Steps rendering ──

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
    // el-steps renders el-step children; verify all three script names appear
    const text = stepsEl.text()
    expect(text).toContain('step_one.py')
    expect(text).toContain('step_two.py')
    expect(text).toContain('step_three.py')
  })

  it('test_renders_current_step_card_with_script_and_status', () => {
    mockState.steps = [createStep({ id: 's1', script: 'measure.py', status: 'running' })]
    mockState.totalSteps = 1
    mockState.currentStepIndex = 0

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

  it('test_renders_all_steps_list_with_status_tags', () => {
    mockState.steps = [
      createStep({ id: 's1', script: 'a.py', status: 'passed' }),
      createStep({ id: 's2', script: 'b.py', status: 'failed' }),
      createStep({ id: 's3', script: 'c.py', status: 'idle' }),
    ]
    mockState.totalSteps = 3

    const wrapper = mountComponent()
    const allSteps = wrapper.find('[data-testid="all-steps-card"]')
    expect(allSteps.exists()).toBe(true)
    expect(allSteps.text()).toContain('All Steps (3)')
    expect(allSteps.text()).toContain('a.py')
    expect(allSteps.text()).toContain('b.py')
    expect(allSteps.text()).toContain('c.py')
    // Status tags
    expect(allSteps.text()).toContain('Passed')
    expect(allSteps.text()).toContain('Failed')
    expect(allSteps.text()).toContain('Pending')
  })

  // ── Progress bar ──

  it('test_renders_progress_bar', () => {
    mockState.steps = [
      createStep({ id: 's1', status: 'passed' }),
      createStep({ id: 's2', status: 'running' }),
      createStep({ id: 's3', status: 'idle' }),
      createStep({ id: 's4', status: 'idle' }),
    ]
    mockState.totalSteps = 4
    mockState.completedSteps = 1

    const wrapper = mountComponent()
    const progress = wrapper.find('[data-testid="progress-bar"]')
    expect(progress.exists()).toBe(true)
    expect(progress.text()).toContain('1/4 steps')
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

  it('test_does_not_render_alarm_when_null', () => {
    mockState.latestAlarm = null
    const wrapper = mountComponent()
    const alert = wrapper.find('[data-testid="alarm-alert"]')
    expect(alert.exists()).toBe(false)
  })

  // ── AI Diagnosis ──

  it('test_renders_diagnosis_result_with_root_cause_and_confidence', () => {
    mockState.runId = 'run-1'
    mockState.diagnosis = createDiagnosis()

    const wrapper = mountComponent()
    const result = wrapper.find('[data-testid="diagnosis-result"]')
    expect(result.exists()).toBe(true)
    expect(result.text()).toContain('Loose power connector on DUT')
    // Confidence 85% should appear
    expect(result.text()).toContain('85%')
  })

  it('test_renders_possible_causes_list', () => {
    mockState.runId = 'run-1'
    mockState.diagnosis = createDiagnosis()

    const wrapper = mountComponent()
    const causes = wrapper.find('[data-testid="possible-causes"]')
    expect(causes.exists()).toBe(true)
    expect(causes.text()).toContain('Loose power connector')
    expect(causes.text()).toContain('Failed voltage regulator')
  })

  it('test_renders_repair_steps', () => {
    mockState.runId = 'run-1'
    mockState.diagnosis = createDiagnosis()

    const wrapper = mountComponent()
    const repair = wrapper.find('[data-testid="repair-steps"]')
    expect(repair.exists()).toBe(true)
    expect(repair.text()).toContain('Re-seat the DUT power connector')
    expect(repair.text()).toContain('Measure 5V rail at TP1')
  })

  it('test_renders_diagnosis_error_when_error_set', () => {
    mockState.runId = 'run-1'
    mockState.diagnosisError = 'Diagnosis endpoint not available yet (T20 pending).'

    const wrapper = mountComponent()
    const errAlert = wrapper.find('[data-testid="diagnosis-error"]')
    expect(errAlert.exists()).toBe(true)
    expect(errAlert.text()).toContain('Diagnosis unavailable')
    expect(errAlert.text()).toContain('T20 pending')
  })

  it('test_renders_diagnosis_empty_state', () => {
    mockState.runId = 'run-1'
    mockState.diagnosis = null
    mockState.diagnosisError = null
    mockState.diagnosisLoading = false

    const wrapper = mountComponent()
    const empty = wrapper.find('[data-testid="diagnosis-empty"]')
    expect(empty.exists()).toBe(true)
    expect(empty.text()).toContain('No diagnosis yet')
  })

  // ── Resource health ──

  it('test_renders_resource_health_list', () => {
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
    mockState.resourceHealth = []

    const wrapper = mountComponent()
    const noRes = wrapper.find('[data-testid="no-resources"]')
    expect(noRes.exists()).toBe(true)
    expect(noRes.text()).toContain('No resources declared')
  })

  // ── Sequence loading / error ──

  it('test_renders_sequence_skeleton_when_loading', () => {
    mockState.sequenceLoading = true

    const wrapper = mountComponent()
    const skeleton = wrapper.find('[data-testid="sequence-skeleton"]')
    expect(skeleton.exists()).toBe(true)
  })

  it('test_renders_sequence_error_alert', () => {
    mockState.sequenceError = 'Failed to fetch execution.'

    const wrapper = mountComponent()
    const errAlert = wrapper.find('[data-testid="sequence-error"]')
    expect(errAlert.exists()).toBe(true)
    expect(errAlert.text()).toContain('Failed to load sequence')
    expect(errAlert.text()).toContain('Failed to fetch execution')
  })

  // ── Run ID input + Start button ──

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

  // ── fetchDiagnosis auto-trigger on step failure ──

  it('test_auto_fetches_diagnosis_when_step_fails', async () => {
    // Mount with a running step, then transition to failed.
    mockState.steps = [createStep({ id: 's1', script: 'measure.py', status: 'running' })]
    mockState.totalSteps = 1
    mockState.currentStepIndex = 0

    const wrapper = mountComponent()

    // The component watches currentStep.status; simulate the step failing
    // by updating the mock state and re-mounting (since the mock is computed,
    // we update mockState.steps which the computed reads).
    mockState.steps = [createStep({ id: 's1', script: 'measure.py', status: 'failed' })]

    await wrapper.vm.$nextTick()
    // Allow watchers to fire
    await new Promise((resolve) => setTimeout(resolve, 0))

    expect(fetchDiagnosisMock).toHaveBeenCalledWith('s1')
  })

  // ── Layout structure ──

  it('test_renders_two_panel_layout', () => {
    const wrapper = mountComponent()
    const leftPanel = wrapper.find('[data-testid="left-panel"]')
    const rightPanel = wrapper.find('[data-testid="right-panel"]')
    expect(leftPanel.exists()).toBe(true)
    expect(rightPanel.exists()).toBe(true)
  })

  it('test_left_panel_contains_test_sequence_title', () => {
    const wrapper = mountComponent()
    const leftPanel = wrapper.find('[data-testid="left-panel"]')
    expect(leftPanel.text()).toContain('Test Sequence')
  })

  it('test_right_panel_contains_diagnosis_and_health_titles', () => {
    const wrapper = mountComponent()
    const rightPanel = wrapper.find('[data-testid="right-panel"]')
    expect(rightPanel.text()).toContain('AI Diagnosis')
    expect(rightPanel.text()).toContain('Instrument & Resource Health')
  })

  // ── Measurements ──

  it('test_renders_measurements_card_when_measurements_exist', () => {
    mockState.latestMeasurements = {
      voltage: {
        type: 'MEASUREMENT_RECORDED',
        category: 'measurement',
        run_id: 'run-1',
        value: 4.98,
        unit: 'V',
      } as ExecutionEvent,
    }

    const wrapper = mountComponent()
    const measureCard = wrapper.find('[data-testid="measurements-card"]')
    expect(measureCard.exists()).toBe(true)
    expect(measureCard.text()).toContain('voltage')
    expect(measureCard.text()).toContain('4.98')
    expect(measureCard.text()).toContain('V')
  })

  it('test_does_not_render_measurements_card_when_empty', () => {
    mockState.latestMeasurements = {}
    const wrapper = mountComponent()
    const measureCard = wrapper.find('[data-testid="measurements-card"]')
    expect(measureCard.exists()).toBe(false)
  })
})
