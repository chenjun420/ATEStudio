/**
 * Tests for the X6 node breakpoint marker helpers (task 23).
 *
 * The SequenceEditor renders script-step nodes as plain X6 Shape.Rect cells
 * (visuals driven by SVG attrs); the breakpoint badge (red dot) and hit halo
 * (amber ring) are two extra markup sub-elements registered on the shape and
 * toggled through attrs. These tests exercise the helpers with a faithful
 * fake node (records setAttrs calls), verifying show/hide semantics and the
 * corner-pinned geometry.
 */
import { describe, it, expect, vi } from 'vitest'
import {
  setBreakpointBadge,
  setBreakpointHitHalo,
  clearAllBreakpointMarkers,
  BP_BADGE_SELECTOR,
  BP_HALO_SELECTOR,
  type MarkerNode,
} from '../breakpointMarkers'

function makeFakeNode(width = 180, height = 80) {
  const attrs: Record<string, Record<string, unknown>> = {}
  // Fake implements only the surface MarkerNode requires; cast because X6's
  // Node.setAttrs is typed `this`-returning while the fake returns void.
  const node = {
    size: () => ({ width, height }),
    setAttrs: vi.fn((patch: Record<string, unknown>) => {
      for (const [key, value] of Object.entries(patch)) {
        attrs[key] = { ...attrs[key], ...(value as Record<string, unknown>) }
      }
    }),
  } as unknown as MarkerNode
  return { node, attrs }
}

describe('breakpointMarkers', () => {
  it('badge hidden by default sets display:none on the badge selector', () => {
    const { node, attrs } = makeFakeNode()
    setBreakpointBadge(node, false)
    expect(attrs[BP_BADGE_SELECTOR].display).toBe('none')
  })

  it('badge shown pins a red dot to the top-right corner', () => {
    const { node, attrs } = makeFakeNode(180, 80)
    setBreakpointBadge(node, true)
    const badge = attrs[BP_BADGE_SELECTOR]
    expect(badge.display).toBe('block')
    expect(badge.refCx).toBe(174) // width - 6
    expect(badge.refCy).toBe(6)
    expect(badge.r).toBe(6)
    expect(badge.fill).toBe('#ef4444')
    // Must not intercept graph interactions.
    expect(badge.style).toEqual({ pointerEvents: 'none' })
  })

  it('toggling badge off after on hides it', () => {
    const { node, attrs } = makeFakeNode()
    setBreakpointBadge(node, true)
    expect(attrs[BP_BADGE_SELECTOR].display).toBe('block')
    setBreakpointBadge(node, false)
    expect(attrs[BP_BADGE_SELECTOR].display).toBe('none')
  })

  it('halo shown draws an amber ring sized to the node', () => {
    const { node, attrs } = makeFakeNode(180, 80)
    setBreakpointHitHalo(node, true)
    const halo = attrs[BP_HALO_SELECTOR]
    expect(halo.display).toBe('block')
    expect(halo.x).toBe(-5)
    expect(halo.y).toBe(-5)
    expect(halo.width).toBe(190) // width + 2*5
    expect(halo.height).toBe(90)
    expect(halo.fill).toBe('none')
    expect(halo.stroke).toBe('#f59e0b')
    expect(halo.strokeWidth).toBe(3)
  })

  it('halo hidden sets display:none', () => {
    const { node, attrs } = makeFakeNode()
    setBreakpointHitHalo(node, false)
    expect(attrs[BP_HALO_SELECTOR].display).toBe('none')
  })

  it('badge and halo use distinct selectors so they do not clobber each other', () => {
    const { node, attrs } = makeFakeNode()
    setBreakpointBadge(node, true)
    setBreakpointHitHalo(node, true)
    expect(attrs[BP_BADGE_SELECTOR].fill).toBe('#ef4444')
    expect(attrs[BP_HALO_SELECTOR].stroke).toBe('#f59e0b')
  })

  it('clearAllBreakpointMarkers hides both markers on every node', () => {
    const a = makeFakeNode()
    const b = makeFakeNode()
    setBreakpointBadge(a.node, true)
    setBreakpointHitHalo(b.node, true)
    clearAllBreakpointMarkers({ getNodes: () => [a.node, b.node] as unknown as MarkerNode[] })
    expect(a.attrs[BP_BADGE_SELECTOR].display).toBe('none')
    expect(a.attrs[BP_HALO_SELECTOR].display).toBe('none')
    expect(b.attrs[BP_BADGE_SELECTOR].display).toBe('none')
    expect(b.attrs[BP_HALO_SELECTOR].display).toBe('none')
  })
})
