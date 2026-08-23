/**
 * T35（v41-gap-analysis #35）— 历史故障热力图测试。
 *
 * 覆盖（计划 §35 验收）：
 *   - 纯层：count → heat level 分桶边界（0/1-2/3-5/≥6）
 *   - 空 stats → applyHeatmap 返回空补丁（零历史 = 均匀底色，QA failure 场景）
 *   - attr 补丁：边 line/*、节点 body/*，绿→黄→橙→红色阶 + 按级透明度
 *   - 图例数据：3 档颜色互异、严重度递增
 *   - adapter：toggle off（clearHeatmap）还原基础样式；画布外 id 静默跳过
 *
 * jsdom 安全：只断言纯数据与 attr 补丁；adapter 用最小假 Graph，
 * 不实例化真实 X6。
 */
import { describe, expect, it } from 'vitest'
import type { Graph } from '@antv/x6'

import {
  HEAT_COLORS,
  HEAT_LEGEND,
  HEAT_OPACITIES,
  applyHeatmap,
  heatAttrsFor,
  heatLevelOf,
  type FaultStatsPayload,
} from '../faultHeatmap'
import { clearHeatmap, syncHeatmap } from '../faultHeatmapAdapter'

// ─── 工具 ──────────────────────────────────────────────────────────────────

/** 构造 cellKindOf：仅列出的 id 存在。 */
const kindsOf =
  (cells: Record<string, 'edge' | 'node'>) =>
  (id: string): 'edge' | 'node' | undefined =>
    cells[id]

/** 最小假 Graph：记录逐键 attr 写入，供 adapter 断言。 */
function fakeGraph(cells: Record<string, 'edge' | 'node'>) {
  const written: Record<string, Record<string, string | number>> = {}
  const cellOf = (id: string) => {
    const kind = cells[id]
    if (!kind) return undefined
    return {
      isEdge: () => kind === 'edge',
      isNode: () => kind === 'node',
      attr: (k: string, v: string | number) => {
        ;(written[id] ??= {})[k] = v
      },
      getData: () => ({ signalType: 'signal' }),
    }
  }
  const g = {
    getCellById: cellOf,
  } as unknown as Graph
  return { g, written }
}

function statsOf(entries: Record<string, number>): FaultStatsPayload {
  const links: FaultStatsPayload['links'] = {}
  for (const [id, count] of Object.entries(entries)) links[id] = { count, last_seen: null }
  return { links, generated_at: '2026-08-24T00:00:00+00:00' }
}

// ─── 纯层：分桶边界 ────────────────────────────────────────────────────────

describe('heatLevelOf 分桶边界', () => {
  it('T1 边界精确分桶：0→0，1/2→1，3/5→2，6/∞→3', () => {
    expect(heatLevelOf(0)).toBe(0)
    expect(heatLevelOf(1)).toBe(1)
    expect(heatLevelOf(2)).toBe(1)
    expect(heatLevelOf(3)).toBe(2)
    expect(heatLevelOf(5)).toBe(2)
    expect(heatLevelOf(6)).toBe(3)
    expect(heatLevelOf(9999)).toBe(3)
  })

  it('T2 非法输入防御：负数/NaN/非有限值 → level 0', () => {
    expect(heatLevelOf(-1)).toBe(0)
    expect(heatLevelOf(Number.NaN)).toBe(0)
    expect(heatLevelOf(Number.POSITIVE_INFINITY)).toBe(0)
  })
})

// ─── 纯层：attr 补丁 ───────────────────────────────────────────────────────

describe('heatAttrsFor / applyHeatmap', () => {
  it('T3 零历史空 stats → 返回空补丁表（均匀底色 no-op）', () => {
    const patches = applyHeatmap(kindsOf({ L1: 'edge' }), statsOf({}))
    expect(patches).toEqual({})
  })

  it('T4 边补丁写 line/stroke + line/opacity，按级取色与透明度', () => {
    const patch1 = heatAttrsFor(1, 'edge')
    expect(patch1['line/stroke']).toBe(HEAT_COLORS[1])
    expect(patch1['line/opacity']).toBe(HEAT_OPACITIES[1])
    const patch3 = heatAttrsFor(3, 'edge')
    expect(patch3['line/stroke']).toBe(HEAT_COLORS[3])
    expect(patch3['line/opacity']).toBe(HEAT_OPACITIES[3])
    // 级别越高不透明度单调不减
    expect(HEAT_OPACITIES[1]).toBeLessThanOrEqual(HEAT_OPACITIES[2])
    expect(HEAT_OPACITIES[2]).toBeLessThanOrEqual(HEAT_OPACITIES[3])
  })

  it('T5 节点补丁写 body/* 前缀；level 0 不产生补丁', () => {
    const nodePatch = heatAttrsFor(2, 'node')
    expect(nodePatch['body/stroke']).toBe(HEAT_COLORS[2])
    expect(nodePatch['body/opacity']).toBe(HEAT_OPACITIES[2])
    expect(nodePatch['line/stroke']).toBeUndefined()
    expect(heatAttrsFor(0, 'edge')).toEqual({})
  })

  it('T6 applyHeatmap：stats → 每链路补丁；画布外 id 与 level0 静默跳过', () => {
    const patches = applyHeatmap(
      kindsOf({ L1: 'edge', L2: 'edge' }),
      statsOf({ L1: 4, L2: 0, LX: 7 }),
    )
    expect(Object.keys(patches)).toEqual(['L1'])
    expect(patches.L1['line/stroke']).toBe(HEAT_COLORS[2])
  })

  it('T7 图例：3 档、颜色互异且沿绿→黄/橙→红递进、含文案', () => {
    expect(HEAT_LEGEND).toHaveLength(3)
    const colors = HEAT_LEGEND.map((l) => l.color)
    expect(new Set(colors).size).toBe(3)
    expect(colors).toEqual([HEAT_COLORS[1], HEAT_COLORS[2], HEAT_COLORS[3]])
    for (const l of HEAT_LEGEND) {
      expect(l.label.length).toBeGreaterThan(0)
      expect(l.minCount).toBeGreaterThan(0)
    }
    // 严重度递增：minCount 单调递增
    expect(HEAT_LEGEND[0].minCount).toBeLessThan(HEAT_LEGEND[1].minCount)
    expect(HEAT_LEGEND[1].minCount).toBeLessThan(HEAT_LEGEND[2].minCount)
  })
})

// ─── adapter：toggle on/off ────────────────────────────────────────────────

describe('faultHeatmapAdapter toggle on/off', () => {
  it('T8 syncHeatmap 写入热力补丁；clearHeatmap 还原基础样式（toggle off reverts）', () => {
    const { g, written } = fakeGraph({ L1: 'edge', L2: 'edge' })

    syncHeatmap(g, statsOf({ L1: 6 }))
    expect(written.L1?.['line/stroke']).toBe(HEAT_COLORS[3])
    expect(written.L2).toBeUndefined()

    clearHeatmap(g)
    // 还原 = 基础 signal 蓝 #409EFF、透明度回 1（routeHighlightAdapter 单一事实来源）
    expect(written.L1?.['line/stroke']).toBe('#409EFF')
    expect(written.L1?.['line/opacity']).toBe(1)

    // 再次 clear 幂等无异常
    expect(() => clearHeatmap(g)).not.toThrow()
  })

  it('T9 syncHeatmap 二次调用：不再故障的链路被还原（幂等同步）', () => {
    const { g, written } = fakeGraph({ L1: 'edge', L2: 'edge' })

    syncHeatmap(g, statsOf({ L1: 2, L2: 3 }))
    expect(written.L1?.['line/stroke']).toBe(HEAT_COLORS[1])
    expect(written.L2?.['line/stroke']).toBe(HEAT_COLORS[2])

    syncHeatmap(g, statsOf({ L1: 2 })) // L2 掉出热力集
    expect(written.L2?.['line/stroke']).toBe('#409EFF')
    expect(written.L2?.['line/opacity']).toBe(1)
  })

  it('T10 空 stats 同步 = 全量还原（QA failure 场景：零历史均匀底色）', () => {
    const { g, written } = fakeGraph({ L1: 'edge' })
    syncHeatmap(g, statsOf({ L1: 9 }))
    expect(written.L1?.['line/stroke']).toBe(HEAT_COLORS[3])
    syncHeatmap(g, statsOf({}))
    expect(written.L1?.['line/stroke']).toBe('#409EFF')
  })
})
