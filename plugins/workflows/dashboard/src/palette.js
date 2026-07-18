import { NODE_COLORS, NODE_ICONS } from "./canvas-nodes.js";
import { SUPPORTED_NODES, SUPPORTED_TRIGGERS } from "./editor-model.js";

export const NODE_CATEGORIES = [
  {
    name: "Triggers",
    color: "trigger",
    nodes: [
      ["manual", "Manual Trigger", "Start the workflow on demand."],
      ["schedule", "Schedule Trigger", "Start the workflow on a schedule."],
      ["webhook", "Webhook", "Start the workflow from an HTTP request."],
    ],
  },
  {
    name: "AI & Logic",
    color: "agent_task",
    nodes: [
      ["agent_task", "AI Agent Task", "Delegate work to a Hermes profile."],
      ["switch", "Switch/Branch", "Route based on a value or condition."],
      ["pass", "Pass/Transform", "Shape or summarize data."],
    ],
  },
  {
    name: "Flow Control",
    color: "parallel",
    nodes: [
      ["parallel", "Parallel Split", "Run independent branches."],
      ["join", "Join", "Wait for branches to complete."],
      ["wait", "Wait/Delay", "Pause before continuing."],
    ],
  },
  {
    name: "Actions",
    color: "send_message",
    nodes: [
      ["send_message", "Send Message", "Send a message to a configured target."],
      ["fail", "Fail/Reject", "Stop the workflow with an error."],
    ],
  },
];

function safeString(value) {
  return value === null || value === undefined ? "" : String(value);
}

function selectionKey(definition) {
  if (!definition) return "";
  return safeString(definition.workflow_id || definition.id) + ":" + safeString(definition.version);
}

function isSupported(type, trigger) {
  const values = trigger ? SUPPORTED_TRIGGERS : SUPPORTED_NODES;
  return values.indexOf(type) !== -1;
}

function renderWorkflowForm(h, props) {
  function call(handler) {
    if (typeof handler === "function") return handler.apply(null, Array.prototype.slice.call(arguments, 1));
  }

  const nameId = props.workflowNameInputId || "hermes-workflow-name";
  const goalId = props.workflowGoalInputId || "hermes-workflow-goal";
  return h("section", { className: "hermes-workflows-palette-section hermes-workflows-palette-creation" },
    h("h3", null, props.activeSpec ? "New workflow / prompt" : "New workflow"),
    h("p", { className: "hermes-workflows-muted" }, "Describe it or start from blank."),
    h("form", { className: "hermes-workflows-stack", "aria-label": "Create workflow", onSubmit: props.draftFromGoal },
      h("label", { htmlFor: nameId }, "Workflow name"),
      h("input", {
        id: nameId,
        value: props.newWorkflowName || "",
        onChange: function (event) { call(props.setNewWorkflowName, event.target.value); },
        placeholder: "Workflow name",
      }),
      h("label", { htmlFor: goalId }, "Describe workflow goal"),
      h("textarea", {
        id: goalId,
        "aria-label": "Describe workflow goal",
        value: props.goalText || "",
        onChange: function (event) { call(props.setGoalText, event.target.value); },
        placeholder: "Example: review code changes, run tests, then deploy if approved.",
      }),
      h("div", { className: "hermes-workflows-row" },
        h("button", { type: "submit", disabled: props.drafting, className: "hermes-workflows-primary" }, props.drafting ? "Generating…" : "Generate From Prompt"),
        h("button", { type: "button", "aria-label": "Start from scratch", onClick: props.startBlankWorkflow }, "Start From Scratch")
      )
    ),
    props.activeSpec ? h("form", { className: "hermes-workflows-stack", onSubmit: props.refineWorkflow },
      h("textarea", {
        value: props.refineText || "",
        onChange: function (event) { call(props.setRefineText, event.target.value); },
        placeholder: "Refine: add a step, change routing, etc.",
        "aria-label": "Refine workflow",
      }),
      h("button", { type: "submit", disabled: props.refining }, props.refining ? "Refining…" : "Refine")
    ) : null,
    props.draftResult ? h("div", { className: "hermes-workflows-palette-draft hermes-workflows-stack" },
      props.draftResult.summary ? h("p", { className: "hermes-workflows-muted" }, props.draftResult.summary) : null,
      (props.draftResult.assumptions || []).length ? h("p", null, h("strong", null, "Assumptions: "), props.draftResult.assumptions.join("; ")) : null,
      (props.draftResult.warnings || []).length ? h("p", { className: "hermes-workflows-muted" }, h("strong", null, "Warnings: "), props.draftResult.warnings.join("; ")) : null,
      h("div", { className: "hermes-workflows-row" },
        h("button", { type: "button", onClick: props.acceptDraftCandidate, className: "hermes-workflows-primary" }, props.candidateSource === "generate" ? "Accept Draft" : "Accept Changes"),
        h("button", { type: "button", onClick: props.rejectDraftCandidate }, "Reject")
      )
    ) : null
  );
}

export function renderWorkflowOnboarding(props) {
  return props.createElement("div", { className: "hermes-workflows-onboarding-form" }, renderWorkflowForm(props.createElement, props));
}

export function renderPalette(props) {
  if (props && props.variant === "rail") return renderWorkflowRail(props);
  const h = props.createElement;
  const React = props.React || {};
  const useState = React.useState || function (initial) { return [initial, function () {}]; };
  const tabState = useState("workflows");
  const activeTab = tabState[0];
  const setActiveTab = tabState[1];
  const searchState = useState("");
  const search = searchState[0];
  const setSearch = searchState[1];
  const definitions = Array.isArray(props.definitions) ? props.definitions : [];
  const executions = Array.isArray(props.executions) ? props.executions : [];

  function call(handler) {
    if (typeof handler === "function") return handler.apply(null, Array.prototype.slice.call(arguments, 1));
  }

  function dragProps(type) {
    return {
      draggable: true,
      onDragStart: function (event) {
        if (event.dataTransfer && event.dataTransfer.setData) event.dataTransfer.setData("text/plain", type);
        if (typeof window !== "undefined") window.__HERMES_DRAG_NODE_TYPE = type;
      },
    };
  }


  function renderDefinitions() {
    return h("section", { className: "hermes-workflows-palette-section" },
      h("h3", null, "Workflows"),
      h("div", { className: "hermes-workflows-sidebar-list" },
        definitions.length ? definitions.map(function (definition) {
          const id = definition.workflow_id || definition.id;
          const key = selectionKey(definition);
          return h("button", {
            key: key,
            type: "button",
            className: "hermes-workflows-sidebar-item" + (key === selectionKey(props.selectedDefinition) ? " is-selected" : ""),
            onClick: function () { call(props.loadDefinition, id, definition.version); },
          },
            h("span", { className: "hermes-workflows-sidebar-item-title" }, safeString(definition.name || id)),
            h("span", { className: "hermes-workflows-sidebar-badge" + (definition.enabled ? " is-enabled" : "") }, definition.enabled ? "on" : "off")
          );
        }) : h("p", { className: "hermes-workflows-muted" }, "No workflows deployed.")
      )
    );
  }

  function renderExecutions() {
    return h("section", { className: "hermes-workflows-palette-section" },
      h("h3", null, "Executions"),
      h("div", { className: "hermes-workflows-sidebar-list" },
        executions.length ? executions.slice(0, 20).map(function (execution) {
          const id = safeString(execution.execution_id || execution.id);
          const status = safeString(execution.status);
          const statusClass = status === "succeeded" ? " is-succeeded" : status === "failed" ? " is-failed" : "";
          return h("button", {
            key: id,
            type: "button",
            className: "hermes-workflows-sidebar-item",
            onClick: function () { call(props.loadExecution, id); },
          },
            h("span", { className: "hermes-workflows-sidebar-item-title" }, id.slice(0, 16)),
            h("span", { className: "hermes-workflows-sidebar-badge" + statusClass }, status)
          );
        }) : h("p", { className: "hermes-workflows-muted" }, "No executions yet.")
      )
    );
  }

  function renderNodeCard(node) {
    const type = node[0];
    const trigger = NODE_CATEGORIES[0].nodes.some(function (item) { return item[0] === type; });
    if (!isSupported(type, trigger)) return null;
    return h("button", Object.assign({
      key: type,
      type: "button",
      className: "hermes-workflows-palette-card",
      "aria-label": "Add " + node[1],
      onClick: function () {
        trigger ? call(props.addTriggerOfType, type) : call(props.addWorkflowCellOfType, type);
      },
    }, dragProps(type)),
      h("span", { className: "hermes-workflows-palette-icon", "aria-hidden": "true" }, NODE_ICONS[type] || "•"),
      h("div", { className: "hermes-workflows-palette-text" },
        h("span", { className: "hermes-workflows-palette-title" }, node[1]),
        h("span", { className: "hermes-workflows-palette-desc" }, node[2])
      )
    );
  }

  function renderNodes() {
    const Fragment = React.Fragment || "div";
    const needle = String(search || "").trim().toLowerCase();
    const visibleCategories = needle
      ? NODE_CATEGORIES.map(function (category) {
          return {
            name: category.name,
            color: category.color,
            nodes: category.nodes.filter(function (node) {
              const type = String(node[0] || "").toLowerCase();
              const label = String(node[1] || "").toLowerCase();
              return type.includes(needle) || label.includes(needle);
            }),
          };
        }).filter(function (category) { return category.nodes.length > 0; })
      : NODE_CATEGORIES;
    return h("div", { className: "hermes-workflows-palette-panel hermes-workflows-node-library", role: "tabpanel", "aria-label": "Nodes" },
      h("p", { className: "hermes-workflows-muted" }, "Drag a node onto the canvas, or click to add it."),
      h("input", {
        type: "search",
        className: "hermes-workflows-palette-search-input",
        placeholder: "Search nodes...",
        "aria-label": "Search nodes",
        value: search,
        onChange: function (event) { setSearch(event.target.value); },
      }),
      visibleCategories.length
        ? visibleCategories.map(function (category) {
            return h(Fragment, { key: category.name },
              h("h3", { className: "hermes-workflows-palette-category", style: { color: NODE_COLORS[category.color] || "inherit" } }, category.name),
              h("div", { className: "hermes-workflows-node-palette" }, category.nodes.map(renderNodeCard))
            );
          })
        : h("p", { className: "hermes-workflows-muted" }, "No nodes match \u201c" + String(search) + "\u201d.")
    );
  }

  return h("aside", { className: "hermes-workflows-sidebar hermes-workflows-palette" },
    h("div", { className: "hermes-workflows-palette-tabs", role: "tablist", "aria-label": "Workflow palette" },
      h("button", {
        type: "button",
        role: "tab",
        className: "hermes-workflows-palette-tab" + (activeTab === "workflows" ? " is-active" : ""),
        "aria-selected": activeTab === "workflows" ? "true" : "false",
        onClick: function () { setActiveTab("workflows"); },
      }, "Workflows"),
      h("button", {
        type: "button",
        role: "tab",
        className: "hermes-workflows-palette-tab" + (activeTab === "nodes" ? " is-active" : ""),
        "aria-selected": activeTab === "nodes" ? "true" : "false",
        onClick: function () { setActiveTab("nodes"); },
      }, "Nodes")
    ),
    activeTab === "nodes" ? renderNodes() : h("div", { className: "hermes-workflows-palette-panel hermes-workflows-workflow-library", role: "tabpanel", "aria-label": "Workflows" },
      props.hideWorkflowForm ? null : renderWorkflowForm(h, props),
      renderDefinitions(),
      renderExecutions()
    )
  );
}

// ponytail: keep the legacy sidebar wrapper for existing callers; app.js uses
// WorkflowRail directly so its hooks run under React.
// Three disclosures, single scroll container, library groups delegate to the
// host's openNodePalette; the floating NodePaletteOverlay owns manual authoring.
export function renderWorkflowRail(props) {
  return WorkflowRail(props);
}

export function WorkflowRail(props) {
  const h = props.createElement;
  const React = props.React || {};
  const useState = React.useState || function (initial) { return [initial, function () {}]; };
  const definitions = Array.isArray(props.definitions) ? props.definitions : [];
  const executions = Array.isArray(props.executions) ? props.executions : [];
  const groups = Array.isArray(props.libraryGroups) ? props.libraryGroups : [];

  const call = function (handler) {
    if (typeof handler === "function") return handler.apply(null, Array.prototype.slice.call(arguments, 1));
  };

  const workflowsExpandedState = useState(true);
  const executionsExpandedState = useState(true);
  const libraryExpandedState = useState(true);
  const [workflowsExpanded, setWorkflowsExpanded] = workflowsExpandedState;
  const [executionsExpanded, setExecutionsExpanded] = executionsExpandedState;
  const [libraryExpanded, setLibraryExpanded] = libraryExpandedState;

  function disclosure(label, controlsId, expanded, setExpanded) {
    return h("button", {
      type: "button",
      className: "hermes-workflows-rail-disclosure" + (expanded ? " is-expanded" : ""),
      "aria-expanded": expanded ? "true" : "false",
      "aria-controls": controlsId,
      onClick: function () { setExpanded(!expanded); },
    },
      h("span", { className: "hermes-workflows-rail-disclosure-label" }, label),
      h("span", { className: "hermes-workflows-rail-disclosure-caret", "aria-hidden": "true" }, "\u25BE")
    );
  }

  const workflowsSection = h("section", { className: "hermes-workflows-rail-section hermes-workflows-rail-workflows" },
    disclosure("Workflows", "hermes-workflows-rail-workflows-panel", workflowsExpanded, setWorkflowsExpanded),
    workflowsExpanded ? h("div", { id: "hermes-workflows-rail-workflows-panel", className: "hermes-workflows-palette-panel" },
      props.hideWorkflowForm ? null : renderWorkflowForm(h, props),
      definitions.length ? h("div", { className: "hermes-workflows-sidebar-list" },
        definitions.map(function (definition) {
          const id = definition.workflow_id || definition.id;
          const key = selectionKey(definition);
          return h("button", {
            key: key,
            type: "button",
            className: "hermes-workflows-sidebar-item" + (key === selectionKey(props.selectedDefinition) ? " is-selected" : ""),
            onClick: function () { call(props.loadDefinition, id, definition.version); },
          },
            h("span", { className: "hermes-workflows-sidebar-item-title" }, safeString(definition.name || id)),
            h("span", { className: "hermes-workflows-sidebar-badge" + (definition.enabled ? " is-enabled" : "") }, definition.enabled ? "on" : "off")
          );
        })
      ) : h("p", { className: "hermes-workflows-muted" }, "No workflows deployed.")
    ) : null
  );

  const executionsSection = h("section", { className: "hermes-workflows-rail-section hermes-workflows-rail-executions" },
    disclosure("Executions", "hermes-workflows-rail-executions-panel", executionsExpanded, setExecutionsExpanded),
    executionsExpanded ? h("div", { id: "hermes-workflows-rail-executions-panel", className: "hermes-workflows-palette-panel" },
      executions.length ? h("div", { className: "hermes-workflows-sidebar-list" },
        executions.slice(0, 20).map(function (execution) {
          const id = safeString(execution.execution_id || execution.id);
          const status = safeString(execution.status);
          const statusClass = status === "succeeded" ? " is-succeeded" : status === "failed" ? " is-failed" : "";
          return h("button", {
            key: id,
            type: "button",
            className: "hermes-workflows-sidebar-item",
            onClick: function () { call(props.loadExecution, id); },
          },
            h("span", { className: "hermes-workflows-sidebar-item-title" }, id.slice(0, 16)),
            h("span", { className: "hermes-workflows-sidebar-badge" + statusClass }, status)
          );
        })
      ) : h("p", { className: "hermes-workflows-muted" }, "No executions yet.")
    ) : null
  );

  function safeId(name) {
    return safeString(name).replace(/[^a-z0-9_-]+/gi, "-").toLowerCase();
  }

  const libraryGroupsContent = groups.length ? h("div", { className: "hermes-workflows-rail-library-groups" },
    groups.map(function (group) {
      const label = safeString(group && group.name);
      const controlsId = "rail-library-group-" + safeId(label);
      const color = (group && group.color) || "inherit";
      const types = Array.isArray(group && group.types) ? group.types : [];
      return h("div", { key: label, className: "hermes-workflows-rail-library-group" },
        h("button", {
          type: "button",
          className: "hermes-workflows-rail-library-group-button",
          "aria-expanded": "false",
          "aria-controls": controlsId,
          onClick: function () { call(props.openNodePalette, label); },
        },
          h("span", { className: "hermes-workflows-rail-library-group-name", style: { color: NODE_COLORS[color] || "inherit" } }, label),
          h("span", { className: "hermes-workflows-rail-library-group-count", "aria-hidden": "true" }, String(types.length))
        ),
        h("span", { id: controlsId, className: "hermes-workflows-sr-only" }, types.join(", "))
      );
    })
  ) : h("p", { className: "hermes-workflows-muted" }, "No node library loaded.");

  const librarySection = h("section", { className: "hermes-workflows-rail-section hermes-workflows-rail-library" },
    disclosure("Library", "hermes-workflows-rail-library-panel", libraryExpanded, setLibraryExpanded),
    libraryExpanded ? h("div", { id: "hermes-workflows-rail-library-panel", className: "hermes-workflows-palette-panel" },
      libraryGroupsContent
    ) : null
  );

  return h("aside", { className: "hermes-workflows-sidebar hermes-workflows-rail" },
    workflowsSection,
    executionsSection,
    librarySection
  );
}

export function NodePaletteOverlay(props) {
  const h = props.createElement;
  const React = props.React || {};
  const useState = React.useState || function (initial) { return [initial, function () {}]; };
  const searchState = useState("");
  const search = searchState[0];
  const setSearch = searchState[1];
  if (!props.isOpen) return null;

  const groupName = safeString(props.nodePaletteGroup).trim().toLowerCase();
  const needle = safeString(search).trim().toLowerCase();
  const categories = (Array.isArray(props.categories) ? props.categories : NODE_CATEGORIES).map(function (category) {
    const nodes = Array.isArray(category && category.nodes) ? category.nodes.filter(function (node) {
      const type = safeString(node && node[0]).toLowerCase();
      const label = safeString(node && node[1]).toLowerCase();
      return (!needle || type.includes(needle) || label.includes(needle)) && isSupported(type, category && category.name === "Triggers");
    }) : [];
    return Object.assign({}, category, { nodes: nodes });
  }).filter(function (category) {
    return (!groupName || safeString(category && category.name).trim().toLowerCase() === groupName) && category.nodes.length > 0;
  });

  function call(handler) {
    if (typeof handler === "function") return handler.apply(null, Array.prototype.slice.call(arguments, 1));
  }

  function dragProps(type) {
    return {
      draggable: true,
      onDragStart: function (event) {
        if (event.dataTransfer && event.dataTransfer.setData) event.dataTransfer.setData("text/plain", type);
        if (typeof window !== "undefined") window.__HERMES_DRAG_NODE_TYPE = type;
      },
    };
  }

  function selectNode(type, trigger) {
    if (typeof props.onSelectNode === "function") return props.onSelectNode(type, trigger);
    if (trigger) return call(props.addTriggerOfType, type);
    return call(props.addWorkflowCellAtPosition, type);
  }

  function renderTile(category, node) {
    const type = node[0];
    const trigger = category.name === "Triggers";
    return h("button", Object.assign({
      key: type,
      type: "button",
      className: "hermes-workflows-palette-card hermes-workflows-node-palette-tile",
      "aria-label": "Add " + node[1],
      onClick: function () { selectNode(type, trigger); },
      style: { "--wf-palette-color": NODE_COLORS[category.color] || "inherit" },
    }, dragProps(type)),
      h("span", { className: "hermes-workflows-palette-icon", "aria-hidden": "true" }, NODE_ICONS[type] || "•"),
      h("span", { className: "hermes-workflows-palette-text" },
        h("span", { className: "hermes-workflows-palette-title" }, node[1]),
        h("span", { className: "hermes-workflows-palette-desc" }, node[2])
      )
    );
  }

  const Fragment = React.Fragment || "div";
  return h("div", { className: "hermes-workflows-node-palette-overlay", role: "dialog", "aria-label": "Add workflow node" },
    h("div", { className: "hermes-workflows-node-palette-panel" },
      h("div", { className: "hermes-workflows-node-palette-header" },
        h("h2", null, "Add Node"),
        h("button", { type: "button", "aria-label": "Close", onClick: props.closeNodePalette }, "Close")
      ),
      h("input", {
        type: "search",
        className: "hermes-workflows-palette-search-input",
        placeholder: "Search nodes...",
        "aria-label": "Search nodes",
        value: search,
        onChange: function (event) { setSearch(event.target.value); },
      }),
      categories.length ? categories.map(function (category) {
        return h(Fragment, { key: category.name },
          h("h3", { className: "hermes-workflows-palette-category", style: { color: NODE_COLORS[category.color] || "inherit" } }, category.name),
          h("div", { className: "hermes-workflows-node-palette" }, category.nodes.map(function (node) { return renderTile(category, node); }))
        );
      }) : h("p", { className: "hermes-workflows-muted" }, "No nodes match the current filter.")
    )
  );
}
