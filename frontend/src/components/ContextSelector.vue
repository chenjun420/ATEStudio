<!--
  上下文选择器 —— 放在头部, 随模式切换内容。

  产测开发 → 产品类型 → 型号
  运行监控 → 厂区 → 工位

  产品类型这一级来自 ATERag 的只读 MCP(`list_models`), 不是本库。
  ATERag 不可达时这一级**保留但标为不可用并说明原因**, 而不是消失 ——
  少一级必须让人看出是少了, 否则它看起来像设计如此。

  另外两个刻意的取舍
  ------------------
  1. **没有默认选中项。** 进入页面时上下文是空的, 不是「自动选中第一个」。自动选中
     会让头部声称一个用户没有选择过的上下文, 于是界面上每个数字都悄悄指向它 ——
     而用户看不出是哪一个。空着, 页面就说「请先选择型号」。
  2. 换上级会清掉下级: 型号只属于一个产品类型, 工位只属于一个厂区, 所以清掉而不是
     留下一个自相矛盾的组合(厂区 A 配一个属于厂区 B 的工位)。

  这一层是**视图筛选**, 不是控制。它不会改变产线在产的型号, 也不会向工位下发
  任何东西 —— 规格 §12 把真实切换单独立项了。页面上也不暗示它会: 控件旁没有
  「应用到产线」之类的说法。
-->
<script setup lang="ts">
import { onMounted, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { useContext } from '@/composables/useContext'

const props = defineProps<{
  /** Which mode's context to show. */
  mode: 'test-dev' | 'runtime'
}>()

const { t } = useI18n()
const {
  productType,
  model,
  plantId,
  stationId,
  productTypes,
  modelsInProductType,
  catalogUnavailable,
  catalogWarning,
  plants,
  loading,
  error,
  stationsInPlant,
  selectProductType,
  selectModel,
  selectPlant,
  selectStation,
  load,
} = useContext()

onMounted(() => {
  void load(props.mode)
})

// Switching mode switches what the selector means, so it reloads rather than
// showing the other mode's options.
watch(
  () => props.mode,
  (next) => {
    void load(next)
  },
)

/** 待签 badge: only on a model that actually has unsigned conditions. */
function pendingFor(code: string): number {
  return modelsInProductType.value.find((m) => m.product_code === code)?.draft_conditions ?? 0
}

function onProductTypeChange(value: string | null | undefined): void {
  selectProductType(value || null)
}
</script>

<template>
  <div class="context-selector" :class="{ 'is-loading': loading }">
    <template v-if="mode === 'test-dev'">
      <el-select
        :model-value="productType"
        :placeholder="t('context.placeholderProductType')"
        class="ctx-select ctx-type"
        clearable
        :disabled="catalogUnavailable || productTypes.length === 0"
        data-testid="ctx-product-type"
        @update:model-value="onProductTypeChange($event ?? null)"
      >
        <template #prefix>
          <span class="ctx-label">{{ t('context.productType') }}</span>
        </template>
        <el-option
          v-for="pt in productTypes"
          :key="pt"
          :label="pt"
          :value="pt"
        />
      </el-select>

      <el-select
        :model-value="model"
        :placeholder="t('context.placeholderModel')"
        class="ctx-select ctx-model"
        clearable
        filterable
        data-testid="ctx-model"
        @update:model-value="selectModel($event || null)"
      >
        <template #prefix>
          <span class="ctx-label">{{ t('context.model') }}</span>
        </template>
        <el-option
          v-for="m in modelsInProductType"
          :key="m.product_code"
          :label="m.product_code"
          :value="m.product_code"
        >
          <span class="ctx-option-code">{{ m.product_code }}</span>
          <span class="ctx-option-meta">
            {{ t('context.modelCounts', {
              requirements: m.requirement_count,
              cases: m.case_count,
            }) }}
          </span>
          <el-tag v-if="pendingFor(m.product_code) > 0" size="small" type="warning">
            {{ t('context.pendingCount', { count: pendingFor(m.product_code) }) }}
          </el-tag>
        </el-option>
      </el-select>
    </template>

    <template v-else>
      <el-select
        :model-value="plantId"
        :placeholder="t('context.placeholderPlant')"
        class="ctx-select ctx-plant"
        clearable
        data-testid="ctx-plant"
        @update:model-value="selectPlant($event || null)"
      >
        <template #prefix>
          <span class="ctx-label">{{ t('context.plant') }}</span>
        </template>
        <el-option
          v-for="p in plants"
          :key="p.id"
          :label="p.name"
          :value="p.id"
        >
          <span class="ctx-option-code">{{ p.name }}（{{ p.code }}）</span>
          <span class="ctx-option-meta">
            {{ t('context.stationCount', { count: p.station_count }) }}
          </span>
        </el-option>
      </el-select>

      <el-select
        :model-value="stationId"
        :placeholder="t('context.placeholderStation')"
        class="ctx-select ctx-station"
        clearable
        :disabled="plants.length === 0"
        data-testid="ctx-station"
        @update:model-value="selectStation($event || null)"
      >
        <template #prefix>
          <span class="ctx-label">{{ t('context.station') }}</span>
        </template>
        <el-option
          v-for="s in stationsInPlant"
          :key="s.id"
          :label="s.name"
          :value="s.id"
        >
          <span class="ctx-option-code">{{ s.name }}（{{ s.code }}）</span>
        </el-option>
      </el-select>
    </template>

    <!--
      The product-type level comes from ATERag. When ATERag is unreachable the
      level stays in place, disabled, and says why — hiding it would make a
      missing capability look like a designed single-level selector.
    -->
    <el-tooltip
      v-if="mode === 'test-dev' && catalogUnavailable"
      :content="catalogWarning || t('context.catalogUnavailableDetail')"
      placement="bottom"
    >
      <span class="ctx-degraded" data-testid="ctx-catalog-unavailable">
        {{ t('context.catalogUnavailable') }}
      </span>
    </el-tooltip>

    <!--
      An empty list is a real state, not an error: nothing has been ingested yet,
      or no plant exists. It gets its own message saying which, because "暂无数据"
      next to an empty dropdown tells the user nothing they can act on.
    -->
    <span v-if="error" class="ctx-error" :title="error">
      {{ t('context.loadFailed') }}
    </span>
    <span
      v-else-if="mode === 'test-dev' && !loading && modelsInProductType.length === 0"
      class="ctx-empty"
    >
      {{ t('context.noModelsYet') }}
    </span>
    <span
      v-else-if="mode === 'runtime' && !loading && plants.length === 0"
      class="ctx-empty"
    >
      {{ t('context.noPlantsYet') }}
    </span>
  </div>
</template>

<style scoped>
.context-selector {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}

.ctx-select {
  min-width: 140px;
}

.ctx-type {
  min-width: 150px;
}

.ctx-model {
  min-width: 190px;
}

.ctx-label {
  font-size: 12px;
  color: rgba(255, 255, 255, 0.7);
  margin-right: 2px;
}

.ctx-option-code {
  margin-right: 10px;
}

.ctx-option-meta {
  color: var(--el-text-color-secondary);
  font-size: 12px;
  margin-right: 8px;
}

.ctx-empty,
.ctx-error,
.ctx-degraded {
  font-size: 12px;
  white-space: nowrap;
}

.ctx-empty {
  color: rgba(255, 255, 255, 0.65);
}

.ctx-error {
  color: var(--el-color-danger);
}

.ctx-degraded {
  color: var(--el-color-warning);
  cursor: help;
  border-bottom: 1px dotted currentColor;
}

.is-loading {
  opacity: 0.7;
}
</style>