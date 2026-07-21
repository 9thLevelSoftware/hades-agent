# Complete Canvas-First Workflow Builder Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver a complete canvas-first Workflows page: AI-first onboarding on an always-visible graph canvas, compact workflow rail, floating node palette, polished graph authoring, tabbed node configuration, and responsive/accessibility-complete interactions.

**Architecture:** Preserve the existing Hermes dashboard shell, workflow backend, AI Draft/Refine pipeline, `@xyflow/react` renderer, and active-draft state flow. Recompose the page content through the existing modular dashboard files: `app.js` remains state owner/orchestrator, while `palette.js`, `inspector.js`, `canvas-nodes.js`, `topbar.js`, and `panels.js` own focused render surfaces. Use native HTML5 drag events plus React Flow’s `screenToFlowPosition()`—no additional drag/drop or graph dependency is required.

**Tech Stack:** JavaScript IIFE dashboard plugin, React runtime supplied by the Hermes dashboard SDK, `@xyflow/react`, native HTML5 drag/drop, CSS Grid/Flexbox, Vitest, pytest dashboard asset/build/plugin tests.

## Global Constraints

- Modify only Workflows page content; retain the Hermes global dashboard navigation.
- Preserve prompt-first Draft/Refine creation; YAML/JSON remains an optional advanced escape hatch.
- Preserve workflow spec/runtime/deploy semantics; frontend work must not silently alter dispatcher behavior.
- Reuse installed `@xyflow/react` and native `DragEvent`/`DataTransfer`; do **not** add another drag/drop, graph, CSS, or component framework.
- Click-to-add must remain available anywhere drag/drop is offered.
- Use visible labels, native buttons, keyboard focus, Escape dismissal, and text status—not color alone.
- A canvas fit happens only after nodes render; palette drops and manual node drags must never auto-fit afterward.
- Build dashboard artifacts exclusively with `npm run build:workflows --workspace web` from the repository root so tracked `dist/` remains reproducible.
- Worktree used by this plan: `/Users/christopherwilloughby/.hermes/hermes-agent/.worktrees/workflow-builder-ui-restructure`.

## File Structure

| File | Responsibility after this work |
| --- | --- |
| `plugins/workflows/dashboard/src/app.js` | Own page state, React Flow callbacks, graph viewport/selection/context actions, compose rail/canvas/palette/inspector/drawer. |
| `plugins/workflows/dashboard/src/workspace.js` | Pure helpers for viewport-centered placement, grid snapping, and workflow rail grouping. |
| `plugins/workflows/dashboard/src/palette.js` | Compact workflow rail and floating searchable node palette; drag/click creation affordances. |
| `plugins/workflows/dashboard/src/canvas-nodes.js` | Compact icon-led node rendering, state classes, handles, and node labels. |
| `plugins/workflows/dashboard/src/inspector.js` | Tabbed node configuration renderer with Parameters, Input & Output, Settings. |
| `plugins/workflows/dashboard/src/topbar.js` | Page-local global actions plus Add Node, Fit View, and minimap visibility controls. |
| `plugins/workflows/dashboard/src/style.css` | Single authoritative content layout, graph visuals, rail/palette/inspector/drawer, responsive rules. |
| `plugins/workflows/dashboard/src/*test.js` | Unit/source-contract coverage for new pure helpers and render surfaces. |
| `tests/plugins/test_workflows_dashboard_{plugin,assets,build}.py` | Integration/source markers and canonical bundle reproducibility checks. |

---

## Phase 1 — Canvas-First Foundation

### Task 1: Add pure placement and library-group helpers

**Files:**
- Modify: `plugins/workflows/dashboard/src/workspace.js`
- Modify: `plugins/workflows/dashboard/src/workspace.test.js`

**Interfaces:**
- Produces `snapFlowPosition(position, gridSize = 20)` returning `{x, y}` with each finite coordinate rounded to the nearest grid interval.
- Produces `centeredFlowPosition(viewport, nodeSize = { width: 160, height: 88 })` returning graph coordinates centered in the supplied `{x, y, width, height, zoom}` viewport.
- Produces `libraryGroups(categories)` returning only category metadata `{name, color, types}` for the rail.

- [ ] **Step 1: Write failing helper tests**

```js
import { centeredFlowPosition, libraryGroups, snapFlowPosition } from "./workspace.js";

it("snaps a point to the configured flow grid", () => {
  expect(snapFlowPosition({ x: 109, y: 51 }, 20)).toEqual({ x: 100, y: 60 });
});

it("centers a new node in the current flow viewport", () => {
  expect(centeredFlowPosition({ x: 100, y: 50, width: 800, height: 600, zoom: 2 }))
    .toEqual({ x: 260, y: 180 });
});

it("makes rail groups from the existing node categories without node descriptions", () => {
  expect(libraryGroups([{ name: "Triggers", color: "trigger", nodes: [["manual", "Manual", ""]] }]))
    .toEqual([{ name: "Triggers", color: "trigger", types: ["manual"] }]);
});
```

- [ ] **Step 2: Run the focused test to verify RED**

Run:

```bash
cd /Users/christopherwilloughby/.hermes/hermes-agent/.worktrees/workflow-builder-ui-restructure
npm run test:workflows --workspace web -- src/workspace.test.js
```

Expected: FAIL because the three named exports do not exist.

- [ ] **Step 3: Implement the helpers without browser state**

```js
export function snapFlowPosition(position, gridSize = 20) {
  const size = Number.isFinite(gridSize) && gridSize > 0 ? gridSize : 20;
  const x = Number.isFinite(position && position.x) ? position.x : 0;
  const y = Number.isFinite(position && position.y) ? position.y : 0;
  return { x: Math.round(x / size) * size, y: Math.round(y / size) * size };
}
```

Implement `centeredFlowPosition()` by converting viewport pixels into flow-space with `zoom`, subtracting half the supplied node size, then returning `snapFlowPosition()`. Implement `libraryGroups()` with `Array.isArray()` guards and only the category fields listed above.

- [ ] **Step 4: Run focused tests to verify GREEN**

Run the command from Step 2.

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add plugins/workflows/dashboard/src/workspace.js plugins/workflows/dashboard/src/workspace.test.js
git commit -m "feat(workflows): add canvas placement and library group helpers"
```

### Task 2: Make Build mode an always-mounted AI-first canvas

**Files:**
- Modify: `plugins/workflows/dashboard/src/app.js`
- Modify: `plugins/workflows/dashboard/src/palette.js`
- Modify: `plugins/workflows/dashboard/src/palette.test.js`
- Modify: `plugins/workflows/dashboard/src/workspace-layout.test.js`

**Interfaces:**
- `renderBuildMode()` always renders `renderReactFlowGraph(activeSpec())` or `renderReactFlowGraph(null)`; it never replaces the canvas with plain text.
- `renderPalette(props)` accepts `variant: "rail" | "onboarding"` and renders the existing `draftFromGoal`, `startBlankWorkflow`, accept/reject, and refine controls in the appropriate surface.
- `renderReactFlowGraph(null)` must render an empty React Flow canvas plus an onboarding overlay, not fabricate a workflow spec.

- [ ] **Step 1: Write failing source-contract tests**

```js
it("keeps the React Flow canvas mounted for an empty workflow", () => {
  expect(appSource).toContain("renderReactFlowGraph(activeSpec())");
  expect(appSource).not.toContain("No workflow loaded. Use the sidebar");
});

it("renders an AI-first onboarding surface with both creation paths", () => {
  const output = renderPalette(props({ variant: "onboarding", activeSpec: null }));
  const text = JSON.stringify(output);
  expect(text).toContain("Generate From Prompt");
  expect(text).toContain("Start From Scratch");
});
```

- [ ] **Step 2: Run focused tests to verify RED**

```bash
npm run test:workflows --workspace web -- src/palette.test.js src/workspace-layout.test.js
```

Expected: FAIL because empty Build mode still returns the plain text message and `variant` is absent.

- [ ] **Step 3: Implement empty-canvas composition**

In `app.js`:

```js
function renderBuildMode() {
  return h("section", { id: "hermes-workflows-mode-build", role: "tabpanel", className: "hermes-workflows-build-mode" },
    h("div", { className: "hermes-workflows-canvas-area" },
      h("div", { className: "hermes-workflows-canvas-main" },
        h("div", { className: "hermes-workflows-canvas-wrap" },
          renderReactFlowGraph(activeSpec())
        )
      )
    )
  );
}
```

In the graph renderer, pass `[]` nodes and edges when `spec` is falsy and render a centered `hermes-workflows-canvas-onboarding` overlay. Move the existing creation form into a shared `renderWorkflowForm()` path in `palette.js`, selecting it with `variant === "onboarding"`; do not duplicate Draft/Refine submission logic.

- [ ] **Step 4: Add authoritative layout CSS**

Add one `canvas-onboarding` rule that centers an accessible card above the React Flow pane. It must use a visible input border at rest and preserve the current `Generate From Prompt` and `Start From Scratch` labels.

- [ ] **Step 5: Run focused tests to verify GREEN**

Run the command from Step 2.

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add plugins/workflows/dashboard/src/app.js plugins/workflows/dashboard/src/palette.js plugins/workflows/dashboard/src/palette.test.js plugins/workflows/dashboard/src/workspace-layout.test.js plugins/workflows/dashboard/src/style.css
git commit -m "feat(workflows): keep AI-first onboarding on the canvas"
```

### Task 3: Recompose the permanent sidebar as a compact workflow rail

**Files:**
- Modify: `plugins/workflows/dashboard/src/palette.js`
- Modify: `plugins/workflows/dashboard/src/palette.test.js`
- Modify: `plugins/workflows/dashboard/src/style.css`

**Interfaces:**
- `renderPalette(props)` is renamed or internally split into `renderWorkflowRail(props)` and `renderNodePalette(props)` while preserving the exported `renderPalette` compatibility wrapper until every caller is migrated in the same task.
- Rail receives `openNodePalette(groupName)` and uses it for Library group buttons.
- Rail alone scrolls vertically; its inner sections must not independently scroll.

- [ ] **Step 1: Write failing rail tests**

```js
it("renders compact collapsible rail sections", () => {
  const output = renderPalette(props({ variant: "rail", libraryGroups: [
    { name: "Triggers", color: "trigger", types: ["manual"] },
  ] }));
  const text = JSON.stringify(output);
  expect(text).toContain("Workflows");
  expect(text).toContain("Executions");
  expect(text).toContain("Library");
  expect(text).toContain("Triggers");
});

it("opens the filtered node palette from a library group", () => {
  let opened = null;
  const output = renderPalette(props({ variant: "rail", openNodePalette: (group) => { opened = group; } }));
  const triggerButton = findByText(output, "Triggers");
  triggerButton.props.onClick();
  expect(opened).toBe("Triggers");
});
```

Add the local `findByText()` tree traversal helper only in the test file.

- [ ] **Step 2: Run focused test to verify RED**

```bash
npm run test:workflows --workspace web -- src/palette.test.js
```

Expected: FAIL because the current permanent sidebar is tabbed Workflows/Nodes rather than compact rail sections.

- [ ] **Step 3: Implement the rail**

Use native `<button aria-expanded aria-controls>` disclosure controls. The Workflows section contains a compact `New Workflow` button that focuses/opens the canvas onboarding card and deployed definitions. Executions contains current execution rows. Library maps `libraryGroups` to group buttons, each calling `openNodePalette(group.name)`.

Do not add a new data model: reuse `definitions`, `executions`, and existing `NODE_CATEGORIES` metadata.

- [ ] **Step 4: Replace conflicting sidebar scroll rules**

Make `.hermes-workflows-rail` the one scroll container:

```css
.hermes-workflows-rail {
  min-height: 0;
  overflow-y: auto;
  overscroll-behavior: contain;
}
.hermes-workflows-rail .hermes-workflows-palette-panel {
  overflow: visible;
}
```

Delete obsolete permanent `Nodes`-tab CSS and any nested `overflow-y: auto` on rail children.

- [ ] **Step 5: Run focused test to verify GREEN**

Run the Step 2 command.

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add plugins/workflows/dashboard/src/palette.js plugins/workflows/dashboard/src/palette.test.js plugins/workflows/dashboard/src/style.css
git commit -m "feat(workflows): replace permanent palette with compact workflow rail"
```

---

## Phase 2 — Palette and Graph Visual System

### Task 4: Add a floating searchable node palette with accessible drag and click creation

**Files:**
- Modify: `plugins/workflows/dashboard/src/app.js`
- Modify: `plugins/workflows/dashboard/src/palette.js`
- Modify: `plugins/workflows/dashboard/src/palette.test.js`
- Modify: `plugins/workflows/dashboard/src/workspace.test.js`
- Modify: `plugins/workflows/dashboard/src/style.css`

**Interfaces:**
- `renderNodePalette(props)` accepts `{ isOpen, onClose, initialGroup, addAtViewportCenter, addTriggerAtViewportCenter, addWorkflowCellAtPosition, addTriggerOfType }`.
- `app.js` owns `nodePaletteOpen` and `nodePaletteGroup` state.
- `openNodePalette(groupName = "")` sets both values; `closeNodePalette()` clears them.
- Pressing Escape closes the palette first, then the inspector if open.

- [ ] **Step 1: Write failing palette behavior tests**

```js
it("filters a floating palette by its requested library group", () => {
  const output = renderNodePalette(props({ isOpen: true, initialGroup: "Triggers" }));
  const text = JSON.stringify(output);
  expect(text).toContain("Manual Trigger");
  expect(text).not.toContain("AI Agent Task");
});

it("writes a native DataTransfer payload for drag creation", () => {
  const transfer = { value: "", setData: (_kind, value) => { transfer.value = value; } };
  const output = renderNodePalette(props({ isOpen: true }));
  const manual = findByAriaLabel(output, "Add Manual Trigger");
  manual.props.onDragStart({ dataTransfer: transfer });
  expect(transfer.value).toBe("manual");
});
```

- [ ] **Step 2: Run focused tests to verify RED**

```bash
npm run test:workflows --workspace web -- src/palette.test.js src/workspace.test.js
```

Expected: FAIL because there is no floating palette renderer or palette-open state.

- [ ] **Step 3: Implement palette state and toolbar launch**

In `app.js`, add:

```js
const stateNodePaletteOpen = useState(false);
const nodePaletteOpen = stateNodePaletteOpen[0];
const setNodePaletteOpen = stateNodePaletteOpen[1];
const stateNodePaletteGroup = useState("");
const nodePaletteGroup = stateNodePaletteGroup[0];
const setNodePaletteGroup = stateNodePaletteGroup[1];

function openNodePalette(groupName) {
  setNodePaletteGroup(groupName || "");
  setNodePaletteOpen(true);
}
function closeNodePalette() {
  setNodePaletteOpen(false);
  setNodePaletteGroup("");
}
```

Render the palette as a sibling inside the canvas zone so it overlays only the graph area, not the rail or global navigation.

- [ ] **Step 4: Implement deterministic click placement and snapped drag placement**

Use existing `flowInstanceRef` plus Task 1 helpers:

```js
function addWorkflowCellAtViewportCenter(type) {
  const viewport = flowInstanceRef.current && flowInstanceRef.current.getViewport && flowInstanceRef.current.getViewport();
  addWorkflowCellAtPosition(type, centeredFlowPosition(canvasViewportRect(), viewport));
}
```

For drag/drop, retain `screenToFlowPosition()` and wrap its result in `snapFlowPosition()`. Do not call `fitView()` from a drop path.

- [ ] **Step 5: Add focus and dismissal behavior**

Add one `useEffect` that listens for Escape only while palette or inspector is open. Palette close takes precedence. Give the palette `role="dialog"`, `aria-label="Add workflow node"`, a visible Close button, and restore focus to the Add Node launcher when it closes.

- [ ] **Step 6: Style compact tile palette**

Use icon-led button tiles with text labels and `draggable: true`; do not create a new icon system. Reuse `NODE_ICONS` and `NODE_COLORS`. Add visible drag affordance (`cursor: grab`) and `:active { cursor: grabbing; }`.

- [ ] **Step 7: Run focused tests to verify GREEN**

Run Step 2 command.

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add plugins/workflows/dashboard/src/app.js plugins/workflows/dashboard/src/palette.js plugins/workflows/dashboard/src/palette.test.js plugins/workflows/dashboard/src/workspace.test.js plugins/workflows/dashboard/src/style.css
git commit -m "feat(workflows): add floating draggable node palette"
```

### Task 5: Upgrade React Flow canvas controls, snapping, and compact nodes

**Files:**
- Modify: `plugins/workflows/dashboard/src/app.js`
- Modify: `plugins/workflows/dashboard/src/canvas-nodes.js`
- Modify: `plugins/workflows/dashboard/src/canvas-nodes.test.js`
- Modify: `plugins/workflows/dashboard/src/workspace-layout.test.js`
- Modify: `plugins/workflows/dashboard/src/style.css`

**Interfaces:**
- React Flow receives `snapToGrid: true`, `snapGrid: [20, 20]`, and a controlled `minimapVisible` boolean.
- `makeWorkflowNode(kind, SDK)` produces compact `hermes-workflows-rf-node` markup with icon, title, type, status, and existing source/target handles.
- Canvas toolbar handlers: `openNodePalette`, `fitCanvasView`, `toggleMinimap`.

- [ ] **Step 1: Write failing visual/source tests**

```js
it("renders compact node identity and execution state", () => {
  const Node = makeWorkflowNode("agent_task", sdk);
  const output = Node({ data: { id: "review", status: "running", node: { id: "review", type: "agent_task" } } });
  const text = JSON.stringify(output);
  expect(text).toContain("review");
  expect(text).toContain("agent task");
  expect(text).toContain("running");
});

it("enables grid snapping and uses a 20px grid", () => {
  expect(appSource).toContain("snapToGrid: true");
  expect(appSource).toContain("snapGrid: [20, 20]");
});
```

- [ ] **Step 2: Run focused tests to verify RED**

```bash
npm run test:workflows --workspace web -- src/canvas-nodes.test.js src/workspace-layout.test.js
```

Expected: FAIL because snapping/minimap toolbar are absent and node markup is still broad header/body card styling.

- [ ] **Step 3: Implement canvas controls**

Use already-extracted `Controls` and existing `MiniMap` from the SDK. Add a small canvas-local toolbar above React Flow with:

```js
h("button", { type: "button", onClick: () => openNodePalette("") }, "Add Node"),
h("button", { type: "button", onClick: fitCanvasView }, "Fit View"),
h("button", { type: "button", "aria-pressed": minimapVisible, onClick: () => setMinimapVisible(!minimapVisible) }, "Minimap")
```

`fitCanvasView()` calls `flowInstanceRef.current.fitView({ padding: 0.18, duration: 180 })` only when the instance exists.

- [ ] **Step 4: Implement compact nodes and graph state styles**

Keep `NODE_COLORS`/`NODE_ICONS`. Replace the broad header with a square icon area and concise content:

```js
h("div", { className: "hermes-workflows-rf-node-icon", style: { color } }, icon),
h("div", { className: "hermes-workflows-rf-node-copy" },
  h("strong", null, safeString(label)),
  h("span", null, String(kind).replace(/_/g, " "))
)
```

Do not alter node data shape or remove existing `Handle` behavior. Add CSS classes for `.is-selected`, `.is-status-running`, `.is-status-succeeded`, and `.is-status-failed`.

- [ ] **Step 5: Style minimap, controls, grid, and node states**

Style the existing React Flow components rather than rebuilding them. The canvas pane remains the sole grid provider. Ensure minimap stays inside the canvas, has a themed border/background, and can be toggled off.

- [ ] **Step 6: Run focused tests to verify GREEN**

Run Step 2 command.

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add plugins/workflows/dashboard/src/app.js plugins/workflows/dashboard/src/canvas-nodes.js plugins/workflows/dashboard/src/canvas-nodes.test.js plugins/workflows/dashboard/src/workspace-layout.test.js plugins/workflows/dashboard/src/style.css
git commit -m "feat(workflows): add snap canvas controls and compact graph nodes"
```

---

## Phase 3 — Inspector and Complete Graph Authoring

### Task 6: Convert the inspector into real Parameters, Input & Output, and Settings tabs

**Files:**
- Modify: `plugins/workflows/dashboard/src/app.js`
- Modify: `plugins/workflows/dashboard/src/inspector.js`
- Modify: `plugins/workflows/dashboard/src/inspector.test.js`
- Modify: `plugins/workflows/dashboard/src/style.css`

**Interfaces:**
- `renderInspector(props)` accepts `activeInspectorTab`, `setActiveInspectorTab`, and `closeInspector`.
- Tab ids are exactly `parameters`, `io`, and `settings`.
- `closeInspector()` clears `selectedNode` and any node-focused form message without mutating the workflow spec.

- [ ] **Step 1: Write failing inspector tests**

```js
it("renders all three configuration tabs with a close control", () => {
  const output = renderInspector(props({ selectedNode: { id: "agent", type: "agent_task", specKind: "node" } }));
  const text = JSON.stringify(output);
  expect(text).toContain("Node Configuration");
  expect(text).toContain("Parameters");
  expect(text).toContain("Input / Output");
  expect(text).toContain("Settings");
  expect(text).toContain("Close configuration");
});

it("places agent contracts in the Input / Output tab", () => {
  const output = renderInspector(props({
    activeInspectorTab: "io",
    selectedNode: { id: "agent", type: "agent_task", specKind: "node" },
  }));
  expect(JSON.stringify(output)).toContain("Result contract JSON");
});
```

- [ ] **Step 2: Run focused tests to verify RED**

```bash
npm run test:workflows --workspace web -- src/inspector.test.js
```

Expected: FAIL because the inspector has a single property stack and no close control/tabs.

- [ ] **Step 3: Split existing forms into real tab content**

Keep current renderer functions but distribute fields:

| Tab | Content |
| --- | --- |
| Parameters | agent profile/provider/model/title/prompt; trigger type/schedule; switch/wait/message/subworkflow action fields; Apply/Delete. |
| Input / Output | trigger input schema/intake; agent result contract; pass/fail output; send-message target/message; read-only explanatory content for parallel/join. |
| Settings | ID, node type conversion, retry/catch if existing spec fields support them, Advanced JSON. |

Do not add controls for absent backend fields. When a node has no current Input/Output model, render a short unbordered explanation instead of empty fake fields.

- [ ] **Step 4: Wire close and Escape behavior**

In `app.js`:

```js
function closeInspector() {
  setSelectedNode(null);
  setNodeMessage("");
}
```

Pass it as `closeInspector`. Extend the Task 4 Escape effect to call it after palette dismissal is not applicable. Restore focus to the selected node’s React Flow element or the canvas toolbar Add Node button.

- [ ] **Step 5: Style real tab semantics**

Use `<button role="tab">`, `aria-selected`, and an associated `role="tabpanel"`. Inspector scrolls as a single container. The close button uses `aria-label="Close configuration"`.

- [ ] **Step 6: Run focused tests to verify GREEN**

Run Step 2 command.

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add plugins/workflows/dashboard/src/app.js plugins/workflows/dashboard/src/inspector.js plugins/workflows/dashboard/src/inspector.test.js plugins/workflows/dashboard/src/style.css
git commit -m "feat(workflows): add tabbed node configuration inspector"
```

### Task 7: Complete node, edge, and context-menu authoring interactions

**Files:**
- Modify: `plugins/workflows/dashboard/src/app.js`
- Modify: `plugins/workflows/dashboard/src/canvas-nodes.js`
- Modify: `plugins/workflows/dashboard/src/canvas-nodes.test.js`
- Modify: `tests/plugins/test_workflows_dashboard_assets.py`
- Modify: `plugins/workflows/dashboard/src/style.css`

**Interfaces:**
- `duplicateSelectedCell()` clones the selected non-trigger node through the same active-draft update path as palette insertion and selects the clone.
- `onEdgeDoubleClick(event, edge)` opens an in-page edge-label editor; it must not use `window.prompt()`.
- `applyEdgeLabel(edgeId, label)` updates `flowEdges` and corresponding supported switch/parallel branch metadata; unsupported edge types retain no editable label.
- Context menu action ids are `duplicate`, `delete`, and `edit-label` where applicable.

- [ ] **Step 1: Write failing asset/source tests**

```python
def test_workflow_dashboard_has_canvas_authoring_actions() -> None:
    text = BUNDLE.read_text(encoding="utf-8")
    for marker in [
        "onNodeContextMenu",
        "duplicateSelectedCell",
        "onEdgeDoubleClick",
        "applyEdgeLabel",
        "hermes-workflows-edge-label-editor",
    ]:
        assert marker in text
```

Add a focused unit assertion for node accessible labels/handles if `canvas-nodes.test.js` does not already cover both source and target handles.

- [ ] **Step 2: Run focused tests to verify RED**

```bash
npm run test:workflows --workspace web -- src/canvas-nodes.test.js
uv run pytest tests/plugins/test_workflows_dashboard_assets.py -q
```

Expected: FAIL because duplicate and in-page edge label editor do not yet exist.

- [ ] **Step 3: Implement duplicate using existing insertion semantics**

For selected non-trigger nodes, duplicate by creating the same node type with `addSpecNodeAfter`, then copy only fields present in the selected node’s normalized spec. Position the clone with `snapFlowPosition({ x: original.x + 40, y: original.y + 40 })`. Do not clone edges automatically; users explicitly connect the clone.

- [ ] **Step 4: Implement an in-page edge-label editor**

Store:

```js
const stateEdgeLabelEditor = useState({ edgeId: "", label: "", x: 0, y: 0, visible: false });
```

On double click, stop propagation, set that state from the edge, and render a small fixed `<form className="hermes-workflows-edge-label-editor">` with labeled text input, Save, and Cancel. Escape closes it. On Save, call `applyEdgeLabel()` and clear state.

Do not use `window.prompt()`: it blocks browser automation and is inaccessible to the page’s focus model.

- [ ] **Step 5: Extend the existing right-click menu**

Retain `onNodeContextMenu` and add Duplicate for non-trigger nodes. Close the menu after every action and on canvas click. Use the same selected-node/draft mutations as inspector actions; do not build a second graph state store.

- [ ] **Step 6: Add connection feedback and edge styles**

Keep `onConnect` and `upsertSpecEdge`. Add themed edge class/style based on source type, arrowheads, readable label background, and branch labels for supported switch/parallel edges. Preserve the existing rule that trigger-origin edges are not persisted.

- [ ] **Step 7: Run focused tests to verify GREEN**

Run Step 2 commands.

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add plugins/workflows/dashboard/src/app.js plugins/workflows/dashboard/src/canvas-nodes.js plugins/workflows/dashboard/src/canvas-nodes.test.js plugins/workflows/dashboard/src/style.css tests/plugins/test_workflows_dashboard_assets.py
git commit -m "feat(workflows): complete canvas context and edge authoring"
```

---

## Phase 4 — Quality, Responsiveness, and Proof

### Task 8: Finalize responsive layout, drawer behavior, and accessibility

**Files:**
- Modify: `plugins/workflows/dashboard/src/style.css`
- Modify: `plugins/workflows/dashboard/src/workspace.test.js`
- Modify: `plugins/workflows/dashboard/src/workspace-layout.test.js`
- Modify: `tests/plugins/test_workflows_dashboard_plugin.py`

**Interfaces:**
- Desktop (`>= 1280px`): rail + full canvas + contextual inspector.
- Medium (`768px–1279px`): rail can reduce width; inspector becomes an overlay drawer without canvas overlap.
- Small (`<= 767px`): rail and palette are explicit overlays; canvas remains usable; controls keep visible labels.
- Bottom drawer has a bounded expanded height and never reduces the React Flow region below 240px.

- [ ] **Step 1: Write failing layout/accessibility assertions**

```js
it("keeps the canvas usable when the validation drawer expands", () => {
  expect(cssSource).toMatch(/hermes-workflows-bottom-zone[\s\S]*max-height/);
  expect(cssSource).toMatch(/hermes-workflows-build-mode[\s\S]*min-height:\s*240px/);
});

it("defines responsive inspector and rail behavior without changing the global shell", () => {
  expect(cssSource).toMatch(/@media\s*\(max-width:\s*1279px\)/);
  expect(cssSource).toMatch(/hermes-workflows-inspector-zone/);
  expect(cssSource).toMatch(/@media\s*\(max-width:\s*767px\)/);
});
```

Add pytest source assertions that `Add Node`, `Close configuration`, and `aria-label="Add workflow node"` remain in the bundle source.

- [ ] **Step 2: Run focused tests to verify RED**

```bash
npm run test:workflows --workspace web -- src/workspace.test.js src/workspace-layout.test.js
uv run pytest tests/plugins/test_workflows_dashboard_plugin.py -q
```

Expected: FAIL until the final responsive/drawer/ARIA markers exist.

- [ ] **Step 3: Implement responsive rules**

Use only content-page selectors. At medium widths, render inspector as an in-page fixed/right drawer with a backdrop close button; at small widths, the rail becomes a similar overlay launched from a labelled page-local button. Do not touch global dashboard navigation CSS.

Use `max-height: min(36vh, 320px)` for the expanded bottom drawer and retain `min-height: 240px` on Build/canvas chain.

- [ ] **Step 4: Run focused tests to verify GREEN**

Run Step 2 commands.

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add plugins/workflows/dashboard/src/style.css plugins/workflows/dashboard/src/workspace.test.js plugins/workflows/dashboard/src/workspace-layout.test.js tests/plugins/test_workflows_dashboard_plugin.py
git commit -m "fix(workflows): finish responsive canvas and accessibility behavior"
```

### Task 9: Full regression suite, canonical artifact verification, and live rendered proof

**Files:**
- Modify only if failures require a scoped correction: files identified by the failing test.
- Verify: `plugins/workflows/dashboard/dist/index.js`
- Verify: `plugins/workflows/dashboard/dist/style.css`

- [ ] **Step 1: Run canonical frontend build**

```bash
cd /Users/christopherwilloughby/.hermes/hermes-agent/.worktrees/workflow-builder-ui-restructure
npm run build:workflows --workspace web
```

Expected: Vite succeeds and writes `plugins/workflows/dashboard/dist/index.js` and `style.css`.

- [ ] **Step 2: Run all dashboard frontend tests**

```bash
npm run test:workflows --workspace web
```

Expected: all Vitest files pass.

- [ ] **Step 3: Run dashboard API/build/assets tests**

```bash
uv run pytest tests/plugins/test_workflows_dashboard_plugin.py tests/plugins/test_workflows_dashboard_build.py tests/plugins/test_workflows_dashboard_assets.py -q
```

Expected: all tests pass, including canonical tracked-asset reproducibility.

- [ ] **Step 4: Run workflow backend regression tests**

```bash
uv run pytest tests/hermes_cli/test_workflows_*.py -q
```

Expected: all tests pass.

- [ ] **Step 5: Verify a live dashboard rendering**

```bash
uv run hermes dashboard --port 9121 --no-open --skip-build
```

In browser verification:

1. Open `/workflows` with no active workflow and confirm grid + AI onboarding + Add Node control are visible.
2. Create a blank workflow; open floating palette; drag an AI Agent Task to a non-grid-aligned cursor location; confirm the node appears at the snapped drop point.
3. Connect two nodes; select a node; verify inspector tabs and close control.
4. Right-click a node; verify Duplicate and Delete controls.
5. Toggle minimap; expand validation; verify the canvas remains at least 240px high.
6. Check medium viewport behavior and Escape dismissal for palette/inspector.

Capture screenshots only as test evidence; do not commit screenshots unless the existing repository testing convention requires them.

- [ ] **Step 6: Confirm clean artifact and source state**

```bash
git diff --check
git status --short
```

Expected: no whitespace errors; status contains only intended dashboard implementation files and generated tracked `dist/` artifacts.

- [ ] **Step 7: Commit and push**

```bash
git add plugins/workflows/dashboard tests/plugins
git commit -m "feat(workflows): complete canvas-first workflow builder"
git push fork feat/workflow-builder-ui-restructure
```

---

## Plan Self-Review

- **Spec coverage:** Phase 1 covers always-mounted AI-first canvas and compact rail; Phase 2 covers floating palette, drag/drop, minimap, snap/grid, and compact nodes; Phase 3 covers inspector tabs, close behavior, context actions, edge labels, duplicate/delete, and connection feedback; Phase 4 covers responsive behavior, accessibility, canonical assets, backend regression, and live proof.
- **Dependency check:** `@xyflow/react` is already present in `web/package.json`. Native `DataTransfer` plus React Flow coordinate conversion meets the required drag/drop behavior; no additional library is justified.
- **No placeholders:** every task names concrete files, interfaces, RED/GREEN commands, implementation approach, and commit command.
- **Type/interface consistency:** palette state lives in `app.js`; pure placement helpers live in `workspace.js`; `renderInspector` owns configuration tab rendering; React Flow remains the single graph source of truth.

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-07-11-complete-canvas-first-workflow-builder.md`.

Two execution options:

1. **Subagent-Driven (recommended)** — dispatch a fresh subagent per task, review between tasks, and independently verify each task before proceeding.
2. **Inline Execution** — execute the tasks serially in this session with explicit RED/GREEN checkpoints.

Which approach?