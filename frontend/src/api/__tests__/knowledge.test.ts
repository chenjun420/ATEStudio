/**
 * Tests for the knowledge read API module (frontend/src/api/knowledge.ts,
 * tasks 25 & 26).
 *
 * Verifies the shared http client (@/api/interceptor) is used for every call,
 * with the correct /knowledge/* endpoints and query params:
 * - fetchKnowledgeGraph  GET /knowledge/graph        (?limit, ?label)
 * - fetchTraceability    GET /knowledge/traceability (?product_code)
 * - fetchRequirements    GET /knowledge/requirements  ({items,total})
 * - fetchCases           GET /knowledge/cases         (?requirement_id, ?product_code)
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'

const { getMock } = vi.hoisted(() => ({ getMock: vi.fn() }))

vi.mock('@/api/interceptor', () => ({
  default: {
    get: getMock,
    post: vi.fn(),
    put: vi.fn(),
    delete: vi.fn(),
  },
}))

import {
  fetchKnowledgeGraph,
  fetchTraceability,
  fetchRequirements,
  fetchCases,
} from '@/api/knowledge'

describe('api/knowledge transport', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('fetchKnowledgeGraph GETs /knowledge/graph and unwraps {nodes,edges}', async () => {
    getMock.mockResolvedValue({
      data: {
        nodes: [
          { id: 'n1', label: 'Component', type: 'Component', name: 'PSU', properties: {} },
        ],
        edges: [{ source: 'n1', target: 'n2', type: 'relatesTo' }],
      },
    })
    const res = await fetchKnowledgeGraph()
    expect(getMock.mock.calls[0][0]).toBe('/knowledge/graph')
    expect(res.nodes).toHaveLength(1)
    expect(res.edges[0].source).toBe('n1')
  })

  it('fetchKnowledgeGraph forwards limit and label query params', async () => {
    getMock.mockResolvedValue({ data: { nodes: [], edges: [] } })
    await fetchKnowledgeGraph({ limit: 50, label: 'Fault' })
    const params = getMock.mock.calls[0][1].params as Record<string, string | number>
    expect(params).toEqual({ limit: 50, label: 'Fault' })
  })

  it('fetchKnowledgeGraph omits empty filters from the query', async () => {
    getMock.mockResolvedValue({ data: { nodes: [], edges: [] } })
    await fetchKnowledgeGraph({})
    const params = getMock.mock.calls[0][1].params as Record<string, string | number>
    expect(params).toEqual({})
  })

  it('fetchTraceability GETs /knowledge/traceability with product_code', async () => {
    getMock.mockResolvedValue({
      data: {
        product_code: 'P1',
        requirements: [
          {
            id: 'r1',
            requirement_code: 'REQ-1',
            title: 'Voltage tolerance',
            source: 'dsl',
            cases: [
              { id: 'c1', case_code: 'TC-1', title: 'Check 5V', sequence_id: 's1', step_id: 'step-1', atml_ref: null, status: 'draft' },
            ],
          },
        ],
        unlinked_cases: [],
      },
    })
    const res = await fetchTraceability('P1')
    expect(getMock.mock.calls[0][0]).toBe('/knowledge/traceability')
    expect(getMock.mock.calls[0][1].params).toEqual({ product_code: 'P1' })
    expect(res.requirements[0].cases[0].step_id).toBe('step-1')
  })

  it('fetchRequirements GETs /knowledge/requirements and unwraps {items,total}', async () => {
    getMock.mockResolvedValue({
      data: {
        items: [
          {
            id: 'r1',
            product_code: 'P1',
            requirement_code: 'REQ-1',
            title: 'T',
            description: null,
            source: 'manual',
            atml_ref: null,
            created_at: '2026-01-01T00:00:00Z',
            updated_at: '2026-01-01T00:00:00Z',
          },
        ],
        total: 1,
      },
    })
    const res = await fetchRequirements({ product_code: 'P1', limit: 10 })
    expect(getMock.mock.calls[0][0]).toBe('/knowledge/requirements')
    expect(getMock.mock.calls[0][1].params).toMatchObject({ product_code: 'P1', limit: 10 })
    expect(res.total).toBe(1)
    expect(res.items[0].requirement_code).toBe('REQ-1')
  })

  it('fetchCases GETs /knowledge/cases with requirement_id and product_code', async () => {
    getMock.mockResolvedValue({ data: { items: [], total: 0 } })
    await fetchCases({ requirement_id: 'r1', product_code: 'P1' })
    expect(getMock.mock.calls[0][0]).toBe('/knowledge/cases')
    expect(getMock.mock.calls[0][1].params).toEqual({ requirement_id: 'r1', product_code: 'P1' })
  })
})
