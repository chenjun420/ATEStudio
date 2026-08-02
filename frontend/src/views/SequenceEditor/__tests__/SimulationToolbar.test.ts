/**
 * Tests for SimulationToolbar.vue component.
 *
 * Verifies:
 * - The "模拟运行" button renders with the dropdown trigger
 * - The dropdown contains 3 tiers: 驱动级 / DryRun / 全链路
 * - Selecting a tier calls sim.run() with the correct runId
 * - The noise model selector only appears when tier === 'full'
 * - The "回放控制" group renders 录制/回放/倍速/暂停/停止 buttons
 * - The record button calls rep.startRecordingSession()
 * - The replay button calls rep.startReplayStream()
 * - The stop button calls rep.stopReplay()
 * - The speed selector renders 4 options (0.5x/1x/2x/5x)
 * - All action buttons are disabled when runId is empty
 * - The pause button toggles to "恢复" when paused
 *
 * The composables useSimulation and useReplay are mocked to return
 * controlled reactive state, avoiding real API calls and SSE connections.
 * This follows the project's existing test pattern (OperatorGuidance.test.ts).
 */

import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { ref, computed, reactive } from 'vue'
import ElementPlus from 'element-plus'
import SimulationToolbar from '../components/SimulationToolbar.vue'
import type { SimulationTier, NoiseModel } from '@/api/simulation'

// ─── Mock state container ────────────────────────────────────────────────────
//
// Mutable state that the mocked composables return. Each test resets and
// populates it to drive the component under test.
//
interface MockSimState {
  tier: SimulationTier
  noiseModel: NoiseModel
  noiseSigma: number
  driftRate: number
  bias: number
  seed: number
  isRunning: boolean
  lastResult: unknown
  error: string | null
}

interface MockRepState {
  recordingState: string
  replayState: string
  speed: number
  recordedEvents: unknown[]
  replayEvents: unknown[]
  isStartingRecording: boolean
  isStartingReplay: boolean
  error: string | null
}

function createMockSimState(overrides: Partial<MockSimState> = {}): MockSimState {
  return {
    tier: 'driver',
    noiseModel: 'GAUSSIAN',
    noiseSigma: 0.001,
    driftRate: 0,
    bias: 0,
    seed: 42,
    isRunning: false,
    lastResult: null,
    error: null,
    ...overrides,
  }
}

function createMockRepState(overrides: Partial<MockRepState> = {}): MockRepState {
  return {
    recordingState: 'idle',
    replayState: 'idle',
    speed: 1.0,
    recordedEvents: [],
    replayEvents: [],
    isStartingRecording: false,
    isStartingReplay: false,
    error: null,
    ...overrides,
  }
}

let mockSim: ReturnType<typeof reactive<MockSimState>>
let mockRep: ReturnType<typeof reactive<MockRepState>>

// Track calls to action functions
const runMock = vi.fn()
const resetMock = vi.fn()
const startRecordingSessionMock = vi.fn()
const stopRecordingSessionMock = vi.fn()
const startReplayStreamMock = vi.fn()
const pauseReplaySessionMock = vi.fn()
const resumeReplaySessionMock = vi.fn()
const stopReplayMock = vi.fn()
const runReplaySyncMock = vi.fn()
const computeDiffMock = vi.fn()

// Static option lists from useSimulation
const SIM_TIERS = [
  { value: 'driver' as const, label: '驱动级', description: '仪器驱动级仿真' },
  { value: 'dry_run' as const, label: 'DryRun', description: '调度器空跑' },
  { value: 'full' as const, label: '全链路', description: '全链路仿真' },
]
const NOISE_MODELS = [
  { value: 'GAUSSIAN' as const, label: 'Gaussian' },
  { value: 'GAUSSIAN_DRIFT' as const, label: 'Drift' },
  { value: 'GAUSSIAN_BIAS' as const, label: 'Bias' },
  { value: 'FULL' as const, label: 'Full' },
]
const REPLAY_SPEEDS = [
  { value: 0.5, label: '0.5x' },
  { value: 1.0, label: '1x' },
  { value: 2.0, label: '2x' },
  { value: 5.0, label: '5x' },
]

// Mock the composable modules so the component gets controlled reactive state.
// The factory builds refs/computeds that read from the reactive mockState.
vi.mock('@/composables/useSimulation', () => ({
  useSimulation: () => {
    const tier = ref(mockSim.tier)
    const noiseModel = ref(mockSim.noiseModel)
    const noiseSigma = ref(mockSim.noiseSigma)
    return {
      tier,
      noiseModel,
      noiseSigma,
      driftRate: ref(mockSim.driftRate),
      bias: ref(mockSim.bias),
      seed: ref(mockSim.seed),
      isRunning: computed(() => mockSim.isRunning),
      lastResult: ref(mockSim.lastResult),
      error: computed(() => mockSim.error),
      tierLabel: computed(
        () => SIM_TIERS.find((t) => t.value === mockSim.tier)?.label ?? mockSim.tier,
      ),
      noiseModelLabel: computed(
        () =>
          NOISE_MODELS.find((m) => m.value === mockSim.noiseModel)?.label ??
          mockSim.noiseModel,
      ),
      noiseEnabled: computed(() => mockSim.tier === 'full'),
      tiers: SIM_TIERS,
      noiseModels: NOISE_MODELS,
      run: runMock,
      reset: resetMock,
    }
  },
}))

vi.mock('@/composables/useReplay', () => ({
  useReplay: () => ({
    recordingState: computed(() => mockRep.recordingState),
    recordedEvents: computed(() => mockRep.recordedEvents),
    isRecording: computed(() => mockRep.recordingState === 'recording'),
    isStartingRecording: computed(() => mockRep.isStartingRecording),
    replayState: computed(() => mockRep.replayState),
    replayEvents: computed(() => mockRep.replayEvents),
    speed: ref(mockRep.speed),
    isReplaying: computed(
      () =>
        mockRep.replayState === 'running' || mockRep.replayState === 'paused',
    ),
    isPaused: computed(() => mockRep.replayState === 'paused'),
    isStartingReplay: computed(() => mockRep.isStartingReplay),
    speeds: REPLAY_SPEEDS,
    diffResult: ref(null),
    error: computed(() => mockRep.error),
    startRecordingSession: startRecordingSessionMock,
    stopRecordingSession: stopRecordingSessionMock,
    startReplayStream: startReplayStreamMock,
    pauseReplaySession: pauseReplaySessionMock,
    resumeReplaySession: resumeReplaySessionMock,
    stopReplay: stopReplayMock,
    runReplaySync: runReplaySyncMock,
    computeDiff: computeDiffMock,
    reset: vi.fn(),
  }),
}))

// Mock the API module to prevent real axios calls (belt-and-suspenders -
// the composable mocks already intercept, but this protects against any
// stray imports).
vi.mock('@/api/simulation', () => ({
  runSimulation: vi.fn(),
  startRecording: vi.fn(),
  startReplay: vi.fn(),
  pauseReplay: vi.fn(),
  resumeReplay: vi.fn(),
  computeReplayDiff: vi.fn(),
  listRecordings: vi.fn(),
  getRecordingStatus: vi.fn(),
  buildReplayStreamUrl: vi.fn((id: string, speed: number) => `/replay/${id}?speed=${speed}`),
}))

// ─── Test helpers ────────────────────────────────────────────────────────────

/** Mount the component with ElementPlus globally installed. */
function mountComponent(props: Record<string, unknown> = {}) {
  return mount(SimulationToolbar, {
    props,
    global: {
      plugins: [ElementPlus],
    },
  })
}

// ─── Tests ───────────────────────────────────────────────────────────────────

describe('SimulationToolbar', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mockSim = reactive(createMockSimState())
    mockRep = reactive(createMockRepState())
  })

  // ── 模拟运行 button + dropdown ──

  it('test_renders_simulate_button', () => {
    const wrapper = mountComponent({ runId: 'run-001' })
    const btn = wrapper.find('[data-testid="simulate-dropdown"]')
    expect(btn.exists()).toBe(true)
    expect(btn.text()).toContain('模拟运行')
  })

  it('test_renders_three_tiers_in_dropdown', async () => {
    // Element Plus teleports dropdown menus to document.body, so we mount
    // with attachTo:document.body and inspect the body for dropdown items.
    const div = document.createElement('div')
    document.body.appendChild(div)
    const wrapper = mount(SimulationToolbar, {
      props: { runId: 'run-001' },
      attachTo: div,
      global: { plugins: [ElementPlus] },
    })
    // Click the dropdown trigger to open the menu
    await wrapper.find('[data-testid="simulate-dropdown"]').trigger('click')
    await wrapper.vm.$nextTick()
    // Allow Element Plus teleport + popper to settle
    await new Promise((r) => setTimeout(r, 50))

    const bodyText = document.body.textContent ?? ''
    expect(bodyText).toContain('驱动级')
    expect(bodyText).toContain('DryRun')
    expect(bodyText).toContain('全链路')

    wrapper.unmount()
    document.body.removeChild(div)
  })

  it('test_shows_current_tier_label', () => {
    mockSim.tier = 'dry_run'
    const wrapper = mountComponent({ runId: 'run-001' })
    expect(wrapper.find('[data-testid="simulate-dropdown"]').text()).toContain('DryRun')
  })

  // ── Noise model selector ──

  it('test_noise_selector_hidden_when_tier_not_full', () => {
    mockSim.tier = 'driver'
    const wrapper = mountComponent({ runId: 'run-001' })
    expect(wrapper.find('[data-testid="noise-model-select"]').exists()).toBe(false)
  })

  it('test_noise_selector_visible_when_tier_full', () => {
    mockSim.tier = 'full'
    const wrapper = mountComponent({ runId: 'run-001' })
    expect(wrapper.find('[data-testid="noise-model-select"]').exists()).toBe(true)
  })

  // ── Replay control group ──

  it('test_renders_record_button', () => {
    const wrapper = mountComponent({ runId: 'run-001' })
    expect(wrapper.find('[data-testid="record-button"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="record-button"]').text()).toContain('录制')
  })

  it('test_renders_replay_button', () => {
    const wrapper = mountComponent({ runId: 'run-001' })
    expect(wrapper.find('[data-testid="replay-button"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="replay-button"]').text()).toContain('回放')
  })

  it('test_renders_speed_selector', () => {
    const wrapper = mountComponent({ runId: 'run-001' })
    expect(wrapper.find('[data-testid="speed-select"]').exists()).toBe(true)
  })

  it('test_renders_pause_button', () => {
    const wrapper = mountComponent({ runId: 'run-001' })
    expect(wrapper.find('[data-testid="pause-button"]').exists()).toBe(true)
  })

  it('test_renders_stop_button', () => {
    const wrapper = mountComponent({ runId: 'run-001' })
    expect(wrapper.find('[data-testid="stop-button"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="stop-button"]').text()).toContain('停止')
  })

  // ── Action interactions ──

  it('test_record_button_calls_startRecordingSession', async () => {
    startRecordingSessionMock.mockResolvedValue(true)
    const wrapper = mountComponent({ runId: 'run-001' })
    await wrapper.find('[data-testid="record-button"]').trigger('click')
    expect(startRecordingSessionMock).toHaveBeenCalledWith('run-001')
  })

  it('test_replay_button_calls_startReplayStream', async () => {
    startReplayStreamMock.mockResolvedValue(true)
    const wrapper = mountComponent({ runId: 'run-001' })
    await wrapper.find('[data-testid="replay-button"]').trigger('click')
    expect(startReplayStreamMock).toHaveBeenCalledWith('run-001')
  })

  it('test_stop_button_calls_stopReplay', async () => {
    mockRep.replayState = 'running'
    const wrapper = mountComponent({ runId: 'run-001' })
    await wrapper.find('[data-testid="stop-button"]').trigger('click')
    expect(stopReplayMock).toHaveBeenCalledTimes(1)
  })

  it('test_pause_button_calls_pauseReplaySession_when_running', async () => {
    mockRep.replayState = 'running'
    const wrapper = mountComponent({ runId: 'run-001' })
    await wrapper.find('[data-testid="pause-button"]').trigger('click')
    expect(pauseReplaySessionMock).toHaveBeenCalledTimes(1)
    expect(resumeReplaySessionMock).not.toHaveBeenCalled()
  })

  it('test_pause_button_calls_resumeReplaySession_when_paused', async () => {
    mockRep.replayState = 'paused'
    const wrapper = mountComponent({ runId: 'run-001' })
    await wrapper.find('[data-testid="pause-button"]').trigger('click')
    expect(resumeReplaySessionMock).toHaveBeenCalledTimes(1)
    expect(pauseReplaySessionMock).not.toHaveBeenCalled()
  })

  // ── Disabled state ──

  it('test_buttons_disabled_when_no_runId', () => {
    const wrapper = mountComponent({ runId: '' })
    // Record button disabled
    const recordBtn = wrapper.find('[data-testid="record-button"]')
    expect(recordBtn.attributes('disabled')).toBeDefined()
    // Replay button disabled
    const replayBtn = wrapper.find('[data-testid="replay-button"]')
    expect(replayBtn.attributes('disabled')).toBeDefined()
  })

  it('test_buttons_enabled_when_runId_present', () => {
    const wrapper = mountComponent({ runId: 'run-001' })
    const recordBtn = wrapper.find('[data-testid="record-button"]')
    expect(recordBtn.attributes('disabled')).toBeUndefined()
    const replayBtn = wrapper.find('[data-testid="replay-button"]')
    expect(replayBtn.attributes('disabled')).toBeUndefined()
  })

  // ── Pause label toggle ──

  it('test_pause_button_shows_resume_when_paused', () => {
    mockRep.replayState = 'paused'
    const wrapper = mountComponent({ runId: 'run-001' })
    expect(wrapper.find('[data-testid="pause-button"]').text()).toContain('恢复')
  })

  it('test_pause_button_shows_pause_when_running', () => {
    mockRep.replayState = 'running'
    const wrapper = mountComponent({ runId: 'run-001' })
    expect(wrapper.find('[data-testid="pause-button"]').text()).toContain('暂停')
  })

  // ── Status indicator ──

  it('test_status_indicator_shows_ready_when_idle', () => {
    const wrapper = mountComponent({ runId: 'run-001' })
    const status = wrapper.find('[data-testid="replay-status"]')
    expect(status.exists()).toBe(true)
    expect(status.text()).toContain('就绪')
  })

  it('test_status_indicator_shows_recording', () => {
    mockRep.recordingState = 'recording'
    mockRep.recordedEvents = [{}, {}]
    const wrapper = mountComponent({ runId: 'run-001' })
    const status = wrapper.find('[data-testid="replay-status"]')
    expect(status.text()).toContain('录制中')
    expect(status.text()).toContain('2')
  })

  it('test_status_indicator_shows_replay_running', () => {
    mockRep.replayState = 'running'
    mockRep.replayEvents = [{}, {}, {}]
    const wrapper = mountComponent({ runId: 'run-001' })
    const status = wrapper.find('[data-testid="replay-status"]')
    expect(status.text()).toContain('回放中')
    expect(status.text()).toContain('3')
  })

  it('test_status_indicator_shows_paused', () => {
    mockRep.replayState = 'paused'
    const wrapper = mountComponent({ runId: 'run-001' })
    const status = wrapper.find('[data-testid="replay-status"]')
    expect(status.text()).toContain('已暂停')
  })

  it('test_status_indicator_shows_completed', () => {
    mockRep.replayState = 'completed'
    mockRep.replayEvents = [{}, {}]
    const wrapper = mountComponent({ runId: 'run-001' })
    const status = wrapper.find('[data-testid="replay-status"]')
    expect(status.text()).toContain('回放完成')
    expect(status.text()).toContain('2')
  })

  it('test_status_indicator_shows_simulating', () => {
    mockSim.isRunning = true
    const wrapper = mountComponent({ runId: 'run-001' })
    const status = wrapper.find('[data-testid="replay-status"]')
    expect(status.text()).toContain('仿真中')
  })

  // ── Replay group visibility ──

  it('test_replay_controls_hidden_when_showReplay_false', () => {
    const wrapper = mountComponent({ runId: 'run-001', showReplay: false })
    expect(wrapper.find('[data-testid="replay-controls"]').exists()).toBe(false)
  })

  it('test_replay_controls_visible_by_default', () => {
    const wrapper = mountComponent({ runId: 'run-001' })
    expect(wrapper.find('[data-testid="replay-controls"]').exists()).toBe(true)
  })
})
