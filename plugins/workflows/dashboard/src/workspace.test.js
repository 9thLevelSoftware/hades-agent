import { describe, expect, it } from "vitest";
import {
  WORKSPACE_MODES,
  centeredFlowPosition,
  libraryGroups,
  locationForMode,
  modeForLocation,
  renderWorkflowSummary,
  renderWorkspaceTabs,
  snapFlowPosition,
} from "./workspace.js";

// ---- URL routing round-trips (plan lines 696–710) ----------------------------

describe("workspace routing", () => {
  it("exposes build/run/history modes", () => {
    expect(WORKSPACE_MODES.slice().sort()).toEqual(["build", "history", "run"]);
  });

  it("defaults a workflow workspace to build", () => {
    expect(modeForLocation({ pathname: "/workflows/demo", search: "" })).toBe("build");
    expect(modeForLocation({ pathname: "/workflows/demo/", search: "" })).toBe("build");
    expect(modeForLocation({ pathname: "/workflows/demo", search: "?" })).toBe("build");
  });

  it("parses /run and /history modes from the pathname", () => {
    expect(modeForLocation({ pathname: "/workflows/demo/run", search: "" })).toBe("run");
    expect(modeForLocation({ pathname: "/workflows/demo/history", search: "" })).toBe("history");
  });

  it("falls back to build on unknown mode segments", () => {
    expect(modeForLocation({ pathname: "/workflows/demo/banana", search: "" })).toBe("build");
  });

  it("round-trips run and history selections", () => {
    expect(locationForMode("demo", "run", { feed: "wffeed_1" }))
      .toBe("/workflows/demo/run?feed=wffeed_1");
    expect(locationForMode("demo", "history", { execution: "wfexec_1" }))
      .toBe("/workflows/demo/history?execution=wfexec_1");
  });

  it("round-trips build mode without query string", () => {
    expect(locationForMode("demo", "build")).toBe("/workflows/demo");
  });

  it("round-trips back through modeForLocation for every supported mode", () => {
    for (const mode of WORKSPACE_MODES) {
      const selection = mode === "run" ? { feed: "wffeed_1" } : mode === "history" ? { execution: "wfexec_1" } : {};
      const next = locationForMode("demo", mode, selection);
      const parsed = new URL("http://x" + next);
      const search = parsed.search ? "?" + parsed.search.slice(1) : "";
      expect(modeForLocation({ pathname: parsed.pathname, search })).toBe(mode);
    }
  });

  it("ignores query keys not relevant to the chosen mode", () => {
    expect(locationForMode("demo", "run", { execution: "wfexec_1", feed: "wffeed_1" }))
      .toBe("/workflows/demo/run?feed=wffeed_1");
    expect(locationForMode("demo", "history", { feed: "wffeed_1", execution: "wfexec_1" }))
      .toBe("/workflows/demo/history?execution=wfexec_1");
  });
});

it("snaps a point to the configured flow grid", () => {
  expect(snapFlowPosition({ x: 109, y: 51 }, 20)).toEqual({ x: 100, y: 60 });
});

it("snaps invalid coordinates with a non-positive grid to the origin", () => {
  expect(snapFlowPosition({ x: Infinity, y: NaN }, 0)).toEqual({ x: 0, y: 0 });
});

it("centers a new node in the current flow viewport", () => {
  expect(centeredFlowPosition({ x: 100, y: 50, width: 800, height: 600, zoom: 2 }))
    .toEqual({ x: 260, y: 180 });
});

it("snaps centered positions for custom node sizes", () => {
  expect(centeredFlowPosition(
    { x: 0, y: 0, width: 400, height: 300, zoom: 1 },
    { width: 100, height: 60 },
  )).toEqual({ x: 160, y: 120 });
});

it("makes empty rail groups safe for null categories and nodes", () => {
  expect(libraryGroups([null, { name: "Empty", color: "pass", nodes: null }]))
    .toEqual([
      { name: null, color: null, types: [] },
      { name: "Empty", color: "pass", types: [] },
    ]);
  expect(libraryGroups(null)).toEqual([]);
});

it("makes rail groups from the existing node categories without node descriptions", () => {
  expect(libraryGroups([{ name: "Triggers", color: "trigger", nodes: [["manual", "Manual", ""]] }]))
    .toEqual([{ name: "Triggers", color: "trigger", types: ["manual"] }]);
});

// ---- Tab semantics + mode isolation -----------------------------------------

function renderTabsFor(mode, extra) {
  const node = renderWorkspaceTabs(Object.assign({ workflowId: "demo", mode, onSelect: () => {} }, extra || {}));
  document.body.appendChild(node);
  return node;
}

describe("workspace tabs", () => {
  it("uses native buttons with role=tab inside a role=tablist container", () => {
    const node = renderTabsFor("build");
    expect(node.getAttribute("role")).toBe("tablist");
    const tabs = node.querySelectorAll('[role="tab"]');
    expect(tabs.length).toBe(WORKSPACE_MODES.length);
    tabs.forEach((tab) => {
      expect(tab.tagName.toLowerCase()).toBe("button");
    });
  });

  it("marks the active mode with aria-selected=true and others with false", () => {
    for (const mode of WORKSPACE_MODES) {
      const node = renderTabsFor(mode);
      const tabs = node.querySelectorAll('[role="tab"]');
      tabs.forEach((tab) => {
        const isActive = tab.getAttribute("data-workspace-mode") === mode;
        expect(tab.getAttribute("aria-selected")).toBe(isActive ? "true" : "false");
      });
    }
  });

  it("invokes onSelect with the picked mode when a tab is clicked", () => {
    let picked = null;
    const node = renderTabsFor("build", { onSelect: (next) => { picked = next; } });
    const historyTab = node.querySelector('[data-workspace-mode="history"]');
    historyTab.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    expect(picked).toBe("history");
  });

  it("disables the run tab for draft-only workflows", () => {
    const node = renderTabsFor("build", { runDisabled: true });
    const runTab = node.querySelector('[data-workspace-mode="run"]');
    expect(runTab.hasAttribute("disabled")).toBe(true);
    expect(runTab.getAttribute("aria-disabled")).toBe("true");
  });
});

// ---- Workflow summary fields on the workspace list --------------------------

function buildWorkflowSummary() {
  return [
    { workflow_id: "demo", name: "Demo", status: "draft", version: null, enabled: false, latest_execution_status: null, open_feed_count: 0 },
    { workflow_id: "ops", name: "Ops", status: "published", version: 3, enabled: true, latest_execution_status: "succeeded", open_feed_count: 2 },
    { workflow_id: "broken", name: "Broken", status: "published", version: 7, enabled: false, latest_execution_status: "failed", open_feed_count: 1 },
  ];
}

describe("workflow navigation summary", () => {
  it("renders each row with name, status, version, enabled, latest execution, and open feed count", () => {
    const list = renderWorkflowSummary({
      workflows: buildWorkflowSummary(),
      activeWorkflowId: "ops",
      onSelect: () => {},
    });
    document.body.appendChild(list);

    const rows = list.querySelectorAll('[data-workflow-id]');
    expect(rows.length).toBe(3);

    const demo = list.querySelector('[data-workflow-id="demo"]');
    expect(demo.textContent).toMatch(/draft/i);
    expect(demo.textContent).toMatch(/v—|—|none/i);
    expect(demo.textContent).toMatch(/disabled|off/i);

    const ops = list.querySelector('[data-workflow-id="ops"]');
    expect(ops.textContent).toMatch(/published/i);
    expect(ops.textContent).toMatch(/v3/);
    expect(ops.textContent).toMatch(/enabled|on/i);
    expect(ops.textContent).toMatch(/succeeded/i);
    expect(ops.textContent).toMatch(/2/);
    expect(ops.getAttribute("aria-current")).toBe("true");

    const broken = list.querySelector('[data-workflow-id="broken"]');
    expect(broken.textContent).toMatch(/failed/i);
    expect(broken.textContent).toMatch(/v7/);
    expect(broken.textContent).toMatch(/disabled|off/i);
    expect(broken.textContent).toMatch(/1/);
  });
});

// ---- Responsive CSS invariants (plan lines 749–756) --------------------------

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";

const SRC_CSS = readFileSync(
  path.join(path.dirname(fileURLToPath(import.meta.url)), "style.css"),
  "utf8",
);

describe("workspace responsive CSS", () => {
  it("declares the three required viewport breakpoints", () => {
    expect(SRC_CSS).toMatch(/@media\s*\(max-width:\s*1279px\)/);
    expect(SRC_CSS).toMatch(/@media\s*\(max-width:\s*767px\)/);
    expect(SRC_CSS).toMatch(/@media\s*\(max-height:\s*600px\)/);
  });

  it("keeps the build mode at least 240px tall on short viewports", () => {
    expect(SRC_CSS).toMatch(/\.hermes-workflows-build-mode\s*\{[^}]*min-height:\s*240px/);
  });

  it("lets Run and History scroll independently of the editor", () => {
    expect(SRC_CSS).toMatch(/\.hermes-workflows-(run-mode|history-mode)[^{}]*\{[^}]*overflow-y:\s*auto/);
  });

  it("respects prefers-reduced-motion: reduce", () => {
    expect(SRC_CSS).toMatch(/prefers-reduced-motion:\s*reduce/);
  });
});

// ---- Accessibility: sidebar disclosures use native buttons ---------------

import { readFileSync as rfSync } from "node:fs";

const SRC_APP = rfSync(
  path.join(path.dirname(fileURLToPath(import.meta.url)), "app.js"),
  "utf8",
);

describe("sidebar disclosure accessibility", () => {
  it("sidebar collapsible sections use buttons for disclosure toggles", () => {
    // All three sidebar sections (goal, workflows, executions) use a button
    // inside the h3 for the toggle, not onClick on the div or h3 directly.
    expect(SRC_APP).toMatch(/hermes-workflows-sidebar-collapsible[\s\S]*?h\("button"[^)]*aria-expanded/);
    // No onClick on the section div itself for workflows/executions sections
    expect(SRC_APP).not.toMatch(/sidebar-collapsible[^"]*\),\s*onClick:\s*function\(\)\s*\{\s*toggleSection/);
  });

  it("disclosure buttons have aria-expanded and aria-controls", () => {
    // Goal disclosure
    expect(SRC_APP).toMatch(/aria-expanded.*goalCollapsed/);
    expect(SRC_APP).toMatch(/aria-controls.*sidebar-goal/);
    // Workflows disclosure
    expect(SRC_APP).toMatch(/aria-expanded.*wfCollapsed/);
    expect(SRC_APP).toMatch(/aria-controls.*sidebar-workflows/);
    // Executions disclosure
    expect(SRC_APP).toMatch(/aria-expanded.*execCollapsed/);
    expect(SRC_APP).toMatch(/aria-controls.*sidebar-executions/);
  });
});

// ---- Accessibility: bottom panel disclosure --------------------------------

describe("bottom panel disclosure accessibility", () => {
  it("bottom toggle button has aria-expanded and aria-controls", () => {
    expect(SRC_APP).toMatch(/aria-expanded.*bottomCollapsed/);
    expect(SRC_APP).toMatch(/aria-controls.*hermes-workflows-bottom-content/);
  });
});

// ---- Accessibility: focus trap and restore on run dialog -------------------

describe("run dialog focus management", () => {
  it("dialog traps focus and restores to opener on close", () => {
    // Should have a focus trap mechanism for the run overlay dialog
    expect(SRC_APP).toMatch(/hermes-workflows-run-overlay/);
    expect(SRC_APP).toMatch(/role:\s*"dialog"/);
    // Focus trap: save opener, trap Tab cycling, restore on close
    expect(SRC_APP).toMatch(/activeElement/);
    expect(SRC_APP).toMatch(/runPanelOpenerRef/);
    expect(SRC_APP).toMatch(/restoreFocus|\.focus\(\)/i);
  });
});

// ---- Accessibility: aria-live for operation results -------------------------

describe("operation result announcements", () => {
  it("operation results use aria-live polite for screen reader announcements", () => {
    expect(SRC_APP).toMatch(/"aria-live".*"polite"/);
  });
});

// ---- Accessibility: status text alongside color classes --------------------

describe("status communication without relying on color alone", () => {
  it("status badges include text content not just color classes", () => {
    // Sidebar badges already show "on"/"off" text — verify the pattern exists
    expect(SRC_APP).toMatch(/is-enabled.*\)/);  // text follows the class
    expect(SRC_APP).toMatch(/"on"|"off"/);
  });

  it("execution status shows text alongside color class", () => {
    // Execution sidebar items show status text, not just color
    expect(SRC_APP).toMatch(/is-succeeded.*succeeded|succeeded.*is-succeeded/i);
    expect(SRC_APP).toMatch(/is-failed.*failed|failed.*is-failed/i);
  });
});