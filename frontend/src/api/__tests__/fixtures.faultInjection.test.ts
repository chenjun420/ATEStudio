/**
 * Tests for link fault injection API client (T30, 设计文档 §8.3).
 *
 * injectLinkFault must POST /executions/{run_id}/fault-injection with a
 * {link_id, fault_type} JSON body — forwarding the operator's choice to the
 * cloud virtual driver. Client-side simulation is forbidden by the plan.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'

const { postMock } = vi.hoisted(() => ({ postMock: vi.fn() }))

vi.mock('@/api/interceptor', () => ({
  default: { post: postMock },
}))

import { injectLinkFault } from '@/api/fixtures'

describe('injectLinkFault api client', () => {
  beforeEach(() => {
    postMock.mockReset()
    postMock.mockResolvedValue({ data: {} })
  })

  it('POSTs /executions/{run_id}/fault-injection with {link_id, fault_type} body', async () => {
    await injectLinkFault('run-123', 'LINK-1', 'open_circuit')
    expect(postMock).toHaveBeenCalledTimes(1)
    expect(postMock).toHaveBeenCalledWith('/executions/run-123/fault-injection', {
      link_id: 'LINK-1',
      fault_type: 'open_circuit',
    })
  })

  it('accepts every §8.3 fault type verbatim (no renaming/mapping)', async () => {
    const types = ['open_circuit', 'short_circuit', 'contact_resistance', 'noise'] as const
    for (const t of types) {
      await injectLinkFault('run-x', 'L', t)
    }
    expect(postMock).toHaveBeenCalledTimes(4)
    expect(postMock.mock.calls.map((c) => (c[1] as Record<string, string>).fault_type)).toEqual([
      'open_circuit',
      'short_circuit',
      'contact_resistance',
      'noise',
    ])
  })

  it('propagates backend rejection (4xx) to the caller', async () => {
    postMock.mockRejectedValue(new Error('Request failed with status code 409'))
    await expect(injectLinkFault('run-x', 'L', 'noise')).rejects.toThrow('409')
  })
})
