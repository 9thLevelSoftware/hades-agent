import type {
  AutonomyExecResponse,
  BrowserManageResponse,
  CommandsCatalogResponse,
  DelegationPauseResponse,
  ProcessStopResponse,
  ReceiptExecResponse,
  ReloadEnvResponse,
  ReloadMcpResponse,
  RollbackDiffResponse,
  RollbackListResponse,
  RollbackRestoreResponse,
  SlashExecResponse,
  SpawnTreeListResponse,
  SpawnTreeLoadResponse,
  ToolsConfigureResponse
} from '../../../gatewayTypes.js'
import type { PanelSection } from '../../../types.js'
import { applyDelegationStatus, getDelegationState } from '../../delegationStore.js'
import { patchOverlayState } from '../../overlayStore.js'
import { getSpawnHistory, pushDiskSnapshot, setDiffPair, type SpawnSnapshot } from '../../spawnHistoryStore.js'
import type { SlashCommand, SlashRunCtx } from '../types.js'

interface SkillInfo {
  category?: string
  description?: string
  name?: string
  path?: string
}

interface SkillsListResponse {
  skills?: Record<string, string[]>
}

interface SkillsInspectResponse {
  info?: SkillInfo
}

interface SkillsSearchResponse {
  results?: { description?: string; name: string }[]
}

interface SkillsInstallResponse {
  installed?: boolean
  name?: string
}

interface SkillsBrowseItem {
  description?: string
  name: string
  source?: string
  trust?: string
}

interface SkillsBrowseResponse {
  items?: SkillsBrowseItem[]
  page?: number
  total?: number
  total_pages?: number
}

interface SkillsReloadResponse {
  output?: string
}

const isLongText = (text: string) => text.length > 180 || text.split('\n').filter(Boolean).length > 2

/**
 * Render one structured `autonomy.exec` result.
 *
 * Deny/conflict decisions become warnings naming the exact edit commands;
 * mutation preview/apply become persistent system messages carrying the
 * exact contract hash; suggestions always carry source/confidence plus the
 * "not authorization" label. An in-flight action that *asks* reuses the
 * existing approval/clarify overlays — this renderer never adds a second
 * modal, so a standalone evaluate/explain just prints its question.
 */
const renderAutonomyResult = (action: string, r: AutonomyExecResponse, ctx: SlashRunCtx): void => {
  const { page, panel, sys } = ctx.transcript
  const decision = r.decision

  if (decision && (decision.verdict === 'deny' || (decision.conflicting_rule_ids?.length ?? 0) > 0)) {
    sys(`warning: ${decision.verdict} / ${decision.code} — ${decision.reason || 'no matching authority'}`)

    for (const target of decision.edit_targets ?? []) {
      sys(`edit: ${target}`)
    }

    return
  }

  if (decision) {
    return isLongText(r.output) ? page(r.output, 'Autonomy decision') : sys(r.output)
  }

  if (r.preview) {
    sys(
      `autonomy change previewed (not applied) · before ${r.preview.before_contract_hash} → after ${r.preview.after_contract_hash}`
    )

    if (r.output) {
      sys(r.output)
    }

    return
  }

  if (r.applied) {
    return sys(`autonomy change applied · contract v${r.applied.contract_version} hash ${r.applied.contract_hash}`)
  }

  if (r.suggestions.length) {
    return panel('Autonomy suggestions (not authorization)', [
      {
        rows: r.suggestions.map(s => [
          s.rule_id,
          `${s.effect} · ${s.state} · ${s.provenance ?? 'unknown provenance'} · confidence ${
            s.confidence_ppm ?? '?'
          } ppm — not authorization`
        ])
      }
    ])
  }

  const title = `Autonomy ${(r.action || action).replace(/^-+/, '') || 'help'}`
  const text = r.output || '(no output)'

  return isLongText(text) ? page(text, title) : sys(text)
}

// unknown_effect is neither a failure nor retry-safe: the effect may or may
// not have happened, so retrying could double-apply it. This warning is
// persistent — it prints on EVERY response that carries an unknown_effect
// receipt or observation.
const UNKNOWN_EFFECT_WARNING = 'unknown_effect — Do not retry the effect; recheck/reconcile evidence.'

const receiptStatuses = (r: ReceiptExecResponse): string[] => [
  ...(r.receipt ? [r.receipt.status] : []),
  ...(r.receipts?.map(summary => summary.status) ?? []),
  ...(r.observations?.map(observation => observation.status) ?? [])
]

/**
 * Render one structured `receipt.exec` result.
 *
 * `output` carries the SAME truthful text rendering as `hades receipt ...`
 * and the classic slash path (one renderer, no second authority surface):
 * `completed_unverified` is never success, `unknown_effect` is never
 * failure/retry-safe, and attestations stay labeled "provenance only".
 * list/claims become panels, show becomes a page, and the mutating verbs
 * (recheck/export/prune) become concise system results.
 */
const renderReceiptResult = (action: string, r: ReceiptExecResponse, ctx: SlashRunCtx): void => {
  const { page, panel, sys } = ctx.transcript

  if (r.warning) {
    sys(`warning: ${r.warning}`)
  }

  if (receiptStatuses(r).includes('unknown_effect')) {
    sys(`warning: ${UNKNOWN_EFFECT_WARNING}`)
  }

  if (r.action === 'list' && r.receipts) {
    if (!r.receipts.length) {
      return sys('no receipts match — capture is controlled by receipts.mode in config.yaml')
    }

    return panel('Receipts', [
      {
        rows: r.receipts.map(summary => [
          summary.receipt_id,
          `${summary.subject_kind}:${summary.subject_id} · ${summary.status} · decided ${summary.decided_at}`
        ])
      }
    ])
  }

  if (r.action === 'show') {
    const title = r.receipt?.receipt_id ? `Receipt ${r.receipt.receipt_id}` : 'Receipt'

    return page(r.output || '(no output)', title)
  }

  if (r.action === 'claims' && r.claim_edges) {
    if (!r.claim_edges.length) {
      return sys('no claims recorded on this receipt')
    }

    return panel('Receipt claims (claim → evidence → artifacts)', [
      {
        rows: r.claim_edges.map(edge => [
          `${edge.claim_id} [${edge.verdict}]`,
          [
            edge.statement,
            `  evidence: ${edge.evidence_ids.join(', ') || '(none)'}`,
            `  artifacts: ${edge.artifact_ids.join(', ') || '(none)'}`,
            ...edge.uncertainty.map(note => `  uncertainty: ${note}`)
          ].join('\n')
        ])
      }
    ])
  }

  if (r.action === 'recheck') {
    const observation = r.observations?.[r.observations.length - 1]

    return sys(
      `recheck appended observation ${observation?.observation_id ?? '(unknown)'} · status ${
        observation?.status ?? '(unknown)'
      } · the original receipt is immutable`
    )
  }

  if (r.action === 'export') {
    return sys(`exported receipt to ${r.export_path ?? '(unknown path)'}`)
  }

  if (r.action === 'prune') {
    return sys(r.output.split('\n')[0] || 'retention plan pruned')
  }

  // verify-signature / retention-plan / help and anything else: the shared
  // text rendering, paged when long.
  const title = `Receipt ${(r.action || action).replace(/^-+/, '') || 'help'}`
  const text = r.output || '(no output)'

  return isLongText(text) ? page(text, title) : sys(text)
}

export const opsCommands: SlashCommand[] = [
  {
    aliases: ['receipts'],
    help: 'inspect verified outcome receipts (evidence, artifacts, rechecks)',
    name: 'receipt',
    run: (arg, ctx) => {
      // Existing slash tokenizer semantics (whitespace split) — never a shell.
      const tokens = arg.trim().split(/\s+/).filter(Boolean)
      const argv = tokens.length ? tokens : ['--help']

      // Native route only: recheck/prune write observations/tombstones, so
      // receipt commands must never reach the slash-worker fallback
      // (slash.exec) — see slashParity.test.ts.
      ctx.gateway
        .rpc<ReceiptExecResponse>('receipt.exec', { argv, session_id: ctx.sid })
        .then(ctx.guarded<ReceiptExecResponse>(r => renderReceiptResult(argv[0]!, r, ctx)))
        .catch(ctx.guardedErr)
    }
  },

  {
    aliases: ['authority'],
    help: 'explain and edit what Hades may do (allow/ask/deny authority)',
    name: 'autonomy',
    run: (arg, ctx) => {
      // Existing slash tokenizer semantics (whitespace split) — never a shell.
      const tokens = arg.trim().split(/\s+/).filter(Boolean)
      const argv = tokens.length ? tokens : ['--help']

      // Native route only: mutating autonomy verbs must never reach the
      // slash-worker fallback (slash.exec) — see slashParity.test.ts.
      ctx.gateway
        .rpc<AutonomyExecResponse>('autonomy.exec', { argv, session_id: ctx.sid })
        .then(ctx.guarded<AutonomyExecResponse>(r => renderAutonomyResult(argv[0]!, r, ctx)))
        .catch(ctx.guardedErr)
    }
  },

  {
    help: 'stop background processes',
    name: 'stop',
    run: (_arg, ctx) => {
      ctx.gateway
        .rpc<ProcessStopResponse>('process.stop', {})
        .then(
          ctx.guarded<ProcessStopResponse>(r => {
            const killed = Number(r.killed ?? 0)
            const noun = killed === 1 ? 'process' : 'processes'
            ctx.transcript.sys(`stopped ${killed} background ${noun}`)
          })
        )
        .catch(ctx.guardedErr)
    }
  },

  {
    aliases: ['reload_mcp'],
    help: 'reload MCP servers in the live session (warns about prompt cache invalidation)',
    name: 'reload-mcp',
    run: (arg, ctx) => {
      // Parse arg: `now` / `always` skip the confirmation gate.
      // `always` additionally persists approvals.mcp_reload_confirm=false.
      const a = (arg || '').trim().toLowerCase()

      const params: { session_id: string | null; confirm?: boolean; always?: boolean } = {
        session_id: ctx.sid
      }

      if (a === 'now' || a === 'approve' || a === 'once' || a === 'yes') {
        params.confirm = true
      } else if (a === 'always') {
        params.confirm = true
        params.always = true
      }

      ctx.gateway
        .rpc<ReloadMcpResponse>('reload.mcp', params)
        .then(
          ctx.guarded<ReloadMcpResponse>(r => {
            if (r.status === 'confirm_required') {
              ctx.transcript.sys(r.message || '/reload-mcp requires confirmation')

              return
            }

            if (r.status === 'reloaded') {
              ctx.transcript.sys(
                params.always
                  ? 'MCP servers reloaded · future /reload-mcp will run without confirmation'
                  : 'MCP servers reloaded'
              )

              return
            }

            ctx.transcript.sys('reload complete')
          })
        )
        .catch(ctx.guardedErr)
    }
  },

  {
    help: 're-read ~/.hades/.env into the running gateway (CLI parity)',
    name: 'reload',
    run: (_arg, ctx) => {
      ctx.gateway
        .rpc<ReloadEnvResponse>('reload.env', {})
        .then(
          ctx.guarded<ReloadEnvResponse>(r => {
            const n = Number(r.updated ?? 0)
            const noun = n === 1 ? 'var' : 'vars'

            ctx.transcript.sys(`reloaded .env (${n} ${noun} updated)`)
          })
        )
        .catch(ctx.guardedErr)
    }
  },

  {
    help: 'manage browser CDP connection [connect|disconnect|status]',
    name: 'browser',
    run: (arg, ctx) => {
      const [rawAction = 'status', ...rest] = arg.trim().split(/\s+/).filter(Boolean)
      const action = rawAction.toLowerCase()

      if (!['connect', 'disconnect', 'status'].includes(action)) {
        return ctx.transcript.sys(
          'usage: /browser [connect|disconnect|status] [url] · persistent: set browser.cdp_url in config.yaml'
        )
      }

      const sid = ctx.sid ?? null
      const url = action === 'connect' ? rest.join(' ').trim() || 'http://127.0.0.1:9222' : undefined

      if (url) {
        ctx.transcript.sys(`checking Chromium-family browser remote debugging at ${url}...`)
      }

      ctx.gateway
        .rpc<BrowserManageResponse>('browser.manage', { action, session_id: sid, ...(url && { url }) })
        .then(
          ctx.guarded<BrowserManageResponse>(r => {
            // Without a session we can't subscribe to streamed
            // browser.progress events, so flush the bundled list.
            if (!sid) {
              r.messages?.forEach(message => ctx.transcript.sys(message))
            }

            if (action === 'status') {
              return ctx.transcript.sys(
                r.connected
                  ? `browser connected: ${r.url || '(url unavailable)'}`
                  : 'browser not connected (try /browser connect <url> or set browser.cdp_url in config.yaml)'
              )
            }

            if (action === 'disconnect') {
              return ctx.transcript.sys('browser disconnected')
            }

            if (r.connected) {
              ctx.transcript.sys('Browser connected to live Chromium-family browser via CDP')
              ctx.transcript.sys(`Endpoint: ${r.url || '(url unavailable)'}`)
              ctx.transcript.sys('next browser tool call will use this CDP endpoint')
            }
          })
        )
        .catch(ctx.guardedErr)
    }
  },

  {
    help: 'list, diff, or restore checkpoints',
    name: 'rollback',
    run: (arg, ctx) => {
      if (!ctx.sid) {
        return ctx.transcript.sys('no active session — nothing to rollback')
      }

      const trimmed = arg.trim()
      const [first = '', ...rest] = trimmed.split(/\s+/).filter(Boolean)
      const lower = first.toLowerCase()

      if (!trimmed || lower === 'list' || lower === 'ls') {
        return ctx.gateway
          .rpc<RollbackListResponse>('rollback.list', { session_id: ctx.sid })
          .then(
            ctx.guarded<RollbackListResponse>(r => {
              if (!r.enabled) {
                return ctx.transcript.sys('checkpoints are not enabled')
              }

              const checkpoints = r.checkpoints ?? []

              if (!checkpoints.length) {
                return ctx.transcript.sys('no checkpoints found')
              }

              ctx.transcript.panel('Rollback checkpoints', [
                {
                  rows: checkpoints.map((c, idx) => [
                    `${idx + 1}. ${c.hash.slice(0, 10)}`,
                    [c.timestamp, c.message].filter(Boolean).join(' · ') || '(no metadata)'
                  ])
                }
              ])
            })
          )
          .catch(ctx.guardedErr)
      }

      if (lower === 'diff') {
        const hash = rest[0]

        if (!hash) {
          return ctx.transcript.sys('usage: /rollback diff <checkpoint>')
        }

        return ctx.gateway
          .rpc<RollbackDiffResponse>('rollback.diff', { hash, session_id: ctx.sid })
          .then(
            ctx.guarded<RollbackDiffResponse>(r => {
              const body = (r.rendered || r.diff || '').trim()

              if (!body && !r.stat) {
                return ctx.transcript.sys('no changes since this checkpoint')
              }

              const text = [r.stat || '', body].filter(Boolean).join('\n\n')
              ctx.transcript.page(text, 'Rollback diff')
            })
          )
          .catch(ctx.guardedErr)
      }

      const hash = first
      const filePath = rest.join(' ').trim()

      return ctx.gateway
        .rpc<RollbackRestoreResponse>('rollback.restore', {
          ...(filePath ? { file_path: filePath } : {}),
          hash,
          session_id: ctx.sid
        })
        .then(
          ctx.guarded<RollbackRestoreResponse>(r => {
            if (!r.success) {
              return ctx.transcript.sys(`rollback failed: ${r.error || r.message || 'unknown error'}`)
            }

            const target = filePath || 'workspace'
            const detail = r.reason || r.message || r.restored_to || 'restored'
            ctx.transcript.sys(`rollback restored ${target}: ${detail}`)

            if ((r.history_removed ?? 0) > 0) {
              ctx.transcript.setHistoryItems(prev => ctx.transcript.trimLastExchange(prev))
            }
          })
        )
        .catch(ctx.guardedErr)
    }
  },

  {
    aliases: ['tasks'],
    help: 'open the spawn-tree dashboard (live audit + kill/pause controls)',
    name: 'agents',
    run: (arg, ctx) => {
      const sub = arg.trim().toLowerCase()

      // Stay compatible with the gateway `/agents [pause|resume|status]` CLI —
      // explicit subcommands skip the overlay and act directly so scripts and
      // multi-step flows can drive it without entering interactive mode.
      if (sub === 'pause' || sub === 'resume' || sub === 'unpause') {
        const paused = sub === 'pause'
        ctx.gateway.gw
          .request<DelegationPauseResponse>('delegation.pause', { paused })
          .then(r => {
            applyDelegationStatus({ paused: r?.paused })
            ctx.transcript.sys(`delegation · ${r?.paused ? 'paused' : 'resumed'}`)
          })
          .catch(ctx.guardedErr)

        return
      }

      if (sub === 'status') {
        const d = getDelegationState()
        ctx.transcript.sys(
          `delegation · ${d.paused ? 'paused' : 'active'} · caps d${d.maxSpawnDepth ?? '?'}/${d.maxConcurrentChildren ?? '?'}`
        )

        return
      }

      patchOverlayState({ agents: true, agentsInitialHistoryIndex: 0 })
    }
  },

  {
    aliases: ['learning', 'memory-graph'],
    help: 'open your learning journey — skills + memories on a timeline',
    name: 'journey',
    run: (_arg, ctx) => {
      void ctx
      patchOverlayState({ journey: true })
    }
  },

  {
    help: 'replay a completed spawn tree · `/replay [N|last|list|load <path>]`',
    name: 'replay',
    run: (arg, ctx) => {
      const history = getSpawnHistory()
      const raw = arg.trim()
      const lower = raw.toLowerCase()

      // ── Disk-backed listing ─────────────────────────────────────
      if (lower === 'list' || lower === 'ls') {
        ctx.gateway
          .rpc<SpawnTreeListResponse>('spawn_tree.list', {
            limit: 30,
            session_id: ctx.sid ?? 'default'
          })
          .then(
            ctx.guarded<SpawnTreeListResponse>(r => {
              const entries = r.entries ?? []

              if (!entries.length) {
                return ctx.transcript.sys('no archived spawn trees on disk for this session')
              }

              const rows: [string, string][] = entries.map(e => {
                const ts = e.finished_at ? new Date(e.finished_at * 1000).toLocaleString() : '?'
                const label = e.label || `${e.count} subagents`

                return [`${ts} · ${e.count}×`, `${label}\n  ${e.path}`]
              })

              ctx.transcript.panel('Archived spawn trees', [{ rows }])
            })
          )
          .catch(ctx.guardedErr)

        return
      }

      // ── Disk-backed load by path ─────────────────────────────────
      if (lower.startsWith('load ')) {
        const path = raw.slice(5).trim()

        if (!path) {
          return ctx.transcript.sys('usage: /replay load <path>')
        }

        ctx.gateway
          .rpc<SpawnTreeLoadResponse>('spawn_tree.load', { path })
          .then(
            ctx.guarded<SpawnTreeLoadResponse>(r => {
              if (!r.subagents?.length) {
                return ctx.transcript.sys('snapshot empty or unreadable')
              }

              // Push onto the in-memory history so the overlay picks it up
              // by index 1 just like any other snapshot.
              pushDiskSnapshot(r, path)
              patchOverlayState({ agents: true, agentsInitialHistoryIndex: 1 })
            })
          )
          .catch(ctx.guardedErr)

        return
      }

      // ── In-memory nav (same-session) ─────────────────────────────
      if (!history.length) {
        return ctx.transcript.sys('no completed spawn trees this session · try /replay list')
      }

      let index = 1

      if (raw && lower !== 'last') {
        const parsed = parseInt(raw, 10)

        if (Number.isNaN(parsed) || parsed < 1 || parsed > history.length) {
          return ctx.transcript.sys(`replay: index out of range 1..${history.length} · use /replay list for disk`)
        }

        index = parsed
      }

      patchOverlayState({ agents: true, agentsInitialHistoryIndex: index })
    }
  },

  {
    help: 'diff two completed spawn trees · `/replay-diff <baseline> <candidate>` (indexes from /replay list or history N)',
    name: 'replay-diff',
    run: (arg, ctx) => {
      const parts = arg.trim().split(/\s+/).filter(Boolean)

      if (parts.length !== 2) {
        return ctx.transcript.sys('usage: /replay-diff <a> <b>  (e.g. /replay-diff 1 2 for last two)')
      }

      const [a, b] = parts
      const history = getSpawnHistory()

      const resolve = (token: string): null | SpawnSnapshot => {
        const n = parseInt(token!, 10)

        if (Number.isFinite(n) && n >= 1 && n <= history.length) {
          return history[n - 1] ?? null
        }

        return null
      }

      const baseline = resolve(a!)
      const candidate = resolve(b!)

      if (!baseline || !candidate) {
        return ctx.transcript.sys(`replay-diff: could not resolve indices · history has ${history.length} entries`)
      }

      setDiffPair({ baseline, candidate })
      patchOverlayState({ agents: true, agentsInitialHistoryIndex: 0 })
    }
  },

  {
    aliases: ['reload_skills'],
    help: 're-scan installed skills in the live TUI gateway',
    name: 'reload-skills',
    run: (_arg, ctx) => {
      ctx.gateway
        .rpc<SkillsReloadResponse>('skills.reload', {})
        .then(
          ctx.guarded<SkillsReloadResponse>(r => {
            ctx.transcript.page(r.output || 'skills reloaded', 'Reload Skills')
            ctx.gateway
              .rpc<CommandsCatalogResponse>('commands.catalog', {})
              .then(
                ctx.guarded<CommandsCatalogResponse>(catalog => {
                  if (!catalog?.pairs) {
                    return
                  }

                  ctx.local.setCatalog({
                    canon: (catalog.canon ?? {}) as Record<string, string>,
                    categories: catalog.categories ?? [],
                    pairs: catalog.pairs as [string, string][],
                    skillCount: (catalog.skill_count ?? 0) as number,
                    sub: (catalog.sub ?? {}) as Record<string, string[]>
                  })
                })
              )
              .catch(() => {})
          })
        )
        .catch(ctx.guardedErr)
    }
  },

  {
    help: 'browse, inspect, install skills',
    name: 'skills',
    run: (arg, ctx, cmd) => {
      const text = arg.trim()

      if (!text) {
        return patchOverlayState({ skillsHub: true })
      }

      const [sub, ...rest] = text.split(/\s+/)
      const query = rest.join(' ').trim()
      const { rpc } = ctx.gateway
      const { panel, sys } = ctx.transcript

      const runViaSlashWorker = () => {
        ctx.gateway.gw
          .request<SlashExecResponse>('slash.exec', { command: cmd.slice(1), session_id: ctx.sid })
          .then(r => {
            if (ctx.stale()) {
              return
            }

            const body = r?.output || '/skills: no output'
            const formatted = r?.warning ? `warning: ${r.warning}\n${body}` : body
            const long = formatted.length > 180 || formatted.split('\n').filter(Boolean).length > 2

            long ? ctx.transcript.page(formatted, 'Skills') : ctx.transcript.sys(formatted)
          })
          .catch(ctx.guardedErr)
      }

      if (sub === 'list') {
        rpc<SkillsListResponse>('skills.manage', { action: 'list' })
          .then(
            ctx.guarded<SkillsListResponse>(r => {
              const cats = Object.entries(r.skills ?? {}).sort()

              if (!cats.length) {
                return sys('no skills available')
              }

              panel(
                'Skills',
                cats.map<PanelSection>(([title, items]) => ({ items, title }))
              )
            })
          )
          .catch(ctx.guardedErr)

        return
      }

      if (sub === 'inspect') {
        if (!query) {
          return sys('usage: /skills inspect <name>')
        }

        rpc<SkillsInspectResponse>('skills.manage', { action: 'inspect', query })
          .then(
            ctx.guarded<SkillsInspectResponse>(r => {
              const info = r.info ?? {}

              if (!info.name) {
                return sys(`unknown skill: ${query}`)
              }

              const rows: [string, string][] = [
                ['Name', String(info.name)],
                ['Category', String(info.category ?? '')],
                ['Path', String(info.path ?? '')]
              ]

              const sections: PanelSection[] = [{ rows }]

              if (info.description) {
                sections.push({ text: String(info.description) })
              }

              panel('Skill', sections)
            })
          )
          .catch(ctx.guardedErr)

        return
      }

      if (sub === 'search') {
        if (!query) {
          return sys('usage: /skills search <query>')
        }

        rpc<SkillsSearchResponse>('skills.manage', { action: 'search', query })
          .then(
            ctx.guarded<SkillsSearchResponse>(r => {
              const results = r.results ?? []

              if (!results.length) {
                return sys(`no results for: ${query}`)
              }

              panel(`Search: ${query}`, [{ rows: results.map(s => [s.name, s.description ?? '']) }])
            })
          )
          .catch(ctx.guardedErr)

        return
      }

      if (sub === 'install') {
        if (!query) {
          return sys('usage: /skills install <name or url>')
        }

        sys(`installing ${query}…`)

        rpc<SkillsInstallResponse>('skills.manage', { action: 'install', query })
          .then(
            ctx.guarded<SkillsInstallResponse>(r =>
              sys(r.installed ? `installed ${r.name ?? query}` : 'install failed')
            )
          )
          .catch(ctx.guardedErr)

        return
      }

      if (sub === 'browse') {
        const pageNum = query ? parseInt(query, 10) : 1

        if (Number.isNaN(pageNum) || pageNum < 1) {
          return sys('usage: /skills browse [page]  (page must be a positive number)')
        }

        sys('fetching community skills (scans 6 sources, may take ~15s)…')

        rpc<SkillsBrowseResponse>('skills.manage', { action: 'browse', page: pageNum })
          .then(
            ctx.guarded<SkillsBrowseResponse>(r => {
              const items = r.items ?? []

              if (!items.length) {
                return sys(`no skills on page ${pageNum}${r.total ? ` (total ${r.total})` : ''}`)
              }

              const rows: [string, string][] = items.map(s => [
                s.trust ? `${s.name} · ${s.trust}` : s.name,
                String(s.description ?? '').slice(0, 160)
              ])

              const footer: string[] = []

              if (r.page && r.total_pages) {
                footer.push(`page ${r.page} of ${r.total_pages}`)
              }

              if (r.total) {
                footer.push(`${r.total} skills total`)
              }

              if (r.page && r.total_pages && r.page < r.total_pages) {
                footer.push(`/skills browse ${r.page + 1} for more`)
              }

              panel(`Browse Skills${pageNum > 1 ? ` — p${pageNum}` : ''}`, [
                { rows },
                ...(footer.length ? [{ text: footer.join(' · ') }] : [])
              ])
            })
          )
          .catch(ctx.guardedErr)

        return
      }

      runViaSlashWorker()
    }
  },

  {
    help: 'view & toggle plugins (no arg opens the hub; enable/disable <name> for direct toggle)',
    name: 'plugins',
    run: (arg, ctx, cmd) => {
      // No argument → open the interactive Plugins Hub overlay. Any
      // subcommand (enable/disable/list/install/…) falls through to the
      // text slash worker so it stays at parity with `hermes plugins`.
      if (!arg.trim()) {
        return patchOverlayState({ pluginsHub: true })
      }

      ctx.gateway.gw
        .request<SlashExecResponse>('slash.exec', { command: cmd.slice(1), session_id: ctx.sid })
        .then(r => {
          if (ctx.stale()) {
            return
          }

          const body = r?.output || '/plugins: no output'
          const text = r?.warning ? `warning: ${r.warning}\n${body}` : body
          const long = text.length > 180 || text.split('\n').filter(Boolean).length > 2

          long ? ctx.transcript.page(text, 'Plugins') : ctx.transcript.sys(text)
        })
        .catch(ctx.guardedErr)
    }
  },

  {
    help: 'enable or disable tools (client-side history reset on change)',
    name: 'tools',
    run: (arg, ctx, cmd) => {
      const [subcommand, ...names] = arg.trim().split(/\s+/).filter(Boolean)

      if (subcommand !== 'disable' && subcommand !== 'enable') {
        ctx.gateway.gw
          .request<SlashExecResponse>('slash.exec', { command: cmd.slice(1), session_id: ctx.sid })
          .then(r => {
            if (ctx.stale()) {
              return
            }

            const body = r?.output || '/tools: no output'
            const text = r?.warning ? `warning: ${r.warning}\n${body}` : body
            const long = text.length > 180 || text.split('\n').filter(Boolean).length > 2

            long ? ctx.transcript.page(text, 'Tools') : ctx.transcript.sys(text)
          })
          .catch(ctx.guardedErr)

        return
      }

      if (!names.length) {
        ctx.transcript.sys(`usage: /tools ${subcommand} <name> [name ...]`)
        ctx.transcript.sys(`built-in toolset: /tools ${subcommand} web`)
        ctx.transcript.sys(`MCP tool: /tools ${subcommand} github:create_issue`)

        return
      }

      ctx.gateway
        .rpc<ToolsConfigureResponse>('tools.configure', { action: subcommand, names, session_id: ctx.sid })
        .then(
          ctx.guarded<ToolsConfigureResponse>(r => {
            if (r.info) {
              ctx.session.setSessionStartedAt(Date.now())
              ctx.session.resetVisibleHistory(r.info)
            }

            if (r.changed?.length) {
              ctx.transcript.sys(`${subcommand === 'disable' ? 'disabled' : 'enabled'}: ${r.changed.join(', ')}`)
            }

            if (r.unknown?.length) {
              ctx.transcript.sys(`unknown toolsets: ${r.unknown.join(', ')}`)
            }

            if (r.missing_servers?.length) {
              ctx.transcript.sys(`missing MCP servers: ${r.missing_servers.join(', ')}`)
            }

            if (r.reset) {
              ctx.transcript.sys('session reset. new tool configuration is active.')
            }
          })
        )
        .catch(ctx.guardedErr)
    }
  }
]
