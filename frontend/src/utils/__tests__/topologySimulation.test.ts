/**
 * Tests for topology-driven simulation init pure functions (T31, 设计文档 §8.3.8).
 *
 * TopologyDrivenSimulation 初始化的前端职责：
 *   - 从拓扑 instruments 派生 GPIB 网关段（MockGPIBGateway + attach_device）
 *   - 从拓扑 instruments 派生 TCP 设备段（MockTCPDevice，port 0 = 动态分配）
 *   - 启动前校验链路端点（悬空端点必须阻断启动）
 *   - 组装随 POST /executions/{run_id}/simulate 下发的 topology_init 段
 */
import { describe, it, expect } from 'vitest'

import type { FixtureTopologyData, Instrument } from '@/api/fixtures'
import {
  buildTopologySimulateInit,
  deriveGpibGateways,
  deriveTcpDevices,
  validateTopologyLinks,
} from '@/utils/topologySimulation'

function inst(partial: Partial<Instrument> & { id: string }): Instrument {
  return {
    name: partial.id,
    type: 'custom',
    channels: [],
    ...partial,
  }
}

function topo(partial: Partial<FixtureTopologyData>): FixtureTopologyData {
  return {
    instruments: [],
    fixtures: [],
    duts: [],
    links: [],
    routes: [],
    ...partial,
  }
}

describe('deriveGpibGateways (§8.3.8 step 1)', () => {
  it('attaches gpib-communication instruments to the gateway with board_index and profile', () => {
    const t = topo({
      instruments: [
        inst({ id: 'GW1', type: 'gpib_gateway', communication: { type: 'gpib', address: '0' } }),
        inst({
          id: 'DMM1',
          type: 'dmm',
          communication: { type: 'gpib', address: '5' },
          simulation_profile: 'keysight_34401a',
        }),
      ],
    })
    expect(deriveGpibGateways(t)).toEqual([
      {
        instrument_id: 'GW1',
        board_index: '0',
        attached_devices: [
          { instrument_id: 'DMM1', address: '5', profile: 'keysight_34401a' },
        ],
      },
    ])
  })

  it('defaults profile to "default" and honours explicit config.gateway_id routing', () => {
    const t = topo({
      instruments: [
        inst({ id: 'GW_A', type: 'gpib_gateway', communication: { type: 'gpib', address: '0' } }),
        inst({ id: 'GW_B', type: 'gpib_gateway', communication: { type: 'gpib', address: '1' } }),
        inst({
          id: 'PSU1',
          type: 'psu',
          communication: { type: 'gpib', address: '7', config: { gateway_id: 'GW_B' } },
        }),
      ],
    })
    const gateways = deriveGpibGateways(t)
    expect(gateways.find((g) => g.instrument_id === 'GW_B')?.attached_devices).toEqual([
      { instrument_id: 'PSU1', address: '7', profile: 'default' },
    ])
    expect(gateways.find((g) => g.instrument_id === 'GW_A')?.attached_devices).toEqual([])
  })

  it('returns [] when the topology has no gpib_gateway instrument', () => {
    const t = topo({
      instruments: [inst({ id: 'DMM1', type: 'dmm', communication: { type: 'gpib', address: '5' } })],
    })
    expect(deriveGpibGateways(t)).toEqual([])
  })
})

describe('deriveTcpDevices (§8.3.8 step 2)', () => {
  it('maps configured ports verbatim and uses port 0 (ephemeral) when unset', () => {
    const t = topo({
      instruments: [
        inst({ id: 'TCP1', type: 'tcp_device', communication: { type: 'tcp', port: 9000 } }),
        inst({ id: 'TCP2', type: 'tcp_device', communication: { type: 'tcp' } }),
      ],
    })
    expect(deriveTcpDevices(t)).toEqual([
      { instrument_id: 'TCP1', host: '127.0.0.1', port: 9000 },
      { instrument_id: 'TCP2', host: '127.0.0.1', port: 0 },
    ])
  })

  it('includes tcp-typed communication even when kind is not tcp_device, skips non-tcp', () => {
    const t = topo({
      instruments: [
        inst({ id: 'SCOPE1', type: 'oscilloscope', communication: { type: 'tcp', port: 5555 } }),
        inst({ id: 'DMM1', type: 'dmm', communication: { type: 'gpib', address: '5' } }),
      ],
    })
    expect(deriveTcpDevices(t)).toEqual([{ instrument_id: 'SCOPE1', host: '127.0.0.1', port: 5555 }])
  })
})

describe('validateTopologyLinks (§8.3.8 step 3)', () => {
  it('returns [] for a fully wired topology', () => {
    const t = topo({
      instruments: [
        inst({
          id: 'PSU1',
          type: 'psu',
          channels: [{ id: 'CH1', name: '+12V', type: 'power', direction: 'output' }],
        }),
      ],
      fixtures: [{ id: 'FX1', name: '夹具', terminals: [{ id: 'T1' }] }],
      duts: [{ id: 'DUT1', test_points: [{ id: 'TP1' }] }],
      links: [
        {
          id: 'L1',
          from: { entity_type: 'instrument_channel', entity_id: 'PSU1', port_id: 'CH1' },
          to: { entity_type: 'fixture_terminal', entity_id: 'FX1', port_id: 'T1' },
          signal_type: 'power',
        },
        {
          id: 'L2',
          from: { entity_type: 'fixture_terminal', entity_id: 'FX1', port_id: 'T1' },
          to: { entity_type: 'dut_testpoint', entity_id: 'DUT1', port_id: 'TP1' },
          signal_type: 'power',
        },
      ],
    })
    expect(validateTopologyLinks(t)).toEqual([])
  })

  it('reports a dangling instrument endpoint with the link id', () => {
    const t = topo({
      links: [
        {
          id: 'L1',
          from: { entity_type: 'instrument_channel', entity_id: 'GHOST', port_id: 'CH1' },
          to: { entity_type: 'dut_testpoint', entity_id: 'DUT1', port_id: 'TP1' },
          signal_type: 'signal',
        },
      ],
      duts: [{ id: 'DUT1', test_points: [{ id: 'TP1' }] }],
    })
    const problems = validateTopologyLinks(t)
    expect(problems).toHaveLength(1)
    expect(problems[0].link_id).toBe('L1')
    expect(problems[0].message).toContain('GHOST')
  })

  it('reports an instrument channel port that does not exist on the instrument', () => {
    const t = topo({
      instruments: [
        inst({
          id: 'PSU1',
          type: 'psu',
          channels: [{ id: 'CH1', name: '+12V', type: 'power', direction: 'output' }],
        }),
      ],
      links: [
        {
          id: 'L9',
          from: { entity_type: 'instrument_channel', entity_id: 'PSU1', port_id: 'NOPE' },
          to: { entity_type: 'fixture_terminal', entity_id: 'FX1', port_id: 'T1' },
          signal_type: 'signal',
        },
      ],
      fixtures: [{ id: 'FX1', name: '夹具', terminals: [{ id: 'T1' }] }],
    })
    const problems = validateTopologyLinks(t)
    expect(problems).toHaveLength(1)
    expect(problems[0].link_id).toBe('L9')
    expect(problems[0].message).toContain('NOPE')
  })

  it('reports missing fixture terminal, dut testpoint and relay endpoints', () => {
    const t = topo({
      fixtures: [
        { id: 'FX1', name: '夹具', terminals: [], relays: [{ id: 'RL1' }] },
      ],
      duts: [{ id: 'DUT1', test_points: [] }],
      links: [
        {
          id: 'LA',
          from: { entity_type: 'fixture_terminal', entity_id: 'FX1', port_id: 'MISSING_T' },
          to: { entity_type: 'relay_contact', entity_id: 'RL1', port_id: 'com' },
          signal_type: 'signal',
        },
        {
          id: 'LB',
          from: { entity_type: 'fixture_terminal', entity_id: 'NO_FX', port_id: 'T1' },
          to: { entity_type: 'dut_testpoint', entity_id: 'DUT1', port_id: 'NO_TP' },
          signal_type: 'signal',
        },
        {
          id: 'LC',
          from: { entity_type: 'relay_contact', entity_id: 'NO_RL', port_id: 'com' },
          to: { entity_type: 'dut_testpoint', entity_id: 'NO_DUT', port_id: 'TP1' },
          signal_type: 'signal',
        },
      ],
    })
    const problems = validateTopologyLinks(t)
    expect(problems.map((p) => p.link_id)).toEqual(['LA', 'LB', 'LB', 'LC', 'LC'])
  })
})

describe('buildTopologySimulateInit (payload assembly)', () => {
  it('assembles gpib_gateways + tcp_devices sections from one topology', () => {
    const t = topo({
      instruments: [
        inst({ id: 'GW1', type: 'gpib_gateway', communication: { type: 'gpib', address: '0' } }),
        inst({ id: 'DMM1', type: 'dmm', communication: { type: 'gpib', address: '5' } }),
        inst({ id: 'TCP1', type: 'tcp_device', communication: { type: 'tcp', port: 9000 } }),
      ],
    })
    expect(buildTopologySimulateInit(t)).toEqual({
      gpib_gateways: [
        {
          instrument_id: 'GW1',
          board_index: '0',
          attached_devices: [{ instrument_id: 'DMM1', address: '5', profile: 'default' }],
        },
      ],
      tcp_devices: [{ instrument_id: 'TCP1', host: '127.0.0.1', port: 9000 }],
    })
  })

  it('returns empty sections for an empty topology (never undefined keys)', () => {
    expect(buildTopologySimulateInit(topo({}))).toEqual({
      gpib_gateways: [],
      tcp_devices: [],
    })
  })
})
