# Canvas-First Workflow Builder Design

**Status:** Approved design direction

## Goal

Rebuild only the Workflows page content into a complete canvas-first visual workflow builder while preserving Hermes’s global dashboard navigation, the AI-first Draft/Refine pipeline, the existing workflow runtime, and the existing React Flow graph model.

## Constraints

- Retain the Hermes global dashboard sidebar; do not recreate the mockup’s application rail.
- Use the already-installed `@xyflow/react` for canvas interactions and HTML5 drag-and-drop. Do not add another graph, drag-and-drop, or component framework.
- Preserve prompt-first creation: raw YAML/JSON is an advanced escape hatch, never the primary path.
- Keep existing backend workflow definitions, triggers, nodes, execution history, validation, and deploy behavior.
- Build all interaction states accessibly: keyboard focus, visible labels, Escape to dismiss overlays, and buttons for every drag-only action.
- No fake configuration tabs: each inspector tab must contain useful current data or controls.

## Information Architecture

### Page shell

The Workflows page has four content regions below the existing top bar:

1. **Workflow rail:** compact left navigation with workflow creation, deployed workflow selection, execution selection, and expandable library groups.
2. **Canvas workspace:** the primary, always-mounted React Flow surface. It owns the visual grid, minimap, fit/zoom controls, node/edge interactions, empty state, and graph selection.
3. **Node palette overlay:** a floating, searchable add-node panel launched from an explicit `Add Node` control and keyboard shortcut. It supports both click-to-add and HTML5 drag/drop.
4. **Node inspector:** a right-side selected-node configuration drawer. It opens only for a selected graph item and closes via an explicit control or Escape.

The validation/execution panel remains a bottom drawer spanning page content; it defaults collapsed and never reduces the canvas to an unusable viewport.

### AI-first empty canvas

When no workflow is selected, Build mode still mounts the grid canvas and React Flow controls. A centered onboarding card contains:

- workflow-name input;
- goal textarea;
- `Generate From Prompt` primary action;
- `Start From Scratch` secondary action;
- draft loading, safe error, assumptions, warnings, accept, and reject states.

The empty canvas does not show a dead-end message. It keeps Add Node available so a user can build manually without leaving the canvas.

### Workflow rail

The rail is a compact, vertically scrollable navigation area with collapsible sections:

- **Workflows:** create/new action and deployed definitions;
- **Executions:** recent executions;
- **Library:** collapsible Triggers, Actions, Logic, and Flow Control groups.

It is navigation and discovery, not the primary node authoring surface. Library items can open the floating palette filtered to that group.

### Node palette

The palette is a modal-like floating panel anchored near the canvas top edge, not a permanent full-height card list. It provides:

- search;
- compact icon-led node tiles;
- group labels matching the workflow rail;
- pointer drag/drop onto the canvas;
- click-to-add at a deterministic visible canvas location;
- Escape/close dismissal;
- accessible buttons and text labels alongside icon tiles.

Existing `dataTransfer` plus `screenToFlowPosition()` remain the placement mechanism. Click-to-add centers the new item in the current viewport without changing the current viewport transform.

### Canvas

The canvas owns the grid and is visible for empty, draft, and deployed workflows. It provides:

- square grid background through CSS on the React Flow pane;
- React Flow zoom, pan, fit view, and styled minimap;
- snap-to-grid for node movement and palette drops;
- contextual selected-node presentation;
- source/target handles with clear connection affordance;
- labeled edges, especially switch/parallel branch edges;
- right-click node context menu with duplicate and delete actions;
- a canvas toolbar with Add Node, Fit View, and toggleable minimap controls.

Initial fitting occurs only after current nodes have rendered. Dropping or manually moving a node must not auto-fit the viewport afterward.

### Nodes and edges

Nodes are compact icon-led shells rather than broad colored-header cards. Each presents an icon, concise title, node-type label, connection handles, and an optional execution state. The visual system supports idle, selected, running, succeeded, and failed states.

Edges use a consistent themed stroke and arrow treatment. Switch and parallel edges expose their branch label. Double-clicking an eligible edge edits that label; keyboard-accessible edge selection offers the same action.

### Inspector

The inspector title is `Node Configuration` and includes a Close button. It has three tabs:

- **Parameters:** existing type-specific editable fields and Apply/Delete actions.
- **Input / Output:** trigger input schema, agent result contract, pass/fail output, send-message target/message, or a node-appropriate read-only statement when no mapped input/output model exists.
- **Settings:** ID, type conversion, retry/catch where supported, and Advanced JSON.

The inspector remains the only detailed editor. It must not duplicate the canvas, workflow rail, or bottom drawer.

## Interaction and data flow

- Draft/Refine remains the source of AI-created specs.
- Canvas state maps a workflow spec plus `nodePositions` to React Flow nodes/edges.
- Palette drag/drop converts screen coordinates with `screenToFlowPosition()` and writes the resulting position to `nodePositions`.
- Node drag persists positions after movement ends; snap-to-grid normalizes coordinates.
- Node selection opens the inspector; close clears selection without changing the spec.
- Connecting handles updates the workflow edge model; edge labels update existing switch/parallel routing fields where supported.
- Context actions mutate the same active draft/spec path as inspector actions.
- Validation and deploy continue to operate on the active draft; deploy does not start execution.

## Execution phases

### Phase 1 — Canvas-first foundation

Always-mounted grid canvas, AI-first onboarding card, compact workflow rail, full-height canvas geometry, bottom drawer behavior, and regression tests for empty/draft layouts.

### Phase 2 — Palette and node visual system

Floating searchable palette, compact draggable/clickable tiles, grouped rail library launchers, snap-to-grid placement, redesigned node shells/handles/selected and execution states, and styled minimap/controls.

### Phase 3 — Inspector and graph authoring

Tabbed node inspector with close behavior, type-appropriate Parameters/Input & Output/Settings content, context menu, keyboard actions, edge labels, branch affordances, duplicate/delete, and connection feedback.

### Phase 4 — Quality, responsiveness, and proof

Responsive content-only page behavior, keyboard and screen-reader audit, visual regression coverage at desktop and constrained heights, interaction tests for drag/drop/connection/context actions, full build/test verification, and live dashboard verification.

## Explicit completion criteria

- A first-time user can create a workflow from an AI prompt without seeing YAML.
- A manual user can open Add Node, drag a node to a precise grid position, connect it, select it, configure it, and remove/duplicate it from the canvas.
- The canvas remains the largest page region regardless of whether a workflow is selected.
- No inspector overlap, nested-scroll trap, stale bundle, or small/cut-off React Flow viewport occurs.
- Workflow validation, deployment, history, and existing runtime execution semantics remain intact.
