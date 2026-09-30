<script setup lang="ts">
/**
 * ATERag 评审向导 (P5) — upload → review → approve, end to end.
 *
 * 五步:
 *   1 上传规格  — 选 bundle, dry-run 预览导入影响
 *   2 冲突处置  — 人工编辑被覆盖的项, 逐条决定
 *   3 条件评审  — 按需求逐条签字 (可批量)
 *   4 流程规划  — 生成测试序列, 展示分段/未覆盖/无判据
 *   5 批准导出  — 落库 + 导出计划与脚本
 *
 * 为什么向导要分五步而不是一个页面
 * --------------------------------
 * 这五步是**五个不同的人在做不同性质的决定**, 混在一起会互相掩护:
 *   - 上传的人未必懂判据, 评审的人未必碰得动导入
 *   - 批量批准是最危险的一步(红线: 未签字的条件不得作为产测判据),
 *     它的上一步必须已经把"要签的是什么"摊开
 *   - 覆盖率与缺口必须在批准之前看: 签了字才发现某需求一个条件都没有,
 *     已经来不及了
 * 混成一页的真正后果是: 每一步都在别人看不见的状态下发生, 出问题时
 * 无从判断是哪一步做错了。
 *
 * 红线的位置
 * ----------
 * 第 3 步只列出 draft 条件并要求署名; 第 4 步如实显示"哪些需求没有可
 * 执行步骤"; 第 5 步在存在未签字条件时**拒绝**落库。没有静默通过的地方。
 */
import { computed, onMounted, ref, watch } from 'vue'
import {
  ElAlert,
  ElButton,
  ElCard,
  ElDescriptions,
  ElDescriptionsItem,
  ElEmpty,
  ElForm,
  ElFormItem,
  ElInput,
  ElOption,
  ElSelect,
  ElSkeleton,
  ElStep,
  ElSteps,
  ElTable,
  ElTableColumn,
  ElTag,
  ElMessage,
} from 'element-plus'
import {
  approveConditions,
  fetchConditionSummary,
  fetchConditions,
  type ConditionSummary,
  type ConditionSummaryRow,
  type TestCondition,
} from '@/api/testConditions'
import { importBundle, planBundle, type ImportPreview, type PlanPreview } from '@/api/ateragImport'

// ─── Step state ─────────────────────────────────────────────────────────────

const step = ref(0)

/**
 * Step 1 is named "导入抽取结果", not "上传规格".
 *
 * The earlier name promised something this screen cannot do. Uploading the
 * spec happens in ATERag (`ingest_document`); by the time anything reaches
 * this wizard, extraction has already run and what arrives is a bundle. An
 * engineer who read "上传规格" and came here to load a document found no such
 * control and no explanation — the step looked broken rather than misplaced.
 */
const STEPS = ['导入抽取结果', '冲突处置', '条件评审', '流程规划', '批准导出']

/** Steps that must be cleared before the next is reachable. */
const canAdvance = computed<boolean[]>(() => [
  Boolean(importPreview.value || loadingImport.value),
  true, // conflicts are resolved or explicitly waived, not silently accepted
  signature.value.trim().length > 0 || approvedThisSession.value > 0,
  Boolean(planPreview.value || loadingPlan.value),
  true,
])

function goNext(): void {
  if (step.value < STEPS.length - 1 && canAdvance.value[step.value]) step.value += 1
}

// ─── 1. Upload / dry-run ─────────────────────────────────────────────────────

const bundleText = ref('')
const bundleFileName = ref('')
const loadingImport = ref(false)
const importPreview = ref<ImportPreview | null>(null)
const importError = ref<string | null>(null)

function onFileChosen(file: File): void {
  bundleFileName.value = file.name
  const reader = new FileReader()
  reader.onload = () => {
    bundleText.value = String(reader.result ?? '')
  }
  reader.readAsText(file)
}

const fileInput = ref<HTMLInputElement | null>(null)

/** Opens the picker via a ref, not `document.getElementById` — templates have
 * no `document` in scope, and an id lookup is a global implicit dependency
 * that breaks the moment the component is mounted twice. */
function pickFile(): void {
  fileInput.value?.click()
}

function onFileInput(e: Event): void {
  const input = e.target as HTMLInputElement
  const file = input.files?.[0]
  if (file) onFileChosen(file)
}

/**
 * Dry-run the import.
 *
 * Dry-run first, always. A wrong import overwrites reviewed conditions and
 * approved cases; the cost of looking before committing is zero, and the cost
 * of not looking is a re-review of a whole product.
 */
async function runDryRun(): Promise<void> {
  if (!bundleText.value.trim()) {
    importError.value = '请先选择或粘贴 ATERag 导出的 bundle JSON'
    return
  }
  loadingImport.value = true
  importError.value = null
  try {
    importPreview.value = await importBundle(bundleText.value, true)
  } catch (e) {
    importError.value = e instanceof Error ? e.message : String(e)
  } finally {
    loadingImport.value = false
  }
}

// ─── 2. Conflicts ────────────────────────────────────────────────────────────

const confirmedOverwrite = ref(false)

const conflicts = computed(() => importPreview.value?.conflicts ?? [])

/**
 * Whether the conflicts block the upload.
 *
 * They do, unless the operator ticks the box. A conflict means ATERag would
 * overwrite something a human wrote; proceeding silently is how a reviewer's
 * edit disappears and nobody notices until the criteria are wrong.
 */
const conflictsBlocking = computed(() => conflicts.value.length > 0 && !confirmedOverwrite.value)

// ─── 3. Condition review ─────────────────────────────────────────────────────

const productCode = ref('')
const summary = ref<ConditionSummary>({ items: [], total: 0, pending_total: 0 })
const loadingSummary = ref(false)
const summaryError = ref<string | null>(null)

const selectedRequirement = ref<ConditionSummaryRow | null>(null)
const conditions = ref<TestCondition[]>([])
const loadingConditions = ref(false)
const conditionPage = ref(1)
const conditionPageSize = ref(50)
const conditionTotal = ref(0)
const sideFilter = ref<'all' | 'input' | 'output'>('all')
const statusFilter = ref<'all' | 'draft' | 'approved'>('draft')

const signature = ref('')
const approving = ref(false)
const approvedThisSession = ref(0)

const selectedIds = ref<Set<string>>(new Set())

/** Requirements with drafts still awaiting a decision — the actual work list. */
const pendingRows = computed(() => summary.value.items.filter((r) => r.draft > 0))

/** Stale requirements are kept for traceability but are not reviewable work. */
const staleRows = computed(() =>
  summary.value.items.filter((r) => r.requirement_status === 'stale'),
)

async function loadSummary(): Promise<void> {
  loadingSummary.value = true
  summaryError.value = null
  try {
    summary.value = await fetchConditionSummary(productCode.value || undefined)
  } catch (e) {
    summaryError.value = e instanceof Error ? e.message : String(e)
  } finally {
    loadingSummary.value = false
  }
}

async function openRequirement(row: ConditionSummaryRow): Promise<void> {
  selectedRequirement.value = row
  selectedIds.value = new Set()
  conditionPage.value = 1
  await loadConditions()
}

async function loadConditions(): Promise<void> {
  if (!selectedRequirement.value) return
  loadingConditions.value = true
  try {
    const page = await fetchConditions({
      requirement_id: selectedRequirement.value.requirement_id,
      product_code: productCode.value || undefined,
      side: sideFilter.value === 'all' ? undefined : sideFilter.value,
      status: statusFilter.value === 'all' ? undefined : statusFilter.value,
      skip: (conditionPage.value - 1) * conditionPageSize.value,
      limit: conditionPageSize.value,
    })
    conditions.value = page.items
    conditionTotal.value = page.total
    // The spec clause is carried on every condition row (denormalised server
    // side), so it is read off the first one rather than fetched separately.
    // Taken from the page in view: a reviewer signing page 3 is looking at
    // page 3's clause, and an empty list legitimately has nothing to show.
    specText.value = {
      requirement_description: page.items[0]?.requirement_description ?? null,
      requirement_notes: page.items[0]?.requirement_notes ?? null,
    }
    // Selection is per-page: keeping ids from a previous page would approve
    // conditions the reviewer cannot currently see.
    selectedIds.value = new Set()
  } finally {
    loadingConditions.value = false
  }
}

/** The spec clause behind the conditions currently on screen. */
const specText = ref<{ requirement_description: string | null; requirement_notes: string | null }>({
  requirement_description: null,
  requirement_notes: null,
})

/**
 * Whether this clause was written by a person rather than cut by the rules.
 *
 * `annotation_draft` is set on the owning requirement, so it rides along on
 * every one of its conditions. A requirement can also have a mix — some
 * clauses rule-cut, some hand-written — which is why this is per row.
 */
function isAnnotationDraft(row: TestCondition): boolean {
  return row.requirement_flags.includes('annotation_draft')
}

watch([sideFilter, statusFilter, conditionPage], () => {
  void loadConditions()
})

function toggleRow(row: TestCondition): void {
  const next = new Set(selectedIds.value)
  if (next.has(row.id)) next.delete(row.id)
  else next.add(row.id)
  selectedIds.value = next
}

const allOnPageSelected = computed(
  () => conditions.value.length > 0 && conditions.value.every((c) => selectedIds.value.has(c.id)),
)

function togglePage(): void {
  selectedIds.value = allOnPageSelected.value
    ? new Set()
    : new Set(conditions.value.map((c) => c.id))
}

async function runApprove(): Promise<void> {
  if (!selectedRequirement.value) return
  if (!signature.value.trim()) {
    ElMessage.error('请先填写签字人: 无署名的批准无法审计')
    return
  }
  approving.value = true
  try {
    const result = await approveConditions({
      requirement_id: selectedRequirement.value.requirement_id,
      by: signature.value,
      // An empty selection means "all drafts under this requirement" — the
      // same rule the API applies, so the two never disagree.
      condition_ids: selectedIds.value.size ? [...selectedIds.value] : undefined,
    })
    ElMessage.success(result.message)
    approvedThisSession.value += result.approved
    await loadSummary()
    await loadConditions()
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : String(e))
  } finally {
    approving.value = false
  }
}

// ─── 4. Flow planning ────────────────────────────────────────────────────────

const loadingPlan = ref(false)
const planPreview = ref<PlanPreview | null>(null)
const planError = ref<string | null>(null)

async function runPlan(): Promise<void> {
  if (!bundleText.value.trim()) {
    planError.value = '需要 bundle JSON 才能规划流程'
    return
  }
  loadingPlan.value = true
  planError.value = null
  try {
    planPreview.value = await planBundle(bundleText.value)
  } catch (e) {
    planError.value = e instanceof Error ? e.message : String(e)
  } finally {
    loadingPlan.value = false
  }
}

// ─── 5. Approve + export ─────────────────────────────────────────────────────

const committing = ref(false)
const commitResult = ref<string | null>(null)

/** Blocking issues for the final step, computed not guessed. */
const blockers = computed<string[]>(() => {
  const out: string[] = []
  if (conflictsBlocking.value) {
    out.push(`${conflicts.value.length} 处人工修改会被 ATERag 覆盖, 需显式确认`)
  }
  if (summary.value.pending_total > 0) {
    out.push(`${summary.value.pending_total} 条条件未签字 —— 按红线不得作为产测判据`)
  }
  const gaps = planPreview.value?.gaps?.length ?? 0
  if (gaps > 0) {
    out.push(`${gaps} 个测量步骤无数值判据, 运行时只会报通过`)
  }
  if (!planPreview.value) {
    out.push('尚未生成流程计划')
  }
  return out
})

const canCommit = computed(() => blockers.value.length === 0 && !committing.value)

async function runCommit(): Promise<void> {
  if (!canCommit.value) return
  committing.value = true
  try {
    const res = await importBundle(bundleText.value, false, confirmedOverwrite.value)
    commitResult.value = res.message ?? '导入完成'
    ElMessage.success('已落库')
    await loadSummary()
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : String(e))
  } finally {
    committing.value = false
  }
}

function downloadPlan(): void {
  if (!planPreview.value?.plan_yaml) return
  const blob = new Blob([planPreview.value.plan_yaml], { type: 'text/yaml' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = `${productCode.value || 'aterag'}_plan.yaml`
  a.click()
  URL.revokeObjectURL(url)
}

// ─── Lifecycle ───────────────────────────────────────────────────────────────

onMounted(loadSummary)
</script>

<template>
  <div class="wizard p-4">
    <ElSteps :active="step" align-center finish-status="success" class="mb-6">
      <ElStep v-for="(s, i) in STEPS" :key="i" :title="s" />
    </ElSteps>

    <!-- ── 1. Upload ────────────────────────────────────────────────────── -->
    <ElCard v-if="step === 0" shadow="never">
      <template #header>第 1 步 · 导入规格书抽取结果</template>
      <ElAlert
        class="mb-3"
        type="info"
        :closable="false"
        show-icon
        title="这里导入的是抽取结果, 不是规格书原件"
        description="规格书原件由 ATERag 侧上传 (MCP 工具 ingest_document), 抽取完成后用 export_studio.py 导出 studio_bundle.json, 再回到本页导入。如果还没有 bundle, 请先在 ATERag 侧完成上传与抽取。"
      />
      <ElForm label-width="120px">
        <ElFormItem label="bundle JSON">
          <el-input
            v-model="bundleText"
            type="textarea"
            :rows="3"
            placeholder="选择 ATERag 导出的 studio_bundle.json, 或直接粘贴"
            @input="bundleFileName = ''"
          />
        </ElFormItem>
      </ElForm>
      <p class="text-xs text-gray-500 mb-3">
        先做 dry-run 预览再落库。错误的导入会覆盖已评审的条件和已批准的用例,
        撤销代价远高于看一眼。
      </p>
      <div class="flex gap-2">
        <el-button v-if="!bundleText" @click="pickFile">选择文件</el-button>
        <input
          ref="fileInput"
          type="file"
          accept=".json,application/json"
          class="hidden"
          @change="onFileInput"
        />
        <ElButton type="primary" :loading="loadingImport" @click="runDryRun">
          预览导入影响 (dry-run)
        </ElButton>
      </div>
      <ElAlert v-if="importError" class="mt-3" type="error" :closable="false" :title="importError" />
      <ElAlert
        v-if="bundleFileName"
        class="mt-3"
        type="info"
        :closable="false"
        :title="`已载入 ${bundleFileName}`"
      />
    </ElCard>

    <!-- ── 2. Conflicts ─────────────────────────────────────────────────── -->
    <ElCard v-else-if="step === 1" shadow="never">
      <template #header>第 2 步 · 冲突处置</template>
      <ElEmpty v-if="conflicts.length === 0" description="无冲突: 不会覆盖任何人工修改" />
      <template v-else>
        <ElAlert
          type="warning"
          :closable="false"
          title="ATERag 产出的字段是规格书的权威值, 与人工修改冲突时会覆盖后者"
          class="mb-3"
        />
        <ElTable :data="conflicts" size="small" max-height="320">
          <ElTableColumn prop="identifier" label="对象" min-width="180" />
          <ElTableColumn prop="field" label="字段" width="120" />
          <ElTableColumn prop="current_value" label="当前值" min-width="160" show-overflow-tooltip />
          <ElTableColumn prop="incoming_value" label="将写入" min-width="160" show-overflow-tooltip />
          <ElTableColumn prop="last_modified_by" label="最后修改者" width="120" />
        </ElTable>
        <ElAlert type="error" :closable="false" class="mt-3">
          <el-checkbox v-model="confirmedOverwrite">
            我已逐条核对, 确认以规格书权威值覆盖上述人工修改
          </el-checkbox>
        </ElAlert>
      </template>
    </ElCard>

    <!-- ── 3. Condition review ──────────────────────────────────────────── -->
    <ElCard v-else-if="step === 2" shadow="never">
      <template #header>
        <div class="flex items-center justify-between">
          <span>第 3 步 · 条件评审签字</span>
          <ElTag v-if="summary.pending_total > 0" type="warning">
            {{ summary.pending_total }} 条待签字
          </ElTag>
          <ElTag v-else type="success">全部已签字</ElTag>
        </div>
      </template>

      <div class="grid grid-cols-[320px_1fr] gap-4">
        <!-- work list -->
        <div>
          <ElInput v-model="productCode" placeholder="按产品过滤(可空)" class="mb-2" clearable>
            <template #append>
              <ElButton @click="loadSummary">查询</ElButton>
            </template>
          </ElInput>
          <ElSkeleton v-if="loadingSummary" :rows="5" animated />
          <ElAlert
            v-else-if="summaryError"
            type="error"
            :closable="false"
            :title="summaryError"
          />
          <div v-else class="max-h-[420px] overflow-auto">
            <div
              v-for="row in pendingRows"
              :key="row.requirement_id"
              class="p-2 rounded cursor-pointer border-b border-gray-100 hover:bg-blue-50"
              :class="{ 'bg-blue-100': selectedRequirement?.requirement_id === row.requirement_id }"
              @click="openRequirement(row)"
            >
              <div class="text-sm font-medium">
                <span class="text-gray-400">{{ row.section_path }}</span>
                {{ row.requirement_code }}
              </div>
              <div class="text-xs text-gray-500 truncate">{{ row.title }}</div>
              <ElTag size="small" type="warning" class="mt-1">
                {{ row.draft }} 条待签
              </ElTag>
            </div>
            <ElEmpty
              v-if="pendingRows.length === 0"
              description="没有待签字条件"
              :image-size="60"
            />
            <div v-if="staleRows.length" class="mt-3 p-2 bg-gray-50 rounded">
              <div class="text-xs text-gray-500 mb-1">
                stale (新版本规格书中已消失, 保留仅为追溯, 不计为待办)
              </div>
              <div v-for="row in staleRows" :key="row.requirement_id" class="text-xs text-gray-400">
                {{ row.requirement_code }}
              </div>
            </div>
          </div>
        </div>

        <!-- detail -->
        <div>
          <div v-if="!selectedRequirement" class="text-gray-400 text-sm py-8 text-center">
            从左侧选择一个需求开始评审
          </div>
          <template v-else>
            <div class="flex items-center gap-2 mb-3 flex-wrap">
              <ElTag>{{ selectedRequirement.section_path }}</ElTag>
              <span class="font-medium">{{ selectedRequirement.requirement_code }}</span>
              <span class="text-sm text-gray-500">{{ selectedRequirement.title }}</span>
            </div>

            <!--
              The spec clause, above the extracted conditions.

              Signing means confirming that the clause says what the condition
              claims. That comparison cannot be done from the condition table
              alone: every row is a distilled fragment, and a fragment that
              looks reasonable can still invert the original's meaning. Putting
              the clause on screen is what makes the signature mean anything —
              without it a reviewer is signing a blank form.

              The note is shown too because the number is often in the table
              while "what this number means" is only in the note.
            -->
            <div class="mb-3 p-3 rounded border border-gray-200 bg-gray-50">
              <div class="text-xs text-gray-500 mb-1">
                规格书原文
                <span v-if="selectedRequirement.section_path" class="ml-1">
                  · 条款 {{ selectedRequirement.section_path }}
                </span>
              </div>
              <div class="text-sm leading-relaxed whitespace-pre-wrap">
                {{ specText.requirement_description || '（该需求没有原文描述）' }}
              </div>
              <div v-if="specText.requirement_notes" class="mt-2 pt-2 border-t border-gray-200">
                <div class="text-xs text-gray-500 mb-1">原文备注</div>
                <div class="text-sm text-gray-700 whitespace-pre-wrap">
                  {{ specText.requirement_notes }}
                </div>
              </div>
            </div>

            <div class="flex gap-2 mb-2 items-center flex-wrap">
              <ElSelect v-model="statusFilter" size="small" style="width: 120px">
                <ElOption label="待签字" value="draft" />
                <ElOption label="已签字" value="approved" />
                <ElOption label="全部" value="all" />
              </ElSelect>
              <ElSelect v-model="sideFilter" size="small" style="width: 120px">
                <ElOption label="输入侧" value="input" />
                <ElOption label="输出侧" value="output" />
                <ElOption label="两侧" value="all" />
              </ElSelect>
              <ElInput
                v-model="signature"
                size="small"
                style="width: 200px"
                placeholder="签字人姓名/工号"
              />
              <ElButton
                type="primary"
                size="small"
                :loading="approving"
                :disabled="!signature.trim()"
                @click="runApprove"
              >
                {{ selectedIds.size ? `批准选中 ${selectedIds.size} 条` : '批准本需求全部待签' }}
              </ElButton>
            </div>
            <p class="text-xs text-gray-500 mb-2">
              不勾选则批准本需求全部待签条件。签字会记录在服务端日志, 条件状态由
              draft 变为 approved。
            </p>

            <ElTable
              :data="conditions"
              size="small"
              v-loading="loadingConditions"
              row-key="id"
              height="380"
            >
              <ElTableColumn width="46" align="center">
                <template #header>
                  <input type="checkbox" :checked="allOnPageSelected" @change="togglePage" />
                </template>
                <template #default="{ row }">
                  <input
                    type="checkbox"
                    :checked="selectedIds.has(row.id)"
                    @change="toggleRow(row as TestCondition)"
                  />
                </template>
              </ElTableColumn>
              <ElTableColumn prop="side" label="侧" width="70">
                <template #default="{ row }">
                  <ElTag size="small" :type="row.side === 'input' ? 'warning' : 'success'">
                    {{ row.side === 'input' ? '输入' : '输出' }}
                  </ElTag>
                </template>
              </ElTableColumn>
              <ElTableColumn prop="kind" label="种类" width="150" />
              <ElTableColumn prop="text" label="内容" min-width="220" show-overflow-tooltip>
                <template #default="{ row }">
                  <div>{{ row.text }}</div>
                  <!--
                    Marks clauses a person wrote rather than the rules cut.
                    Without this they render like any other row, and a reviewer
                    has no way to know these are the ones the red line is about:
                    unapproved criteria that must not become production bounds.
                  -->
                  <ElTag
                    v-if="isAnnotationDraft(row)"
                    size="small"
                    type="danger"
                    class="mt-1"
                  >
                    人工注记 · 未经签字不得作为判据
                  </ElTag>
                </template>
              </ElTableColumn>
              <ElTableColumn label="值" min-width="180" show-overflow-tooltip>
                <template #default="{ row }">
                  <span v-if="row.value" class="font-mono text-xs">
                    {{ JSON.stringify(row.value) }}
                  </span>
                  <span v-else class="text-gray-400">—</span>
                </template>
              </ElTableColumn>
              <ElTableColumn prop="source" label="来源" width="120" />
              <ElTableColumn prop="status" label="状态" width="90">
                <template #default="{ row }">
                  <ElTag size="small" :type="row.status === 'approved' ? 'success' : 'warning'">
                    {{ row.status === 'approved' ? '已签' : '待签' }}
                  </ElTag>
                </template>
              </ElTableColumn>
            </ElTable>

            <el-pagination
              v-model:current-page="conditionPage"
              :page-size="conditionPageSize"
              :total="conditionTotal"
              layout="total, prev, pager, next"
              size="small"
              class="mt-2"
            />
          </template>
        </div>
      </div>
    </ElCard>

    <!-- ── 4. Plan ──────────────────────────────────────────────────────── -->
    <ElCard v-else-if="step === 3" shadow="never">
      <template #header>第 4 步 · 流程规划</template>
      <ElButton type="primary" :loading="loadingPlan" @click="runPlan">生成测试序列</ElButton>
      <ElAlert v-if="planError" class="mt-3" type="error" :closable="false" :title="planError" />
      <template v-if="planPreview">
        <ElDescriptions class="mt-3" :column="4" border size="small">
          <ElDescriptionsItem label="分段">{{ planPreview.segments }}</ElDescriptionsItem>
          <ElDescriptionsItem label="步骤">{{ planPreview.steps }}</ElDescriptionsItem>
          <ElDescriptionsItem label="逐台稳定">
            {{ planPreview.settle_s.toFixed(1) }}s
          </ElDescriptionsItem>
          <ElDescriptionsItem label="未映射场景">
            {{ planPreview.unmapped?.length ?? 0 }}
          </ElDescriptionsItem>
          <ElDescriptionsItem label="未批准条件(已排除)">
            {{ planPreview.pending?.length ?? 0 }}
          </ElDescriptionsItem>
          <ElDescriptionsItem label="无判据测量">
            {{ planPreview.gaps?.length ?? 0 }}
          </ElDescriptionsItem>
        </ElDescriptions>

        <ElAlert
          v-if="(planPreview.pending?.length ?? 0) > 0"
          type="warning"
          class="mt-3"
          :closable="false"
          :title="`${planPreview.pending.length} 条条件未签字, 已排除在执行序列之外。未签字的条件不得作为产测判据。`"
        />
        <ElAlert
          v-if="(planPreview.gaps?.length ?? 0) > 0"
          type="error"
          class="mt-3"
          :closable="false"
          :title="`${planPreview.gaps.length} 个测量步骤没有数值判据, 运行时只会报通过, 不得上机`"
        />

        <div v-if="(planPreview.unmapped?.length ?? 0) > 0" class="mt-3">
          <div class="text-sm font-medium mb-1">未映射场景 (这些需求不会被测到)</div>
          <ElTable :data="planPreview.unmapped" size="small" max-height="200">
            <ElTableColumn prop="requirement_code" label="需求" min-width="200" />
            <ElTableColumn prop="scenario_name" label="场景" min-width="160" />
            <ElTableColumn prop="missing" label="缺什么" min-width="160" />
          </ElTable>
        </div>

        <div v-if="planPreview.warnings?.length" class="mt-3">
          <ElAlert
            v-for="(w, i) in planPreview.warnings"
            :key="i"
            type="info"
            :closable="false"
            :title="w"
            class="mb-1"
          />
        </div>
      </template>
    </ElCard>

    <!-- ── 5. Commit ────────────────────────────────────────────────────── -->
    <ElCard v-else-if="step === 4" shadow="never">
      <template #header>第 5 步 · 批准落库与导出</template>
      <ElAlert v-if="blockers.length" type="error" :closable="false" title="尚不可落库">
        <ul class="list-disc pl-5 mt-1">
          <li v-for="(b, i) in blockers" :key="i">{{ b }}</li>
        </ul>
      </ElAlert>
      <ElAlert
        v-else
        type="success"
        :closable="false"
        title="所有前置条件已满足: 无冲突, 无未签字条件, 无无判据测量"
        class="mb-3"
      />
      <div class="flex gap-2">
        <ElButton type="primary" :disabled="!canCommit" :loading="committing" @click="runCommit">
          确认落库
        </ElButton>
        <ElButton :disabled="!planPreview?.plan_yaml" @click="downloadPlan">下载计划 YAML</ElButton>
      </div>
      <ElAlert
        v-if="commitResult"
        class="mt-3"
        type="success"
        :closable="false"
        :title="commitResult"
      />
      <p class="text-xs text-gray-500 mt-3">
        落库只写需求/条件/限值/用例。测试序列与脚本是草稿, 需补齐本站仪表接线后
        另行签发 —— 生成脚本里的 FILL_IN 标记就是为此。
      </p>
    </ElCard>

    <!-- nav -->
    <div class="flex justify-between mt-4">
      <ElButton :disabled="step === 0" @click="step -= 1">上一步</ElButton>
      <ElButton
        type="primary"
        :disabled="!canAdvance[step]"
        @click="goNext"
      >
        下一步
      </ElButton>
    </div>
  </div>
</template>

<style scoped>
.wizard {
  max-width: 1400px;
  margin: 0 auto;
}
</style>
