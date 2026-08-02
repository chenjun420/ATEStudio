<script setup lang="ts">
/**
 * SimulationToolbar - 模拟运行 + 回放控制工具栏
 *
 * Two button groups:
 * 1. 模拟运行 (Simulation): el-dropdown with 3 tiers (驱动级/DryRun/全链路)
 *    + noise model selector (Gaussian/Drift/Bias/Full) shown only when
 *    tier === 'full'. Selecting a tier triggers the simulate API.
 * 2. 回放控制 (Replay): el-button-group with 录制/回放/倍速/暂停/停止.
 *    The 倍速 selector is an el-select with 0.5x/1x/2x/5x.
 *
 * Props:
 *   runId:       The active execution run ID. Recording/replay/simulate
 *                are disabled when empty.
 *   showReplay:  Whether to render the replay control group (default true).
 *
 * Emits:
 *   simulate:    Emitted when a simulation run completes successfully.
 *                Payload: the SimulationResponse.
 *   recording:   Emitted when recording starts. Payload: runId.
 *   replay:      Emitted when a replay stream starts. Payload: runId.
 *   diff-ready:  Emitted when a diff is computed. Payload: ReplayDiffResponse.
 *
 * The composables `useSimulation` and `useReplay` own all state and API
 * calls; this component is a thin presentation layer. The composables are
 * injected via props so tests can mock them. When `simulation` or `replay`
 * props are omitted, the component creates its own instances.
 */
import { computed, ref, toRef, watch } from 'vue'
import {
  ElButton,
  ElButtonGroup,
  ElDropdown,
  ElDropdownItem,
  ElDropdownMenu,
  ElIcon,
  ElSelect,
  ElSelectOption,
  ElTooltip,
} from 'element-plus'
import { useSimulation } from '@/composables/useSimulation'
import { useReplay } from '@/composables/useReplay'
import type { SimulationResponse } from '@/api/simulation'
import type { ReplayDiffResponse } from '@/api/simulation'

const props = withDefaults(
  defineProps<{
    /** Active execution run ID. */
    runId: string
    /** Whether to render the replay control group. */
    showReplay?: boolean
    /** Inject a pre-built useSimulation instance (for testing). */
    simulation?: ReturnType<typeof useSimulation>
    /** Inject a pre-built useReplay instance (for testing). */
    replay?: ReturnType<typeof useReplay>
  }>(),
  {
    showReplay: true,
    simulation: undefined,
    replay: undefined,
  },
)

const emit = defineEmits<{
  simulate: [result: SimulationResponse]
  recording: [runId: string]
  replay: [runId: string]
  'diff-ready': [result: ReplayDiffResponse]
}>()

// ── Composables (use injected instances or create our own) ──
const runIdRef = toRef(props, 'runId')
const ownSimulation = useSimulation()
const ownReplay = useReplay(runIdRef)

const sim = props.simulation ?? ownSimulation
const rep = props.replay ?? ownReplay

// ── Local UI state ──
/** Whether the noise-model selector is visible (only for full-chain). */
const noiseSelectorVisible = computed(() => sim.noiseEnabled.value)

/** Whether any action is currently disabled due to no run ID. */
const noRunId = computed(() => !props.runId)

/** Whether the pause button should show "resume" instead. */
const pauseLabel = computed(() => (rep.isPaused.value ? '恢复' : '暂停'))

// ── Actions ──

/**
 * Handle a tier selection from the simulate dropdown.
 * Sets the tier, then triggers the simulate API.
 */
async function handleTierCommand(tier: string): Promise<void> {
  if (tier !== 'driver' && tier !== 'dry_run' && tier !== 'full') return
  sim.tier.value = tier
  await runSimulate()
}

/**
 * Trigger the simulation. Emits 'simulate' on success.
 */
async function runSimulate(): Promise<void> {
  const result = await sim.run(props.runId)
  if (result) {
    emit('simulate', result)
  }
}

/**
 * Start recording. Emits 'recording'.
 */
async function handleStartRecording(): Promise<void> {
  const ok = await rep.startRecordingSession(props.runId)
  if (ok) emit('recording', props.runId)
}

/**
 * Start the streaming replay. Emits 'replay'.
 */
async function handleStartReplay(): Promise<void> {
  const ok = await rep.startReplayStream(props.runId)
  if (ok) emit('replay', props.runId)
}

/**
 * Toggle pause/resume based on current state.
 */
async function handlePauseToggle(): Promise<void> {
  if (rep.isPaused.value) {
    await rep.resumeReplaySession()
  } else {
    await rep.pauseReplaySession()
  }
}

/**
 * Stop the replay (closes the SSE stream).
 */
function handleStopReplay(): void {
  rep.stopReplay()
}

/**
 * Compute a diff between the recorded events and the current replay.
 * Emits 'diff-ready' on success.
 */
async function handleComputeDiff(): Promise<void> {
  if (rep.recordedEvents.value.length === 0) {
    return
  }
  const result = await rep.computeDiff(props.runId, rep.recordedEvents.value)
  if (result) {
    emit('diff-ready', result)
  }
}

// Watch replay completion to auto-fetch the diff for the viewer.
watch(
  () => rep.replayState.value,
  (state) => {
    if (state === 'completed' && rep.recordedEvents.value.length > 0) {
      void handleComputeDiff()
    }
  },
)
</script>

<template>
  <div class="simulation-toolbar" data-testid="simulation-toolbar">
    <!-- ── 模拟运行 (Simulation) ── -->
    <ElDropdown
      trigger="click"
      :disabled="sim.isRunning.value || noRunId"
      data-testid="simulate-dropdown"
      @command="handleTierCommand"
    >
      <ElButton
        :loading="sim.isRunning.value"
        :disabled="noRunId"
        type="primary"
        size="small"
      >
        <ElIcon class="tw-mr-1"><svg viewBox="0 0 24 24" fill="currentColor"><path d="M12 2L4 7v10l8 5 8-5V7l-8-5zm0 2.236L18 8.5v7L12 19.764 6 15.5v-7l6-4.264z"/></svg></ElIcon>
        模拟运行
        <span class="tw-ml-1 tw-text-xs tw-opacity-75">({{ sim.tierLabel.value }})</span>
      </ElButton>
      <template #dropdown>
        <ElDropdownMenu>
          <ElDropdownItem
            v-for="t in sim.tiers"
            :key="t.value"
            :command="t.value"
            :disabled="sim.tier.value === t.value"
          >
            <div class="tw-flex tw-flex-col">
              <span class="tw-font-medium">{{ t.label }}</span>
              <span class="tw-text-xs tw-text-neutral-500">{{ t.description }}</span>
            </div>
          </ElDropdownItem>
        </ElDropdownMenu>
      </template>
    </ElDropdown>

    <!-- Noise model selector (only visible when tier === 'full') -->
    <ElTooltip
      v-if="noiseSelectorVisible"
      content="噪声模型 - 仅全链路仿真可用"
      placement="bottom"
    >
      <ElSelect
        v-model="sim.noiseModel.value"
        size="small"
        class="noise-select"
        data-testid="noise-model-select"
      >
        <ElSelectOption
          v-for="m in sim.noiseModels"
          :key="m.value"
          :label="m.label"
          :value="m.value"
        />
      </ElSelect>
    </ElTooltip>

    <!-- Divider -->
    <div v-if="showReplay" class="tw-w-px tw-h-6 tw-bg-neutral-200 tw-mx-2"></div>

    <!-- ── 回放控制 (Replay) ── -->
    <ElButtonGroup v-if="showReplay" data-testid="replay-controls">
      <!-- 录制 (Start recording) -->
      <ElButton
        size="small"
        :type="rep.isRecording.value ? 'danger' : 'default'"
        :loading="rep.isStartingRecording.value"
        :disabled="noRunId || rep.isRecording.value"
        title="开始录制"
        data-testid="record-button"
        @click="handleStartRecording"
      >
        <svg class="tw-w-3.5 tw-h-3.5" viewBox="0 0 24 24" fill="currentColor">
          <circle cx="12" cy="12" r="6" />
        </svg>
        录制
      </ElButton>

      <!-- 回放 (Start replay) -->
      <ElButton
        size="small"
        type="success"
        :loading="rep.isStartingReplay.value"
        :disabled="noRunId || rep.isReplaying.value"
        title="开始回放"
        data-testid="replay-button"
        @click="handleStartReplay"
      >
        <svg class="tw-w-3.5 tw-h-3.5" fill="currentColor" viewBox="0 0 24 24">
          <path d="M8 5v14l11-7z" />
        </svg>
        回放
      </ElButton>

      <!-- 倍速 (Speed selector) -->
      <ElSelect
        v-model="rep.speed.value"
        size="small"
        class="speed-select"
        :disabled="!rep.isReplaying.value"
        title="回放倍速"
        data-testid="speed-select"
      >
        <ElSelectOption
          v-for="s in rep.speeds"
          :key="s.value"
          :label="s.label"
          :value="s.value"
        />
      </ElSelect>

      <!-- 暂停/恢复 (Pause/Resume) -->
      <ElButton
        size="small"
        :type="rep.isPaused.value ? 'warning' : 'default'"
        :disabled="!rep.isReplaying.value"
        :title="pauseLabel"
        data-testid="pause-button"
        @click="handlePauseToggle"
      >
        <svg v-if="!rep.isPaused.value" class="tw-w-3.5 tw-h-3.5" fill="currentColor" viewBox="0 0 24 24">
          <path d="M6 4h4v16H6V4zm8 0h4v16h-4V4z" />
        </svg>
        <svg v-else class="tw-w-3.5 tw-h-3.5" fill="currentColor" viewBox="0 0 24 24">
          <path d="M8 5v14l11-7z" />
        </svg>
        {{ pauseLabel }}
      </ElButton>

      <!-- 停止 (Stop) -->
      <ElButton
        size="small"
        type="danger"
        :disabled="!rep.isReplaying.value"
        title="停止回放"
        data-testid="stop-button"
        @click="handleStopReplay"
      >
        <svg class="tw-w-3.5 tw-h-3.5" fill="currentColor" viewBox="0 0 24 24">
          <rect x="6" y="6" width="12" height="12" rx="1" />
        </svg>
        停止
      </ElButton>
    </ElButtonGroup>

    <!-- Status indicator -->
    <span
      v-if="showReplay"
      class="tw-ml-2 tw-text-xs tw-text-neutral-500"
      data-testid="replay-status"
    >
      <template v-if="rep.isRecording.value">录制中 ({{ rep.recordedEvents.value.length }} 事件)</template>
      <template v-else-if="rep.replayState.value === 'running'">回放中 ({{ rep.replayEvents.value.length }} 事件)</template>
      <template v-else-if="rep.replayState.value === 'paused'">已暂停</template>
      <template v-else-if="rep.replayState.value === 'completed'">回放完成 ({{ rep.replayEvents.value.length }} 事件)</template>
      <template v-else-if="rep.replayState.value === 'error'">回放错误</template>
      <template v-else-if="sim.isRunning.value">仿真中...</template>
      <template v-else>就绪</template>
    </span>
  </div>
</template>

<style scoped>
.simulation-toolbar {
  display: flex;
  align-items: center;
  gap: 0.5rem;
}

.noise-select {
  width: 110px;
}

.speed-select {
  width: 80px;
}

/* Ensure el-select inputs match button height in the group */
:deep(.speed-select .el-input__wrapper),
:deep(.noise-select .el-input__wrapper) {
  border-radius: 4px;
}
</style>
