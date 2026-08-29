import { defineStore } from 'pinia'
import { reactive, ref, shallowRef } from 'vue'

import type { FixtureTopologyData, FaultInfo } from '@/api/fixtures'
import { openTicketedEventSource, type TicketedEventSource } from '@/utils/sseTicket'

/**
 * 拓扑运行时状态（设计文档 §8.3.6）。
 *
 * 订阅 GET /api/v1/executions/{runId}/topology-stream 的 SSE 事件：
 *   instrument / link / relay / measurement / fixture / fault
 * 维护运行时状态并在拓扑画布上高亮活跃链路、仪器/继电器状态、测量值。
 *
 * EventSource 无法携带 Authorization 头，故经 RH-3 一次性 ticket 鉴权
 * （openTicketedEventSource：失败自动换票重连一次）；组件在
 * onBeforeUnmount 时调用 disconnect() 避免泄漏。
 */
export const useTopologyRuntimeStore = defineStore('topologyRuntime', () => {
  // 静态拓扑（加载的工装配置）——浅引用避免深拷贝开销
  const topology = shallowRef<FixtureTopologyData | null>(null)

  // 运行时状态
  const instrumentStatus = reactive<Record<string, Record<string, unknown>>>({})
  const linkStatus = reactive<Record<string, Record<string, unknown>>>({})
  const relayState = reactive<Record<string, Record<string, unknown>>>({})
  const measurementStatus = reactive<Record<string, Record<string, unknown>>>({})
  const fixtureStatus = reactive<Record<string, Record<string, unknown>>>({})
  const faults = reactive<FaultEntry[]>([])

  // 连接状态
  const connected = ref(false)
  const activeRunId = ref<string | null>(null)
  const error = ref<string | null>(null)

  let eventSource: TicketedEventSource | null = null

  /**
   * 故障条目（含可选定位信息，供拓扑画布高亮 §8.3.7）。
   */
  type FaultEntry = FaultInfo & { location?: unknown }

  function setTopology(data: FixtureTopologyData | null) {
    topology.value = data
  }

  function updateInstrumentStatus(id: string, status: string, extra?: Record<string, unknown>) {
    instrumentStatus[id] = { instrument_id: id, status, ...extra }
  }

  function updateLinkStatus(id: string, active: boolean, status?: string) {
    linkStatus[id] = { link_id: id, active, status: status ?? (active ? 'active' : 'idle') }
  }

  function updateRelayState(id: string, state: string) {
    relayState[id] = { relay_id: id, state }
  }

  function updateMeasurement(dutId: string, testpointId: string, value: number | null, status?: string) {
    const key = `${dutId}:${testpointId}`
    measurementStatus[key] = { dut_id: dutId, testpoint_id: testpointId, value, status }
  }

  function updateFixtureStatus(id: string, status: string, extra?: Record<string, unknown>) {
    fixtureStatus[id] = { fixture_id: id, status, ...extra }
  }

  function addFault(fault: FaultInfo, location?: unknown) {
    // 合并定位信息（suspect_links/suspect_relays 等），供画布高亮使用（§8.3.7）
    faults.push({ ...fault, location: location ?? null })
  }

  function clearRuntime() {
    for (const key of Object.keys(instrumentStatus)) delete instrumentStatus[key]
    for (const key of Object.keys(linkStatus)) delete linkStatus[key]
    for (const key of Object.keys(relayState)) delete relayState[key]
    for (const key of Object.keys(measurementStatus)) delete measurementStatus[key]
    for (const key of Object.keys(fixtureStatus)) delete fixtureStatus[key]
    faults.splice(0, faults.length)
  }

  /**
   * 订阅指定执行的拓扑运行时 SSE 流。
   *
   * @param runId 执行 run_id（可为空——跳过连接）。
   */
  function connect(runId: string | null) {
    disconnect()
    if (!runId) return

    activeRunId.value = runId
    const path = `/api/v1/executions/${runId}/topology-stream`
    const parse = (e: MessageEvent<string>) => JSON.parse(e.data) as Record<string, unknown>

    eventSource = openTicketedEventSource(path, {
      onOpen: () => {
        connected.value = true
        error.value = null
      },
      onError: () => {
        // 瞬时断流由原生自动重连/ticket 换票处理；重连期间标记未连接。
        connected.value = false
        error.value = '拓扑状态流连接断开，正在重连…'
      },
      listeners: {
        instrument: (e) => {
          const data = parse(e)
          updateInstrumentStatus(String(data.instrument_id), String(data.status ?? ''), data)
        },
        link: (e) => {
          const data = parse(e)
          updateLinkStatus(String(data.link_id), Boolean(data.active), String(data.status ?? ''))
        },
        relay: (e) => {
          const data = parse(e)
          updateRelayState(String(data.relay_id), String(data.state ?? ''))
        },
        measurement: (e) => {
          const data = parse(e)
          updateMeasurement(
            String(data.dut_id),
            String(data.testpoint_id),
            data.value == null ? null : Number(data.value),
            String(data.status ?? ''),
          )
        },
        fixture: (e) => {
          const data = parse(e)
          updateFixtureStatus(String(data.fixture_id), String(data.status ?? ''), data)
        },
        fault: (e) => {
          const data = JSON.parse(e.data) as { fault?: FaultInfo; location?: unknown }
          if (data.fault) addFault(data.fault, data.location)
        },
      },
    })
  }

  /** 断开 SSE 连接并清空连接状态（保留已收集的运行时状态）。 */
  function disconnect() {
    if (eventSource) {
      eventSource.close()
      eventSource = null
    }
    connected.value = false
    activeRunId.value = null
  }

  /** 断开并清空全部运行时状态。 */
  function reset() {
    disconnect()
    clearRuntime()
    topology.value = null
    error.value = null
  }

  return {
    // 静态拓扑
    topology,
    setTopology,
    // 运行时状态
    instrumentStatus,
    linkStatus,
    relayState,
    measurementStatus,
    fixtureStatus,
    faults,
    // 连接状态
    connected,
    activeRunId,
    error,
    // 动作
    connect,
    disconnect,
    reset,
    clearRuntime,
  }
})
