/**
 * 拓扑驱动仿真初始化组合式函数（T31，设计文档 §8.3.8）。
 *
 * 桥接 topologyRuntime store（静态拓扑）与纯函数层 utils/topologySimulation：
 *   - validateBeforeStart：启动仿真前校验链路；有问题时 ElMessageBox 列出并
 *     返回 false（调用方必须中止启动——悬空链路绝不放行，§8.3.8 步骤 3）。
 *   - buildInitSection：组装随 POST /executions/{run_id}/simulate 下发的
 *     topology_init 段（GPIB 网关 + TCP 设备）；无拓扑时返回 undefined，
 *     请求退化为普通三层仿真。
 */
import { ElMessageBox } from 'element-plus'

import { useTopologyRuntimeStore } from '@/stores/topologyRuntime'
import {
  buildTopologySimulateInit,
  validateTopologyLinks,
  type TopologySimulateInit,
} from '@/utils/topologySimulation'

export function useTopologySimulation() {
  const runtime = useTopologyRuntimeStore()

  /**
   * 启动前校验当前拓扑链路。
   *
   * @returns true = 可启动（无拓扑视为通过）；false = 已弹窗列出问题，须中止。
   */
  async function validateBeforeStart(): Promise<boolean> {
    const topo = runtime.topology
    if (!topo) return true

    const problems = validateTopologyLinks(topo)
    if (problems.length === 0) return true

    const lines = problems.map((p) => `• ${p.link_id}：${p.message}`).join('\n')
    await ElMessageBox.alert(lines, '拓扑链路校验未通过，已阻止启动', {
      type: 'error',
      confirmButtonText: '知道了',
    })
    return false
  }

  /** 组装 topology_init 段；未加载拓扑时返回 undefined。 */
  function buildInitSection(): TopologySimulateInit | undefined {
    const topo = runtime.topology
    return topo ? buildTopologySimulateInit(topo) : undefined
  }

  return { validateBeforeStart, buildInitSection }
}
