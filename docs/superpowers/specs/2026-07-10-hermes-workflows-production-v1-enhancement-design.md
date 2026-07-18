# Hermes Workflows Production v1 Enhancement Design

**Date:** 2026-07-10  
**Status:** Approved design  
**Target baseline:** `9thLevelSoftware/hermes-agent` `fork/main` at `3030e92ae`  
**Source audit:** Hermes Workflows E2E UX/UI, Flow, and Functionality Audit, 2026-07-10

## 1. Purpose

Turn the current Hermes Workflows implementation into a production-quality v1 that is trustworthy to author, publish, operate, and diagnose from the Dashboard.

The current runtime, CLI, model tools, AI drafting/refinement, single execution, cancellation, and continuous-feed lifecycle work end to end. The remaining work is primarily product architecture, interaction correctness, maintainability, accessibility, and release confidence. This design resolves every finding from the 2026-07-10 audit without combining that work with a deeper execution-engine rewrite.

## 2. Product direction

Workflows is an **AI-first orchestration product**:

- Natural-language generation and refinement are the primary authoring path.
- Structured editing and the graph canvas are review, correction, and advanced-control surfaces.
- AI output is never opaque: every runtime-supported behavior remains inspectable and editable without raw YAML or JSON.
- Published workflow versions are immutable.
- Authoring, running, and history are separate modes within one workflow workspace.
- Continuous feeds are operational sessions pinned to a deployed workflow version.

## 3. Scope

### 3.1 In scope

- Close all ten audit findings:
  - continuous-feed layout collapse;
  - incorrect/missing trigger graph nodes after execution;
  - stale E2E provenance assertion;
  - raw HTTP/JSON error copy;
  - misleading Manual Tick feedback;
  - global execution list inside a selected workflow;
  - inaccessible sidebar disclosures and unlabeled YAML editor;
  - missing responsive layout;
  - feed actions that ignore current state;
  - undiscoverable horizontally overflowing node palette.
- Modular, source-first Dashboard plugin frontend with reproducible generated assets.
- AI-first creation and refinement with semantic change review.
- Complete structured authoring for every workflow behavior supported in v1.
- Immutable publish/version lifecycle.
- Build, Run, and History workspace modes.
- Clear continuous-feed lifecycle and operator controls.
- Filtered, actionable execution history.
- Inline validation and stable API error envelopes.
- Responsive and keyboard/screen-reader accessible UI.
- Cross-surface parity among Dashboard, CLI, model tools, API, and dispatcher.
- Deterministic build, focused browser coverage, upgrade coverage, documentation, and release gates.

### 3.2 Explicitly out of scope

These require separate designs and implementation plans:

- Batch item splitting.
- Binary/document attachment storage and document splitting.
- Arbitrary or explicitly marked graph cycles.
- Repeated node-visit identity and true in-execution loops.
- Manual-input nodes inside a running execution.
- A new workflow scheduler or execution engine.
- Moving Workflows out of the Dashboard plugin system.

Unsupported v2 fields must be rejected clearly at validation/publish time. They must not appear as inert controls or partially working promises in v1.

## 4. Non-negotiable product invariants

1. Every execution and feed identifies an immutable workflow version.
2. Draft edits never mutate a published version.
3. Draft workflows cannot execute.
4. Every runtime-supported behavior is available through structured UI.
5. YAML/JSON is import, export, and debugging only.
6. AI-generated workflows never require YAML to understand or repair.
7. The server owns validation and lifecycle rules; the client mirrors them for immediacy.
8. Closed feeds are terminal and immutable.
9. Build, Run, and History remain independently usable at supported viewport sizes.
10. Execution status decoration never changes graph membership or the user's canvas viewport.
11. Sensitive data remains redacted in model tools, Dashboard history, API errors, dedupe storage, and generated task prompts.
12. Existing CLI, model-tool, schedule, and non-continuous workflows remain backward compatible.

## 5. Information architecture

### 5.1 Workflow list

The Workflows landing view lists workflow-level information only:

- name and description;
- draft and published status;
- active published version;
- enabled or disabled state;
- latest execution status and time;
- number of open feeds;
- create, duplicate, disable, archive, and delete actions.

Execution history is not rendered globally in the workflow list. A separate explicit **All Executions** view aggregates all workflows and always displays the workflow name and version on each row.

### 5.2 Workflow workspace

Opening a workflow preserves its identity and provides three primary modes.

#### Build

- AI generation and refinement are the dominant entry point.
- The current unpublished draft is shown as a human-readable summary, structured editor, validation result, and graph.
- The graph canvas is a secondary review/correction surface.
- Publish creates a new immutable version.

#### Run

- The operator chooses a published version.
- Single-intake workflows show a schema-driven run form.
- Continuous workflows show feed sessions, lifecycle controls, queue counters, item composition, item list, and linked execution states.
- Feed operations do not share a vertical flex stack with the graph editor.

#### History

- Defaults to executions for the current workflow.
- Displays version, trigger, status, timestamps, and input summary.
- Execution detail contains the event timeline, node attempts, outputs/errors, cancellation, rerun, and linked feed item.
- The selected execution may decorate a read-only graph for its exact published version. It must not decorate a different published version or a diverged draft. Execution detail remains authoritative.

## 6. Workflow lifecycle

### 6.1 Drafts

- Generation, manual edits, imports, and refinements update a draft.
- Drafts carry dirty state and cannot be silently overwritten by server refresh.
- Reloading while dirty requires an explicit discard or preserve decision.
- Undo applies to accepted draft changes within the current editing session.

### 6.2 Publish

Publish is enabled only when:

- schema and graph validation pass;
- every referenced worker profile exists;
- deterministic capability incompatibilities are absent, while environment-dependent capability warnings are explicitly acknowledged;
- AI assumptions requiring user choice are resolved;
- the draft differs from the latest published version;
- the expected latest version still matches the server.

Publishing creates a new immutable version and presents a semantic change summary. Existing executions and feeds stay pinned to their original versions.

### 6.3 Disable, archive, and delete

- Disable prevents new runs while preserving history.
- Archive removes a workflow from the default active list while preserving definitions and history.
- Delete is destructive only when no retained history depends on the workflow, or through an explicit purge operation with a strong confirmation.

## 7. AI-first authoring

### 7.1 Creation

The New Workflow flow collects:

- goal;
- optional constraints;
- expected runtime inputs;
- optional worker profiles;
- optional output examples.

The user can **Generate draft** or **Start blank**. AI generation returns a complete typed candidate spec plus:

- a plain-language summary;
- assumptions;
- unresolved choices;
- validation results.

An invalid generated draft remains available for repair. The user can invoke **Repair with AI** or edit structured fields directly.

### 7.2 Refinement

A refinement request produces a complete candidate draft. Before acceptance, the UI shows a semantic change summary covering:

- trigger changes;
- input-schema and intake changes;
- nodes added, removed, or changed;
- edge/routing changes;
- worker-profile and workspace changes;
- retry, timeout, and result-contract changes.

The user accepts or rejects the candidate. Acceptance updates the draft only; it never publishes automatically.

### 7.3 Structured authoring coverage

The structured editor supports all v1 schema behavior:

- workflow metadata and enabled state;
- manual and schedule triggers;
- input schema fields and deterministic constraints;
- single and continuous intake;
- dedupe and readiness conditions through guided builders;
- Pass, Agent Task, Switch, Parallel, Join, Wait, and Fail nodes;
- profile, workspace, retry, timeout, and result-contract settings;
- edges, switch routing, success routing, and failure routing.

Common fields appear first. Advanced runtime settings remain structured behind progressive disclosure.

### 7.4 Advanced YAML/JSON

The Advanced menu provides:

- export;
- import into the current draft;
- raw editing for diagnostics.

Raw content is never published or executed directly. It must parse into the typed model and pass the same validation pipeline. The editor has a native accessible label and preserves round-trip fidelity with the structured editor.

## 8. Graph and structured editor behavior

### 8.1 Dedicated graph view models

Persisted trigger and node specs are converted to separate graph view models. A trigger's persisted subtype, such as `manual`, cannot overwrite the React Flow renderer type.

Graph updates are separated into:

- **membership/layout changes:** rebuild nodes and edges, with an intentional fit-view policy;
- **status-only changes:** update node decoration while preserving membership, pan, and zoom.

### 8.2 Canvas interaction

- Selecting a node opens its structured editor.
- Keyboard users can select, connect where supported, and delete nodes.
- Node type is editable through visible structured controls. Changing type opens a preview of incompatible fields that will be removed; the conversion proceeds only after confirmation and produces a valid default configuration for the new type.
- Hidden palette items have wrap, edge-fade, chevrons, or another explicit overflow affordance.
- Validation highlights affected nodes without changing layout.

### 8.3 Responsive behavior

- **Wide (`>= 1280px`):** workflow navigation, primary workspace, optional inspector side panel.
- **Medium (`768px` to `1279px`):** workflow navigation collapses; inspector becomes a side sheet.
- **Narrow (`360px` to `767px`):** one primary pane; canvas, structured editor, and details use tabs or sheets.
- **Short height (`<= 600px`):** editor body retains at least 240px of usable height; inner panels scroll independently.
- Feed operations live in Run mode and cannot collapse Build mode.

The release viewport matrix is `1440x900`, `1280x576`, `1024x768`, `768x1024`, and `390x844`.

## 9. Run mode

### 9.1 Single execution

The run form is generated from the selected published version's trigger input schema.

- Client validation gives immediate feedback.
- Server intake validation is authoritative.
- Invalid input keeps the form open and maps errors to fields.
- No execution row is created until validation succeeds.
- Success navigates to the new execution detail.
- Rerun pre-populates only safe non-sensitive values.

### 9.2 Continuous-feed lifecycle

The feed state machine is:

```text
Open -> Paused -> Open
Open -> Closed
Paused -> Closed
Closed -> terminal
```

#### Open

- New items may be created.
- Existing items may be updated only while their status is `needs_input` or `queued`.
- Dispatcher may admit ready items.

#### Paused

- No new item writes.
- No new item admission.
- Already running executions continue.
- Resume returns the feed to Open.

#### Closed

- No writes, updates, admission, or resume.
- The session remains available for history.
- **Start new feed** creates a distinct feed pinned to a chosen published version.

The UI renders only valid actions for the current state. Closed feeds never show Resume.

### 9.3 Feed workspace

Run mode separates:

- feed identity, version, timestamps, and state;
- state-appropriate controls;
- item composer;
- counts by item status;
- searchable/filterable item list;
- item detail, validation messages, and linked execution.

Queued and running items auto-refresh. Terminal items stop polling unless explicitly refreshed. Feed item status and execution terminal status use one shared backend contract.

## 10. Dispatcher and operator diagnostics

The gateway dispatcher remains the normal execution mechanism. Manual Tick is removed from the primary toolbar and placed under diagnostics.

A manual diagnostic tick reports separate counters:

- schedules admitted;
- feed items admitted;
- executions advanced;
- remaining queued work;
- remaining running/waiting work.

Admission and execution advancement are not conflated. Dispatcher fairness remains covered by a regression test so ready feed items cannot starve older queued executions.

## 11. History and execution operations

History is workflow-filtered by default and supports:

- status, version, trigger, and date filters;
- stable execution-detail URLs;
- input summary;
- ordered event timeline;
- node attempts and linked Kanban tasks;
- redacted outputs and errors;
- Cancel for queued, running, or waiting executions;
- Rerun as a new execution;
- navigation to an associated feed and feed item.

Cancellation is idempotent. Polling preserves event order and does not mutate the Build canvas viewport.

## 12. Frontend architecture

Workflows remains a Dashboard plugin. Authored source moves into focused modules and a reproducible build emits packaged assets.

```text
plugins/workflows/dashboard/
├── src/
│   ├── index.js
│   ├── api/
│   │   ├── client.js
│   │   └── errors.js
│   ├── model/
│   │   ├── workflow.js
│   │   ├── graph.js
│   │   ├── validation.js
│   │   └── execution-status.js
│   ├── workspace/
│   │   ├── workflow-workspace.js
│   │   ├── build-mode.js
│   │   ├── run-mode.js
│   │   └── history-mode.js
│   ├── build/
│   │   ├── ai-authoring.js
│   │   ├── structured-editor.js
│   │   ├── graph-canvas.js
│   │   ├── trigger-editor.js
│   │   └── node-editors/
│   ├── run/
│   │   ├── run-form.js
│   │   ├── feed-session.js
│   │   └── feed-items.js
│   ├── history/
│   │   ├── execution-list.js
│   │   └── execution-detail.js
│   └── components/
│       ├── disclosure.js
│       ├── status-badge.js
│       └── validation-summary.js
├── dist/                    # generated and packaged
└── build configuration
```

This decomposition reuses the current Dashboard plugin SDK, React, and React Flow. It is not a framework migration.

### 12.1 State boundaries

- **Server state:** definitions, versions, feeds, items, executions, node attempts, and events.
- **Draft state:** unpublished spec, AI request, candidate refinement, and unsaved edits.
- **View state:** workspace mode, selected node/run/feed/item, canvas viewport, and open sheet.
- **Action state:** validation, generation, publishing, running, cancellation, and feed transitions.

Server refresh never silently overwrites dirty draft state.

## 13. Backend and API contracts

The typed spec, SQLite persistence, dispatcher, graph engine, CLI/tools, and plugin API remain the system architecture.

Required API capabilities include:

- workflow-filtered, paginated execution listing;
- execution detail with node attempts and events;
- draft validation and capability resolution;
- immutable publish with expected-latest-version conflict detection;
- feed summary counters and paginated items;
- state-machine-enforced feed transitions;
- structured dispatcher diagnostic results.

The implementation may extend existing endpoints or add bounded endpoints. It must not duplicate orchestration logic in the UI or create a Dashboard-only runtime path.

## 14. Errors, conflicts, and recovery

Workflow APIs return a stable user-facing envelope:

```json
{
  "code": "workflow_input_invalid",
  "message": "Repository path is required.",
  "field_errors": {
    "repo_path": ["This field is required."]
  },
  "hint": "Provide a local path or repository URL."
}
```

Rules:

- `message` is concise and actionable.
- `field_errors` map to structured controls.
- `hint` is optional.
- Technical details are available only in a diagnostics disclosure.
- Raw HTTP status plus JSON text is never normal UI copy.
- Publishing detects stale latest-version assumptions.
- Feed transitions reject stale state and return the current state.
- Cancellation and refresh are idempotent.
- Network errors preserve user input and offer retry.

## 15. Accessibility

The v1 release baseline includes:

- native buttons for disclosures and actions;
- `aria-expanded` and `aria-controls` on disclosures;
- native tabs for Build, Run, and History;
- labels for every input and raw editor;
- dialog focus trapping and focus return;
- keyboard-operable graph nodes and palette;
- visible focus indicators;
- concise `aria-live` operation outcomes;
- no status conveyed by color alone;
- accessible names for icon-only controls;
- reduced-motion-safe status updates.

## 16. Security and data integrity

- Mounted Dashboard auth remains authoritative and is pinned by E2E tests.
- Model-facing workflow execution serializers are redacted by default.
- Dashboard history and API errors reuse shared redaction.
- Dedupe storage uses deterministic digests rather than raw sensitive values.
- Workflow input inserted into agent prompts is explicitly framed as untrusted data.
- Raw imports pass typed validation before becoming a draft.
- No draft or imported content directly executes or publishes.
- Existing DB upgrade/read compatibility is tested from a pre-continuous-input fixture.
- No data-loss migration is introduced solely for UI concerns.

## 17. Delivery strategy

### Slice 0: trustworthy baseline

- Update the stale provenance assertion.
- Add failing regression coverage for trigger rendering and feed layout.
- Pin current auth, redaction, runtime, CLI, tool, and API behavior.
- Work from a clean current feature branch/worktree.

**Exit gate:** existing focused suite green; new defect tests fail for the intended reasons before fixes.

### Slice 1: modular frontend foundation

- Add source modules and reproducible build.
- Extract API errors, graph view models, status mapping, and common controls without changing behavior.
- Replace bundle-string confidence with source-level tests where practical.

**Exit gate:** deterministic generated assets, source tests, syntax checks, package build, and existing E2E green.

### Slice 2: graph correctness and workspace shell

- Correct trigger view models and viewport preservation.
- Add Build, Run, and History modes.
- Move feed operations out of Build layout.
- Filter history by workflow.
- Move Manual Tick to diagnostics.

**Exit gate:** graph membership remains stable before/after execution; wide, short-height, and narrow browser tests pass.

### Slice 3: AI-first Build mode

- Implement creation, refinement, semantic change review, undo, validation, complete structured schema editing, and immutable publish.
- Add guided readiness-condition and result-contract editors.
- Preserve Advanced YAML round-trip.

**Exit gate:** prompt -> draft -> structured edit -> refine -> publish -> reload works without YAML.

### Slice 4: Run and feed operations

- Implement schema-driven single-run forms.
- Enforce feed state machine and valid controls.
- Auto-refresh active items and linked executions.
- Add structured diagnostic tick feedback and idempotent cancellation.

**Exit gate:** invalid and valid single runs plus full continuous-feed lifecycle pass E2E.

### Slice 5: History, accessibility, and polish

- Add execution filters, detail, rerun, and linked-resource navigation.
- Complete keyboard and screen-reader behavior.
- Add responsive sheets/tabs and all loading, empty, conflict, and recovery states.
- Improve palette and action discoverability.

**Exit gate:** automated accessibility checks plus keyboard-only and viewport-matrix dogfood pass.

### Slice 6: release hardening

- Update docs and examples.
- Verify DB upgrades and cross-surface parity.
- Run full tests, lint, generated-asset checks, package build, security scanners, and real-provider AI smoke tests.
- Repeat the original E2E audit against the release candidate.

## 18. Testing strategy

### 18.1 Unit and model tests

- trigger/node graph view-model mapping;
- semantic diff generation;
- error-envelope normalization;
- feed state transitions;
- status mapping and terminal synchronization;
- condition and result-contract builders;
- redaction and dedupe hashing.

### 18.2 API and integration tests

- authenticated and unauthenticated mounted plugin access;
- draft validation and publish conflicts;
- filtered execution queries;
- field-error mapping;
- feed lifecycle and stale-state rejection;
- cancellation idempotency;
- CLI/model-tool/Dashboard parity;
- DB upgrade/read compatibility;
- dispatcher fairness and diagnostic counters.

### 18.3 Browser E2E tests

- AI generation and repair of an invalid candidate;
- refine with semantic review and accept/reject;
- complete structured editing of every v1 trigger and node behavior;
- publish and reload immutable version;
- invalid and valid single runs;
- continuous feed open, enqueue, pause, resume, close, and new feed;
- execution cancel and rerun;
- graph node count/type before and after status refresh;
- short-height and narrow-width layout;
- keyboard-only creation and operation;
- YAML editor accessible name and round-trip.

### 18.4 Build and release gates

- Python workflow-focused suite;
- relevant gateway, tool, Dashboard, and DB migration suites;
- JavaScript lint/tests and bundle syntax;
- deterministic plugin asset build;
- Python lint/compile/package build;
- dependency and security scanners;
- clean worktree;
- live real-provider AI generation/refinement smoke test.

## 19. Release acceptance criteria

The release is acceptable only when:

1. All ten findings from the 2026-07-10 audit are closed with regression coverage.
2. No Critical or High findings remain in a fresh audit.
3. Every v1 schema behavior is authorable through structured UI.
4. AI generation/refinement produces understandable, repairable drafts.
5. Published versions are immutable and all runs/feeds show their version.
6. Closed feeds are terminal; a new feed is a distinct session.
7. Build, Run, and History pass wide, short-height, tablet, and narrow viewport tests.
8. Keyboard-only authoring and operation complete successfully.
9. CLI, model tools, API, Dashboard, and dispatcher share validation and status semantics.
10. Full relevant test/build/security gates are green from a clean worktree.
11. No unauthenticated plugin API access or sensitive-data leakage is observed.
12. Batch/documents and true loops are documented as unsupported rather than partially exposed.

## 20. Rejected alternatives

### Patch the existing bundle only

Rejected because it would close defects quickly but preserve the coupled 3,000-line frontend source, making the approved AI-first workspace and future improvements fragile.

### Full v2 in the same program

Rejected because batch/documents and true loops require attachment persistence, splitting rules, repeated node-visit identity, and deeper engine/idempotency changes. Mixing those with UI and release hardening would produce an unreviewable risk surface.

### Move Workflows into the main Dashboard application

Rejected because the plugin boundary already works, is authenticated, and packages successfully. The maintainability problem is authored bundled source, not the plugin architecture itself.

### Allow AI to auto-publish

Rejected because workflow changes can trigger tools, agents, and external side effects. AI creates candidate drafts; explicit publish remains the safety boundary.

## 21. Future design seams

This v1 should leave additive seams for later specifications:

- attachment references in input schemas;
- batch feed creation and split previews;
- document type/size enforcement;
- explicit loop edges;
- node visit/attempt identity;
- in-execution manual input nodes.

These seams must not add dormant v2 controls or schemas to the v1 UI unless validation rejects them clearly.
