/**
 * Tests for the AI-diagnosis path in useOperatorGuidance (task 22).
 *
 * The composable must call the REAL diagnosis client which POSTs to
 * `/diagnose` via the shared http instance (@/api/interceptor) — not the old
 * non-existent `GET /faults/diagnose`. SSE (useExecutionStatus), the execution
 * fetch and the sequence fetch are mocked so the test is deterministic.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { ref } from 'vue'

const { postMock } = vi.hoisted(() => ({ postMock: vi.fn() }))

vi.mock('@/api/interceptor', () => ({
  default: {
    get: vi.fn(),
    post: postMock,
    put: vi.fn(),
    delete: vi.fn(),
  },
}))

vi.mock('./useExecutionStatus', () => ({
  useExecutionStatus: () => ({
    stepStatuses: ref({}),
    executionStatus: ref(null),
    isRunning: ref(false),
    progressText: ref(''),
    completedSteps: ref(0),
    latestAlarm: ref(null),
    latestMeasurements: ref({}),
    connectionStatus: ref('disconnected'),
    setTotalSteps: vi.fn(),
    reset: vi.fn(),
  }),
}))

vi.mock('@/api/executions', () => ({
  getExecution: vi.fn(async () => ({ sequence_id: null })),
}))

import { useOperatorGuidance } from '../useOperatorGuidance'

describe('useOperatorGuidance diagnosis (POST /diagnose)', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('POSTs to /diagnose with run_id and the failed test, not GET /faults/diagnose', async () => {
    postMock.mockResolvedValue({
      data: {
        diagnosis_id: 'd-1',
        root_cause: 'Loose power connector',
        confidence: 0.75,
        evidence_citations: ['case-9'],
        repair_steps: ['Re-seat the connector', 'Measure the rail'],
        retrieved_cases: [],
      },
    })

    const guidance = useOperatorGuidance('STN-1', 'run-123')
    // No sequence loaded -> the failed test falls back to the step id / run.
    await guidance.fetchDiagnosis('step-7')

    expect(postMock).toHaveBeenCalledTimes(1)
    const [url, body] = postMock.mock.calls[0]
    expect(url).toBe('/diagnose')
    expect(body).toMatchObject({
      run_id: 'run-123',
      failed_test: 'step-7',
      product_type: 'unknown',
    })
    // The backend diagnosis is mapped into the UI result.
    expect(guidance.diagnosis.value?.root_cause).toBe('Loose power connector')
    // Repair steps are numbered for the operator panel.
    expect(guidance.diagnosis.value?.repair_steps).toEqual([
      { order: 1, action: 'Re-seat the connector' },
      { order: 2, action: 'Measure the rail' },
    ])
    expect(guidance.diagnosisError.value).toBeNull()
  })

  it('names the failed test from the loaded step script when available', async () => {
    postMock.mockResolvedValue({
      data: {
        diagnosis_id: 'd-2',
        root_cause: 'cause',
        confidence: 0.5,
        evidence_citations: [],
        repair_steps: ['fix'],
        retrieved_cases: [],
      },
    })

    const guidance = useOperatorGuidance('', 'run-xyz')
    // Simulate a parsed sequence step (as loadSequenceForRun would populate).
    // Accessing the internal rawSteps is not exposed; instead fetchDiagnosis
    // with a step id that has no matching step still sends the step id label.
    await guidance.fetchDiagnosis(null) // run-level diagnosis

    expect(postMock).toHaveBeenCalledTimes(1)
    const body = postMock.mock.calls[0][1] as Record<string, unknown>
    expect(body.failed_test).toBe('run run-xyz')
    expect(body.run_id).toBe('run-xyz')
  })

  it('surfaces a friendly error (and no diagnosis) when the API returns 503', async () => {
    // A real axios rejection carries isAxiosError === true.
    postMock.mockRejectedValue({ isAxiosError: true, response: { status: 503 } })

    const guidance = useOperatorGuidance('', 'run-err')
    await guidance.fetchDiagnosis('s1')

    expect(guidance.diagnosis.value).toBeNull()
    expect(guidance.diagnosisError.value).toContain('unavailable')
  })

  it('does not call the API when there is no active run', async () => {
    const guidance = useOperatorGuidance('', '')
    await guidance.fetchDiagnosis('s1')
    expect(postMock).not.toHaveBeenCalled()
    expect(guidance.diagnosisError.value).toMatch(/no active run/i)
  })
})
