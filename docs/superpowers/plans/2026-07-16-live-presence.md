# Live Presence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let users collaborate with Hermes through opt-in direct voice, meetings, and screen-assisted sessions with sub-second response, fast barge-in, explicit control handoff, and no hidden sensing or retention.

**Architecture:** Add an internal, model-invisible `agent.realtime` narrow waist containing immutable media/event contracts, a `RealtimeSessionProvider` ABC, a provider registry, and a profile-local orchestrator. Existing local voice, Discord voice, and Google Meet implementations become concrete plugin providers; item #6 authorizes every sensing/transmission/tool/retention transition, item #15 labels and gates every remote frame or sink, and item #12 receives redacted session claims without becoming a media store. CLI and native Ink own consent and session control; providers never capture or transmit until an acknowledged sensor indicator lease exists.

**Tech Stack:** Python 3.13, frozen dataclasses/enums, asyncio/thread-safe queues, canonical JSON/SHA-256, SQLite/WAL through `SessionDB`, existing plugin and gateway registries, item #6 `AuthorityProvider`, item #15 `InformationFlowGuard`, item #12 `ReceiptStore`, Playwright/WebRTC and OpenAI Realtime behind the existing Google Meet plugin, Discord voice/Opus/PCM mixer, existing TTS/STT provider registries, Rich/classic CLI, Ink/TypeScript JSON-RPC TUI, pytest through `scripts/run_tests.sh`, Vitest, deterministic PCM/video/screen fixtures, and versioned YAML proof manifests.

## Global Constraints

- Work from a branch containing item #6's canonical `AuthorityProvider`, `StoredAuthorityProvider`, `ActionContext`, `AuthorityDecision`, and `authorize_effect()`, item #12's immutable `ReceiptStore`, and item #15's `FlowContext`, `FlowDecision`, `InformationFlowGuard`, and `StoredInformationFlowGuard`. Missing prerequisite names fail their prerequisite tests; this plan creates no substitutes.
- Sensing, remote inference, recording, transcription, memory promotion, screen capture, camera capture, meeting join, and tool execution are separate explicit opt-ins. Consent is scoped to exact profile, user/session, sensor, destination/provider, purpose, expiry, and retention choice; an earlier approval is not a reconnect or commit grant.
- Every active microphone, camera, or screen source has an unmistakable persistent terminal/Ink indicator before capture begins and for the full capture interval. Meeting/channel providers also announce sensing in the remote surface. Indicator loss pauses capture locally before any further frame can be transmitted.
- Pause, takeover, and stop bypass normal busy-session queuing. Pause closes the local capture gate immediately; takeover additionally interrupts output and prevents new realtime tool dispatch while preserving the mission, transcript, and any already-running effect's truthful certainty state.
- Recording, transcription, and memory promotion default off and are independent. Raw frames are bounded in memory and discarded after playout/gating unless recording was separately authorized. Transcripts are not recordings, recordings are not memory, and neither is promoted to memory without a separate current authorization.
- A cheap local engagement gate runs before remote transmission where the hardware/provider supports local PCM. Unsupported local gating falls back to explicit push-to-talk; it never silently becomes always-on remote streaming.
- Every remote media frame, model input, tool event, persistence write, and delivery is checked by item #15 with trusted local source/sink/purpose identities. A flow allow and current item #6 allow are both required. Remote text, model output, plugins, or provider events cannot grant consent or declassify data.
- Providers and platform adapters remain plugins or service-gated integrations. The shared ABC/registry is model-invisible Footprint Ladder rung 1; local/Discord/Meet implementations are rung 4 edge consumers. No new model-visible core tool or mid-conversation tool-schema mutation is introduced.
- Stable non-secret defaults live under `realtime:` in profile-local `config.yaml`; API keys and credentials remain in `.env` or secret providers; runtime consent/session/audit metadata lives in profile-local SQLite. No user-facing non-secret `HERMES_*` setting is added.
- The system prompt, cached prefix, effective tool-definition snapshot, provider, and primary model remain byte-stable for a conversation. Realtime state travels through deterministic sidecars/callbacks, never past-message mutation or a synthetic user message; strict message-role alternation and compression-only history mutation remain intact.
- Real-path state, security, device, and remote-I/O tests use a temporary `HERMES_HOME`, real imports, real SQLite/config/plugin discovery, and deterministic media fixtures. Mocks stop only at physical hardware, provider network, platform network, and OS media-process boundaries.
- Session/audit/receipt language is truthful: `connected` is not sensing, `sent` is not heard, a transcript is not verified, provider acknowledgement is not exactly-once, and an interrupted/ambiguous tool effect remains `unknown_effect` until independently reconciled.
- No outbound telemetry is enabled. Proof metrics and media traces stay local under an explicitly selected output directory, exclude raw private content by default, and use opaque participant/provider identifiers.
- Desktop is not a dependency or parity target. Dashboard receives no second chat/consent implementation; its embedded Ink surface is sufficient, with an optional read-only status endpoint only after primary controls pass.
- The frozen proof contains at least 20 opt-in sessions, at least five users, and all three workflows: direct voice, meeting, and screen-assisted work, with recorded hardware/network classes and a paired push-to-talk baseline.

---

## Approved Portfolio Contract

**Layman outcome:** Hermes can collaborate naturally in real time through voice, screen, meetings, and camera input, including interruption and control handoff, while always showing what is being sensed or retained.

**90-day proof:** Run at least 20 opt-in sessions across at least five users and three fixed workflow types—direct voice, a meeting, and screen-assisted work—on recorded hardware/network classes. Measure p95 end-of-user-turn to first-audio latency, p95 speech-stop after barge-in, false engagements per listening hour, tool error, retained-data correctness, battery/network cost, privacy-state comprehension, and paired preference against push-to-talk. For a retained workflow, pass only with p95 first audio below 1,000 ms, p95 barge-in stop below 300 ms, zero unindicated sensing or retention, zero privacy-comprehension errors, and live-mode preference in at least 60% of paired ratings.

**Dependencies and failure conditions:** Item #6 governs consent and all realtime actions; item #15 protects every source-to-sink stream; item #12 stores outcome claims and artifact digests, not raw media by default. If users prefer push-to-talk, false engagement exceeds the frozen nuisance floor, privacy or retention comprehension fails once, local pause leaks one post-pause frame, tool execution occurs without exact current consent, or hardware/battery/network cost exceeds the preregistered ceiling, narrow the affected workflow or stop it instead of forcing an always-on mode.

**Delivery:** Footprint Ladder rung 1 + 4—extend existing lifecycle, plugin, voice, TTS/STT, gateway, CLI, and Ink seams with a model-invisible shared ABC; concrete realtime providers/platforms remain plugins and service-gated. Dashboard is secondary status only if later proven useful; Desktop and a new core tool schema are excluded.

---

## Product Boundary and Frozen Proof

Live Presence owns transient duplex media coordination, sensor/indicator state, local engagement, turn/barge-in timing, and user control seizure. Missions own durable objective/execution state; item #6 owns authority; item #15 owns flow; item #12 owns proof; TTS/STT providers own synthesis/transcription; platform plugins own physical/platform transport. The realtime orchestrator may correlate these contracts but may not duplicate them.

The manifest `benchmarks/realtime/manifest.yaml` freezes version `live-presence-20-v1`, baseline `current_hermes_push_to_talk`, candidate `live_presence`, clocks (`time.monotonic_ns` for latency and UTC for audit), cost source (provider response usage plus session network byte counters), and these denominators before any run:

- exactly 20 required sessions across at least five consenting users, each user participating in at least three sessions;
- at least six direct-voice sessions, six meeting sessions, and six screen-assisted sessions, with the remaining two assigned before collection;
- one paired live/push-to-talk rating per completed workflow session, with ordering counterbalanced and no excluded rating silently shrinking the denominator;
- hardware class (CPU, RAM, audio/video devices, power mode), OS, provider/platform, network class (wired/Wi-Fi/cellular, measured RTT/down/up), codec/sample rate, and enabled sensors recorded for every session;
- deterministic injected fault runs are separate from the 20 human sessions and never substitute for a user session.

Metric definitions and gates are frozen as follows:

| Metric | Exact definition | Pass/stop gate |
|---|---|---|
| First audio | monotonic time from locally detected end-of-user-turn to first non-silent output frame accepted by the platform/device | At least one workflow retained only if p95 `< 1,000 ms`; report p50/p95 per workflow/hardware/network class |
| Barge-in | monotonic time from first locally detected interrupt speech frame to the final non-silent Hermes output frame | p95 `< 300 ms`, zero samples `>= 1,000 ms` |
| Pause/takeover | command/key receipt to local capture gate closed; count remote frames after closure watermark | p95 `< 100 ms` and zero post-pause remote frames/tool starts |
| False engagement | turns opened without the preregistered wake/push-to-talk/direct-address condition per listening hour | `<= 0.5/hour` overall and no user reports more than `2/hour`; otherwise remove hands-free mode for that workflow |
| Tool error | attempted realtime tools with wrong arguments, failed effects, or mismatched turn/session divided by authorized realtime tool attempts | No unauthorized attempts; candidate error rate no more than five percentage points above paired baseline |
| Retention correctness | recorded/transcribed/promoted/deleted state exactly matches the three consent toggles and expiry after reconnect/crash | 100%; any unindicated or unauthorized retention is a hard stop |
| Privacy comprehension | after each session, user correctly identifies active sensors, remote destination, and recording/transcription/memory states from UI without coaching | 100%; one error is a hard stop |
| Preference | live selected over paired push-to-talk for the same workflow after both modes | `>= 60%` with denominator and Wilson 95% interval reported; underpowered remains inconclusive |
| Battery | percentage-point battery drain/hour candidate minus paired baseline on battery-capable hardware | `<= 10` points/hour; higher removes hands-free mode on that hardware class |
| Network | measured application bytes/hour by sensor/workflow | audio-only `<= 150 MB/hour`; screen/camera `<= 1.5 GB/hour`; higher forces lower-rate/local-only mode or stops that workflow |
| Cost | provider/platform charge per completed session and per verified workflow outcome | Report p50/p95 and stop a provider lane above the preregistered per-session cap in `manifest.yaml`; never average away an over-cap session |

Hard floors are zero unindicated sensing, zero unindicated retention, zero privacy-comprehension errors, zero cross-profile/cross-session media, zero unauthorized remote frames, zero unauthorized tool starts, and zero false `verified` receipts. Missing/aborted sessions are listed with reason and do not count toward denominators.

## Current Code Map and Ownership

### Existing seams this plan extends

- `plugins/google_meet/realtime/openai_client.py` — synchronous OpenAI Realtime connection, streamed PCM output, and `response.cancel` barge-in; currently text-to-audio only and provider-specific.
- `plugins/google_meet/meet_bot.py` — Playwright meeting lifecycle, captions, virtual audio bridge, status JSON, crude caption-driven barge-in, and unconditional transcript writes; currently lacks shared consent/retention/indicator contracts.
- `plugins/google_meet/process_manager.py` and `plugins/google_meet/tools.py` — subprocess lifecycle and queue-based `meet_say`; keep compatibility while routing new sessions through the generic provider and CLI, not a new core tool.
- `plugins/platforms/discord/adapter.py` — voice receive buffers, silence detection, STT, voice join/leave, playback, and gateway callback; currently transcribes after whole utterances and has platform-local session state.
- `plugins/platforms/discord/voice_mixer.py` — continuous 20 ms PCM output, speech layering, ducking, and `stop_speech()` suitable for fast local barge-in.
- `agent/tts_provider.py` and `agent/tts_registry.py` — plugin TTS ABC/registry with optional byte streaming; remain the synthesis owner.
- `agent/transcription_provider.py` and `agent/transcription_registry.py` — plugin STT extension seam; remain the transcription owner.
- `hermes_cli/voice.py` — local push-to-talk/continuous microphone capture, silence callback, transcription, and speech playback; direct voice provider wraps these primitives instead of maintaining a second recorder.
- `hermes_cli/plugins.py::PluginContext` — existing provider/platform registration surface; gains only model-invisible realtime registration.
- `gateway/platforms/base.py::BasePlatformAdapter` — platform connect/disconnect, active-session interrupt bypass, media delivery, and processing hooks; gains optional presence hooks without making all adapters realtime-aware.
- `gateway/run.py` and `gateway/session.py` — durable conversation identity, busy-session controls, interrupts, and transcript ownership; realtime control stays a sidecar keyed to these IDs.
- `hermes_cli/commands.py`, `cli.py`, and `hermes_cli/main.py` — canonical slash and top-level command registration.
- `tui_gateway/server.py`, `ui-tui/src/gatewayTypes.ts`, and `ui-tui/src/app/slash/commands/session.ts` — live JSON-RPC, session interruption, `/voice`, and native Ink command routing; primary presence UI extends these paths.
- `gateway/stream_events.py` and `gateway/stream_dispatch.py` — text/tool progress events; realtime tool events correlate to these but media frames never enter the text stream.
- `gateway/platforms/base.py::validate_media_delivery_path()` and item #12 `digest_artifact()` — safe local artifact validation/digests; raw live frames are not delivery artifacts.

### New focused production files

- `agent/realtime/__init__.py` — stable public exports and schema version.
- `agent/realtime/models.py` — immutable capabilities, media formats/frames, consent, sensor, turn, interruption, tool, latency, and session records.
- `agent/realtime/provider.py` — `RealtimeSessionProvider`, `RealtimeTransportSession`, and `RealtimeEventSink` protocols.
- `agent/realtime/registry.py` — thread-safe plugin provider registration, ownership, service gating, and resolution.
- `agent/realtime/store.py` — profile-local session/consent/event metadata and crash/reconnect watermarks; never raw frame bytes.
- `agent/realtime/engagement.py` — local engagement-gate protocol and deterministic PCM energy/direct-address gate.
- `agent/realtime/policy.py` — item #6 action-context and item #15 flow-context builders.
- `agent/realtime/retention.py` — separately authorized recording/transcription/memory sinks, expiry, delete, and receipt-claim projection.
- `agent/realtime/orchestrator.py` — one state machine for consent, indicator leases, duplex routing, reconnect, barge-in, control seizure, and mission-safe tool correlation.
- `hermes_cli/presence.py` — shared command service for top-level CLI, classic slash, and TUI RPC.
- `plugins/local_presence/__init__.py`, `plugins/local_presence/manifest.yaml`, and `plugins/local_presence/provider.py` — local direct voice/screen provider using existing CLI voice primitives and OS capture boundaries.
- `plugins/platforms/discord/realtime_provider.py` — Discord adapter wrapper over receiver/mixer lifecycle.
- `plugins/google_meet/realtime/provider.py` — Google Meet provider wrapper over current subprocess/audio bridge and synchronized screen/video observations.

### Existing production files modified

- `hermes_cli/plugins.py`, `hermes_cli/commands.py`, `hermes_cli/main.py`, `cli.py`
- `gateway/platforms/base.py`, `gateway/run.py`
- `tui_gateway/server.py`, `ui-tui/src/gatewayTypes.ts`, `ui-tui/src/app/slash/commands/session.ts`
- `hermes_cli/voice.py`, `plugins/platforms/discord/adapter.py`, `plugins/platforms/discord/voice_mixer.py`
- `plugins/google_meet/__init__.py`, `plugins/google_meet/process_manager.py`, `plugins/google_meet/meet_bot.py`, `plugins/google_meet/realtime/__init__.py`, `plugins/google_meet/realtime/openai_client.py`

### Focused tests and proof assets

- `tests/agent/realtime/test_models.py`, `test_registry.py`, `test_store.py`, `test_engagement.py`, `test_policy.py`, `test_retention.py`, `test_orchestrator.py`, `test_security.py`
- `tests/plugins/test_local_presence.py`, `tests/plugins/test_google_meet_realtime.py`, `tests/gateway/test_discord_realtime_provider.py`, `tests/gateway/test_discord_voice_mixer.py`
- `tests/hermes_cli/test_presence.py`, `tests/tui_gateway/test_presence_rpc.py`, `ui-tui/src/__tests__/presenceCommand.test.ts`, `ui-tui/src/__tests__/presenceStatus.test.tsx`, `ui-tui/src/__tests__/slashParity.test.ts`
- `tests/integration/test_live_presence_e2e.py`, `tests/benchmarks/test_live_presence_benchmark.py`
- `tests/fixtures/realtime/generate_fixtures.py`, `tests/fixtures/realtime/audio/utterance_24k_mono_s16le.pcm`, `silence_24k_mono_s16le.pcm`, `barge_in_48k_stereo_s16le.pcm`, `tests/fixtures/realtime/video/camera_320x180_i420.bin`, `tests/fixtures/realtime/screen/screen_640x360_rgba.bin`
- `benchmarks/realtime/manifest.yaml`, `benchmarks/realtime/sessions.yaml`, `benchmarks/realtime/runner.py`, `benchmarks/realtime/score.py`
- `website/docs/user-guide/features/live-presence.md`, `website/docs/development/realtime-providers.md`, `website/docs/reference/cli-commands.md`, `website/docs/reference/slash-commands.md`, `website/sidebars.ts`

## Canonical Public Interfaces — Frozen for All Tasks

`agent.realtime` exports these exact names. Frame payloads exist only in bounded process memory; canonical hashes/audit/receipts use `payload_sha256`, never payload bytes.

```python
RealtimeModality = Literal["audio", "video", "screen"]
SensorState = Literal["off", "arming", "active", "paused", "lost", "error"]
SessionState = Literal[
    "created", "awaiting_consent", "arming", "active", "paused",
    "reconnecting", "stopping", "stopped", "failed",
]
ConsentKind = Literal[
    "sense_audio", "sense_video", "sense_screen", "remote_transmit",
    "realtime_tools", "record", "transcribe", "promote_memory",
]

@dataclass(frozen=True)
class AudioFormat:
    encoding: Literal["pcm_s16le", "opus"]
    sample_rate_hz: int
    channels: int
    frame_duration_ms: int

@dataclass(frozen=True)
class VideoFormat:
    encoding: Literal["i420", "rgba", "jpeg", "h264"]
    width: int
    height: int
    frames_per_second: int

@dataclass(frozen=True)
class MediaFrame:
    session_id: str
    modality: RealtimeModality
    direction: Literal["input", "output"]
    sequence: int
    captured_at_monotonic_ns: int
    duration_ms: int
    format: AudioFormat | VideoFormat
    payload: bytes
    payload_sha256: str
    source_id: str
    flow_context_hash: str | None

@dataclass(frozen=True)
class ConsentGrant:
    consent_id: str
    session_id: str
    profile_id: str
    user_id_hash: str
    kind: ConsentKind
    source_ids: tuple[str, ...]
    destination_ids: tuple[str, ...]
    purpose_id: str
    authority_version: int
    authority_hash: str
    issued_at_ms: int
    expires_at_ms: int
    maximum_uses: int | None

@dataclass(frozen=True)
class ConsentState:
    grants: tuple[ConsentGrant, ...]
    recording: bool
    transcription: bool
    memory_promotion: bool
    remote_transmission: bool

@dataclass(frozen=True)
class SensorSnapshot:
    microphone: SensorState
    camera: SensorState
    screen: SensorState
    active_source_ids: tuple[str, ...]
    indicator_lease_id: str | None

@dataclass(frozen=True)
class RealtimeEvent:
    event_id: str
    session_id: str
    sequence: int
    kind: Literal[
        "sensor", "vad", "turn_started", "turn_ended", "interrupted",
        "tool_started", "tool_finished", "latency", "consent", "device_lost",
        "reconnecting", "reconnected", "retention", "error",
    ]
    occurred_at_monotonic_ns: int
    payload_json: str

@dataclass(frozen=True)
class RealtimeSessionRequest:
    session_id: str
    profile_id: str
    conversation_session_id: str
    mission_id: str | None
    provider_id: str
    workflow: Literal["direct_voice", "meeting", "screen_assisted"]
    requested_modalities: tuple[RealtimeModality, ...]
    consent: ConsentState
    reconnect_of: str | None

@dataclass(frozen=True)
class RealtimeSessionSnapshot:
    session_id: str
    state: SessionState
    provider_id: str
    workflow: str
    sensors: SensorSnapshot
    consent: ConsentState
    last_input_sequence: int
    last_output_sequence: int
    last_event_sequence: int
    reconnect_count: int
    uncertainty: tuple[str, ...]

class RealtimeEventSink(Protocol):
    def on_frame(self, frame: MediaFrame) -> None: ...
    def on_event(self, event: RealtimeEvent) -> None: ...

class RealtimeTransportSession(Protocol):
    session_id: str
    def start(self) -> None: ...
    def send_audio(self, frame: MediaFrame) -> None: ...
    def send_video(self, frame: MediaFrame) -> None: ...
    def send_screen(self, frame: MediaFrame) -> None: ...
    def interrupt_output(self, reason: str) -> int: ...
    def pause_capture(self) -> int: ...
    def resume_capture(self) -> None: ...
    def close(self, reason: str) -> None: ...

class RealtimeSessionProvider(Protocol):
    provider_id: str
    def capabilities(self) -> RealtimeCapabilities: ...
    def is_available(self) -> bool: ...
    def open_session(
        self, request: RealtimeSessionRequest, sink: RealtimeEventSink,
    ) -> RealtimeTransportSession: ...
```

`RealtimeOrchestrator.start()` accepts a request plus an `IndicatorLease`; it calls `authorize_effect()` for each requested consent kind, builds/evaluates item #15 `FlowContext` before any remote frame, and opens the provider only after both gates and indicator acknowledgement. Its exact control API is:

`ConsentGrant` is a session-scoped, immutable projection of an item #6 `AuthorityDecision`, not an independent authorization source. Every consuming transition compares its authority version/hash with the current provider decision; no method in `agent.realtime` can mint a grant from model, provider, platform, or plugin content.

```python
class RealtimeOrchestrator:
    def start(self, request: RealtimeSessionRequest, indicator: IndicatorLease) -> RealtimeSessionSnapshot: ...
    def status(self, session_id: str) -> RealtimeSessionSnapshot: ...
    def pause(self, session_id: str, *, actor_id: str) -> RealtimeSessionSnapshot: ...
    def resume(self, session_id: str, *, actor_id: str, indicator: IndicatorLease) -> RealtimeSessionSnapshot: ...
    def take_control(self, session_id: str, *, actor_id: str) -> RealtimeSessionSnapshot: ...
    def stop(self, session_id: str, *, actor_id: str, reason: str) -> RealtimeSessionSnapshot: ...
    def reconnect(self, session_id: str, indicator: IndicatorLease) -> RealtimeSessionSnapshot: ...
```

Reconnect never replays captured input, synthesized output, consent consumption, or tool starts. It restores metadata, reacquires device and indicator leases, reauthorizes current destinations, emits a discontinuity event, and starts at the next sequence. An ambiguous already-started effect remains owned by the mission/transaction journal and is not retried by realtime code.

---

### Task 0: Freeze the Opt-In 20-Session Contract and Fixtures

**Files:**
- Create: `benchmarks/realtime/manifest.yaml`
- Create: `benchmarks/realtime/sessions.yaml`
- Create: `tests/benchmarks/test_live_presence_benchmark.py`
- Create: `tests/fixtures/realtime/generate_fixtures.py`
- Create: `tests/fixtures/realtime/audio/utterance_24k_mono_s16le.pcm`
- Create: `tests/fixtures/realtime/audio/silence_24k_mono_s16le.pcm`
- Create: `tests/fixtures/realtime/audio/barge_in_48k_stereo_s16le.pcm`
- Create: `tests/fixtures/realtime/video/camera_320x180_i420.bin`
- Create: `tests/fixtures/realtime/screen/screen_640x360_rgba.bin`

**Interfaces:**
- Produces proof version `live-presence-20-v1`, exact session IDs/workflow strata, hardware/network schema, metric definitions, thresholds, exclusions, fixture digests, and paired baseline order consumed by Task 10.
- Consumes only the Approved Portfolio Contract; fixture payloads contain synthetic tones/color bars and no personal content.

- [ ] **RED: write the manifest contract test**

```python
def test_manifest_freezes_exact_denominators_and_hard_floors(load_presence_manifest):
    manifest, sessions = load_presence_manifest()
    assert manifest["version"] == "live-presence-20-v1"
    assert len(sessions) == 20
    assert len({row["user_id"] for row in sessions}) >= 5
    assert all(sum(row["workflow"] == flow for row in sessions) >= 6 for flow in (
        "direct_voice", "meeting", "screen_assisted",
    ))
    assert manifest["gates"]["first_audio_p95_ms"] == 1000
    assert manifest["gates"]["barge_in_p95_ms"] == 300
    assert manifest["hard_floors"] == {
        "unindicated_sensing": 0, "unindicated_retention": 0,
        "privacy_comprehension_errors": 0, "unauthorized_remote_frames": 0,
        "unauthorized_tool_starts": 0, "false_verified_receipts": 0,
    }
```

- [ ] **Run RED**

Run: `scripts/run_tests.sh tests/benchmarks/test_live_presence_benchmark.py -q`

Expected: FAIL because the frozen manifest, session corpus, and media fixtures do not exist.

- [ ] **Implement the frozen corpus and deterministic fixtures**

Create the two YAML files with the exact denominators and gates above. `generate_fixtures.py` uses only `math`, `struct`, and `pathlib` to deterministically write a 400 ms 440 Hz PCM utterance, 400 ms zero PCM silence, 200 ms stereo interrupt tone, I420 color bars, and RGBA checkerboard; `--check` regenerates in a temporary directory and compares bytes. Record SHA-256, dimensions, duration, sample rate, and channels in the manifest. Each session row declares `session_id`, opaque `user_id`, workflow, baseline order, OS/hardware/device/power/network/provider classes to record, sensors, faults, consent toggles, and required comprehension questions.

- [ ] **Run GREEN**

Run: `scripts/run_tests.sh tests/benchmarks/test_live_presence_benchmark.py -q`

Expected: PASS with exactly 20 valid sessions, every workflow/user floor satisfied, balanced ordering, matching fixture digests, and all thresholds immutable.

Run: `python tests/fixtures/realtime/generate_fixtures.py --check`

Expected: exits 0 after byte-for-byte regeneration of all five fixtures.

- [ ] **Commit**

```bash
git add benchmarks/realtime/manifest.yaml benchmarks/realtime/sessions.yaml tests/benchmarks/test_live_presence_benchmark.py tests/fixtures/realtime
git commit -m "test: freeze live presence proof contract"
```

---

### Task 1: Define Normalized Duplex Media, Events, and Provider ABC

**Files:**
- Create: `agent/realtime/__init__.py`
- Create: `agent/realtime/models.py`
- Create: `agent/realtime/provider.py`
- Create: `tests/agent/realtime/test_models.py`

**Interfaces:**
- Produces every exact public name/signature in “Canonical Public Interfaces,” plus `RealtimeCapabilities`, `IndicatorLease`, `EngagementDecision`, and schema version `hermes.realtime.v1`.
- Consumes no provider SDK; all public types are immutable, provider-neutral, bounded, and model-invisible.

- [ ] **RED: write immutable media and consent invariants**

```python
def test_frame_hash_format_and_sequence_are_validated(pcm_frame):
    assert pcm_frame.payload_sha256 == hashlib.sha256(pcm_frame.payload).hexdigest()
    with pytest.raises(ValueError, match="payload digest"):
        dataclasses.replace(pcm_frame, payload_sha256="0" * 64)
    with pytest.raises(ValueError, match="sequence"):
        dataclasses.replace(pcm_frame, sequence=-1)


def test_record_transcribe_and_memory_are_independent(consent_state):
    state = dataclasses.replace(
        consent_state, recording=True, transcription=False, memory_promotion=False,
    )
    assert state.recording and not state.transcription and not state.memory_promotion
```

- [ ] **Run RED**

Run: `scripts/run_tests.sh tests/agent/realtime/test_models.py -q`

Expected: FAIL importing `agent.realtime`.

- [ ] **Implement the frozen dataclasses and protocols**

Implement the public block exactly. Validate enum values, positive bounded dimensions/rates/durations, payload digest, nonnegative strictly increasing sequences at the orchestrator boundary, normalized sorted IDs, UTC/monotonic domain separation, consent expiry, and matching `session_id`. `RealtimeCapabilities` declares input/output modalities, formats, local VAD, interruption, tool events, reconnect, recording, transcription, and maximum frame bytes. `IndicatorLease` carries exact sensor set, surface ID, opaque acknowledgement ID, acquired/expiry times, and a thread-safe `is_visible()` callback excluded from canonical serialization.

- [ ] **Run GREEN**

Run: `scripts/run_tests.sh tests/agent/realtime/test_models.py -q`

Expected: PASS; invalid/cross-session/oversized frames and coupled retention flags fail closed while valid audio/video/screen frames round-trip canonically.

- [ ] **Commit**

```bash
git add agent/realtime/__init__.py agent/realtime/models.py agent/realtime/provider.py tests/agent/realtime/test_models.py
git commit -m "feat: define realtime session contracts"
```

---

### Task 2: Add Service-Gated Plugin Registration and Resolution

**Files:**
- Create: `agent/realtime/registry.py`
- Modify: `hermes_cli/plugins.py`
- Create: `tests/agent/realtime/test_registry.py`
- Modify: `tests/hermes_cli/test_plugins.py`

**Interfaces:**
- Produces `register_realtime_provider(provider, *, owner, check_fn=None)`, `get_realtime_provider(provider_id)`, `list_realtime_providers(available_only=True)`, and `PluginContext.register_realtime_provider(provider, *, check_fn=None)`.
- Consumes Task 1 `RealtimeSessionProvider`, existing plugin ownership/override policy, and provider `is_available()`; registration changes no model schema.

- [ ] **RED: write ownership, availability, and schema tests**

```python
def test_unavailable_provider_is_not_resolved(registry, fake_provider):
    registry.register(fake_provider, owner="fixture", check_fn=lambda: False)
    assert registry.get(fake_provider.provider_id) is None
    assert registry.list(available_only=True) == []


def test_realtime_registration_does_not_change_tool_schema(plugin_harness):
    before = canonical_tool_snapshot()
    plugin_harness.register_realtime_provider(FakeRealtimeProvider())
    assert canonical_tool_snapshot() == before
```

- [ ] **Run RED**

Run: `scripts/run_tests.sh tests/agent/realtime/test_registry.py tests/hermes_cli/test_plugins.py -q`

Expected: FAIL because realtime provider registration is absent.

- [ ] **Implement bounded provider registration**

Use the same lock/normalized-name pattern as `agent/tts_registry.py`, but bind each entry to its plugin owner and explicit override policy. Validate the Task 1 protocol/capabilities, provider ID, deterministic capabilities, maximum frame bounds, and `check_fn`. A false/raising `check_fn` makes the provider unavailable without retaining a stale open session. Built-in ownership cannot be shadowed without the existing explicit operator override. Registration and availability checks never import a vendor SDK until its plugin is selected.

- [ ] **Run GREEN**

Run: `scripts/run_tests.sh tests/agent/realtime/test_registry.py tests/hermes_cli/test_plugins.py tests/test_get_tool_definitions_cache_isolation.py -q`

Expected: PASS; two synthetic plugin providers resolve independently, unavailable/malicious overrides fail closed, and effective tool definitions are byte-identical.

- [ ] **Commit**

```bash
git add agent/realtime/registry.py hermes_cli/plugins.py tests/agent/realtime/test_registry.py tests/hermes_cli/test_plugins.py
git commit -m "feat: register realtime providers"
```

---

### Task 3: Persist Metadata and Enforce Authority, Flow, and Indicators

**Files:**
- Create: `agent/realtime/store.py`
- Create: `agent/realtime/policy.py`
- Create: `tests/agent/realtime/test_store.py`
- Create: `tests/agent/realtime/test_policy.py`

**Interfaces:**
- Produces `RealtimeStore`, `build_presence_action_context()`, `build_media_flow_context()`, `authorize_presence_transition()`, and `authorize_media_sink()`.
- Consumes Task 1 models, item #6 `authorize_effect()`, item #15 `StoredInformationFlowGuard.evaluate()`, `SessionDB._execute_write()`, and active-profile `get_hermes_home()`.

- [ ] **RED: write crash, replay, stale-consent, and flow tests**

```python
def test_sensor_cannot_activate_without_visible_indicator_and_current_consent(policy):
    with pytest.raises(PermissionError, match="indicator_not_visible"):
        policy.authorize_start(indicator=expired_indicator(), consent=current_consent())
    with pytest.raises(PermissionError, match="authority_changed"):
        policy.authorize_start(indicator=visible_indicator(), consent=stale_consent())


def test_remote_frame_requires_authority_and_flow_allow(policy, input_frame):
    policy.flow_guard.next_verdict = "block"
    result = policy.authorize_frame(input_frame, destination="provider:test")
    assert result.allowed is False
    assert policy.remote_calls == 0


def test_reopen_marks_active_session_paused_without_replaying_frames(store):
    store.seed_active(last_input_sequence=41, last_output_sequence=9)
    reopened = store.recover()
    assert reopened.state == "paused"
    assert reopened.last_input_sequence == 41
    assert store.frame_payload_rows() == 0
```

- [ ] **Run RED**

Run: `scripts/run_tests.sh tests/agent/realtime/test_store.py tests/agent/realtime/test_policy.py -q`

Expected: FAIL because realtime tables and policy bridges do not exist.

- [ ] **Implement profile-local metadata and the dual security gate**

Create additive SQLite tables for sessions, consent references, sensor/indicator transitions, sequence watermarks, redacted event metadata, retention manifests, and audit hashes. Store provider/workflow/opaque source/destination IDs, authority/flow hashes, byte counts, timing, and uncertainty; reject payload bytes, transcript text, tool args/results, raw user IDs, and credentials. Use atomic CAS for state/sequence transitions, audit-before-active, and idempotent stop/recovery.

Map transitions to item #6 actions `realtime.sense.audio`, `.video`, `.screen`, `.transmit`, `.tool.execute`, `.record`, `.transcribe`, and `.memory.promote`. Build item #15 trusted sources from local device/platform identity and sinks from exact provider/platform/retention destination, with purposes `realtime.dialogue`, `realtime.meeting`, `realtime.screen_assist`, `realtime.tool`, and `realtime.retention`. Recheck authority/flow at start, resume, reconnect, every destination change, every tool start, and every retention write; sample-level frames may reuse a short-lived decision only while exact context hash, consent expiry, indicator lease, device, sink, and authority/flow versions remain unchanged.

- [ ] **Run GREEN**

Run: `scripts/run_tests.sh tests/agent/realtime/test_store.py tests/agent/realtime/test_policy.py tests/hermes_cli/test_profiles.py -q`

Expected: PASS; crash recovery is paused, cross-profile replay/stale grants/destination changes block, raw media never reaches SQLite, and valid current indicator + authority + flow permits exact scoped transitions.

- [ ] **Commit**

```bash
git add agent/realtime/store.py agent/realtime/policy.py tests/agent/realtime/test_store.py tests/agent/realtime/test_policy.py
git commit -m "feat: secure realtime session state"
```

---

### Task 4: Orchestrate Local Engagement, Turns, Barge-In, and Control Seizure

**Files:**
- Create: `agent/realtime/engagement.py`
- Create: `agent/realtime/orchestrator.py`
- Create: `tests/agent/realtime/test_engagement.py`
- Create: `tests/agent/realtime/test_orchestrator.py`

**Interfaces:**
- Produces `LocalEngagementGate.evaluate(frame) -> EngagementDecision`, `PcmEnergyEngagementGate`, the exact `RealtimeOrchestrator` API, `RealtimeOrchestrator.on_frame()`, `on_event()`, and `dispatch_tool_event()`.
- Consumes Tasks 1–3 contracts/registry/store/policy, provider sessions, existing mission/transaction interruption callbacks, and a bounded `IndicatorLease`; it never owns mission state or effect retry.

- [ ] **RED: write engagement, interruption, and mission-isolation tests**

```python
def test_local_gate_drops_background_before_remote_send(orchestrator, background_pcm):
    orchestrator.start(direct_request(), visible_indicator())
    orchestrator.on_frame(background_pcm)
    assert orchestrator.provider.sent_frames == []
    assert orchestrator.metrics.false_engagements == 0


def test_barge_in_stops_output_and_takeover_preserves_mission(orchestrator, clock):
    snap = orchestrator.start(direct_request(mission_id="m-1"), visible_indicator())
    orchestrator.provider.begin_output()
    clock.advance_ms(20)
    result = orchestrator.on_local_speech(interrupt_frame())
    assert result.output_stopped_at_ns - result.detected_at_ns < 300_000_000
    taken = orchestrator.take_control(snap.session_id, actor_id="user-1")
    assert taken.state == "paused"
    assert orchestrator.missions.get("m-1").state == "running"
    assert orchestrator.new_tool_calls == 0
```

- [ ] **Run RED**

Run: `scripts/run_tests.sh tests/agent/realtime/test_engagement.py tests/agent/realtime/test_orchestrator.py -q`

Expected: FAIL because local engagement and the state machine are absent.

- [ ] **Implement local gating and the single state machine**

`PcmEnergyEngagementGate` computes bounded integer RMS over PCM s16le frames, maintains noise-floor hysteresis, requires the configured direct-address/wake window or push-to-talk latch, and emits `engaged`, `background`, or `uncertain`; `uncertain` stays local and requests push-to-talk. Provider-native VAD may supply authenticated `vad` events but cannot bypass the engagement/consent gate.

Implement CAS state transitions `created -> awaiting_consent -> arming -> active`, `active <-> paused`, `active -> reconnecting`, and terminal `stopped|failed`. Start order is indicator acknowledgement, item #6 checks, item #15 checks, store audit, provider open, local device arm, then `active`. Every frame validates session/source/sequence/size/digest, indicator visibility, engagement, and policy before routing. Output frames are tracked in 20 ms-equivalent playout watermarks so interruption records the actual last non-silent frame.

`pause()` first atomically closes a process-local capture gate, then interrupts output/provider and persists state. `take_control()` performs pause plus blocks new realtime tool correlation; it calls the existing agent interrupt only for the current realtime response, never mission cancel/reset or transcript rewrite. A tool already handed to transaction middleware retains its journal state; the orchestrator records its correlation as pending/`unknown_effect` and waits for the existing completion/reconciliation event.

- [ ] **Run GREEN**

Run: `scripts/run_tests.sh tests/agent/realtime/test_engagement.py tests/agent/realtime/test_orchestrator.py tests/run_agent/test_stream_interrupt_retry.py tests/gateway/test_command_bypass_active_session.py -q`

Expected: PASS; background stays local, sequences are monotonic, pause/takeover stops capture before provider calls, barge-in stops output within the deterministic 300 ms fixture budget, and mission/tool certainty remains intact.

- [ ] **Commit**

```bash
git add agent/realtime/engagement.py agent/realtime/orchestrator.py tests/agent/realtime/test_engagement.py tests/agent/realtime/test_orchestrator.py
git commit -m "feat: orchestrate live presence sessions"
```

---

### Task 5: Separate Recording, Transcription, Memory, and Receipt Claims

**Files:**
- Create: `agent/realtime/retention.py`
- Create: `tests/agent/realtime/test_retention.py`
- Modify: `agent/realtime/orchestrator.py`
- Modify: `agent/realtime/store.py`

**Interfaces:**
- Produces `RealtimeRetentionService.record_frame()`, `append_transcript_segment()`, `promote_memory()`, `purge_session()`, `build_receipt_claims() -> tuple[ReceiptClaim, ...]`, and `RetentionManifest`.
- Consumes current Task 3 authority/flow checks, active-profile paths, existing transcription/memory providers, item #12 `ReceiptStore`/`ReceiptClaim`/`EvidenceDigest`/`ArtifactDigest`, and no raw receipt storage.

- [ ] **RED: write independent opt-in and deletion tests**

```python
@pytest.mark.parametrize(
    "record,transcribe,memory,expected",
    [
        (False, False, False, (0, 0, 0)),
        (True, False, False, (1, 0, 0)),
        (False, True, False, (0, 1, 0)),
        (False, True, True, (0, 1, 1)),
    ],
)
def test_retention_options_are_independent(retention, frame, record, transcribe, memory, expected):
    retention.run(frame, consent(record=record, transcribe=transcribe, memory=memory))
    assert retention.counts() == expected


def test_crash_recovery_and_expiry_never_promote_orphan_media(retention):
    retention.crash_after_record_before_manifest()
    retention.recover(now_ms=EXPIRY_MS + 1)
    assert retention.raw_files() == []
    assert retention.memory_calls == 0
```

- [ ] **Run RED**

Run: `scripts/run_tests.sh tests/agent/realtime/test_retention.py -q`

Expected: FAIL because retention service/manifests are absent.

- [ ] **Implement three distinct sinks and conservative recovery**

Recording writes only after `realtime.record` authority and `persist` flow allow, under `get_hermes_home()/realtime/recordings/<session-id>/`, using create-exclusive files, normalized extensions, exact byte caps, owner-only permissions where supported, atomic manifest updates, and configured expiry. Transcription consumes a transient frame stream only after its own authorization and writes bounded timestamp/speaker/digest segments to SQLite; recording permission is neither required nor implied. Memory promotion accepts selected transcript claim IDs only after a fresh `realtime.memory.promote` allow and existing memory-provider flow check; it never reads raw recordings implicitly.

On crash, unmanifested files are quarantined then deleted, incomplete transcript segments remain `completed_unverified`, and no memory call is retried. `purge_session()` closes writers, deletes raw media/transcript rows and derived memory through the existing provider deletion contract, appends tombstones, and returns exact success/unknown lists. Receipt claims contain consent/indicator/sensor intervals, byte/segment counts, latency/tool outcome, retention manifest/artifact digests, and deletion uncertainty; the realtime scorer never constructs `VerifiedReceiptDecision` and raw content never enters claims/audit.

- [ ] **Run GREEN**

Run: `scripts/run_tests.sh tests/agent/realtime/test_retention.py tests/agent/test_memory_manager.py tests/agent/test_compressor_media_stripping.py -q`

Expected: PASS across all eight consent combinations, expiry/crash/replay, symlink/path attacks, deletion cascades, and redacted receipt projection with zero implicit promotion.

- [ ] **Commit**

```bash
git add agent/realtime/retention.py agent/realtime/orchestrator.py agent/realtime/store.py tests/agent/realtime/test_retention.py
git commit -m "feat: govern realtime retention"
```

---

### Task 6: Deliver Direct Voice and Screen Capture Through a Local Plugin and CLI

**Files:**
- Create: `plugins/local_presence/__init__.py`
- Create: `plugins/local_presence/manifest.yaml`
- Create: `plugins/local_presence/provider.py`
- Modify: `hermes_cli/voice.py`
- Create: `hermes_cli/presence.py`
- Modify: `hermes_cli/commands.py`
- Modify: `hermes_cli/main.py`
- Modify: `cli.py`
- Create: `tests/plugins/test_local_presence.py`
- Create: `tests/hermes_cli/test_presence.py`

**Interfaces:**
- Produces provider `local.presence.v1`; `build_parser()`, `presence_command(args) -> int`, `run_argv(argv, output_mode) -> CommandResult`, `run_slash(rest) -> str`; commands `hermes presence` and `/presence` with `status|providers|start|pause|resume|takeover|stop|consent|retention|doctor`.
- Consumes Tasks 1–5, existing local recorder/silence/TTS primitives in `hermes_cli.voice`, current config helpers, and canonical command registry.

- [ ] **RED: write direct-session and command-contract tests**

```python
def test_start_requires_previewed_exact_consent(cli):
    preview = cli.run("presence start direct --mic")
    assert preview.requires_confirmation
    assert preview.sensors == ("microphone",)
    assert cli.capture_calls == 0
    cli.authority_change()
    applied = cli.run(f"presence consent apply {preview.preview_id}")
    assert applied.exit_code != 0
    assert cli.capture_calls == 0


def test_indicator_loss_closes_local_capture_before_next_frame(local_provider):
    session = local_provider.open_with_visible_indicator()
    session.indicator.hide()
    session.capture_tick()
    assert session.captured_after_indicator_loss == 0
    assert session.remote_frames == 0
```

- [ ] **Run RED**

Run: `scripts/run_tests.sh tests/plugins/test_local_presence.py tests/hermes_cli/test_presence.py -q`

Expected: FAIL because local presence provider and command service do not exist.

- [ ] **Implement the local provider and primary CLI controls**

Wrap `start_recording`, continuous capture, silence callbacks, and `speak_text` behind Task 1 without changing `/voice` behavior. Physical device/process access is lazy and service-gated by microphone/screen capability checks. Screen capture emits bounded low-rate frames only while separately authorized and visible as `LIVE SCREEN`; camera remains unsupported in this plugin until a concrete local camera consumer passes the same gate. Capture callbacks copy into bounded queues and zero/drop payload references after routing.

`hermes presence start direct --mic [--screen]` always emits an exact preview first: sensors, provider/destination, local gate, tool capability, recording/transcription/memory all-off defaults, expiry, estimated network/cost, and indicator form. Apply uses a one-time preview hash/current authority. `pause`, `takeover`, and `stop` bypass both active-session guards through `should_bypass_active_session()` and dispatch inline. `retention record|transcribe|memory on` previews and authorizes only that toggle; turning off closes the sink immediately. `doctor` checks provider/dependency/device/indicator/authority/flow/recording-dir health without opening a sensor.

The classic CLI renders a persistent high-contrast `[LIVE MIC]`, `[LIVE SCREEN]`, `[PAUSED]`, and `[RECORDING]` status region plus start/stop tones. It never relies on a spinner or transient log line. If rendering fails or stdout detaches, the indicator lease invalidates and capture pauses.

- [ ] **Run GREEN**

Run: `scripts/run_tests.sh tests/plugins/test_local_presence.py tests/hermes_cli/test_presence.py tests/tools/test_voice_mode.py tests/tools/test_voice_cli_integration.py tests/hermes_cli/test_commands.py -q`

Expected: PASS; direct voice and screen fixtures flow only after exact consent, indicators remain continuous, existing `/voice` stays compatible, and emergency controls work while an agent is busy.

- [ ] **Commit**

```bash
git add plugins/local_presence hermes_cli/voice.py hermes_cli/presence.py hermes_cli/commands.py hermes_cli/main.py cli.py tests/plugins/test_local_presence.py tests/hermes_cli/test_presence.py
git commit -m "feat: add local live presence controls"
```

---

### Task 7: Adapt Discord Voice Receive and Mixer to the Shared Provider

**Files:**
- Create: `plugins/platforms/discord/realtime_provider.py`
- Modify: `plugins/platforms/discord/adapter.py`
- Modify: `plugins/platforms/discord/voice_mixer.py`
- Create: `tests/gateway/test_discord_realtime_provider.py`
- Modify: `tests/gateway/test_discord_voice_mixer.py`
- Modify: `tests/integration/test_voice_channel_flow.py`

**Interfaces:**
- Produces provider `discord.voice.v1`, normalized 20 ms PCM/VAD/turn/interruption events, remote channel indicator acknowledgement, and `interrupt_output()` backed by `VoiceMixer.stop_speech()`.
- Consumes Tasks 1–5, existing `VoiceReceiver`, `VoiceMixer`, Discord join/leave/access checks, STT/TTS registries, and gateway voice callback; the adapter remains the platform/network owner.

- [ ] **RED: write Discord consent, barge-in, and teardown tests**

```python
async def test_discord_never_receives_or_transcribes_before_channel_indicator(harness):
    await harness.join_without_indicator_ack()
    harness.inject_pcm(USER_ID, utterance_pcm())
    assert harness.normalized_frames == []
    assert harness.transcription_calls == 0


async def test_discord_barge_in_stops_mixer_without_leaving_channel(harness):
    await harness.start_authorized()
    harness.mixer.play_speech(long_pcm())
    result = await harness.inject_interrupt(USER_ID, barge_pcm())
    assert result.stop_latency_ms < 300
    assert not harness.mixer.speech_active
    assert harness.voice_client.is_connected()
```

- [ ] **Run RED**

Run: `scripts/run_tests.sh tests/gateway/test_discord_realtime_provider.py tests/gateway/test_discord_voice_mixer.py tests/integration/test_voice_channel_flow.py -q`

Expected: FAIL because Discord voice is not a `RealtimeSessionProvider` and has no shared indicator/consent gate.

- [ ] **Implement the Discord edge adapter**

Wrap guild/channel/user identities into trusted provider sources/sinks, preserve the existing allowed-user/guild checks, and announce `Hermes live: microphone listening; recording/transcription/memory <states>. Use /presence pause or /presence stop.` in the bound text channel before receiver start. Treat the visible bot voice-channel membership plus successful announcement message as the remote indicator lease; message deletion/send failure pauses receiver locally. Participant PCM is normalized before provider-native silence detection; transcription occurs only when its independent toggle is on, while realtime audio may stream without transcript retention.

On human speech during Hermes output, call mixer `stop_speech()` synchronously before scheduling model/provider cancel. Track actual mixer read watermark for barge latency. `pause` keeps connection but pauses receiver, stops speech/ambient indicator, and updates the announcement. `stop` tears down receiver, listen task, mixer, timeout, and provider state idempotently. Reconnect reacquires exact channel/user/indicator/authority/flow and never replays buffered PCM or a prior tool event.

- [ ] **Run GREEN**

Run: `scripts/run_tests.sh tests/gateway/test_discord_realtime_provider.py tests/gateway/test_discord_voice_mixer.py tests/integration/test_voice_channel_flow.py tests/gateway/test_command_bypass_active_session.py -q`

Expected: PASS; Discord supplies continuous normalized frames and sub-300 ms deterministic barge-in, while blocked users, missing indicators, pause, reconnect, and teardown produce zero leaked frames/transcripts.

- [ ] **Commit**

```bash
git add plugins/platforms/discord/realtime_provider.py plugins/platforms/discord/adapter.py plugins/platforms/discord/voice_mixer.py tests/gateway/test_discord_realtime_provider.py tests/gateway/test_discord_voice_mixer.py tests/integration/test_voice_channel_flow.py
git commit -m "feat: adapt Discord live presence"
```

---

### Task 8: Adapt Google Meet Audio, Captions, Screen, and Reconnect

**Files:**
- Create: `plugins/google_meet/realtime/provider.py`
- Modify: `plugins/google_meet/realtime/__init__.py`
- Modify: `plugins/google_meet/realtime/openai_client.py`
- Modify: `plugins/google_meet/meet_bot.py`
- Modify: `plugins/google_meet/process_manager.py`
- Modify: `plugins/google_meet/__init__.py`
- Modify: `tests/plugins/test_google_meet_realtime.py`

**Interfaces:**
- Produces provider `google_meet.realtime.v1`, normalized duplex audio/caption turn events, sampled screen/video observations, `interrupt_output()`, participant-facing indicator acknowledgement, and reconnect discontinuities.
- Consumes Tasks 1–5, current `RealtimeSession`/`RealtimeSpeaker`, Playwright/audio bridge/process lifecycle, item #6/#15 gates, and credentials supplied through existing secret config.

- [ ] **RED: write Meet privacy, frame, interruption, and reconnect tests**

```python
def test_meet_requires_remote_announcement_before_permissions_or_capture(meet_harness):
    meet_harness.chat_announcement_fails = True
    result = meet_harness.start(screen=True, camera=True)
    assert result.state == "paused"
    assert meet_harness.browser_permission_calls == 0
    assert meet_harness.provider_frames == 0


def test_meet_reconnect_drops_old_frames_and_reauthorizes_destination(meet_harness):
    session = meet_harness.start_authorized()
    meet_harness.disconnect_after_input_sequence(12)
    meet_harness.authority.revoke_remote_transmit()
    reopened = meet_harness.reconnect(session.session_id)
    assert reopened.state == "paused"
    assert meet_harness.replayed_sequences == []
    assert meet_harness.second_websocket_connects == 0
```

- [ ] **Run RED**

Run: `scripts/run_tests.sh tests/plugins/test_google_meet_realtime.py -q`

Expected: FAIL because Meet is provider-specific queue/file state without shared consent, video/screen frames, or safe reconnect.

- [ ] **Implement the Meet provider without expanding its model tools**

Wrap existing WebSocket audio deltas/cancel into the shared transport and expose input-audio append/commit events only after local/meeting engagement. Replace provider-specific stable non-secret subprocess env inputs with an explicit profile-local JSON launch contract passed by path; keep environment variables only as internal process transport and secret key bridge, never user-facing configuration. Register through `PluginContext.register_realtime_provider(..., check_fn=...)`; retain existing `meet_join/status/leave/say` compatibility and service gating without adding a core tool.

Before browser microphone/camera/screen permission, send/verify a meeting chat announcement and set bot display state `Hermes • LIVE`; announce exact sensing/record/transcribe/memory state and stop route. If announcement cannot be confirmed, remain paused. Sample screen/camera at the authorized rate, timestamp against audio monotonic clock, emit bounded video/screen frames, and drop on navigation/meeting identity change until item #6/#15 reauthorize. Captions are transient turn events unless transcription retention is separately on; current unconditional `transcript.txt` writes move behind that toggle.

Use local speech/caption VAD to call `response.cancel` and stop the PCM pump immediately. Device loss or WebSocket/process crash closes capture locally, marks uncertainty, tears down bridge/pump/thread idempotently, and reconnects only after new indicator/authority/flow checks; no JSONL/PCM queue replay crosses the discontinuity.

- [ ] **Run GREEN**

Run: `scripts/run_tests.sh tests/plugins/test_google_meet_realtime.py tests/agent/realtime/test_orchestrator.py tests/agent/realtime/test_retention.py -q`

Expected: PASS for audio/video/screen normalization, participant announcement, separate transcript retention, barge-in, device/process loss, stale-consent reconnect, and idempotent cleanup with no old media replay.

- [ ] **Commit**

```bash
git add plugins/google_meet/realtime/provider.py plugins/google_meet/realtime/__init__.py plugins/google_meet/realtime/openai_client.py plugins/google_meet/meet_bot.py plugins/google_meet/process_manager.py plugins/google_meet/__init__.py tests/plugins/test_google_meet_realtime.py
git commit -m "feat: adapt Google Meet live presence"
```

---

### Task 9: Add Native Ink Consent, Indicators, and Emergency Controls

**Files:**
- Modify: `tui_gateway/server.py`
- Modify: `ui-tui/src/gatewayTypes.ts`
- Modify: `ui-tui/src/app/slash/commands/session.ts`
- Create: `ui-tui/src/components/PresenceStatus.tsx`
- Create: `tests/tui_gateway/test_presence_rpc.py`
- Create: `ui-tui/src/__tests__/presenceCommand.test.ts`
- Create: `ui-tui/src/__tests__/presenceStatus.test.tsx`
- Modify: `ui-tui/src/__tests__/slashParity.test.ts`

**Interfaces:**
- Produces JSON-RPC `presence.exec`, events `presence.state`, `presence.consent_request`, `presence.latency`, and request `presence.indicator_ack`; native `/presence` routing and persistent `PresenceStatus`.
- Consumes Task 6 `run_argv`, orchestrator snapshots, existing session interrupt priority lane, and TUI transport lifecycle; it does not create a second session store.

- [ ] **RED: write RPC and persistent-indicator tests**

```python
def test_pause_and_takeover_run_while_prompt_is_busy(rpc, running_prompt):
    paused = rpc.call("presence.exec", {"argv": ["pause"]})
    assert paused["state"] == "paused"
    assert running_prompt.mission_cancelled is False


def test_disconnect_revokes_indicator_and_pauses_capture(rpc, live_session):
    rpc.disconnect()
    assert live_session.capture_gate_closed
    assert live_session.frames_after_disconnect == 0
```

```tsx
it('shows every active sensor and retention state until acknowledged stop', () => {
  render(<PresenceStatus snapshot={liveSnapshot} />)
  expect(screen.getByText(/LIVE MIC/)).toBeVisible()
  expect(screen.getByText(/LIVE SCREEN/)).toBeVisible()
  expect(screen.getByText(/RECORDING OFF/)).toBeVisible()
})
```

- [ ] **Run RED**

Run: `scripts/run_tests.sh tests/tui_gateway/test_presence_rpc.py -q`

Expected: FAIL because presence RPC/events are absent.

Run: `cd ui-tui && npm test -- --run src/__tests__/presenceCommand.test.ts src/__tests__/presenceStatus.test.tsx src/__tests__/slashParity.test.ts`

Expected: FAIL because native command routing and `PresenceStatus` are absent.

- [ ] **Implement one primary Ink control surface**

Route `presence.exec` in the live gateway process, not `_SlashWorker`, so it shares orchestrator state and bypasses long-running RPC serialization for pause/takeover/stop. Consent request payloads contain only exact sensors/destination/purpose/expiry/toggles/cost estimates and preview hash; apply requires explicit user input and current hash. `presence.indicator_ack` creates/renews the lease only after the component is mounted and visible.

Render a fixed, high-contrast status row outside scrollback/spinner: `LIVE MIC`, `LIVE CAMERA`, `LIVE SCREEN`, `REMOTE`, `RECORDING`, `TRANSCRIBING`, and `MEMORY` independently, plus provider/latency and pause/takeover key hints. It cannot be hidden by tool activity. Resize, render error, gateway disconnect, app suspend, or lost acknowledgement pauses capture. Dashboard inherits this through its embedded TUI; no secondary React chat/control surface or Desktop dependency is added.

- [ ] **Run GREEN**

Run: `scripts/run_tests.sh tests/tui_gateway/test_presence_rpc.py tests/hermes_cli/test_presence.py tests/test_tui_gateway_server.py -q`

Expected: PASS; consent is structured/profile-local and emergency controls remain responsive during model/tool work.

Run: `cd ui-tui && npm test -- --run src/__tests__/presenceCommand.test.ts src/__tests__/presenceStatus.test.tsx src/__tests__/slashParity.test.ts && npm run typecheck`

Expected: PASS; every sensor/retention state remains visible and disconnect/render loss closes its indicator lease.

- [ ] **Commit**

```bash
git add tui_gateway/server.py ui-tui/src/gatewayTypes.ts ui-tui/src/app/slash/commands/session.ts ui-tui/src/components/PresenceStatus.tsx tests/tui_gateway/test_presence_rpc.py ui-tui/src/__tests__/presenceCommand.test.ts ui-tui/src/__tests__/presenceStatus.test.tsx ui-tui/src/__tests__/slashParity.test.ts
git commit -m "feat: add Ink live presence controls"
```

---

### Task 10: Prove Real-Path Media, Recovery, Security, and Cache Invariants

**Files:**
- Create: `tests/integration/test_live_presence_e2e.py`
- Create: `tests/agent/realtime/test_security.py`
- Modify: `tests/test_get_tool_definitions_cache_isolation.py`
- Modify: `tests/agent/test_system_prompt.py`
- Modify: `tests/agent/test_turn_finalizer_interrupt_alternation.py`
- Modify: `tests/hermes_cli/test_profiles.py`

**Interfaces:**
- Produces no production API; this is the release gate for Tasks 1–9.
- Consumes real temp-`HERMES_HOME` config/SQLite/plugin discovery, deterministic checked-in media, real local orchestration/CLI/RPC imports, item #6/#12/#15 implementations, and fake physical/provider/platform boundaries only.

- [ ] **RED: write the real-path fault and privacy matrix**

```python
@pytest.mark.parametrize("fault", [
    "crash_before_indicator_ack", "crash_after_capture_before_send",
    "crash_after_send_before_ack", "reconnect_with_stale_authority",
    "reconnect_with_changed_sink", "duplicate_provider_event",
    "out_of_order_frame", "microphone_loss", "screen_permission_revoked",
    "websocket_drop", "discord_channel_move", "meeting_identity_change",
    "indicator_render_loss", "record_disk_full", "transcriber_unavailable",
    "memory_delete_partial", "tool_unknown_effect", "receipt_write_failure",
])
def test_fault_converges_without_hidden_sensing_replay_or_false_success(presence_e2e, fault):
    result = presence_e2e.run_fault(fault)
    assert result.unindicated_sensor_intervals == 0
    assert result.unauthorized_remote_frames == 0
    assert result.replayed_frames == 0
    assert result.false_verified_receipts == 0
    assert result.final_state in {"paused", "stopped", "failed"}
```

```python
@pytest.mark.parametrize("attack", [
    "remote_text_grants_consent", "model_event_enables_recording",
    "plugin_forges_indicator", "cross_profile_session_id",
    "frame_digest_mismatch", "oversized_frame", "sequence_replay",
    "unicode_destination_confusable", "screen_redirect_changes_sink",
    "transcript_promotes_memory", "recording_path_symlink",
    "tool_event_after_takeover", "audit_contains_pcm",
])
def test_attack_never_crosses_sensor_retention_or_tool_gate(presence_e2e, attack):
    result = presence_e2e.attempt_attack(attack)
    assert result.sensor_or_sink_calls == 0
    assert result.audit_contains_raw_content is False
```

- [ ] **Run RED**

Run: `scripts/run_tests.sh tests/integration/test_live_presence_e2e.py tests/agent/realtime/test_security.py -q`

Expected: FAIL at each unwired real boundary; repair only the owning Task 1–9 module, never weaken a hard floor.

- [ ] **Implement the complete offline E2E harness and invariant checks**

Start from a temporary `HERMES_HOME`; write real `config.yaml`; open real `SessionDB`, autonomy/flow/receipt/realtime stores; load plugins through the real manager; invoke `hermes_cli.presence.run_argv`; route through real TUI RPC dispatch; use real media model validation, local gate, retention filesystem, reconnect, and receipt claim builder. Hardware/provider doubles expose only capture/playback/WebSocket/Discord/Playwright boundaries and record exact calls/monotonic timestamps/bytes.

Hash system message, effective tool definitions, primary provider, and primary model before/after start, every sensor event, consent change, frame, barge-in, tool event, pause/takeover, retention toggle, reconnect, purge, and recovery. Assert all four stable; strict role alternation; no media/flow/consent sidecar enters API messages; no history mutation outside compression; and provider/model changes require the existing explicit new-conversation boundary. Assert session/profile/sink IDs never cross, raw PCM/video/screen/transcript/tool args never enter audit or receipt claims, and remote provider cost/usage stays session-ledger-only.

- [ ] **Run GREEN**

Run: `scripts/run_tests.sh tests/agent/realtime tests/integration/test_live_presence_e2e.py tests/plugins/test_local_presence.py tests/plugins/test_google_meet_realtime.py tests/gateway/test_discord_realtime_provider.py tests/hermes_cli/test_presence.py tests/tui_gateway/test_presence_rpc.py tests/hermes_cli/test_profiles.py -q`

Expected: PASS; every crash/reconnect/replay/device-loss/privacy/security case converges paused/stopped without unindicated sensing, stale replay, retention leak, or false success.

Run: `scripts/run_tests.sh tests/test_get_tool_definitions_cache_isolation.py tests/agent/test_system_prompt.py tests/agent/test_turn_finalizer_interrupt_alternation.py -q`

Expected: PASS with byte-stable prompt/tool schema/provider/model and valid roles.

- [ ] **Commit**

```bash
git add tests/integration/test_live_presence_e2e.py tests/agent/realtime/test_security.py tests/test_get_tool_definitions_cache_isolation.py tests/agent/test_system_prompt.py tests/agent/test_turn_finalizer_interrupt_alternation.py tests/hermes_cli/test_profiles.py
git commit -m "test: prove live presence boundaries"
```

---

### Task 11: Run the Frozen Proof, Document Incubation, and Gate Rollout

**Files:**
- Create: `benchmarks/realtime/runner.py`
- Create: `benchmarks/realtime/score.py`
- Modify: `tests/benchmarks/test_live_presence_benchmark.py`
- Create: `website/docs/user-guide/features/live-presence.md`
- Create: `website/docs/development/realtime-providers.md`
- Modify: `website/docs/reference/cli-commands.md`
- Modify: `website/docs/reference/slash-commands.md`
- Modify: `website/sidebars.ts`

**Interfaces:**
- Produces `run_session_plan(manifest_path, sessions_path, mode, output_dir)`, `score_runs(baseline_path, candidate_path) -> LivePresenceReport`, local `results.jsonl`, `report.json`, and `report.md`.
- Consumes Task 0's frozen corpus, Tasks 1–10 implementation, current push-to-talk baseline, and no telemetry/network upload.

- [ ] **RED: write exact scorer, safety-floor, and denominator tests**

```python
def test_score_requires_all_users_workflows_and_hard_floors(report_factory):
    report = score_runs(*report_factory(sessions=20, users=5, workflows={
        "direct_voice": 7, "meeting": 7, "screen_assisted": 6,
    }))
    assert report.first_audio_p95_ms < 1000
    assert report.barge_in_p95_ms < 300
    assert report.preference_rate >= 0.60
    assert report.hard_floor_failures == ()


def test_one_privacy_error_or_missing_session_is_nonpassing(report_factory):
    privacy = score_runs(*report_factory(privacy_comprehension_errors=1))
    missing = score_runs(*report_factory(sessions=19))
    assert not privacy.passed and privacy.stop_reasons == ("privacy_comprehension_errors=1",)
    assert not missing.passed and "required_sessions=20 actual=19" in missing.stop_reasons
```

- [ ] **Run RED**

Run: `scripts/run_tests.sh tests/benchmarks/test_live_presence_benchmark.py -q`

Expected: FAIL because runner/scorer/report types are absent.

- [ ] **Implement local collection and exact scoring**

Runner validates the manifest before device access, displays consent, executes baseline/candidate in frozen order, and records one redacted row per session plus frame/event timing rows. It records every defined metric, denominator, exclusion, comprehension answer correctness, hardware/network class, application bytes, battery start/end/power state, provider usage/cost, consent/indicator intervals, tool/retention outcomes, and receipt IDs/digests. Raw media/transcript capture remains off unless that specific proof session independently opts in; local proof output is outside git.

Scorer computes p50/p95 using the frozen nearest-rank method, Wilson 95% intervals for rates, per-workflow and hardware/network slices, paired preference, candidate-minus-baseline tool/battery/network/cost deltas, and every hard floor. A retained workflow must meet both latency gates; the overall candidate needs zero hard-floor failures and at least one retained workflow. Underpowered/missing/excluded strata are inconclusive/non-passing, never silently removed or post-hoc relaxed.

- [ ] **Run GREEN on fixtures and the authorized human protocol**

Run: `python benchmarks/realtime/runner.py --manifest benchmarks/realtime/manifest.yaml --sessions benchmarks/realtime/sessions.yaml --mode fixture --output .local-proof/live-presence-fixture`

Expected: exits 0 after all deterministic direct/Discord/Meet media and injected-fault rows pass without opening physical sensors or networks.

Run after five or more users have explicitly enrolled and received opaque proof IDs: `python benchmarks/realtime/runner.py --manifest benchmarks/realtime/manifest.yaml --sessions benchmarks/realtime/sessions.yaml --mode human --output .local-proof/live-presence-human`

Expected: executes exactly 20 opt-in paired sessions, pauses rather than proceeding when consent/hardware/network metadata is incomplete, and writes only local redacted results.

Run: `python benchmarks/realtime/score.py --baseline .local-proof/live-presence-human/baseline.jsonl --candidate .local-proof/live-presence-human/candidate.jsonl --output .local-proof/live-presence-report.md`

Expected: exits 0 only when all denominators, p95 latency/barge-in gates, zero privacy/sensing/retention/tool hard floors, preference `>= 60%`, false-engagement/tool/retention/battery/network/cost ceilings, and at least one retained workflow pass. Before authorized human sessions exist, this command is expected to report `inconclusive`, not fabricate results.

- [ ] **Write user/operator/provider documentation and incubation rules**

The user guide documents the layman outcome; direct/meeting/screen workflows; exact start preview/apply; persistent sensor/remote/record/transcribe/memory indicators; pause/takeover/stop; participant announcement; provider/device/network/cost status; recording/transcript/memory separation; expiry/delete/export; reconnect/device loss; CLI/Ink routes; no Desktop dependency; and how to return to push-to-talk.

The provider guide includes the exact ABC/types, bounded queue/frame rules, trusted identity and clocks, indicator acknowledgement, item #6/#15 call order, TTS/STT ownership, engagement/VAD, turns/barge-in/tool correlation, reconnect/no-replay semantics, retention/receipt redaction, plugin ownership/service gating, temp-`HERMES_HOME` real-path tests, and complete synthetic audio/video/screen provider example. Vendor providers stay standalone plugins; a third provider is not added to core merely to generalize the ABC.

Rollout is explicitly incubated:

1. Land ABC/store/policy with providers disabled and run fixture/security proof.
2. Enable push-to-talk direct voice for opt-in test profiles; recording/transcription/memory remain off.
3. Admit hands-free direct voice only after its local gate meets false-engagement, privacy, battery, network, latency, and barge-in floors.
4. Admit Discord and Meet separately only after participant indicator/announcement and reconnect/device-loss lanes pass.
5. Admit screen/camera separately per provider and hardware class; lower rate/local-only or remove when network/battery ceilings fail.
6. Stop the affected lane immediately on one unindicated sensing/retention interval, privacy-comprehension error, unauthorized remote frame/tool start, cross-profile media, stale consent permit, replay after reconnect, false verified receipt, audit raw-content leak, cache/schema/provider/model drift, role violation, or cost cap breach.
7. Narrow hands-free to explicit push-to-talk if preference is below 60%, false engagement exceeds 0.5/hour, a user exceeds 2/hour, or battery/network ceiling fails. A failure in one workflow does not authorize weakening another workflow's gate.
8. Roll back by setting `realtime.enabled: false` or disabling one provider in `config.yaml`, then invoke `hermes presence stop --all`; close local capture first, preserve redacted audit/receipts for diagnosis, purge opted-in retained media through the retention service, and never delete `state.db` or rewrite conversation history.

- [ ] **Run final verification**

Run: `scripts/run_tests.sh tests/agent/realtime tests/integration/test_live_presence_e2e.py tests/benchmarks/test_live_presence_benchmark.py tests/plugins/test_local_presence.py tests/plugins/test_google_meet_realtime.py tests/gateway/test_discord_realtime_provider.py tests/hermes_cli/test_presence.py tests/tui_gateway/test_presence_rpc.py -q`

Expected: PASS.

Run: `cd ui-tui && npm test -- --run src/__tests__/presenceCommand.test.ts src/__tests__/presenceStatus.test.tsx src/__tests__/slashParity.test.ts && npm run typecheck`

Expected: PASS.

Run: `cd website && npm run lint:diagrams && npm run typecheck && npm run build`

Expected: PASS with resolved live-presence/provider/reference links.

Run: `scripts/run_tests.sh`

Expected: full Python suite PASS under CI-parity isolation.

Run: `git diff --check`

Expected: no whitespace errors.

- [ ] **Commit**

```bash
git add benchmarks/realtime/runner.py benchmarks/realtime/score.py tests/benchmarks/test_live_presence_benchmark.py website/docs/user-guide/features/live-presence.md website/docs/development/realtime-providers.md website/docs/reference/cli-commands.md website/docs/reference/slash-commands.md website/sidebars.ts
git commit -m "docs: incubate live presence"
```

---

## Final Verification Matrix

| Contract | Required proof |
|---|---|
| Shared narrow waist | One model-invisible `RealtimeSessionProvider` ABC normalizes duplex audio/video/screen, VAD/turn/interruption/tool/latency/consent events; local, Discord, and Meet use it |
| Sensor visibility | Capture cannot arm without a current acknowledged indicator lease; local/Ink indicators persist and remote meeting/channel announcement is confirmed |
| Independent retention | Recording, transcription, and memory are separate default-off authority/flow decisions; all eight combinations, expiry, purge, and crash are tested |
| User control | Pause/takeover/stop bypass both busy guards; p95 local gate closure `< 100 ms`; no post-pause frame/tool start; mission state and effect certainty remain intact |
| Local engagement | Local gate precedes remote frames where supported; uncertain/unsupported becomes push-to-talk; false engagement `<= 0.5/hour` and no user `> 2/hour` |
| Authority and IFC | Every start/resume/reconnect/destination/tool/retention transition imports item #6 and #15 contracts; no local duplicate; remote text cannot grant consent |
| Receipts | Item #12 receives redacted claims/digests only; realtime never selects `verified`; raw media/transcript/tool args never enter receipt/audit |
| Recovery | Crash/reconnect/device loss pauses locally, drops old frames/events, reacquires all leases/gates, and does not retry ambiguous effects |
| Performance | At least one retained workflow has p95 first audio `< 1,000 ms`; all retained workflows have p95 barge-in `< 300 ms` and no sample `>= 1,000 ms` |
| Human proof | Exactly 20 opt-in paired sessions, at least five users, all three workflows, recorded hardware/network, zero privacy errors, and preference `>= 60%` |
| Resource cost | Tool errors stay within five points of baseline with zero unauthorized starts; retention is 100% correct; battery/network/provider cost gates pass per class |
| Security/privacy | Zero unindicated sensing/retention, unauthorized remote frames, cross-profile media, raw audit content, replay permits, or false verified receipts |
| Primary UX | Top-level/classic CLI and native Ink provide complete consent/status/emergency controls; Dashboard relies on embedded Ink; Desktop is not a dependency |
| Cache/conversation | System prompt, effective tool schema, primary provider/model hashes are stable; roles alternate; only compression mutates history |
| Extensibility | Footprint Ladder rung 1 + 4; provider/platform code remains plugin/service-gated; no new model tool or user-facing non-secret environment setting |
| Rollback | Per-provider/global config disable closes capture first, preserves diagnostic metadata, purges opted-in media through the service, and never rewrites history |

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-07-16-live-presence.md`. Two execution options:

1. **Subagent-Driven (recommended)** — use `superpowers:subagent-driven-development`, dispatch a fresh worker per task, and review contract then code quality between tasks.
2. **Inline Execution** — use `superpowers:executing-plans`, implement in batches with checkpoints after Tasks 3, 6, 9, and 11.
