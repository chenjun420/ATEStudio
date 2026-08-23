/**
 * Component tests for InstrumentGantt.vue
 * (T36, v41-gap-analysis #36, 设计文档 §7.6 / §8.4).
 *
 * Covered:
 * - one row label per instrument; empty-state message when no calls
 * - span x/width math: bar geometry proportional to (start,end)/totalDuration
 * - hover tooltip shows method + duration; click emits `call-select` payload
 */
import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'

import InstrumentGantt from '../InstrumentGantt.vue'
import { GANTT_LAYOUT, type GanttEventInput } from '@/utils/ganttTimeline'

function call(
  idx: number,
  resource: string,
  method: string,
  t: number,
  elapsed_ms: number,
): GanttEventInput {
  return {
    event_type: 'instrument_call',
    step_id: `s${idx}`,
    timestamp: new Date(1700000000000).toISOString(),
    data: { resource, method, t, elapsed_ms },
  }
}

describe('InstrumentGantt.vue', () => {
  it('renders one row label per instrument and no empty state when data present', () => {
    const w = mount(InstrumentGantt, {
      props: {
        events: [
          call(0, 'DMM1', 'read', 0, 100),
          call(1, 'PSU1', 'set_voltage', 0.2, 300),
        ],
      },
    })
    const labels = w.findAll('.gantt-row-label').map((n) => n.text())
    expect(labels).toEqual(['DMM1', 'PSU1'])
    expect(w.find('.gantt-empty').exists()).toBe(false)
  })

  it('shows the empty-state message when there are no instrument calls', () => {
    const w = mount(InstrumentGantt, { props: { events: [] } })
    expect(w.find('.gantt-empty').exists()).toBe(true)
    expect(w.findAll('rect.gantt-bar')).toHaveLength(0)
  })

  it('computes bar x/width proportionally to start/end over totalDuration', () => {
    // DMM1 spans [0,1]s total → a [0,0.5] bar is half the timeline width.
    const w = mount(InstrumentGantt, {
      props: {
        events: [
          call(0, 'DMM1', 'a', 0, 500),
          call(1, 'DMM1', 'b', 0.5, 500),
        ],
      },
    })
    const bars = w.findAll('rect.gantt-bar')
    expect(bars).toHaveLength(2)
    // x 轴含左侧标签列偏移；总时长 1s → 每条占绘图区一半宽。
    const first = bars[0]!
    expect(Number(first.attributes('x'))).toBeCloseTo(GANTT_LAYOUT.labelWidth, 6)
    expect(Number(first.attributes('width'))).toBeCloseTo(GANTT_LAYOUT.width / 2, 4)
    const second = bars[1]!
    expect(Number(second.attributes('x'))).toBeCloseTo(
      GANTT_LAYOUT.labelWidth + GANTT_LAYOUT.width / 2,
      4,
    )
    expect(Number(second.attributes('width'))).toBeCloseTo(GANTT_LAYOUT.width / 2, 4)
  })

  it('hover shows tooltip with method + duration; click emits call-select payload', async () => {
    const w = mount(InstrumentGantt, {
      props: { events: [call(0, 'DMM1', 'read', 1, 250)] },
    })
    expect(w.find('.gantt-tooltip').exists()).toBe(false)

    const bar = w.find('rect.gantt-bar')
    await bar.trigger('mouseenter')
    const tip = w.find('.gantt-tooltip')
    expect(tip.exists()).toBe(true)
    expect(tip.text()).toContain('read')
    expect(tip.text()).toContain('250')

    await bar.trigger('mouseleave')
    expect(w.find('.gantt-tooltip').exists()).toBe(false)

    await bar.trigger('click')
    expect(w.emitted('call-select')).toHaveLength(1)
    expect(w.emitted('call-select')![0]![0]).toMatchObject({
      resource: 'DMM1',
      method: 'read',
      start: 1,
      end: 1.25,
      lane: 0,
      idx: 0,
    })
  })
})
