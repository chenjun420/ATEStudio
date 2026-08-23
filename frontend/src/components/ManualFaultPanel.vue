<script setup lang="ts">
/**
 * ManualFaultPanel — 手动故障注入面板（T38，v41-gap-analysis #38，§7.7/§8.4）。
 *
 * 不等待 DSL 规则触发，由操作员直接向运行中的仿真注入故障：
 *   - scope 选择器（link/instrument/step/scheduler/protocol）
 *   - 目标选择：link/instrument 域从拓扑运行时（topologyRuntime.topology）
 *     派生下拉；step/scheduler/protocol 域为自由输入
 *   - 故障类型下拉按 scope 过滤（utils/manualFault.ts 目录，与后端一致）
 *   - params JSON 文本域 + 客户端校验（JSON 合法性 + 数值范围）
 *   - 提交 → POST /executions/{run_id}/manual-fault → toast 反馈；
 *     无活动运行时禁用注入（计划 #38 硬约束）
 *   - 本会话已注入规则列表（规则为 once 语义，仅作历史展示）
 */
import { computed, ref } from 'vue'
import {
  ElButton,
  ElEmpty,
  ElForm,
  ElFormItem,
  ElInput,
  ElMessage,
  ElOption,
  ElSelect,
  ElTag,
} from 'element-plus'
import { injectManualFault, type ManualFaultResponse } from '@/api/executions'
import { useTopologyRuntimeStore } from '@/stores/topologyRuntime'
import {
  MANUAL_FAULT_SCOPES,
  buildManualFaultPayload,
  faultTypesForScope,
  type ManualFaultScope,
} from '@/utils/manualFault'

const props = defineProps<{
  /** 当前活动运行 ID；为空表示无活动运行（禁用注入）。 */
  runId: string | null | undefined
}>()

const runtime = useTopologyRuntimeStore()

// ─── 表单状态 ───────────────────────────────────────────────────────────────

const scope = ref<ManualFaultScope>('link')
const targetId = ref('')
const faultType = ref('')
const paramsText = ref('')
const submitting = ref(false)
const validationError = ref<string | null>(null)

/** 已注入规则（本会话历史，once 规则触发后自动失效）。 */
interface InjectedEntry {
  faultId: string
  scope: string
  layer: string
  targetId: string
  faultType: string
}
const injected = ref<InjectedEntry[]>([])

// ─── 派生 ───────────────────────────────────────────────────────────────────

const faultTypeOptions = computed(() => faultTypesForScope(scope.value))

const hasActiveRun = computed(() => Boolean(props.runId))

/** link 域目标选项：来自拓扑运行时的链路 ID。 */
const linkOptions = computed(() =>
  (runtime.topology?.links ?? []).map((l) => l.id),
)

/** instrument 域目标选项：来自拓扑运行时的仪器 ID。 */
const instrumentOptions = computed(() =>
  (runtime.topology?.instruments ?? []).map((i) => i.id),
)

const targetFromTopology = computed(() => scope.value === 'link' || scope.value === 'instrument')

const targetOptions = computed(() =>
  scope.value === 'link' ? linkOptions.value : instrumentOptions.value,
)

// ─── 动作 ───────────────────────────────────────────────────────────────────

function onScopeChange() {
  // 切换 scope 后原故障类型可能越界：重置并清空拓扑派生目标。
  faultType.value = ''
  if (!targetFromTopology.value) return
  targetId.value = ''
}

async function submit() {
  if (!hasActiveRun.value || !props.runId) {
    ElMessage.warning('无活动运行，无法注入故障')
    return
  }
  const built = buildManualFaultPayload({
    scope: scope.value,
    targetId: targetId.value,
    faultType: faultType.value,
    paramsText: paramsText.value,
  })
  if (!built.ok) {
    validationError.value = built.error
    ElMessage.warning(built.error)
    return
  }
  validationError.value = null
  submitting.value = true
  try {
    const res: ManualFaultResponse = await injectManualFault(props.runId, built.payload)
    injected.value.unshift({
      faultId: res.fault_id,
      scope: res.scope,
      layer: res.layer,
      targetId: res.target_id,
      faultType: res.fault_type,
    })
    ElMessage.success(`故障已注入：${res.target_id} ← ${res.fault_type} (${res.layer})`)
  } catch (e) {
    ElMessage.error(`故障注入失败: ${e instanceof Error ? e.message : String(e)}`)
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <div class="manual-fault-panel" data-testid="manual-fault-panel">
    <el-form label-width="72px" size="small" @submit.prevent>
      <el-form-item label="目标域">
        <el-select
          v-model="scope"
          data-testid="mf-scope"
          style="width: 100%"
          :disabled="!hasActiveRun"
          @change="onScopeChange"
        >
          <el-option v-for="s in MANUAL_FAULT_SCOPES" :key="s.value" :label="s.label" :value="s.value" />
        </el-select>
      </el-form-item>
      <el-form-item label="目标">
        <el-select
          v-if="targetFromTopology"
          v-model="targetId"
          data-testid="mf-target"
          style="width: 100%"
          filterable
          allow-create
          default-first-option
          clearable
          placeholder="从拓扑选择或输入 ID"
          :disabled="!hasActiveRun"
        >
          <el-option v-for="t in targetOptions" :key="t" :label="t" :value="t" />
        </el-select>
        <el-input
          v-else
          v-model="targetId"
          data-testid="mf-target-input"
          :placeholder="scope === 'scheduler' ? '如 * 或站点 ID' : '步骤/资源 ID'"
          :disabled="!hasActiveRun"
        />
      </el-form-item>
      <el-form-item label="故障类型">
        <el-select
          v-model="faultType"
          data-testid="mf-fault-type"
          style="width: 100%"
          placeholder="按目标域过滤"
          :disabled="!hasActiveRun"
        >
          <el-option v-for="t in faultTypeOptions" :key="t.value" :label="t.label" :value="t.value" />
        </el-select>
      </el-form-item>
      <el-form-item label="参数 JSON">
        <el-input
          v-model="paramsText"
          data-testid="mf-params"
          type="textarea"
          :rows="3"
          placeholder='可选，如 {"probability": 0.5}'
          :disabled="!hasActiveRun"
        />
      </el-form-item>
      <div v-if="validationError" class="mf-error" data-testid="mf-error">{{ validationError }}</div>
      <el-form-item label-width="72px">
        <el-button
          type="danger"
          data-testid="mf-submit"
          style="width: 100%"
          :loading="submitting"
          :disabled="!hasActiveRun"
          @click="submit"
        >
          注入故障
        </el-button>
      </el-form-item>
      <div v-if="!hasActiveRun" class="mf-hint" data-testid="mf-no-run-hint">无活动运行 — 注入已禁用</div>
    </el-form>

    <template v-if="injected.length > 0">
      <div class="mf-list-title">本会话已注入（{{ injected.length }}）</div>
      <div v-for="entry in injected" :key="entry.faultId" class="mf-entry" data-testid="mf-entry">
        <el-tag size="small" type="danger">{{ entry.faultType }}</el-tag>
        <span class="mf-entry-target">{{ entry.targetId }}</span>
        <el-tag size="small" type="info">{{ entry.layer }}</el-tag>
      </div>
    </template>
    <el-empty v-else description="尚未注入故障" :image-size="40" />
  </div>
</template>

<style scoped>
.manual-fault-panel {
  width: 100%;
}
.mf-error {
  color: var(--el-color-danger);
  font-size: 12px;
  margin: 0 0 8px 72px;
}
.mf-hint {
  color: var(--el-text-color-secondary);
  font-size: 12px;
  text-align: center;
}
.mf-list-title {
  font-size: 12px;
  font-weight: 600;
  margin: 8px 0 4px;
}
.mf-entry {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  padding: 3px 0;
}
.mf-entry-target {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 110px;
}
</style>
