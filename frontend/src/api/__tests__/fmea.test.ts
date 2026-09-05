/**
 * Tests for the FMEA API module (frontend/src/api/fmea.ts).
 *
 * Verifies the shared http client (@/api/interceptor) is used for every call
 * with the correct /fmea endpoints, that list passes query params, and — most
 * importantly — that create/update payloads never carry a client-supplied
 * `rpn` (the server derives RPN = severity*occurrence*detection).
 *
 * The pure helpers (computeRpn / isValidRating / rpnBand) are also covered.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'

const { getMock, postMock, putMock, deleteMock } = vi.hoisted(() => ({
  getMock: vi.fn(),
  postMock: vi.fn(),
  putMock: vi.fn(),
  deleteMock: vi.fn(),
}))

vi.mock('@/api/interceptor', () => ({
  default: {
    get: getMock,
    post: postMock,
    put: putMock,
    delete: deleteMock,
  },
}))

import {
  fetchFmeas,
  fetchFmea,
  createFmea,
  updateFmea,
  deleteFmea,
  computeRpn,
  isValidRating,
  rpnBand,
  type FmeaRecord,
} from '@/api/fmea'

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

describe('api/fmea transport', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('fetchFmeas GETs /fmea and unwraps {items,total}', async () => {
    getMock.mockResolvedValue({ data: { items: [makeRecord()], total: 1 } })
    const res = await fetchFmeas()
    expect(getMock).toHaveBeenCalledTimes(1)
    expect(getMock.mock.calls[0][0]).toBe('/fmea')
    expect(res.total).toBe(1)
    expect(res.items[0].component_code).toBe('PSU-12V')
  })

  it('fetchFmeas forwards component_code / fault_code / skip / limit query params', async () => {
    getMock.mockResolvedValue({ data: { items: [], total: 0 } })
    await fetchFmeas({ component_code: 'PSU', fault_code: 'drift', skip: 10, limit: 50 })
    const params = getMock.mock.calls[0][1].params as Record<string, string | number>
    expect(params).toEqual({ component_code: 'PSU', fault_code: 'drift', skip: 10, limit: 50 })
  })

  it('fetchFmea GETs /fmea/{id} with an encoded id', async () => {
    getMock.mockResolvedValue({ data: makeRecord({ id: 'a b' }) })
    const rec = await fetchFmea('a b')
    expect(getMock.mock.calls[0][0]).toBe('/fmea/a%20b')
    expect(rec.id).toBe('a b')
  })

  it('createFmea POSTs /fmea WITHOUT an rpn field in the payload', async () => {
    postMock.mockResolvedValue({ data: makeRecord({ rpn: 84 }) })
    const rec = await createFmea({
      component_code: 'PSU-12V',
      failure_mode: 'Output drifts high',
      severity: 7,
      occurrence: 4,
      detection: 3,
    })
    expect(postMock).toHaveBeenCalledTimes(1)
    expect(postMock.mock.calls[0][0]).toBe('/fmea')
    const body = postMock.mock.calls[0][1] as Record<string, unknown>
    expect(body).not.toHaveProperty('rpn')
    expect(body.severity).toBe(7)
    // The server-computed rpn comes back on the response.
    expect(rec.rpn).toBe(84)
  })

  it('updateFmea PUTs /fmea/{id} WITHOUT an rpn field in the payload', async () => {
    putMock.mockResolvedValue({ data: makeRecord({ severity: 10, rpn: 120 }) })
    const rec = await updateFmea('f1', { severity: 10 })
    expect(putMock.mock.calls[0][0]).toBe('/fmea/f1')
    const body = putMock.mock.calls[0][1] as Record<string, unknown>
    expect(body).not.toHaveProperty('rpn')
    expect(body.severity).toBe(10)
    expect(rec.rpn).toBe(120)
  })

  it('deleteFmea DELETEs /fmea/{id}', async () => {
    deleteMock.mockResolvedValue({})
    await deleteFmea('f1')
    expect(deleteMock).toHaveBeenCalledWith('/fmea/f1')
  })
})

describe('api/fmea pure helpers', () => {
  it('computeRpn multiplies S*O*D', () => {
    expect(computeRpn(7, 4, 3)).toBe(84)
    expect(computeRpn(10, 10, 10)).toBe(1000)
    expect(computeRpn(1, 1, 1)).toBe(1)
  })

  it('isValidRating accepts integers 1-10 and rejects out-of-range / non-integers', () => {
    expect(isValidRating(1)).toBe(true)
    expect(isValidRating(10)).toBe(true)
    expect(isValidRating(0)).toBe(false)
    expect(isValidRating(11)).toBe(false)
    expect(isValidRating(5.5)).toBe(false)
    expect(isValidRating(null)).toBe(false)
    expect(isValidRating(undefined)).toBe(false)
  })

  it('rpnBand buckets by >=100 danger, >=60 warning, else normal', () => {
    expect(rpnBand(100)).toBe('danger')
    expect(rpnBand(240)).toBe('danger')
    expect(rpnBand(60)).toBe('warning')
    expect(rpnBand(99)).toBe('warning')
    expect(rpnBand(59)).toBe('normal')
    expect(rpnBand(1)).toBe('normal')
  })
})
