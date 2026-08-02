import { computed, ref } from 'vue'
import { ElMessage } from 'element-plus'
import {
  runSimulation,
  type NoiseModel,
  type SimulationResponse,
  type SimulationTier,
} from '@/api/simulation'

/**
 * Available simulation tiers for the dropdown.
 * Labels use Chinese to match the SequenceEditor UI domain language.
 */
export interface SimulationTierOption {
  value: SimulationTier
  label: string
  /** Short description shown in the dropdown. */
  description: string
}

export const SIMULATION_TIERS: readonly SimulationTierOption[] = [
  {
    value: 'driver',
    label: '驱动级',
    description: '仪器驱动级仿真 - SIM 驱动器替代真实仪器',
  },
  {
    value: 'dry_run',
    label: 'DryRun',
    description: '调度器空跑 - 完整调度图遍历，无真实执行',
  },
  {
    value: 'full',
    label: '全链路',
    description: '全链路仿真 - 驱动仿真 + 调度空跑 + 噪声模型',
  },
] as const

/**
 * Available noise models for the full-chain tier.
 */
export interface NoiseModelOption {
  value: NoiseModel
  label: string
}

export const NOISE_MODELS: readonly NoiseModelOption[] = [
  { value: 'GAUSSIAN', label: 'Gaussian' },
  { value: 'GAUSSIAN_DRIFT', label: 'Drift' },
  { value: 'GAUSSIAN_BIAS', label: 'Bias' },
  { value: 'FULL', label: 'Full' },
] as const

/**
 * Default noise parameters. Mirror backend NoiseConfig defaults.
 */
export const DEFAULT_NOISE_SIGMA = 0.001
export const DEFAULT_DRIFT_RATE = 0.0
export const DEFAULT_BIAS = 0.0
export const DEFAULT_SEED = 42

/**
 * Composable for simulation mode selection and triggering the simulate API.
 *
 * State:
 * - `tier`:           currently selected simulation tier (ref).
 * - `noiseModel`:     currently selected noise model (only used when tier==='full').
 * - `noiseSigma`:     Gaussian noise sigma.
 * - `driftRate`:      drift rate per second.
 * - `bias`:           constant bias offset.
 * - `seed`:           RNG seed for reproducibility.
 * - `isRunning`:      whether a simulation is in progress.
 * - `lastResult`:     the last simulation response (null before first run).
 * - `error`:          error message from the last failed run (null on success).
 *
 * Computed:
 * - `tierLabel`:      Chinese label for the currently selected tier.
 * - `noiseModelLabel`:label for the currently selected noise model.
 * - `noiseEnabled`:   true only when tier === 'full'.
 *
 * Actions:
 * - `run(runId)`:     POST /executions/{runId}/simulate with current config.
 * - `reset()`:        clear result + error.
 *
 * Errors are surfaced via ElMessage and the `error` ref - no silent fallback.
 */
export function useSimulation() {
  // ── Selection state ──
  const tier = ref<SimulationTier>('driver')
  const noiseModel = ref<NoiseModel>('GAUSSIAN')
  const noiseSigma = ref<number>(DEFAULT_NOISE_SIGMA)
  const driftRate = ref<number>(DEFAULT_DRIFT_RATE)
  const bias = ref<number>(DEFAULT_BIAS)
  const seed = ref<number>(DEFAULT_SEED)

  // ── Run state ──
  const isRunning = ref(false)
  const lastResult = ref<SimulationResponse | null>(null)
  const error = ref<string | null>(null)

  // ── Computed labels ──
  const tierLabel = computed(
    () => SIMULATION_TIERS.find((t) => t.value === tier.value)?.label ?? tier.value,
  )
  const noiseModelLabel = computed(
    () =>
      NOISE_MODELS.find((m) => m.value === noiseModel.value)?.label ??
      noiseModel.value,
  )
  const noiseEnabled = computed(() => tier.value === 'full')

  /**
   * Build the SimulationRequest payload from current state.
   * Noise parameters are only included when tier === 'full'.
   */
  function buildRequest() {
    if (noiseEnabled.value) {
      return {
        tier: tier.value,
        noise_model: noiseModel.value,
        noise_sigma: noiseSigma.value,
        drift_rate: driftRate.value,
        bias: bias.value,
        seed: seed.value,
      }
    }
    return { tier: tier.value }
  }

  /**
   * Trigger a simulation run for the given execution run_id.
   *
   * Surfaces errors via ElMessage and the `error` ref. Does NOT throw to the
   * caller - inspect `error.value` or `lastResult.value` after the call.
   *
   * @returns The SimulationResponse on success, or null on failure.
   */
  async function run(runId: string): Promise<SimulationResponse | null> {
    if (!runId) {
      error.value = 'No run ID - start an execution first.'
      ElMessage.warning('请先启动执行以获取 run ID')
      return null
    }

    isRunning.value = true
    error.value = null
    try {
      const result = await runSimulation(runId, buildRequest())
      lastResult.value = result
      ElMessage.success(`仿真完成: ${result.events.length} 个事件`)
      return result
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e)
      error.value = msg
      ElMessage.error(`仿真失败: ${msg}`)
      return null
    } finally {
      isRunning.value = false
    }
  }

  /** Clear the last result and error. Does not reset tier/noise selection. */
  function reset(): void {
    lastResult.value = null
    error.value = null
  }

  return {
    // Selection state
    tier,
    noiseModel,
    noiseSigma,
    driftRate,
    bias,
    seed,
    // Run state
    isRunning,
    lastResult,
    error,
    // Computed
    tierLabel,
    noiseModelLabel,
    noiseEnabled,
    // Static option lists (for template v-for)
    tiers: SIMULATION_TIERS,
    noiseModels: NOISE_MODELS,
    // Actions
    run,
    reset,
  }
}
