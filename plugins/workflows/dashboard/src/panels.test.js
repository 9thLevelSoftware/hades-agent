import { describe, expect, it } from "vitest";
import {
  renderBottomPanel,
  renderDiagnosticsPanel,
  renderFeedInputField,
  renderHistoryMode,
  renderInputFeedPanel,
  renderRunInputField,
  renderRunStartPanel,
} from "./panels.js";

const h = (tag, props, ...children) => ({ tag, props, children });
const noop = () => {};

it("renders each extracted panel with mock props", () => {
  expect(renderBottomPanel({
    createElement: h,
    React: {},
    bottomCollapsed: false,
    setBottomCollapsed: noop,
    activeSpec: null,
    workflowCapabilities: null,
    renderValidationChecklist: () => h("div", null),
  })).toBeTruthy();

  expect(renderRunStartPanel({
    createElement: h,
    React: {},
    runPanelOpen: true,
    setRunPanelOpen: noop,
    runWorkflowId: "demo",
    definitions: [],
    inputFieldsForSpec: () => [],
    runInputSpec: () => null,
    inputFieldValues: {},
    setInputFieldValues: noop,
    showAdvancedInputJson: false,
    setShowAdvancedInputJson: noop,
    runInputText: "{}",
    setRunInputText: noop,
    running: false,
    runFieldErrors: {},
    runWorkflow: noop,
    selectedRunVersion: () => null,
    fail: noop,
  })).toBeTruthy();

  expect(renderHistoryMode({
    createElement: h,
    React: {},
    executions: [],
    selectedExecution: null,
    loadExecution: () => Promise.resolve(),
    refresh: noop,
    loading: false,
    pushMode: noop,
    renderTimeline: () => h("div", null),
    fail: noop,
  })).toBeTruthy();

  expect(renderInputFeedPanel({
    createElement: h,
    React: {},
    selectedDefinition: { workflow_id: "demo", spec: { triggers: [] } },
    workflowIdForDefinition: (definition) => definition.workflow_id,
    selectedInputTrigger: () => null,
    inputFieldsForSpec: () => [],
    inputFeeds: [],
    selectedFeedId: "",
    setSelectedFeedId: noop,
    openContinuousFeed: noop,
    setSelectedFeedStatus: noop,
    feedBusy: false,
    loadInputFeedItems: noop,
    inputFeedItems: [],
    updateInputFeedItem: noop,
    addItemToFeed: noop,
    feedInputValues: {},
    setFeedInputValues: noop,
    showAdvancedFeedInputJson: false,
    setShowAdvancedFeedInputJson: noop,
    feedInputText: "{}",
    setFeedInputText: noop,
  })).toBeTruthy();

  expect(renderDiagnosticsPanel({
    createElement: h,
    React: {},
    diagnosticsOpen: false,
    setDiagnosticsOpen: noop,
    renderExecutionStallWarning: () => null,
    ticking: false,
    manualTick: noop,
    loading: false,
    refresh: noop,
  })).toBeTruthy();
});

describe("extracted panel field helpers", () => {
  it("render input fields", () => {
    expect(renderRunInputField({ name: "value" }, {
      createElement: h,
      inputFieldValues: {},
      setInputFieldValues: noop,
      runFieldErrors: {},
    })).toBeTruthy();
    expect(renderFeedInputField({ name: "value" }, {
      createElement: h,
      feedInputValues: {},
      setFeedInputValues: noop,
    })).toBeTruthy();
  });
});
