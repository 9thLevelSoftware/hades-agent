import { createRequire } from "node:module";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import {
  NodePaletteOverlay,
  renderPalette,
  renderWorkflowOnboarding,
  renderWorkflowRail,
} from "./palette.js";

const require = createRequire(import.meta.url);
const reactDomRequire = createRequire(require.resolve("react-dom/client"));
const React = reactDomRequire("react");
const { act } = React;
const { createRoot } = reactDomRequire("react-dom/client");
globalThis.IS_REACT_ACT_ENVIRONMENT = true;

const h = (tag, props, ...children) => ({ tag, props, children });
const noop = () => {};
const fakeReact = { Fragment: "Fragment", useState: (initial) => [initial, noop] };

const sourceDir = path.dirname(fileURLToPath(import.meta.url));
const cssSource = fs.readFileSync(path.join(sourceDir, "style.css"), "utf8");

function props(overrides = {}) {
  return Object.assign({
    createElement: h,
    React: fakeReact,
    activeSpec: null,
    goalText: "",
    setGoalText: noop,
    newWorkflowName: "",
    setNewWorkflowName: noop,
    draftFromGoal: noop,
    drafting: false,
    startBlankWorkflow: noop,
    refineWorkflow: noop,
    refining: false,
    refineText: "",
    setRefineText: noop,
    draftResult: null,
    candidateSource: null,
    acceptDraftCandidate: noop,
    rejectDraftCandidate: noop,
    definitions: [],
    selectedDefinition: null,
    loadDefinition: () => Promise.resolve(),
    executions: [],
    loadExecution: () => Promise.resolve(),
    addTriggerOfType: noop,
    addWorkflowCellOfType: noop,
    libraryGroups: [],
    openNodePalette: noop,
  }, overrides);
}

function contains(node, predicate) {
  if (Array.isArray(node)) return node.some((child) => contains(child, predicate));
  if (!node || typeof node !== "object") return predicate(node);
  return predicate(node) || contains(node.children, predicate);
}

describe("renderPalette", () => {
  it("returns an element with a palette class", () => {
    const node = renderPalette(props());
    expect(node.props.className).toContain("palette");
  });

  it("shows the workflow creation form when no workflow is active", () => {
    const node = renderPalette(props({ activeSpec: null }));
    expect(contains(node, (value) => value === "Generate From Prompt")).toBe(true);
    expect(contains(node, (value) => value === "Start From Scratch")).toBe(true);
  });

  it("renders hook-free onboarding with visible accessible field labels", () => {
    const node = renderWorkflowOnboarding(props({ activeSpec: null }));
    expect(node.props.className).toContain("hermes-workflows-onboarding-form");
    expect(contains(node, (value) => value === "Workflow name")).toBe(true);
    expect(contains(node, (value) => value === "Describe workflow goal")).toBe(true);
    expect(contains(node, (value) => value === "Generate From Prompt")).toBe(true);
    expect(contains(node, (value) => value === "Start From Scratch")).toBe(true);
  });

  it("hides only the sidebar creation form when requested", () => {
    const node = renderPalette(props({
      hideWorkflowForm: true,
      definitions: [{ id: "existing", name: "Existing workflow", enabled: true }],
      executions: [{ id: "execution-1", status: "succeeded" }],
    }));
    const json = JSON.stringify(node);
    expect(json).not.toContain("Generate From Prompt");
    expect(json).not.toContain("Describe workflow goal");
    expect(json).toContain("Existing workflow");
    expect(json).toContain("Executions");
  });

  it("keeps the sidebar mounted across the onboarding hook transition", async () => {
    const container = document.createElement("div");
    document.body.appendChild(container);
    const root = createRoot(container);

    function Host() {
      const [hasSpec, setHasSpec] = React.useState(false);
      const startBlankWorkflow = () => setHasSpec(true);
      const sidebarProps = props({
        React,
        createElement: React.createElement,
        activeSpec: hasSpec ? { id: "workflow" } : null,
        startBlankWorkflow,
      });
      const onboardingProps = props({
        React,
        createElement: React.createElement,
        startBlankWorkflow,
      });
      return React.createElement(
        React.Fragment,
        null,
        renderPalette(sidebarProps),
        hasSpec === false ? renderWorkflowOnboarding(onboardingProps) : null,
      );
    }

    try {
      await act(async () => root.render(React.createElement(Host)));
      const startButton = container.querySelector(".hermes-workflows-palette-creation button[aria-label='Start from scratch']");
      expect(startButton).not.toBeNull();
      expect(container.querySelector(".hermes-workflows-onboarding-form")).not.toBeNull();
      let updateError;
      try {
        await act(async () => startButton.click());
      } catch (error) {
        updateError = error;
      }
      expect(updateError).toBeUndefined();
      expect(container.querySelector(".hermes-workflows-sidebar")).not.toBeNull();
      expect(container.querySelector(".hermes-workflows-onboarding-form")).toBeNull();
    } finally {
      act(() => root.unmount());
      container.remove();
    }
  });
});

describe("renderWorkflowRail", () => {
  it("returns an element with the rail class", () => {
    const node = renderWorkflowRail(props({ variant: "rail" }));
    expect(node.props.className).toContain("hermes-workflows-rail");
  });

  it("renders Workflows / Executions / Library sections with disclosure buttons", () => {
    const node = renderWorkflowRail(props({
      variant: "rail",
      libraryGroups: [
        { name: "Triggers", color: "trigger", types: ["manual"] },
        { name: "AI & Logic", color: "agent_task", types: ["agent_task"] },
      ],
    }));
    expect(contains(node, (value) => value === "Workflows")).toBe(true);
    expect(contains(node, (value) => value === "Executions")).toBe(true);
    expect(contains(node, (value) => value === "Library")).toBe(true);
    const disclosures = [];
    contains(node, (n) => {
      if (n && typeof n === "object" && n.tag === "button" && n.props && n.props["aria-expanded"] !== undefined) {
        disclosures.push(n);
      }
      return false;
    });
    expect(disclosures.length).toBeGreaterThanOrEqual(3);
    for (const disclosure of disclosures) {
      expect(disclosure.props["aria-expanded"]).toMatch(/true|false/);
      expect(disclosure.props["aria-controls"]).toBeTruthy();
    }
  });

  it("calls openNodePalette with the group name when a library group is clicked", () => {
    let captured = null;
    const openNodePalette = (name) => { captured = name; };
    const node = renderWorkflowRail(props({
      variant: "rail",
      openNodePalette,
      libraryGroups: [
        { name: "Triggers", color: "trigger", types: ["manual"] },
        { name: "Actions", color: "send_message", types: ["send_message"] },
      ],
    }));
    // Find the Triggers group disclosure button and click it
    let triggersButton = null;
    contains(node, (n) => {
      if (n && typeof n === "object" && n.tag === "button" && n.props && n.props["aria-controls"] === "rail-library-group-triggers") {
        triggersButton = n;
      }
      return false;
    });
    expect(triggersButton).not.toBeNull();
    triggersButton.props.onClick();
    expect(captured).toBe("Triggers");
  });

  it("still shows definitions and executions when hideWorkflowForm is true", () => {
    const node = renderWorkflowRail(props({
      variant: "rail",
      hideWorkflowForm: true,
      definitions: [{ id: "existing", name: "Existing workflow", enabled: true }],
      executions: [{ id: "execution-1", status: "succeeded" }],
    }));
    const json = JSON.stringify(node);
    expect(json).not.toContain("Generate From Prompt");
    expect(json).toContain("Existing workflow");
    expect(json).toContain("Executions");
  });

  it("keeps the AI-first Draft/Refine controls when hideWorkflowForm is false", () => {
    const node = renderWorkflowRail(props({
      variant: "rail",
      activeSpec: null,
      hideWorkflowForm: false,
    }));
    const json = JSON.stringify(node);
    expect(json).toContain("Generate From Prompt");
    expect(json).toContain("Start From Scratch");
  });
});

describe("NodePaletteOverlay", () => {
  it("renders visible manual node choices and writes drag payloads", () => {
    const node = NodePaletteOverlay(props({ isOpen: true }));
    const json = JSON.stringify(node);
    expect(json).toContain("Add Node");
    expect(json).toContain("Manual Trigger");
    expect(json).toContain("AI Agent Task");

    let manualTile = null;
    contains(node, (candidate) => {
      if (candidate && typeof candidate === "object" && candidate.tag === "button" && candidate.props && candidate.props.draggable === true && candidate.props["aria-label"] === "Add Manual Trigger") {
        manualTile = candidate;
      }
      return false;
    });
    expect(manualTile).not.toBeNull();
    let payload = null;
    manualTile.props.onDragStart({
      dataTransfer: { setData: (format, value) => { payload = { format, value }; } },
    });
    expect(payload).toEqual({ format: "text/plain", value: "manual" });
  });

  it("filters by library group and keeps click-to-add tiles available", () => {
    const node = NodePaletteOverlay(props({ isOpen: true, nodePaletteGroup: "Triggers" }));
    const json = JSON.stringify(node);
    expect(json).toContain("Manual Trigger");
    expect(json).not.toContain("AI Agent Task");
    let tile = null;
    contains(node, (candidate) => {
      if (candidate && typeof candidate === "object" && candidate.tag === "button" && candidate.props && candidate.props.draggable === true) tile = candidate;
      return false;
    });
    expect(tile).not.toBeNull();
    expect(tile.props.onClick).toBeTypeOf("function");
  });

  it("returns null when closed", () => {
    expect(NodePaletteOverlay(props({ isOpen: false }))).toBeNull();
  });
});

describe("workflow rail CSS contract", () => {
  it("makes .hermes-workflows-rail the sole scroll container", () => {
    expect(cssSource).toMatch(/\.hermes-workflows-rail\s*\{[^}]*overflow-y:\s*auto/s);
    expect(cssSource).toMatch(/\.hermes-workflows-rail\s*\{[^}]*overscroll-behavior:\s*contain/s);
    expect(cssSource).toMatch(/\.hermes-workflows-rail\s*\{[^}]*min-height:\s*0/s);
  });

  it("resets child overflow inside the rail", () => {
    expect(cssSource).toMatch(/\.hermes-workflows-rail\s+\.hermes-workflows-palette-panel\s*\{[^}]*overflow:\s*visible/s);
  });

  it("hides the legacy palette tabs that are not part of the rail", () => {
    // The old Workflows/Nodes tab buttons must not be reachable from the rail.
    // Either the class is display:none or the parent that owns it is hidden.
    expect(cssSource).toMatch(/\.hermes-workflows-palette-tabs\s*\{[^}]*display:\s*none/s);
  });
});
