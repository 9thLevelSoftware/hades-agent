import { describe, expect, it } from "vitest";
import { renderInspector } from "./inspector.js";

const h = (tag, props, ...children) => ({ tag, props, children });
const noop = () => {};

function props(overrides = {}) {
  return Object.assign({
    createElement: h,
    React: { Fragment: "Fragment" },
    selectedNode: null,
    cellId: "node",
    setCellId: noop,
    cellType: "pass",
    setCellType: noop,
    activeSpec: () => ({ nodes: {} }),
    agentProfile: "",
    setAgentProfile: noop,
    agentProvider: "",
    setAgentProvider: noop,
    agentModel: "",
    setAgentModel: noop,
    agentTitle: "",
    setAgentTitle: noop,
    promptText: "",
    setPromptText: noop,
    resultContractText: "{}",
    setResultContractText: noop,
    agentRoutingOptions: { profiles: [], providers: [] },
    promptAssistantOpen: false,
    setPromptAssistantOpen: noop,
    promptAssistantGoal: "",
    setPromptAssistantGoal: noop,
    promptAssistantObjective: "",
    setPromptAssistantObjective: noop,
    promptAssistantContext: "",
    setPromptAssistantContext: noop,
    promptAssistantOutput: "{}",
    setPromptAssistantOutput: noop,
    promptAssistantConstraints: "",
    setPromptAssistantConstraints: noop,
    promptAssistantAdvanced: false,
    setPromptAssistantAdvanced: noop,
    draftPromptWithAssistant: noop,
    triggerInputRows: [],
    setTriggerInputRows: noop,
    triggerInputName: "",
    setTriggerInputName: noop,
    triggerInputKind: "text",
    setTriggerInputKind: noop,
    triggerInputRequired: true,
    setTriggerInputRequired: noop,
    triggerInputDefault: "",
    setTriggerInputDefault: noop,
    triggerInputMinLength: "",
    setTriggerInputMinLength: noop,
    triggerInputMaxLength: "",
    setTriggerInputMaxLength: noop,
    triggerIntakeMode: "single",
    setTriggerIntakeMode: noop,
    triggerDedupeKey: "",
    setTriggerDedupeKey: noop,
    triggerReadyPath: "",
    setTriggerReadyPath: noop,
    triggerSchedule: "",
    setTriggerSchedule: noop,
    addTriggerInputFieldFromUi: noop,
    removeTriggerInputField: noop,
    switchDefault: "",
    setSwitchDefault: noop,
    switchCases: [],
    setSwitchCases: noop,
    switchCaseName: "",
    setSwitchCaseName: noop,
    switchCasePath: "$.input.status",
    setSwitchCasePath: noop,
    switchCaseEquals: "",
    setSwitchCaseEquals: noop,
    addSwitchCaseFromUi: noop,
    cellSeconds: "60",
    setCellSeconds: noop,
    cellOutputText: "",
    setCellOutputText: noop,
    sendMessagePlatform: "auto",
    setSendMessagePlatform: noop,
    sendMessageText: "",
    setSendMessageText: noop,
    sendMessageTarget: "",
    setSendMessageTarget: noop,
    subworkflowRef: "",
    setSubworkflowRef: noop,
    subworkflowInputMappingText: "{}",
    setSubworkflowInputMappingText: noop,
    applyAgentCellForm: noop,
    applyBasicCellForm: noop,
    deleteSelectedCell: noop,
    advancedJsonOpen: false,
    setAdvancedJsonOpen: noop,
    nodeJson: "{}",
    setNodeJson: noop,
    applyNodeJson: noop,
    useJsonDraft: noop,
    nodeMessage: "",
    setNodeMessage: noop,
    setDraftSpec: noop,
    setSelectedDefinition: noop,
    closeInspector: noop,
    activeInspectorTab: "parameters",
    setActiveInspectorTab: noop,
  }, overrides);
}

function findAll(root, predicate) {
  const results = [];
  function visit(node) {
    if (Array.isArray(node)) { node.forEach(visit); return; }
    if (!node || typeof node !== "object") return;
    if (predicate(node)) results.push(node);
    if (Array.isArray(node.children)) node.children.forEach(visit);
  }
  visit(root);
  return results;
}

describe("renderInspector", () => {
  it("renders an empty state without a selected node", () => {
    const output = renderInspector(props());
    expect(output.props.className).toBe("hermes-workflows-inspector");
    const text = JSON.stringify(output);
    expect(text).toContain("Select a node from the canvas");
  });

  it("dispatches agent_task nodes to the agent renderer", () => {
    const output = renderInspector(props({ selectedNode: { id: "agent", type: "agent_task", specKind: "node" } }));
    expect(JSON.stringify(output)).toContain("Assigned profile");
  });

  it("dispatches trigger nodes to the trigger renderer", () => {
    const output = renderInspector(props({
      selectedNode: { id: "start", type: "trigger", specKind: "trigger", trigger_type: "manual" },
      activeInspectorTab: "input_output",
    }));
    expect(JSON.stringify(output)).toContain("Input schema");
  });

  it("renders Node Configuration heading, close button, and three tabs when a node is selected", () => {
    const output = renderInspector(props({ selectedNode: { id: "start", type: "trigger", specKind: "trigger", trigger_type: "manual" } }));
    const text = JSON.stringify(output);
    expect(text).toContain("Node Configuration");
    const closeButtons = findAll(output, (n) => n.tag === "button" && n.props && n.props["aria-label"] === "Close configuration");
    expect(closeButtons.length).toBeGreaterThan(0);
    const tabs = findAll(output, (n) => n.tag === "button" && n.props && n.props.role === "tab");
    const labels = tabs.map((t) => (t.children && t.children[0]) || "");
    expect(labels).toEqual(expect.arrayContaining(["Parameters", "Input & Output", "Settings"]));
  });

  it("renders trigger type as a select, not free input", () => {
    const output = renderInspector(props({
      selectedNode: { id: "start", type: "trigger", specKind: "trigger", trigger_type: "manual" },
      activeInspectorTab: "parameters",
    }));
    const text = JSON.stringify(output);
    // Trigger type must be a select with SUPPORTED_TRIGGERS options, not an <input>.
    const triggerTypeSelects = findAll(output, (n) => {
      if (n.tag !== "select") return false;
      // crude: select children include options for manual + schedule
      const text = JSON.stringify(n);
      return text.includes("manual") && text.includes("schedule");
    });
    expect(triggerTypeSelects.length).toBeGreaterThan(0);
    // No free-text input named "Trigger type"
    expect(text).not.toMatch(/<input[^>]*placeholder="manual"/);
  });

  it("trigger Input & Output tab exposes Input schema and Intake mode group panels, but no Apply/Delete row", () => {
    const output = renderInspector(props({
      selectedNode: { id: "start", type: "trigger", specKind: "trigger", trigger_type: "manual" },
      activeInspectorTab: "input_output",
    }));
    const text = JSON.stringify(output);
    expect(text).toContain("Input schema");
    expect(text).toContain("Intake mode");
    // Settings tab Apply / Delete row must NOT appear on the Input & Output tab.
    expect(text).not.toMatch(/"Apply"/);
    expect(text).not.toMatch(/"Delete"/);
  });

  it("accepts activeInspectorTab and renders Result contract JSON for agent_task Input & Output", () => {
    const output = renderInspector(props({
      selectedNode: { id: "agent", type: "agent_task", specKind: "node" },
      activeInspectorTab: "input_output",
    }));
    const text = JSON.stringify(output);
    expect(text).toContain("Result contract JSON");
    expect(text).toContain("${ input.foo }");
  });

  it("places Apply / Delete buttons and Advanced JSON toggle on the Settings tab only", () => {
    const output = renderInspector(props({
      selectedNode: { id: "agent", type: "agent_task", specKind: "node" },
      activeInspectorTab: "settings",
    }));
    const text = JSON.stringify(output);
    expect(text).toContain("Apply");
    expect(text).toContain("Delete");
    expect(text).toContain("Advanced JSON");
  });

  it("renders a per-field wrapper with htmlFor for required inputs", () => {
    const output = renderInspector(props({
      selectedNode: { id: "start", type: "trigger", specKind: "trigger", trigger_type: "manual" },
      activeInspectorTab: "parameters",
    }));
    const fields = findAll(output, (n) => n.tag === "div" && n.props && n.props.className === "hermes-workflows-field");
    expect(fields.length).toBeGreaterThan(0);
    const labelledLabels = findAll(output, (n) => n.tag === "label" && n.props && n.props.htmlFor);
    expect(labelledLabels.length).toBeGreaterThan(0);
  });

  it("groups min length and max length in a field-row, never as overlapping labels", () => {
    const output = renderInspector(props({
      selectedNode: { id: "start", type: "trigger", specKind: "trigger", trigger_type: "manual" },
      activeInspectorTab: "input_output",
    }));
    const rows = findAll(output, (n) => n.tag === "div" && n.props && n.props.className === "hermes-workflows-field-row");
    expect(rows.length).toBeGreaterThan(0);
    const text = JSON.stringify(output);
    // No free-floating label text overlapping with "Required input"
    expect(text).not.toMatch(/"required"\s*,\s*"optional"/);
  });

  it("shows the empty-state hint listing Add Node (N) shortcut", () => {
    const output = renderInspector(props());
    const text = JSON.stringify(output);
    expect(text).toContain("Add Node");
  });

  it("closeInspector is in the prop callback signature (defaults to noop)", () => {
    const output = renderInspector(props({ selectedNode: { id: "start", type: "trigger", specKind: "trigger", trigger_type: "manual" } }));
    const text = JSON.stringify(output);
    expect(text).toContain("Close configuration");
  });
});