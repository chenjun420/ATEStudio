<!--
  工位列表 —— 厂区 / 工位 两级目录。

  Route: /ops/stations

  这个页面以前不存在。顶着「节点列表」名字的那个页面读的是 ``/workers``, 也就是
  执行器进程的注册表, 与 ``stations`` 表无关 —— 它现在叫「工位执行器」, 在
  ``/ops/workers``。两者都在菜单里, 因为它们是两件事: 这一页是产线上的物理位置,
  那一页是在这些位置上跑的执行器。

  数据源: ``GET /api/v1/plants``、``GET /api/v1/stations``、``POST``/``PATCH``/
  ``DELETE`` 同路径。这些端点在 P0 建好并在板卡上端到端验过, 但前端一直没有客户端
  —— 直到现在。

  与上下文选择器的关系: 头部选了厂区之后, 这一页默认只看那个厂区。可以改, 改的是
  这一页的筛选, 不是产线状态。

  删除的诚实性
  ------------
  删厂区: 后端在该厂区仍有工位时返回 409, 不级联。级联会把一条产线的工位连同故障
  历史一起删掉, 作为「整理一个分组」的副作用 —— 而 ``stations.plant_id`` 的 NOT NULL
  本来就会把这种静默删除挡下来。所以这里把 409 原样说给用户, 不假装删成功了。
  删工位: 会连同其故障案例一起删, 因为案例脱离工位没有意义。这一点在确认框里写明。
-->
<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  createPlant,
  createStation,
  deletePlant,
  deleteStation,
  listPlants,
  listStations,
  updateStation,
  type Plant,
  type Station,
} from '@/api/plants'
import { useContext } from '@/composables/useContext'

const router = useRouter()
const { plantId, load: loadContext } = useContext()

const plants = ref<Plant[]>([])
const stations = ref<Station[]>([])
const loading = ref(false)
const saving = ref(false)
const error = ref<string | null>(null)

/** '' = 全部厂区. Defaults to the header's context selection. */
const plantFilter = ref<string>('')
const keyword = ref('')
const showInactive = ref(false)

const dialogVisible = ref(false)
const dialogMode = ref<'create' | 'edit'>('create')
const stationDraft = reactive({
  id: '' as string,
  plant_id: '',
  code: '',
  name: '',
  attributes: '',
  is_active: true,
})

const plantDialogVisible = ref(false)
const plantDraft = reactive({ code: '', name: '' })

// The header's plant selection is the default view here, not a lock: changing
// the filter below only changes this page.
onMounted(async () => {
  await loadContext('runtime')
  if (plantId.value) plantFilter.value = plantId.value
  await refresh()
})

watch(plantId, (next) => {
  if (next) plantFilter.value = next
})

async function refresh(): Promise<void> {
  loading.value = true
  error.value = null
  try {
    const [p, s] = await Promise.all([
      listPlants(),
      listStations(undefined, 0, 200, showInactive.value),
    ])
    plants.value = p.items
    stations.value = s.items
  } catch (e: unknown) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    loading.value = false
  }
}

const filtered = computed<Station[]>(() => {
  const kw = keyword.value.trim().toLowerCase()
  return stations.value.filter((s) => {
    if (plantFilter.value && s.plant_id !== plantFilter.value) return false
    if (!kw) return true
    return (
      s.code.toLowerCase().includes(kw) ||
      s.name.toLowerCase().includes(kw) ||
      (s.plant_name ?? '').toLowerCase().includes(kw)
    )
  })
})

const plantName = (id: string): string =>
  plants.value.find((p) => p.id === id)?.name ?? '—'

function resetDraft(): void {
  stationDraft.id = ''
  stationDraft.plant_id = plantFilter.value || plants.value[0]?.id || ''
  stationDraft.code = ''
  stationDraft.name = ''
  stationDraft.attributes = ''
  stationDraft.is_active = true
}

function openCreate(): void {
  // Creating a station needs a plant. Saying so beats opening a dialog whose
  // only possible submission is a 400.
  if (plants.value.length === 0) {
    ElMessage.warning('请先创建厂区')
    plantDialogVisible.value = true
    return
  }
  resetDraft()
  dialogMode.value = 'create'
  dialogVisible.value = true
}

function openEdit(row: Station): void {
  stationDraft.id = row.id
  stationDraft.plant_id = row.plant_id
  stationDraft.code = row.code
  stationDraft.name = row.name
  stationDraft.attributes = row.attributes ?? ''
  stationDraft.is_active = row.is_active
  dialogMode.value = 'edit'
  dialogVisible.value = true
}

async function submitStation(): Promise<void> {
  saving.value = true
  try {
    if (dialogMode.value === 'create') {
      await createStation({
        plant_id: stationDraft.plant_id,
        code: stationDraft.code.trim(),
        name: stationDraft.name.trim(),
        attributes: stationDraft.attributes.trim() || null,
      })
      ElMessage.success('工位已创建')
    } else {
      // `code` and `plant_id` are not sent: the backend treats both as part of
      // the station's identity and rejects updates to them. The dialog shows
      // them read-only for the same reason.
      await updateStation(stationDraft.id, {
        name: stationDraft.name.trim(),
        attributes: stationDraft.attributes.trim() || null,
        is_active: stationDraft.is_active,
      })
      ElMessage.success('工位已更新')
    }
    dialogVisible.value = false
    await refresh()
  } catch (e: unknown) {
    ElMessage.error(e instanceof Error ? e.message : String(e))
  } finally {
    saving.value = false
  }
}

async function submitPlant(): Promise<void> {
  saving.value = true
  try {
    await createPlant({
      code: plantDraft.code.trim(),
      name: plantDraft.name.trim(),
    })
    ElMessage.success('厂区已创建')
    plantDialogVisible.value = false
    plantDraft.code = ''
    plantDraft.name = ''
    await refresh()
  } catch (e: unknown) {
    ElMessage.error(e instanceof Error ? e.message : String(e))
  } finally {
    saving.value = false
  }
}

async function removeStation(row: Station): Promise<void> {
  const extra =
    row.fault_case_count > 0
      ? `该工位有 ${row.fault_case_count} 条故障案例，将一并删除。案例脱离工位没有意义。`
      : '该工位没有故障案例。'
  try {
    await ElMessageBox.confirm(
      `确认删除工位「${row.name}（${row.code}）」？${extra}`,
      '删除工位',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' },
    )
  } catch {
    return // cancelled
  }
  try {
    await deleteStation(row.id)
    ElMessage.success('工位已删除')
    await refresh()
  } catch (e: unknown) {
    ElMessage.error(e instanceof Error ? e.message : String(e))
  }
}

async function removePlant(row: Plant): Promise<void> {
  try {
    await ElMessageBox.confirm(
      row.station_count > 0
        ? `厂区「${row.name}」下还有 ${row.station_count} 个工位，无法删除。请先移走或删除这些工位。`
        : `确认删除空厂区「${row.name}」？`,
      '删除厂区',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  try {
    await deletePlant(row.id)
    ElMessage.success('厂区已删除')
    await refresh()
  } catch (e: unknown) {
    // 409 arrives here when a station appeared between the count and the
    // delete. The backend's refusal is the correct outcome; say what it said
    // rather than a generic failure.
    ElMessage.error(e instanceof Error ? e.message : String(e))
  }
}

/** The one place a station id is actually known. */
function openOperatorPanel(row: Station): void {
  router.push(`/operator/${row.id}`)
}

function openFaultCases(row: Station): void {
  router.push({ path: '/ops/fault-cases', query: { station_id: row.id } })
}
</script>

<template>
  <div class="page">
    <header class="page-head">
      <div>
        <h1>工位列表</h1>
        <p class="sub">厂区 → 工位。产线上的物理位置，与「工位执行器」是两个页面。</p>
      </div>
      <div class="head-actions">
        <ElButton @click="plantDialogVisible = true">新建厂区</ElButton>
        <ElButton type="primary" @click="openCreate">新建工位</ElButton>
      </div>
    </header>

    <div v-if="error" class="banner">
      <ElTag type="danger">{{ error }}</ElTag>
    </div>

    <div class="filters">
      <ElInput
        v-model="keyword"
        placeholder="搜索工位编码 / 名称 / 厂区"
        clearable
        class="f-keyword"
      />
      <ElSelect v-model="plantFilter" placeholder="全部厂区" clearable class="f-plant">
        <ElOption
          v-for="p in plants"
          :key="p.id"
          :label="`${p.name}（${p.code}）`"
          :value="p.id"
        />
      </ElSelect>
      <ElCheckbox v-model="showInactive" @change="refresh">显示已停用工位</ElCheckbox>
    </div>

    <!-- 厂区一览: it is the top level of the context, and it is also where
         deletion is refused, so the count has to be visible here rather than
         only in a failure message. -->
    <section v-if="plants.length > 0" class="plants">
      <h2>厂区（{{ plants.length }}）</h2>
      <div class="plant-grid">
        <div
          v-for="p in plants"
          :key="p.id"
          class="plant-card"
          :class="{ 'is-inactive': !p.is_active }"
        >
          <div class="plant-main">
            <span class="plant-name">{{ p.name }}</span>
            <ElTag size="small" type="info">{{ p.code }}</ElTag>
            <ElTag v-if="!p.is_active" size="small" type="warning">已停用</ElTag>
          </div>
          <div class="plant-meta">
            {{ p.station_count }} 个工位
          </div>
          <div class="plant-actions">
            <ElButton link type="primary" @click="plantFilter = p.id">只看这个</ElButton>
            <ElButton link type="danger" @click="removePlant(p)">删除</ElButton>
          </div>
        </div>
      </div>
    </section>

    <ElSkeleton v-if="loading && stations.length === 0" :rows="6" animated />

    <ElEmpty
      v-else-if="filtered.length === 0"
      :description="
        stations.length === 0
          ? '还没有工位。先建厂区，再建工位。'
          : '没有符合条件的工位。'
      "
    />

    <ElTable v-else :data="filtered" row-key="id" stripe>
      <ElTableColumn prop="code" label="工位编码" width="140" />
      <ElTableColumn prop="name" label="工位名称" min-width="160" />
      <ElTableColumn label="厂区" min-width="140">
        <template #default="{ row }">{{ plantName(row.plant_id) }}</template>
      </ElTableColumn>
      <ElTableColumn label="状态" width="90">
        <template #default="{ row }">
          <ElTag :type="row.is_active ? 'success' : 'info'" size="small">
            {{ row.is_active ? '启用' : '停用' }}
          </ElTag>
        </template>
      </ElTableColumn>
      <ElTableColumn label="故障案例" width="100">
        <template #default="{ row }">
          <ElButton
            v-if="row.fault_case_count > 0"
            link
            type="primary"
            @click="openFaultCases(row)"
          >
            {{ row.fault_case_count }} 条
          </ElButton>
          <span v-else class="muted">0</span>
        </template>
      </ElTableColumn>
      <ElTableColumn label="操作" width="230" fixed="right">
        <template #default="{ row }">
          <ElButton link type="primary" @click="openOperatorPanel(row)">
            操作员面板
          </ElButton>
          <ElButton link type="primary" @click="openFaultCases(row)">案例</ElButton>
          <ElButton link type="primary" @click="openEdit(row)">编辑</ElButton>
          <ElButton link type="danger" @click="removeStation(row)">删除</ElButton>
        </template>
      </ElTableColumn>
    </ElTable>

    <!-- 工位编辑 -->
    <ElDialog
      v-model="dialogVisible"
      :title="dialogMode === 'create' ? '新建工位' : '编辑工位'"
      width="520px"
    >
      <ElForm label-width="96px">
        <ElFormItem label="所属厂区">
          <!-- Not editable: plant_id is part of the station's identity and the
               backend rejects changes to it. -->
          <ElSelect v-model="stationDraft.plant_id" :disabled="dialogMode === 'edit'">
            <ElOption
              v-for="p in plants"
              :key="p.id"
              :label="`${p.name}（${p.code}）`"
              :value="p.id"
            />
          </ElSelect>
        </ElFormItem>
        <ElFormItem label="工位编码">
          <ElInput
            v-model="stationDraft.code"
            :disabled="dialogMode === 'edit'"
            placeholder="厂区内唯一"
          />
        </ElFormItem>
        <ElFormItem label="工位名称">
          <ElInput v-model="stationDraft.name" />
        </ElFormItem>
        <ElFormItem label="自由属性">
          <ElInput
            v-model="stationDraft.attributes"
            type="textarea"
            :rows="3"
            placeholder="JSON 文本，可留空"
          />
        </ElFormItem>
        <ElFormItem v-if="dialogMode === 'edit'" label="启用">
          <ElSwitch v-model="stationDraft.is_active" />
        </ElFormItem>
      </ElForm>
      <template #footer>
        <ElButton @click="dialogVisible = false">取消</ElButton>
        <ElButton type="primary" :loading="saving" @click="submitStation">保存</ElButton>
      </template>
    </ElDialog>

    <!-- 厂区新建 -->
    <ElDialog v-model="plantDialogVisible" title="新建厂区" width="440px">
      <ElForm label-width="72px">
        <ElFormItem label="厂区编码">
          <ElInput v-model="plantDraft.code" placeholder="全局唯一" />
        </ElFormItem>
        <ElFormItem label="厂区名称">
          <ElInput v-model="plantDraft.name" />
        </ElFormItem>
      </ElForm>
      <template #footer>
        <ElButton @click="plantDialogVisible = false">取消</ElButton>
        <ElButton type="primary" :loading="saving" @click="submitPlant">创建</ElButton>
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
  margin-bottom: 16px;
}

.f-keyword {
  width: 260px;
}

.f-plant {
  width: 200px;
}

.plants {
  margin-bottom: 20px;
}

.plants h2 {
  font-size: 15px;
  margin: 0 0 10px;
}

.plant-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
  gap: 10px;
}

.plant-card {
  border: 1px solid var(--el-border-color);
  border-radius: 6px;
  padding: 10px 12px;
}

.plant-card.is-inactive {
  opacity: 0.6;
}

.plant-main {
  display: flex;
  align-items: center;
  gap: 6px;
}

.plant-name {
  font-weight: 600;
}

.plant-meta {
  color: var(--el-text-color-secondary);
  font-size: 12px;
  margin: 4px 0;
}

.plant-actions {
  display: flex;
  gap: 4px;
}

.muted {
  color: var(--el-text-color-secondary);
}
</style>
