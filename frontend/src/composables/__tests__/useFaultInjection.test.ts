/**
 * Tests for useFaultInjection composable (T30, 设计文档 §8.3).
 *
 * Covers: strict §8.3 fault-type set (no extras), success toast, error toast
 * on backend failure (4xx), and the in-flight `injecting` flag.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'

const { injectLinkFaultMock } = vi.hoisted(() => ({ injectLinkFaultMock: vi.fn() }))

vi.mock('@/api/fixtures', () => ({
  injectLinkFault: injectLinkFaultMock,
}))

const { successMock, errorMock, warningMock } = vi.hoisted(() => ({
  successMock: vi.fn(),
  errorMock: vi.fn(),
  warningMock: vi.fn(),
}))

vi.mock('element-plus', () => ({
  ElMessage: { success: successMock, error: errorMock, warning: warningMock },
}))

import { useFaultInjection, FAULT_TYPES } from '@/composables/useFaultInjection'

describe('FAULT_TYPES (§8.3 set)', () => {
  it('offers exactly the four doc §8.3 fault types and nothing more', () => {
    expect(FAULT_TYPES.map((t) => t.value)).toEqual([
      'open_circuit',
      'short_circuit',
      'contact_resistance',
      'noise',
    ])
  })
})

describe('useFaultInjection', () => {
  beforeEach(() => {
    injectLinkFaultMock.mockReset()
    successMock.mockClear()
    errorMock.mockClear()
    warningMock.mockClear()
  })

  it('forwards runId/linkId/type to the api client and reports success', async () => {
    injectLinkFaultMock.mockResolvedValue(undefined)
    const { injectFault } = useFaultInjection()
    const ok = await injectFault('run-1', 'LINK-7', 'open_circuit')
    expect(ok).toBe(true)
    expect(injectLinkFaultMock).toHaveBeenCalledWith('run-1', 'LINK-7', 'open_circuit')
    expect(successMock).toHaveBeenCalledTimes(1)
    expect(errorMock).not.toHaveBeenCalled()
  })

  it('shows an error toast on backend failure (4xx) and returns false', async () => {
    injectLinkFaultMock.mockRejectedValue(new Error('Request failed with status code 404'))
    const { injectFault } = useFaultInjection()
    const ok = await injectFault('run-1', 'LINK-7', 'short_circuit')
    expect(ok).toBe(false)
    expect(errorMock).toHaveBeenCalledTimes(1)
    expect(String(errorMock.mock.calls[0]?.[0])).toContain('404')
    expect(successMock).not.toHaveBeenCalled()
  })

  it('toggles injecting=true only while the request is in flight', async () => {
    let resolveApi!: () => void
    injectLinkFaultMock.mockReturnValue(
      new Promise<void>((resolve) => {
        resolveApi = resolve
      }),
    )
    const { injecting, injectFault } = useFaultInjection()
    expect(injecting.value).toBe(false)
    const pending = injectFault('run-2', 'L2', 'noise')
    expect(injecting.value).toBe(true)
    resolveApi()
    await pending
    expect(injecting.value).toBe(false)
  })
})
