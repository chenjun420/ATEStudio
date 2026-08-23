<script setup lang="ts">
/**
 * ExecutionDiffPanel — 运行对比视图（T37，v41-gap-analysis #37，§7.9.3）。
 *
 * 职责：渲染 ExecutionDiff.compare() 摘要（props-down 纯展示）：
 *   - 顶部总徽标：match → 绿色"全部一致"；否则红色"存在差异"（含事件计数）
 *   - 5 个分节表格（步骤/测量/耗时/资源调用/变量），差异行红色高亮
 *   - 每节 >500 行截断并提示（不虚拟滚动、零图表库——纯 HTML 表格 +
 *     --el-* 设计令牌，与 InstrumentGantt 同一套视觉约定）
 *   - 无摘要时显示空态引导文案；原始 payload 不默认展开（行内仅标量值）
 */
import { computed } from 'vue'
import {
  buildDiffSections,
  MAX_ROWS,
  type DiffSummary,
} from '@/utils/diffView'

const props = defineProps<{
  /** 后端 diff 摘要；null 表示尚未对比。 */
  summary: DiffSummary | null
  loading?: boolean
}>()

const sections = computed(() => (props.summary ? buildDiffSections(props.summary) : []))
</script>

<template>
  <div class="execution-diff-panel">
    <div v-if="loading" class="diff-empty">对比中…</div>
    <div v-else-if="!summary" class="diff-empty">选择基线运行后展示对比结果</div>
    <template v-else>
      <div class="diff-header">
        <span class="diff-badge" :class="summary.match ? 'is-match' : 'is-violation'">
          {{ summary.match ? '✓ 全部一致' : '✕ 存在差异' }}
        </span>
        <span class="diff-meta">事件 A={{ summary.meta.events_a }} / B={{ summary.meta.events_b }}</span>
      </div>

      <section v-for="sec in sections" :key="sec.id" class="diff-section" :data-section="sec.id">
        <h4 class="diff-section-title">{{ sec.title }}</h4>
        <p v-if="sec.rows.length === 0" class="diff-none">无差异</p>
        <table v-else class="diff-table">
          <thead>
            <tr>
              <th v-for="h in sec.headers" :key="h">{{ h }}</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="row in sec.rows" :key="row.key" :class="{ 'row-violation': row.status === 'violation' }">
              <td v-for="(cell, i) in row.cells" :key="i">{{ cell }}</td>
            </tr>
          </tbody>
        </table>
        <p v-if="sec.truncated" class="diff-truncated">仅显示前 {{ MAX_ROWS }} 行（共更多差异被折叠）</p>
      </section>
    </template>
  </div>
</template>

<style scoped>
.execution-diff-panel {
  padding: 4px 0;
}
.diff-empty {
  color: var(--el-text-color-secondary);
  font-size: 12px;
  text-align: center;
  padding: 16px 0;
}
.diff-header {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
}
.diff-badge {
  font-size: 12px;
  font-weight: 600;
  padding: 2px 10px;
  border-radius: 4px;
}
.diff-badge.is-match {
  color: var(--el-color-success);
  background: var(--el-color-success-light-9);
  border: 1px solid var(--el-color-success-light-5);
}
.diff-badge.is-violation {
  color: var(--el-color-danger);
  background: var(--el-color-danger-light-9);
  border: 1px solid var(--el-color-danger-light-5);
}
.diff-meta {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
.diff-section {
  margin-bottom: 10px;
}
.diff-section-title {
  font-size: 12px;
  font-weight: 600;
  color: var(--el-text-color-primary);
  margin: 0 0 4px;
}
.diff-none {
  color: var(--el-text-color-secondary);
  font-size: 12px;
  margin: 0;
}
.diff-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 12px;
}
.diff-table th {
  text-align: left;
  color: var(--el-text-color-secondary);
  font-weight: 500;
  padding: 3px 8px;
  border-bottom: 1px solid var(--el-border-color-lighter);
}
.diff-table td {
  padding: 3px 8px;
  border-bottom: 1px solid var(--el-border-color-extra-light);
  color: var(--el-text-color-primary);
  word-break: break-all;
}
.diff-table tr.row-violation td {
  background: var(--el-color-danger-light-9);
}
.diff-truncated {
  color: var(--el-color-warning);
  font-size: 11px;
  margin: 2px 0 0;
}
</style>
