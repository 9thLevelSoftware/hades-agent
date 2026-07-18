import { describe, expect, it } from "vitest";
import { renderTopBar } from "./topbar.js";

const h = (tag, props, ...children) => ({ tag, props, children });

function props(overrides = {}) {
  return Object.assign({
    createElement: h,
    React: { Fragment: "Fragment" },
    activeSpec: { name: "Draft workflow" },
    selectedDefinition: null,
    workflowIdForDefinition: (definition) => definition && definition.workflow_id,
    versionForDefinition: (definition) => definition && definition.version,
    renderWorkspaceTabs: () => null,
    validateDefinition: () => {},
    validating: false,
    deployDefinition: () => {},
    deploying: false,
    deleteWorkflow: () => {},
    deleting: false,
    openRunPanel: () => {},
    running: false,
    refresh: () => {},
    loading: false,
    showAdvancedYaml: false,
    setShowAdvancedYaml: () => {},
    persistedRunCapable: () => false,
  }, overrides);
}

function statusNode(node) {
  return node.children[0].children.find((child) => child && child.props && child.props.className === "hermes-workflows-topbar-status");
}

describe("renderTopBar", () => {
  it("returns an element with a topbar class", () => {
    const node = renderTopBar(props());
    expect(node.props.className).toContain("hermes-workflows-topbar");
  });

  it("shows draft status when no definition is selected", () => {
    expect(statusNode(renderTopBar(props())).children).toContain("draft");
  });

  it("shows version info for a selected definition", () => {
    const node = renderTopBar(props({
      selectedDefinition: { workflow_id: "demo", version: 3 },
    }));
    expect(statusNode(node).children).toContain("v3 · enabled");
  });

  it("shows an always-visible Add Node action that opens the manual palette", () => {
    let opened = 0;
    const node = renderTopBar(props({ openNodePalette: () => { opened += 1; } }));
    const actions = node.children[2];
    const addNode = actions.children.find((child) => child && child.children && child.children.includes("Add Node"));
    expect(addNode).toBeTruthy();
    expect(addNode.props["aria-label"]).toBe("Add Node");
    addNode.props.onClick();
    expect(opened).toBe(1);
  });
});
