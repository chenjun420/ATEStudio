<!--
  工位故障案例库。

  Route: /ops/fault-cases

  这些案例不是档案, 是**检索语料**: ``/diagnose`` 检索到它们, 然后把 ``cause`` /
  ``fix`` 原文作为带引用的证据返回。所以这一页上「已验证 / 未验证」的区别不是元数据
  的整洁问题 —— 未经验证的措施只能作为候选, 不能作为结论呈现。规格 §9 与
  「未批准的条件不得作为产测判据」同源。

  因此:
  * ``fix_verified`` 默认 False, 且**不显示**为「已修复」, 而是「未验证」。
  * 措施正文永远显示, 但带标记 —— 案例可能被检索到并被引用, 引用的是原文, 所以
    隐去未验证的措施只会让人以为那里没有内容。
  * 索引状态单独一行, 并显示 skipped 的数量。板上曾经发生过「库里有案例、向量库是空的、
    检索静默退化」—— 不报错, 只是找不到。skipped 是「没被索引的条数」, 在缺
    embedding 凭据的部署上它等于总数, 所以必须说出来。
-->
<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { useRoute } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  createFaultCase,
  deleteFaultCase,
  listFaultCases,
  listStations,
  reindexFaultCases,
  updateFaultCase,
  type FaultCase,
  type ReindexResponse,
  type Station,
} from '@/api/plants'

const route = useRoute()

const cases = ref<FaultCase[]>([])
const stations = ref<Station[]>([])
const loading = ref(false)
const saving = ref(false)
const error = ref<string | null>(null)
const reindex = ref<ReindexResponse | null>(null)

const query = reactive({
  station_id: (route.query.station_id as string) || '',
  product_code: '',
  keyword: '',
})

/** Only verified fixes may be presented as answers. */
const onlyVerified = ref(false)

const dialogVisible = ref(false)
const dialogMode = ref<'create' | 'edit'>('create')
const draft = reactive({
  id: '',
  station_id: '',
  symptom: '',
  cause: '',
  effect: '',
  fix: '',
  fix_verified: false,
  product_code: '',
  severity: null as number | null,
  occurrence: null as number | null,
  detection: null as number | null,
  rpn: null as number | null,
})

onMounted(async () => {
  try {
    const s = await listStations(undefined, 0, 200)
    stations.value = s.items
  } catch (e: unknown) {
    error.value = e instanceof Error ? e.message : String(e)
  }
  await refresh()
})

async function refresh(): Promise<void> {
  loading.value = true
  error.value = null
  try {
    const res = await listFaultCases({
      station_id: query.station_id || undefined,
      product_code: query.product_code || undefined,
      skip: 0,
      limit: 200,
    })
    cases.value = res.items
  } catch (e: unknown) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    loading.value = false
  }
}

const filtered = computed<FaultCase[]>(() => {
  const kw = query.keyword.trim().toLowerCase()
  if (!kw) return cases.value
  return cases.value.filter(
    (c) =>
      c.symptom.toLowerCase().includes(kw) ||
      (c.cause ?? '').toLowerCase().includes(kw) ||
      (c.station_name ?? '').toLowerCase().includes(kw) ||
      (c.station_code ?? '').toLowerCase().includes(kw),
  )
})

const verifiedCount = computed(
  () => cases.value.filter((c) => c.fix_verified).length,
)

const stationLabel = (id: string): string => {
  const s = stations.value.find((x) => x.id === id)
  return s ? `${s.name}（${s.code}）` : id
}

function resetDraft(): void {
  draft.id = ''
  draft.station_id = query.station_id || stations.value[0]?.id || ''
  draft.symptom = ''
  draft.cause = ''
  draft.effect = ''
  draft.fix = ''
  // Matches the server default: creating a case that says its fix is verified
  // is a claim someone has to make deliberately, not a box that starts ticked.
  draft.fix_verified = false
  draft.product_code = ''
  draft.severity = null
  draft.occurrence = null
  draft.detection = null
  draft.rpn = null
}

function openCreate(): void {
  if (stations.value.length === 0) {
    ElMessage.warning('还没有工位。请先在「工位列表」里建一个工位。')
    return
  }
  resetDraft()
  dialogMode.value = 'create'
  dialogVisible.value = true
}

function openEdit(row: FaultCase): void {
  Object.assign(draft, {
    id: row.id,
    station_id: row.station_id,
    symptom: row.symptom,
    cause: row.cause ?? '',
    effect: row.effect ?? '',
    fix: row.fix ?? '',
    fix_verified: row.fix_verified,
    product_code: row.product_code ?? '',
    severity: row.severity,
    occurrence: row.occurrence,
    detection: row.detection,
    rpn: row.rpn,
  })
  dialogMode.value = 'edit'
  dialogVisible.value = true
}

async function submit(): Promise<void> {
  if (!draft.station_id) {
    ElMessage.warning('请选择工位')
    return
  }
  if (!draft.symptom.trim()) {
    // The backend rejects an empty symptom, and rightly: a case with no
    // observation cannot be retrieved by anything, so it would sit there
    // making "this station has no history" and "this station has never
    // failed" indistinguishable.
    ElMessage.warning('现象不能为空')
    return
  }
  saving.value = true
  try {
    const body = {
      symptom: draft.symptom.trim(),
      cause: draft.cause.trim() || null,
      effect: draft.effect.trim() || null,
      fix: draft.fix.trim() || null,
      fix_verified: draft.fix_verified,
      product_code: draft.product_code.trim() || null,
      severity: draft.severity,
      occurrence: draft.occurrence,
      detection: draft.detection,
      rpn: draft.rpn,
    }
    if (dialogMode.value === 'create') {
      await createFaultCase({ station_id: draft.station_id, ...body })
      ElMessage.success('案例已录入')
    } else {
      await updateFaultCase(draft.id, body)
      ElMessage.success('案例已更新')
    }
    dialogVisible.value = false
    await refresh()
  } catch (e: unknown) {
    ElMessage.error(e instanceof Error ? e.message : String(e))
  } finally {
    saving.value = false
  }
}

/** Marking a fix verified is a claim about the physical line. */
async function markVerified(row: FaultCase): Promise<void> {
  try {
    await ElMessageBox.confirm(
      `确认「${row.fix ?? '该措施'}」已有人现场确认有效？\n\n` +
        '标记后，诊断建议可以把它作为已验证措施引用。',
      '标记为已验证',
      { type: 'warning', confirmButtonText: '已确认有效', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  try {
    await updateFaultCase(row.id, { fix_verified: true })
    ElMessage.success('已标记为已验证')
    await refresh()
  } catch (e: unknown) {
    ElMessage.error(e instanceof Error ? e.message : String(e))
  }
}

async function removeCase(row: FaultCase): Promise<void> {
  try {
    await ElMessageBox.confirm(
      `确认删除案例「${row.symptom.slice(0, 40)}${row.symptom.length > 40 ? '…' : ''}」？\n\n` +
        '后端会同时移除它的向量 —— 留下向量会让检索引用一条已经不存在的案例。',
      '删除案例',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  try {
    await deleteFaultCase(row.id)
    ElMessage.success('案例已删除')
    await refresh()
  } catch (e: unknown) {
    ElMessage.error(e instanceof Error ? e.message : String(e))
  }
}

async function runReindex(): Promise<void> {
  try {
    await ElMessageBox.confirm(
      '重建向量索引。缺失 embedding 凭据的部署上，被跳过的条数会等于案例总数 —— ' +
        '那时检索会静默退化（不报错，只是找不到）。',
      '重建索引',
      { type: 'info', confirmButtonText: '重建', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  try {
    reindex.value = await reindexFaultCases()
    await refresh()
  } catch (e: unknown) {
    ElMessage.error(e instanceof Error ? e.message : String(e))
  }
}
</script>

<template>
  <div class="page">
    <header class="page-head">
      <div>
        <h1>工位故障案例库</h1>
        <p class="sub">
          这些案例是 <code>/diagnose</code> 的检索语料。措施未经验证时只能作为候选，
          不能作为结论。
        </p>
      </div>
      <div class="head-actions">
        <ElButton @click="runReindex">重建索引</ElButton>
        <ElButton type="primary" @click="openCreate">录入案例</ElButton>
      </div>
    </header>

    <div v-if="error" class="banner"><ElTag type="danger">{{ error }}</ElTag></div>

    <div class="filters">
      <ElInput
        v-model="query.keyword"
        placeholder="搜索现象 / 原因 / 工位"
        clearable
        class="f-keyword"
      />
      <ElSelect
        v-model="query.station_id"
        placeholder="全部工位"
        clearable
        class="f-select"
        @change="refresh"
      >
        <ElOption
          v-for="s in stations"
          :key="s.id"
          :label="`${s.name}（${s.code}）`"
          :value="s.id"
        />
      </ElSelect>
      <ElInput
        v-model="query.product_code"
        placeholder="型号"
        clearable
        class="f-model"
        @change="refresh"
      />
      <ElCheckbox v-model="onlyVerified">只看已验证措施</ElCheckbox>
    </div>

    <div class="summary">
      <span>共 {{ filtered.length }} 条</span>
      <span class="muted">
        已验证措施 {{ verifiedCount }} / {{ cases.length }} 条
      </span>
      <span v-if="reindex" class="muted">
        上次重建：索引 {{ reindex.indexed }} 条，
        <template v-if="reindex.skipped > 0">
          <strong class="warn">跳过 {{ reindex.skipped }} 条</strong>
          （缺 embedding 凭据或索引不可用）
        </template>
        <template v-else>无跳过</template>
      </span>
    </div>

    <ElSkeleton v-if="loading && cases.length === 0" :rows="6" animated />

    <ElEmpty
      v-else-if="filtered.length === 0"
      :description="
        cases.length === 0 ? '案例库还是空的。' : '没有符合条件的案例。'
      "
    />

    <div v-else class="case-list">
      <article
        v-for="c in filtered"
        :key="c.id"
        v-show="!onlyVerified || c.fix_verified"
        class="case"
      >
        <header class="case-head">
          <div class="case-where">
            <ElTag size="small" type="info">{{ stationLabel(c.station_id) }}</ElTag>
            <ElTag v-if="c.product_code" size="small">{{ c.product_code }}</ElTag>
            <ElTag v-if="c.rpn !== null" size="small" type="warning">RPN {{ c.rpn }}</ElTag>
          </div>
          <div class="case-when">{{ new Date(c.occurred_at).toLocaleString() }}</div>
        </header>

        <dl class="case-body">
          <dt>现象</dt>
          <dd class="symptom">{{ c.symptom }}</dd>

          <dt>原因</dt>
          <dd>
            <template v-if="c.cause">{{ c.cause }}</template>
            <span v-else class="muted">未记录</span>
          </dd>

          <dt>影响</dt>
          <dd>
            <template v-if="c.effect">{{ c.effect }}</template>
            <span v-else class="muted">未记录</span>
          </dd>

          <dt>措施</dt>
          <dd>
            <template v-if="c.fix">
              {{ c.fix }}
              <ElTag
                :type="c.fix_verified ? 'success' : 'warning'"
                size="small"
                class="verified-tag"
              >
                {{ c.fix_verified ? '已验证' : '未验证' }}
              </ElTag>
            </template>
            <span v-else class="muted">未记录</span>
          </dd>
        </dl>

        <footer class="case-foot">
          <div class="sod">
            <span v-if="c.severity !== null">S {{ c.severity }}</span>
            <span v-if="c.occurrence !== null">O {{ c.occurrence }}</span>
            <span v-if="c.detection !== null">D {{ c.detection }}</span>
          </div>
          <div class="case-actions">
            <ElButton
              v-if="c.fix && !c.fix_verified"
              link
              type="warning"
              @click="markVerified(c)"
            >
              标记已验证
            </ElButton>
            <ElButton link type="primary" @click="openEdit(c)">编辑</ElButton>
            <ElButton link type="danger" @click="removeCase(c)">删除</ElButton>
          </div>
        </footer>
      </article>
    </div>

    <ElDialog
      v-model="dialogVisible"
      :title="dialogMode === 'create' ? '录入故障案例' : '编辑故障案例'"
      width="640px"
    >
      <ElForm label-width="88px">
        <ElFormItem label="工位" required>
          <ElSelect v-model="draft.station_id" :disabled="dialogMode === 'edit'">
            <ElOption
              v-for="s in stations"
              :key="s.id"
              :label="`${s.name}（${s.code}）`"
              :value="s.id"
            />
          </ElSelect>
        </ElFormItem>
        <ElFormItem label="现象" required>
          <ElInput
            v-model="draft.symptom"
            type="textarea"
            :rows="2"
            placeholder="按原文写。检索匹配的就是这段文字，不做归一化。"
          />
        </ElFormItem>
        <ElFormItem label="原因">
          <ElInput v-model="draft.cause" type="textarea" :rows="2" />
        </ElFormItem>
        <ElFormItem label="影响">
          <ElInput v-model="draft.effect" type="textarea" :rows="2" />
        </ElFormItem>
        <ElFormItem label="措施">
          <ElInput v-model="draft.fix" type="textarea" :rows="2" />
        </ElFormItem>
        <ElFormItem label="措施已验证">
          <ElSwitch v-model="draft.fix_verified" />
          <span class="hint">未勾选时，诊断建议只能把该措施作为候选引用。</span>
        </ElFormItem>
        <ElFormItem label="型号">
          <ElInput v-model="draft.product_code" placeholder="可空：工装类故障与在产型号无关" />
        </ElFormItem>
        <ElFormItem label="S / O / D">
          <div class="sod-row">
            <ElInputNumber v-model="draft.severity" :min="1" :max="10" placeholder="S" />
            <ElInputNumber v-model="draft.occurrence" :min="1" :max="10" placeholder="O" />
            <ElInputNumber v-model="draft.detection" :min="1" :max="10" placeholder="D" />
            <ElInputNumber v-model="draft.rpn" :min="0" placeholder="RPN" />
          </div>
          <span class="hint">
            RPN 按当初评估的值存，不由 S×O×D 重算 —— 两者不符是有意义的信息。
          </span>
        </ElFormItem>
      </ElForm>
      <template #footer>
        <ElButton @click="dialogVisible = false">取消</ElButton>
        <ElButton type="primary" :loading="saving" @click="submit">保存</ElButton>
      </template>
    </ElDialog>
  </div>
</template>

<style scoped>
.page {
  padding: 20px 24px;
}

.page-head {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  margin-bottom: 16px;
}

.page-head h1 {
  margin: 0;
  font-size: 20px;
}

.sub {
  margin: 4px 0 0;
  color: var(--el-text-color-secondary);
  font-size: 13px;
}

.head-actions {
  display: flex;
  gap: 8px;
}

.banner {
  margin-bottom: 12px;
}

.filters {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 12px;
  flex-wrap: wrap;
}

.f-keyword {
  width: 240px;
}

.f-select {
  width: 190px;
}

.f-model {
  width: 150px;
}

.summary {
  display: flex;
  gap: 18px;
  align-items: center;
  font-size: 13px;
  margin-bottom: 10px;
}

.muted {
  color: var(--el-text-color-secondary);
}

.warn {
  color: var(--el-color-warning);
}

.case-list {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.case {
  border: 1px solid var(--el-border-color);
  border-radius: 6px;
  padding: 12px 14px;
}

.case-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.case-where {
  display: flex;
  gap: 6px;
}

.case-when {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.case-body {
  margin: 10px 0 0;
  display: grid;
  grid-template-columns: 52px 1fr;
  gap: 4px 10px;
}

.case-body dt {
  color: var(--el-text-color-secondary);
  font-size: 13px;
}

.case-body dd {
  margin: 0;
  font-size: 14px;
}

.symptom {
  font-weight: 600;
}

.verified-tag {
  margin-left: 6px;
}

.case-foot {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-top: 10px;
  padding-top: 8px;
  border-top: 1px solid var(--el-border-color-lighter);
}

.sod {
  display: flex;
  gap: 12px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.case-actions {
  display: flex;
  gap: 4px;
}

.sod-row {
  display: flex;
  gap: 8px;
}

.hint {
  display: block;
  font-size: 12px;
  color: var(--el-text-color-secondary);
  line-height: 1.5;
}
</style>
