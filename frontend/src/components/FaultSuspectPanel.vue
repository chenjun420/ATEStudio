<script setup lang="ts">
/**
 * 疑似故障卡片面板（T33，v41-gap-analysis #33，设计文档 §8.3.7 故障定位视图）。
 *
 * 数据源：stores/topologyRuntime.ts::faults（SSE fault 事件，后端
 * FaultLocalizer 已产出定位与建议——本组件绝不重算定位）。
 * 渲染：按严重度降序的卡片列表（severity 徽章配色 + 链路 id + 置信度 +
 * 修复建议）；点击卡片 emit `select-link`（携带链路 id），由父视图在画布上
 * 高亮该链路。纯展示组件：props-down / events-up，无弹窗、不阻塞交互。
 */
import { computed } from 'vue'

import {
  buildSuspectCards,
  type SuspectCard,
  type SuspectFault,
} from '@/utils/faultSuggestions'

const props = defineProps<{ faults: readonly SuspectFault[] }>()

const emit = defineEmits<{ (e: 'select-link', linkId: string): void }>()

const cards = computed<SuspectCard[]>(() => buildSuspectCards(props.faults))

/** severity → el-tag type 配色（critical/error 红、warning 黄、未知灰）。 */
const SEVERITY_TAG: Record<string, 'danger' | 'warning' | 'info'> = {
  critical: 'danger',
  error: 'danger',
  warning: 'warning',
}

function onCardClick(card: SuspectCard) {
  if (!card.linkId) return // 无定位信息的卡片不可点击高亮
  emit('select-link', card.linkId)
}
</script>

<template>
  <div class="fault-suspect-panel">
    <el-empty v-if="cards.length === 0" description="无故障" :image-size="50" />
    <div
      v-for="card in cards"
      :key="card.key"
      class="suspect-card"
      :class="{ clickable: Boolean(card.linkId) }"
      :title="card.linkId ? '点击在画布上高亮该链路' : ''"
      @click="onCardClick(card)"
    >
      <div class="card-head">
        <el-tag size="small" :type="SEVERITY_TAG[card.severity] ?? 'info'">{{ card.severity }}</el-tag>
        <el-tag v-if="card.linkId" size="small" type="info" effect="plain">{{ card.linkId }}</el-tag>
        <span class="ftype">{{ card.faultType }}</span>
        <span v-if="card.confidence != null" class="conf">{{ Math.round(card.confidence * 100) }}%</span>
      </div>
      <div class="msg">{{ card.message }}</div>
      <div class="sugg">💡 {{ card.suggestion }}</div>
    </div>
  </div>
</template>

<style scoped>
.fault-suspect-panel {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.suspect-card {
  border: 1px solid var(--el-border-color-lighter, #ebeef5);
  border-radius: 4px;
  padding: 6px 8px;
  font-size: 12px;
  line-height: 1.5;
}

.suspect-card.clickable {
  cursor: pointer;
}

.suspect-card.clickable:hover {
  border-color: var(--el-color-danger, #f56c6c);
  background: var(--el-color-danger-light-9, #fef0f0);
}

.card-head {
  display: flex;
  align-items: center;
  gap: 4px;
  flex-wrap: wrap;
}

.ftype {
  color: var(--el-text-color-primary, #303133);
  font-weight: 600;
}

.conf {
  margin-left: auto;
  color: var(--el-text-color-secondary, #909399);
}

.msg {
  color: var(--el-text-color-regular, #606266);
  margin-top: 2px;
}

.sugg {
  color: var(--el-color-primary, #409eff);
  margin-top: 2px;
}
</style>
