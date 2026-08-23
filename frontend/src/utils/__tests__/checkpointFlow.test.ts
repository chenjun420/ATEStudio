/**
 * checkpointFlow.ts 纯函数测试（T42，v41-gap-analysis #42）。
 *
 * 覆盖：状态机 pending→acked、按 created 排序、upsert 合并、
 * ack 载荷构建（operator 必填/note 可选省略）、开始门控、
 * DUT 换件提示、终态判定、完成签名、离线横幅阈值。
 */

import { describe, it, expect } from 'vitest'
import {
  acknowledgeCheckpoint,
  buildAckPayload,
  buildCompletionSignature,
  canStartRun,
  isTerminalStatus,
  needsDutSwap,
  partitionCheckpoints,
  shouldShowOfflineBanner,
  sortCheckpoints,
  upsertPending,
  type CheckpointItem,
  type PendingCheckpointResponse,
} from '../checkpointFlow'

function makeItem(overrides: Partial<CheckpointItem> = {}): CheckpointItem {
  return {
    runId: 'run-1',
    stepId: 'step-1',
    checkpoint: { type: 'confirm', prompt: '确认工装就绪', timeout_sec: 30 },
    createdAt: '2026-08-24T01:00:00Z',
    status: 'pending',
    ...overrides,
  }
}

function makePending(overrides: Partial<PendingCheckpointResponse> = {}): PendingCheckpointResponse {
  return {
    run_id: 'run-1',
    pending: true,
    step_id: 'step-1',
    checkpoint: { type: 'scan', prompt: '扫码', timeout_sec: 30 },
    created_at: '2026-08-24T02:00:00Z',
    ...overrides,
  }
}

describe('sortCheckpoints', () => {
  it('test_sorts_by_created_at_ascending', () => {
    const items = [
      makeItem({ stepId: 'b', createdAt: '2026-08-24T02:00:00Z' }),
      makeItem({ stepId: 'a', createdAt: '2026-08-24T01:00:00Z' }),
      makeItem({ stepId: 'c', createdAt: '2026-08-24T03:00:00Z' }),
    ]
    expect(sortCheckpoints(items).map((i) => i.stepId)).toEqual(['a', 'b', 'c'])
  })

  it('test_invalid_dates_sort_last_and_input_not_mutated', () => {
    const items = [
      makeItem({ stepId: 'bad', createdAt: 'not-a-date' }),
      makeItem({ stepId: 'good', createdAt: '2026-08-24T01:00:00Z' }),
    ]
    const sorted = sortCheckpoints(items)
    expect(sorted.map((i) => i.stepId)).toEqual(['good', 'bad'])
    // 纯函数：入参顺序不变
    expect(items.map((i) => i.stepId)).toEqual(['bad', 'good'])
  })
})

describe('upsertPending', () => {
  it('test_appends_new_pending_checkpoint', () => {
    const next = upsertPending([], makePending({ step_id: 's1' }))
    expect(next).toHaveLength(1)
    expect(next[0].stepId).toBe('s1')
    expect(next[0].status).toBe('pending')
  })

  it('test_replaces_existing_pending_for_same_step', () => {
    const existing = [makeItem({ stepId: 's1', createdAt: 'old' })]
    const next = upsertPending(existing, makePending({ step_id: 's1', created_at: 'new' }))
    expect(next).toHaveLength(1)
    expect(next[0].createdAt).toBe('new')
  })

  it('test_noop_when_payload_not_pending_or_incomplete', () => {
    const existing = [makeItem()]
    expect(upsertPending(existing, makePending({ pending: false }))).toHaveLength(1)
    expect(upsertPending(existing, makePending({ checkpoint: null }))).toHaveLength(1)
    expect(upsertPending(existing, makePending({ created_at: null }))).toHaveLength(1)
  })
})

describe('partitionCheckpoints + acknowledgeCheckpoint state machine', () => {
  it('test_partitions_pending_and_completed', () => {
    const items = [
      makeItem({ stepId: 'p1', status: 'pending' }),
      makeItem({ stepId: 'd1', status: 'acked' }),
    ]
    const { pending, completed } = partitionCheckpoints(items)
    expect(pending.map((i) => i.stepId)).toEqual(['p1'])
    expect(completed.map((i) => i.stepId)).toEqual(['d1'])
  })

  it('test_ack_transitions_pending_to_acked_with_signature_fields', () => {
    const acked = acknowledgeCheckpoint(makeItem(), '张三', '备注', '2026-08-24T05:00:00Z')
    expect(acked.status).toBe('acked')
    expect(acked.ackedBy).toBe('张三')
    expect(acked.ackedAt).toBe('2026-08-24T05:00:00Z')
    expect(acked.note).toBe('备注')
  })

  it('test_ack_is_irreversible_and_original_untouched', () => {
    const original = makeItem()
    const once = acknowledgeCheckpoint(original, '张三')
    const twice = acknowledgeCheckpoint(once, '李四', undefined, '2026-08-24T06:00:00Z')
    expect(twice).toBe(once) // 已 acked 原样返回（不可逆）
    expect(twice.ackedBy).toBe('张三')
    expect(original.status).toBe('pending') // 纯函数：入参不变
  })

  it('test_empty_note_stored_as_undefined', () => {
    const acked = acknowledgeCheckpoint(makeItem(), '张三', '')
    expect(acked.note).toBeUndefined()
  })
})

describe('buildAckPayload', () => {
  it('test_builds_payload_with_operator_and_note', () => {
    const built = buildAckPayload({ stepId: 's1' }, ' 张三 ', ' 工装 OK ')
    expect(built.ok).toBe(true)
    if (built.ok) {
      expect(built.payload).toEqual({ step_id: 's1', operator: '张三', note: '工装 OK' })
    }
  })

  it('test_omits_note_key_when_blank', () => {
    const built = buildAckPayload({ stepId: 's1' }, '张三', '   ')
    expect(built.ok).toBe(true)
    if (built.ok) {
      expect(built.payload).not.toHaveProperty('note')
    }
  })

  it('test_rejects_empty_operator', () => {
    expect(buildAckPayload({ stepId: 's1' }, '').ok).toBe(false)
    expect(buildAckPayload({ stepId: 's1' }, '   ').ok).toBe(false)
  })

  it('test_rejects_missing_step_id', () => {
    const built = buildAckPayload({ stepId: '' }, '张三')
    expect(built.ok).toBe(false)
    if (!built.ok) expect(built.error).toContain('步骤 ID')
  })
})

describe('canStartRun gating', () => {
  it('test_blocked_with_reason_when_pending_exists', () => {
    const gate = canStartRun([makeItem(), makeItem({ stepId: 's2' })])
    expect(gate.allowed).toBe(false)
    expect(gate.reason).toContain('2 个未确认检查点')
  })

  it('test_allowed_when_no_pending', () => {
    expect(canStartRun([]).allowed).toBe(true)
  })
})

describe('run lifecycle helpers', () => {
  it('test_needs_dut_swap_between_runs_only', () => {
    expect(needsDutSwap('run-1', 'run-2', true)).toBe(true)
    expect(needsDutSwap('run-1', 'run-1', true)).toBe(false)
    expect(needsDutSwap(null, 'run-2', true)).toBe(false)
    expect(needsDutSwap('run-1', null, true)).toBe(false)
  })

  it('test_terminal_status_detection', () => {
    for (const s of ['COMPLETED', 'FAILED', 'ABORTED']) expect(isTerminalStatus(s)).toBe(true)
    for (const s of ['RUNNING', 'PENDING', '']) expect(isTerminalStatus(s)).toBe(false)
  })
})

describe('buildCompletionSignature', () => {
  it('test_captures_signature_with_operator_and_timestamp', () => {
    const built = buildCompletionSignature('run-9', '李四', '2026-08-24T07:00:00Z')
    expect(built.ok).toBe(true)
    if (built.ok) {
      expect(built.payload).toEqual({
        run_id: 'run-9',
        operator: '李四',
        signed_at: '2026-08-24T07:00:00Z',
      })
    }
  })

  it('test_rejects_blank_operator_or_run', () => {
    expect(buildCompletionSignature('run-9', '').ok).toBe(false)
    expect(buildCompletionSignature('', '李四').ok).toBe(false)
  })
})

describe('shouldShowOfflineBanner', () => {
  it('test_shown_after_threshold_consecutive_failures', () => {
    expect(shouldShowOfflineBanner(0)).toBe(false)
    expect(shouldShowOfflineBanner(2)).toBe(false)
    expect(shouldShowOfflineBanner(3)).toBe(true)
    expect(shouldShowOfflineBanner(5)).toBe(true)
    expect(shouldShowOfflineBanner(2, 2)).toBe(true) // 自定义阈值
  })
})
