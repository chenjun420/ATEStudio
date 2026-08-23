<script setup lang="ts">
/**
 * SimulationReportPanel — 仿真报告面板（T41，v41-gap-analysis #41，§8.4）。
 *
 * 职责：渲染 GET /executions/{run_id}/simulation-report 组合报告
 * （props-down 纯展示），三个可折叠分节：
 *   - 覆盖率：步骤/分支 % 条（纯 div 宽度，零图表库）+ 分支覆盖表
 *   - 资源竞争：死锁告警（等待环）+ Top-N 等待资源表
 *   - 故障记录：severity 配色标签列表（复用既有严重度约定）
 * 空节自动折叠并显示降级原因；warnings 渲染为部分报告横幅；
 * 导出按钮把当前报告下载为单个 JSON 文件（不含凭据——后端录制已脱敏）。
 */
import { computed, ref, watch } from 'vue'
import { ElAlert, ElTag } from 'element-plus'
import {
  barWidthPercent,
  buildBranchRows,
  buildContentionRows,
  buildCoverageBars,
  buildDeadlockAlerts,
  buildFaultRows,
  buildReportSections,
  severityTagType,
  type SimulationReportResponse,
} from '@/utils/simulationReportView'

const props = defineProps<{
  /** 后端组合报告；null 表示尚未拉取。 */
  report: SimulationReportResponse | null
  loading?: boolean
}>()

const collapsed = ref<Set<string>>(new Set())

const sections = computed(() => (props.report ? buildReportSections(props.report) : []))
const bars = computed(() =>
  props.report?.coverage.report ? buildCoverageBars(props.report.coverage.report) : [],
)
const branchRows = computed(() =>
  props.report?.coverage.report ? buildBranchRows(props.report.coverage.report) : [],
)
const contentionRows = computed(() =>
  props.report?.contention.report ? buildContentionRows(props.report.contention.report) : [],
)
const deadlocks = computed(() =>
  props.report?.contention.report ? buildDeadlockAlerts(props.report.contention.report) : [],
)
const faultRows = computed(() => (props.report ? buildFaultRows(props.report.faults.records) : []))

// 计划 AC：空节自动折叠（无内容 → 折叠 + 展示降级原因/空态）。
watch(
  () => props.report,
  (r) => {
    const next = new Set<string>()
    if (r) for (const sec of buildReportSections(r)) if (!sec.hasContent) next.add(sec.id)
    collapsed.value = next
  },
  { immediate: true },
)

function toggle(id: string): void {
  const next = new Set(collapsed.value)
  if (next.has(id)) next.delete(id)
  else next.add(id)
  collapsed.value = next
}

/** 导出当前报告为单个 JSON 文件（客户端 Blob，无新增依赖）。 */
function exportJson(): void {
  if (!props.report) return
  const blob = new Blob([JSON.stringify(props.report, null, 2)], { type: 'application/json' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = `simulation-report-${props.report.run_id}.json`
  a.click()
  URL.revokeObjectURL(url)
}
</script>

<template>
  <div class="sim-report-panel">
    <div v-if="loading" class="srp-empty">报告生成中…</div>
    <div v-else-if="!report" class="srp-empty">运行仿真后可查看报告</div>
    <template v-else>
      <div class="srp-toolbar">
        <span class="srp-meta">{{ report.run_status }} · {{ report.generated_at.slice(0, 19).replace('T', ' ') }}</span>
        <el-button size="small" link type="primary" data-testid="export-report" @click="exportJson">导出 JSON</el-button>
      </div>

      <el-alert
        v-if="report.warnings.length"
        type="warning"
        :title="'部分数据缺失，报告不完整'"
        :description="report.warnings.join('；')"
        :closable="false"
        class="srp-banner"
      />

      <section v-for="sec in sections" :key="sec.id" class="srp-section" :data-section="sec.id">
        <h4 class="srp-section-title" role="button" @click="toggle(sec.id)">
          <span>{{ collapsed.has(sec.id) ? '▸' : '▾' }} {{ sec.title }}</span>
          <el-tag v-if="!sec.available" size="small" type="info">缺失</el-tag>
        </h4>

        <template v-if="!collapsed.has(sec.id)">
          <!-- 覆盖率：% 条 + 分支表 -->
          <template v-if="sec.id === 'coverage'">
            <p v-if="!sec.hasContent" class="srp-none">{{ sec.emptyReason ?? '暂无覆盖率数据' }}</p>
            <template v-else>
              <div v-for="bar in bars" :key="bar.key" class="cov-bar-row" :data-bar="bar.key">
                <span class="cov-label">{{ bar.label }}</span>
                <div class="cov-track">
                  <div
                    class="cov-fill"
                    :class="bar.percent >= 100 ? 'is-full' : 'is-partial'"
                    :style="{ width: barWidthPercent(bar.percent) + '%' }"
                  />
                </div>
                <span class="cov-num">{{ bar.percent }}%（{{ bar.covered }}/{{ bar.total }}）</span>
              </div>
              <table v-if="branchRows.length" class="srp-table">
                <thead>
                  <tr><th>分支</th><th>then</th><th>else</th><th>已覆盖臂</th><th>状态</th></tr>
                </thead>
                <tbody>
                  <tr v-for="row in branchRows" :key="row.key">
                    <td>{{ row.branchId }}</td>
                    <td>{{ row.thenIds.join(', ') || '—' }}</td>
                    <td>{{ row.elseIds.join(', ') || '—' }}</td>
                    <td>{{ row.armsCovered.join('/') }}</td>
                    <td>
                      <el-tag size="small" :type="row.full ? 'success' : 'warning'">
                        {{ row.full ? '双臂全覆盖' : '部分覆盖' }}
                      </el-tag>
                    </td>
                  </tr>
                </tbody>
              </table>
            </template>
          </template>

          <!-- 资源竞争：死锁告警 + Top-N 表 -->
          <template v-else-if="sec.id === 'contention'">
            <p v-if="!sec.available" class="srp-none">{{ sec.emptyReason ?? '暂无竞争数据' }}</p>
            <template v-else>
              <el-alert
                v-for="(d, i) in deadlocks"
                :key="i"
                type="error"
                :title="`检测到死锁：${d.cycle_owners.join(' ↔ ')}（涉及 ${d.involved_resources.join('/')}）`"
                :closable="false"
                class="srp-deadlock"
              />
              <p v-if="contentionRows.length === 0 && deadlocks.length === 0" class="srp-none">无资源竞争记录</p>
              <table v-if="contentionRows.length" class="srp-table">
                <thead>
                  <tr><th>资源</th><th>等待次数</th><th>峰值等待者</th><th>平均等待 (ms)</th></tr>
                </thead>
                <tbody>
                  <tr v-for="row in contentionRows" :key="row.resource">
                    <td>{{ row.resource }}</td>
                    <td>{{ row.contentionCount }}</td>
                    <td>{{ row.maxWaiters }}</td>
                    <td>{{ row.meanWaitMs }}</td>
                  </tr>
                </tbody>
              </table>
            </template>
          </template>

          <!-- 故障记录：severity 标签列表 -->
          <template v-else>
            <p v-if="faultRows.length === 0" class="srp-none">本次运行无故障记录</p>
            <ul v-else class="srp-faults">
              <li v-for="row in faultRows" :key="row.faultId" class="srp-fault">
                <el-tag size="small" :type="severityTagType(row.severity)">{{ row.severity }}</el-tag>
                <code class="srp-fault-id">{{ row.faultId }}</code>
                <span>{{ row.type }}</span>
                <span v-if="row.target" class="srp-fault-target">@{{ row.target }}</span>
                <span v-if="row.timestamp" class="srp-fault-ts">{{ row.timestamp }}</span>
              </li>
            </ul>
          </template>
        </template>
      </section>
    </template>
  </div>
</template>

<style scoped>
.sim-report-panel {
  padding: 4px 0;
}
.srp-empty {
  color: var(--el-text-color-secondary);
  font-size: 12px;
  text-align: center;
  padding: 16px 0;
}
.srp-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 6px;
}
.srp-meta {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
.srp-banner {
  margin-bottom: 8px;
}
.srp-section {
  margin-bottom: 10px;
}
.srp-section-title {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  font-weight: 600;
  color: var(--el-text-color-primary);
  margin: 0 0 4px;
  cursor: pointer;
  user-select: none;
}
.srp-none {
  color: var(--el-text-color-secondary);
  font-size: 12px;
  margin: 0;
}
.cov-bar-row {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  padding: 3px 0;
}
.cov-label {
  width: 56px;
  flex-shrink: 0;
  color: var(--el-text-color-secondary);
}
.cov-track {
  flex: 1;
  height: 8px;
  border-radius: 4px;
  background: var(--el-fill-color-light);
  overflow: hidden;
}
.cov-fill {
  height: 100%;
  border-radius: 4px;
  transition: width 0.3s ease;
}
.cov-fill.is-full {
  background: var(--el-color-success);
}
.cov-fill.is-partial {
  background: var(--el-color-primary);
}
.cov-num {
  width: 96px;
  flex-shrink: 0;
  text-align: right;
  color: var(--el-text-color-primary);
}
.srp-deadlock {
  margin-bottom: 6px;
}
.srp-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 12px;
  margin-top: 4px;
}
.srp-table th {
  text-align: left;
  color: var(--el-text-color-secondary);
  font-weight: 500;
  padding: 3px 8px;
  border-bottom: 1px solid var(--el-border-color-lighter);
}
.srp-table td {
  padding: 3px 8px;
  border-bottom: 1px solid var(--el-border-color-extra-light);
  color: var(--el-text-color-primary);
  word-break: break-all;
}
.srp-faults {
  list-style: none;
  margin: 0;
  padding: 0;
}
.srp-fault {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  padding: 4px 0;
  border-bottom: 1px solid var(--el-border-color-extra-light);
}
.srp-fault-id {
  color: var(--el-text-color-primary);
}
.srp-fault-target,
.srp-fault-ts {
  color: var(--el-text-color-secondary);
}
</style>
