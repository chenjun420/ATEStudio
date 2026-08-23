import { ref } from 'vue'
import { ElMessage } from 'element-plus'
import { injectLinkFault, type LinkFaultKind } from '@/api/fixtures'

/** 菜单条目（doc §8.3 规定的四种链路故障，不得超出此集合）。 */
export const FAULT_TYPES: ReadonlyArray<{ value: LinkFaultKind; label: string }> = [
  { value: 'open_circuit', label: '断路 open_circuit' },
  { value: 'short_circuit', label: '短路 short_circuit' },
  { value: 'contact_resistance', label: '接触电阻 contact_resistance' },
  { value: 'noise', label: '噪声 noise' },
]

export type LinkFaultType = LinkFaultKind

/**
 * useFaultInjection — 链路故障注入（T30，设计文档 §8.3）。
 *
 * 调用 POST /executions/{run_id}/fault-injection 把操作员选择的故障
 * 转发给云端虚拟驱动；成功/失败均以 ElMessage 反馈。绝不进行纯客户端模拟。
 *
 * @returns injecting 请求进行中标志；injectFault 执行注入并返回是否成功。
 */
export function useFaultInjection() {
  const injecting = ref(false)

  async function injectFault(
    runId: string,
    linkId: string,
    faultType: LinkFaultType,
  ): Promise<boolean> {
    injecting.value = true
    try {
      await injectLinkFault(runId, linkId, faultType)
      ElMessage.success(`故障已注入：${linkId} ← ${faultType}`)
      return true
    } catch (e) {
      ElMessage.error(`故障注入失败: ${e instanceof Error ? e.message : String(e)}`)
      return false
    } finally {
      injecting.value = false
    }
  }

  return { injecting, injectFault }
}
