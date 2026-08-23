/**
 * Tests for LinkFaultContextMenu component (T30, 设计文档 §8.3).
 *
 * Covers: menu renders the four §8.3 fault options, hidden state, disabled
 * state (no active run), select/close emits, outside-click and Escape close.
 */
import { describe, it, expect, afterEach } from 'vitest'
import { mount, type VueWrapper } from '@vue/test-utils'
import LinkFaultContextMenu from '../LinkFaultContextMenu.vue'
import { FAULT_TYPES } from '@/composables/useFaultInjection'

function mountMenu(props: Record<string, unknown> = {}): VueWrapper {
  return mount(LinkFaultContextMenu, {
    props: {
      visible: true,
      x: 100,
      y: 120,
      linkId: 'LINK-1',
      ...props,
    },
    attachTo: document.body,
  })
}

let wrapper: VueWrapper | null = null

afterEach(() => {
  wrapper?.unmount()
  wrapper = null
})

describe('LinkFaultContextMenu', () => {
  it('renders exactly the four §8.3 fault options when visible', () => {
    wrapper = mountMenu()
    const buttons = wrapper.findAll('button.menu-item')
    expect(buttons).toHaveLength(4)
    expect(buttons.map((b) => b.attributes('data-fault-type'))).toEqual(
      FAULT_TYPES.map((t) => t.value),
    )
    expect(buttons.map((b) => b.attributes('data-fault-type'))).toEqual([
      'open_circuit',
      'short_circuit',
      'contact_resistance',
      'noise',
    ])
  })

  it('renders nothing when visible=false', () => {
    wrapper = mountMenu({ visible: false })
    expect(wrapper.find('#link-fault-context-menu').exists()).toBe(false)
  })

  it('shows the target link id in the menu header', () => {
    wrapper = mountMenu({ linkId: 'LINK-PWR-9' })
    expect(wrapper.find('.menu-link-id').text()).toContain('LINK-PWR-9')
  })

  it('disables every item when disabled=true and shows the no-active-run hint', () => {
    wrapper = mountMenu({ disabled: true })
    const buttons = wrapper.findAll('button.menu-item')
    expect(buttons).toHaveLength(4)
    for (const b of buttons) {
      expect((b.element as HTMLButtonElement).disabled).toBe(true)
    }
    expect(wrapper.find('.menu-hint').exists()).toBe(true)
  })

  it('clicking an enabled item emits select with that type then close', async () => {
    wrapper = mountMenu()
    await wrapper.find('button[data-fault-type="contact_resistance"]').trigger('click')
    const selectEvents = wrapper.emitted('select')
    expect(selectEvents).toBeTruthy()
    expect(selectEvents![0]).toEqual(['contact_resistance'])
    expect(wrapper.emitted('close')).toBeTruthy()
  })

  it('clicking a disabled item emits neither select nor close', async () => {
    wrapper = mountMenu({ disabled: true })
    await wrapper.find('button[data-fault-type="open_circuit"]').trigger('click')
    expect(wrapper.emitted('select')).toBeUndefined()
    expect(wrapper.emitted('close')).toBeUndefined()
  })

  it('mousedown outside the menu emits close', async () => {
    wrapper = mountMenu()
    document.body.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }))
    await wrapper.vm.$nextTick()
    expect(wrapper.emitted('close')).toBeTruthy()
  })

  it('mousedown inside the menu does NOT emit close', async () => {
    wrapper = mountMenu()
    const btn = wrapper.find('button.menu-item').element
    btn.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }))
    await wrapper.vm.$nextTick()
    expect(wrapper.emitted('close')).toBeUndefined()
  })

  it('Escape keydown emits close', async () => {
    wrapper = mountMenu()
    document.body.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
    await wrapper.vm.$nextTick()
    expect(wrapper.emitted('close')).toBeTruthy()
  })
})
