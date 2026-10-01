/**
 * 上下文选择器的状态。
 *
 * 两个模式各有自己的上下文, 互不干扰:
 *
 *   产测开发 → 产品类型 → 型号
 *   运行监控 → 厂区 → 工位
 *
 * 持久化到 localStorage, 因为选择上下文的目的就是「接下来一段时间都看这个」——
 * 刷新一下就丢掉, 等于每次都要重选。
 *
 * 产品类型这一级来自 ATERag
 * ------------------------
 * 本库的 `test_requirements` 只有 `product_code`, `product_configs` 只有
 * `product_type`, 两者之间没有键。映射在 ATERag 的 registry 里, 经只读 MCP 的
 * `list_models` 到达后端 (`api/v1/models`)。
 *
 * 这个模块的初版把这件事写成「没有产品类型这一级」, 并声称要等 P4 的 bundle 契约。
 * 那是错的 —— 映射早就在 ATERag 侧(`ProductEntry{domain}` 按 `model_id` 建索引),
 * 而且已经通过 MCP 暴露、且那个工具早已在 ATERag 的只读白名单里。缺的只是这一侧的
 * 接线。规格 §4.2 要的是两级, 所以这里恢复两级。
 *
 * ATERag 不可达时那一级显示为不可用并说明原因, 而不是悄悄消失: 少一级必须让人看出
 * 是少了, 否则它看起来像设计如此。
 *
 * 切换工位不改变工位在产型号
 * ----------------------------
 * 规格 §12: 本期「型号切换」只是**视图切换**, 服务端状态、NATS 下发与工位回执都不做。
 * 所以 `model` 只影响本界面看什么, 不影响产线在跑什么 —— 界面上也不得暗示它会。
 */

import { computed, readonly, ref, watch } from 'vue'
import type { CatalogSource, ModelSummary } from '@/api/models'

const STORAGE_KEY = 'ate-context'

export type ModelOption = ModelSummary

export interface PlantOption {
  id: string
  code: string
  name: string
  station_count: number
}

export interface StationOption {
  id: string
  code: string
  name: string
  plant_id: string
}

interface PersistedContext {
  productType: string | null
  model: string | null
  plantId: string | null
  stationId: string | null
}

function loadPersisted(): PersistedContext {
  const empty: PersistedContext = {
    productType: null,
    model: null,
    plantId: null,
    stationId: null,
  }
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (!raw) return empty
    const parsed = JSON.parse(raw) as Partial<PersistedContext>
    return {
      productType: typeof parsed.productType === 'string' ? parsed.productType : null,
      model: typeof parsed.model === 'string' ? parsed.model : null,
      plantId: typeof parsed.plantId === 'string' ? parsed.plantId : null,
      stationId: typeof parsed.stationId === 'string' ? parsed.stationId : null,
    }
  } catch {
    // A corrupt value must not take the shell down with it. The worst outcome of
    // dropping the selection is re-picking it, which is cheaper than a blank
    // screen nobody can get out of.
    return empty
  }
}

const initial = loadPersisted()

// Module-level refs: shared across every component, the same way useApps does
// it. A component that created its own copy would silently disagree with the
// header about which station is selected.
const _productType = ref<string | null>(initial.productType)
const _model = ref<string | null>(initial.model)
const _plantId = ref<string | null>(initial.plantId)
const _stationId = ref<string | null>(initial.stationId)

const _models = ref<ModelOption[]>([])
const _productTypes = ref<string[]>([])
const _catalogSource = ref<CatalogSource>('aterag-mcp')
const _catalogWarning = ref<string | null>(null)
const _plants = ref<PlantOption[]>([])
const _stations = ref<StationOption[]>([])
const _loading = ref(false)
const _error = ref<string | null>(null)

watch(
  [_productType, _model, _plantId, _stationId],
  () => {
    const payload: PersistedContext = {
      productType: _productType.value,
      model: _model.value,
      plantId: _plantId.value,
      stationId: _stationId.value,
    }
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(payload))
    } catch {
      // Private-mode quota errors are not worth failing a selection over.
    }
  },
  { deep: false },
)

export function useContext() {
  /**
   * Load whatever the current mode needs.
   *
   * Called on mode change rather than eagerly on mount: the 产测开发 context
   * (models) has nothing to do with 运行监控, and fetching both means every
   * request can fail for reasons the user cannot see.
   */
  async function load(mode: 'test-dev' | 'runtime'): Promise<void> {
    _loading.value = true
    _error.value = null
    try {
      if (mode === 'test-dev') {
        const { listModels } = await import('@/api/models')
        const res = await listModels()
        _models.value = res.items
        _productTypes.value = res.product_types
        _catalogSource.value = res.catalog_source
        _catalogWarning.value = res.catalog_warning
        reconcileProductType()
        reconcileModel()
      } else {
        const { listPlants, listStations } = await import('@/api/plants')
        const [plants, stations] = await Promise.all([
          listPlants(),
          // No plant filter: the selector must be able to show that a plant has
          // no stations, which is the state that decides whether it can be
          // deleted.
          listStations(undefined, 0, 200),
        ])
        _plants.value = plants.items
        _stations.value = stations.items
        reconcilePlant()
        reconcileStation()
      }
    } catch (e: unknown) {
      _error.value = e instanceof Error ? e.message : String(e)
    } finally {
      _loading.value = false
    }
  }

  /** A product type ATERag no longer knows about cannot stay selected. */
  function reconcileProductType(): void {
    if (_productType.value && !_productTypes.value.includes(_productType.value)) {
      _productType.value = null
    }
  }

  /** Drop a selection that no longer exists in the data. */
  function reconcileModel(): void {
    if (_models.value.length === 0) {
      _model.value = null
      return
    }
    if (_model.value && !_models.value.some((m) => m.product_code === _model.value)) {
      _model.value = null
    }
    // No default selection on purpose. Auto-selecting the first model makes the
    // header claim a context the user never chose, and every number on the page
    // then silently refers to it.
  }

  function reconcilePlant(): void {
    if (_plants.value.length === 0) {
      _plantId.value = null
      _stationId.value = null
      return
    }
    if (_plantId.value && !_plants.value.some((p) => p.id === _plantId.value)) {
      _plantId.value = null
      _stationId.value = null
    }
  }

  function reconcileStation(): void {
    if (!_stationId.value) return
    const station = _stations.value.find((s) => s.id === _stationId.value)
    // A station belongs to exactly one plant. If the plant changed under a
    // selected station — or the station is gone — the pair is incoherent.
    // Drop the station rather than showing 厂区 A next to a station in 厂区 B.
    if (station && station.plant_id === _plantId.value) return
    _stationId.value = null
  }

  function selectProductType(productType: string | null): void {
    _productType.value = productType
    // A model belongs to exactly one product type, so changing the type
    // invalidates the model — same shape as changing a plant invalidating a
    // station.
    _model.value = null
  }

  function selectModel(productCode: string | null): void {
    _model.value = productCode
  }

  function selectPlant(plantId: string | null): void {
    _plantId.value = plantId
    // Changing plant invalidates the station: stations are children of plants.
    _stationId.value = null
  }

  function selectStation(stationId: string | null): void {
    _stationId.value = stationId
  }

  /** Models of the selected product type, or all of them when none is chosen. */
  const modelsInProductType = computed<ModelOption[]>(() => {
    if (!_productType.value) return _models.value
    return _models.value.filter((m) => m.product_type === _productType.value)
  })

  const currentModel = computed<ModelOption | null>(
    () => _models.value.find((m) => m.product_code === _model.value) ?? null,
  )

  const currentPlant = computed<PlantOption | null>(
    () => _plants.value.find((p) => p.id === _plantId.value) ?? null,
  )

  const currentStation = computed<StationOption | null>(
    () => _stations.value.find((s) => s.id === _stationId.value) ?? null,
  )

  const stationsInPlant = computed<StationOption[]>(() => {
    if (!_plantId.value) return _stations.value
    return _stations.value.filter((s) => s.plant_id === _plantId.value)
  })

  /** True when the product-type level could not be populated from ATERag. */
  const catalogUnavailable = computed(() => _catalogSource.value === 'unavailable')

  return {
    productType: readonly(_productType),
    model: readonly(_model),
    plantId: readonly(_plantId),
    stationId: readonly(_stationId),
    models: readonly(_models),
    productTypes: readonly(_productTypes),
    catalogUnavailable,
    catalogWarning: readonly(_catalogWarning),
    plants: readonly(_plants),
    stations: readonly(_stations),
    loading: readonly(_loading),
    error: readonly(_error),
    currentModel,
    currentPlant,
    currentStation,
    modelsInProductType,
    stationsInPlant,
    load,
    selectProductType,
    selectModel,
    selectPlant,
    selectStation,
  }
}