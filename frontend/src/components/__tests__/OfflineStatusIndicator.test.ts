/**
 * Tests for OfflineStatusIndicator + utils/offlineStatus (T43, v41-gap-analysis #43).
 *
 * Covers: byte formatter boundaries (0 / negative / huge), capacity level
 * thresholds (ok<70 / warn<90 / full>=90), age formatting, SSE frame reducer
 * online<->offline transitions + malformed-frame resilience, badge render per
 * state, pending-upload chip visibility, paused-download warning, reconcile
 * button disabled while in-flight.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount, flushPromises, type VueWrapper } from '@vue/test-utils'
import ElementPlus from 'element-plus'

import {
  formatBytes,
  formatAgeHours,
  capacityLevel,
  reduceOfflineStatusFrame,
  setStreamConnected,
  initialOfflineStatusState,
  type OfflineStatusSnapshot,
  type OfflineStatusState,
} from '@/utils/offlineStatus'
import OfflineStatusIndicator from '../OfflineStatusIndicator.vue'
import { fetchOfflineStatus, triggerReconcile } from '@/api/offline'

// ─── Module mocks ────────────────────────────────────────────────────────────

const fetchOfflineStatusMock = vi.fn()
const triggerReconcileMock = vi.fn()

vi.mock('@/api/offline', () => ({
  fetchOfflineStatus: (...args: unknown[]) => fetchOfflineStatusMock(...args),
  triggerReconcile: (...args: unknown[]) => triggerReconcileMock(...args),
}))

// ─── Fixtures ────────────────────────────────────────────────────────────────

export function makeSnapshot(
  overrides: Partial<OfflineStatusSnapshot> = {},
): OfflineStatusSnapshot {
  return {
    online: true,
    pending_upload_count: 0,
    cache_health: {
      size_bytes: 1024 * 1024,
      oldest_record_age_h: 2.5,
      capacity_pct: 12.5,
      downloads_paused: false,
    },
    ...overrides,
    cache_health: {
      size_bytes: 1024 * 1024,
      oldest_record_age_h: 2.5,
      capacity_pct: 12.5,
      downloads_paused: false,
      ...(overrides.cache_health ?? {}),
    },
  }
}

// ─── Utils: formatters ──────────────────────────────────────────────────────

describe('utils/offlineStatus · formatBytes', () => {
  it('formats zero bytes without decimals', () => {
    expect(formatBytes(0)).toBe('0 B')
  })

  it('clamps negative sizes to zero (defensive, never scary)', () => {
    expect(formatBytes(-1)).toBe('0 B')
    expect(formatBytes(-(1024 ** 3))).toBe('0 B')
  })

  it('keeps whole bytes below 1 KB unitless', () => {
    expect(formatBytes(1)).toBe('1 B')
    expect(formatBytes(1023)).toBe('1023 B')
  })

  it('uses binary units with one decimal above 1 KB', () => {
    expect(formatBytes(1024)).toBe('1.0 KB')
    expect(formatBytes(1536)).toBe('1.5 KB')
    expect(formatBytes(5 * 1024 ** 2)).toBe('5.0 MB')
    expect(formatBytes(3.25 * 1024 ** 3)).toBe('3.3 GB')
  })

  it('caps at TB for huge caches', () => {
    expect(formatBytes(5 * 1024 ** 4)).toBe('5.0 TB')
    expect(formatBytes(1024 ** 5)).toBe('1024.0 TB')
  })
})

describe('utils/offlineStatus · formatAgeHours', () => {
  it('renders an em dash for empty cache (null age)', () => {
    expect(formatAgeHours(null)).toBe('—')
  })

  it('renders minutes below one hour', () => {
    expect(formatAgeHours(0)).toBe('0 分钟')
    expect(formatAgeHours(0.5)).toBe('30 分钟')
  })

  it('renders hours between 1h and 48h', () => {
    expect(formatAgeHours(1)).toBe('1 小时')
    expect(formatAgeHours(2.5)).toBe('2.5 小时')
    expect(formatAgeHours(47.9)).toBe('47.9 小时')
  })

  it('renders days beyond 48h', () => {
    expect(formatAgeHours(48)).toBe('2 天')
    expect(formatAgeHours(73)).toBe('3 天')
  })
})

describe('utils/offlineStatus · capacityLevel thresholds', () => {
  it('ok strictly below 70%', () => {
    expect(capacityLevel(0)).toBe('ok')
    expect(capacityLevel(69.9)).toBe('ok')
  })

  it('warn at exactly 70% and up to below 90%', () => {
    expect(capacityLevel(70)).toBe('warn')
    expect(capacityLevel(89.9)).toBe('warn')
  })

  it('full at exactly 90% and above', () => {
    expect(capacityLevel(90)).toBe('full')
    expect(capacityLevel(120)).toBe('full')
  })

  it('treats negative and non-finite percentages as ok (degraded data)', () => {
    expect(capacityLevel(-5)).toBe('ok')
    expect(capacityLevel(Number.NaN)).toBe('ok')
  })
})

// ─── Utils: SSE frame reducer ────────────────────────────────────────────────

describe('utils/offlineStatus · reduceOfflineStatusFrame', () => {
  const T0 = 1_700_000_000_000

  it('applies frame 0 onto the initial state and marks the stream connected', () => {
    const next = reduceOfflineStatusFrame(
      initialOfflineStatusState(),
      makeSnapshot({ online: false, pending_upload_count: 4 }),
      T0,
    )
    expect(next.status?.online).toBe(false)
    expect(next.status?.pending_upload_count).toBe(4)
    expect(next.connected).toBe(true)
    expect(next.lastUpdateAt).toBe(T0)
  })

  it('transitions online -> offline on a later frame', () => {
    let state = reduceOfflineStatusFrame(initialOfflineStatusState(), makeSnapshot(), T0)
    state = reduceOfflineStatusFrame(state, makeSnapshot({ online: false }), T0 + 1000)
    expect(state.status?.online).toBe(false)
    expect(state.lastUpdateAt).toBe(T0 + 1000)
  })

  it('transitions offline -> online and keeps cache health flowing', () => {
    let state = reduceOfflineStatusFrame(
      initialOfflineStatusState(),
      makeSnapshot({ online: false }),
      T0,
    )
    state = reduceOfflineStatusFrame(
      state,
      makeSnapshot({
        online: true,
        cache_health: {
          size_bytes: 2048,
          oldest_record_age_h: null,
          capacity_pct: 0.2,
          downloads_paused: false,
        },
      }),
      T0 + 2000,
    )
    expect(state.status?.online).toBe(true)
    expect(state.status?.cache_health.size_bytes).toBe(2048)
    expect(state.status?.cache_health.oldest_record_age_h).toBeNull()
  })

  it('propagates pending count growth and downloads_paused flag', () => {
    let state = reduceOfflineStatusFrame(initialOfflineStatusState(), makeSnapshot(), T0)
    state = reduceOfflineStatusFrame(
      state,
      makeSnapshot({
        pending_upload_count: 7,
        cache_health: {
          size_bytes: 900 * 1024 ** 2,
          oldest_record_age_h: 96,
          capacity_pct: 92,
          downloads_paused: true,
        },
      }),
      T0 + 3000,
    )
    expect(state.status?.pending_upload_count).toBe(7)
    expect(state.status?.cache_health.downloads_paused).toBe(true)
    expect(state.status?.cache_health.capacity_pct).toBe(92)
  })

  it('ignores malformed frames instead of corrupting badge state', () => {
    const base = reduceOfflineStatusFrame(initialOfflineStatusState(), makeSnapshot(), T0)
    for (const bad of [null, undefined, {}, { online: 'yes' }, 'offline']) {
      const next = reduceOfflineStatusFrame(base, bad as unknown, T0 + 5000)
      expect(next).toEqual(base)
    }
  })

  it('marks the stream disconnected without touching badge status (SSE drop)', () => {
    const base = reduceOfflineStatusFrame(initialOfflineStatusState(), makeSnapshot(), T0)
    const degraded = setStreamConnected(base, false, T0 + 42)
    expect(degraded.connected).toBe(false)
    expect(degraded.status).toEqual(base.status)
    expect(degraded.lastUpdateAt).toBe(T0 + 42)
  })
})

// ─── Component ──────────────────────────────────────────────────────────────

function mountIndicator(): VueWrapper {
  return mount(OfflineStatusIndicator, {
    global: { plugins: [ElementPlus] },
    attachTo: document.body,
  })
}

let wrapper: VueWrapper | null = null

beforeEach(() => {
  fetchOfflineStatusMock.mockReset()
  triggerReconcileMock.mockReset()
  fetchOfflineStatusMock.mockResolvedValue(makeSnapshot())
  triggerReconcileMock.mockResolvedValue({
    ok: true,
    uploaded: 3,
    acked: 3,
    confirmed_entries: 3,
    conflicts_resolved: 0,
    quarantined: 0,
    locks_released: 0,
    duration: 0.4,
    quarantine: [],
  })
})

afterEach(() => {
  wrapper?.unmount()
  wrapper = null
  document.body.innerHTML = ''
})

describe('OfflineStatusIndicator', () => {
  it('renders 在线 badge from the initial snapshot', async () => {
    wrapper = mountIndicator()
    await flushPromises()
    const badge = wrapper.find('[data-testid="offline-badge"]')
    expect(badge.exists()).toBe(true)
    expect(badge.text()).toContain('在线')
    expect(badge.classes()).toContain('is-online')
  })

  it('flips to 离线 when an offline SSE frame arrives', async () => {
    wrapper = mountIndicator()
    await flushPromises()
    const vm = wrapper.vm as unknown as { handleFrame: (f: unknown) => void }
    vm.handleFrame(makeSnapshot({ online: false }))
    await wrapper.vm.$nextTick()
    const badge = wrapper.find('[data-testid="offline-badge"]')
    expect(badge.text()).toContain('离线')
    expect(badge.classes()).toContain('is-offline')
  })

  it('hides the pending chip at zero and shows the count once queued', async () => {
    wrapper = mountIndicator()
    await flushPromises()
    expect(wrapper.find('[data-testid="pending-chip"]').exists()).toBe(false)

    const vm = wrapper.vm as unknown as { handleFrame: (f: unknown) => void }
    vm.handleFrame(makeSnapshot({ pending_upload_count: 3 }))
    await wrapper.vm.$nextTick()
    const chip = wrapper.find('[data-testid="pending-chip"]')
    expect(chip.exists()).toBe(true)
    expect(chip.text()).toContain('3')
  })

  it('shows the paused-downloads warning only when flagged', async () => {
    wrapper = mountIndicator()
    await flushPromises()
    expect(wrapper.find('[data-testid="paused-warning"]').exists()).toBe(false)

    const vm = wrapper.vm as unknown as { handleFrame: (f: unknown) => void }
    vm.handleFrame(
      makeSnapshot({
        cache_health: {
          size_bytes: 900 * 1024 ** 2,
          oldest_record_age_h: 96,
          capacity_pct: 92,
          downloads_paused: true,
        },
      }),
    )
    await wrapper.vm.$nextTick()
    expect(wrapper.find('[data-testid="paused-warning"]').exists()).toBe(true)
  })

  it('disables 手动同步 while a reconcile is in flight, re-enables after', async () => {
    let release!: (v: unknown) => void
    triggerReconcileMock.mockReturnValue(
      new Promise((resolve) => {
        release = resolve
      }),
    )
    wrapper = mountIndicator()
    await flushPromises()

    const btnBefore = wrapper.find('[data-testid="reconcile-btn"]')
    expect(btnBefore.attributes('disabled')).toBeUndefined()

    const vm = wrapper.vm as unknown as { reconcile: () => Promise<void> }
    const pending = vm.reconcile()
    await wrapper.vm.$nextTick()
    const btnPending = wrapper.find('[data-testid="reconcile-btn"]').element as HTMLButtonElement
    expect(btnPending.disabled).toBe(true)

    release({ ok: true })
    await pending
    await wrapper.vm.$nextTick()
    const btnAfter = wrapper.find('[data-testid="reconcile-btn"]').element as HTMLButtonElement
    expect(btnAfter.disabled).toBe(false)
    expect(triggerReconcileMock).toHaveBeenCalledTimes(1)
  })

  it('recovers the button when reconcile rejects (no crash, no stuck state)', async () => {
    triggerReconcileMock.mockRejectedValue(new Error('503 reconciler not configured'))
    wrapper = mountIndicator()
    await flushPromises()

    const vm = wrapper.vm as unknown as { reconcile: () => Promise<void> }
    await vm.reconcile()
    await wrapper.vm.$nextTick()
    const btn = wrapper.find('[data-testid="reconcile-btn"]').element as HTMLButtonElement
    expect(btn.disabled).toBe(false)
  })

  it('survives a failed initial snapshot fetch with an empty (not scary) state', async () => {
    fetchOfflineStatusMock.mockRejectedValue(new Error('503 not configured'))
    wrapper = mountIndicator()
    await flushPromises()
    // Badge still renders; status stays empty until SSE frame 0 arrives.
    expect(wrapper.find('[data-testid="offline-badge"]').exists()).toBe(true)
    const vm = wrapper.vm as unknown as { state: OfflineStatusState }
    expect(vm.state.status).toBeNull()
  })
})
