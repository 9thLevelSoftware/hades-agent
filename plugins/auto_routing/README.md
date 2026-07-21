# Auto Routing Plugin

Auto Routing is an opt-in, profile-local static router for choosing among
Hermes runtimes that are already executable. Setup and edit remain
**shadow-only**. A separate doctor-gated activation command can enable routing
for new sessions and delegated children after the exact provider, model,
access path, reasoning, and fallback contracts have been validated.

Subscription permission is persisted in the immutable policy envelope, so a
prohibition remains enforced across setup, restart, and doctor validation.

## Enable it

Enable the plugin separately in every profile that should use it:

```text
hermes plugins enable auto-routing
```

For an advisor-guided setup or edit, explicitly load the namespaced skill:

```text
auto-routing:auto-routing
```

Enabling the plugin alone does not change any provider or model. Shadow mode
records hypothetical decisions without changing constructor inputs. Active
mode affects only a fresh construction boundary; a live or resumed session
keeps its durable route and is never reclassified.

## Commands

```text
hermes auto-routing setup --proposal FILE [--apply --expected-config-sha SHA256] [--json]
hermes auto-routing edit --proposal FILE [--apply --expected-config-sha SHA256] [--json]
hermes auto-routing inventory [--refresh] [--include-ineligible] [--json]
hermes auto-routing verify-runtime RUNTIME_STABLE_ID [--apply --expect-hash HASH --ack-billable] [--json]
hermes auto-routing refresh-catalog [--models-dev] [--hermes] [--file FILE] [--json]
hermes auto-routing plan --request FILE [--prompt-file FILE ...] [--json]
hermes auto-routing validate [--proposal FILE] [--json]
hermes auto-routing activate [--mode shadow|active] [--apply --expected-config-sha SHA256] [--json]
hermes auto-routing explain (--decision-id ID | --session-id ID | --operation-id ID --task-index N) [--detailed] [--json]
hermes auto-routing feedback --evidence-id EVIDENCE_ID --value rating-1|rating-2|rating-3|rating-4|rating-5|rejected|corrected|manual-reroute [--json]
hermes auto-routing report [--days 1..3650] [--decision-id ID] [--profile-id ID] [--runtime-id SHA256] [--reasoning-effort EFFORT] [--json]
hermes auto-routing adapt status --profile-id ID [--json]
hermes auto-routing adapt history --profile-id ID [--json]
hermes auto-routing adapt freeze --profile-id ID [--apply --expect-hash SHA256] [--json]
hermes auto-routing adapt unfreeze --profile-id ID [--apply --expect-hash SHA256] [--json]
hermes auto-routing adapt rollback --profile-id ID --revision ID [--apply --expect-hash SHA256] [--json]
hermes auto-routing manage inventory [--json]
hermes auto-routing manage ranking [--json]
hermes auto-routing manage status [--json]
hermes auto-routing manage history [--profile-id ID] [--json]
hermes auto-routing manage reconcile [--apply --expect-hash SHA256] [--json]
hermes auto-routing manage enable|disable|freeze|unfreeze [--apply --expect-hash SHA256] [--json]
hermes auto-routing manage recover --receipt-id ID [--apply --expect-hash SHA256] [--json]
hermes auto-routing manage schedule --schedule CRON [--apply --expect-hash SHA256] [--json]
hermes auto-routing status [--json]
hermes auto-routing doctor [--json]
```

Every command publishes a write class in help and JSON:

- `read_only` for planning, validation, explain, report, status, adaptation
  status/history, doctor, and inventory reads;
- `append_only_observation` for feedback and explicit inventory/catalog
  refreshes; and
- `guarded_control_plane` for setup/edit, activation, adaptation
  freeze/unfreeze/rollback, every management mutation/recovery, and billable
  verification.

Setup and edit always preview first. Apply requires the exact preview hash and
writes the YAML authority plus its complete baseline revision through a
recoverable journaled saga. `plan` never emits a pre-approved apply command.

`explain` reads one immutable decision by decision ID, fresh-session ID, or
delegation operation ID plus task index. Its default JSON is concise;
`--detailed` emits every persisted decision fact and candidate evaluation.
Both forms are content-free and never include the raw task, provider response,
credentials, or endpoint material.

## Stage 3 evidence workflow

Stage 3 collects local evidence for routed work; it is descriptive and makes
no adaptive writes. Use it in this order:

1. Use `explain` to obtain the content-free evidence IDs for a completed routed
   turn.
2. Record feedback only when the user explicitly supplies one finite value.
   Never infer feedback from silence, tone, follow-up, or an assistant's
   self-evaluation. Feedback is append-only, so contradictory observations
   remain visible instead of overwriting history.
3. Use the read-only `report` for descriptive observed-event counts, value
   histograms, feedback, cost/token totals, first-task/continuation separation,
   and warnings. It intentionally provides no decision-population denominator
   or attribution-coverage percentage.
4. Treat report group order as deterministic presentation, not as rankings,
   and do not recommend changing a route from Stage 3 data.

Only turns credited to an exact active route binding and its current recorded
epoch are included. Shadow, inherit, manual, off, and otherwise unrouted work
is excluded. A continuation never borrows the first task's derived context:
continuation context is unavailable rather than reclassified. Latency
availability is nullable, so missing latency remains missing.

Only `verified` is objective positive quality evidence. The outcomes
`completed_unverified`, `partial`, `blocked`, `failed`, `interrupted`,
`unresolved`, and `cancelled` are all quality-unknown; they are not negative
scores. Finite explicit feedback is reported separately from turn outcomes.

Stage 3 reports remain descriptive even when Stage 4 adaptation is enabled.
The static selector never reads evidence or learner state; a separate local
lifecycle service may consume only the allowed, exactly attributed evidence
described below before a future new-decision boundary.
The Stage 3 report surface still excludes MoA, judges, canaries, rankings,
recommendations, evaluators, optimizers, autonomous route mutation, and
outbound telemetry.

## Conservative profile adaptation

Stage 4 adaptation is **opt-in per profile**. Profiles created before this
feature and profiles with `enabled: false` stay static. An enabled profile must
declare one or more `primary_challengers`; the adaptive overlay may only
reorder those exact approved primary choices and choose reasoning defaults
inside their existing bounds. A representative profile fragment is:

```yaml
profiles:
  coding:
    primary:
      runtime:
        provider: configured-provider
        model: configured-primary-model
        auth_identity: configured-auth
        credential_pool_identity: configured-pool
        endpoint_identity: configured-endpoint
        api_mode: chat_completions
        local_backend: ""
        inventory_revision: inventory-1
      reasoning: {default: medium, min: low, max: high}
      supported_reasoning_efforts: [low, medium, high]
      revision_status: active
    primary_challengers:
      - runtime:
          provider: configured-provider
          model: configured-challenger-model
          auth_identity: configured-auth
          credential_pool_identity: configured-pool
          endpoint_identity: configured-endpoint
          api_mode: chat_completions
          local_backend: ""
          inventory_revision: inventory-1
        reasoning: {default: medium, min: low, max: high}
        supported_reasoning_efforts: [low, medium, high]
        revision_status: challenger
    fallbacks: []
    adaptation:
      enabled: true
      canary_fraction: 0.05
      minimum_comparable_samples: 20
      observed_regression_threshold: 0.10
      cooldown_base_seconds: 3600
      cooldown_max_seconds: 86400
      confidence_level: 0.90
```

The defaults are a deterministic 5% canary, 20 comparable samples, a 10%
observed-regression threshold, one-hour initial cooldown capped at one day,
and 90% confidence. Quality learning accepts only verified outcomes and
explicit feedback (`rating-1` through `rating-5`, `rejected`, or `corrected`)
that is exactly attributed to an initial routed task in the same profile and
content-free context bucket. `manual-reroute` excludes an observation from
automatic quality scoring. Silence, latency, cost, retries, provider failures,
and every quality-unknown outcome are never inferred as negative quality.

`adapt status` and `adapt history` are read-only. The three mutation commands
are preview-first. A preview returns a SHA-256 hash bound to the authority,
profile, active revision, complete profile generation and lifecycle state,
requested action, and every action argument. Rollback also binds the target
revision and its checksum. Apply requires the unchanged hash:

```text
hermes auto-routing adapt freeze --profile-id coding --json
hermes auto-routing adapt freeze --profile-id coding --apply --expect-hash SHA256 --json
hermes auto-routing adapt unfreeze --profile-id coding --json
hermes auto-routing adapt rollback --profile-id coding --revision REVISION_ID --json
```

Freeze stops new proposals, canary assignments, and automatic publication. It
does not stop routing, evidence collection, feedback, or reporting. An
operator rollback requires the profile to be frozen and restores the exact
same-profile, same-authority complete revision named in the preview. History
is immutable, and a stale generation or changed argument fails closed; repeat
the preview instead of reusing its hash. Crash recovery and canary reuse are
deterministic from persisted profile-local state.

Stage 4 explicitly excludes provider discovery, model discovery or download,
candidate/fallback mutation, policy mutation, profile split/merge or topology
mutation, classifier or evaluator learning, judges, outbound telemetry,
model-visible tools, and MoA. Manual provider/model/reasoning intent still
wins, existing decisions replay their recorded complete snapshot, and the
user-owned fallback bytes remain unchanged through promotion, rejection, and
rollback.

## Autonomous profile management

Stage 5 is a separate, **global opt-in** for conservative management of the
profiles that already exist in the active profile's Auto Routing authority. It
is disabled by default and does not enable Stage 4 adaptation. Add a fragment
like this under the Auto Routing plugin authority, using only the active
profile's local pack path and your real trusted public key:

```yaml
autonomous_profile_management:
  enabled: true
  ranking_pack:
    ranking_pack_path: auto-routing/ranking-packs/current.json
    trusted_ed25519_public_keys:
      - BASE64_ED25519_PUBLIC_KEY
  daily_change_limit: 1
  schedule: "17 */6 * * *"
```

The pack is a JSON envelope copied into the configured path by the user. Hermes
does not fetch or refresh it. The path must remain below the active profile's
`auto-routing/ranking-packs/` directory. Symlinks, junctions, traversal,
non-regular files, and path or handle races fail closed. The complete envelope
has these fields and no extras:

```json
{
  "schema_version": 1,
  "pack_id": "PACK_ID",
  "issued_at": "2026-01-01T00:00:00Z",
  "expires_at": "2026-02-01T00:00:00Z",
  "key_id": "SHA256_KEY_ID",
  "rankings": {
    "RUNTIME_STABLE_ID": {
      "quality": 0.0,
      "reliability": 0.0,
      "latency": 0.0,
      "cost": 0.0
    }
  },
  "signature": "BASE64_ED25519_SIGNATURE"
}
```

`signature` is an Ed25519 signature over deterministic canonical JSON for all
other fields. `key_id` is the SHA-256 identity of the signing public key. Every
ranking key is an exact stable runtime ID from persisted inventory. The four
metrics are finite normalized values in `[0, 1]`: higher quality and reliability
are better, while lower latency and cost are better. Hermes combines them only
with the destination profile's existing normalized objective weights; it does
not infer or update the metrics.

Management considers only the newest unambiguous persisted inventory snapshot.
A candidate must already be verified through a configured Hermes access path or
be an installed compatible local open model, support the profile's requirements,
and have a signed row in the current pack. Missing, expired, future-dated,
malformed, untrusted, tampered, escaped, or unreadable packs produce a
content-free `reason_code` hold and no profile write. Stale or ambiguous
inventory, a manual authority change, a freeze, an in-progress canary, a lease
conflict, or the per-profile UTC daily cap likewise holds without consuming a
change admission.

This feature never downloads or enables a runtime, never enables a provider,
never makes a paid verification request, and never uses MoA, evaluators,
classifiers, judges, outbound telemetry, or open-web ranking refreshes. It never
creates, deletes, merges, or splits profiles and never changes existing Stage 4
adaptation controls. Existing decisions, live conversations, resumes,
delegations, and recorded fallback chains keep their immutable snapshots.

### Guarded control and scheduling

Read persisted eligibility and pack status before enabling management:

```text
hermes auto-routing manage inventory --json
hermes auto-routing manage ranking --json
```

Every user-triggered mutation is preview-first. The preview hash binds the
current Auto Routing and management authorities, control generation, schedule,
pack path and verified fingerprint, daily cap, cron identity, and—for manual
reconciliation—the exact persisted inventory, ranking, revision, and profile
state. Apply only the unchanged preview the user approved:

```text
hermes auto-routing manage ranking --json
hermes auto-routing manage enable --json
hermes auto-routing manage enable --apply --expect-hash SHA256 --json
hermes auto-routing manage status --json
hermes auto-routing manage freeze --json
hermes auto-routing manage freeze --apply --expect-hash SHA256 --json
hermes auto-routing manage history --profile-id coding --json
```

Use the same preview/approval/apply pattern for `disable`, `unfreeze`,
`reconcile`, exact receipt recovery, schedule changes, whole-set ranking trust
replacement, and the per-profile daily cap. Ranking-trust previews expose only
the verified pack and trusted-key-set fingerprints plus key count; raw public
keys, signatures, and ranking rows are never returned. For example:

```text
hermes auto-routing manage ranking-trust --ranking-pack-path auto-routing/ranking-packs/current.json --trusted-ed25519-public-key BASE64_KEY --json
hermes auto-routing manage ranking-trust --ranking-pack-path auto-routing/ranking-packs/current.json --trusted-ed25519-public-key BASE64_KEY --apply --expect-hash SHA256 --json
hermes auto-routing manage daily-cap --limit 2 --json
hermes auto-routing manage daily-cap --limit 2 --apply --expect-hash SHA256 --json
hermes auto-routing manage schedule --schedule "17 */6 * * *" --json
hermes auto-routing manage schedule --schedule "17 */6 * * *" --apply --expect-hash SHA256 --json
hermes auto-routing manage reconcile --json
hermes auto-routing manage reconcile --apply --expect-hash SHA256 --json
hermes auto-routing manage recover --receipt-id RECEIPT_ID --json
hermes auto-routing manage recover --receipt-id RECEIPT_ID --apply --expect-hash SHA256 --json
```

Trust replacement validates the proposed local pack against the complete
proposed key set before any config write. The daily cap accepts only integers
from 1 through 10; lowering it never erases admissions already recorded for the
current UTC day. Neither operation replaces the existing management cron job.
When Auto Routing is active, every config-changing management control derives
a new immutable activation receipt from the exact active predecessor so fresh
routing remains active; a failed apply restores the prior config and removes
only rollover records created by that attempt.

Enabling management installs one profile-local, no-agent cron job. It runs only
the local scheduled reconciliation command; it does not start an agent or make
an LLM call. Updating the schedule adopts or replaces only the recorded managed
job. Disabling removes that exact job. Freeze leaves the schedule installed but
makes reconciliation return `management_frozen` without a profile change.

### Holds and recovery

Use `manage status` for global control, per-profile phase, remaining daily
changes, schedule, and cron identity. Use `manage history` for immutable
revision, lifecycle-event, and receipt metadata. Explain the exact returned
`reason_code`; do not translate a hold into a model recommendation. Correct the
local inventory, pack, configuration, cooldown, or operator control named by
that code, then obtain a new preview because an old hash is no longer valid.

For rollback or recovery, use this fail-closed sequence:

1. Preview and explicitly apply `manage freeze`.
2. Read `manage status` and `manage history`; identify the exact revision and
   receipt phase (`prepared`, `config_replaced`, `committed`, or
   `recovery_required`) without opening raw prompts, credentials, or endpoints.
3. For one exact incomplete receipt, preview
   `manage recover --receipt-id RECEIPT_ID --json`. Verify that it binds the
   frozen control generation, complete receipt identity and phase, current
   config checksum, and checksum-matched receipt backup. Obtain explicit
   approval, then apply only that unchanged hash:
   `manage recover --receipt-id RECEIPT_ID --apply --expect-hash SHA256 --json`.
   This can restore only the receipt's exact pre-change config bytes. If preview
   or apply cannot prove the receipt, authority, current config, or backup, keep
   management frozen and have an operator repair that exact receipt and backup.
   Never hand-edit SQLite, substitute another backup, or force a new
   reconciliation over unresolved recovery state.
   Before touching bytes from the failed resulting authority, recovery records
   a content-free, receipt-bound `config_restore_started` event. If the process
   stops after the exact bytes are restored but before lifecycle finalization,
   retry the same receipt through a new preview: that durable marker lets the
   retry finish the same deterministic recovery revision and profile state. A
   marker is never inferred merely because the file already contains the
   preceding bytes.
4. Re-run status and history. Confirm receipt-bound `recovered` lifecycle
   evidence exists for every affected profile, then preview any required
   reconcile or control repair and obtain explicit approval before applying its
   new hash. If a profile remains in `recovery_required` because its exact prior
   experiment state cannot be proven, keep management frozen for operator
   repair even though the config bytes were restored; do not flatten an
   ambiguous canary or cooldown to `eligible`.
5. Only when no affected profile remains in `recovery_required`, preview
   `manage unfreeze`, obtain explicit approval for that updated state, and then
   apply the unchanged new hash.

Automatic canary rejection uses the same exact receipt-bound rollback, records a
cooldown, and never settles the lifecycle before config recovery succeeds.
Management remains frozen or in `recovery_required` whenever exact recovery
cannot be proven.

## Guarded activation

Activation is always preview-first:

```text
hermes auto-routing activate --mode active --json
hermes auto-routing activate --mode active --apply --expected-config-sha SHA256 --json
```

An active preview succeeds only when the read-only doctor validates the
current authority, verified targets and safe default, classifier trust and
economics, resolver signatures, fresh-session and delegation boundaries,
exact credential-pool projection, reasoning projection, and the complete
pre-call fallback contract. It reports the current config precondition, the
proposed active-config hash, and the authority, inventory-contract, and adapter
fingerprints that will be bound into an immutable activation receipt.
These structural checks use a versioned profile-HMAC-attested projection
descriptor written into the persisted inventory observation by the Hermes
adapter. A fresh adapter process can validate the resolver/access mapping
without trusting an arbitrary runtime key. Doctor never resolves a provider,
refreshes credentials, mints a token, discovers models, or probes an endpoint.

Apply re-runs doctor while holding the profile/config lock and one serialized
SQLite write transaction. It rechecks the adapter fingerprint immediately
before publishing the receipt, then commits the receipt and authority before
removing the recovery journal. A crash recovers either to active with the
matching receipt or to the prior non-projecting mode. Hand-editing
`mode: active` never authorizes projection; new decisions require the matching
durable receipt. Return to shadow with the same preview/apply flow using
`--mode shadow`.

Each newly approved inventory fingerprint receives its own immutable receipt,
even when authority, active config, and adapter capability are unchanged. New
decisions use the newest applicable receipt. Existing sessions validate and
replay the exact historical receipt ID stored in their decision.

Availability may change after activation. Every new decision rebuilds and
hard-filters current inventory, while a resumed decision tries only its
recorded eligible fallback chain. Inventory drift is audited by the receipt;
it does not require routine reactivation. Authority, config, or adapter
contract drift does block new projection until a new guarded activation.

## Billable verification boundary

No command probes provider access automatically. `verify-runtime` is the only
Auto Routing operation that may consume money or quota. Preview is free and names
the exact runtime, fixed request shape, economics source, maximum cost/quota,
budget reservation, and expiration. Apply additionally requires:

- policy opt-in (`allow_paid_access_probes: true`);
- the unchanged preview hash; and
- explicit `--ack-billable` acknowledgement.

Inventory, catalog refresh, planning, normal chat, and autonomous stages never
invoke this probe.

## Profile-local state and privacy

All state follows the active profile:

- authority: the profile's `config.yaml`, under
  `plugins.entries.auto-routing`;
- durable observations and revisions: `auto-routing/state.db` below the active
  profile home; and
- the owner-only random credential-binding key:
  `auto-routing/credential-selection.key` below the active profile home; and
- the owner-only random canary-assignment key:
  `auto-routing/canary-assignment.key` below the active profile home; and
- short-lived apply journals and recovery backups: beside that profile's
  `config.yaml`.

The binding key contains no provider credential; it only prevents public
credential-selection fingerprints from becoming offline secret verifiers and
keeps those fingerprints stable across processes. The store and journals are
content-free. They do not retain raw prompt bodies, provider responses,
credentials, secret endpoints, or headers. Representative dry runs
persist/output prompt indexes and derived requirements only.

The canary-assignment key is not a provider credential, but it is protected
profile control-plane state: a profile-local random HMAC key used only to
assign a stable control or challenger arm for a new eligible operation. It is
not sent to a provider and it does not identify a user or task. Hermes creates
it once with owner-only protection (POSIX mode
`0600`); reads and writes pin the `auto-routing` parent directory so a link or
rename race cannot redirect the key outside the active profile. Native Windows
uses a directory handle that denies delete/rename while the key is accessed.

If the file is absent, Hermes may generate a replacement under the profile
lock. That intentionally changes deterministic arm assignment for *future*
operations, while persisted assignments and recorded decision replays remain
unchanged. A malformed, permission-unsafe, linked, or otherwise corrupt key
is never replaced automatically: canary assignment fails closed until the
operator restores or intentionally removes the key and verifies the profile.

Stage 3 evidence stays in the active profile's local
`auto-routing/state.db`. Hermes sends no outbound telemetry and no provider
attribution tag for it. Evidence and feedback are immutable observations;
contradictory feedback remains append-only and visible.

| Evidence storage | Stored or forbidden |
|---|---|
| Attribution | Content-free decision, session, turn, task, epoch, profile, runtime, parent-evidence, and evidence IDs; unsafe external identifiers are domain-separated hashes |
| Route facts | Exact recorded runtime ID and reasoning effort, initial-task flag, and derived initial-task context bucket |
| Outcome | One finite turn outcome, its normalized value when defined, and bounded confidence values |
| Explicit feedback | One finite rating or `rejected`, `corrected`, or `manual-reroute`; no free text |
| Operations | API calls, tool iterations, retries, token counters, cost, nullable latency, and observation time |
| Forbidden content | Raw prompts, tasks, responses, reasons, assistant self-evaluations, URLs, paths, endpoints, credentials, headers, secrets, and provider payloads |

## Static-routing safety properties

- Setup and edit can write only `off` or `shadow`; only `activate` can write
  `active`.
- Only exact `verified` runtimes can be proposed as targets.
- Catalog evidence cannot create executable access.
- YAML and baseline authority are not usable unless both match exactly.
- Active projection additionally requires a matching immutable activation
  receipt.
- Interrupted config and activation applies recover deterministically or
  remain fail-closed.
- Auto-owned fallbacks are resolved before client construction and never leak
  into Hermes's global fallback chain.
- Post-call model-changing failover is deliberately disabled for Auto-owned
  routes and is reported by doctor as a reduced-capability warning.
- Prompt caching and message history remain untouched.
