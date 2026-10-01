/**
 * 型号清单 API.
 *
 * Backs the 产测开发 context selector, and the 型号总览 page that P5 will add.
 * Both read this same contract, so the counts on the overview are the counts
 * the selector showed.
 *
 * `product_type` comes from ATERag, over read-only MCP (`list_models`). It is
 * null when ATERag has no opinion on a model, and `catalog_source` is
 * `"unavailable"` when ATERag could not be asked at all — two different states,
 * and the selector renders them differently on purpose. A level that silently
 * disappears reads as a design decision; one that states why does not.
 *
 * Contract: src/ate_cloud/api/v1/models_catalog.py,
 *           src/ate_cloud/services/aterag_catalog.py
 */
import http from './interceptor'

export interface ModelSummary {
  product_code: string
  /** 产品类型, from ATERag's registry. null when unknown there. */
  product_type: string | null
  requirement_count: number
  case_count: number
  /**
   * 条件里 status 为 draft 的条数 —— 待签。
   *
   * Not "ready" and not "in force". This is the count of conditions nobody has
   * signed, which is the opposite of treating them as judgement criteria.
   */
  draft_conditions: number
  approved_conditions: number
}

export type CatalogSource = 'aterag-mcp' | 'unavailable'

export interface ModelListResponse {
  total: number
  items: ModelSummary[]
  /** 产品类型 known to ATERag. The selector's first level. */
  product_types: string[]
  catalog_source: CatalogSource
  /** Why product_type is unavailable, when it is. */
  catalog_warning: string | null
}

export async function listModels(): Promise<ModelListResponse> {
  const response = await http.get<ModelListResponse>('/models')
  return response.data
}
