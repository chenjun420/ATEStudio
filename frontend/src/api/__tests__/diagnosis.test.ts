/**
 * Tests for the diagnosis API module (frontend/src/api/diagnosis.ts, task 22).
 *
 * Verifies the shared http client (@/api/interceptor) is used for a POST to
 * `/diagnose` with the backend DiagnoseRequest payload and that the
 * DiagnoseResponse is unwrapped. No bare axios is involved.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'

const { postMock } = vi.hoisted(() => ({ postMock: vi.fn() }))

vi.mock('@/api/interceptor', () => ({
  default: {
    get: vi.fn(),
    post: postMock,
    put: vi.fn(),
    delete: vi.fn(),
  },
}))

import { diagnoseFault } from '@/api/diagnosis'

describe('api/diagnosis transport', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('diagnoseFault POSTs /diagnose with the run/symptom payload and unwraps data', async () => {
    const response = {
      diagnosis_id: 'd-1',
      root_cause: 'Loose power connector',
      confidence: 0.8,
      evidence_citations: ['case-7'],
      repair_steps: ['Re-seat connector', 'Measure rail'],
      retrieved_cases: [{ id: 'case-7' }],
    }
    postMock.mockResolvedValue({ data: response })

    const res = await diagnoseFault({
      product_type: 'PSU-12V',
      failed_test: 'measure_voltage.py',
      error_code: 'E42',
      log_snippet: 'voltage low',
      run_id: 'run-1',
    })

    expect(postMock).toHaveBeenCalledTimes(1)
    expect(postMock.mock.calls[0][0]).toBe('/diagnose')
    const body = postMock.mock.calls[0][1] as Record<string, unknown>
    expect(body).toMatchObject({
      product_type: 'PSU-12V',
      failed_test: 'measure_voltage.py',
      run_id: 'run-1',
    })
    // Response is unwrapped from axios `data`.
    expect(res.diagnosis_id).toBe('d-1')
    expect(res.repair_steps).toEqual(['Re-seat connector', 'Measure rail'])
  })

  it('diagnoseFault sends minimal required fields when optional ones are omitted', async () => {
    postMock.mockResolvedValue({
      data: {
        diagnosis_id: 'd-2',
        root_cause: '',
        confidence: 0,
        evidence_citations: [],
        repair_steps: [],
        retrieved_cases: [],
      },
    })

    await diagnoseFault({ product_type: 'X', failed_test: 'f', run_id: 'r' })

    const body = postMock.mock.calls[0][1] as Record<string, unknown>
    expect(body).toEqual({ product_type: 'X', failed_test: 'f', run_id: 'r' })
  })
})
