/**
 * T32 (v41-gap-analysis #32) — RouteHighlighter 测试。
 *
 * 覆盖两层：
 *   - 纯函数层 utils/routeHighlight.ts（零 X6 依赖）：高亮集合计算 + 激活路由选取
 *   - X6 适配层 utils/routeHighlightAdapter.ts：用最小假 Graph/Cell 验证
 *     accent/dim/clear 的 attr 写入与故障样式优先（fault wins）
 */
import { describe, expect, it } from 'vitest'

import type { RouteLike } from '@/utils/routes'
import {
  computeRouteHighlight,
  pickActiveRouteId,
  type HighlightLinkInput,
  type HighlightRelayInput,
} from '../routeHighlight'
import {
  ACCENT_COLOR,
  DIM_OPACITY,
  applyRouteHighlight,
  clearRouteHighlight,
  syncRouteHighlight,
} from '../routeHighlightAdapter'

// ─── 夹具数据 ────────────────────────────────────────────────────────────────

function link(id: string, extra: Partial<HighlightLinkInput> = {}): HighlightLinkInput {
  return { id, ...extra }
}

function relay(id: string): HighlightRelayInput {
  return { id }
}

const ROUTE_A: RouteLike = { id: 'ROUTE_A', links: ['L1', 'L2'], relays: ['R1'] }
const ROUTE_B: RouteLike = { id: 'ROUTE_B', links: ['L3'], relays: ['R2'] }

const LINKS: HighlightLinkInput[] = [
  link('L1'),
  link('L2'),
  link('L3'),
  link('L4'),
]

const RELAYS: HighlightRelayInput[] = [relay('R1'), relay('R2'), relay('R3')]

// ─── 纯函数层：computeRouteHighlight ────────────────────────────────────────

describe('computeRouteHighlight', () => {
  it('无选中（null/空串）→ 三个集合全空（no-op）', () => {
    for (const selectedRouteId of [null, undefined, '']) {
      const r = computeRouteHighlight({ routes: [ROUTE_A], links: LINKS, relays: RELAYS, selectedRouteId })
      expect(r.accentedLinkIds.size).toBe(0)
      expect(r.accentedRelayContacts.size).toBe(0)
      expect(r.dimmedNodeIds.size).toBe(0)
    }
  })

  it('选中不存在的路由 → no-op', () => {
    const r = computeRouteHighlight({ routes: [ROUTE_A], links: LINKS, relays: RELAYS, selectedRouteId: 'ROUTE_X' })
    expect(r.accentedLinkIds.size).toBe(0)
    expect(r.accentedRelayContacts.size).toBe(0)
    expect(r.dimmedNodeIds.size).toBe(0)
  })

  it('单路由：仅其链路+继电器被强调，其余链路/继电器被压暗', () => {
    const r = computeRouteHighlight({ routes: [ROUTE_A], links: LINKS, relays: RELAYS, selectedRouteId: 'ROUTE_A' })
    expect([...r.accentedLinkIds].sort()).toEqual(['L1', 'L2'])
    expect([...r.accentedRelayContacts]).toEqual(['R1'])
    // 压暗 = 不在路径上的链路 L3/L4 + 继电器 R2/R3
    expect([...r.dimmedNodeIds].sort()).toEqual(['L3', 'L4', 'R2', 'R3'])
    // 路径成员绝不进入压暗集合
    expect(r.dimmedNodeIds.has('L1')).toBe(false)
    expect(r.dimmedNodeIds.has('R1')).toBe(false)
  })

  it('多路由隔离：选 A 时 B 的成员不被强调、照常压暗', () => {
    const r = computeRouteHighlight({
      routes: [ROUTE_A, ROUTE_B],
      links: LINKS,
      relays: RELAYS,
      selectedRouteId: 'ROUTE_A',
    })
    expect(r.accentedLinkIds.has('L3')).toBe(false)
    expect(r.accentedRelayContacts.has('R2')).toBe(false)
    expect(r.dimmedNodeIds.has('L3')).toBe(true)
    expect(r.dimmedNodeIds.has('R2')).toBe(true)
  })

  it('故障链路胜出：路径内故障链路既不强调也不压暗（保留 fault 样式）', () => {
    const faulted = [link('L1', { status: 'fault' }), link('L4', { fault_info: { type: 'open_circuit' } })]
    const r = computeRouteHighlight({
      routes: [ROUTE_A],
      links: [...LINKS.filter((l) => l.id !== 'L1' && l.id !== 'L4'), ...faulted],
      relays: RELAYS,
      selectedRouteId: 'ROUTE_A',
    })
    expect(r.accentedLinkIds.has('L1')).toBe(false)
    expect(r.dimmedNodeIds.has('L1')).toBe(false)
    // 路径外故障链路同样不压暗
    expect(r.accentedLinkIds.has('L4')).toBe(false)
    expect(r.dimmedNodeIds.has('L4')).toBe(false)
    // 其余语义不受影响
    expect(r.accentedLinkIds.has('L2')).toBe(true)
    expect(r.dimmedNodeIds.has('L3')).toBe(true)
  })

  it('路由引用了画布上不存在的链路/继电器 → 忽略悬空 id', () => {
    const dangling: RouteLike = { id: 'ROUTE_D', links: ['L1', 'GHOST_L'], relays: ['R1', 'GHOST_R'] }
    const r = computeRouteHighlight({ routes: [dangling], links: LINKS, relays: RELAYS, selectedRouteId: 'ROUTE_D' })
    expect([...r.accentedLinkIds]).toEqual(['L1'])
    expect([...r.accentedRelayContacts]).toEqual(['R1'])
    expect(r.dimmedNodeIds.has('GHOST_L')).toBe(false)
    expect(r.dimmedNodeIds.has('GHOST_R')).toBe(false)
  })
})

// ─── 纯函数层：pickActiveRouteId ────────────────────────────────────────────

describe('pickActiveRouteId', () => {
  it('无激活路由 → null', () => {
    expect(pickActiveRouteId([ROUTE_A, ROUTE_B])).toBeNull()
  })

  it('恰好一个激活 → 返回其 id', () => {
    expect(pickActiveRouteId([ROUTE_A, { ...ROUTE_B, active: true }])).toBe('ROUTE_B')
  })

  it('多个同时激活 → null（避免歧义误强调）', () => {
    expect(pickActiveRouteId([{ ...ROUTE_A, active: true }, { ...ROUTE_B, active: true }])).toBeNull()
  })
})

// ─── X6 适配层（假 Graph/Cell，不加载真实 X6）────────────────────────────────

interface FakeCell {
  id: string
  isEdge(): boolean
  isNode(): boolean
  getData(): unknown
  attr(path: string, value?: unknown): unknown
  attrs: Record<string, unknown>
}

function fakeCell(id: string, opts: { edge?: boolean; data?: unknown } = {}): FakeCell {
  const attrs: Record<string, unknown> = {}
  return {
    id,
    isEdge: () => opts.edge ?? false,
    isNode: () => !(opts.edge ?? false),
    getData: () => opts.data,
    attr(path: string, value?: unknown) {
      if (arguments.length >= 2) {
        attrs[path] = value
        return undefined
      }
      return attrs[path]
    },
    attrs,
  }
}

function fakeGraph(cells: FakeCell[]) {
  const byId = new Map(cells.map((c) => [c.id, c]))
  return {
    getCellById: (id: string) => byId.get(id) ?? null,
  } as unknown as Parameters<typeof applyRouteHighlight>[0]
}

const SIGNAL_DATA = { signalType: 'signal' }

describe('routeHighlightAdapter', () => {
  it('applyRouteHighlight：路径边强调描边（accent 色、加宽、实线、不透明）', () => {
    const l1 = fakeCell('L1', { edge: true, data: SIGNAL_DATA })
    const g = fakeGraph([l1])
    applyRouteHighlight(g, {
      accentedLinkIds: new Set(['L1']),
      accentedRelayContacts: new Set(['R1']),
      dimmedNodeIds: new Set(),
    })
    expect(l1.attrs['line/stroke']).toBe(ACCENT_COLOR)
    expect(l1.attrs['line/strokeWidth']).toBe(2 /* signal base */ + 2)
    expect(l1.attrs['line/strokeDasharray']).toBe('')
    expect(l1.attrs['line/opacity']).toBe(1)
  })

  it('applyRouteHighlight：非路径边保持基础信号色但压暗', () => {
    const l3 = fakeCell('L3', { edge: true, data: { signalType: 'rf' } })
    const g = fakeGraph([l3])
    applyRouteHighlight(g, {
      accentedLinkIds: new Set(),
      accentedRelayContacts: new Set(),
      dimmedNodeIds: new Set(['L3']),
    })
    expect(l3.attrs['line/stroke']).toBe('#9B59B6') // rf 基础色不变
    expect(l3.attrs['line/strokeDasharray']).toBe('4 2')
    expect(l3.attrs['line/opacity']).toBe(DIM_OPACITY)
  })

  it('applyRouteHighlight：继电器触点不在画布上 → 静默跳过不崩溃', () => {
    const g = fakeGraph([])
    expect(() =>
      applyRouteHighlight(g, {
        accentedLinkIds: new Set(),
        accentedRelayContacts: new Set(['R1']),
        dimmedNodeIds: new Set(['R2', 'R3']),
      }),
    ).not.toThrow()
  })

  it('applyRouteHighlight：故障边不在结果集合中 → attrs 完全不被触碰', () => {
    const lf = fakeCell('LF', { edge: true, data: SIGNAL_DATA })
    lf.attr('line/stroke', '#F56C6C')
    lf.attr('line/strokeWidth', 4)
    const g = fakeGraph([lf])
    applyRouteHighlight(g, {
      accentedLinkIds: new Set(),
      accentedRelayContacts: new Set(),
      dimmedNodeIds: new Set(),
    })
    expect(lf.attrs['line/stroke']).toBe('#F56C6C')
    expect(lf.attrs['line/strokeWidth']).toBe(4)
    expect(lf.attrs['line/opacity']).toBeUndefined()
  })

  it('clearRouteHighlight：恢复基础样式与完全不透明', () => {
    const l1 = fakeCell('L1', { edge: true, data: SIGNAL_DATA })
    // 先模拟被高亮过的状态
    l1.attr('line/stroke', ACCENT_COLOR)
    l1.attr('line/opacity', DIM_OPACITY)
    const g = fakeGraph([l1])
    clearRouteHighlight(g, ['L1', 'R1'])
    expect(l1.attrs['line/stroke']).toBe('#409EFF') // signal 基础色
    expect(l1.attrs['line/strokeWidth']).toBe(2)
    expect(l1.attrs['line/strokeDasharray']).toBe('')
    expect(l1.attrs['line/opacity']).toBe(1)
  })

  it('syncRouteHighlight：无激活路由 → 等价于清除（no-op 恢复）', () => {
    const l1 = fakeCell('L1', { edge: true, data: SIGNAL_DATA })
    l1.attr('line/stroke', ACCENT_COLOR)
    const g = fakeGraph([l1])
    syncRouteHighlight(g, { routes: [ROUTE_A], links: LINKS, relays: RELAYS })
    expect(l1.attrs['line/stroke']).toBe('#409EFF')
    expect(l1.attrs['line/opacity']).toBe(1)
  })

  it('syncRouteHighlight：唯一激活路由 → 计算并应用高亮', () => {
    const l1 = fakeCell('L1', { edge: true, data: SIGNAL_DATA })
    const l3 = fakeCell('L3', { edge: true, data: SIGNAL_DATA })
    const g = fakeGraph([l1, l3])
    syncRouteHighlight(g, {
      routes: [{ ...ROUTE_B, active: true }],
      links: LINKS,
      relays: RELAYS,
    })
    expect(l1.attrs['line/opacity']).toBe(DIM_OPACITY) // L1 不在 ROUTE_B
    expect(l3.attrs['line/stroke']).toBe(ACCENT_COLOR) // L3 ∈ ROUTE_B
  })
})
