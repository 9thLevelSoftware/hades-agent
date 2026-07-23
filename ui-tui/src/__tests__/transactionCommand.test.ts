import { describe, expect, it, vi } from 'vitest'

import { findSlashCommand } from '../app/slash/registry.js'
import type { TransactionExecResponse } from '../gatewayTypes.js'

const transactionResponse = (overrides: Partial<TransactionExecResponse> = {}): TransactionExecResponse => ({
  action: 'show',
  exit_code: 0,
  ok: true,
  output: 'transaction tx-1: ready (revision 1)',
  ...overrides
})

const buildCtx = (result: Partial<TransactionExecResponse> = {}, { stale = false } = {}) => {
  const rpc = vi.fn((_method: string, _params?: Record<string, unknown>) =>
    Promise.resolve(transactionResponse(result))
  )

  const request = vi.fn(() => Promise.resolve({}))
  const sys = vi.fn()
  const page = vi.fn()
  const panel = vi.fn()
  const isStale = () => stale

  const guarded =
    <T>(fn: (r: T) => void) =>
    (r: null | T) => {
      if (!isStale() && r) {
        fn(r)
      }
    }

  const ctx = {
    gateway: { gw: { request }, rpc },
    guarded,
    guardedErr: vi.fn(),
    sid: 'sid-1',
    stale: isStale,
    transcript: { page, panel, sys }
  }

  const run = async (arg: string) => {
    findSlashCommand('transaction')!.run(arg, ctx as never, `/transaction${arg ? ` ${arg}` : ''}`)
    await Promise.resolve()
    await Promise.resolve()
  }

  return { ctx, page, panel, rpc, run, sys }
}

const printed = (sys: ReturnType<typeof vi.fn>) => sys.mock.calls.map(c => c[0]).join('\n')

describe('/transaction slash command', () => {
  it('registers transaction natively with the tx alias', () => {
    expect(findSlashCommand('transaction')?.name).toBe('transaction')
    expect(findSlashCommand('tx')?.name).toBe('transaction')
  })

  it('routes /transaction through native transaction.exec, never slash.exec', () => {
    const { ctx, rpc } = buildCtx()

    findSlashCommand('transaction')!.run('preview tx-1', ctx as never, '/transaction preview tx-1')

    expect(rpc).toHaveBeenCalledWith('transaction.exec', {
      argv: ['preview', 'tx-1'],
      session_id: 'sid-1'
    })
    expect(ctx.gateway.gw.request).not.toHaveBeenCalledWith('slash.exec', expect.anything())
    expect(ctx.gateway.gw.request).not.toHaveBeenCalled()
  })

  it('tokenizes with the slash argv splitter, never a shell', async () => {
    const { rpc, run } = buildCtx()

    await run('  commit   tx-1  --through-node write ')

    expect(rpc).toHaveBeenCalledWith('transaction.exec', {
      argv: ['commit', 'tx-1', '--through-node', 'write'],
      session_id: 'sid-1'
    })
  })

  it('renders short mutating results as system lines', async () => {
    const { page, run, sys } = buildCtx({
      action: 'commit',
      output: 'commit committed; nodes: write'
    })

    await run('commit tx-1')

    expect(printed(sys)).toContain('commit committed')
    expect(page).not.toHaveBeenCalled()
  })

  it('renders long inspection output as a page', async () => {
    const longOutput = Array.from({ length: 12 }, (_, i) => `node line ${i}`).join('\n')
    const { page, run } = buildCtx({ action: 'preview', output: longOutput })

    await run('preview tx-1')

    expect(page).toHaveBeenCalledWith(longOutput, 'Transaction preview')
  })

  it('prints a persistent unknown_effect warning naming reconcile', async () => {
    const { run, sys } = buildCtx({
      action: 'reconcile',
      counts: { landed: 0, not_landed: 0, skipped: 0, unknown: 1 },
      output: 'reconciled: status unknown_effect',
      status: 'unknown_effect',
      transaction: {
        current_revision: 1,
        receipt_id: null,
        status: 'unknown_effect',
        transaction_id: 'tx-1'
      }
    })

    await run('reconcile tx-1')

    const text = printed(sys)
    expect(text).toContain('unknown_effect')
    expect(text).toContain('/transaction reconcile tx-1')
  })

  it.each([
    ['blocked', 'transaction blocked — execution was prevented'],
    ['failed', 'transaction failed — operation did not complete'],
    ['partially_compensated', 'transaction partially compensated — compensation incomplete']
  ])('renders %s as a known failure without uncertainty guidance', async (status, output) => {
    const { run, sys } = buildCtx({
      action: 'commit',
      ok: false,
      output,
      status,
      compensated_nodes: ['write'],
      transaction: {
        current_revision: 1,
        receipt_id: null,
        status,
        transaction_id: 'tx-1'
      }
    })

    await run('commit tx-1')

    const text = printed(sys)
    expect(text).toContain(output)
    expect(text).not.toContain('effect uncertain')
    expect(text).not.toContain('do not retry')
    expect(text).not.toContain('/transaction reconcile')
  })

  it('warns for unknown counts even when status is in flight', async () => {
    const { run, sys } = buildCtx({
      action: 'reconcile',
      counts: { landed: 1, not_landed: 0, skipped: 0, unknown: 1 },
      output: 'reconciled: status committing',
      status: 'committing',
      transaction: {
        current_revision: 1,
        receipt_id: null,
        status: 'committing',
        transaction_id: 'tx-in-flight'
      }
    })

    await run('reconcile tx-in-flight')

    const text = printed(sys)
    expect(text).toContain('warning: unknown_effect')
    expect(text).toContain('/transaction reconcile tx-in-flight')
  })

  it('renders successful compensation as normal output without an uncertainty warning', async () => {
    const { run, sys } = buildCtx({
      action: 'compensate',
      compensated_nodes: ['write'],
      ok: true,
      output: 'compensation compensated; nodes: write',
      status: 'compensated'
    })

    await run('compensate tx-1')

    const text = printed(sys)
    expect(text).toContain('compensation compensated')
    expect(text).not.toContain('warning:')
    expect(text).not.toContain('do not retry')
    expect(text).not.toContain('/transaction reconcile')
  })

  it('uses the top-level transaction ID when an uncertain result has no nested transaction', async () => {
    const { run, sys } = buildCtx({
      action: 'commit',
      ok: false,
      output: 'transaction effect uncertain — do not retry; reconcile first',
      status: 'unknown_effect',
      transaction_id: 'tx:actionable'
    })

    await run('commit tx:actionable')

    const text = printed(sys)
    expect(text).toContain('/transaction reconcile tx:actionable')
    expect(text).not.toContain('/transaction reconcile <tx>')
  })

  it('drops the render when the slash flight went stale', async () => {
    const { page, run, sys } = buildCtx({}, { stale: true })

    await run('show tx-1')

    expect(sys).not.toHaveBeenCalled()
    expect(page).not.toHaveBeenCalled()
  })
})
