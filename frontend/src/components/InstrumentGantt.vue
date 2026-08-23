<script setup lang="ts">
/**
 * InstrumentGantt — 仪器调用甘特时间线（T36，v41-gap-analysis #36，§7.6/§8.4）。
 *
 * 职责：把调用日志中的 instrument_call 事件渲染为每仪器一行（重叠调用
 * 分 lane 子行）的水平时间条；顶部刻度轴；悬停 tooltip 显示方法+耗时；
 * 点击条目向父级 emit `call-select`（props-down / events-up 约定）。
 *
 * 边界：
 *   - 区间推导/lane 打包/刻度全部来自纯层 utils/ganttTimeline.ts，本组件
 *     只做几何映射（秒 → px），零图表库、零内部定时器——渲染仅由 props
 *     变化驱动（控制台在运行结束后一次性写入 result，天然满足"重跑期间
 *     不做高频重绘"的约束）；
 *   - 无数据时显示空态文案，不伪造任何区间。
 */
import { computed, ref } from 'vue'
import {
  buildGanttTimeline,
  generateTicks,
  GANTT_LAYOUT,
  type GanttCall,
} from '@/utils/ganttTimeline'

const props = defineProps<{
  /** 调用日志事件（SimulationConsole result.events 或录制平铺事件均可）。 */
  events: ReadonlyArray<unknown>
}>()

const emit = defineEmits<{
  (e: 'call-select', call: GanttCall & { resource: string }): void
}>()

const timeline = computed(() => buildGanttTimeline(props.events as never))
const ticks = computed(() => generateTicks(timeline.value.totalDuration))

// ─── 几何映射（秒 → px）────────────────────────────────────────────────────

const L = GANTT_LAYOUT

/** 仪器块 y 原点：表头 + 前面各仪器 lane 数 × 行高。 */
function blockY(resourceIndex: number): number {
  let y = L.headerHeight
  for (let i = 0; i < resourceIndex; i++) y += (timeline.value.instruments[i]?.laneCount ?? 1) * L.laneHeight
  return y
}

const svgHeight = computed(
  () =>
    L.headerHeight +
    timeline.value.instruments.reduce((acc, inst) => acc + inst.laneCount * L.laneHeight, 0),
)

function xOf(start: number): number {
  if (timeline.value.totalDuration <= 0) return 0
  return (start / timeline.value.totalDuration) * L.width
}

function widthOf(call: GanttCall): number {
  if (timeline.value.totalDuration <= 0) return 0
  // 零宽区间给 2px 可见最小宽度（纯视觉下限，不改变数据）。
  return Math.max(((call.end - call.start) / timeline.value.totalDuration) * L.width, 2)
}

function tickLabel(v: number): string {
  return v >= 1 ? `${Number(v.toFixed(2))}s` : `${Math.round(v * 1000)}ms`
}

// ─── 悬停 tooltip + 点击回传 ────────────────────────────────────────────────

interface TipState {
  visible: boolean
  x: number
  y: number
  text: string
}
const tip = ref<TipState>({ visible: false, x: 0, y: 0, text: '' })

function durationMs(call: GanttCall): string {
  return `${Math.round((call.end - call.start) * 1000)} ms`
}

function onEnter(call: GanttCall, resource: string, evt: MouseEvent): void {
  tip.value = {
    visible: true,
    x: evt.clientX + 12,
    y: evt.clientY + 12,
    text: `${resource} · ${call.method || '(unknown)'} · ${durationMs(call)}${call.hasError ? ' · error' : ''}`,
  }
}

function onLeave(): void {
  tip.value = { ...tip.value, visible: false }
}

function onSelect(call: GanttCall, resource: string): void {
  emit('call-select', { ...call, resource })
}
</script>

<template>
  <div class="instrument-gantt">
    <div v-if="timeline.instruments.length === 0" class="gantt-empty">暂无仪器调用数据</div>
    <div v-else class="gantt-canvas">
      <svg
        class="gantt-svg"
        :width="GANTT_LAYOUT.labelWidth + GANTT_LAYOUT.width"
        :height="svgHeight"
      >
        <!-- 刻度轴 -->
        <g class="gantt-ticks">
          <template v-for="t in ticks" :key="`tick-${t}`">
            <line
              :x1="GANTT_LAYOUT.labelWidth + xOf(t)"
              :x2="GANTT_LAYOUT.labelWidth + xOf(t)"
              :y1="GANTT_LAYOUT.headerHeight - 6"
              :y2="svgHeight"
              class="gantt-tick-line"
            />
            <text
              :x="GANTT_LAYOUT.labelWidth + xOf(t)"
              :y="GANTT_LAYOUT.headerHeight - 8"
              class="gantt-tick-label"
            >{{ tickLabel(t) }}</text>
          </template>
        </g>
        <!-- 行 -->
        <g v-for="(inst, ri) in timeline.instruments" :key="inst.resource">
          <text
            class="gantt-row-label"
            :x="4"
            :y="blockY(ri) + GANTT_LAYOUT.laneHeight / 2 + 4"
          >{{ inst.resource }}</text>
          <rect
            v-for="call in inst.calls"
            :key="`${inst.resource}-${call.idx}`"
            class="gantt-bar"
            :class="{ 'gantt-bar-error': call.hasError }"
            :data-call-idx="call.idx"
            :x="GANTT_LAYOUT.labelWidth + xOf(call.start)"
            :y="blockY(ri) + call.lane * GANTT_LAYOUT.laneHeight + 2"
            :width="widthOf(call)"
            :height="GANTT_LAYOUT.laneHeight - 4"
            rx="2"
            @mouseenter="onEnter(call, inst.resource, $event)"
            @mouseleave="onLeave"
            @click="onSelect(call, inst.resource)"
          />
        </g>
      </svg>
      <div
        v-if="tip.visible"
        class="gantt-tooltip"
        :style="{ left: `${tip.x}px`, top: `${tip.y}px` }"
      >
        {{ tip.text }}
      </div>
    </div>
  </div>
</template>

<style scoped>
.instrument-gantt {
  position: relative;
  padding: 4px 0;
}
.gantt-empty {
  color: var(--el-text-color-secondary);
  font-size: 12px;
  text-align: center;
  padding: 16px 0;
}
.gantt-canvas {
  position: relative;
  overflow-x: auto;
}
.gantt-tick-line {
  stroke: var(--el-border-color-lighter);
  stroke-width: 1;
}
.gantt-tick-label {
  fill: var(--el-text-color-secondary);
  font-size: 10px;
}
.gantt-row-label {
  fill: var(--el-text-color-primary);
  font-size: 12px;
}
.gantt-bar {
  fill: var(--el-color-primary);
  cursor: pointer;
}
.gantt-bar:hover {
  opacity: 0.8;
}
.gantt-bar-error {
  fill: var(--el-color-danger);
}
.gantt-tooltip {
  position: fixed;
  z-index: 10;
  background: var(--el-bg-color-overlay);
  border: 1px solid var(--el-border-color-light);
  border-radius: 4px;
  box-shadow: var(--el-box-shadow-light);
  color: var(--el-text-color-primary);
  font-size: 12px;
  padding: 4px 8px;
  pointer-events: none;
  white-space: nowrap;
}
</style>
