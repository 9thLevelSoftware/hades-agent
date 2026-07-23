import { describe, expect, it, vi } from 'vitest'

import { findSlashCommand } from '../app/slash/registry.js'
import type { ReceiptDetail, ReceiptExecResponse, ReceiptObservationDetail } from '../gatewayTypes.js'

const RECEIPT_ID = `rct_${'a'.repeat(64)}`

const receiptDetail = (overrides: Partial<ReceiptDetail> = {}): ReceiptDetail => ({
  artifact_count: 0,
  claim_count: 0,
  content_hash: 'sha256:receipt-hash',
  decided_at: '2026-07-10T00:00:00Z',
  evidence_count: 0,
  mission_id: null,
  observation_count: 0,
  receipt_id: RECEIPT_ID,
  scorer_id: 'hades.code-turn-end-state',
  scorer_version: '1.0',
  session_id: 's1',
  status: 'verified',
  subject_id: 's1:t1',
  subject_kind: 'turn',
  transaction_id: null,
  turn_id: 't1',
  uncertainty: [],
  ...overrides
})

const observationDetail = (overrides: Partial<ReceiptObservationDetail> = {}): ReceiptObservationDetail => ({
  content_hash: 'sha256:obs-hash',
  observation_id: `obs_${'b'.repeat(64)}`,
  observed_at: '2026-07-11T09:00:00Z',
  previous_observation_id: null,
  receipt_id: RECEIPT_ID,
  scorer_id: 'hades.code-turn-end-state',
  scorer_version: '1.0',
  status: 'failed',
  uncertainty: [],
  ...overrides
})

const receiptResponse = (overrides: Partial<ReceiptExecResponse> = {}): ReceiptExecResponse => ({
  action: 'list',
  exit_code: 0,
  ok: true,
  output: '(no output)',
  receipts: [],
  ...overrides
})

/** Build a SlashRunCtx double whose native rpc and slash-worker request are both observable. */
const buildCtx = (result: Partial<ReceiptExecResponse> = {}, { stale = false } = {}) => {
  const rpc = vi.fn((_method: string, _params?: Record<string, unknown>) => Promise.resolve(receiptResponse(result)))

  const request = vi.fn(() => Promise.resolve({}))
  const sys = vi.fn()
  const page = vi.fn()
  const panel = vi.fn()
  const isStale = () => stale

  // Mirror the real guarded semantics: drop the render when the slash
  // flight went stale (session switched) or the response is empty.
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
    findSlashCommand('receipt')!.run(arg, ctx as never, `/receipt${arg ? ` ${arg}` : ''}`)
    await Promise.resolve()
    await Promise.resolve()
  }

  return { ctx, page, panel, rpc, run, sys }
}

const printed = (sys: ReturnType<typeof vi.fn>) => sys.mock.calls.map(c => c[0]).join('\n')

describe('/receipt slash command', () => {
  it('registers receipt natively with the receipts alias', () => {
    expect(findSlashCommand('receipt')?.name).toBe('receipt')
    expect(findSlashCommand('receipts')?.name).toBe('receipt')
  })

  it('routes receipt commands through native receipt.exec, never slash.exec', () => {
    const { ctx, rpc } = buildCtx()

    findSlashCommand('receipt')!.run(`recheck ${RECEIPT_ID}`, ctx as never, `/receipt recheck ${RECEIPT_ID}`)

    expect(rpc).toHaveBeenCalledWith('receipt.exec', {
      argv: ['recheck', RECEIPT_ID],
      session_id: 'sid-1'
    })
    expect(ctx.gateway.gw.request).not.toHaveBeenCalledWith('slash.exec', expect.anything())
    expect(ctx.gateway.gw.request).not.toHaveBeenCalled()
  })

  it('tokenizes with the slash argv splitter, never a shell', async () => {
    const { rpc, run } = buildCtx()

    await run(`  show   ${RECEIPT_ID}  --observation all `)

    expect(rpc).toHaveBeenCalledWith('receipt.exec', {
      argv: ['show', RECEIPT_ID, '--observation', 'all'],
      session_id: 'sid-1'
    })
  })

  it('bare /receipt asks the shared parser for help', async () => {
    const { rpc, run } = buildCtx({ action: '--help', output: 'usage: hades receipt ...', receipts: undefined })

    await run('')

    expect(rpc).toHaveBeenCalledWith('receipt.exec', { argv: ['--help'], session_id: 'sid-1' })
  })

  it('renders the receipt list as a panel of truthful summaries', async () => {
    const { panel, run } = buildCtx({
      action: 'list',
      output: `RECEIPTS (1)\n  ${RECEIPT_ID} ...`,
      receipts: [
        {
          content_hash: 'sha256:receipt-hash',
          decided_at: '2026-07-10T00:00:00Z',
          receipt_id: RECEIPT_ID,
          scorer_id: 'hades.receipts.default',
          scorer_version: '1.0',
          session_id: 's1',
          status: 'completed_unverified',
          subject_id: 's1:t1',
          subject_kind: 'turn'
        }
      ]
    })

    await run('list')

    expect(panel).toHaveBeenCalled()
    const [title, sections] = panel.mock.calls[0] as [string, { rows: [string, string][] }[]]

    expect(title).toBe('Receipts')
    const flat = sections.flatMap(s => s.rows.flat()).join('\n')

    expect(flat).toContain(RECEIPT_ID)
    expect(flat).toContain('completed_unverified')
    // completed_unverified is never rendered as success.
    expect(flat.toLowerCase()).not.toContain('success')
  })

  it('renders show as a page distinguishing the original from the latest recheck', async () => {
    const output = [
      `Receipt ${RECEIPT_ID}`,
      'Original: verified (independently scored end state) — decided 2026-07-10T00:00:00Z',
      'Latest recheck: failed (the requested end state does not hold) — observed 2026-07-11T09:00:00Z',
      'Attestations (provenance only — a signature never proves truth):'
    ].join('\n')

    const { page, run } = buildCtx({
      action: 'show',
      observations: [observationDetail()],
      output,
      receipt: receiptDetail(),
      receipts: undefined
    })

    await run(`show ${RECEIPT_ID}`)

    expect(page).toHaveBeenCalledWith(output, `Receipt ${RECEIPT_ID}`)
    const [text] = page.mock.calls[0] as [string, string]

    expect(text).toContain('Original: verified')
    expect(text).toContain('Latest recheck: failed')
    expect(text).toContain('provenance only')
  })

  it('renders claims as a panel of claim → evidence → artifact edges', async () => {
    const { panel, run } = buildCtx({
      action: 'claims',
      claim_edges: [
        {
          artifact_ids: [`art_${'d'.repeat(64)}`],
          claim_id: `clm_${'c'.repeat(64)}`,
          claim_kind: 'effect',
          evidence_ids: [`evd_${'e'.repeat(64)}`],
          required: true,
          statement: 'README contains marker',
          uncertainty: [],
          verdict: 'satisfied'
        }
      ],
      output: 'Claims ...',
      receipts: undefined
    })

    await run(`claims ${RECEIPT_ID}`)

    expect(panel).toHaveBeenCalled()
    const [title, sections] = panel.mock.calls[0] as [string, { rows: [string, string][] }[]]

    expect(title.toLowerCase()).toContain('claim')
    const flat = sections.flatMap(s => s.rows.flat()).join('\n')

    expect(flat).toContain(`clm_${'c'.repeat(64)}`)
    expect(flat).toContain(`evd_${'e'.repeat(64)}`)
    expect(flat).toContain(`art_${'d'.repeat(64)}`)
    expect(flat).toContain('satisfied')
  })

  it('always prints the persistent unknown-effect warning for unknown_effect receipts', async () => {
    const { run, sys } = buildCtx({
      action: 'show',
      output: `Receipt ${RECEIPT_ID}\nOriginal: unknown_effect ...`,
      receipt: receiptDetail({ status: 'unknown_effect' }),
      receipts: undefined
    })

    await run(`show ${RECEIPT_ID}`)

    const text = printed(sys)

    expect(text).toContain('Do not retry the effect; recheck/reconcile evidence.')
  })

  it('renders recheck as a concise system result and warns on unknown_effect observations', async () => {
    const { page, run, sys } = buildCtx({
      action: 'recheck',
      observations: [observationDetail({ status: 'unknown_effect' })],
      output: `Recheck of ${RECEIPT_ID} appended observation ...`,
      receipts: undefined
    })

    await run(`recheck ${RECEIPT_ID}`)

    expect(page).not.toHaveBeenCalled()
    const text = printed(sys)

    expect(text).toContain('appended')
    expect(text).toContain('unknown_effect')
    expect(text).toContain('immutable')
    expect(text).toContain('Do not retry the effect; recheck/reconcile evidence.')
  })

  it('renders export as a concise system result without the export path', async () => {
    const { page, run, sys } = buildCtx({
      action: 'export',
      exported: true,
      output: 'receipt export completed (path withheld)',
      receipts: undefined
    })

    await run(`export ${RECEIPT_ID} --output receipt.json`)

    expect(page).not.toHaveBeenCalled()
    expect(printed(sys)).toContain('path withheld')
    expect(printed(sys)).not.toContain('/tmp/work/receipt.json')
  })

  it('surfaces a truthfully-unsigned export warning', async () => {
    const { run, sys } = buildCtx({
      action: 'export',
      exported: true,
      output: 'receipt export completed (path withheld)',
      receipts: undefined,
      warning: 'signing provider unavailable — the export is truthfully unsigned'
    })

    await run(`export ${RECEIPT_ID} --output receipt.json --sign`)

    expect(printed(sys)).toContain('truthfully unsigned')
  })

  it('renders prune as a concise system result', async () => {
    const { page, run, sys } = buildCtx({
      action: 'prune',
      output: 'Pruned retention plan rpl_x\n  receipts deleted: 1',
      receipts: undefined
    })

    await run('prune --confirm-plan sha256:abc')

    expect(page).not.toHaveBeenCalled()
    expect(printed(sys)).toContain('Pruned retention plan')
  })

  it('keeps the provenance-only label on verify-signature output', async () => {
    const { run, sys } = buildCtx({
      action: 'verify-signature',
      output: 'Signature verification (provenance only — never truth):\n  (no attestations recorded)',
      receipts: undefined
    })

    await run(`verify-signature ${RECEIPT_ID}`)

    expect(printed(sys)).toContain('provenance only')
  })

  it('pages long non-detail output (pager behavior)', async () => {
    const output = Array.from({ length: 8 }, (_, i) => `line ${i}: retention detail`).join('\n')

    const { page, run } = buildCtx({
      action: 'retention-plan',
      output,
      receipts: undefined,
      retention_plan_hash: 'sha256:plan'
    })

    await run('retention-plan')

    expect(page).toHaveBeenCalledWith(output, 'Receipt retention-plan')
  })

  it('drops the render when the slash flight went stale (session switch guard)', async () => {
    const { page, panel, run, sys } = buildCtx(
      { action: 'show', output: 'Receipt ...', receipt: receiptDetail(), receipts: undefined },
      { stale: true }
    )

    await run(`show ${RECEIPT_ID}`)

    expect(sys).not.toHaveBeenCalled()
    expect(page).not.toHaveBeenCalled()
    expect(panel).not.toHaveBeenCalled()
  })
})
