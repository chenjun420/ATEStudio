/**
 * Tests for runSimulation topology_init passthrough (T31, 设计文档 §8.3.8).
 *
 * The frontend derives GPIB gateway / TCP device init sections from the loaded
 * fixture topology and forwards them on POST /executions/{run_id}/simulate.
 * No client-side simulation — the backend virtual driver path owns startup.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'

const { postMock } = vi.hoisted(() => ({ postMock: vi.fn() }))

vi.mock('@/api/interceptor', () => ({
  default: { post: postMock },
}))

import { runSimulation } from '@/api/simulation'

describe('runSimulation topology_init passthrough', () => {
  beforeEach(() => {
    postMock.mockReset()
    postMock.mockResolvedValue({
      data: { session_id: 'r1', tier: 'full', status: 'passed', events: [], duration_seconds: 0 },
    })
  })

  it('POSTs /executions/{run_id}/simulate forwarding the topology_init section verbatim', async () => {
    const topology_init = {
      gpib_gateways: [
        {
          instrument_id: 'GW1',
          board_index: '0',
          attached_devices: [{ instrument_id: 'DMM1', address: '5', profile: 'default' }],
        },
      ],
      tcp_devices: [{ instrument_id: 'TCP1', host: '127.0.0.1', port: 9000 }],
    }
    await runSimulation('run-42', { tier: 'full', topology_init })
    expect(postMock).toHaveBeenCalledTimes(1)
    expect(postMock).toHaveBeenCalledWith('/executions/run-42/simulate', {
      tier: 'full',
      topology_init,
    })
  })

  it('omits topology_init when no topology is loaded (plain 3-tier request)', async () => {
    await runSimulation('run-7', { tier: 'dry_run' })
    const body = postMock.mock.calls[0][1] as Record<string, unknown>
    expect(body).toEqual({ tier: 'dry_run' })
    expect('topology_init' in body).toBe(false)
  })
})
