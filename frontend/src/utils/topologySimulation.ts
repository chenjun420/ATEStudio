/**
 * 拓扑驱动仿真初始化纯函数（T31，设计文档 §8.3.8 TopologyDrivenSimulation）。
 *
 * 数据形状与 src/shared/fixture_topology.py 对齐（frontend/src/api/fixtures.ts
 * 的 FixtureTopologyData / Instrument / Link）。语义（§8.3.8 initialize 三步）：
 *   1. GPIB 网关：type === 'gpib_gateway' 的仪器成为 MockGPIBGateway
 *      （board_index 取 communication.address）；communication.type === 'gpib'
 *      的其余仪器 attach 到网关（显式 config.gateway_id 优先，否则首个网关）。
 *   2. TCP 设备：type === 'tcp_device' 或 communication.type === 'tcp' 的仪器
 *      启动 MockTCPDevice；port 缺省为 0（后端动态分配后回写拓扑）。
 *   3. 链路校验：启动前校验每条 Link 两端端点存在性——悬空端点必须阻断启动。
 *
 * 所有函数均为纯函数（不修改入参、零 X6/DOM 依赖），便于测试与组合；
 * Vue 侧封装见 composables/useTopologySimulation.ts。
 */

import type { FixtureTopologyData, Instrument, LinkEndpoint } from '@/api/fixtures'

// ─── 初始化段类型（随 POST /executions/{run_id}/simulate 下发）───────────────

/** 挂载到 GPIB 网关的虚拟设备（MockDriverFactory.create 参数，§8.3.8）。 */
export interface GpibAttachedDevice {
  instrument_id: string
  /** GPIB 主地址（instrument.communication.address）。 */
  address: string | null
  /** 仿真 profile（instrument.simulation_profile，缺省 'default'）。 */
  profile: string
}

/** MockGPIBGateway 构造描述。 */
export interface GpibGatewayInit {
  instrument_id: string
  /** GPIB 板卡号（网关 communication.address）。 */
  board_index: string | null
  attached_devices: GpibAttachedDevice[]
}

/** MockTCPDevice 构造描述；port=0 表示由后端动态分配端口。 */
export interface TcpDeviceInit {
  instrument_id: string
  host: string
  port: number
}

/** simulate 请求的 topology_init 段（§8.3.8 步骤 1+2 的派生结果）。 */
export interface TopologySimulateInit {
  gpib_gateways: GpibGatewayInit[]
  tcp_devices: TcpDeviceInit[]
}

// ─── 链路校验 ───────────────────────────────────────────────────────────────

/** 单条链路问题（link_id 定位画布元素，message 展示给操作员）。 */
export interface LinkProblem {
  link_id: string
  message: string
}

const TCP_HOST = '127.0.0.1'

function isGpibComm(inst: Instrument): boolean {
  return inst.communication?.type === 'gpib'
}

/**
 * §8.3.8 步骤 1：派生 GPIB 网关及其挂载设备。
 * 无网关时返回 []（设备无从挂载，与文档 "if gateway" 行为一致）。
 */
export function deriveGpibGateways(topology: FixtureTopologyData): GpibGatewayInit[] {
  const gateways = topology.instruments.filter((i) => i.type === 'gpib_gateway')
  if (gateways.length === 0) return []

  const attached = new Map<string, GpibAttachedDevice[]>(
    gateways.map((g) => [g.id, [] as GpibAttachedDevice[]]),
  )

  for (const inst of topology.instruments) {
    if (inst.type === 'gpib_gateway' || !isGpibComm(inst)) continue
    const explicitId = inst.communication?.config?.gateway_id
    const target =
      (typeof explicitId === 'string' && gateways.find((g) => g.id === explicitId)) || gateways[0]
    attached.get(target.id)!.push({
      instrument_id: inst.id,
      address: inst.communication?.address ?? null,
      profile: inst.simulation_profile || 'default',
    })
  }

  return gateways.map((g) => ({
    instrument_id: g.id,
    board_index: g.communication?.address ?? null,
    attached_devices: attached.get(g.id)!,
  }))
}

/**
 * §8.3.8 步骤 2：派生虚拟 TCP 设备列表。
 * port 未配置时为 0 → 后端 MockTCPDevice.start() 动态分配并回写拓扑。
 */
export function deriveTcpDevices(topology: FixtureTopologyData): TcpDeviceInit[] {
  return topology.instruments
    .filter((i) => i.type === 'tcp_device' || i.communication?.type === 'tcp')
    .map((i) => ({
      instrument_id: i.id,
      host: TCP_HOST,
      port: typeof i.communication?.port === 'number' ? i.communication.port : 0,
    }))
}

/** 组装 simulate 请求的 topology_init 段（空拓扑返回空数组键，键恒存在）。 */
export function buildTopologySimulateInit(topology: FixtureTopologyData): TopologySimulateInit {
  return {
    gpib_gateways: deriveGpibGateways(topology),
    tcp_devices: deriveTcpDevices(topology),
  }
}

/**
 * §8.3.8 步骤 3：链路连通性校验（仿真模式）。
 *
 * 逐条检查 Link 两端端点的实体与端口是否存在：
 *   - instrument_channel → instruments[].id + channels[].id
 *   - fixture_terminal   → fixtures[].id + terminals[].id
 *   - dut_testpoint      → duts[].id + test_points[].id
 *   - relay_contact      → fixtures[].relays[].id（触点结构自由，仅查实体）
 *
 * 返回全部问题（含 link_id 以便画布定位）；空数组 = 校验通过。
 */
export function validateTopologyLinks(topology: FixtureTopologyData): LinkProblem[] {
  const problems: LinkProblem[] = []

  const instrumentById = new Map(topology.instruments.map((i) => [i.id, i]))
  const fixtureById = new Map(topology.fixtures.map((f) => [f.id, f]))
  const dutTestPoints = new Map(
    topology.duts.map((d) => [d.id, new Set(d.test_points.map((t) => t.id))]),
  )
  const relayIds = new Set(
    topology.fixtures.flatMap((f) => (f.relays ?? []).map((r) => r.id)),
  )

  const check = (linkId: string, side: string, ep: LinkEndpoint): void => {
    switch (ep.entity_type) {
      case 'instrument_channel': {
        const inst = instrumentById.get(ep.entity_id)
        if (!inst) {
          problems.push({ link_id: linkId, message: `${side}端仪器 ${ep.entity_id} 不存在` })
          break
        }
        if (!inst.channels.some((c) => c.id === ep.port_id)) {
          problems.push({
            link_id: linkId,
            message: `${side}端仪器 ${ep.entity_id} 缺少通道 ${ep.port_id}`,
          })
        }
        break
      }
      case 'fixture_terminal': {
        const fixture = fixtureById.get(ep.entity_id)
        if (!fixture) {
          problems.push({ link_id: linkId, message: `${side}端夹具 ${ep.entity_id} 不存在` })
          break
        }
        if (!(fixture.terminals ?? []).some((t) => t.id === ep.port_id)) {
          problems.push({
            link_id: linkId,
            message: `${side}端夹具 ${ep.entity_id} 缺少端子 ${ep.port_id}`,
          })
        }
        break
      }
      case 'dut_testpoint': {
        const tps = dutTestPoints.get(ep.entity_id)
        if (!tps) {
          problems.push({ link_id: linkId, message: `${side}端 DUT ${ep.entity_id} 不存在` })
          break
        }
        if (!tps.has(ep.port_id)) {
          problems.push({
            link_id: linkId,
            message: `${side}端 DUT ${ep.entity_id} 缺少测试点 ${ep.port_id}`,
          })
        }
        break
      }
      case 'relay_contact': {
        if (!relayIds.has(ep.entity_id)) {
          problems.push({ link_id: linkId, message: `${side}端继电器 ${ep.entity_id} 不存在` })
        }
        break
      }
    }
  }

  for (const link of topology.links) {
    check(link.id, '起始', link.from)
    check(link.id, '目标', link.to)
  }
  return problems
}
