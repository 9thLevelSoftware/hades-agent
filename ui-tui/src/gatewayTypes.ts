import type { UsageModelData } from '@hermes/shared/billing'
import type { HermesSkin } from '@hermes/shared/skin'

import type { SessionInfo, SlashCategory, SubagentStatus, Usage } from './types.js'

/** The cross-surface skin contract (canonical shape in `@hermes/shared`).
 *  Includes the paired light_colors/dark_colors overlays from #20379. */
export type GatewaySkin = HermesSkin

export interface GatewayCompletionItem {
  display: string
  meta?: string
  text: string
}

export interface GatewayTranscriptMessage {
  context?: string
  name?: string
  role: 'assistant' | 'system' | 'tool' | 'user'
  text?: string
}

// ── Commands / completion ────────────────────────────────────────────

export interface CommandsCatalogResponse {
  canon?: Record<string, string>
  categories?: SlashCategory[]
  pairs?: [string, string][]
  skill_count?: number
  sub?: Record<string, string[]>
  warning?: string
}

export interface CompletionResponse {
  items?: GatewayCompletionItem[]
  replace_from?: number
}

export interface SlashExecResponse {
  output?: string
  warning?: string
}

// ── Autonomy (Preferences & Autonomy Center) ─────────────────────────
// Native `autonomy.exec` RPC: the bounded structured envelope over the
// shared `hades autonomy ...` parser/service. Mutating /autonomy verbs
// must use this route, never slash.exec.

export interface AutonomyRuleDoc {
  action_classes?: string[]
  confidence_ppm?: number
  data_classes?: string[]
  description?: string
  edit_command?: string
  effect: string
  expires_at_ms?: null | number
  max_uses?: null | number
  provenance?: string
  recipient_classes?: string[]
  remaining_uses?: null | number
  rule_id: string
  source: string
  state: string
}

export interface AutonomyDecisionDoc {
  authority_hash?: string
  authority_version?: number
  clarification?: null | { choices: string[]; code?: string; question: string }
  code: string
  conflicting_rule_ids?: string[]
  context_hash?: string
  edit_targets?: string[]
  expires_at_ms?: null | number
  matched_rule_ids?: string[]
  reason?: string
  required_evidence?: { kind: string; stage: string }[]
  stage?: string
  verdict: 'allow' | 'ask' | 'deny'
}

export interface AutonomyPreviewDoc {
  added_rule_ids?: string[]
  after_contract_hash: string
  applied: false
  before_contract_hash: string
  changed_rule_ids?: string[]
  profile_id?: string
  removed_rule_ids?: string[]
  warnings?: string[]
}

export interface AutonomyAppliedDoc {
  applied: true
  config_hash?: string
  contract_hash: string
  contract_version: number
}

export interface AutonomyContractDoc {
  hash?: string
  mode?: string
  profile_id?: string
  version?: number
}

export interface AutonomyExecResponse {
  action: string
  applied: AutonomyAppliedDoc | null
  approval_pending: boolean
  audit: Record<string, unknown>[]
  contract: AutonomyContractDoc | null
  decision: AutonomyDecisionDoc | null
  exit_code: number
  ok: boolean
  output: string
  preview: AutonomyPreviewDoc | null
  profile_home: string
  rules: AutonomyRuleDoc[]
  suggestions: AutonomyRuleDoc[]
}

// ── Verified outcome & artifact receipts (native /receipt) ───────────

export interface ReceiptSourceKeyDoc {
  source_id: string
  source_kind: string
}

export interface ReceiptRequestedOutcomeDoc {
  constraints: string[]
  content_hash: string
  description: string
  outcome_kind: string
  producer_id: string
}

export interface ReceiptClaimDoc {
  artifact_ids: string[]
  claim_id: string
  claim_kind: string
  content_hash: string
  evidence_ids: string[]
  expected_json: string
  observed_json: string
  required: boolean
  statement: string
  uncertainty: string[]
  verdict: string
}

export interface ReceiptEvidenceDoc {
  artifact_ids: string[]
  content_hash: string
  evidence_id: string
  evidence_kind: string
  fresh_until: null | string
  observed_at: string
  payload_hash: string
  producer_id: string
  source_ref: string
  summary: string
}

export interface ReceiptArtifactDoc {
  artifact_id: string
  captured_at: string
  content_hash: string
  display_name: string
  media_type: null | string
  mtime_ns: null | number
  sha256: string
  size_bytes: number
  source_kind: string
  source_ref: string
}

export interface ReceiptSummary {
  content_hash: string
  decided_at: string
  receipt_id: string
  scorer_id: string
  scorer_version: string
  session_id: null | string
  source: ReceiptSourceKeyDoc
  status: string
  subject_id: string
  subject_kind: string
}

export interface ReceiptDetail {
  artifacts: ReceiptArtifactDoc[]
  claims: ReceiptClaimDoc[]
  content_hash: string
  decided_at: string
  evidence: ReceiptEvidenceDoc[]
  mission_id: null | string
  receipt_id: string
  requested_outcome: ReceiptRequestedOutcomeDoc
  scorer_id: string
  scorer_version: string
  session_id: null | string
  source: ReceiptSourceKeyDoc
  status: string
  subject_id: string
  subject_kind: string
  transaction_id: null | string
  turn_id: null | string
  uncertainty: string[]
}

export interface ReceiptObservationDetail {
  artifacts: ReceiptArtifactDoc[]
  claims: ReceiptClaimDoc[]
  content_hash: string
  evidence: ReceiptEvidenceDoc[]
  observation_id: string
  observed_at: string
  previous_observation_id: null | string
  receipt_id: string
  scorer_id: string
  scorer_version: string
  status: string
  uncertainty: string[]
}

/** Every claim→evidence→artifact edge as one structured record. */
export interface ReceiptClaimEdge {
  artifact_ids: string[]
  claim_id: string
  claim_kind: string
  evidence_ids: string[]
  required: boolean
  statement: string
  uncertainty: string[]
  verdict: string
}

export interface ReceiptExecResponse {
  action: string
  claim_edges?: ReceiptClaimEdge[]
  exit_code: number
  export_path?: string
  observations?: ReceiptObservationDetail[]
  ok: boolean
  /** Shared truthful text rendering from hades_cli.receipts (one renderer). */
  output: string
  profile_home: string
  receipt?: ReceiptDetail
  receipts?: ReceiptSummary[]
  retention_plan_hash?: string
  warning?: string
}

// ── Remote Spending (Phase 2b) ───────────────────────────────────────

// Wire shapes now live in @hermes/shared for reuse by TypeScript clients.
export type {
  BillingAutoReload,
  BillingCardInfo,
  BillingChargeResponse,
  BillingChargeStatusResponse,
  BillingErrorPayload,
  BillingMonthlyCap,
  BillingMutationResponse,
  BillingStateResponse,
  SubscriptionPreviewResponse,
  SubscriptionStateResponse,
  SubscriptionTierOption,
  SubscriptionUpgradeResponse,
  UsageBarData,
  UsageModelData
} from '@hermes/shared/billing'

export type CommandDispatchResponse =
  | { output?: string; type: 'exec' | 'plugin' }
  | { target: string; type: 'alias' }
  | { message?: string; name: string; type: 'skill' }
  | { message: string; notice?: string; type: 'send' }
  | { message: string; notice?: string; type: 'prefill' }

// ── Config ───────────────────────────────────────────────────────────

export interface ConfigDisplayConfig {
  battery?: boolean
  bell_on_complete?: boolean
  busy_input_mode?: string
  details_mode?: string
  inline_diffs?: boolean
  mouse_tracking?: boolean | null | number | string
  sections?: Record<string, string>
  show_cost?: boolean
  show_reasoning?: boolean
  streaming?: boolean
  thinking_mode?: string
  /**
   * Nudge the user toward the /agents spawn-tree dashboard the first time a
   * turn starts delegating, via a one-time transient activity hint.  Opens
   * nothing — just advertises the command.  Default true.
   */
  tui_agents_nudge?: boolean
  tui_auto_resume_recent?: boolean
  tui_compact?: boolean
  /** Legacy alias for display.mouse_tracking. */
  tui_mouse?: boolean | null | number | string
  // Forward-compat: backend may send styles this client doesn't know yet —
  // `normalizeIndicatorStyle` falls back to 'kaomoji' for those — but the
  // wire type is documented as `string` so consumers don't get a false
  // narrowing-and-autocomplete contract on a value that requires runtime
  // validation anyway.
  tui_status_indicator?: string
  tui_statusbar?: 'bottom' | 'off' | 'on' | 'top' | boolean
  /** Theme mode pin: 'light' / 'dark' beat background auto-detection; 'auto'
   *  (default) trusts the OSC-11 probe + env signals. */
  tui_theme?: string
}

export interface ConfigVoiceConfig {
  // Raw `yaml.safe_load()` value from config; may be non-string if hand-edited.
  // Callers must normalize/validate at runtime (parseVoiceRecordKey()).
  record_key?: unknown
}

export interface ConfigFullResponse {
  config?: {
    display?: ConfigDisplayConfig
    voice?: ConfigVoiceConfig
    paste_collapse_threshold?: number
    paste_collapse_char_threshold?: number
  }
}

export interface ConfigMtimeResponse {
  /** Revision hash of MCP-relevant config sections; reload MCP only when it
   *  changes (cosmetic writes like /skin must not trigger reconnects). */
  mcp_rev?: string
  mtime?: number
}

export interface ConfigGetValueResponse {
  display?: string
  home?: string
  value?: string
}

export interface ConfigSetResponse {
  confirm_message?: string
  confirm_required?: boolean
  credential_warning?: string
  history_reset?: boolean
  info?: SessionInfo
  value?: string
  warning?: string
}

export interface SetupStatusResponse {
  provider_configured?: boolean
}

export interface SystemBatteryResponse {
  available?: boolean
  category?: string
  percent?: null | number
  plugged?: null | boolean
}

// ── Session lifecycle ────────────────────────────────────────────────

export interface SessionCreateResponse {
  info?: SessionInfo & { config_warning?: string; credential_warning?: string }
  session_id: string
}

export interface SessionResumeResponse {
  inflight?: null | SessionInflightTurn
  info?: SessionInfo
  message_count?: number
  messages: GatewayTranscriptMessage[]
  resumed?: string
  running?: boolean
  session_id: string
  started_at?: number
  status?: LiveSessionStatus
}

export type LiveSessionStatus = 'idle' | 'starting' | 'waiting' | 'working'

export interface SessionActiveItem {
  current?: boolean
  id: string
  last_active?: number
  message_count?: number
  model?: string
  preview?: string
  session_key?: string
  started_at?: number
  status: LiveSessionStatus
  title?: string
}

export interface SessionActiveListResponse {
  sessions?: SessionActiveItem[]
}

export interface SessionInflightTurn {
  assistant?: string
  streaming?: boolean
  user?: string
}

export interface SessionActivateResponse {
  inflight?: null | SessionInflightTurn
  info?: SessionInfo
  message_count?: number
  messages: GatewayTranscriptMessage[]
  running?: boolean
  session_id: string
  session_key?: string
  started_at?: number
  status?: LiveSessionStatus
}

export interface SessionListItem {
  id: string
  message_count: number
  preview: string
  source?: string
  started_at: number
  title: string
}

export interface SessionListResponse {
  sessions?: SessionListItem[]
}

export interface SessionDeleteResponse {
  deleted: string
}

export interface SessionMostRecentResponse {
  session_id?: null | string
  source?: string
  started_at?: number
  title?: string
}

export interface SessionTitleResponse {
  pending?: boolean
  session_key?: string
  title?: string
}

export interface SessionSaveResponse {
  file?: string
}

export interface SessionUndoResponse {
  removed?: number
}

export interface SessionUsageResponse {
  active_subagents?: number
  cache_read?: number
  cache_write?: number
  calls?: number
  compressions?: number
  context_max?: number
  context_percent?: number
  context_used?: number
  cost_status?: 'estimated' | 'exact'
  cost_usd?: number
  credits_lines?: string[]
  input?: number
  model?: string
  output?: number
  total?: number
  // Shared dollar usage model (two-bar view) so /usage renders the same bars
  // as /subscription. Dollars only — never "credits".
  usage?: UsageModelData
}

export interface SessionStatusResponse {
  output?: string
}

export interface SessionCompressResponse {
  after_messages?: number
  after_tokens?: number
  before_messages?: number
  before_tokens?: number
  info?: SessionInfo
  messages?: GatewayTranscriptMessage[]
  removed?: number
  summary?: {
    headline?: string
    noop?: boolean
    note?: null | string
    token_line?: string
  }
  usage?: Usage
}

export interface SessionBranchResponse {
  session_id?: string
  title?: string
}

export interface SessionCloseResponse {
  closed?: boolean
  ok?: boolean
}

export interface SessionInterruptResponse {
  ok?: boolean
}

export interface SessionSteerResponse {
  status?: 'queued' | 'rejected'
  text?: string
}

// ── Prompt / submission ──────────────────────────────────────────────

export interface PromptSubmitResponse {
  ok?: boolean
}

export interface BackgroundStartResponse {
  task_id?: string
}

export interface ClarifyRespondResponse {
  ok?: boolean
}

export interface ApprovalRespondResponse {
  ok?: boolean
}

export interface SudoRespondResponse {
  ok?: boolean
}

export interface SecretRespondResponse {
  ok?: boolean
}

// ── Shell / clipboard / input ────────────────────────────────────────

export interface ShellExecResponse {
  code: number
  stderr?: string
  stdout?: string
}

export interface ClipboardPasteResponse {
  attached?: boolean
  count?: number
  height?: number
  message?: string
  token_estimate?: number
  width?: number
}

export interface InputDetectDropResponse {
  height?: number
  is_image?: boolean
  matched?: boolean
  name?: string
  text?: string
  token_estimate?: number
  width?: number
}

export interface TerminalResizeResponse {
  ok?: boolean
}

// ── Image attach ─────────────────────────────────────────────────────

export interface ImageAttachResponse {
  height?: number
  name?: string
  remainder?: string
  token_estimate?: number
  width?: number
}

// ── Voice ────────────────────────────────────────────────────────────

export interface VoiceToggleResponse {
  audio_available?: boolean
  available?: boolean
  details?: string
  enabled?: boolean
  record_key?: string
  stt_available?: boolean
  tts?: boolean
}

export interface VoiceRecordResponse {
  status?: 'busy' | 'recording' | 'stopped'
  text?: string
}

// ── Tools (TS keeps configure since it resets local history) ─────────

export interface ToolsConfigureResponse {
  changed?: string[]
  enabled_toolsets?: string[]
  info?: SessionInfo
  missing_servers?: string[]
  reset?: boolean
  unknown?: string[]
}

// ── Model picker ─────────────────────────────────────────────────────

export interface ModelOptionProvider {
  auth_type?: string
  authenticated?: boolean
  is_current?: boolean
  key_env?: string
  models?: string[]
  name: string
  slug: string
  total_models?: number
  warning?: string
}

export interface ModelOptionsResponse {
  model?: string
  provider?: string
  providers?: ModelOptionProvider[]
}

// ── MCP ──────────────────────────────────────────────────────────────

export interface ReloadMcpResponse {
  status?: string
  message?: string
  /** The mcp_rev the server actually loaded (re-hashed after discovery).
   *  The client records THIS as its accepted revision, not the one it
   *  requested — a reload that raced a config edit reports the newer rev. */
  loaded_rev?: string
}

export interface ReloadEnvResponse {
  updated?: number
}

export interface ProcessStopResponse {
  killed?: number
}

export interface BrowserManageResponse {
  connected?: boolean
  messages?: string[]
  url?: string
}

export interface RollbackCheckpoint {
  hash: string
  message?: string
  timestamp?: string
}

export interface RollbackListResponse {
  checkpoints?: RollbackCheckpoint[]
  enabled?: boolean
}

export interface RollbackDiffResponse {
  diff?: string
  rendered?: string
  stat?: string
}

export interface RollbackRestoreResponse {
  error?: string
  history_removed?: number
  message?: string
  reason?: string
  restored_to?: string
  success?: boolean
}

// ── Subagent events ──────────────────────────────────────────────────

export interface SubagentEventPayload {
  api_calls?: number
  cost_usd?: number
  depth?: number
  duration_seconds?: number
  files_read?: string[]
  files_written?: string[]
  goal: string
  input_tokens?: number
  iteration?: number
  model?: string
  output_tail?: { is_error?: boolean; preview?: string; tool?: string }[]
  output_tokens?: number
  parent_id?: null | string
  reasoning_tokens?: number
  status?: SubagentStatus
  subagent_id?: string
  summary?: string
  task_count?: number
  task_index: number
  text?: string
  tool_count?: number
  tool_name?: string
  tool_preview?: string
  toolsets?: string[]
}

// ── Delegation control RPCs ──────────────────────────────────────────

export interface DelegationStatusResponse {
  active?: {
    depth?: number
    goal?: string
    model?: null | string
    parent_id?: null | string
    started_at?: number
    status?: string
    subagent_id?: string
    tool_count?: number
  }[]
  max_concurrent_children?: number
  max_spawn_depth?: number
  paused?: boolean
}

export interface DelegationPauseResponse {
  paused?: boolean
}

export interface SubagentInterruptResponse {
  found?: boolean
  subagent_id?: string
}

// ── Spawn-tree snapshots ─────────────────────────────────────────────

export interface SpawnTreeListEntry {
  count: number
  finished_at?: number
  label?: string
  path: string
  session_id?: string
  started_at?: number | null
}

export interface SpawnTreeListResponse {
  entries?: SpawnTreeListEntry[]
}

export interface SpawnTreeLoadResponse {
  finished_at?: number
  label?: string
  session_id?: string
  started_at?: null | number
  subagents?: unknown[]
}

export type GatewayEvent =
  | { payload?: { skin?: GatewaySkin }; session_id?: string; type: 'gateway.ready' }
  | { payload?: GatewaySkin; session_id?: string; type: 'skin.changed' }
  | { payload: SessionInfo; session_id?: string; type: 'session.info' }
  | { payload?: { text?: string }; session_id?: string; type: 'thinking.delta' }
  | { payload?: { kind?: string }; session_id?: string; type: 'reaction' }
  | { payload?: undefined; session_id?: string; type: 'message.start' }
  | { payload?: { kind?: string; text?: string }; session_id?: string; type: 'status.update' }
  | {
      payload?: {
        id?: string
        key?: string
        kind?: 'sticky' | 'ttl'
        level?: 'error' | 'info' | 'success' | 'warn'
        text?: string
        ttl_ms?: null | number
      }
      session_id?: string
      type: 'notification.show'
    }
  | { payload?: { key?: string }; session_id?: string; type: 'notification.clear' }
  | {
      payload: { user_code?: string; verification_url: string }
      session_id?: string
      type: 'billing.step_up.verification'
    }
  | { payload?: { state?: 'idle' | 'listening' | 'transcribing' }; session_id?: string; type: 'voice.status' }
  | { payload?: { no_speech_limit?: boolean; text?: string }; session_id?: string; type: 'voice.transcript' }
  | { payload?: { reason?: string }; session_id?: string; type: 'dashboard.new_session_requested' }
  | { payload: { line: string }; session_id?: string; type: 'gateway.stderr' }
  | {
      payload?: { level?: 'info' | 'warn' | 'error'; message?: string }
      session_id?: string
      type: 'browser.progress'
    }
  | {
      payload?: { cwd?: string; python?: string; stderr_tail?: string }
      session_id?: string
      type: 'gateway.start_timeout'
    }
  | { payload?: { preview?: string }; session_id?: string; type: 'gateway.protocol_error' }
  | {
      payload?: { text?: string; verbose?: boolean }
      session_id?: string
      type: 'reasoning.delta' | 'reasoning.available'
    }
  | {
      payload: { count?: number; index?: number; label?: string; text?: string }
      session_id?: string
      type: 'moa.reference'
    }
  | { payload?: { aggregator?: string }; session_id?: string; type: 'moa.aggregating' }
  | { payload: { name?: string; preview?: string }; session_id?: string; type: 'tool.progress' }
  | { payload: { name?: string }; session_id?: string; type: 'tool.generating' }
  | {
      payload: { args_text?: string; context?: string; name?: string; tool_id: string; todos?: unknown[] }
      session_id?: string
      type: 'tool.start'
    }
  | {
      payload: {
        duration_s?: number
        error?: string
        inline_diff?: string
        name?: string
        result_text?: string
        summary?: string
        tool_id: string
        todos?: unknown[]
      }
      session_id?: string
      type: 'tool.complete'
    }
  | {
      payload: { choices: string[] | null; question: string; request_id: string }
      session_id?: string
      type: 'clarify.request'
    }
  | {
      payload: {
        allow_permanent?: boolean
        choices?: string[]
        command: string
        description: string
        smart_denied?: boolean
      }
      session_id?: string
      type: 'approval.request'
    }
  | { payload: { request_id: string }; session_id?: string; type: 'sudo.request' }
  | { payload: { env_var: string; prompt: string; request_id: string }; session_id?: string; type: 'secret.request' }
  | { payload: { request_id: string }; session_id?: string; type: 'secret.expire' | 'sudo.expire' }
  | { payload: { task_id: string; text: string }; session_id?: string; type: 'background.complete' }
  | { payload?: { text?: string }; session_id?: string; type: 'review.summary' }
  | { payload: SubagentEventPayload; session_id?: string; type: 'subagent.spawn_requested' }
  | { payload: SubagentEventPayload; session_id?: string; type: 'subagent.start' }
  | { payload: SubagentEventPayload; session_id?: string; type: 'subagent.thinking' }
  | { payload: SubagentEventPayload; session_id?: string; type: 'subagent.tool' }
  | { payload: SubagentEventPayload; session_id?: string; type: 'subagent.progress' }
  | { payload: SubagentEventPayload; session_id?: string; type: 'subagent.complete' }
  | { payload: { rendered?: string; text?: string }; session_id?: string; type: 'message.delta' }
  | { payload?: { text?: string }; session_id?: string; type: 'message.interim' }
  | {
      payload: { already_streamed?: boolean; text: string }
      session_id?: string
      type: 'message.interim'
    }
  | {
      payload?: { reasoning?: string; rendered?: string; response_previewed?: boolean; text?: string; usage?: Usage }
      session_id?: string
      type: 'message.complete'
    }
  | { payload?: { message?: string }; session_id?: string; type: 'error' }
