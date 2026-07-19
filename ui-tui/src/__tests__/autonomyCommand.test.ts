import { describe, expect, it, vi } from 'vitest'

import { findSlashCommand } from '../app/slash/registry.js'
import type { AutonomyExecResponse } from '../gatewayTypes.js'

const autonomyResponse = (overrides: Partial<AutonomyExecResponse> = {}): AutonomyExecResponse => ({
  action: 'status',
  applied: null,
  approval_pending: false,
  audit: [],
  contract: { hash: 'hash-1', mode: 'enforce', profile_id: 'default', version: 3 },
  decision: null,
  exit_code: 0,
  ok: true,
  output: 'profile: default  mode: enforce',
  preview: null,
  profile_home: '/tmp/profile',
  rules: [],
  suggestions: [],
  ...overrides
})

const guarded =
  <T>(fn: (r: T) => void) =>
  (r: null | T) => {
    if (r) {
      fn(r)
    }
  }

/** Build a SlashRunCtx double whose native rpc and slash-worker request are both observable. */
const buildCtx = (result: Partial<AutonomyExecResponse> = {}) => {
  const rpc = vi.fn((_method: string, _params?: Record<string, unknown>) =>
    Promise.resolve(autonomyResponse(result))
  )
  const request = vi.fn(() => Promise.resolve({}))
  const sys = vi.fn()
  const page = vi.fn()
  const panel = vi.fn()

  const ctx = {
    gateway: { gw: { request }, rpc },
    guarded,
    guardedErr: vi.fn(),
    sid: 'sid-1',
    stale: () => false,
    transcript: { page, panel, sys }
  }

  const run = async (arg: string) => {
    findSlashCommand('autonomy')!.run(arg, ctx as never, `/autonomy${arg ? ` ${arg}` : ''}`)
    await Promise.resolve()
    await Promise.resolve()
  }

  return { ctx, page, panel, rpc, run, sys }
}

const printed = (sys: ReturnType<typeof vi.fn>) => sys.mock.calls.map(c => c[0]).join('\n')

describe('/autonomy slash command', () => {
  it('registers autonomy with the authority alias', () => {
    expect(findSlashCommand('autonomy')?.name).toBe('autonomy')
    expect(findSlashCommand('authority')?.name).toBe('autonomy')
  })

  it('routes mutating autonomy commands through native autonomy.exec', () => {
    const { ctx, rpc } = buildCtx()

    findSlashCommand('autonomy')!.run(
      'mandate revoke m-1 --reason done',
      ctx as never,
      '/autonomy mandate revoke m-1 --reason done'
    )

    expect(rpc).toHaveBeenCalledWith('autonomy.exec', {
      argv: ['mandate', 'revoke', 'm-1', '--reason', 'done'],
      session_id: 'sid-1'
    })
    expect(ctx.gateway.gw.request).not.toHaveBeenCalledWith('slash.exec', expect.anything())
    expect(ctx.gateway.gw.request).not.toHaveBeenCalled()
  })

  it('tokenizes with the slash argv splitter, never a shell', async () => {
    const { rpc, run } = buildCtx()

    await run('  rule   show  allow-send ')

    expect(rpc).toHaveBeenCalledWith('autonomy.exec', {
      argv: ['rule', 'show', 'allow-send'],
      session_id: 'sid-1'
    })
  })

  it('bare /autonomy asks the shared parser for help', async () => {
    const { rpc, run } = buildCtx({ action: 'help', output: 'usage: hades autonomy ...' })

    await run('')

    expect(rpc).toHaveBeenCalledWith('autonomy.exec', { argv: ['--help'], session_id: 'sid-1' })
  })

  it('renders status output as a page', async () => {
    const { page, run } = buildCtx({
      action: 'status',
      output: 'profile: default  mode: enforce\ncontract: version 3 hash hash-1\nrules: 1 stable'
    })

    await run('status')

    expect(page).toHaveBeenCalledWith(
      'profile: default  mode: enforce\ncontract: version 3 hash hash-1\nrules: 1 stable',
      'Autonomy status'
    )
  })

  it('renders a mutation preview as a persistent system message with the exact hash', async () => {
    const { run, sys } = buildCtx({
      action: 'rule',
      approval_pending: true,
      output: 'previewed change (not applied)',
      preview: {
        added_rule_ids: ['allow-send-2'],
        after_contract_hash: 'after-hash',
        applied: false,
        before_contract_hash: 'before-hash',
        changed_rule_ids: [],
        profile_id: 'default',
        removed_rule_ids: [],
        warnings: []
      }
    })

    await run('rule add --file rule.yaml')

    const text = printed(sys)

    expect(text).toContain('not applied')
    expect(text).toContain('before-hash')
    expect(text).toContain('after-hash')
  })

  it('renders an applied mutation with the exact new contract hash', async () => {
    const { run, sys } = buildCtx({
      action: 'rule',
      applied: { applied: true, config_hash: 'cfg-1', contract_hash: 'hash-9', contract_version: 9 },
      output: 'applied: contract version 9 hash hash-9'
    })

    await run('rule add --file rule.yaml --apply --expected-contract-hash before-hash')

    const text = printed(sys)

    expect(text).toContain('applied')
    expect(text).toContain('hash-9')
  })

  it('renders a deny decision as a warning naming the edit commands', async () => {
    const { run, sys } = buildCtx({
      action: 'evaluate',
      decision: {
        authority_hash: 'hash-1',
        authority_version: 3,
        clarification: null,
        code: 'sensitive_data_boundary',
        conflicting_rule_ids: ['deny-cred'],
        context_hash: 'ctx-1',
        edit_targets: ['hades autonomy rule edit deny-cred --file RULE.yaml'],
        expires_at_ms: null,
        matched_rule_ids: ['deny-cred'],
        reason: 'credential data cannot leave the profile',
        required_evidence: [],
        stage: 'explain',
        verdict: 'deny'
      },
      exit_code: 3,
      ok: false,
      output: 'deny / sensitive_data_boundary'
    })

    await run('evaluate --file action.yaml')

    const text = printed(sys)

    expect(text).toContain('warning')
    expect(text).toContain('deny / sensitive_data_boundary')
    expect(text).toContain('hades autonomy rule edit deny-cred --file RULE.yaml')
  })

  it('renders suggestions with provenance and the not-authorization label', async () => {
    const { panel, run } = buildCtx({
      action: 'suggestion',
      output: '(suggestions)',
      suggestions: [
        {
          action_classes: ['message.send'],
          confidence_ppm: 990000,
          effect: 'allow',
          provenance: 'learner:pattern-miner (observed-behavior)',
          rule_id: 'suggest-1',
          source: 'learned_suggestion',
          state: 'awaiting_confirmation'
        }
      ]
    })

    await run('suggestion list')

    expect(panel).toHaveBeenCalled()
    const [title, sections] = panel.mock.calls[0] as [string, { rows: [string, string][] }[]]

    expect(title.toLowerCase()).toContain('not authorization')
    const flat = sections.flatMap(s => s.rows.flat()).join('\n')

    expect(flat).toContain('suggest-1')
    expect(flat).toContain('learner:pattern-miner')
    expect(flat).toContain('990000')
  })
})
