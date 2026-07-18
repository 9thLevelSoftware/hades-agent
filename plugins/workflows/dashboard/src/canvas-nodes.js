export const NODE_COLORS = {
  trigger: "#ff6d5a",
  pass: "#1abc9c",
  switch: "#3498db",
  agent_task: "#9b59b6",
  wait: "#f39c12",
  parallel: "#27ae60",
  join: "#2ecc71",
  fail: "#e74c3c",
  send_message: "#e67e22",
  subworkflow: "#2c3e50",
};

export const NODE_ICONS = {
  trigger: "⚡",
  manual: "▶",
  schedule: "⏰",
  webhook: "🔗",
  kanban_event: "📋",
  pass: "▣",
  switch: "◇",
  agent_task: "🤖",
  wait: "⏱",
  parallel: "⇉",
  join: "⇥",
  fail: "✕",
  send_message: "✉",
  subworkflow: "⎌",
};

function safeString(value) {
  if (value === null || value === undefined || value === "") return "—";
  return String(value);
}

function classSafe(value) {
  return String(value || "unknown").replace(/[^a-z0-9_-]+/gi, "-").toLowerCase();
}

export function makeWorkflowNode(kind, SDK) {
  const h = SDK.React.createElement;
  const FlowSDK = SDK.ReactFlow || SDK.reactFlow || {};
  const Handle = FlowSDK.Handle;
  const Position = FlowSDK.Position || { Left: "left", Right: "right" };
  const color = NODE_COLORS[kind] || "#64748b";

  return function WorkflowNode(props) {
    const data = (props && props.data) || {};
    const status = data.status || "idle";
    const node = data.node || {};
    const label = node.id || data.id;
    const icon = NODE_ICONS[node.trigger_type || node.triggerType || node.type || kind] || NODE_ICONS[kind] || "•";

    return h("div", {
      className: "hermes-workflows-rf-node is-" + classSafe(kind) + " is-status-" + classSafe(status),
      role: "button",
      tabIndex: 0,
      "aria-label": "Edit workflow cell " + safeString(label || "cell"),
      onClick: function (event) {
        event.stopPropagation();
        if (data.onSelect) data.onSelect(node);
      },
      onKeyDown: function (event) {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          event.stopPropagation();
          if (data.onSelect) data.onSelect(node);
        }
      },
    },
      Handle ? h(Handle, { type: "target", position: Position.Left }) : null,
      h("div", { className: "hermes-workflows-rf-node-header", style: { background: color } },
        h("span", { className: "hermes-workflows-rf-node-icon" }, icon),
        h("span", { className: "hermes-workflows-rf-node-label" }, safeString(label)),
      ),
      h("div", { className: "hermes-workflows-rf-node-body" },
        h("div", { className: "hermes-workflows-rf-node-type" }, String(kind).replace(/_/g, " ")),
        status && status !== "idle" ? h("div", { className: "hermes-workflows-rf-node-status" }, safeString(status)) : null,
      ),
      Handle ? h(Handle, { type: "source", position: Position.Right }) : null,
    );
  };
}
